from __future__ import annotations

from pathlib import Path

from wpfreeze.wrapup import RunSummary, StepTiming, artefact_paths, format_wrapup, time_step


def test_duration_formatting_seconds_minutes_hours():
    summary9 = RunSummary(project="p", base_url="https://example.com/", output_dir=Path("out"))
    summary9.steps.append(StepTiming(step="build", seconds=9, exit_code=0))
    assert "9s" in format_wrapup(summary9)

    summary70 = RunSummary(project="p", base_url="https://example.com/", output_dir=Path("out"))
    summary70.steps.append(StepTiming(step="build", seconds=70, exit_code=0))
    assert "1m10s" in format_wrapup(summary70)

    summary3700 = RunSummary(project="p", base_url="https://example.com/", output_dir=Path("out"))
    summary3700.steps.append(StepTiming(step="build", seconds=3700, exit_code=0))
    assert "1h01m" in format_wrapup(summary3700)


def test_artefact_paths_omits_what_doesnt_exist(tmp_path: Path):
    (tmp_path / "report.html").write_text("x", encoding="utf-8")
    (tmp_path / "site").mkdir()

    paths = artefact_paths(tmp_path)

    labels = {label for label, _ in paths}
    assert labels == {"Built site", "Report"}


def test_artefact_paths_empty_output_dir(tmp_path: Path):
    assert artefact_paths(tmp_path) == []


def test_format_wrapup_marks_non_zero_step():
    summary = RunSummary(project="landscapes", base_url="https://example.com/", output_dir=Path("out"))
    summary.steps.append(StepTiming(step="acquire", seconds=5, exit_code=0))
    summary.steps.append(StepTiming(step="build", seconds=5, exit_code=1))

    text = format_wrapup(summary)

    assert "completed with gaps (exit 1)" in text
    acquire_line = next(line for line in text.splitlines() if "acquire" in line)
    assert "exit" not in acquire_line


def test_format_wrapup_marks_failed_step():
    summary = RunSummary(project="landscapes", base_url="https://example.com/", output_dir=Path("out"))
    summary.steps.append(StepTiming(step="acquire", seconds=5, exit_code=2))

    assert "failed (exit 2)" in format_wrapup(summary)


def test_time_step_records_timing_and_returns_exit_code():
    summary = RunSummary(project="landscapes", base_url="https://example.com/", output_dir=Path("out"))

    exit_code = time_step(summary, "build", lambda: 0)

    assert exit_code == 0
    assert len(summary.steps) == 1
    assert summary.steps[0].step == "build"
    assert summary.steps[0].exit_code == 0
    assert summary.steps[0].seconds >= 0
