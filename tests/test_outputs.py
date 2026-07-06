from __future__ import annotations

import pytest

from wpfreeze.manifest import FLAG_ATTACHMENT_PAGE, Manifest, ManifestRecord, Status
from wpfreeze.outputs import (
    OutputPathCollisionError,
    compute_output_paths,
    external_asset_bucket,
    external_output_path,
    generate_redirects_htaccess,
    internal_output_path,
)
from wpfreeze.urlnorm import SiteProfile

PROFILE = SiteProfile(
    canonical_host="example.com",
    site_hosts=frozenset({"example.com"}),
    use_https=True,
    trailing_slash=True,
)


def _fetched(url, **kwargs):
    return ManifestRecord(url=url, status=Status.FETCHED.value, **kwargs)


@pytest.mark.parametrize(
    "url_path, expected",
    [
        ("/", "/index.html"),
        ("", "/index.html"),
        ("/foo/bar/", "/foo/bar.html"),
        ("/foo/", "/foo.html"),
        ("/category/essays/page/2/", "/category/essays/page-2.html"),
        ("/category/essays/page/2", "/category/essays/page-2.html"),
        ("/sitemap.xml", "/sitemap.xml"),
        ("/style.css", "/style.css"),
        ("/wp-content/uploads/2024/photo.jpg", "/wp-content/uploads/2024/photo.jpg"),
    ],
)
def test_internal_output_path(url_path, expected):
    assert internal_output_path(url_path) == expected


def test_internal_output_path_is_trailing_slash_independent():
    """The output scheme must not depend on the source site's own
    trailing-slash preference -- a no-trailing-slash site's page path
    still becomes foo.html, not foo verbatim."""
    assert internal_output_path("/foo/bar") == internal_output_path("/foo/bar/") == "/foo/bar.html"


@pytest.mark.parametrize(
    "filename, expected_bucket",
    [
        ("font.woff2", "/assets/fonts/"),
        ("font.woff", "/assets/fonts/"),
        ("style.css", "/assets/css/external/"),
        ("script.js", "/assets/js/external/"),
        ("photo.jpg", "/assets/img/external/"),
        ("photo.PNG", "/assets/img/external/"),
        ("document.pdf", None),
    ],
)
def test_external_asset_bucket(filename, expected_bucket):
    assert external_asset_bucket(filename) == expected_bucket


def test_external_output_path_buckets_by_type():
    assert external_output_path("https://cdn.example.net/font.woff2", "abc123") == "/assets/fonts/font.woff2"


def test_external_output_path_catchall_uses_host():
    assert external_output_path("https://cdn.example.net/thing.pdf", "abc123") == (
        "/assets/external/cdn.example.net/thing.pdf"
    )


def test_external_output_path_disambiguates_with_hash_prefix():
    path = external_output_path("https://cdn.example.net/logo.png", "deadbeef12345678", disambiguate=True)
    assert path == "/assets/img/external/logo-deadbeef.png"


def test_compute_output_paths_internal_pages():
    manifest = Manifest()
    manifest.upsert(_fetched("https://example.com/"))
    manifest.upsert(_fetched("https://example.com/about/"))
    compute_output_paths(manifest, PROFILE, {})
    assert manifest.get("https://example.com/").output_path == "/index.html"
    assert manifest.get("https://example.com/about/").output_path == "/about.html"


def test_compute_output_paths_attachment_page_gets_none():
    manifest = Manifest()
    record = _fetched("https://example.com/photo-1/")
    record.add_flag(FLAG_ATTACHMENT_PAGE)
    manifest.upsert(record)
    compute_output_paths(manifest, PROFILE, {})
    assert record.output_path is None


def test_compute_output_paths_external_asset():
    manifest = Manifest()
    record = _fetched("https://cdn.example.net/logo.png", content_hash="abc123")
    manifest.upsert(record)
    compute_output_paths(manifest, PROFILE, {})
    assert record.output_path == "/assets/img/external/logo.png"


def test_compute_output_paths_hash_duplicate_shares_canonical_path():
    manifest = Manifest()
    canonical = _fetched("https://example.com/wp-content/uploads/photo.jpg")
    dup = _fetched("https://example.com/wp-content/uploads/photo-150x150.jpg")
    manifest.upsert(canonical)
    manifest.upsert(dup)
    compute_output_paths(manifest, PROFILE, {dup.url: canonical.url})
    assert dup.output_path == canonical.output_path == "/wp-content/uploads/photo.jpg"


def test_compute_output_paths_raises_on_internal_collision():
    manifest = Manifest()
    # Contrived: two distinct URLs whose paths both reduce to the same
    # output_path (would require two different original paths mapping to
    # one -- e.g. "/foo/" and a differently-discovered alias-less "/foo"
    # entry that somehow both ended up as separate top-level records).
    a = _fetched("https://example.com/foo/")
    b = ManifestRecord(url="https://example.com/foo", status=Status.FETCHED.value)
    manifest.upsert(a)
    manifest.upsert(b)
    with pytest.raises(OutputPathCollisionError):
        compute_output_paths(manifest, PROFILE, {})


def test_generate_redirects_htaccess_includes_mechanical_and_explicit_rules():
    manifest = Manifest()
    record = _fetched("https://example.com/about/")
    record.output_path = "/about.html"
    record.add_alias("https://example.com/about-us/")
    record.add_redirect_from("https://example.com/?p=5")
    manifest.upsert(record)

    htaccess = generate_redirects_htaccess(manifest)
    assert "RewriteEngine On" in htaccess
    assert "Redirect 301 /about-us/ /about.html" in htaccess
    assert "Redirect 301 /?p=5 /about.html" in htaccess


def test_generate_redirects_htaccess_skips_canonical_self_reference():
    manifest = Manifest()
    record = _fetched("https://example.com/about/")
    record.output_path = "/about.html"
    manifest.upsert(record)
    htaccess = generate_redirects_htaccess(manifest)
    assert "Redirect 301 /about/ /about.html" not in htaccess
