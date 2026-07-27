from __future__ import annotations

import pytest

from wpfreeze.urlnorm import SiteProfile, normalize_url, resolve_url

# A profile for a site that: serves https, redirects www -> non-www
# (canonical_host = "example.com"), and prefers a trailing slash on
# directory-like paths.
SLASH_PROFILE = SiteProfile(
    canonical_host="example.com",
    site_hosts=frozenset({"example.com", "www.example.com"}),
    use_https=True,
    trailing_slash=True,
)

# A profile for a site that prefers *no* trailing slash on directory-like
# paths, and does not serve https (so http stays http for its own host).
NO_SLASH_HTTP_PROFILE = SiteProfile(
    canonical_host="example.org",
    site_hosts=frozenset({"example.org"}),
    use_https=False,
    trailing_slash=False,
)


@pytest.mark.parametrize(
    "url, expected",
    [
        # Scheme/host lowercasing (path case preserved; directory-like
        # path gains this profile's preferred trailing slash).
        ("HTTPS://EXAMPLE.COM/Foo", "https://example.com/Foo/"),
        ("https://Example.Com/", "https://example.com/"),
        # http -> https upgrade for the site's own host.
        ("http://example.com/", "https://example.com/"),
        ("http://www.example.com/", "https://example.com/"),
        # Fragment always stripped.
        ("https://example.com/page/#section", "https://example.com/page/"),
        ("https://example.com/page#top", "https://example.com/page/"),
        # www/non-www folded to canonical host.
        ("https://www.example.com/about/", "https://example.com/about/"),
        # Dot-segment resolution.
        ("https://example.com/a/../b/", "https://example.com/b/"),
        ("https://example.com/a/./b/", "https://example.com/a/b/"),
        ("https://example.com/a/b/../../c/", "https://example.com/c/"),
        # Excess ".." beyond root collapses to root, not an error.
        ("https://example.com/../../", "https://example.com/"),
        # Duplicate slash collapse.
        ("https://example.com//a//b/", "https://example.com/a/b/"),
        ("https://example.com///", "https://example.com/"),
        # Root path stays root.
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        # Percent-decode unreserved characters only.
        ("https://example.com/%7Efoo/", "https://example.com/~foo/"),
        ("https://example.com/foo%2Fbar/", "https://example.com/foo%2Fbar/"),
        ("https://example.com/foo%2fbar/", "https://example.com/foo%2Fbar/"),
        # Trailing slash added to directory-like paths.
        ("https://example.com/foo", "https://example.com/foo/"),
        ("https://example.com/foo/bar", "https://example.com/foo/bar/"),
        # File-like paths (dot in final segment) are left without a
        # trailing slash even though the site prefers one.
        ("https://example.com/sitemap.xml", "https://example.com/sitemap.xml"),
        ("https://example.com/style.css/", "https://example.com/style.css/"),
        # Default ports stripped, non-default kept.
        ("https://example.com:443/foo/", "https://example.com/foo/"),
        ("http://example.com:80/foo/", "https://example.com/foo/"),
        ("https://example.com:8443/foo/", "https://example.com:8443/foo/"),
    ],
)
def test_normalize_url_slash_profile(url: str, expected: str):
    assert normalize_url(url, SLASH_PROFILE) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        # This site doesn't serve https -- don't upgrade its own host --
        # and this profile strips the trailing slash from directory-like
        # paths.
        ("http://example.org/foo/", "http://example.org/foo"),
        ("http://example.org/foo", "http://example.org/foo"),
        # Root is never stripped down to nothing.
        ("http://example.org/", "http://example.org/"),
    ],
)
def test_normalize_url_no_slash_http_profile(url: str, expected: str):
    assert normalize_url(url, NO_SLASH_HTTP_PROFILE) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        # p=, page_id=, attachment_id= kept by default (pretty permalink
        # not yet known).
        ("https://example.com/?p=123", "https://example.com/?p=123"),
        ("https://example.com/?page_id=5", "https://example.com/?page_id=5"),
        ("https://example.com/?attachment_id=9", "https://example.com/?attachment_id=9"),
        # Everything else stripped.
        ("https://example.com/?replytocom=5", "https://example.com/"),
        ("https://example.com/?s=hello", "https://example.com/"),
        # Mixed query: keep only the permalink keys, drop the rest.
        ("https://example.com/?p=123&replytocom=5", "https://example.com/?p=123"),
    ],
)
def test_normalize_url_keeps_permalink_query_by_default(url: str, expected: str):
    assert normalize_url(url, SLASH_PROFILE) == expected


def test_normalize_url_strips_permalink_query_once_pretty_form_known():
    result = normalize_url(
        "https://example.com/?p=123", SLASH_PROFILE, pretty_permalink_known=True
    )
    assert result == "https://example.com/"


def test_normalize_url_does_not_upgrade_or_fold_external_host():
    # example.net is not in site_hosts: no https upgrade, no folding,
    # no trailing-slash preference applied.
    result = normalize_url("http://example.net/foo", SLASH_PROFILE)
    assert result == "http://example.net/foo"


def test_normalize_url_lowercases_external_host_only():
    result = normalize_url("HTTP://CDN.EXAMPLE.NET/Foo.JPG", SLASH_PROFILE)
    assert result == "http://cdn.example.net/Foo.JPG"


@pytest.mark.parametrize(
    "base, link, expected",
    [
        ("https://example.com/dir/page/", "../other/", "https://example.com/dir/other/"),
        ("https://example.com/dir/page.html", "sibling.html", "https://example.com/dir/sibling.html"),
        ("https://example.com/dir/page/", "/absolute", "https://example.com/absolute"),
        ("https://example.com/dir/page/", "https://elsewhere.com/x", "https://elsewhere.com/x"),
        ("https://example.com/dir/page/", "//cdn.example.com/a.js", "https://cdn.example.com/a.js"),
    ],
)
def test_resolve_url(base: str, link: str, expected: str):
    assert resolve_url(base, link) == expected


def test_resolve_url_returns_none_for_malformed_bracketed_link():
    """A regex-matched, URL-shaped string pulled out of arbitrary <script>
    text can be garbage that merely looks protocol-relative -- e.g. JS
    array-index syntax like `//foo[0]/bar` -- which urlsplit rejects as an
    invalid IPv6 host. This must degrade to None, not raise."""
    assert resolve_url("https://example.com/", "//foo[0]/bar") is None


def test_idempotent_on_already_normalized_urls():
    """Normalizing an already-normalized URL must be a no-op (fixpoint
    property the crawl loop depends on for alias detection)."""
    already = "https://example.com/foo/bar/"
    assert normalize_url(already, SLASH_PROFILE) == already


# --- in_scope ---------------------------------------------------------------

MULTISITE_SLASH = SiteProfile(
    canonical_host="example.com",
    site_hosts=frozenset({"example.com", "www.example.com"}),
    use_https=True,
    trailing_slash=True,
    base_path="/courses/",
)

# The combination that used to lose the subsite's front page. It is not
# exotic: _probe_trailing_slash only ever probes (and so only ever returns
# False) on a subdirectory install, which is the only case where base_path
# is not "/" -- the two conditions always arrive together.
MULTISITE_NO_SLASH = SiteProfile(
    canonical_host="example.com",
    site_hosts=frozenset({"example.com", "www.example.com"}),
    use_https=True,
    trailing_slash=False,
    base_path="/courses/",
)


@pytest.mark.parametrize("profile", [MULTISITE_SLASH, MULTISITE_NO_SLASH])
def test_in_scope_accepts_the_subsite_root_with_or_without_its_trailing_slash(profile):
    assert profile.in_scope("https://example.com/courses/")
    assert profile.in_scope("https://example.com/courses")


@pytest.mark.parametrize("profile", [MULTISITE_SLASH, MULTISITE_NO_SLASH])
def test_in_scope_still_confines_to_the_subsite(profile):
    assert profile.in_scope("https://example.com/courses/about/")
    assert not profile.in_scope("https://example.com/siblinglab/")
    assert not profile.in_scope("https://example.com/coursesX/")  # not a path-segment prefix
    assert not profile.in_scope("https://elsewhere.example.org/courses/")


def test_in_scope_round_trips_a_normalized_subsite_root_under_either_slash_preference():
    """normalize_url strips the trailing slash when the site prefers none,
    which is precisely what used to fall out of scope."""
    for profile in (MULTISITE_SLASH, MULTISITE_NO_SLASH):
        normalized = normalize_url("https://example.com/courses/", profile)
        assert profile.in_scope(normalized), normalized


def test_in_scope_degrades_to_owns_host_at_a_domain_root():
    assert SLASH_PROFILE.base_path == "/"
    assert SLASH_PROFILE.in_scope("https://example.com/")
    assert SLASH_PROFILE.in_scope("https://example.com/anything/deep/")
    assert SLASH_PROFILE.in_scope("https://example.com")  # bare origin, empty path
    assert not SLASH_PROFILE.in_scope("https://elsewhere.example.org/")


MULTISITE_WITH_CDN = SiteProfile(
    canonical_host="example.com",
    site_hosts=frozenset({"example.com", "www.example.com", "cdn.example.net"}),
    base_path="/courses/",
    extra_hosts=frozenset({"cdn.example.net"}),
)


def test_configured_extra_hosts_are_not_confined_by_base_path():
    """base_path exists only because sibling subsites share the *primary*
    hostname. A CDN serves this site's uploads from its own root, so the
    prefix means nothing there -- and an ordinary single-site run already
    treats extra hosts this way, since base_path "/" makes the test
    vacuous. Applying it on multisite made one config line mean two
    different things depending on install shape."""
    assert MULTISITE_WITH_CDN.in_scope("https://cdn.example.net/assets/app.js")
    assert MULTISITE_WITH_CDN.in_scope("https://cdn.example.net/wp-content/uploads/x.jpg")


def test_the_primary_host_is_still_confined_when_extra_hosts_exist():
    assert MULTISITE_WITH_CDN.in_scope("https://example.com/courses/about/")
    assert not MULTISITE_WITH_CDN.in_scope("https://example.com/siblinglab/")
    assert not MULTISITE_WITH_CDN.in_scope("https://cdn.unrelated.net/a.js")
