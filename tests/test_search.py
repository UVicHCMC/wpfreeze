from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from wpfreeze.cli import SearchSettings
from wpfreeze.search import (
    ECHO_THRESHOLD,
    MIN_WORDS,
    SHINGLE_SIZE,
    ContentIssues,
    EchoedPage,
    SearchIndexResult,
    SearchStats,
    SearchUnavailable,
    ThinContentPage,
    apply_search,
    extract_indexed_text,
    format_content_issues_summary,
    format_search_summary,
    run_pagefind_index,
    scan_content_issues,
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


def test_exclude_pages_skips_tagging_and_records_exclusion():
    soup = _soup("<html><body><article class='entry-content'>x</article></body></html>")
    stats = SearchStats()
    apply_search(
        soup,
        SearchSettings(body_selectors=(".entry-content",), exclude_pages=("/blog.html",)),
        stats,
        "search.js",
        page_output="/blog.html",
    )

    assert soup.find("article").get("data-pagefind-body") is None
    assert stats.pages_excluded_by_config == ["/blog.html"]
    # Not counted as a selector mismatch -- the owner excluded this on
    # purpose, it's not "the selector should have matched and didn't".
    assert stats.pages_without_body_match == []
    assert stats.pages_with_body_match == 0


def test_exclude_pages_does_not_affect_unlisted_pages():
    soup = _soup("<html><body><article class='entry-content'>x</article></body></html>")
    stats = SearchStats()
    apply_search(
        soup,
        SearchSettings(body_selectors=(".entry-content",), exclude_pages=("/other.html",)),
        stats,
        "search.js",
        page_output="/post.html",
    )

    assert soup.find("article").get("data-pagefind-body") == ""
    assert stats.pages_with_body_match == 1
    assert stats.pages_excluded_by_config == []


def test_exclude_pages_does_not_affect_form_tagging():
    soup = _soup(
        "<html><body><form role='search'><input name='q'></form>"
        "<article class='entry-content'>x</article></body></html>"
    )
    stats = SearchStats()
    apply_search(
        soup,
        SearchSettings(body_selectors=(".entry-content",), exclude_pages=("/blog.html",)),
        stats,
        "search.js",
        page_output="/blog.html",
    )

    # An excluded page can still host a working search box -- only its own
    # content stops being indexed.
    assert soup.find("form").get("data-wpfreeze-search") == "1"
    assert stats.forms_tagged == 1


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


def test_summary_reports_pages_excluded_by_config():
    stats = SearchStats(
        enabled=True, pages_seen=2, forms_tagged=2, pages_with_body_match=1,
        pages_excluded_by_config=["/blog.html"],
    )
    summary = format_search_summary(stats, SearchIndexResult(ok=True, pages_indexed=1))

    assert "excluded by config" in summary
    assert "1 page(s)" in summary
    assert "search.exclude_pages" in summary


def test_summary_omits_exclusion_line_when_none_excluded():
    stats = SearchStats(enabled=True, pages_seen=1, forms_tagged=1, pages_with_body_match=1)
    summary = format_search_summary(stats, SearchIndexResult(ok=True, pages_indexed=1))

    assert "excluded by config" not in summary


# --- extract_indexed_text ----------------------------------------------------


def test_extract_returns_whole_body_when_no_sitewide_tagging():
    soup = _soup("<html><body><nav>menu</nav><p>Real content here</p></body></html>")
    text = extract_indexed_text(soup, SearchSettings(), body_tagged_sitewide=False)

    assert text == "menu Real content here"


def test_extract_returns_none_when_sitewide_tagged_but_this_page_has_no_match():
    soup = _soup("<html><body><p>untagged content</p></body></html>")
    text = extract_indexed_text(soup, SearchSettings(), body_tagged_sitewide=True)

    assert text is None


def test_extract_returns_tagged_region_text_only():
    soup = _soup(
        "<html><body><nav>menu</nav>"
        "<article data-pagefind-body>Real content here</article></body></html>"
    )
    text = extract_indexed_text(soup, SearchSettings(), body_tagged_sitewide=True)

    assert text == "Real content here"
    assert "menu" not in text


def test_extract_returns_empty_string_not_none_for_indexed_but_empty_region():
    # Distinguishes "not indexed" (None) from "indexed but holds nothing"
    # (""), which callers must not conflate -- see search.py's docstring.
    soup = _soup("<html><body><article data-pagefind-body></article></body></html>")
    text = extract_indexed_text(soup, SearchSettings(), body_tagged_sitewide=True)

    assert text == ""
    assert text is not None


def test_extract_honours_ignore_selectors_without_mutating_the_soup():
    soup = _soup(
        "<html><body><article data-pagefind-body>"
        "Keep this <div class='related-posts'>drop this</div> and this"
        "</article></body></html>"
    )
    text = extract_indexed_text(
        soup, SearchSettings(ignore_selectors=(".related-posts",)), body_tagged_sitewide=True
    )

    assert text == "Keep this and this"
    # The parsed soup itself (what build_site would write) is untouched --
    # this function only reads.
    assert soup.select_one(".related-posts") is not None


def test_extract_get_text_separator_does_not_fuse_inline_tags():
    # A bare get_text() would fuse "one" and "two" into "onetwo",
    # corrupting both word counts and shingle boundaries.
    soup = _soup("<html><body><article data-pagefind-body><p>one<em>two</em></p></article></body></html>")
    text = extract_indexed_text(soup, SearchSettings(), body_tagged_sitewide=True)

    assert text == "one two"


# --- scan_content_issues ------------------------------------------------------


def _write_page(site_dir: Path, output_path: str, body_html: str) -> None:
    path = site_dir / output_path.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<html><body>{body_html}</body></html>", encoding="utf-8")


def _words(n: int, prefix: str) -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_scan_reports_thin_pages_under_min_words(tmp_path: Path):
    site_dir = tmp_path / "site"
    _write_page(site_dir, "/thin.html", "<p>only four words here</p>")
    _write_page(site_dir, "/ok.html", f"<p>{_words(MIN_WORDS, 'w')}</p>")

    issues = scan_content_issues(site_dir, SearchSettings())

    assert issues.thin_pages == [ThinContentPage(page="/thin.html", word_count=4)]
    assert issues.echoed_pages == []


def test_scan_min_words_boundary_exact_count_is_not_thin(tmp_path: Path):
    site_dir = tmp_path / "site"
    _write_page(site_dir, "/exact.html", f"<p>{_words(MIN_WORDS, 'w')}</p>")

    issues = scan_content_issues(site_dir, SearchSettings())

    assert issues.thin_pages == []


def test_scan_one_under_min_words_is_thin(tmp_path: Path):
    site_dir = tmp_path / "site"
    _write_page(site_dir, "/short.html", f"<p>{_words(MIN_WORDS - 1, 'w')}</p>")

    issues = scan_content_issues(site_dir, SearchSettings())

    assert len(issues.thin_pages) == 1
    assert issues.thin_pages[0].word_count == MIN_WORDS - 1


def test_scan_thin_page_in_acknowledged_thin_pages_is_marked_acknowledged(tmp_path: Path):
    site_dir = tmp_path / "site"
    _write_page(site_dir, "/thin.html", "<p>only four words here</p>")

    issues = scan_content_issues(site_dir, SearchSettings(acknowledged_thin_pages=("/thin.html",)))

    assert issues.thin_pages == [ThinContentPage(page="/thin.html", word_count=4, acknowledged=True)]


def test_scan_thin_page_not_in_acknowledged_thin_pages_is_not_marked(tmp_path: Path):
    site_dir = tmp_path / "site"
    _write_page(site_dir, "/thin.html", "<p>only four words here</p>")

    issues = scan_content_issues(site_dir, SearchSettings(acknowledged_thin_pages=("/other.html",)))

    assert issues.thin_pages[0].acknowledged is False


def test_scan_acknowledged_thin_pages_still_counts_toward_pages_scanned(tmp_path: Path):
    # Acknowledging a page changes whether the checklist nags about it
    # (cleanup.py's business), not whether scan_content_issues considers
    # it indexed/scanned -- the calibration denominator (see
    # ContentIssues.pages_scanned) must stay meaningful regardless of
    # acknowledgement.
    site_dir = tmp_path / "site"
    _write_page(site_dir, "/thin.html", "<p>only four words here</p>")

    issues = scan_content_issues(site_dir, SearchSettings(acknowledged_thin_pages=("/thin.html",)))

    assert issues.pages_scanned == 1


def _write_password_protected_page(site_dir: Path, output_path: str) -> None:
    # Mirrors what policy.py's _strip_forms leaves behind after removing a
    # WordPress password prompt: the marker on <body>, and (per the same
    # removal) nothing left in the content region.
    path = site_dir / output_path.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<html><body data-wpfreeze-password-protected="1"></body></html>',
        encoding="utf-8",
    )


def test_scan_skips_pages_marked_password_protected_by_policy(tmp_path: Path):
    site_dir = tmp_path / "site"
    _write_password_protected_page(site_dir, "/secret.html")
    _write_page(site_dir, "/ok.html", f"<p>{_words(MIN_WORDS, 'w')}</p>")

    issues = scan_content_issues(site_dir, SearchSettings())

    # Not reported as thin (already reported under "Password-protected
    # pages" in the excised-content section) and not counted in the
    # pages_scanned denominator either -- same treatment as a page
    # extract_indexed_text returns None for.
    assert issues.thin_pages == []
    assert issues.pages_scanned == 1


def test_scan_password_protected_page_skipped_even_if_it_somehow_has_text(tmp_path: Path):
    # Belt and suspenders: the marker alone is enough to skip the page,
    # regardless of what's in its body -- this should never happen in
    # practice (the marker is only set when the form removal left the page
    # empty), but the skip must not depend on emptiness to be correct.
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    path = site_dir / "secret.html"
    path.write_text(
        f'<html><body data-wpfreeze-password-protected="1">'
        f'<p>{_words(MIN_WORDS, "w")}</p></body></html>',
        encoding="utf-8",
    )

    issues = scan_content_issues(site_dir, SearchSettings())

    assert issues.thin_pages == []
    assert issues.echoed_pages == []
    assert issues.pages_scanned == 0


def test_scan_respects_sitewide_tagging_not_per_page(tmp_path: Path):
    # One page tagged anywhere makes Pagefind's rule sitewide: an untagged
    # page is NOT indexed at all (thin or otherwise), not whole-body
    # fallback -- the sitewide-tagging correctness trap.
    site_dir = tmp_path / "site"
    _write_page(
        site_dir, "/tagged.html",
        f"<article data-pagefind-body>{_words(MIN_WORDS, 'w')}</article>",
    )
    _write_page(site_dir, "/untagged.html", f"<p>{_words(MIN_WORDS, 'w')}</p>")

    issues = scan_content_issues(site_dir, SearchSettings())

    # untagged.html has real content but no tagged region -- must not
    # appear as thin (that would mean it was treated as indexed).
    assert [p.page for p in issues.thin_pages] == []
    assert issues.pages_scanned == 1  # only the tagged page was indexed


def test_scan_sitewide_tagging_prefilter_does_not_false_positive_on_visible_text(tmp_path: Path):
    # _detect_sitewide_tagging's substring pre-filter (checking raw text
    # for "data-pagefind-body" before parsing, to avoid holding every
    # page's soup in memory at once) must not trust the substring alone --
    # a page whose *visible text* happens to mention the literal string
    # (documenting this very feature, say) is not the same as a page
    # carrying the real attribute. Only a parsed soup.select_one match may
    # decide. Confirmed this fixture actually distinguishes the two: a
    # naive substring-only check gets it wrong (True) where the real,
    # parse-and-confirm check gets it right (False).
    site_dir = tmp_path / "site"
    _write_page(
        site_dir, "/docs.html",
        f"<p>This page explains the data-pagefind-body attribute.</p> {_words(MIN_WORDS, 'w')}",
    )
    _write_page(site_dir, "/other.html", f"<p>{_words(MIN_WORDS, 'w')}</p>")

    issues = scan_content_issues(site_dir, SearchSettings())

    # No page carries the real attribute anywhere, so tagging is OFF
    # sitewide -- both pages fall back to whole-body indexing. A
    # substring-only false positive would instead treat this as sitewide
    # tagging being on, and (since neither page has a real
    # data-pagefind-body element) both pages would vanish from the index
    # entirely.
    assert issues.pages_scanned == 2
    assert issues.thin_pages == []


def test_scan_direction_regression_aggregator_flagged_sources_not(tmp_path: Path):
    """The draft's bug, regression-tested directly: a page assembled from
    fragments of several other pages must be flagged as the duplicate,
    and the source pages it was built from must NOT be flagged just for
    contributing a fragment. Corpus-wide echo fraction, not the draft's
    pairwise containment grouped by the longer page (which inverted this).
    """
    site_dir = tmp_path / "site"
    tag = "data-pagefind-body"
    _write_page(site_dir, "/source-a.html", f"<article {tag}>{_words(30, 'alpha')}</article>")
    _write_page(site_dir, "/source-b.html", f"<article {tag}>{_words(30, 'bravo')}</article>")
    _write_page(site_dir, "/source-c.html", f"<article {tag}>{_words(30, 'charlie')}</article>")
    # Built from the first 10 words of each source -- a real fragment
    # from each, contiguous, so their shingles genuinely overlap.
    fragment_a = _words(10, "alpha")
    fragment_b = _words(10, "bravo")
    fragment_c = _words(10, "charlie")
    _write_page(
        site_dir, "/aggregator.html",
        f"<article {tag}>{fragment_a} {fragment_b} {fragment_c}</article>",
    )

    issues = scan_content_issues(site_dir, SearchSettings())

    flagged = {p.page for p in issues.echoed_pages}
    assert flagged == {"/aggregator.html"}
    agg = next(p for p in issues.echoed_pages if p.page == "/aggregator.html")
    assert agg.echo_fraction >= ECHO_THRESHOLD
    assert set(agg.sources) == {"/source-a.html", "/source-b.html", "/source-c.html"}


def test_scan_truncated_teaser_still_flags_aggregator(tmp_path: Path):
    """The specific case the draft's pairwise-containment algorithm
    missed: a *truncated* excerpt of a longer post, not a verbatim full
    copy. Real shape: ~60-word excerpt of a ~250-word post."""
    site_dir = tmp_path / "site"
    tag = "data-pagefind-body"
    post_words = _words(250, "post")
    _write_page(site_dir, "/real-post.html", f"<article {tag}>{post_words}</article>")
    excerpt = " ".join(post_words.split()[:60])  # contiguous truncated teaser
    _write_page(site_dir, "/blog.html", f"<article {tag}>{excerpt}</article>")

    issues = scan_content_issues(site_dir, SearchSettings())

    flagged = {p.page for p in issues.echoed_pages}
    assert "/blog.html" in flagged
    assert "/real-post.html" not in flagged


def test_scan_pages_sharing_only_short_boilerplate_are_not_flagged(tmp_path: Path):
    site_dir = tmp_path / "site"
    tag = "data-pagefind-body"
    boilerplate = "all rights reserved wpfreeze test site"  # 6 words, shared
    _write_page(site_dir, "/a.html", f"<article {tag}>{_words(30, 'alpha')} {boilerplate}</article>")
    _write_page(site_dir, "/b.html", f"<article {tag}>{_words(30, 'bravo')} {boilerplate}</article>")

    issues = scan_content_issues(site_dir, SearchSettings())

    assert issues.echoed_pages == []


def test_scan_thin_page_excluded_from_echo_pass_and_no_zero_division(tmp_path: Path):
    site_dir = tmp_path / "site"
    tag = "data-pagefind-body"
    _write_page(site_dir, "/stub.html", f"<article {tag}>hi</article>")  # 1 word, < SHINGLE_SIZE too
    _write_page(site_dir, "/other-stub.html", f"<article {tag}>hi</article>")

    issues = scan_content_issues(site_dir, SearchSettings())  # must not raise

    assert len(issues.thin_pages) == 2
    assert issues.echoed_pages == []


def test_scan_sources_ranked_by_contributed_shingle_count(tmp_path: Path):
    site_dir = tmp_path / "site"
    tag = "data-pagefind-body"
    # >= MIN_WORDS on its own, so the aggregator itself isn't thin.
    shared_big = _words(30, "shared")
    # small-source's 5 "shared" words are a prefix of shared_big's 30 (both
    # start at i=0), so it genuinely overlaps, just far less.
    _write_page(site_dir, "/big-source.html", f"<article {tag}>{shared_big} {_words(15, 'onlybig')}</article>")
    _write_page(
        site_dir, "/small-source.html",
        f"<article {tag}>{_words(5, 'shared')} {_words(20, 'onlysmall')}</article>",
    )
    _write_page(site_dir, "/aggregator.html", f"<article {tag}>{shared_big}</article>")

    issues = scan_content_issues(site_dir, SearchSettings())

    agg = next(p for p in issues.echoed_pages if p.page == "/aggregator.html")
    assert agg.sources[0] == "/big-source.html"  # contributes more shingles than small-source


def test_scan_sample_is_a_contiguous_normalized_run(tmp_path: Path):
    """sample is the longest contiguous echoed run of NORMALIZED text --
    lowercased, punctuation stripped -- not the original page text
    verbatim (see EchoedPage.sample's field comment). Uses a fixture with
    real mixed case and punctuation on purpose: an all-lowercase,
    punctuation-free fixture (e.g. _words()) cannot distinguish "returns
    normalized text" from "returns the original text", which is exactly
    how the original version of this test shipped without catching that
    the code was never verbatim.
    """
    site_dir = tmp_path / "site"
    tag = "data-pagefind-body"
    fragment = (
        "The Book Launch for Tobi Dahmen's graphic novel, Al-Fazia, "
        "took place on May 26, 2026, in Hamburg, Germany."
    )
    padding = _words(10, "extra")  # pads both pages past MIN_WORDS
    _write_page(site_dir, "/source.html", f"<article {tag}>{fragment} {padding}</article>")
    _write_page(site_dir, "/aggregator.html", f"<article {tag}>{fragment} {padding}</article>")

    issues = scan_content_issues(site_dir, SearchSettings())

    agg = next(p for p in issues.echoed_pages if p.page == "/aggregator.html")
    assert agg.sample.strip() != ""
    assert agg.sample == agg.sample.lower()  # no uppercase survives
    assert "'" not in agg.sample and "," not in agg.sample  # no punctuation survives
    assert "dahmen" in agg.sample  # still recognizably the same content
    assert agg.sample not in fragment  # not literally a substring of the original, cased/punctuated text


def test_scan_extract_respects_ignore_selectors(tmp_path: Path):
    # A page whose real content is exactly MIN_WORDS, plus a widget that
    # would push it over MIN_WORDS+1 if counted -- ignore_selectors must
    # strip the widget before the word count is taken, matching what
    # Pagefind's own --exclude-selectors would actually index.
    site_dir = tmp_path / "site"
    tag = "data-pagefind-body"
    _write_page(
        site_dir, "/page.html",
        f"<article {tag}>{_words(MIN_WORDS - 1, 'w')} "
        f"<div class='widget'>{_words(10, 'noise')}</div></article>",
    )

    with_ignore = scan_content_issues(site_dir, SearchSettings(ignore_selectors=(".widget",)))
    without_ignore = scan_content_issues(site_dir, SearchSettings())

    # MIN_WORDS - 1 real words alone is thin; with the 10-word widget
    # counted it would not be.
    assert with_ignore.thin_pages == [ThinContentPage(page="/page.html", word_count=MIN_WORDS - 1)]
    assert without_ignore.thin_pages == []


def test_scan_exclude_pages_composes_with_apply_search_end_to_end(tmp_path: Path):
    """scan_content_issues needs no awareness of exclude_pages -- it
    composes for free through the sitewide-tagging mechanism apply_search
    already uses. Proves that by running the actual pipeline both functions
    sit in (apply_search writes the markup scan_content_issues then reads),
    not by testing each function against its own hand-built fixture in
    isolation."""
    site_dir = tmp_path / "site"
    settings = SearchSettings(body_selectors=(".entry-content",), exclude_pages=("/blog.html",))
    pages = {
        "/blog.html": "<article class='entry-content'>" + _words(40, "w") + "</article>",
        "/post.html": "<article class='entry-content'>" + _words(40, "p") + "</article>",
    }
    for output_path, body_html in pages.items():
        soup = _soup(f"<html><body>{body_html}</body></html>")
        apply_search(soup, settings, SearchStats(), "search.js", page_output=output_path)
        path = site_dir / output_path.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(soup), encoding="utf-8")

    assert "data-pagefind-body" not in (site_dir / "blog.html").read_text(encoding="utf-8")

    issues = scan_content_issues(site_dir, settings)

    # blog.html never appears anywhere -- not indexed, not thin, not
    # echoed, not counted -- exactly like a page body_selectors itself
    # excluded, with zero special-casing in scan_content_issues.
    assert issues.pages_scanned == 1
    assert issues.thin_pages == []
    assert issues.echoed_pages == []


# --- format_content_issues_summary --------------------------------------------


def test_content_issues_summary_reports_counts():
    issues = ContentIssues(
        pages_scanned=5,
        thin_pages=[ThinContentPage(page="/a.html", word_count=3)],
        echoed_pages=[EchoedPage(page="/b.html", echo_fraction=0.7, shingle_count=10)],
    )
    summary = format_content_issues_summary(issues)

    assert "thin content" in summary
    assert "echoed content" in summary
    assert str(MIN_WORDS) in summary
    # Both counts reported against pages_scanned, not bare counts -- the
    # ">15% of pages flagged means the threshold is wrong" rule of thumb
    # is unusable without the denominator.
    assert "1 of 5 page(s) scanned" in summary
    assert "acknowledged" not in summary


def test_content_issues_summary_notes_acknowledged_count():
    issues = ContentIssues(
        pages_scanned=5,
        thin_pages=[
            ThinContentPage(page="/a.html", word_count=3, acknowledged=True),
            ThinContentPage(page="/b.html", word_count=4),
        ],
    )
    summary = format_content_issues_summary(issues)

    assert "2 of 5 page(s) scanned" in summary
    assert "(1 acknowledged)" in summary
