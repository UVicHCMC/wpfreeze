from __future__ import annotations

import json
from pathlib import Path

import pytest

from wpfreeze.cli import SiteConfig
from wpfreeze.ownertasks import (
    OwnerTasksInputMissing,
    _classify_missing,
    _group_resize_variants,
    _task_id,
    load_tasks,
)

_BASE = "https://www.example.com"


def _config(output_dir: Path) -> SiteConfig:
    return SiteConfig(base_url=f"{_BASE}/", output_dir=output_dir, name="examplesite")


def _write(output_dir: Path, name: str, data: dict) -> None:
    (output_dir / name).write_text(json.dumps(data), encoding="utf-8")


def _broken_links(*results: dict) -> dict:
    return {"base_url": f"{_BASE}/", "checked_at": "2026-09-01T00:00:00+00:00", "results": list(results)}


def _ok(url: str) -> dict:
    return {"url": url, "pages": ["a.html"], "ok": True, "status": 200, "reason": None}


def _dead(url: str, pages: list[str], status: int = 404) -> dict:
    return {"url": url, "pages": pages, "ok": False, "status": status, "reason": f"HTTP {status}"}


def _authgated(url: str, pages: list[str]) -> dict:
    return {
        "url": url,
        "pages": pages,
        "ok": False,
        "status": 403,
        "reason": "HTTP 403 (auth-gated -- may not actually be broken)",
    }


def _missing(url: str, discovered_via: list[str] | None = None, status: int = 404) -> dict:
    return {
        "url": url,
        "status": "missing",
        "http_status": status,
        "discovered_via": discovered_via or [],
        "output_path": None,
    }


# --- partition into the three types ----------------------------------------


def test_load_tasks_partitions_the_three_task_types(tmp_path: Path):
    _write(
        tmp_path,
        "broken-external-links.json",
        _broken_links(
            _ok("https://good.example/"),
            _dead("https://gone.example/x", ["about.html", "team.html"]),
            _authgated("https://journal.example/article", ["research.html"]),
        ),
    )
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved_samples": [
                {
                    "page": f"{_BASE}/submit/",
                    "value": f"{_BASE}/tuoched/",
                    "context": "Touched",
                    "page_output": "/submit.html",
                    "target": f"{_BASE}/tuoched/",
                }
            ]
        },
    )
    _write(
        tmp_path,
        "manifest.json",
        {"records": [_missing(f"{_BASE}/wp-content/uploads/2016/05/photo.jpg", [f"crawl:{_BASE}/story/"])]},
    )

    tasks = load_tasks(tmp_path, _config(tmp_path))

    assert [t.type for t in tasks.by_type("external_link")] == ["external_link", "external_link"]
    assert len(tasks.by_type("internal_link")) == 1
    assert len(tasks.by_type("missing_file")) == 1
    assert tasks.unrun_checks == []

    internal = tasks.by_type("internal_link")[0]
    assert internal.target == f"{_BASE}/tuoched/"
    assert internal.context == "Touched"
    assert internal.pages == ["submit.html"]  # leading slash stripped

    missing = tasks.by_type("missing_file")[0]
    assert missing.filename == "photo.jpg"
    assert missing.upload_path == "/wp-content/uploads/2016/05/photo.jpg"
    assert missing.pages == [f"{_BASE}/story/"]  # raw URL: manifest has no output_path for it


def test_load_tasks_splits_dead_from_authgated(tmp_path: Path):
    _write(
        tmp_path,
        "broken-external-links.json",
        _broken_links(
            _dead("https://gone-a.example/", ["p1.html"]),
            _dead("https://gone-b.example/", ["p2.html"], status=410),
            _authgated("https://locked-a.example/", ["p3.html"]),
            _authgated("https://locked-b.example/", ["p4.html"]),
            {"url": "https://timeout.example/", "pages": ["p5.html"], "ok": False, "status": None,
             "reason": "unreachable (connection timed out)"},
        ),
    )

    tasks = load_tasks(tmp_path, _config(tmp_path))

    assert len(tasks.by_group("authgated")) == 2
    # 410 and the unreachable both count as "confirmed dead", not auth-gated
    assert len(tasks.by_group("dead")) == 3
    assert {t.detected["status"] for t in tasks.by_group("dead")} == {404, 410, None}


# --- missing-file filter --------------------------------------------------


def test_classify_missing_keeps_media_drops_the_rest():
    records = [
        _missing(f"{_BASE}/wp-content/uploads/2016/05/photo.jpg"),
        _missing(f"{_BASE}/harps.mp3"),
        _missing(f"{_BASE}/brochure.pdf"),
        _missing(f"{_BASE}/?attachment_id=600"),
        _missing(f"{_BASE}/wp-content/themes/x/style.css"),
        _missing(f"{_BASE}/wp-includes/js/app.js"),
        _missing(f"{_BASE}/wp-content/fonts/icons.ttf"),
        {"url": f"{_BASE}/other.jpg", "status": "external_unfetchable", "http_status": 404},  # not "missing"
    ]

    media, noise = _classify_missing(records)

    assert sorted(m["url"].rsplit("/", 1)[-1] for m in media) == [
        "brochure.pdf",
        "harps.mp3",
        "photo.jpg",
    ]
    assert noise == 4  # attachment_id, css, js, ttf -- NOT the external_unfetchable row


def test_load_tasks_records_handled_missing_count(tmp_path: Path):
    _write(tmp_path, "broken-external-links.json", _broken_links())
    _write(
        tmp_path,
        "manifest.json",
        {
            "records": [
                _missing(f"{_BASE}/photo.jpg"),
                _missing(f"{_BASE}/?attachment_id=1"),
                _missing(f"{_BASE}/a.css"),
                _missing(f"{_BASE}/b.js"),
            ]
        },
    )

    tasks = load_tasks(tmp_path, _config(tmp_path))

    assert len(tasks.by_type("missing_file")) == 1
    assert tasks.handled_missing_count == 3


# --- resize-variant grouping --------------------------------------------------


def test_group_resize_variants_folds_variant_into_its_original():
    media = [
        _missing(f"{_BASE}/uploads/Josie-image.jpg"),
        _missing(f"{_BASE}/uploads/Josie-image-244x300.jpg"),
        _missing(f"{_BASE}/uploads/Josie-image-150x150.jpg"),
    ]

    [entry] = _group_resize_variants(media)

    assert entry["filename"] == "Josie-image.jpg"
    assert entry["variants"] == ["Josie-image-150x150.jpg", "Josie-image-244x300.jpg"]  # sorted


def test_group_resize_variants_keeps_orphan_variant_and_non_dimension_names():
    media = [
        _missing(f"{_BASE}/uploads/IMG_1090.jpg"),  # digits, no NxN -- not a variant
        _missing(f"{_BASE}/uploads/map-1024x768.png"),  # a variant whose original is not missing
        _missing(f"{_BASE}/uploads/photo-2x4-lumber.jpg"),  # an x that is not a dimension
    ]

    entries = _group_resize_variants(media)

    assert sorted(e["filename"] for e in entries) == [
        "IMG_1090.jpg",
        "map-1024x768.png",
        "photo-2x4-lumber.jpg",
    ]
    assert all(e["variants"] == [] for e in entries)


# --- discovered_via parsing --------------------------------------------------


def test_missing_file_pages_resolve_through_manifest_output_path(tmp_path: Path):
    _write(tmp_path, "broken-external-links.json", _broken_links())
    _write(
        tmp_path,
        "manifest.json",
        {
            "records": [
                {"url": f"{_BASE}/story/", "status": "fetched", "output_path": "/story/index.html"},
                _missing(f"{_BASE}/photo.jpg", [f"crawl:{_BASE}/story/", "sitemap"]),
            ]
        },
    )

    [missing] = load_tasks(tmp_path, _config(tmp_path)).by_type("missing_file")

    # "crawl:" resolved to the site-relative built path; the non-crawl entry ignored
    assert missing.pages == ["story/index.html"]


def test_missing_file_with_empty_discovered_via_still_loads(tmp_path: Path):
    _write(tmp_path, "broken-external-links.json", _broken_links())
    _write(tmp_path, "manifest.json", {"records": [_missing(f"{_BASE}/photo.jpg", [])]})

    [missing] = load_tasks(tmp_path, _config(tmp_path)).by_type("missing_file")

    assert missing.pages == []
    assert missing.filename == "photo.jpg"


# --- stable ids -------------------------------------------------------------


def test_task_ids_are_stable_across_runs(tmp_path: Path):
    payload = _broken_links(_dead("https://gone.example/x", ["a.html"]))
    _write(tmp_path, "broken-external-links.json", payload)

    first = load_tasks(tmp_path, _config(tmp_path))
    second = load_tasks(tmp_path, _config(tmp_path))

    assert [t.id for t in first.tasks] == [t.id for t in second.tasks]
    assert first.tasks[0].id.startswith("ext-")


def test_task_id_distinguishes_type_and_target():
    assert _task_id("external_link", "https://x.example/") != _task_id("internal_link", "https://x.example/")
    assert _task_id("missing_file", "a") != _task_id("missing_file", "b")
    assert _task_id("external_link", "https://x.example/").startswith("ext-")
    assert _task_id("internal_link", "https://x.example/").startswith("int-")
    assert _task_id("missing_file", "https://x.example/").startswith("mis-")


# --- absent inputs -------------------------------------------------------------


def test_absent_broken_external_links_raises(tmp_path: Path):
    with pytest.raises(OwnerTasksInputMissing, match="run `wpfreeze checklinks` first"):
        load_tasks(tmp_path, _config(tmp_path))


def test_absent_build_report_marks_internal_links_unrun_not_empty(tmp_path: Path):
    _write(tmp_path, "broken-external-links.json", _broken_links(_dead("https://gone.example/", ["a.html"])))
    # no build-report.json, no manifest.json

    tasks = load_tasks(tmp_path, _config(tmp_path))

    assert tasks.by_type("internal_link") == []
    assert "internal_links" in tasks.unrun_checks
    assert "missing_files" in tasks.unrun_checks
    assert len(tasks.by_type("external_link")) == 1  # section 1 still works


def test_capture_finished_pulled_from_report_json(tmp_path: Path):
    _write(tmp_path, "broken-external-links.json", _broken_links())
    _write(tmp_path, "report.json", {"summary": {"run_finished": "2026-08-19T20:53:06+00:00"}})

    tasks = load_tasks(tmp_path, _config(tmp_path))

    assert tasks.capture_finished == "2026-08-19T20:53:06+00:00"
    assert tasks.checked_at == "2026-09-01T00:00:00+00:00"


# --- against the real landscapes capture ------------------------------------

_LANDSCAPES = Path(__file__).parent.parent / "output" / "landscapes"


@pytest.mark.skipif(
    not (_LANDSCAPES / "broken-external-links.json").exists(),
    reason="no real landscapes capture present locally",
)
def test_load_tasks_against_real_landscapes_capture():
    tasks = load_tasks(_LANDSCAPES, _config(_LANDSCAPES))

    external = tasks.by_type("external_link")
    internal = tasks.by_type("internal_link")
    missing = tasks.by_type("missing_file")

    assert len(external) == 126
    assert len(internal) == 47
    assert len(missing) == 5

    # the 1a/1b split -- distinct URLs, not page instances
    assert len(tasks.by_group("authgated")) == 28
    assert len(tasks.by_group("dead")) == 98

    # the five originals, variants folded
    assert sorted(m.filename for m in missing) == [
        "IMG_1090.jpg",
        "Josie-image.jpg",
        "Terry-portrait-2010_opt.jpg",
        "harps_of_enoshima.mp3",
        "josie-gray.jpeg",
    ]
    folded = {m.filename: m.variants for m in missing if m.variants}
    assert folded == {
        "Josie-image.jpg": ["Josie-image-244x300.jpg"],
        "Terry-portrait-2010_opt.jpg": ["Terry-portrait-2010_opt-150x150.jpg"],
    }

    assert tasks.handled_missing_count == 62
    assert tasks.unrun_checks == []
