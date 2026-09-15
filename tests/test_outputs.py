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


@pytest.mark.parametrize(
    "url_path, expected",
    [
        ("/reseaufranco/", "/index.html"),
        ("/reseaufranco", "/index.html"),
        ("/reseaufranco/programmation/", "/reseaufranco/programmation.html"),
        ("/reseaufranco/2021/11/15/salon/", "/reseaufranco/2021/11/15/salon.html"),
        ("/reseaufranco/tag/cinema/page/2/", "/reseaufranco/tag/cinema/page-2.html"),
        ("/reseaufranco/wp-content/uploads/photo.jpg", "/reseaufranco/wp-content/uploads/photo.jpg"),
        # A sibling site on the same multisite network is out of scope and
        # never reaches here, but its path must not be mistaken for the root.
        ("/reseaufrancophonie/", "/reseaufrancophonie.html"),
    ],
)
def test_internal_output_path_roots_a_subdirectory_install_at_index(url_path, expected):
    """A multisite subdirectory install's own root becomes index.html, not
    a lone subsite.html beside the directory holding every other page --
    otherwise the built site has no index.html at all."""
    assert internal_output_path(url_path, "/reseaufranco/") == expected


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


def test_external_output_path_extensionless_url_uses_content_type():
    """https://fonts.googleapis.com/css?family=... has no extension in its
    path -- without a fallback it lands in the host catch-all as a bare
    "css" file, which a webserver serves as text/plain and browsers then
    refuse to load as a stylesheet."""
    path = external_output_path(
        "https://fonts.googleapis.com/css?family=Open+Sans",
        "abc123",
        content_type="text/css; charset=utf-8",
    )
    assert path == "/assets/css/external/css.css"


def test_external_output_path_extensionless_url_without_content_type_falls_back_to_catchall():
    path = external_output_path("https://fonts.googleapis.com/css?family=Open+Sans", "abc123")
    assert path == "/assets/external/fonts.googleapis.com/css"


def test_compute_output_paths_internal_pages():
    manifest = Manifest()
    manifest.upsert(_fetched("https://example.com/"))
    manifest.upsert(_fetched("https://example.com/about/"))
    compute_output_paths(manifest, PROFILE, {})
    assert manifest.get("https://example.com/").output_path == "/index.html"
    assert manifest.get("https://example.com/about/").output_path == "/about.html"


def test_compute_output_paths_subdirectory_install_has_an_index():
    """The whole point: a subdirectory install's built site must have an
    index.html at its root. Before this, the home page landed on
    /reseaufranco.html -- a lone file beside the /reseaufranco/ directory
    holding every other page -- and the archive root was empty."""
    profile = SiteProfile(
        canonical_host="onlineacademiccommunity.uvic.ca",
        site_hosts=frozenset({"onlineacademiccommunity.uvic.ca"}),
        base_path="/reseaufranco/",
    )
    base = "https://onlineacademiccommunity.uvic.ca/reseaufranco"
    manifest = Manifest()
    manifest.site_profile = profile
    manifest.upsert(_fetched(f"{base}/"))
    manifest.upsert(_fetched(f"{base}/programmation/"))
    compute_output_paths(manifest, profile, {})

    assert manifest.get(f"{base}/").output_path == "/index.html"
    assert manifest.get(f"{base}/programmation/").output_path == "/reseaufranco/programmation.html"

    # ...and a redeploy at the original path still finds it: the generic
    # ^(.*)/$ rule would send /reseaufranco/ to the now-nonexistent
    # /reseaufranco.html, so the special case must precede it.
    htaccess = generate_redirects_htaccess(manifest)
    assert "RewriteRule ^reseaufranco/?$ /index.html [L]" in htaccess
    assert htaccess.index("^reseaufranco/?$") < htaccess.index(r"^(.*)/$")


def test_generate_redirects_htaccess_omits_the_root_rule_for_a_site_at_the_domain_root():
    """A site already at "/" needs no special case -- the webserver's own
    DirectoryIndex finds index.html -- and emitting one would rewrite
    every request on the host."""
    manifest = Manifest()
    manifest.site_profile = PROFILE
    manifest.upsert(_fetched("https://example.com/"))
    compute_output_paths(manifest, PROFILE, {})
    assert "/index.html [L]" not in generate_redirects_htaccess(manifest)


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


def test_generate_redirects_htaccess_escapes_raw_whitespace_in_a_malformed_alias():
    """Regression, seen in the wild on landscapesofinjustice.com: a
    hand-typed link on the original site (`/audrey kobayashi/`, alongside
    a properly-escaped `/audrey%20kobayashi/` alias for the same page) put
    a literal space into an alias URL. Written verbatim, that turns one
    `Redirect 301 <src> <dst>` line into five whitespace-separated tokens
    -- Apache's mod_alias rejects the extra argument as a config syntax
    error, which 500s the *entire* directory the .htaccess lives in, not
    just that one redirect."""
    manifest = Manifest()
    record = _fetched("https://example.com/audrey-kobayashi/")
    record.output_path = "/audrey-kobayashi.html"
    record.add_alias("https://example.com/audrey kobayashi/")
    manifest.upsert(record)

    htaccess = generate_redirects_htaccess(manifest)
    for line in htaccess.splitlines():
        if line.startswith("Redirect"):
            assert len(line.split(" ")) == 4, line
    assert "Redirect 301 /audrey%20kobayashi/ /audrey-kobayashi.html" in htaccess
