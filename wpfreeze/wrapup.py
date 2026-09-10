"""Per-step timing and a short "what actually happened, where is it"
summary for `wpfreeze freeze` and for an individual subcommand that did
real work.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from wpfreeze.progress import format_duration

# (short human label, relative path under output_dir) -- fixed order,
# checked in this order, only what actually exists is ever printed.
# artefact_paths must not invent paths: a missing vnu-report.json means
# validate never ran, and listing it anyway would be a lie.
_ARTEFACTS: tuple[tuple[str, str], ...] = (
    ("Built site", "site"),
    ("Report", "report.html"),
    ("Checklist", "cleanup-todo.html"),
    ("Broken links", "broken-external-links.html"),
    ("Owner worksheet", "owner-tasks.html"),
    ("VNU report", "vnu-report.json"),
    ("Build report", "build-report.json"),
    ("Diagnostics", "diagnostics.json"),
    ("Manifest", "manifest.json"),
    ("Upload script", "upload.sh"),
    ("Logs", "logs"),
)


@dataclass(frozen=True)
class StepTiming:
    step: str  # "acquire", "build", ...
    seconds: float
    exit_code: int
    note: str = ""  # e.g. "skipped (no HTML checker available)" -- shown instead of an exit-code marker


@dataclass
class RunSummary:
    project: str
    base_url: str
    output_dir: Path
    steps: list[StepTiming] = field(default_factory=list)


def time_step(summary: RunSummary, step: str, fn: Callable[[], int]) -> int:
    """Runs `fn`, records how long it took and what it returned onto
    `summary.steps`, and returns that same exit code -- callers branch on
    the return value exactly as if they'd called `fn()` directly."""
    started = time.monotonic()
    exit_code = fn()
    summary.steps.append(StepTiming(step=step, seconds=time.monotonic() - started, exit_code=exit_code))
    return exit_code


def artefact_paths(output_dir: Path) -> list[tuple[str, Path]]:
    """(label, path) for every artefact in `_ARTEFACTS` that actually
    exists under `output_dir` right now, in a fixed display order."""
    paths = []
    for label, rel in _ARTEFACTS:
        path = output_dir / rel
        if path.exists():
            paths.append((label, path))
    return paths


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def format_wrapup(summary: RunSummary) -> str:
    """Brief, human-facing -- this is the whole point. A step that exited
    non-zero gets a trailing marker rather than being folded silently into
    the total, since a timing table that hides a failure is worse than no
    table."""
    total_seconds = sum(t.seconds for t in summary.steps)
    lines = [f"{summary.project} — done in {format_duration(total_seconds)}"]
    for timing in summary.steps:
        duration = format_duration(timing.seconds)
        if timing.note:
            marker = f" — {timing.note}"
        elif timing.exit_code == 2:
            marker = f" — failed (exit {timing.exit_code})"
        elif timing.exit_code == 1:
            marker = f" — completed with gaps (exit {timing.exit_code})"
        else:
            marker = ""
        lines.append(f"  {timing.step:<10}{duration:>7}{marker}")

    artefacts = artefact_paths(summary.output_dir)
    if artefacts:
        lines.append("")
        width = max(len(label) for label, _ in artefacts)
        for label, path in artefacts:
            display = _display_path(path)
            suffix = "/" if path.is_dir() else ""
            lines.append(f"  {label:<{width}}   {display}{suffix}")

    return "\n".join(lines)
