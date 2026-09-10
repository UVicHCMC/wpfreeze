"""Owner-tasks report -- data layer.

Synthesises a bounded worklist for a *site owner* (not the archivist) from
the JSON that three earlier steps leave on disk:

  - broken-external-links.json  (checklinks) -> dead outbound links
  - build-report.json           (build)      -> broken internal links
  - manifest.json               (acquire)    -> missing media files

Same "read whatever is on disk" precedent as cleanup.py: this is a pure
function of those files, and a section whose source report is absent is
reported as un-run rather than silently empty (see `unrun_checks`).

Rendering lives in a later phase; this module only shapes the data.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from wpfreeze.manifest import Status

if TYPE_CHECKING:
    from wpfreeze.cli import SiteConfig

logger = logging.getLogger(__name__)

OWNER_TASKS_FILENAME = "owner-tasks.html"

# Extensions an owner could plausibly still have a copy of. Everything else
# a "missing" record points at -- bare ?attachment_id= URLs, stylesheets,
# scripts, fonts -- is wpfreeze's problem, not theirs, and lands in the
# handled-for-you footnote instead (see _classify_missing).
_MEDIA_EXTS = frozenset(
    {
        "jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tif", "tiff",
        "mp3", "wav", "m4a", "aac", "ogg", "flac",
        "mp4", "mov", "avi", "webm", "mkv", "m4v", "wmv",
        "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "odt", "ods", "odp",
    }
)

# WordPress writes resize variants next to the original as `name-WxH.ext`.
# A missing variant and its missing original are one ask, not two -- we
# regenerate the sizes from the original. Anchored at the extension so
# `photo-2x4-lumber.jpg` (an `x` that is not a dimension) does not match.
_RESIZE_VARIANT_RE = re.compile(r"^(.+)-\d+x\d+(\.[A-Za-z0-9]+)$")

_ID_PREFIX = {"external_link": "ext", "internal_link": "int", "missing_file": "mis"}


class OwnerTasksInputMissing(Exception):
    """`broken-external-links.json` is absent. The caller should tell the
    user to run `wpfreeze checklinks` first and exit 2 -- the same shape
    `checklinks --recheck` uses for a missing `external-links.json`."""


@dataclass
class OwnerTask:
    id: str  # stable across runs: hash of type + target
    type: str  # "external_link" | "internal_link" | "missing_file"
    target: str
    pages: list[str] = field(default_factory=list)
    detected: dict = field(default_factory=dict)  # {"status": .., "reason": ..}
    context: str | None = None  # the link text, for internal links
    group: str | None = None  # "dead" | "authgated", external links only
    filename: str | None = None  # missing files
    upload_path: str | None = None  # missing files: original server path
    variants: list[str] = field(default_factory=list)  # folded resize variants


@dataclass
class OwnerTasks:
    project: str
    base_url: str
    generated: str  # ISO 8601, when this report was built
    capture_finished: str | None  # report.json summary.run_finished
    checked_at: str | None  # broken-external-links.json checked_at
    tasks: list[OwnerTask] = field(default_factory=list)
    # Section keys whose source report was absent -- rendered as "this
    # check has not been run", not as "nothing to do".
    unrun_checks: list[str] = field(default_factory=list)
    # Count of "missing" records that do not need the owner (the footnote).
    handled_missing_count: int = 0
    # Total external links checked (broken + live), for the section-1 prose.
    external_checked_count: int = 0
    # sha256 of each source report, so a returned response file can be
    # reconciled against the exact capture it was generated from.
    source_hashes: dict[str, str | None] = field(default_factory=dict)

    def by_type(self, task_type: str) -> list[OwnerTask]:
        return [t for t in self.tasks if t.type == task_type]

    def by_group(self, group: str) -> list[OwnerTask]:
        return [t for t in self.tasks if t.group == group]


def _load_json(path: Path) -> dict | None:
    """A report, or None if it is absent, unreadable, malformed, or not a
    JSON object -- every caller reads None as "this step hasn't run".
    Mirrors cleanup.py's own loader (copied, not imported, for the same
    reason its comment gives)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _task_id(task_type: str, target: str) -> str:
    digest = hashlib.sha256(f"{task_type}\x00{target}".encode("utf-8")).hexdigest()[:6]
    return f"{_ID_PREFIX[task_type]}-{digest}"


def _basename(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


def _url_extension(url: str) -> str:
    name = _basename(url)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _pages_from_discovered_via(discovered_via: list[str], page_paths: dict[str, str]) -> list[str]:
    """The pages that referenced a missing file, from its `discovered_via`
    entries (`"crawl:<page-url>"`). Resolved to the page's site-relative
    output path where the manifest knows it, left as the raw URL where it
    does not."""
    pages: set[str] = set()
    for entry in discovered_via:
        if entry.startswith("crawl:"):
            page_url = entry[len("crawl:"):]
            pages.add(page_paths.get(page_url, page_url))
    return sorted(pages)


def _classify_missing(records: list[dict]) -> tuple[list[dict], int]:
    """Partition `status == "missing"` records into owner-relevant media
    and everything else (returned only as a count -- the footnote needs
    the number, not the list)."""
    media: list[dict] = []
    noise = 0
    for record in records:
        if record.get("status") != Status.MISSING.value:
            continue
        if _url_extension(record.get("url", "")) in _MEDIA_EXTS:
            media.append(record)
        else:
            noise += 1
    return media, noise


def _group_resize_variants(media: list[dict]) -> list[dict]:
    """Fold each missing resize variant into its missing original. A
    variant whose original is not itself missing stays as its own entry --
    the owner still has to supply something. Deterministic: entries keep
    first-seen order, variant lists are sorted."""
    by_name: dict[str, dict] = {}
    for record in media:
        by_name.setdefault(_basename(record.get("url", "")), record)

    entries: dict[str, dict] = {}
    for name, record in by_name.items():
        match = _RESIZE_VARIANT_RE.match(name)
        original = f"{match.group(1)}{match.group(2)}" if match else name
        if match and original in by_name:
            entry = entries.setdefault(original, _new_entry(by_name[original]))
            entry["variants"].append(name)
        else:
            entries.setdefault(name, _new_entry(record))

    for entry in entries.values():
        entry["variants"].sort()
    return list(entries.values())


def _new_entry(record: dict) -> dict:
    url = record.get("url", "")
    return {
        "url": url,
        "filename": _basename(url),
        "upload_path": urlsplit(url).path or url,
        "discovered_via": list(record.get("discovered_via", [])),
        "http_status": record.get("http_status"),
        "variants": [],
    }


def load_tasks(output_dir: Path, config: "SiteConfig") -> OwnerTasks:
    """Build the worklist from whatever JSON is in `output_dir`.

    Raises OwnerTasksInputMissing if `broken-external-links.json` is
    absent -- section 1 is the spine of the report and there is nothing
    useful to emit without it. `build-report.json` or `manifest.json`
    being absent is survivable: that section is recorded in
    `unrun_checks` and omitted."""
    ext_data = _load_json(output_dir / "broken-external-links.json")
    if ext_data is None:
        raise OwnerTasksInputMissing(
            f"No {output_dir / 'broken-external-links.json'} found; run "
            "`wpfreeze checklinks` first."
        )

    tasks: list[OwnerTask] = []
    unrun: list[str] = []

    # Section 1 -- dead outbound links.
    for result in ext_data.get("results", []):
        if result.get("ok"):
            continue
        reason = result.get("reason") or ""
        tasks.append(
            OwnerTask(
                id=_task_id("external_link", result["url"]),
                type="external_link",
                target=result["url"],
                pages=sorted(result.get("pages", [])),
                detected={"status": result.get("status"), "reason": result.get("reason")},
                group="authgated" if "auth-gated" in reason else "dead",
            )
        )

    # Section 2 -- broken internal links.
    build_report = _load_json(output_dir / "build-report.json")
    if build_report is None:
        unrun.append("internal_links")
    else:
        for sample in build_report.get("unresolved_samples", []):
            page = (sample.get("page_output") or "").lstrip("/")
            tasks.append(
                OwnerTask(
                    id=_task_id("internal_link", sample["target"]),
                    type="internal_link",
                    target=sample["target"],
                    pages=[page] if page else [],
                    context=sample.get("context") or None,
                )
            )

    # Section 3 -- missing media files.
    manifest_data = _load_json(output_dir / "manifest.json")
    handled_missing = 0
    if manifest_data is None:
        unrun.append("missing_files")
    else:
        records = manifest_data.get("records", [])
        page_paths = {
            record["url"]: (record.get("output_path") or "").lstrip("/")
            for record in records
            if record.get("output_path") and record.get("url")
        }
        media, handled_missing = _classify_missing(records)
        for entry in _group_resize_variants(media):
            tasks.append(
                OwnerTask(
                    id=_task_id("missing_file", entry["url"]),
                    type="missing_file",
                    target=entry["url"],
                    pages=_pages_from_discovered_via(entry["discovered_via"], page_paths),
                    detected={"status": entry["http_status"]},
                    filename=entry["filename"],
                    upload_path=entry["upload_path"],
                    variants=entry["variants"],
                )
            )

    report = _load_json(output_dir / "report.json")
    capture_finished = None
    if report and isinstance(report.get("summary"), dict):
        capture_finished = report["summary"].get("run_finished")

    return OwnerTasks(
        project=config.name,
        base_url=config.base_url,
        generated=datetime.now(timezone.utc).isoformat(),
        capture_finished=capture_finished,
        checked_at=ext_data.get("checked_at"),
        tasks=tasks,
        unrun_checks=unrun,
        handled_missing_count=handled_missing,
        external_checked_count=len(ext_data.get("results", [])),
        source_hashes={
            "broken_external_links": _sha256(output_dir / "broken-external-links.json"),
            "build_report": _sha256(output_dir / "build-report.json"),
        },
    )


# ---------------------------------------------------------------------------
# Rendering -- phase 3: semantic markup only.
#
# _CSS is empty and there is no <script> beyond the JSON data island. The
# stylesheet and the interaction layer (action-driven field reveal,
# progress, save-to-JSON, rehydrate-from-JSON, clipboard) are a later
# phase. The markup here is the contract that phase attaches to: `data-`
# attributes and stable class names, not element order.
# ---------------------------------------------------------------------------

_CSS = ""

# Owner-facing labels are fixed copy -- do not paraphrase. `value` is the
# stable enum written into the response file.
_LINK_ACTIONS: tuple[tuple[str, str], ...] = (
    ("", "Choose an action…"),
    ("replace", "I have a new address for this"),
    ("wayback", "Use an archived copy from the Wayback Machine"),
    ("unlink", "Keep the words, remove the link"),
    ("unlink_note", 'Keep the words, remove the link, add "(no longer available)"'),
    ("remove", "Delete the link and its text"),
    ("keep", "Leave it as it is"),
    ("defer", "I do not know yet -- come back to this"),
)
_MISSING_ACTIONS: tuple[tuple[str, str], ...] = (
    ("", "Choose an action…"),
    ("will_send", "I will send you this file"),
    ("replace", "It is online somewhere else"),
    ("remove", "Remove it from the page"),
    ("placeholder", "I cannot find it -- use a placeholder"),
    ("defer", "I need to look for it"),
)

_URL_PLACEHOLDER = "https://…"
_NOTE_PLACEHOLDER = "Anything we should know (optional)"
_WAYBACK_PREFIX = "https://web.archive.org/web/2020/"


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _host_label(base_url: str) -> str:
    host = urlsplit(base_url).hostname or base_url
    return host[4:] if host.startswith("www.") else host


def _page_link_html(page: str) -> str:
    """A page reference -> a link to the local built copy. A resolved
    site-relative path sits under `site/`; an unresolved raw page URL is
    linked as-is."""
    if "://" in page:
        return f'<a href="{_esc(page)}">{_esc(page)}</a>'
    return f'<a href="{_esc("site/" + page)}">{_esc(page)}</a>'


def _pages_details_html(pages: list[str], prefix: str) -> str:
    if not pages:
        return ""
    items = "".join(f"<li>{_page_link_html(p)}</li>" for p in pages)
    noun = "page" if len(pages) == 1 else "pages"
    return (
        f'<details class="task-pages"><summary>{_esc(prefix)} {len(pages)} {noun}</summary>'
        f"<ul>{items}</ul></details>"
    )


def _select_html(options: tuple[tuple[str, str], ...], default: str) -> str:
    parts = [f'<select class="task-action" data-default="{_esc(default)}">']
    for value, label in options:
        selected = " selected" if value == default else ""
        parts.append(f'<option value="{_esc(value)}"{selected}>{_esc(label)}</option>')
    parts.append("</select>")
    return "".join(parts)


def _action_inputs_html() -> str:
    return (
        f'<input class="task-url" type="url" placeholder="{_esc(_URL_PLACEHOLDER)}" hidden>'
        f'<input class="task-note" type="text" placeholder="{_esc(_NOTE_PLACEHOLDER)}" hidden>'
    )


def _link_task_html(task: OwnerTask) -> str:
    """Sections 1 and 2 share this anatomy. Auth-gated rows default to
    `keep` -- and mark that option `selected` so the page is still correct
    with no JavaScript."""
    default = "keep" if task.group == "authgated" else ""
    status = task.detected.get("status")
    attrs = [
        'class="task"',
        f'data-item-id="{_esc(task.id)}"',
        f'data-task-type="{_esc(task.type)}"',
        f'data-status="{_esc("" if status is None else status)}"',
        f'data-wayback-url="{_esc(_WAYBACK_PREFIX + task.target)}"',
    ]
    if task.group:
        attrs.append(f'data-group="{_esc(task.group)}"')

    meta = task.detected.get("reason") or task.context or ""
    parts = [f"<article {' '.join(attrs)}>"]
    parts.append(f'<p class="task-target">{_esc(task.target)}</p>')
    if meta:
        parts.append(f'<p class="task-meta">{_esc(meta)}</p>')
    parts.append(_pages_details_html(task.pages, "Used on"))
    parts.append(f'<label class="task-action-label">What to do {_select_html(_LINK_ACTIONS, default)}</label>')
    parts.append(_action_inputs_html())
    parts.append("</article>")
    return "".join(parts)


def _missing_task_html(task: OwnerTask) -> str:
    status = task.detected.get("status")
    attrs = [
        'class="task"',
        f'data-item-id="{_esc(task.id)}"',
        'data-task-type="missing_file"',
        f'data-status="{_esc("" if status is None else status)}"',
    ]
    meta = f"Was at <code>{_esc(task.upload_path)}</code>"
    if task.pages:
        meta += " · Needed by " + ", ".join(_page_link_html(p) for p in task.pages)

    parts = [f"<article {' '.join(attrs)}>"]
    parts.append(f'<h3 class="task-filename">{_esc(task.filename)}</h3>')
    parts.append(f'<p class="task-meta">{meta}</p>')
    if task.variants:
        parts.append(
            '<p class="task-variant-note">We only need the original. '
            "We will regenerate the smaller sizes ourselves.</p>"
        )
    parts.append(f'<label class="task-action-label">What to do {_select_html(_MISSING_ACTIONS, "")}</label>')
    parts.append(_action_inputs_html())
    parts.append("</article>")
    return "".join(parts)


def _unrun_section_html(section_id: str, section_key: str, heading: str, note: str) -> str:
    return (
        f'<section id="{section_id}" data-section="{section_key}" data-unrun="true">'
        f"<h2>{_esc(heading)}</h2>"
        f'<p class="section-desc">{_esc(note)}</p>'
        "</section>"
    )


def _section_external_html(tasks: OwnerTasks) -> str:
    dead = tasks.by_group("dead")
    authgated = tasks.by_group("authgated")
    total = len(dead) + len(authgated)
    date = (tasks.checked_at or "")[:10]
    dead_rows = "".join(_link_task_html(t) for t in dead) or "<p>None.</p>"
    auth_rows = "".join(_link_task_html(t) for t in authgated) or "<p>None.</p>"
    return f"""
<section id="section-external" data-section="external_links">
  <h2>Links to other websites that no longer work</h2>
  <p class="section-desc">Your pages link out to {tasks.external_checked_count} addresses on
    other websites. We tried all of them on {_esc(date)}; {total} did not load. Tell us
    what to do with each one. A few may simply have been having a bad day.</p>
  <div class="task-group" data-group="dead">
    <h3>Confirmed dead ({len(dead)})</h3>
    <p class="section-desc">These returned a "not found" error, or the website they point
      to no longer exists at all.</p>
    {dead_rows}
  </div>
  <details class="task-group" data-group="authgated">
    <summary>Probably fine -- these need a login or a subscription ({len(authgated)})</summary>
    <p class="section-desc">These refused our automated check, which is normal for journal
      articles, library databases and members-only pages -- they most likely work
      perfectly for a real visitor. We have set them all to "leave as is". Change one only
      if you know it is genuinely dead.</p>
    {auth_rows}
  </details>
</section>
"""


def _section_internal_html(tasks: OwnerTasks) -> str:
    if "internal_links" in tasks.unrun_checks:
        return _unrun_section_html(
            "section-internal",
            "internal_links",
            "Links to pages on your own site that do not exist",
            "This check has not been run yet, so this section is empty. Run "
            "`wpfreeze build` to populate it.",
        )
    rows = "".join(_link_task_html(t) for t in tasks.by_type("internal_link"))
    return f"""
<section id="section-internal" data-section="internal_links">
  <h2>Links to pages on your own site that do not exist</h2>
  <p class="section-desc">These point at pages on your own website that we could not find.
    Usually that means a typo in the address, or a page that was deleted at some point.
    You will often recognise the intended page straight away.</p>
  {rows or "<p>None found.</p>"}
</section>
"""


def _section_missing_html(tasks: OwnerTasks) -> str:
    if "missing_files" in tasks.unrun_checks:
        return _unrun_section_html(
            "section-missing",
            "missing_files",
            "Files we could not download",
            "This check has not been run yet, so this section is empty. Run "
            "`wpfreeze acquire` first.",
        )
    rows = "".join(_missing_task_html(t) for t in tasks.by_type("missing_file"))
    return f"""
<section id="section-missing" data-section="missing_files">
  <h2>Files we could not download</h2>
  <p class="section-desc">Your pages refer to these images and media files, but the files
    themselves are no longer on the server, so we had nothing to copy. If you still have
    the originals anywhere, send them to us and we will put them back.</p>
  <p class="delivery-instruction">Send the files whatever way suits you -- email, a USB
    stick, OneDrive, anything. <strong>Please keep the filenames exactly as they appear
    below</strong>: that is how we match each file back to the page that needs it.</p>
  <button type="button" class="copy-filenames">Copy the list of filenames</button>
  {rows or "<p>None.</p>"}
</section>
"""


def _footnote_html(tasks: OwnerTasks) -> str:
    if not tasks.handled_missing_count:
        return ""
    return f"""
<details id="handled-footnote">
  <summary>Things we already handled (no action needed)</summary>
  <p>For completeness: we also found {tasks.handled_missing_count} other missing items
    that do not need you -- WordPress thumbnail stubs, stylesheets and a font -- which we
    either regenerated or safely left out.</p>
</details>
"""


def _task_payload(task: OwnerTask) -> dict:
    payload: dict = {
        "id": task.id,
        "type": task.type,
        "target": task.target,
        "pages": task.pages,
        "detected": task.detected,
        "action": "keep" if task.group == "authgated" else None,
        "note": "",
    }
    if task.type == "external_link":
        payload["group"] = task.group
        payload["replacement_url"] = ""
        payload["wayback_url"] = _WAYBACK_PREFIX + task.target
    elif task.type == "internal_link":
        payload["context"] = task.context
        payload["replacement_url"] = ""
        payload["wayback_url"] = _WAYBACK_PREFIX + task.target
    elif task.type == "missing_file":
        payload["filename"] = task.filename
        payload["upload_path"] = task.upload_path
        payload["variants"] = task.variants
        payload["replacement_url"] = ""
    return payload


def _data_island_html(tasks: OwnerTasks) -> str:
    """The task list, as the response-file skeleton. Phase 4's script reads
    this, fills `action`/`replacement_url`/`note` as the owner works, and
    serialises it back out on save."""
    payload = {
        "schema": "wpfreeze/owner-response@1",
        "project": tasks.project,
        "base_url": tasks.base_url,
        "report_generated": tasks.generated,
        "responded_at": None,
        "respondent": None,
        "source_reports": {
            "broken_external_links_sha256": tasks.source_hashes.get("broken_external_links"),
            "build_report_sha256": tasks.source_hashes.get("build_report"),
            "capture_run_finished": tasks.capture_finished,
        },
        "counts": {"total": len(tasks.tasks), "answered": 0, "deferred": 0},
        "items": [_task_payload(t) for t in tasks.tasks],
    }
    # Inside a <script> element the HTML parser scans raw text for `</script`,
    # `<script` and `<!--` before it ever sees JSON. Escaping every `<` (and
    # `>` for symmetry) to its \uXXXX form keeps the data inert regardless of
    # what a URL or link text contains. json.dumps is ASCII-only, so these
    # are the only `<`/`>` in the string.
    body = json.dumps(payload, indent=2).replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<script type="application/json" id="owner-tasks-data">\n{body}\n</script>'


def render_owner_tasks_html(tasks: OwnerTasks) -> str:
    site = _host_label(tasks.base_url)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(site)} -- what we need from you</title>
<style>{_CSS}</style>
</head>
<body>
<p class="open-in-browser">Save this file and open it in a web browser. If you are reading
  this in an email preview pane, the buttons below will not work.</p>
<h1>What we need from you</h1>
<p class="intro">We have made a complete standalone copy of {_esc(site)}. It is finished
  except for a handful of things only you can settle. Nothing you do on this page touches
  your live website -- it only records your decisions, and when you are done you will send
  us the file it produces.</p>

<div class="owner-progress">
  <span class="progress-count">0 of {len(tasks.tasks)} handled</span>
  <button type="button" class="save-button">Save my answers</button>
</div>

{_section_external_html(tasks)}
{_section_internal_html(tasks)}
{_section_missing_html(tasks)}
{_footnote_html(tasks)}

{_data_island_html(tasks)}
</body>
</html>
"""


def write_owner_tasks(tasks: OwnerTasks, output_dir: Path, path: Path | None = None) -> Path:
    dest = path or (output_dir / OWNER_TASKS_FILENAME)
    dest.write_text(render_owner_tasks_html(tasks), encoding="utf-8")
    return dest
