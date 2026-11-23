"""Remove what a previous build wrote and this one didn't.

`build` writes into an existing site directory rather than a fresh one, so
anything an earlier build emitted that this build no longer produces --
a page whose output path changed, a bundle whose content hash moved, a
search index from back when search was enabled -- would otherwise sit in
`site/` indefinitely and ship with it. Nothing flags it: verification only
scans this run's own output, by design, so a stale page is invisible to
every check wpfreeze has.

The rule is deliberately narrow, because `site/` is also a directory a
person may have put things in:

- Only a file the *previous build recorded writing* is ever deleted. The
  record is the ledger, `<output_dir>/build-files.json`, keyed by the
  resolved site directory so `--site-dir` builds never prune each other.
- Even then, only if it is exactly as that build left it (same size and
  mtime_ns). A stale file someone has since hand-edited is kept and
  reported, never deleted -- an edit is work, and work is not ours to
  throw away.
- A file no build ever recorded (copied in by hand, or left by a build
  older than the ledger) is listed, never touched.

The ledger lives beside `site/`, not in it, so it is never uploaded.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

LEDGER_NAME = "build-files.json"
LEDGER_VERSION = 1

# How many paths per category the console summary names before summarising.
_SUMMARY_SAMPLES = 10


@dataclass
class PruneReport:
    """What pruning did, for the console and build-report.json."""

    ledger_found: bool = False
    removed: list[str] = field(default_factory=list)
    removed_dirs: list[str] = field(default_factory=list)
    kept_modified: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)


def _safe_relative(rel: str) -> PurePosixPath | None:
    """A ledger path as a site-relative path, or None if it could point
    outside the site directory. The ledger is a file on disk; it is not
    trusted to stay inside `site_dir` just because wpfreeze wrote it."""
    path = PurePosixPath(rel)
    if not rel or path.is_absolute() or ".." in path.parts:
        return None
    return path


def _fingerprint(path: Path) -> list[int] | None:
    try:
        st = path.lstat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def load_ledger(output_dir: Path, site_dir: Path) -> dict | None:
    """The previous build's entry for `site_dir`, or None if there is none
    (first build since the ledger existed, a different --site-dir, or an
    unreadable ledger -- all of which mean "delete nothing")."""
    path = output_dir / LEDGER_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("build: ignoring unreadable %s (%s); nothing will be pruned", path, exc)
        return None
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        logger.warning("build: ignoring %s with unknown format; nothing will be pruned", path)
        return None
    entry = (data.get("sites") or {}).get(str(site_dir.resolve()))
    return entry if isinstance(entry, dict) else None


def write_ledger(output_dir: Path, site_dir: Path, written: set[str], owned_dirs: set[str]) -> Path:
    """Record this build's output for `site_dir`, keeping other site dirs'
    entries intact."""
    path = output_dir / LEDGER_NAME
    data: dict = {"version": LEDGER_VERSION, "sites": {}}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("version") == LEDGER_VERSION:
            data["sites"] = dict(existing.get("sites") or {})
    except (OSError, json.JSONDecodeError):
        pass

    files: dict[str, list[int]] = {}
    for rel in sorted(written):
        fingerprint = _fingerprint(site_dir / rel)
        if fingerprint is not None:
            files[rel] = fingerprint
    data["sites"][str(site_dir.resolve())] = {"files": files, "dirs": sorted(owned_dirs)}
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path


def _remove_empty_parents(site_dir: Path, start: Path, removed_dirs: list[str]) -> None:
    current = start
    while current != site_dir and site_dir in current.parents:
        try:
            current.rmdir()  # only succeeds on an empty directory
        except OSError:
            return
        removed_dirs.append(current.relative_to(site_dir).as_posix())
        current = current.parent


def prune_site(
    site_dir: Path,
    previous: dict | None,
    written: set[str],
    owned_dirs: set[str],
) -> PruneReport:
    """Delete the previous build's leftovers from `site_dir`; list the rest.

    `written` is every site-relative path this build wrote; `owned_dirs`
    the directories it owns wholesale (the Pagefind bundle, which the
    indexer regenerates from scratch each time -- see run_pagefind_index).
    """
    report = PruneReport(ledger_found=previous is not None)
    previous = previous or {}

    previous_files = previous.get("files") or {}
    for rel, recorded in sorted(previous_files.items()):
        if rel in written:
            continue
        safe = _safe_relative(rel)
        if safe is None:
            logger.warning("build: ignoring unsafe ledger path %r", rel)
            continue
        if any(PurePosixPath(d) in safe.parents for d in owned_dirs):
            continue
        target = site_dir / safe
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or not target.is_file() or _fingerprint(target) != list(recorded):
            report.kept_modified.append(rel)
            continue
        target.unlink()
        report.removed.append(rel)
        _remove_empty_parents(site_dir, target.parent, report.removed_dirs)

    for rel in sorted(previous.get("dirs") or []):
        if rel in owned_dirs:
            continue
        safe = _safe_relative(rel)
        target = site_dir / safe if safe is not None else None
        if target is None or target.is_symlink() or not target.is_dir():
            continue
        shutil.rmtree(target)
        report.removed_dirs.append(rel)

    if site_dir.is_dir():
        owned = {site_dir / d for d in owned_dirs}
        # Path.walk never follows a symlinked directory (rglob's behaviour
        # here differs between 3.12 and 3.13+), so a link out of site_dir is
        # listed as one untracked entry rather than walked into.
        for root, dirnames, filenames in site_dir.walk():
            dirnames[:] = [d for d in dirnames if root / d not in owned]
            symlinked_dirs = [d for d in dirnames if (root / d).is_symlink()]
            dirnames[:] = [d for d in dirnames if d not in symlinked_dirs]
            for name in filenames + symlinked_dirs:
                rel = (root / name).relative_to(site_dir).as_posix()
                if rel not in written and rel not in report.kept_modified:
                    report.untracked.append(rel)
        report.untracked.sort()

    return report


def _sample(paths: list[str]) -> str:
    shown = [f"      {p}" for p in paths[:_SUMMARY_SAMPLES]]
    if len(paths) > _SUMMARY_SAMPLES:
        shown.append(f"      ... and {len(paths) - _SUMMARY_SAMPLES} more (see build-report.json)")
    return "\n".join(shown)


def format_prune_summary(report: PruneReport) -> str:
    """Empty when there is nothing to say: a clean rebuild prints nothing."""
    lines: list[str] = []
    if report.removed or report.removed_dirs:
        lines.append(
            f"  removed {len(report.removed)} file(s) the previous build wrote and this one did not"
            + (f", plus {len(report.removed_dirs)} emptied/retired director(ies)" if report.removed_dirs else "")
        )
    if report.kept_modified:
        lines.append(
            f"  kept {len(report.kept_modified)} file(s) the previous build wrote and this one did not,"
            " because they have changed since -- delete them by hand if they should not ship:"
        )
        lines.append(_sample(report.kept_modified))
    if report.untracked:
        lines.append(
            f"  {len(report.untracked)} file(s) in the site directory were not written by this build"
            " and will ship with it unless removed:"
        )
        lines.append(_sample(report.untracked))
        if not report.ledger_found:
            lines.append(
                "      (no record of an earlier build here, so nothing was removed automatically;"
                " from now on, files a rebuild stops producing are)"
            )
    if not lines:
        return ""
    return "Site directory cleanup:\n" + "\n".join(lines)
