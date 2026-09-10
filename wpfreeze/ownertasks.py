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
    )
