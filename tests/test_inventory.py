from __future__ import annotations

from pathlib import Path

import pytest

from wpfreeze.inventory import (
    DbConfig,
    build_author_urls,
    build_inventory_queries,
    build_post_urls,
    build_term_urls,
    extract_links_from_rest_items,
    parse_batch_output,
    parse_rest_total_pages,
    parse_robots_sitemaps,
    parse_sitemap_xml,
    rest_collection_url,
    run_mysql_query,
)


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
# Database inventory: batch-output parsing and query-string URL building
# ---------------------------------------------------------------------------


def test_parse_batch_output_skips_header():
    raw = "ID\tpost_type\n1\tpost\n2\tpage\n3\tattachment\n"
    assert parse_batch_output(raw) == [["1", "post"], ["2", "page"], ["3", "attachment"]]


def test_parse_batch_output_empty():
    assert parse_batch_output("") == []
    assert parse_batch_output("ID\tpost_type\n") == []


def test_build_post_urls_distinguishes_attachments():
    rows = [["1", "post"], ["2", "page"], ["3", "attachment"], ["4", "custom_type"]]
    items = build_post_urls("https://example.com", rows)
    assert [i.url for i in items] == [
        "https://example.com/?p=1",
        "https://example.com/?p=2",
        "https://example.com/?attachment_id=3",
        "https://example.com/?p=4",
    ]
    assert all(i.discovered_via == "database" for i in items)


def test_build_term_urls_categories_tags_and_custom_taxonomy():
    rows = [
        ["5", "news", "category"],
        ["6", "opinion", "post_tag"],
        ["7", "team a", "custom_tax"],
    ]
    items = build_term_urls("https://example.com", rows)
    assert [i.url for i in items] == [
        "https://example.com/?cat=5",
        "https://example.com/?tag=opinion",
        "https://example.com/?taxonomy=custom_tax&term=team%20a",
    ]


def test_build_author_urls():
    items = build_author_urls("https://example.com/", [["9"], ["10"]])
    assert [i.url for i in items] == [
        "https://example.com/?author=9",
        "https://example.com/?author=10",
    ]


def test_build_inventory_queries_uses_table_prefix():
    queries = build_inventory_queries("wp_")
    assert "wp_posts" in queries["posts"]
    assert "wp_terms" in queries["terms"] and "wp_term_taxonomy" in queries["terms"]
    assert "wp_users" in queries["authors"] and "wp_posts" in queries["authors"]


def test_build_inventory_queries_custom_prefix():
    queries = build_inventory_queries("custom_")
    assert "custom_posts" in queries["posts"]


# ---------------------------------------------------------------------------
# run_mysql_query: subprocess seam -- credentials never on the command line
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
