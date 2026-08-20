from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from wpfreeze.cli import SearchSettings
from wpfreeze.search import (
    SearchIndexResult,
    SearchStats,
    SearchUnavailable,
    apply_search,
    format_search_summary,
    run_pagefind_index,
    write_search_asset,
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html5lib")


# --- apply_search: form tagging --------------------------------------------


def test_role_search_form_is_tagged():
    soup = _soup('<html><body><form role="search"><input name="q"></form></body></html>')
    stats = SearchStats()
    apply_search(soup, SearchSettings(), stats, "search.js")

    form = soup.find("form")
    assert form.get("data-wpfreeze-search") == "1"
    assert stats.forms_tagged == 1
    assert stats.pages_without_form == []


def test_bare_name_s_input_form_is_tagged():
    soup = _soup('<html><body><form><input name="s"></form></body></html>')
    stats = SearchStats()
    apply_search(soup, SearchSettings(), stats, "search.js")

    assert soup.find("form").get("data-wpfreeze-search") == "1"
    assert stats.forms_tagged == 1


def test_other_forms_are_left_untouched():
    soup = _soup('<html><body><form><input name="email"></form></body></html>')
    stats = SearchStats()
    apply_search(soup, SearchSettings(), stats, "search.js")

    assert soup.find("form").get("data-wpfreeze-search") is None
    assert stats.forms_tagged == 0
    assert stats.pages_without_form == [""]


def test_pages_without_form_records_the_output_path():
    soup = _soup("<html><body><p>no forms here</p></body></html>")
    stats = SearchStats()
    apply_search(soup, SearchSettings(), stats, "search.js", page_output="/no-search.html")

    assert stats.pages_without_form == ["/no-search.html"]


# --- apply_search: script tag injection ------------------------------------


def test_script_tag_is_a_module_and_the_last_body_child():
    soup = _soup("<html><body><p>content</p></body></html>")
    stats = SearchStats()
    apply_search(soup, SearchSettings(), stats, "../assets/pagefind-search.js")

    scripts = soup.find_all("script")
    assert len(scripts) == 1
    assert scripts[0]["type"] == "module"
    assert scripts[0]["src"] == "../assets/pagefind-search.js"
    assert soup.find("body").contents[-1] is scripts[0]


def test_script_tag_injected_even_with_no_body_tag():
    soup = BeautifulSoup("<p>fragment only</p>", "html5lib")
    stats = SearchStats()
    apply_search(soup, SearchSettings(), stats, "search.js")

    # html5lib always synthesizes a <body>, so this exercises the same
    # append path but documents that a missing <body> is handled (falls
    # back to the document root) rather than raising.
    assert soup.find("script", attrs={"type": "module"}) is not None


# --- apply_search: body_selectors -------------------------------------------


def test_empty_body_selectors_marks_every_page_matched_and_injects_nothing():
    soup = _soup("<html><body><div class='content'>x</div></body></html>")
    stats = SearchStats()
    apply_search(soup, SearchSettings(), stats, "search.js", page_output="/a.html")

    assert "data-pagefind-body" not in str(soup.find("div"))
    assert stats.pages_with_body_match == 1
    assert stats.pages_without_body_match == []


def test_matching_selector_tags_the_element_and_counts_as_matched():
    soup = _soup("<html><body><article class='entry-content'>x</article></body></html>")
    stats = SearchStats()
    apply_search(
        soup, SearchSettings(body_selectors=(".entry-content",)), stats, "search.js", page_output="/post.html"
    )

    assert soup.find("article").get("data-pagefind-body") == ""
    assert stats.pages_with_body_match == 1
    assert stats.pages_without_body_match == []


def test_non_matching_selector_lands_the_page_in_without_body_match():
    soup = _soup("<html><body><div class='sidebar'>x</div></body></html>")
    stats = SearchStats()
    apply_search(
        soup, SearchSettings(body_selectors=(".entry-content",)), stats, "search.js", page_output="/archive.html"
    )

    assert stats.pages_with_body_match == 0
    assert stats.pages_without_body_match == ["/archive.html"]


def test_multiple_selectors_and_multiple_matches_all_get_tagged():
    soup = _soup(
        "<html><body><article class='entry-content'>a</article>"
        "<div class='extra'>b</div><div class='extra'>c</div></body></html>"
    )
    stats = SearchStats()
    apply_search(
        soup,
        SearchSettings(body_selectors=(".entry-content", ".extra")),
        stats,
        "search.js",
    )

    tagged = soup.select("[data-pagefind-body]")
    assert len(tagged) == 3
    assert stats.pages_with_body_match == 1


# --- write_search_asset -----------------------------------------------------


def test_write_search_asset_writes_to_assets_pagefind_search_js(tmp_path: Path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()

    path = write_search_asset(site_dir)

    assert path == site_dir / "assets" / "pagefind-search.js"
    content = path.read_text(encoding="utf-8")
    assert "type=\"module\"" not in content  # that's the HTML tag, not this file
    assert "import.meta.url" in content
    assert "data-wpfreeze-search" in content


# --- run_pagefind_index ------------------------------------------------------


def _fake_run(monkeypatch, *, stdout: str = "", stderr: str = "", returncode: int = 0, raises: Exception | None = None):
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if raises is not None:
            raise raises

        class _Result:
            pass

        result = _Result()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    monkeypatch.setattr("wpfreeze.search.subprocess.run", _run)
    return calls


def _fake_pagefind_installed(monkeypatch, installed: bool = True):
    monkeypatch.setattr(
        "wpfreeze.search.importlib.util.find_spec",
        lambda name: object() if installed else None,
    )


def test_missing_package_raises_search_unavailable_with_install_command(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch, installed=False)

    with pytest.raises(SearchUnavailable) as exc_info:
        run_pagefind_index(tmp_path, SearchSettings())

    assert "pip install" in str(exc_info.value)
    assert "pipx inject" in str(exc_info.value)


def test_nonzero_exit_returns_not_ok_with_stderr(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch)
    _fake_run(monkeypatch, returncode=1, stderr="boom: something broke")

    result = run_pagefind_index(tmp_path, SearchSettings())

    assert result.ok is False
    assert "boom: something broke" in result.error


def test_success_parses_entry_json_and_sums_page_counts(monkeypatch, tmp_path: Path):
    # run_pagefind_index deletes any existing site_dir/pagefind BEFORE
    # running the indexer (see the stale-chunks test below), so the entry
    # file has to be written by the faked subprocess call itself -- the
    # real `pagefind` binary is what would normally write it there.
    _fake_pagefind_installed(monkeypatch)

    def _run(cmd, **kwargs):
        bundle = tmp_path / "pagefind"
        bundle.mkdir()
        (bundle / "pagefind-entry.json").write_text(
            json.dumps({"languages": {"en": {"page_count": 10}, "fr": {"page_count": 3}}}),
            encoding="utf-8",
        )

        class _Result:
            pass

        result = _Result()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 0
        return result

    monkeypatch.setattr("wpfreeze.search.subprocess.run", _run)

    result = run_pagefind_index(tmp_path, SearchSettings())

    assert result.ok is True
    assert result.pages_indexed == 13
    assert set(result.languages) == {"en", "fr"}


def test_success_with_missing_entry_file_is_still_ok_with_zero_counts(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch)
    _fake_run(monkeypatch, returncode=0)

    result = run_pagefind_index(tmp_path, SearchSettings())

    assert result.ok is True
    assert result.pages_indexed == 0
    assert result.languages == ()


def test_existing_pagefind_dir_is_removed_before_the_run(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch)
    stale = tmp_path / "pagefind"
    stale.mkdir()
    (stale / "old-chunk.json").write_text("{}", encoding="utf-8")
    _fake_run(monkeypatch, returncode=0)

    run_pagefind_index(tmp_path, SearchSettings())

    assert not (stale / "old-chunk.json").exists()


def test_ignore_selectors_become_one_comma_joined_flag(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch)
    calls = _fake_run(monkeypatch, returncode=0)

    run_pagefind_index(tmp_path, SearchSettings(ignore_selectors=("a", "b")))

    cmd = calls[0]
    assert "--exclude-selectors" in cmd
    assert cmd[cmd.index("--exclude-selectors") + 1] == "a, b"


def test_force_language_becomes_a_flag(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch)
    calls = _fake_run(monkeypatch, returncode=0)

    run_pagefind_index(tmp_path, SearchSettings(force_language="en"))

    cmd = calls[0]
    assert "--force-language" in cmd
    assert cmd[cmd.index("--force-language") + 1] == "en"


def test_neither_flag_appears_when_unset(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch)
    calls = _fake_run(monkeypatch, returncode=0)

    run_pagefind_index(tmp_path, SearchSettings())

    cmd = calls[0]
    assert "--exclude-selectors" not in cmd
    assert "--force-language" not in cmd


def test_timeout_raises_search_unavailable(monkeypatch, tmp_path: Path):
    _fake_pagefind_installed(monkeypatch)
    _fake_run(monkeypatch, raises=subprocess.TimeoutExpired(cmd=["x"], timeout=1))

    with pytest.raises(SearchUnavailable):
        run_pagefind_index(tmp_path, SearchSettings())


# --- format_search_summary ---------------------------------------------------


def test_disabled_search_produces_no_summary():
    assert format_search_summary(SearchStats(enabled=False), None) == ""


def test_summary_reports_forms_content_and_index_counts():
    stats = SearchStats(
        enabled=True,
        pages_seen=5,
        forms_tagged=4,
        pages_without_form=["/no-form.html"],
        pages_with_body_match=4,
        pages_without_body_match=["/archive.html"],
    )
    result = SearchIndexResult(ok=True, pages_indexed=4, languages=("en",))

    summary = format_search_summary(stats, result)

    assert "Search:" in summary
    assert "4" in summary and "1 page(s) have no form" in summary
    assert "1 did not (not searchable)" in summary
    assert "language(s): en" in summary


def test_summary_reports_a_failed_index():
    stats = SearchStats(enabled=True, pages_seen=1, forms_tagged=1, pages_with_body_match=1)
    result = SearchIndexResult(ok=False, error="pagefind exploded")

    summary = format_search_summary(stats, result)

    assert "FAILED" in summary
    assert "pagefind exploded" in summary
