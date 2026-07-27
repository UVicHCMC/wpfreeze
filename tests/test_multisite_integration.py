"""End-to-end crawl against a locally-served WordPress multisite
subdirectory network.

These exercise, against a real server over a real socket, the three things
that unit tests could only assert in pieces:

- a sibling subsite sharing the hostname stays out of scope;
- the network-wide /wp-content/ theme -- which lives *outside* base_path --
  is still parsed, so the fonts and images its stylesheets reference are
  discovered;
- the subsite's own front page survives on an install that prefers no
  trailing slash.

The profile comes from the real `probe_site`, not a hand-built SiteProfile,
so base_path derivation and the trailing-slash probe are themselves under
test rather than assumed.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import requests

from wpfreeze.cli import probe_site
from wpfreeze.crawl import compile_exclusions, crawl_fixpoint
from wpfreeze.fetch import FetchConfig, RateLimiter
from wpfreeze.manifest import Manifest, Status

from fixture_site import MultisiteFixture

FAST_CONFIG = FetchConfig(max_attempts=2, timeout=2.0, backoff_base=0.01)


def _acquire(site: MultisiteFixture, raw_dir: Path, extra_hosts=()):
    """Probe, seed from base_url, and crawl to a fixpoint -- the real
    pipeline, minus inventory discovery and Wayback."""
    profile = probe_site(
        site.base_url, requests.Session(), "wpfreeze-test", list(extra_hosts), timeout=2.0
    )
    manifest = Manifest()
    manifest.get_or_create(site.base_url, discovered_via="base_url")
    crawl_fixpoint(
        manifest,
        profile,
        requests.Session(),
        RateLimiter(0.0),
        FAST_CONFIG,
        raw_dir,
        compile_exclusions([]),
    )
    return profile, manifest


def _paths(manifest: Manifest) -> set[str]:
    return {urlsplit(r.url).path for r in manifest.all()}


def _fetched_paths(manifest: Manifest) -> set[str]:
    return {
        urlsplit(r.url).path
        for r in manifest.all()
        if r.status in (Status.FETCHED.value, Status.FETCHED_WAYBACK.value)
    }


# ---------------------------------------------------------------------------
# Scope confinement
# ---------------------------------------------------------------------------


def test_probe_derives_the_subsite_base_path(tmp_path: Path):
    with MultisiteFixture() as site:
        profile, _ = _acquire(site, tmp_path / "raw")
        assert profile.base_path == "/courses/"
        assert profile.trailing_slash is True


def test_a_sibling_subsite_is_never_fetched(tmp_path: Path):
    """Same host, different site. It shares the hostname but is kept alive
    independently of this one's static replacement, so admitting it would
    both bloat the capture and misrepresent what was archived."""
    with MultisiteFixture() as site:
        _, manifest = _acquire(site, tmp_path / "raw")

        assert not any("/rocketry" in p for p in _paths(manifest)), _paths(manifest)
        # ...and the server was never even asked for it.
        assert not any("/rocketry" in p for p in site.site_request_log), site.site_request_log


def test_the_subsites_own_pages_are_fetched(tmp_path: Path):
    with MultisiteFixture() as site:
        _, manifest = _acquire(site, tmp_path / "raw")
        fetched = _fetched_paths(manifest)
        assert "/courses/" in fetched
        assert "/courses/about/" in fetched


# ---------------------------------------------------------------------------
# The network-wide theme: owned host, outside base_path
# ---------------------------------------------------------------------------


def test_shared_theme_css_is_parsed_so_its_assets_are_discovered(tmp_path: Path):
    """The regression this fixture exists for.

    On a subdirectory install the theme lives under the network-wide
    /wp-content/, outside base_path. Gating the CSS parse on in_scope
    fetched those stylesheets but never read them, so every font and
    background image they reference went undiscovered -- silently, and only
    on multisite. Nothing links to hero.jpg or font.woff2 from any page;
    they are reachable only by parsing CSS that is itself out of scope.
    """
    with MultisiteFixture() as site:
        _, manifest = _acquire(site, tmp_path / "raw")
        fetched = _fetched_paths(manifest)

        assert "/wp-content/themes/demo/style.css" in fetched
        # via @import, one level deeper and still outside base_path
        assert "/wp-content/themes/demo/print.css" in fetched
        # url() targets, reachable only through the two stylesheets above
        assert "/wp-content/uploads/hero.jpg" in fetched, sorted(fetched)
        assert "/wp-content/uploads/font.woff2" in fetched, sorted(fetched)


def test_shared_assets_are_stored_with_their_real_bytes(tmp_path: Path):
    with MultisiteFixture() as site:
        _, manifest = _acquire(site, tmp_path / "raw")
        record = next(
            r for r in manifest.all() if r.url.endswith("/wp-content/uploads/font.woff2")
        )
        assert record.local_path is not None
        # local_path is recorded relative to output_dir and already carries
        # the "raw/" segment.
        assert (tmp_path / record.local_path).read_bytes() == b"FONT-WOFF2-BYTES"


def test_sibling_html_is_not_used_as_a_crawl_root(tmp_path: Path):
    """The asymmetry that makes the CSS rule safe: HTML outside base_path
    stays a leaf, so /rocketry/members/ -- linked only from the sibling's
    own page -- is never reached."""
    with MultisiteFixture() as site:
        _, manifest = _acquire(site, tmp_path / "raw")
        assert not any("/rocketry/members" in p for p in _paths(manifest))


# ---------------------------------------------------------------------------
# Configured extra hosts
# ---------------------------------------------------------------------------


def test_a_configured_extra_host_is_fetched_and_not_path_confined(tmp_path: Path):
    """A CDN serves this site's assets from its own root, so base_path
    means nothing there -- it has no sibling subsite to be confused with."""
    with MultisiteFixture() as site:
        cdn_host = urlsplit(site.cdn_base).hostname
        profile, manifest = _acquire(site, tmp_path / "raw", extra_hosts=[cdn_host])

        assert profile.in_scope(f"{site.cdn_base}/assets/app.js")
        assert "/assets/app.js" in _fetched_paths(manifest)


# ---------------------------------------------------------------------------
# The no-trailing-slash install
# ---------------------------------------------------------------------------


def test_a_subsite_that_prefers_no_trailing_slash_keeps_its_front_page(tmp_path: Path):
    """base_path is always "/"-terminated, but normalize_url strips that
    slash when the site prefers none -- so "/courses" failed a textual
    prefix test against "/courses/" and the subsite's front page fell out
    of scope entirely. Both preconditions arrive together: trailing_slash is
    only ever probed on a subdirectory install.
    """
    with MultisiteFixture(trailing_slash=False) as site:
        profile, manifest = _acquire(site, tmp_path / "raw")

        assert profile.trailing_slash is False
        assert profile.base_path == "/courses/"

        fetched = _fetched_paths(manifest)
        assert "/courses" in fetched, sorted(fetched)
        # ...and the front page still contributed its own links.
        assert "/courses/about" in fetched, sorted(fetched)
        # ...while confinement still holds.
        assert not any("/rocketry" in p for p in _paths(manifest))


def test_no_trailing_slash_install_still_reaches_the_shared_theme(tmp_path: Path):
    """Both fixes have to hold at once: if the front page falls out of
    scope it is never parsed, so the theme it links is never found either.
    """
    with MultisiteFixture(trailing_slash=False) as site:
        _, manifest = _acquire(site, tmp_path / "raw")
        fetched = _fetched_paths(manifest)
        assert "/wp-content/uploads/hero.jpg" in fetched, sorted(fetched)
