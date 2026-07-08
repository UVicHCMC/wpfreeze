from __future__ import annotations

from pathlib import Path

import pytest

from wpfreeze.inventory import (
    DbConfig,
    WxrDocument,
    WxrItem,
    discover_wxr,
    extract_links_from_rest_items,
    parse_rest_total_pages,
    parse_robots_sitemaps,
    parse_sitemap_xml,
    parse_wxr_xml,
    rest_collection_url,
    run_mysql_query,
    wxr_author_urls,
    wxr_post_urls,
    wxr_term_urls,
)
from wpfreeze.manifest import Manifest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_WXR_PATH = FIXTURES_DIR / "sample-wxr.xml"


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def test_parse_robots_sitemaps_single():
    robots = "User-agent: *\nDisallow: /wp-admin/\nSitemap: https://example.com/wp-sitemap.xml\n"
    assert parse_robots_sitemaps(robots) == ["https://example.com/wp-sitemap.xml"]


def test_parse_robots_sitemaps_multiple_case_insensitive():
    robots = "SITEMAP: https://example.com/a.xml\nsitemap:https://example.com/b.xml\n"
    assert parse_robots_sitemaps(robots) == [
        "https://example.com/a.xml",
        "https://example.com/b.xml",
    ]


def test_parse_robots_sitemaps_none_present():
    assert parse_robots_sitemaps("User-agent: *\nDisallow: /\n") == []


# ---------------------------------------------------------------------------
# sitemap XML
# ---------------------------------------------------------------------------


def test_parse_sitemap_urlset():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/page-1/</loc></url>
        <url><loc>https://example.com/page-2/</loc></url>
    </urlset>"""
    pages, nested = parse_sitemap_xml(xml)
    assert pages == ["https://example.com/page-1/", "https://example.com/page-2/"]
    assert nested == []


def test_parse_sitemap_index():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>https://example.com/wp-sitemap-posts-1.xml</loc></sitemap>
        <sitemap><loc>https://example.com/wp-sitemap-pages-1.xml</loc></sitemap>
    </sitemapindex>"""
    pages, nested = parse_sitemap_xml(xml)
    assert pages == []
    assert nested == [
        "https://example.com/wp-sitemap-posts-1.xml",
        "https://example.com/wp-sitemap-pages-1.xml",
    ]


def test_parse_sitemap_no_namespace_still_parses():
    xml = b"<urlset><url><loc>https://example.com/x/</loc></url></urlset>"
    pages, nested = parse_sitemap_xml(xml)
    assert pages == ["https://example.com/x/"]


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


def test_rest_collection_url_builds_paginated_query():
    url = rest_collection_url("https://example.com", "posts", page=2, per_page=50)
    assert url == "https://example.com/wp-json/wp/v2/posts?per_page=50&page=2"


def test_rest_collection_url_strips_trailing_slash_on_base():
    url = rest_collection_url("https://example.com/", "pages")
    assert url.startswith("https://example.com/wp-json/wp/v2/pages")


def test_parse_rest_total_pages_reads_header_case_insensitively():
    assert parse_rest_total_pages({"X-WP-TotalPages": "7"}) == 7
    assert parse_rest_total_pages({"x-wp-totalpages": "3"}) == 3


def test_parse_rest_total_pages_defaults_to_one_when_absent_or_bad():
    assert parse_rest_total_pages({}) == 1
    assert parse_rest_total_pages({"X-WP-TotalPages": "not-a-number"}) == 1


def test_extract_links_from_rest_items():
    items = [
        {"id": 1, "link": "https://example.com/post-1/"},
        {"id": 2, "link": "https://example.com/post-2/"},
        {"id": 3},  # malformed/missing link, skipped
    ]
    assert extract_links_from_rest_items(items) == [
        "https://example.com/post-1/",
        "https://example.com/post-2/",
    ]


# ---------------------------------------------------------------------------
# WXR (WordPress XML export) inventory
# ---------------------------------------------------------------------------


def test_strip_invalid_xml_chars_removes_control_chars():
    from wpfreeze.inventory import _strip_invalid_xml_chars

    cleaned, count = _strip_invalid_xml_chars("before\x1eafter\x1f!")
    assert cleaned == "beforeafter!"
    assert count == 2


def test_strip_invalid_xml_chars_leaves_tab_newline_cr_alone():
    from wpfreeze.inventory import _strip_invalid_xml_chars

    cleaned, count = _strip_invalid_xml_chars("a\tb\nc\rd")
    assert cleaned == "a\tb\nc\rd"
    assert count == 0


def test_parse_wxr_xml_sanitizes_invalid_control_chars_before_parsing():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss><channel><item><wp:post_id xmlns:wp="http://wordpress.org/export/1.2/">1'
        "</wp:post_id></item></channel></rss>"
    ).encode("utf-8")
    # Inject a raw XML-invalid control character into otherwise-valid bytes --
    # a strict parse of the unsanitized bytes must fail, and parse_wxr_xml
    # must succeed anyway (this is the regression test for lxml's
    # recover=True silently dropping content instead).
    poisoned = xml.replace(b"<item>", b"<item>\x1e")
    with pytest.raises(Exception):
        from lxml import etree

        etree.fromstring(poisoned)
    document = parse_wxr_xml(poisoned)
    assert len(document.items) == 1


def test_parse_wxr_xml_extracts_authors_categories_and_items():
    document = parse_wxr_xml(SAMPLE_WXR_PATH.read_bytes())
    assert {a.login: a.author_id for a in document.authors} == {"alice": "1", "bob": "2"}
    assert {c.nicename: c.term_id for c in document.categories} == {"news": "5"}
    assert len(document.items) == 7


def test_wxr_post_urls_applies_type_allowlist_and_status_quirk():
    document = parse_wxr_xml(SAMPLE_WXR_PATH.read_bytes())
    urls = [item.url for item in wxr_post_urls(document)]
    # Published post, published page, the inherit-status attachment (kept
    # despite not being 'publish' -- this is the single most important
    # regression case here), and the two edge-case posts are all kept;
    # the draft and the publish-status nav_menu_item junk are excluded.
    assert urls == [
        "https://example.com/news/published-post/",
        "https://example.com/?page_id=103",
        "https://example.com/news/published-post/an-attachment/",
        "https://example.com/news/orphan-category/",
        "https://example.com/news/ghost-author/",
    ]


def test_wxr_post_urls_keeps_attachment_with_inherit_status():
    document = WxrDocument(
        authors=[],
        categories=[],
        items=[
            WxrItem("1", "attachment", "inherit", "https://example.com/img.jpg", "alice", []),
            WxrItem("2", "attachment", "publish", "https://example.com/never.jpg", "alice", []),
        ],
    )
    urls = [item.url for item in wxr_post_urls(document)]
    assert urls == ["https://example.com/img.jpg"]


def test_wxr_term_urls_resolves_category_nicename_to_id_and_skips_unresolvable():
    document = parse_wxr_xml(SAMPLE_WXR_PATH.read_bytes())
    urls = [item.url for item in wxr_term_urls("https://example.com", document)]
    assert urls == ["https://example.com/?cat=5", "https://example.com/?tag=announcement"]


def test_wxr_author_urls_restricted_to_authors_of_kept_published_posts():
    document = parse_wxr_xml(SAMPLE_WXR_PATH.read_bytes())
    urls = [item.url for item in wxr_author_urls("https://example.com", document)]
    # alice (post 101) and bob (page 103, post 106) are both used; the
    # ghost creator on post 107 has no top-level <wp:author> entry and is
    # skipped rather than raising.
    assert urls == ["https://example.com/?author=1", "https://example.com/?author=2"]


def test_discover_wxr_seeds_manifest_with_discovered_via_xml_backup():
    manifest = Manifest()
    found = discover_wxr(manifest, "https://example.com", SAMPLE_WXR_PATH)
    assert found is True
    record = manifest.get_or_create("https://example.com/news/published-post/")
    assert "xml_backup" in record.discovered_via
    record = manifest.get_or_create("https://example.com/?cat=5")
    assert "xml_backup" in record.discovered_via
    record = manifest.get_or_create("https://example.com/?author=1")
    assert "xml_backup" in record.discovered_via


def test_discover_wxr_returns_false_on_missing_file():
    manifest = Manifest()
    found = discover_wxr(manifest, "https://example.com", Path("/no/such/file.xml"))
    assert found is False
    assert len(manifest) == 0


def test_discover_wxr_returns_false_on_unparseable_file(tmp_path: Path):
    bad_path = tmp_path / "bad.xml"
    bad_path.write_bytes(b"not xml at all <<<")
    manifest = Manifest()
    found = discover_wxr(manifest, "https://example.com", bad_path)
    assert found is False


# Local-only smoke test against a real, untracked WXR export sitting in the
# repo root (never committed -- contains the site's admin email, draft
# content, and private posts). Skipped everywhere else, including CI.
# Deliberately does not assert an exact stripped-character count: a fresh
# re-export of the same site produces a different file with different
# counts. The hand-built fixture above is the permanent regression suite;
# this is just a sanity check against one real-world data point.
REAL_WXR_PATH = FIXTURES_DIR.parent.parent / "site-c.WordPress.2026-07-08.xml"


@pytest.mark.skipif(not REAL_WXR_PATH.exists(), reason="real-world WXR sample not present locally")
def test_parse_wxr_xml_handles_real_world_export():
    from wpfreeze.inventory import _is_kept_published

    document = parse_wxr_xml(REAL_WXR_PATH.read_bytes())
    assert len(document.items) == 1483

    attachment_items = [item for item in document.items if item.post_type == "attachment"]
    assert len(attachment_items) == 953
    assert all(_is_kept_published(item) for item in attachment_items)

    junk_types = {
        "et_pb_layout", "custom_css", "itsec-dashboard", "itsec-dash-card",
        "wp_global_styles", "nav_menu_item",
    }
    assert not any(_is_kept_published(item) for item in document.items if item.post_type in junk_types)


# ---------------------------------------------------------------------------
# run_mysql_query: subprocess seam -- credentials never on the command line
#
# DbConfig/run_mysql_query are retained here only because wpfreeze.dbsetup
# still imports them; both they and this test section are deleted together
# with dbsetup.py (see the WXR-pivot plan's Order-of-implementation step 5).
# ---------------------------------------------------------------------------


def test_run_mysql_query_never_puts_credentials_on_command_line(monkeypatch):
    captured = {}

    def fake_run(args, input, capture_output, text, check):
        captured["args"] = args
        captured["input"] = input
        # The defaults file must exist and be read while the fake command runs.
        defaults_arg = next(a for a in args if a.startswith("--defaults-extra-file="))
        defaults_path = Path(defaults_arg.split("=", 1)[1])
        captured["defaults_file_contents"] = defaults_path.read_text()

        class Result:
            stdout = "ID\tpost_type\n1\tpost\n"

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    db_config = DbConfig(host="localhost", name="wpdb", user="wpuser", password="hunter2")
    output = run_mysql_query(db_config, "SELECT 1;")

    assert output == "ID\tpost_type\n1\tpost\n"
    assert not any("hunter2" in a for a in captured["args"])
    assert "password=hunter2" in captured["defaults_file_contents"]
    assert "user=wpuser" in captured["defaults_file_contents"]
    assert captured["input"] == "SELECT 1;"


def test_run_mysql_query_cleans_up_temp_defaults_file(monkeypatch):
    seen_path = {}

    def fake_run(args, input, capture_output, text, check):
        defaults_arg = next(a for a in args if a.startswith("--defaults-extra-file="))
        seen_path["path"] = Path(defaults_arg.split("=", 1)[1])
        assert seen_path["path"].exists()

        class Result:
            stdout = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    run_mysql_query(DbConfig(name="wpdb", user="u"), "SELECT 1;")
    assert not seen_path["path"].exists()
