from __future__ import annotations

import json
from pathlib import Path

from wpfreeze.cleanup import (
    build_cleanup_todo,
    build_cleanup_todo_html,
    write_cleanup_todo,
    write_cleanup_todo_html,
)


def _write(path: Path, name: str, data: dict) -> None:
    (path / name).write_text(json.dumps(data), encoding="utf-8")


def test_returns_none_when_nothing_has_run_yet(tmp_path: Path):
    assert build_cleanup_todo(tmp_path) is None
    assert write_cleanup_todo(tmp_path) is None
    assert not (tmp_path / "cleanup-todo.md").exists()
    assert build_cleanup_todo_html(tmp_path) is None
    assert write_cleanup_todo_html(tmp_path) is None
    assert not (tmp_path / "cleanup-todo.html").exists()


def test_html_renders_headings_lists_and_inline_markup(tmp_path: Path):
    _write(
        tmp_path,
        "vnu-report.json",
        {
            "documents_checked": 1,
            "total_messages": 2,
            "issues": [
                {"message": 'An "img" element must have an "alt" & no more.', "count": 2, "pages": ["a&b.html"]}
            ],
        },
    )
    write_cleanup_todo(tmp_path)
    html_path = write_cleanup_todo_html(tmp_path)

    assert html_path == tmp_path / "cleanup-todo.html"
    content = html_path.read_text(encoding="utf-8")
    assert "<title>Cleanup checklist</title>" in content
    assert "<h1>Cleanup checklist</h1>" in content
    assert "<h2>Site markup/content quirks</h2>" in content
    assert "<li><strong>2x</strong>" in content
    assert "<code>a&amp;b.html</code>" in content
    # the raw markdown's stray '&' and quotes must be escaped, not
    # interpreted as HTML, and not double-escaped
    assert "must have an &quot;alt&quot; &amp; no more" in content
    assert "&amp;quot;" not in content
    # markdown delimiters themselves must not leak into the HTML as text
    assert "**" not in content


def test_html_renders_markdown_links_as_anchors_opening_in_a_new_tab(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 1,
            "unresolved_samples": [
                {
                    "page": "https://s/submit/",
                    "value": "https://s/touched-by-disposession/",
                    "context": "",
                    "page_output": "/submit.html",
                    "target": "https://s/touched-by-disposession/",
                }
            ],
        },
    )
    html_path = write_cleanup_todo_html(tmp_path)
    content = html_path.read_text(encoding="utf-8")
    assert (
        '<a href="https://s/touched-by-disposession/" target="_blank" rel="noopener">'
        "<code>https://s/touched-by-disposession/</code></a>" in content
    )
    assert (
        '<a href="site/submit.html" target="_blank" rel="noopener"><code>https://s/submit/</code></a>' in content
    )
    # markdown link syntax itself must not leak into the HTML as text
    assert "](" not in content


def test_html_collapses_long_lists_but_short_lists_stay_plain(tmp_path: Path):
    long_issues = [{"message": f"Issue {i}.", "count": 1, "pages": [f"p{i}.html"]} for i in range(15)]
    _write(
        tmp_path,
        "vnu-report.json",
        {"documents_checked": 15, "total_messages": 15, "issues": long_issues},
    )
    _write(
        tmp_path,
        "diagnostics.json",
        {
            "duplicate_local_paths": [{"local_path": "a.html", "urls": ["x", "y"]}],
            "disk_hash_mismatches": [],
            "content_type_shape_mismatches": [],
            "homepage": {"found": True},
        },
    )
    html_path = write_cleanup_todo_html(tmp_path)
    content = html_path.read_text(encoding="utf-8")

    # 15 markup issues > threshold: collapsed behind <details>, all present
    assert "<details><summary>15 items -- click to expand</summary>" in content
    assert "Issue 0." in content
    assert "Issue 14." in content
    # 1 integrity problem <= threshold: plain <ul>, no <details> wrapper
    assert "<ul>\n<li>1 file(s) were claimed" in content


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


def test_broken_reference_links_to_original_and_local_copy(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 1,
            "unresolved_samples": [
                {
                    "page": "https://s/submit/",
                    "value": "https://s/touched-by-disposession/",
                    "context": "",
                    "page_output": "/submit.html",
                    "target": "https://s/touched-by-disposession/",
                }
            ],
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "[`https://s/touched-by-disposession/`](https://s/touched-by-disposession/)" in content
    assert "[`https://s/submit/`](site/submit.html)" in content


def test_broken_reference_without_link_data_falls_back_to_plain_text(tmp_path: Path):
    # Legacy build-report.json shape: no page_output/target to link to.
    _write(
        tmp_path,
        "build-report.json",
        {"unresolved": 1, "unresolved_samples": [["https://s/team/", "https://s/never-captured/"]]},
    )
    content = build_cleanup_todo(tmp_path)
    assert "`https://s/never-captured/`" in content
    assert "[`https://s/never-captured/`]" not in content
    assert "`https://s/team/`" in content
    assert "[`https://s/team/`]" not in content


def test_context_shown_only_to_disambiguate_duplicate_targets(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 3,
            "unresolved_samples": [
                # Unique (page, value): no context needed, none shown even
                # though it was recorded.
                ["https://s/about/", "https://s/dead/", "About us"],
                # Same (page, value) reached via two different links: context
                # is the only way to tell them apart, so it's shown for both.
                ["https://s/submit/", "https://s/touched-by-disposession/", "Touched by Dispossession"],
                ["https://s/submit/", "https://s/touched-by-disposession/", "menu link"],
            ],
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert 'text: "About us"' not in content
    assert 'text: "Touched by Dispossession"' in content
    assert 'text: "menu link"' in content


def test_identical_broken_reference_repeated_on_a_page_is_shown_once(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 2,
            "unresolved_samples": [
                ["https://s/students/", "https://s/kara-isozaki", "Kara Isozaki"],
                ["https://s/students/", "https://s/kara-isozaki", "Kara Isozaki"],
            ],
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert content.count("kara-isozaki") == 1


def test_excised_forms_are_listed_per_page_and_flagged_for_attention(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 3,
                "forms_removed_pages": {"https://s/contact/": 2, "https://s/subscribe/": 1},
                "telemetry_removed": 0,
                "feeds_removed": 0,
                "wp_meta_links_removed": 0,
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Content intentionally excised" in content
    assert "3 form(s) across 2 page(s)" in content
    assert "`https://s/contact/` (2 form(s))" in content
    assert "`https://s/subscribe/`" in content and "(1 form(s))" not in content
    assert "worth a look before you call it done" in content


def test_comment_category_mentions_dead_link_and_blurb_cleanup(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 1,
                "forms_removed_pages": {
                    "https://s/post/": {"count": 1, "output_path": "/post.html", "categories": {"comment": 1}}
                },
                "dead_fragment_links_removed": 3,
                "comment_count_blurbs_removed": 1,
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "3 now-dead in-page link(s) unwrapped" in content
    assert '1 stale "N comments" blurb(s) removed' in content


def test_comment_category_without_extra_cleanup_omits_the_aside(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 1,
                "forms_removed_pages": {
                    "https://s/post/": {"count": 1, "output_path": "/post.html", "categories": {"comment": 1}}
                },
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "Also cleaned up" not in content
    assert "Full list:" in content


def test_search_forms_are_their_own_category_not_other(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 1,
                "forms_removed_pages": {
                    "https://s/purpose/": {"count": 1, "output_path": "/purpose.html", "categories": {"search": 1}}
                },
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "**Search forms**" in content
    assert "strip_search_forms: false" in content
    assert "search: enabled: true" in content
    assert "[`https://s/purpose/`](site/purpose.html)" in content
    # search is a recognized, deliberate removal like comment forms, but
    # unlike comment forms it still pushes "worth a look" (no wrapper
    # cleanup happens for it, see policy.py's _is_search_form)
    assert "worth a look before you call it done" in content


def test_password_forms_are_their_own_category_and_do_not_need_attention(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 1,
                "forms_removed_pages": {
                    "https://s/secret/": {
                        "count": 1,
                        "output_path": "/secret.html",
                        "categories": {"password": 1},
                    }
                },
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "**Password-protected pages**" in content
    assert "[`https://s/secret/`](site/secret.html)" in content
    assert "not thin or broken content" in content
    # Unlike a generic "other"/"search" removal, a password-prompt removal
    # leaves nothing actionable behind -- should not push the headline into
    # "worth a look" on its own.
    assert "worth a look before you call it done" not in content


def test_search_forms_kept_in_place_section(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "search_forms_kept_pages": {
                    "https://s/purpose/": {"count": 1, "output_path": "/purpose.html"},
                    "https://s/contact/": {"count": 2, "output_path": "/contact.html"},
                }
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Search forms left in place" in content
    assert "3 search form(s) across 2 page(s)" in content
    # outlier (2 forms) sorts before the single-form page
    section = content.split("## Search forms left in place", 1)[1]
    assert section.index("contact.html") < section.index("purpose.html")
    assert "[`https://s/contact/`](site/contact.html) (2 form(s))" in content
    assert "does not make them functional" in content
    assert "worth a look before you call it done" in content


def test_search_forms_kept_in_place_absent_when_none_kept(tmp_path: Path):
    _write(tmp_path, "build-report.json", {"unresolved": 0, "unresolved_samples": [], "policy": {}})
    content = build_cleanup_todo(tmp_path)
    assert "Search forms left in place" not in content


def test_search_forms_kept_in_place_reworded_when_index_ok(tmp_path: Path):
    """With a successful Pagefind index behind them, the kept forms are
    actually working search boxes -- the "raw material... not a working
    search box" wording (asserted above for the search.enabled=False case)
    would now be false, and the section should stop asking for a look."""
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "search_forms_kept_pages": {
                    "https://s/purpose/": {"count": 1, "output_path": "/purpose.html"},
                }
            },
            "search": {"enabled": True, "index_ok": True, "indexed_pages": 42},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Search forms left in place" in content
    assert "wired up to a local Pagefind index covering 42 page(s)" in content
    assert "working search boxes, not just raw material" in content
    assert "does not make them functional" not in content
    assert "worth a look before you call it done" not in content


def test_search_forms_kept_in_place_original_wording_when_index_not_ok(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "search_forms_kept_pages": {
                    "https://s/purpose/": {"count": 1, "output_path": "/purpose.html"},
                }
            },
            "search": {"enabled": True, "index_ok": False},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "does not make them functional" in content
    assert "worth a look before you call it done" in content


def test_search_coverage_section_lists_both_categories(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {},
            "search": {
                "enabled": True,
                "pages_without_body_match": ["/category/news.html"],
                "pages_without_form": ["/landing.html"],
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Pages not covered by search" in content
    assert "Absent from the index" in content
    assert "[`/category/news.html`](site/category/news.html)" in content
    assert "In the index but unreachable" in content
    assert "[`/landing.html`](site/landing.html)" in content
    assert "worth a look before you call it done" in content


def test_search_coverage_section_absent_when_search_disabled(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {},
            "search": {"enabled": False, "pages_without_body_match": ["/x.html"]},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "Pages not covered by search" not in content


def test_search_coverage_section_absent_when_both_lists_empty(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {"unresolved": 0, "unresolved_samples": [], "policy": {}, "search": {"enabled": True}},
    )
    content = build_cleanup_todo(tmp_path)
    assert "Pages not covered by search" not in content


def test_thin_content_section_lists_pages_with_word_counts(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {},
            "search": {"enabled": True},
            "content_issues": {
                "pages_scanned": 2,
                "thin_pages": [{"page": "/stub.html", "word_count": 3}],
                "echoed_pages": [],
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Pages with thin content" in content
    assert "[`/stub.html`](site/stub.html)" in content
    assert "3 word(s)" in content
    assert "worth a look before you call it done" in content


def test_thin_content_section_absent_when_no_thin_pages(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {},
            "search": {"enabled": True},
            "content_issues": {"pages_scanned": 2, "thin_pages": [], "echoed_pages": []},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "Pages with thin content" not in content


def test_thin_content_section_absent_when_content_issues_missing(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {"unresolved": 0, "unresolved_samples": [], "policy": {}, "search": {"enabled": True}},
    )
    content = build_cleanup_todo(tmp_path)
    assert "Pages with thin content" not in content


def test_echoed_content_section_lists_pages_with_fraction_sources_and_sample(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {},
            "search": {"enabled": True},
            "content_issues": {
                "pages_scanned": 3,
                "thin_pages": [],
                "echoed_pages": [
                    {
                        "page": "/blog.html",
                        "echo_fraction": 0.75,
                        "shingle_count": 40,
                        "sources": ["/post-1.html", "/post-2.html"],
                        "sample": "a shared excerpt of real text",
                    }
                ],
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Pages that mostly duplicate other pages" in content
    assert "[`/blog.html`](site/blog.html)" in content
    assert "75% shared" in content
    assert "[`/post-1.html`](site/post-1.html)" in content
    assert "a shared excerpt of real text" in content
    assert "search.exclude_pages" in content
    assert "worth a look before you call it done" in content


def test_echoed_content_section_absent_when_no_echoed_pages(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {},
            "search": {"enabled": True},
            "content_issues": {"pages_scanned": 1, "thin_pages": [], "echoed_pages": []},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "Pages that mostly duplicate other pages" not in content


def test_echoed_content_section_sources_beyond_three_are_summarized(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {},
            "search": {"enabled": True},
            "content_issues": {
                "pages_scanned": 5,
                "thin_pages": [],
                "echoed_pages": [
                    {
                        "page": "/blog.html",
                        "echo_fraction": 0.6,
                        "shingle_count": 10,
                        "sources": ["/a.html", "/b.html", "/c.html", "/d.html"],
                        "sample": "",
                    }
                ],
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "+1 more" in content


def test_excised_form_page_links_to_local_copy(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 1,
                "forms_removed_pages": {"https://s/contact/": {"count": 1, "output_path": "/contact.html"}},
                "telemetry_removed": 0,
                "feeds_removed": 0,
                "wp_meta_links_removed": 0,
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "[`https://s/contact/`](site/contact.html)" in content


def test_excised_forms_pages_without_output_path_are_not_linked(tmp_path: Path):
    # Legacy build-report.json shape: a bare int per page, no output_path.
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {"forms_removed": 1, "forms_removed_pages": {"https://s/contact/": 1}},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "`https://s/contact/`" in content
    assert "[`https://s/contact/`]" not in content


def test_comment_forms_removed_on_many_pages_are_listed_in_full_with_outliers_first(tmp_path: Path):
    # Simulates a real site where nearly every page carries the theme's one
    # boilerplate comment form, plus two pages that had an actual extra
    # comment thread (a page with more than one #respond block). Nothing is
    # cut -- all 22 pages must appear -- but the two outliers should sort
    # ahead of the alphabetically-earlier single-form pages so a skimming
    # reader spots them first.
    pages = {
        f"https://s/page-{i}/": {"count": 1, "output_path": f"/page-{i}.html", "categories": {"comment": 1}}
        for i in range(20)
    }
    pages["https://s/aaa-many-forms/"] = {
        "count": 3,
        "output_path": "/aaa-many-forms.html",
        "categories": {"comment": 3},
    }
    pages["https://s/zzz-many-forms/"] = {
        "count": 2,
        "output_path": "/zzz-many-forms.html",
        "categories": {"comment": 2},
    }
    total = sum(p["count"] for p in pages.values())
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {"forms_removed": total, "forms_removed_pages": pages},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "**Comment forms**" in content
    assert "lower priority" in content
    assert "[`https://s/aaa-many-forms/`](site/aaa-many-forms.html) (3 form(s))" in content
    assert "[`https://s/zzz-many-forms/`](site/zzz-many-forms.html) (2 form(s))" in content
    # comment forms alone shouldn't push the "worth a look" headline
    assert "worth a look before you call it done" not in content
    assert "`https://s/page-19/`" in content
    assert "more (see build-report.json" not in content
    section = content.split("## Content intentionally excised", 1)[1]
    assert section.index("aaa-many-forms") < section.index("page-0/")


def test_legacy_forms_without_category_data_bucket_as_unspecified(tmp_path: Path):
    # A build-report.json written before form categorization existed: bare
    # int per page, no "categories" key at all. Can't be split by category,
    # so it lands under "unspecified" rather than being guessed at, and
    # (unlike "comment") still counts toward "worth a look".
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {"forms_removed": 1, "forms_removed_pages": {"https://s/contact/": 1}},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "Forms (from a build made before categorization existed)" in content
    assert "worth a look before you call it done" in content


def test_other_category_forms_push_the_worth_a_look_headline(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 1,
                "forms_removed_pages": {
                    "https://s/contact/": {"count": 1, "output_path": "/contact.html", "categories": {"other": 1}}
                },
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "**Other forms**" in content
    assert "worth a look before you call it done" in content


def test_invisible_excisions_are_summarized_without_a_page_list(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "policy": {
                "forms_removed": 0,
                "forms_removed_pages": {},
                "telemetry_removed": 5,
                "feeds_removed": 2,
                "wp_meta_links_removed": 4,
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Content intentionally excised" in content
    assert "5 telemetry/analytics reference(s), 2 feed link(s), 4 WP protocol-discovery link(s)" in content
    # nothing visible was removed, so this shouldn't drive the "look at
    # this first" headline the way a form removal does
    assert "worth a look before you call it done" not in content


def test_no_excisions_omits_the_section(tmp_path: Path):
    _write(tmp_path, "build-report.json", {"unresolved": 0, "unresolved_samples": [], "policy": {}})
    content = build_cleanup_todo(tmp_path)
    assert "## Content intentionally excised" not in content


def test_unresolved_samples_are_listed_in_full(tmp_path: Path):
    samples = [[f"https://s/page-{i}/", f"https://s/missing-{i}/"] for i in range(30)]
    _write(tmp_path, "build-report.json", {"unresolved": 30, "unresolved_samples": samples})
    content = build_cleanup_todo(tmp_path)
    assert "missing-0" in content
    assert "missing-29" in content
    assert "more (see build-report.json)" not in content


def test_unresolved_samples_short_of_count_is_still_summarized(tmp_path: Path):
    # A build-report.json written before build.py stopped capping
    # unresolved_samples at 50 can have fewer samples than `unresolved`.
    samples = [[f"https://s/page-{i}/", f"https://s/missing-{i}/"] for i in range(10)]
    _write(tmp_path, "build-report.json", {"unresolved": 30, "unresolved_samples": samples})
    content = build_cleanup_todo(tmp_path)
    assert "missing-9" in content
    assert "...and 20 more (see build-report.json)" in content


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
    # `pages` are already site-relative output paths, so the example links directly
    assert "[`team.html`](site/team.html)" in content


def test_vnu_issues_are_listed_in_full(tmp_path: Path):
    issues = [
        {"message": f"Issue number {i}.", "count": 1, "pages": [f"page-{i}.html"]} for i in range(30)
    ]
    _write(
        tmp_path,
        "vnu-report.json",
        {"documents_checked": 30, "total_messages": 30, "issues": issues},
    )
    content = build_cleanup_todo(tmp_path)
    assert "Issue number 0." in content
    assert "Issue number 29." in content
    assert "more distinct issue" not in content


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


def test_broken_local_links_reach_the_checklist(tmp_path: Path):
    """verify_site's findings used to be printed and discarded, so a build
    that exited 1 over broken local links still produced a checklist
    saying the capture was clean."""
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "verification": {
                "documents": 3,
                "checked": 12,
                "external": 1,
                "skipped": 0,
                "broken": 2,
                "broken_samples": [
                    {"source": "team.html", "reference": "./photo.jpg", "target": "photo.jpg", "reason": "missing"},
                    {"source": "about.html", "reference": "./doc.pdf", "target": "doc.pdf", "reason": "missing"},
                ],
            },
        },
    )
    _write(tmp_path, "vnu-report.json", {"issues": [], "total_messages": 0, "documents_checked": 3})
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
    assert "worth a look before you call it done" in content
    assert "## Local link verification" in content
    assert "./photo.jpg" in content and "team.html" in content
    # `source` is already a site-relative output path, so it links directly
    assert "[`team.html`](site/team.html)" in content


def test_broken_local_links_are_listed_in_full(tmp_path: Path):
    samples = [
        {"source": f"page-{i}.html", "reference": f"./missing-{i}.jpg", "target": f"missing-{i}.jpg", "reason": "missing"}
        for i in range(30)
    ]
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "verification": {
                "documents": 30,
                "checked": 30,
                "external": 0,
                "skipped": 0,
                "broken": 30,
                "broken_samples": samples,
            },
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "missing-0.jpg" in content
    assert "missing-29.jpg" in content
    assert "more (see build-report.json)" not in content


def test_no_verify_build_says_verification_did_not_run(tmp_path: Path):
    _write(tmp_path, "build-report.json", {"unresolved": 0, "unresolved_samples": [], "verification": None})
    content = build_cleanup_todo(tmp_path)
    assert "--no-verify" in content


def test_clean_verification_adds_no_noise(tmp_path: Path):
    _write(
        tmp_path,
        "build-report.json",
        {
            "unresolved": 0,
            "unresolved_samples": [],
            "verification": {"documents": 3, "checked": 12, "external": 1, "skipped": 0, "broken": 0, "broken_samples": []},
        },
    )
    content = build_cleanup_todo(tmp_path)
    assert "## Local link verification" not in content
