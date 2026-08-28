from __future__ import annotations

from pathlib import Path

import pytest

from wpfreeze.inventory import (
    WxrDocument,
    WxrItem,
    discover_wxr,
    extract_links_from_rest_items,
    parse_rest_total_pages,
    parse_robots_sitemaps,
    parse_sitemap_xml,
    parse_wxr_xml,
    register_shortlink_aliases,
    rest_collection_url,
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


def test_register_shortlink_aliases_links_id_to_the_pretty_permalink_record():
    """WordPress stamps <link rel="shortlink" href=".../?p=<id>"> on every
    post/page/attachment, but nothing else ever fetches that ugly spelling
    as its own crawl target on a REST-only-inventoried site (no WXR) --
    extract.py deliberately never follows shortlink <link> tags. Without
    this, a build-time reference to that exact spelling stays unresolved
    even though the content was captured fine under its pretty permalink."""
    from wpfreeze.urlnorm import SiteProfile

    profile = SiteProfile(
        canonical_host="example.com",
        site_hosts=frozenset({"example.com", "www.example.com"}),
    )
    manifest = Manifest()
    manifest.get_or_create("https://example.com/rockets/")

    items = [
        {"id": 1614, "link": "https://example.com/rockets/"},
        {"id": 9999, "link": "https://elsewhere.example.org/not-ours/"},  # out of scope
        {"link": "https://example.com/no-id/"},  # missing id, skipped
        {"id": 42},  # missing link, skipped
    ]
    register_shortlink_aliases(manifest, items, "https://example.com/", profile)

    record = manifest.get_or_create("https://example.com/rockets/")
    assert "https://example.com/?p=1614" in record.aliases
    assert not any("9999" in a for a in record.aliases)


def test_register_shortlink_aliases_resolves_p_link_at_build_time():
    """End-to-end: once the alias is registered, build.py's lookup table
    picks it up and a ?p=<id> reference resolves to the same local file as
    the pretty permalink, instead of staying unresolved."""
    from wpfreeze.build import build_lookup
    from wpfreeze.urlnorm import SiteProfile

    profile = SiteProfile(
        canonical_host="example.com",
        site_hosts=frozenset({"example.com", "www.example.com"}),
    )
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/rockets/")
    record.status = "fetched"
    record.output_path = "/rockets.html"

    register_shortlink_aliases(
        manifest,
        [{"id": 1614, "link": "https://example.com/rockets/"}],
        "https://example.com/",
        profile,
    )

    lookup = build_lookup(manifest)
    assert lookup["https://example.com/?p=1614"] == "/rockets.html"


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
# Any real WXR export dropped in the repo root (they are gitignored) is
# picked up. Discovered by glob rather than named: which site the export
# came from is nobody else's business, and a contributor with a different
# export should get the same sanity check rather than a skip.
_REAL_WXR_CANDIDATES = sorted(FIXTURES_DIR.parent.parent.glob("*.WordPress.*.xml"))
REAL_WXR_PATH = _REAL_WXR_CANDIDATES[0] if _REAL_WXR_CANDIDATES else None


@pytest.mark.skipif(REAL_WXR_PATH is None, reason="no real-world WXR sample present locally")
def test_parse_wxr_xml_handles_real_world_export():
    from wpfreeze.inventory import _is_kept_published

    document = parse_wxr_xml(REAL_WXR_PATH.read_bytes())
    # Structural, not exact counts: a re-export of the same site -- let alone
    # somebody else's export -- produces different totals. The hand-built
    # fixture above is the permanent regression suite; this only asserts that
    # real-world data parses and classifies sanely at scale.
    assert len(document.items) > 100

    attachment_items = [item for item in document.items if item.post_type == "attachment"]
    assert attachment_items, "a real export should contain attachments"
    assert all(_is_kept_published(item) for item in attachment_items)

    junk_types = {
        "et_pb_layout", "custom_css", "itsec-dashboard", "itsec-dash-card",
        "wp_global_styles", "nav_menu_item",
    }
    assert not any(_is_kept_published(item) for item in document.items if item.post_type in junk_types)


# ---------------------------------------------------------------------------
# discover_sitemaps: which locations get probed, and how misses are reported
# ---------------------------------------------------------------------------


SITEMAP_INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/page-sitemap.xml</loc></sitemap>
</sitemapindex>"""

PAGE_SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/about/</loc></url>
  <url><loc>https://example.com/contact/</loc></url>
</urlset>"""


class _FakeFetcher:
    """Stands in for fetch_with_retries; serves a fixed URL -> body map and
    records every URL asked for, so a test can assert on probe locations."""

    def __init__(self, responses: dict[str, bytes], redirects: dict[str, str] | None = None):
        self.responses = responses
        self.redirects = redirects or {}
        self.requested: list[str] = []

    def __call__(self, url, session, rate_limiter, fetch_config):
        from wpfreeze.fetch import SUCCESS, FetchOutcome, FetchResult

        self.requested.append(url)
        final_url = self.redirects.get(url, url)
        if final_url not in self.responses:
            return FetchOutcome(category="wayback_candidate", http_status=404, attempts=1)
        body = self.responses[final_url]
        return FetchOutcome(
            category=SUCCESS,
            http_status=200,
            attempts=1,
            result=FetchResult(
                status_code=200, content=body, headers={},
                content_type="application/xml", final_url=final_url,
            ),
        )


def _profile_for(base_url):
    from urllib.parse import urlsplit

    from wpfreeze.urlnorm import SiteProfile

    host = urlsplit(base_url).hostname
    path = urlsplit(base_url).path or "/"
    if not path.endswith("/"):
        path += "/"
    return SiteProfile(
        canonical_host=host,
        site_hosts=frozenset({host, "www." + host}),
        use_https=True,
        trailing_slash=True,
        base_path=path,
    )


def _discover(monkeypatch, responses, base_url="https://example.com/", exclusions=(), redirects=None):
    from wpfreeze import inventory

    fetcher = _FakeFetcher(responses, redirects)
    monkeypatch.setattr(inventory, "fetch_with_retries", fetcher)
    manifest = Manifest()
    ok = inventory.discover_sitemaps(
        manifest, base_url, _profile_for(base_url), list(exclusions), None, None, None
    )
    return ok, manifest, fetcher


def test_discover_sitemaps_probes_all_conventional_locations(monkeypatch):
    _, _, fetcher = _discover(monkeypatch, {})
    assert "https://example.com/wp-sitemap.xml" in fetcher.requested      # WordPress core
    assert "https://example.com/sitemap_index.xml" in fetcher.requested   # Yoast
    assert "https://example.com/sitemap.xml" in fetcher.requested         # AIOSEO / Jetpack / generic


def test_discover_sitemaps_finds_generic_sitemap_xml(monkeypatch):
    """A site (All in One SEO, Jetpack) that serves only sitemap.xml, with
    no core or Yoast name and nothing in robots.txt, is still found."""
    ok, manifest, _ = _discover(monkeypatch, {
        "https://example.com/sitemap.xml": PAGE_SITEMAP_XML,
    })
    assert ok is True
    assert "https://example.com/about/" in manifest


def test_discover_sitemaps_dedups_a_name_that_redirects_to_another_probed_name(monkeypatch):
    """sitemap.xml 301-ing to sitemap_index.xml must not fetch or parse the
    index twice -- the redirect target is also on the probe list."""
    _, manifest, fetcher = _discover(
        monkeypatch,
        {
            "https://example.com/sitemap_index.xml": SITEMAP_INDEX_XML,
            "https://example.com/page-sitemap.xml": PAGE_SITEMAP_XML,
        },
        redirects={"https://example.com/sitemap.xml": "https://example.com/sitemap_index.xml"},
    )
    # The index's own child sitemap is fetched exactly once, not once per
    # name that reaches the index.
    assert fetcher.requested.count("https://example.com/page-sitemap.xml") == 1
    assert "https://example.com/about/" in manifest


def test_discover_sitemaps_finds_yoast_index_when_core_name_is_absent(monkeypatch):
    ok, manifest, _ = _discover(monkeypatch, {
        "https://example.com/sitemap_index.xml": SITEMAP_INDEX_XML,
        "https://example.com/page-sitemap.xml": PAGE_SITEMAP_XML,
    })
    assert ok is True
    assert "https://example.com/about/" in manifest
    assert "https://example.com/contact/" in manifest


def test_discover_sitemaps_reports_success_at_info(monkeypatch, caplog):
    with caplog.at_level("INFO", logger="wpfreeze.inventory"):
        _discover(monkeypatch, {
            "https://example.com/sitemap_index.xml": SITEMAP_INDEX_XML,
            "https://example.com/page-sitemap.xml": PAGE_SITEMAP_XML,
        })
    messages = [r.getMessage() for r in caplog.records]
    assert any("sitemap https://example.com/page-sitemap.xml: 2 URL(s)" in m for m in messages)


def test_discover_sitemaps_keeps_conventional_misses_off_the_console(monkeypatch, caplog):
    """A 404 on a guessed location is the expected case for every site that
    uses the other name -- it belongs in the log file, not the console."""
    with caplog.at_level("INFO", logger="wpfreeze.inventory"):
        _discover(monkeypatch, {
            "https://example.com/sitemap_index.xml": SITEMAP_INDEX_XML,
            "https://example.com/page-sitemap.xml": PAGE_SITEMAP_XML,
        })
    console = [r.getMessage() for r in caplog.records if r.levelno >= 20]
    assert not any("wp-sitemap.xml" in m for m in console)


def test_discover_sitemaps_warns_when_an_advertised_sitemap_is_missing(monkeypatch, caplog):
    """robots.txt promising a sitemap that 404s is a real defect, unlike a
    missed guess -- it must not be filed under the same quiet message."""
    robots = b"Sitemap: https://example.com/gone-sitemap.xml\n"
    with caplog.at_level("DEBUG", logger="wpfreeze.inventory"):
        _discover(monkeypatch, {"https://example.com/robots.txt": robots})
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= 30]
    assert any("gone-sitemap.xml" in m and "robots.txt" in m for m in warnings)


def test_discover_sitemaps_warns_when_an_index_promises_a_missing_child(monkeypatch, caplog):
    with caplog.at_level("DEBUG", logger="wpfreeze.inventory"):
        _discover(monkeypatch, {
            "https://example.com/sitemap_index.xml": SITEMAP_INDEX_XML,
        })
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= 30]
    assert any("page-sitemap.xml" in m and "sitemap index" in m for m in warnings)


class _FakeProgress:
    """Records tick() calls -- stands in for wpfreeze.progress.Progress so a
    test can assert discovery actually reports movement, without a real
    terminal. See progress.py's own module docstring: there is no
    background render thread, so a blocking discovery call that never
    ticks looks completely frozen for its whole duration -- that's the
    real bug this coverage exists to catch a regression of."""

    def __init__(self):
        self.ticks: list[str] = []

    def tick(self, n: int = 1, detail: str = "") -> None:
        self.ticks.append(detail)


def test_discover_sitemaps_ticks_progress_once_per_sitemap_fetched(monkeypatch):
    from wpfreeze import inventory

    progress = _FakeProgress()
    fetcher = _FakeFetcher({"https://example.com/sitemap.xml": PAGE_SITEMAP_XML})
    monkeypatch.setattr(inventory, "fetch_with_retries", fetcher)
    inventory.discover_sitemaps(
        Manifest(), "https://example.com/", _profile_for("https://example.com/"), [], None, None, None,
        progress=progress,
    )
    # One tick per URL actually fetched (conventional-location probes plus
    # any nested sitemap) -- not zero, and not just one for the whole call.
    assert len(progress.ticks) == len(fetcher.requested)
    assert "https://example.com/sitemap.xml" in progress.ticks


def test_discover_sitemaps_with_no_progress_does_not_raise(monkeypatch):
    # progress=None (the default) must stay a no-op, same as every other
    # optional-progress call site in this codebase.
    ok, _, _ = _discover(monkeypatch, {})
    assert ok is False


def test_discover_rest_api_ticks_progress_once_per_page_fetched(monkeypatch):
    import json as _json

    from wpfreeze import inventory

    progress = _FakeProgress()
    pages_fetched: list[str] = []

    def _fake_fetch(url, session, rate_limiter, fetch_config):
        pages_fetched.append(url)
        body = _json.dumps([{"id": 1, "link": "https://example.com/hello/"}]).encode() if "/posts" in url else b"[]"
        return _FakeOutcome(body, url)

    monkeypatch.setattr(inventory, "fetch_with_retries", _fake_fetch)
    inventory.discover_rest_api(
        Manifest(), "https://example.com/", _rest_profile("https://example.com/"), [], None, None, None,
        progress=progress,
    )
    assert len(progress.ticks) == len(pages_fetched)
    assert any("/posts" in d for d in progress.ticks)


# ---------------------------------------------------------------------------
# Scope confinement at seed time (multisite subdirectory installs)
# ---------------------------------------------------------------------------


# A WordPress multisite network sitemap: every sibling site on the shared
# host, one site on a wholly different host, and the target subsite itself --
# listed WITHOUT a trailing slash, which is how the real thing does it.
NETWORK_SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/courses</loc></url>
  <url><loc>https://example.com/courses/about/</loc></url>
  <url><loc>https://example.com/siblinglab/</loc></url>
  <url><loc>https://example.com/otherlab/research/</loc></url>
  <url><loc>https://elsewhere.example.org/</loc></url>
</urlset>"""


def test_seeding_confines_to_base_path_on_a_multisite_subdirectory(monkeypatch):
    _, manifest, _ = _discover(
        monkeypatch,
        {"https://example.com/courses/sitemap_index.xml": NETWORK_SITEMAP_XML},
        base_url="https://example.com/courses/",
    )
    urls = {r.url for r in manifest.all()}
    assert urls == {
        "https://example.com/courses/",
        "https://example.com/courses/about/",
    }


def test_seeding_normalizes_before_the_prefix_test(monkeypatch):
    """A network sitemap lists a subsite's own homepage with no trailing
    slash. A textual prefix test against "/courses/" drops it unless the
    URL is normalized first -- losing the single most important page."""
    _, manifest, _ = _discover(
        monkeypatch,
        {"https://example.com/courses/sitemap_index.xml": NETWORK_SITEMAP_XML},
        base_url="https://example.com/courses/",
    )
    assert "https://example.com/courses/" in manifest


def test_seeding_at_a_domain_root_admits_the_whole_host(monkeypatch):
    """base_path "/" must leave ordinary single-site runs untouched."""
    _, manifest, _ = _discover(
        monkeypatch,
        {"https://example.com/sitemap_index.xml": NETWORK_SITEMAP_XML},
    )
    urls = {r.url for r in manifest.all()}
    assert "https://example.com/siblinglab/" in urls
    assert "https://example.com/otherlab/research/" in urls
    assert "https://elsewhere.example.org/" not in urls  # still host-scoped


def test_seeding_applies_exclusions_so_no_record_is_ever_created(monkeypatch):
    import re

    _, manifest, _ = _discover(
        monkeypatch,
        {"https://example.com/sitemap_index.xml": NETWORK_SITEMAP_XML},
        exclusions=[re.compile(r"/otherlab/")],
    )
    urls = {r.url for r in manifest.all()}
    assert "https://example.com/siblinglab/" in urls
    assert not any("otherlab" in u for u in urls)


class _FakeOutcome:
    """Minimal stand-in for fetch_with_retries' return value."""

    def __init__(self, body: bytes, url: str):
        from wpfreeze.fetch import SUCCESS

        self.category = SUCCESS
        self.result = type(
            "R", (), {"content": body, "headers": {}, "final_url": url, "status_code": 200}
        )()


def _rest_profile(base_url, trailing_slash=True):
    from urllib.parse import urlsplit

    from wpfreeze.urlnorm import SiteProfile

    host = urlsplit(base_url).hostname
    path = urlsplit(base_url).path or "/"
    if not path.endswith("/"):
        path += "/"
    return SiteProfile(
        canonical_host=host,
        site_hosts=frozenset({host, "www." + host}),
        use_https=True,
        trailing_slash=trailing_slash,
        base_path=path,
    )


def test_seeding_keeps_the_subsite_homepage_when_the_site_prefers_no_trailing_slash():
    """A network sitemap lists the subsite's own homepage without a
    trailing slash. Under trailing_slash=False, normalize_url strips the
    slash the base_path prefix test expects -- which used to drop the
    single most important URL in the capture."""
    from wpfreeze import inventory

    profile = _rest_profile("https://example.com/courses/", trailing_slash=False)
    manifest = Manifest()
    kept, rejected = inventory._seed(
        manifest,
        [
            "https://example.com/courses",
            "https://example.com/courses/about/",
            "https://example.com/siblinglab/",
        ],
        "sitemap",
        profile,
        [],
    )
    urls = {r.url for r in manifest.all()}
    assert any(u.rstrip("/").endswith("/courses") for u in urls), urls
    assert not any("siblinglab" in u for u in urls)
    assert (kept, rejected) == (2, 1)


def test_shortlink_aliases_never_create_a_record_seed_rejected():
    """register_shortlink_aliases runs over the same REST items _seed just
    filtered. Using get_or_create there resurrected anything _seed had
    dropped as excluded -- with empty provenance, so it was invisible to
    the inventory-source counts while still inflating the manifest."""
    import re

    from wpfreeze import inventory

    profile = _rest_profile("https://example.com/")
    items = [
        {"id": 11, "link": "https://example.com/public-post/"},
        {"id": 12, "link": "https://example.com/private/secret-page/"},
    ]
    exclusions = [re.compile(r"/private/")]

    manifest = Manifest()
    inventory._seed(
        manifest, inventory.extract_links_from_rest_items(items), "rest_api", profile, exclusions
    )
    inventory.register_shortlink_aliases(manifest, items, "https://example.com/", profile)

    urls = {r.url for r in manifest.all()}
    assert urls == {"https://example.com/public-post/"}
    assert not any("/private/" in u for u in urls)
    # the kept record still gets its shortlink alias
    kept = manifest.get("https://example.com/public-post/")
    assert "https://example.com/?p=11" in kept.aliases


def test_shortlink_aliases_are_confined_to_the_site_being_archived():
    """The in_scope guard here had no test at all: deleting it left the
    whole suite passing."""
    from wpfreeze import inventory

    profile = _rest_profile("https://example.com/courses/")
    items = [
        {"id": 21, "link": "https://example.com/courses/post/"},
        {"id": 22, "link": "https://example.com/siblinglab/post/"},
    ]
    manifest = Manifest()
    # Pre-seed both, so only the scope guard can distinguish them.
    for item in items:
        manifest.get_or_create(item["link"], discovered_via="test")
    inventory.register_shortlink_aliases(manifest, items, "https://example.com/courses/", profile)

    assert manifest.get("https://example.com/courses/post/").aliases == [
        "https://example.com/courses/?p=21"
    ]
    assert manifest.get("https://example.com/siblinglab/post/").aliases == []


def test_rest_discovery_honours_exclusions_end_to_end(monkeypatch):
    """Exclusions were only ever tested on the sitemap path, yet the REST
    path is the one that also runs register_shortlink_aliases."""
    import json as _json
    import re

    from wpfreeze import inventory

    items = [
        {"id": 1, "link": "https://example.com/public/"},
        {"id": 2, "link": "https://example.com/private/hidden/"},
    ]

    def _fake_fetch(url, session, rate_limiter, fetch_config):
        body = _json.dumps(items).encode() if "/posts" in url else b"[]"
        return _FakeOutcome(body, url)

    monkeypatch.setattr(inventory, "fetch_with_retries", _fake_fetch)

    manifest = Manifest()
    inventory.discover_rest_api(
        manifest,
        "https://example.com/",
        _rest_profile("https://example.com/"),
        [re.compile(r"/private/")],
        None,
        None,
        None,
    )

    urls = {r.url for r in manifest.all()}
    assert "https://example.com/public/" in urls
    assert not any("/private/" in u for u in urls), urls
