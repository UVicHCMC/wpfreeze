from __future__ import annotations

import json
from pathlib import Path

from wpfreeze.cleanup import build_cleanup_todo, write_cleanup_todo


def _write(path: Path, name: str, data: dict) -> None:
    (path / name).write_text(json.dumps(data), encoding="utf-8")


def test_returns_none_when_nothing_has_run_yet(tmp_path: Path):
    assert build_cleanup_todo(tmp_path) is None
    assert write_cleanup_todo(tmp_path) is None
    assert not (tmp_path / "cleanup-todo.md").exists()


def test_clean_run_gets_the_all_clear_headline(tmp_path: Path):
    _write(tmp_path, "build-report.json", {"unresolved": 0, "unresolved_samples": []})
    _write(tmp_path, "vnu-report.json", {"issues": [], "total_messages": 0, "documents_checked": 5})
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": True},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "looks clean" in content
    assert "None found -- VNU reported a clean bill of health" in content


def test_unresolved_references_are_listed_with_samples(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 2,
            "unresolved_samples": [
                ["https://s/team/", "https://s/never-captured/"],
                ["https://s/about/", "https://s/also-missing/"],
            ],
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "worth a look before you call it done" in content
    assert "## Broken references" in content
    assert "https://s/never-captured/" in content
    assert "linked from `https://s/team/`" in content


def test_unresolved_sample_overflow_is_summarized_not_dumped(tmp_path: Path):
    samples = [[f"https://s/page-{i}/", f"https://s/missing-{i}/"] for i in range(30)]
    _write(tmp_path, "build-report.json", {"unresolved": 30, "unresolved_samples": samples})
    content = build_cleanup_todo(tmp_path)
    assert "missing-0" in content
    assert "missing-29" not in content
    assert "...and 15 more" in content


def test_vnu_issues_are_listed_with_page_counts(tmp_path: Path):
    _write(
        tmp_path,
        "vnu-report.json",
        {
            "documents_checked": 17,
            "total_messages": 106,
            "issues": [
                {
                    "message": 'An "img" element must have an "alt" attribute.',
                    "count": 106,
                    "pages": ["team.html", "store.html"],
                }
            ],
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Site markup/content quirks" in content
    assert "106x" in content
    assert "alt" in content
    assert "yours to hand-edit directly" in content


def test_integrity_anomalies_are_flagged_as_look_at_this_first(tmp_path: Path):
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [{"local_path": "raw/index.html", "urls": ["a", "b"]}],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": True},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "look at this first" in content
    assert "worth a look before you call it done" in content
    assert "claimed by more than one URL" in content


def test_homepage_not_found_is_flagged(tmp_path: Path):
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": False},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "No manifest record matches the site's own homepage" in content


def test_missing_reports_are_noted_not_treated_as_error(tmp_path: Path):
    """Only diagnostics.json exists (e.g. build/validate never ran) --
    the doc should say so for the missing pieces, not omit them silently
    or crash."""
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": True},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "wpfreeze build" in content and "hasn't been run yet" in content
    assert "wpfreeze validate" in content and "hasn't been run" in content


def test_write_cleanup_todo_writes_the_file(tmp_path: Path):
    _write(tmp_path, "vnu-report.json", {"issues": [], "total_messages": 0, "documents_checked": 1})
    path = write_cleanup_todo(tmp_path)
    assert path == tmp_path / "cleanup-todo.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("# Cleanup checklist")


def test_corrupt_json_is_treated_as_absent_not_a_crash(tmp_path: Path):
    (tmp_path / "vnu-report.json").write_text("{not valid json", encoding="utf-8")
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": True},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert content is not None
    assert "hasn't been run against the built site" in content


def test_all_clear_is_withheld_when_a_check_never_ran(tmp_path: Path):
    """The all-clear may only be given for checks that actually ran.
    Previously a section with no report contributed "nothing wrong"
    identically to one that ran and found nothing, so the headline
    certified work never performed -- contradicting the "hasn't been run
    yet" prose in its own body."""
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": True},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "looks clean" not in content
    assert "not yet a clean bill of health" in content
    assert "build" in content and "validate" in content
    # ...and the body still says which ones are outstanding.
    assert "hasn't been run yet" in content


def test_all_clear_still_given_when_everything_ran_clean(tmp_path: Path):
    _write(tmp_path, "build-report.json", {"unresolved": 0, "unresolved_samples": []})
    _write(tmp_path, "vnu-report.json", {"issues": [], "total_messages": 0, "documents_checked": 5})
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": True},
        },
    )
    assert "looks clean" in build_cleanup_todo(tmp_path)


def test_malformed_report_shapes_do_not_crash(tmp_path: Path):
    """Each of these is a field guarded on one line and trusted on the
    next. All three used to raise."""
    _write(tmp_path, "build-report.json", {"unresolved": 0, "unresolved_samples": []})
    # homepage key present but null
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": None,
        },
    )
    # a VNU issue missing its count/message
    _write(tmp_path, "vnu-report.json", {"issues": [{"pages": ["a.html"]}], "total_messages": 1})
    content = build_cleanup_todo(tmp_path)
    assert content is not None
    assert "(no message recorded)" in content


def test_report_that_is_valid_json_but_not_an_object_is_treated_as_absent(tmp_path: Path):
    (tmp_path / "build-report.json").write_text("[]", encoding="utf-8")
    _write(tmp_path, "vnu-report.json", {"issues": [], "total_messages": 0, "documents_checked": 1})
    content = build_cleanup_todo(tmp_path)
    assert content is not None
    assert "hasn't been run yet" in content
