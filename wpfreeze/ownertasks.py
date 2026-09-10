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
# Rendering.
#
# The page is one self-contained file opened from a desktop, often over
# file://, possibly with no network at all: no frameworks, no web fonts,
# no external requests of any kind. The palette and font stack follow
# linkcheck.py's own _CSS so this looks related to the reports that
# accompany it.
#
# The markup is the contract the script attaches to -- `data-` attributes
# and stable class names, never element order. The whole progress
# affordance is that an answered card visibly recedes; the bar at the top
# is secondary to that.
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #fff; --fg: #1a1a1a; --muted: #5a5a5a; --line: #ddd;
  --card: #fff; --card-line: #d9d9d9;
  --todo: #7a5200; --todo-bg: #fff9ec;
  --done: #14591c; --done-bg: #f0f7f1;
  --link: #1a56c4; --visited: #7a3fa0;
}
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  color: var(--fg); background: var(--bg);
  max-width: 60rem; margin: 0 auto; padding: 0 1.25rem 4rem;
  line-height: 1.55;
}
a { color: var(--link); }
a:visited { color: var(--visited); }
h1 { font-size: 1.8rem; margin: 1.5rem 0 0.5rem; }
h2 { font-size: 1.3rem; margin: 0 0 0.4rem; }
h3 { font-size: 1.05rem; margin: 1.5rem 0 0.3rem; }
code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.9em; }

.open-in-browser {
  margin: 1.25rem 0 0; padding: 0.6rem 0.9rem;
  background: var(--todo-bg); border: 1px solid var(--todo);
  border-radius: 4px; color: var(--todo); font-size: 0.92rem;
}
.intro { margin: 0.5rem 0 1.25rem; }

.owner-progress {
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; gap: 0.9rem; flex-wrap: wrap;
  padding: 0.7rem 0; margin-bottom: 1.5rem;
  background: var(--bg); border-bottom: 1px solid var(--line);
}
.progress-track {
  flex: 1 1 10rem; min-width: 7rem; height: 8px;
  background: var(--line); border-radius: 4px; overflow: hidden;
}
.progress-track > span { display: block; height: 100%; width: 0; background: var(--done); }
.progress-count { font-variant-numeric: tabular-nums; font-size: 0.92rem; color: var(--muted); }

button {
  font: inherit; font-size: 0.92rem; padding: 0.4rem 0.9rem;
  border: 1px solid var(--card-line); border-radius: 4px;
  background: var(--card); color: var(--fg); cursor: pointer;
}
button:hover { border-color: var(--muted); }
.save-button { font-weight: 600; }

section { margin: 2.5rem 0; }
.section-desc { color: var(--muted); margin: 0 0 1rem; max-width: 46rem; }
.delivery-instruction { margin: 0 0 1rem; max-width: 46rem; }

.task {
  border: 1px solid var(--card-line); border-left: 4px solid var(--todo);
  border-radius: 5px; padding: 0.8rem 1rem; margin: 0.7rem 0;
  background: var(--card);
}
.task[data-answered="true"] {
  border-left-color: var(--done); background: var(--done-bg); opacity: 0.66;
}
.task[data-answered="true"]:hover, .task:focus-within { opacity: 1; }
.task-target, .task-filename {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  overflow-wrap: anywhere; margin: 0 0 0.25rem;
}
.task-target { font-size: 0.95rem; }
.task-filename { font-size: 1.15rem; font-weight: 600; }
/* The reason string can be a whole urllib traceback line with no spaces
   in it (host='...', port=80) -- without this it pushes the page wider
   than a phone screen. */
.task-meta { color: var(--muted); font-size: 0.88rem; margin: 0 0 0.5rem; overflow-wrap: anywhere; }
.task-variant-note { color: var(--todo); font-size: 0.88rem; margin: 0 0 0.5rem; }
.task-pages { margin: 0 0 0.5rem; font-size: 0.88rem; }
.task-pages summary { cursor: pointer; color: var(--muted); }
.task-pages ul { margin: 0.4rem 0 0; padding-left: 1.2rem; max-height: 12rem; overflow-y: auto; }
.task-pages li { overflow-wrap: anywhere; }

.task-action-label { display: block; font-size: 0.92rem; margin-top: 0.5rem; }
.task-action, .task-url, .task-note, #respondent {
  font: inherit; font-size: 0.92rem; padding: 0.35rem 0.45rem;
  border: 1px solid var(--card-line); border-radius: 4px;
  background: var(--card); color: var(--fg);
}
.task-action { margin-left: 0.4rem; max-width: 100%; }
.task-url, .task-note { display: block; width: 100%; margin-top: 0.45rem; }
/* An author `display` rule outranks the UA stylesheet's [hidden]{display:none},
   so without this the reveal fields are visible on every unanswered card. */
[hidden] { display: none !important; }

.task-group { margin-bottom: 2rem; }
details.task-group > summary {
  cursor: pointer; font-weight: 600; color: var(--muted);
  padding: 0.5rem 0; border-top: 1px solid var(--line);
}

.review { border-top: 1px solid var(--line); padding-top: 1rem; }
.review-body dl { margin: 0; }
.review-body dt { font-weight: 600; margin-top: 0.9rem; font-size: 0.95rem; }
.review-body dd { margin: 0.2rem 0 0 1.1rem; font-size: 0.88rem; color: var(--muted); }
.review-body ul { margin: 0.2rem 0 0; padding-left: 1.3rem; font-size: 0.88rem; }
.review-body li { overflow-wrap: anywhere; margin: 0.1rem 0; }
.review-empty { color: var(--muted); }

.save-area { margin: 2rem 0; padding: 1rem; border: 1px solid var(--card-line); border-radius: 5px; }
.respondent-label { display: block; font-size: 0.92rem; margin-bottom: 0.7rem; }
#respondent { display: block; margin-top: 0.3rem; width: 100%; max-width: 22rem; }
.save-status { margin: 0.7rem 0 0; font-size: 0.92rem; color: var(--done); }

.resume { margin: 1.5rem 0; padding: 0.9rem 1rem; border: 1px dashed var(--card-line); border-radius: 5px; }
.resume.dragover { border-color: var(--link); background: var(--todo-bg); }
.resume p { margin: 0 0 0.5rem; font-size: 0.92rem; color: var(--muted); }
.resume-status { font-size: 0.88rem; }

#handled-footnote { margin-top: 2.5rem; font-size: 0.92rem; color: var(--muted); }
#handled-footnote summary { cursor: pointer; }

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1e1e1e; --fg: #ddd; --muted: #a4a4a4; --line: #444;
    --card: #262626; --card-line: #454545;
    --todo: #ffcc70; --todo-bg: #33280f;
    --done: #8fd89a; --done-bg: #1c2a1e;
    --link: #7db3ff; --visited: #d3a6f0;
  }
}

@media print {
  body { max-width: none; padding: 0; }
  .owner-progress { position: static; border-bottom: none; }
  .save-button, .copy-filenames, .resume, .save-status { display: none; }
  .task { break-inside: avoid; opacity: 1 !important; page-break-inside: avoid; }
  .task-pages ul { max-height: none; overflow: visible; }
  a { color: inherit; }
}
"""

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
_URL_LABEL = "Replacement address"
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


def _action_inputs_html(task_id: str) -> str:
    """Both fields carry an id and an aria-label: `placeholder` alone is not
    an accessible name, and a form field with neither id nor name is also
    what browsers warn about."""
    return (
        f'<input class="task-url" id="url-{_esc(task_id)}" name="url-{_esc(task_id)}" type="url" '
        f'aria-label="{_esc(_URL_LABEL)}" placeholder="{_esc(_URL_PLACEHOLDER)}" hidden>'
        f'<input class="task-note" id="note-{_esc(task_id)}" name="note-{_esc(task_id)}" type="text" '
        f'aria-label="{_esc(_NOTE_PLACEHOLDER)}" placeholder="{_esc(_NOTE_PLACEHOLDER)}" hidden>'
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
    parts.append(_action_inputs_html(task.id))
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
    parts.append(_action_inputs_html(task.id))
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


# The interaction layer. The JSON island is the single source of truth:
# every control writes into it and Save serialises it back out, so the
# saved file and the page can never disagree.
#
# Action *labels* are read back out of the rendered <option>s rather than
# repeated here -- the owner-facing copy has exactly one home, the Python
# constants above.
#
# Everything degrades: no island or a parse failure leaves the static page
# working, localStorage is wrapped (Chrome gives a file:// page an opaque
# origin and throws), and the clipboard falls back to execCommand because
# navigator.clipboard needs a secure context that file:// is not.
_JS = """
(function () {
  "use strict";

  var island = document.getElementById("owner-tasks-data");
  if (!island) return;
  var data;
  try { data = JSON.parse(island.textContent); } catch (e) { return; }
  if (!data || !Array.isArray(data.items)) return;

  var byId = Object.create(null);
  data.items.forEach(function (item) { byId[item.id] = item; });

  // Which extra fields each action asks for. Mirrors the reveal map in the
  // plan's markup contract; anything absent shows neither field.
  var NEEDS_URL = { replace: 1 };
  var NEEDS_NOTE = { replace: 1, unlink_note: 1, keep: 1, defer: 1, will_send: 1, placeholder: 1 };

  var all = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  // Labels are per task type, not per action value: sections 1/2 and
  // section 3 both use `replace`, `remove` and `defer` with deliberately
  // different owner-facing wording ("Delete the link and its text" vs
  // "Remove it from the page"). A single flat map silently relabels every
  // link answer with the file wording.
  var ACTION_LABELS = {};
  all(".task").forEach(function (art) {
    var type = art.getAttribute("data-task-type");
    if (!type || ACTION_LABELS[type]) return;
    var select = art.querySelector(".task-action");
    if (!select) return;
    var labels = {};
    Array.prototype.forEach.call(select.options, function (opt) {
      if (opt.value) labels[opt.value] = opt.textContent;
    });
    ACTION_LABELS[type] = labels;
  });

  function labelFor(item) {
    var byType = ACTION_LABELS[item.type] || {};
    return byType[item.action] || item.action;
  }

  var articles = all(".task");
  var dirty = false;

  // --- per-card wiring ------------------------------------------------
  articles.forEach(function (art) {
    var item = byId[art.getAttribute("data-item-id")];
    if (!item) return;
    var select = art.querySelector(".task-action");
    var urlInput = art.querySelector(".task-url");
    var noteInput = art.querySelector(".task-note");
    if (!select) return;

    function reveal() {
      var action = select.value;
      if (urlInput) { urlInput.hidden = !NEEDS_URL[action]; urlInput.required = !!NEEDS_URL[action]; }
      if (noteInput) { noteInput.hidden = !NEEDS_NOTE[action]; }
      art.setAttribute("data-answered", action ? "true" : "false");
    }

    art.syncFromState = function () {
      select.value = item.action || "";
      if (urlInput) urlInput.value = item.replacement_url || "";
      if (noteInput) noteInput.value = item.note || "";
      reveal();
    };

    select.addEventListener("change", function () {
      item.action = select.value || null;
      if (select.value === "wayback") {
        // The prefill: no field to fill in, the archived address is derived.
        item.replacement_url = art.getAttribute("data-wayback-url") || "";
      } else if (!NEEDS_URL[select.value]) {
        item.replacement_url = "";
      }
      reveal();
      touched();
    });
    if (urlInput) urlInput.addEventListener("input", function () {
      item.replacement_url = urlInput.value; touched();
    });
    if (noteInput) noteInput.addEventListener("input", function () {
      item.note = noteInput.value; touched();
    });

    art.syncFromState();
  });

  // --- progress + review ----------------------------------------------
  function answered() {
    var n = 0;
    data.items.forEach(function (i) { if (i.action) n++; });
    return n;
  }

  function updateProgress() {
    var done = answered(), total = data.items.length;
    all(".progress-count").forEach(function (el) {
      el.textContent = done + " of " + total + " handled";
    });
    all(".progress-track > span").forEach(function (el) {
      el.style.width = total ? (done * 100 / total) + "%" : "0";
    });
  }

  function renderReview() {
    var box = document.querySelector(".review-body");
    if (!box) return;
    // Keyed by type *and* action so each group can carry its own wording;
    // unanswered items collapse into one group across all three sections.
    var groups = {}, titles = {}, order = [];
    data.items.forEach(function (i) {
      var key = i.action ? i.type + "|" + i.action : "";
      if (!groups[key]) {
        groups[key] = [];
        titles[key] = i.action ? labelFor(i) : "Not yet answered";
        order.push(key);
      }
      groups[key].push(i);
    });
    order.sort(function (a, b) {
      if (!a) return 1;
      if (!b) return -1;
      return titles[a] < titles[b] ? -1 : 1;
    });
    if (!order.length) { box.textContent = ""; return; }
    var dl = document.createElement("dl");
    order.forEach(function (key) {
      var items = groups[key];
      var dt = document.createElement("dt");
      dt.textContent = titles[key] + " (" + items.length + ")";
      dl.appendChild(dt);
      var dd = document.createElement("dd");
      var ul = document.createElement("ul");
      items.forEach(function (i) {
        var li = document.createElement("li");
        li.textContent = i.filename || i.target;
        if (i.replacement_url) li.textContent += "  \\u2192  " + i.replacement_url;
        if (i.note) li.textContent += "  (" + i.note + ")";
        ul.appendChild(li);
      });
      dd.appendChild(ul);
      dl.appendChild(dd);
    });
    box.textContent = "";
    box.appendChild(dl);
  }

  function refresh() { updateProgress(); renderReview(); }
  function touched() { dirty = true; refresh(); persist(); }

  // --- save -------------------------------------------------------------
  function saveFilename() {
    return (data.project || "site") + "-owner-response-" +
      new Date().toISOString().slice(0, 10) + ".json";
  }

  function buildResponse() {
    var out = JSON.parse(JSON.stringify(data));
    var name = document.getElementById("respondent");
    out.responded_at = new Date().toISOString();
    out.respondent = name && name.value.trim() ? name.value.trim() : null;
    var done = 0, deferred = 0;
    out.items.forEach(function (i) {
      if (i.action) done++;
      if (i.action === "defer") deferred++;
    });
    out.counts = { total: out.items.length, answered: done, deferred: deferred };
    return out;
  }

  function save() {
    var name = saveFilename();
    var blob = new Blob([JSON.stringify(buildResponse(), null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
    dirty = false;

    var left = data.items.length - answered();
    var status = document.querySelector(".save-status");
    if (!status) return;
    status.textContent = left === 0
      ? "Saved. Email " + name + " back to us, along with any files you are sending."
      : "Saved -- " + left + (left === 1 ? " item is" : " items are") +
        " still unanswered. You can finish later: reopen this page and drop the saved file onto it.";
  }

  all(".save-button").forEach(function (b) { b.addEventListener("click", save); });

  // --- resume -----------------------------------------------------------
  function resumeStatus(text) {
    var el = document.querySelector(".resume-status");
    if (el) el.textContent = text;
  }

  function applyResponse(loaded, describe) {
    if (!loaded || !Array.isArray(loaded.items)) return false;
    var matched = 0;
    loaded.items.forEach(function (saved) {
      var item = saved && byId[saved.id];
      if (!item) return;
      item.action = saved.action || null;
      item.replacement_url = saved.replacement_url || "";
      item.note = saved.note || "";
      matched++;
    });
    var name = document.getElementById("respondent");
    if (name && loaded.respondent) name.value = loaded.respondent;
    articles.forEach(function (art) { if (art.syncFromState) art.syncFromState(); });
    refresh();

    if (describe) {
      var here = (data.source_reports || {}).broken_external_links_sha256;
      var there = (loaded.source_reports || {}).broken_external_links_sha256;
      var note = (here && there && here !== there)
        ? " Note: that file was saved against a different check of this site, so some items may not line up."
        : "";
      resumeStatus("Restored " + matched + " of " + loaded.items.length + " answers." + note);
    }
    return matched;
  }

  function readFile(file) {
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      var parsed;
      try { parsed = JSON.parse(reader.result); }
      catch (e) { resumeStatus("That does not look like a saved answers file."); return; }
      if (applyResponse(parsed, true) === false) {
        resumeStatus("That does not look like a saved answers file.");
      }
    };
    reader.onerror = function () { resumeStatus("That file could not be read."); };
    reader.readAsText(file);
  }

  var zone = document.getElementById("resume");
  var picker = document.getElementById("resume-file");
  if (picker) picker.addEventListener("change", function () { readFile(picker.files[0]); });
  if (zone) {
    ["dragenter", "dragover"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault(); zone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      zone.addEventListener(evt, function () { zone.classList.remove("dragover"); });
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files) readFile(e.dataTransfer.files[0]);
    });
  }
  // Dropping anywhere else must not make the browser navigate away from a
  // half-finished page.
  ["dragover", "drop"].forEach(function (evt) {
    document.addEventListener(evt, function (e) { e.preventDefault(); });
  });

  // --- clipboard --------------------------------------------------------
  function copyFallback(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    done(ok);
  }

  all(".copy-filenames").forEach(function (button) {
    var original = button.textContent;
    button.addEventListener("click", function () {
      var names = all("#section-missing .task-filename").map(function (el) {
        return el.textContent.trim();
      });
      if (!names.length) return;
      var text = names.join("\\n");
      var done = function (ok) {
        button.textContent = ok === false ? "Press Ctrl+C to copy" : "Copied";
        setTimeout(function () { button.textContent = original; }, 2500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done(true); },
                                                 function () { copyFallback(text, done); });
      } else {
        copyFallback(text, done);
      }
    });
  });

  // --- best-effort local autosave --------------------------------------
  var LS_KEY = "wpfreeze-owner-tasks:" + (data.project || "") + ":" + (data.report_generated || "");
  function persist() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(buildResponse())); } catch (e) { /* opaque origin */ }
  }
  (function restore() {
    var raw = null;
    try { raw = localStorage.getItem(LS_KEY); } catch (e) { return; }
    if (!raw) return;
    var parsed;
    try { parsed = JSON.parse(raw); } catch (e) { return; }
    applyResponse(parsed, false);
    resumeStatus("Picked up where this browser left off. Save when you are done.");
  })();

  // --- housekeeping -----------------------------------------------------
  window.addEventListener("beforeunload", function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });
  window.addEventListener("beforeprint", function () {
    all("details").forEach(function (d) { d.open = true; });
  });

  refresh();
})();
"""


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
  <span class="progress-track"><span></span></span>
  <span class="progress-count">0 of {len(tasks.tasks)} handled</span>
  <button type="button" class="save-button">Save my answers</button>
</div>

<div class="resume" id="resume">
  <p>Picking up where you left off? Drop your saved file here.</p>
  <input type="file" id="resume-file" accept="application/json,.json">
  <p class="resume-status" role="status"></p>
</div>

{_section_external_html(tasks)}
{_section_internal_html(tasks)}
{_section_missing_html(tasks)}

<section id="review" class="review">
  <h2>Review your answers</h2>
  <div class="review-body"></div>
</section>

<div class="save-area">
  <label class="respondent-label">Your name (optional)
    <input id="respondent" type="text" autocomplete="name">
  </label>
  <button type="button" class="save-button">Save my answers</button>
  <p class="save-status" role="status"></p>
</div>

{_footnote_html(tasks)}

{_data_island_html(tasks)}
<script>{_JS}</script>
</body>
</html>
"""


def write_owner_tasks(tasks: OwnerTasks, output_dir: Path, path: Path | None = None) -> Path:
    dest = path or (output_dir / OWNER_TASKS_FILENAME)
    dest.write_text(render_owner_tasks_html(tasks), encoding="utf-8")
    return dest
