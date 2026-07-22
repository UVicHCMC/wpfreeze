"""Post-run diagnostics: a compact, agent-facing summary of a completed
acquire run, distinct in purpose from report.py's report.html/report.json.

report.py answers "is this archive complete, and what should the site
owner do about the gaps" -- it's the deliverable. This module answers
"did anything about this *run* look wrong," for whoever is debugging
wpfreeze itself: status counts, log warnings/errors, the specific
false-positive/gap shapes seen on real sites this week (bare JS
base-path references), and two direct integrity checks -- duplicate
local_path claims and manifest/disk content_hash mismatches -- that
would have caught the local_path_for query-string collision and the
homepage-content-corruption bug immediately instead of requiring by-hand
investigation. Kept small and structured deliberately: this is meant to
be read in full, cheaply, not skimmed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from wpfreeze.manifest import GAP_STATUSES, Manifest, ManifestRecord, Status

logger = logging.getLogger(__name__)

_LOG_LEVEL_RE = re.compile(r"^\S+ \S+ (WARNING|ERROR|CRITICAL) (.*)$")
_MAX_LOG_ISSUES = 50
_MAX_GAP_SAMPLES = 50
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _normalize_for_comparison(url: str) -> str:
    url = url.rstrip("/").lower()
    return url.replace("://www.", "://", 1)


def _find_homepage_record(manifest: Manifest, base_url: str) -> ManifestRecord | None:
    target = _normalize_for_comparison(base_url)
    for record in manifest.all():
        candidates = (record.url, *record.aliases)
        if any(_normalize_for_comparison(c) == target for c in candidates):
            return record
    return None


def _is_bare_directory_reference(url: str) -> bool:
    return url.rstrip().endswith("/")


def _collect_log_issues(log_path: Path | None) -> dict[str, Any]:
    """Groups by (level, message) with the timestamp stripped -- the same
    warning firing on every one of a thousand pages (e.g. one XML-export
    cleanup notice) is one issue to review, not a thousand."""
    if log_path is None or not log_path.exists():
        return {"available": False, "issues": [], "total": 0, "truncated": False}
    counts: Counter[str] = Counter()
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _LOG_LEVEL_RE.match(line)
        if match:
            level, rest = match.groups()
            counts[f"{level} {rest}"] += 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    issues = [{"line": line, "count": count} for line, count in ordered[:_MAX_LOG_ISSUES]]
    return {
        "available": True,
        "issues": issues,
        "total": len(counts),
        "truncated": len(counts) > _MAX_LOG_ISSUES,
    }


def _gap_records(manifest: Manifest) -> list[ManifestRecord]:
    return [r for r in manifest.all() if r.status in GAP_STATUSES or r.status == Status.EXCLUDED.value]


def _summarize_gaps(records: list[ManifestRecord]) -> dict[str, Any]:
    bare_directory = [r for r in records if _is_bare_directory_reference(r.url)]
    samples = [
        {"url": r.url, "status": r.status, "http_status": r.http_status}
        for r in records[:_MAX_GAP_SAMPLES]
    ]
    return {
        "total": len(records),
        "bare_directory_count": len(bare_directory),
        "bare_directory_sample": [r.url for r in bare_directory[:10]],
        "samples": samples,
        "truncated": len(records) > _MAX_GAP_SAMPLES,
    }


def _duplicate_local_paths(manifest: Manifest) -> list[dict[str, Any]]:
    """Two different manifest records claiming the same local_path is
    always a bug -- one silently overwrote the other's file on disk (see
    the local_path_for query-string collision this exact check would
    have caught immediately instead of requiring a several-hour
    by-hand trace)."""
    groups: dict[str, list[str]] = {}
    for record in manifest.all():
        if record.local_path:
            groups.setdefault(record.local_path, []).append(record.url)
    return [
        {"local_path": path, "urls": urls}
        for path, urls in sorted(groups.items())
        if len(urls) > 1
    ]


_FETCHED_STATUSES = frozenset({Status.FETCHED.value, Status.FETCHED_WAYBACK.value})


def _disk_hash_mismatches(manifest: Manifest, output_dir: Path) -> list[dict[str, Any]]:
    """Re-hash every fetched record's file on disk and compare against the
    manifest's own content_hash, recorded at fetch time. A mismatch means
    the file was overwritten by something else after that record's fetch
    completed -- exactly the symptom (not the cause) of the homepage
    identity-collision bug, and a direct check for any future variant of
    the same failure mode regardless of root cause."""
    mismatches: list[dict[str, Any]] = []
    for record in manifest.all():
        if record.status not in _FETCHED_STATUSES or not record.local_path or not record.content_hash:
            continue
        path = output_dir / record.local_path
        try:
            disk_bytes = path.read_bytes()
        except OSError:
            mismatches.append({"url": record.url, "local_path": record.local_path, "issue": "file_missing"})
            continue
        disk_hash = hashlib.sha256(disk_bytes).hexdigest()
        if disk_hash != record.content_hash:
            mismatches.append(
                {
                    "url": record.url,
                    "local_path": record.local_path,
                    "issue": "hash_mismatch",
                    "manifest_hash": record.content_hash,
                    "disk_hash": disk_hash,
                }
            )
    return mismatches


_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def _content_type_shape_mismatches(manifest: Manifest) -> list[dict[str, Any]]:
    """A URL whose path ends in "/" has no filename to carry its identity,
    so a record for one that served CSS, JS or an image is nearly always an
    identity collision: several distinct resources normalized onto the same
    bare directory URL and whichever fetched last won.

    This needs checking separately from the gap statuses precisely because
    the dangerous version of it looks *successful*. WordPress.com's
    /_static/??<spec> bundles all collapse to a bare "https://host/_static/"
    once normalization strips the query (see extract.decode_static_bundle);
    asked for that URL, the Wayback Machine returned a real archived bundle,
    so the record landed as fetched_wayback carrying 275 KB of entirely
    plausible CSS that stood in for five different bundles. A gap you can
    see is recoverable; this is not, because nothing about it looks wrong.
    Measured over two real captures this flags 0 of 5,839 records on a site
    without the bug and exactly the 2 corrupted records on the site with it.
    """
    mismatches: list[dict[str, Any]] = []
    for record in manifest.all():
        content_type = (record.content_type or "").split(";")[0].strip().lower()
        if not content_type or content_type in _HTML_CONTENT_TYPES:
            continue
        if not urlsplit(record.url).path.endswith("/"):
            continue
        mismatches.append(
            {
                "url": record.url,
                "status": record.status,
                "content_type": content_type,
                "local_path": record.local_path,
            }
        )
    return mismatches


def _homepage_check(manifest: Manifest, output_dir: Path, base_url: str) -> dict[str, Any]:
    record = _find_homepage_record(manifest, base_url)
    if record is None:
        return {"found": False}
    result: dict[str, Any] = {
        "found": True,
        "url": record.url,
        "status": record.status,
        "http_status": record.http_status,
        "folded_alias_count": len(record.aliases),
    }
    content_type = (record.content_type or "").split(";")[0].strip().lower()
    if record.local_path and content_type in ("text/html", "application/xhtml+xml"):
        try:
            html_bytes = (output_dir / record.local_path).read_bytes()
        except OSError:
            result["title"] = None
        else:
            match = _TITLE_RE.search(html_bytes)
            result["title"] = (
                match.group(1).decode("utf-8", errors="replace").strip() if match else None
            )
    return result


def build_diagnostics(
    manifest: Manifest,
    output_dir: Path,
    base_url: str,
    log_path: Path | None,
) -> dict[str, Any]:
    """Pure function: no I/O beyond reading already-fetched files on disk
    and the given log file. Safe to call repeatedly/in tests."""
    counts = Counter(r.status for r in manifest.all())
    pending = counts.get(Status.PENDING.value, 0) + counts.get(Status.RETRYING.value, 0)
    flag_counts: Counter[str] = Counter()
    redirect_alias_total = 0
    for record in manifest.all():
        flag_counts.update(record.flags)
        redirect_alias_total += len(record.redirect_from)

    return {
        "counts": dict(sorted(counts.items())),
        "pending": pending,
        "has_gaps": manifest.has_gaps(),
        "flag_counts": dict(sorted(flag_counts.items())),
        "redirect_alias_total": redirect_alias_total,
        "log_issues": _collect_log_issues(log_path),
        "gaps": _summarize_gaps(_gap_records(manifest)),
        "duplicate_local_paths": _duplicate_local_paths(manifest),
        "content_type_shape_mismatches": _content_type_shape_mismatches(manifest),
        "disk_hash_mismatches": _disk_hash_mismatches(manifest, output_dir),
        "homepage": _homepage_check(manifest, output_dir, base_url),
    }


def write_diagnostics(diagnostics: dict[str, Any], output_dir: Path) -> Path:
    path = output_dir / "diagnostics.json"
    path.write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
    return path


def format_diagnostics_summary(diagnostics: dict[str, Any]) -> str:
    """A handful of lines for the terminal -- the full detail lives in
    diagnostics.json for whoever (or whatever) reviews the run next."""
    lines = ["Diagnostics:"]
    lines.append(f"  status counts: {diagnostics['counts']}")
    lines.append(f"  pending: {diagnostics['pending']}  has_gaps: {diagnostics['has_gaps']}")
    if diagnostics["flag_counts"]:
        lines.append(f"  flags: {diagnostics['flag_counts']}")
    log_issues = diagnostics["log_issues"]
    if log_issues["available"]:
        lines.append(f"  distinct WARNING/ERROR/CRITICAL log lines: {log_issues['total']}")
    gaps = diagnostics["gaps"]
    if gaps["bare_directory_count"]:
        lines.append(
            f"  {gaps['bare_directory_count']} gap(s) are bare directory references "
            "(likely a JS base-path config value, not a real page)"
        )
    dup_paths = diagnostics["duplicate_local_paths"]
    if dup_paths:
        lines.append(f"  ** {len(dup_paths)} local_path collision(s) -- files silently overwrote each other **")
    shape = diagnostics["content_type_shape_mismatches"]
    if shape:
        lines.append(
            f"  ** {len(shape)} directory-like URL(s) serving non-HTML content "
            "-- likely an identity collision, check even if status looks fetched **"
        )
    mismatches = diagnostics["disk_hash_mismatches"]
    if mismatches:
        lines.append(f"  ** {len(mismatches)} manifest/disk content_hash mismatch(es) **")
    homepage = diagnostics["homepage"]
    if homepage.get("found"):
        title = homepage.get("title")
        lines.append(f"  homepage: {homepage['status']} ({homepage['http_status']})" + (f" -- \"{title}\"" if title else ""))
    else:
        lines.append("  homepage: no matching manifest record found")
    return "\n".join(lines)
