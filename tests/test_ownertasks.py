from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from wpfreeze.cli import SiteConfig
from wpfreeze.ownertasks import (
    OwnerTasksInputMissing,
    _classify_missing,
    _group_resize_variants,
    _task_id,
    load_tasks,
    render_owner_tasks_html,
    write_owner_tasks,
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
    """Asserts invariants against whatever is on disk rather than a
    memorised snapshot. `checklinks` rewrites broken-external-links.json
    from a live network run, so the broken-link counts legitimately drift
    between runs -- 126 broken on 2026-08-24, 123 two weeks later as three
    hosts came back. Pinning those numbers would make this fail on every
    fresh check, which teaches the reader nothing about load_tasks."""
    source = json.loads((_LANDSCAPES / "broken-external-links.json").read_text(encoding="utf-8"))
    broken = [r for r in source["results"] if not r["ok"]]
    expected_authgated = sum(1 for r in broken if "auth-gated" in (r["reason"] or ""))
    build_report = json.loads((_LANDSCAPES / "build-report.json").read_text(encoding="utf-8"))

    tasks = load_tasks(_LANDSCAPES, _config(_LANDSCAPES))
    external = tasks.by_type("external_link")
    internal = tasks.by_type("internal_link")
    missing = tasks.by_type("missing_file")

    # every broken link becomes exactly one task, and the 1a/1b split is
    # exhaustive -- the decision unit is the distinct URL, not the page
    assert len(external) == len(broken)
    assert len(tasks.by_group("authgated")) == expected_authgated
    assert len(tasks.by_group("dead")) == len(broken) - expected_authgated
    assert len(internal) == len(build_report["unresolved_samples"])

    # the manifest does not change without a re-acquire, so these are fixed:
    # 69 missing records -> 7 owner-relevant media -> 5 asks, variants folded
    assert sorted(m.filename for m in missing) == [
        "IMG_1090.jpg",
        "Josie-image.jpg",
        "Terry-portrait-2010_opt.jpg",
        "harps_of_enoshima.mp3",
        "josie-gray.jpeg",
    ]
    assert {m.filename: m.variants for m in missing if m.variants} == {
        "Josie-image.jpg": ["Josie-image-244x300.jpg"],
        "Terry-portrait-2010_opt.jpg": ["Terry-portrait-2010_opt-150x150.jpg"],
    }
    assert tasks.handled_missing_count == 62
    assert tasks.unrun_checks == []


# --- rendering (phase 3: semantic markup, no CSS, no behaviour) -------------


def _render(tmp_path: Path, *, build_report: bool = True, manifest: bool = True) -> str:
    _write(
        tmp_path,
        "broken-external-links.json",
        _broken_links(
            _dead('https://gone.example/a?x=1&y=2', ["about.html"]),
            _authgated("https://journal.example/", ["research.html"]),
        ),
    )
    if build_report:
        _write(
            tmp_path,
            "build-report.json",
            {
                "unresolved_samples": [
                    {"target": "https://x.example/<b>", "context": 'a "quoted" link',
                     "page_output": "/p.html", "page": "", "value": ""}
                ]
            },
        )
    if manifest:
        _write(
            tmp_path,
            "manifest.json",
            {
                "records": [
                    _missing(f"{_BASE}/uploads/pic.jpg", [f"crawl:{_BASE}/story/"]),
                    _missing(f"{_BASE}/uploads/pic-150x150.jpg", [f"crawl:{_BASE}/story/"]),
                    _missing(f"{_BASE}/?attachment_id=9"),
                ]
            },
        )
    return render_owner_tasks_html(load_tasks(tmp_path, _config(tmp_path)))


def test_render_is_self_contained(tmp_path: Path):
    """One file, opened from a desktop that may have no network: the only
    two script elements are the data island and the inline behaviour, and
    nothing is fetched from anywhere."""
    html = _render(tmp_path)

    assert html.startswith("<!doctype html>")
    assert html.count("<script") == 2
    assert '<script type="application/json" id="owner-tasks-data">' in html
    assert "<style>" in html and "<style></style>" not in html
    for remote in ("http://", "https://cdn", "<link", "@import", "src=\"http"):
        assert remote not in html.split('id="owner-tasks-data"')[0], remote


def test_render_escapes_quotes_and_angle_brackets_in_targets(tmp_path: Path):
    html = _render(tmp_path)

    assert "https://x.example/&lt;b&gt;" in html
    assert "https://gone.example/a?x=1&amp;y=2" in html
    assert "a &quot;quoted&quot; link" in html
    # nothing outside the JSON island carries raw markup or an unescaped &
    outside_island = re.sub(
        r'<script type="application/json".*?</script>', "", html, flags=re.S
    )
    assert "<b>" not in outside_island
    assert "x=1&y=2" not in outside_island
    # the island itself escapes every < and > so the <script> stays inert
    island = re.search(
        r'<script type="application/json" id="owner-tasks-data">\n(.*?)\n</script>', html, re.S
    ).group(1)
    assert "<" not in island and ">" not in island
    assert "\\u003cb\\u003e" in island  # the raw target, neutralised


def test_render_has_full_task_anatomy_per_the_markup_contract(tmp_path: Path):
    html = _render(tmp_path)

    assert 'data-task-type="external_link"' in html
    assert 'data-task-type="internal_link"' in html
    assert 'data-task-type="missing_file"' in html
    assert 'data-group="dead"' in html and 'data-group="authgated"' in html
    assert 'class="task-action" data-default="keep"' in html  # auth-gated default
    assert '<option value="keep" selected>' in html  # correct with JS disabled
    # Both reveal fields exist, start hidden, and carry an id + accessible
    # name (a placeholder alone is neither).
    for cls in ("task-url", "task-note"):
        field = re.search(rf'<input class="{cls}"[^>]*>', html).group(0)
        assert " hidden>" in field, field
        assert ' id="' in field and ' name="' in field, field
        assert ' aria-label="' in field, field


def test_render_folds_variants_and_shows_the_note(tmp_path: Path):
    html = _render(tmp_path)

    assert '<h3 class="task-filename">pic.jpg</h3>' in html
    assert '<h3 class="task-filename">pic-150x150.jpg</h3>' not in html  # folded, not its own row
    assert "task-variant-note" in html
    assert html.count('data-task-type="missing_file"') == 1


def test_render_counts_match_the_task_set(tmp_path: Path):
    html = _render(tmp_path)

    assert html.count('<article class="task"') == 4  # 2 external + 1 internal + 1 missing
    assert "0 of 4 handled" in html


def test_render_marks_an_unrun_section_rather_than_emitting_it_empty(tmp_path: Path):
    html = _render(tmp_path, build_report=False)

    assert 'id="section-internal"' in html and 'data-unrun="true"' in html
    assert "has not been run yet" in html
    # section 1 still rendered its tasks
    assert 'data-task-type="external_link"' in html


def test_json_island_round_trips_and_prefills_authgated(tmp_path: Path):
    html = _render(tmp_path)
    body = re.search(
        r'<script type="application/json" id="owner-tasks-data">\n(.*?)\n</script>', html, re.S
    ).group(1)
    data = json.loads(body.replace("<\\/", "</"))

    assert data["schema"] == "wpfreeze/owner-response@1"
    assert data["counts"]["total"] == 4
    assert len(data["items"]) == 4
    authgated = [i for i in data["items"] if i.get("group") == "authgated"]
    assert authgated and all(i["action"] == "keep" for i in authgated)
    assert all(i["action"] is None for i in data["items"] if i["type"] == "missing_file")


def test_write_owner_tasks_writes_the_file(tmp_path: Path):
    _write(tmp_path, "broken-external-links.json", _broken_links(_dead("https://gone.example/", ["a.html"])))
    tasks = load_tasks(tmp_path, _config(tmp_path))

    dest = write_owner_tasks(tasks, tmp_path)

    assert dest.name == "owner-tasks.html"
    assert dest.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_render_includes_the_interaction_layer(tmp_path: Path):
    html = _render(tmp_path)

    assert html.count('class="save-button"') == 2  # sticky bar and save area
    assert 'class="progress-track"' in html
    assert 'id="review"' in html and 'class="review-body"' in html
    assert 'id="resume"' in html and 'id="resume-file"' in html
    assert 'id="respondent"' in html
    assert "Picking up where you left off? Drop your saved file here." in html
    assert "Review your answers" in html


def test_stylesheet_does_not_defeat_the_hidden_attribute(tmp_path: Path):
    """`.task-url { display: block }` outranks the UA stylesheet's
    `[hidden] { display: none }`, so without an explicit override every
    reveal field renders on every unanswered card. Found by opening the
    page, not by reading the markup -- the `hidden` attribute was present
    and correct the whole time."""
    from wpfreeze.ownertasks import _CSS

    assert "[hidden]" in _CSS
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", _CSS)


def test_the_two_action_vocabularies_collide_on_shared_enums():
    """Sections 1/2 and section 3 reuse `replace`, `remove` and `defer`
    with deliberately different owner-facing wording. A label map keyed by
    action value alone silently relabels every link answer with the file
    wording -- which is what the review panel did until it was keyed by
    task type as well. If these ever stop differing, the map may be
    flattened; until then it may not."""
    from wpfreeze.ownertasks import _LINK_ACTIONS, _MISSING_ACTIONS

    link = dict(_LINK_ACTIONS)
    missing = dict(_MISSING_ACTIONS)
    shared = [v for v in link if v and v in missing]

    assert sorted(shared) == ["defer", "remove", "replace"]
    for value in shared:
        assert link[value] != missing[value], value


def test_js_keys_action_labels_by_task_type(tmp_path: Path):
    """Pins the fix for the collision above in the shipped script, the way
    FREEZE_PERMITTED_STEPS is pinned by a test rather than a comment."""
    from wpfreeze.ownertasks import _JS

    assert "ACTION_LABELS[type] = labels" in _JS
    assert "ACTION_LABELS[item.type]" in _JS


@pytest.mark.skipif(
    not (_LANDSCAPES / "broken-external-links.json").exists(),
    reason="no real landscapes capture present locally",
)
def test_render_against_real_landscapes_capture():
    tasks = load_tasks(_LANDSCAPES, _config(_LANDSCAPES))
    html = render_owner_tasks_html(tasks)

    # one card per task, and the type/group counts agree with the data --
    # derived, not pinned, for the reason the loader test above explains
    assert html.count('<article class="task"') == len(tasks.tasks)
    for task_type in ("external_link", "internal_link", "missing_file"):
        assert html.count(f'data-task-type="{task_type}"') == len(tasks.by_type(task_type))
    # the wrapper div/details also carry the attribute, hence -1
    assert html.count('data-group="dead"') - 1 == len(tasks.by_group("dead"))
    assert html.count('data-group="authgated"') - 1 == len(tasks.by_group("authgated"))
    assert html.count("<script") == 2  # data island + behaviour
    assert html.count('data-task-type="missing_file"') == 5
