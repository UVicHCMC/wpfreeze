from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import requests

from wpfreeze.crawl import compile_exclusions, crawl_fixpoint
from wpfreeze.fetch import FetchConfig, RateLimiter
from wpfreeze.manifest import (
    FLAG_AUTH_GATED,
    Manifest,
    Status,
)
from wpfreeze.urlnorm import SiteProfile

from fixture_site import FixtureSite

FAST_CONFIG = FetchConfig(max_attempts=2, timeout=2.0, backoff_base=0.01)


def _profile_for(site: FixtureSite) -> SiteProfile:
    return SiteProfile(
        canonical_host="127.0.0.1",
        site_hosts=frozenset({"127.0.0.1"}),
        use_https=False,
        trailing_slash=True,
    )


def _seed_and_crawl(site: FixtureSite, raw_dir: Path, manifest_save_path=None) -> Manifest:
    manifest = Manifest()
    manifest.get_or_create(site.site_base + "/", discovered_via="base_url")
    profile = _profile_for(site)
    crawl_fixpoint(
        manifest,
        profile,
        requests.Session(),
        RateLimiter(0.0),
        FAST_CONFIG,
        raw_dir,
        compile_exclusions([]),
        manifest_save_path=manifest_save_path,
    )
    return manifest


def test_crawl_discovers_and_fetches_everything(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    with FixtureSite() as site:
        manifest = _seed_and_crawl(site, raw_dir)

        by_path = {}
        for record in manifest.all():
            path = record.url.split("127.0.0.1")[-1].split(":")[-1]
            # crude but adequate: strip host:port, keep path
            from urllib.parse import urlsplit

            by_path[urlsplit(record.url).path or "/"] = record

        assert set(by_path) >= {
            "/",
            "/about/",
            "/blog/post-1/",
            "/gone/",
            "/secret/",
            "/style.css",
            "/print.css",
            "/wp-content/uploads/2024/bg.png",
            "/wp-content/uploads/2024/photo.jpg",
            "/img/placeholder.gif",
        }

        assert by_path["/"].status == Status.FETCHED.value
        assert by_path["/about/"].status == Status.FETCHED.value
        assert by_path["/blog/post-1/"].status == Status.FETCHED.value
        assert by_path["/style.css"].status == Status.FETCHED.value
        assert by_path["/print.css"].status == Status.FETCHED.value
        assert by_path["/wp-content/uploads/2024/bg.png"].status == Status.FETCHED.value
        assert by_path["/wp-content/uploads/2024/photo.jpg"].status == Status.FETCHED.value

        # 404 -> retrying (awaiting Wayback stage), no special flag.
        assert by_path["/gone/"].status == Status.RETRYING.value
        assert by_path["/gone/"].flags == []
        assert by_path["/gone/"].http_status == 404

        # 403 -> retrying, auth_gated flagged.
        assert by_path["/secret/"].status == Status.RETRYING.value
        assert FLAG_AUTH_GATED in by_path["/secret/"].flags
        assert by_path["/secret/"].http_status == 403

        # Redirect: /old-page/ must not survive as its own manifest entry;
        # it's folded into /about/ as an alias + redirect_from.
        assert "/old-page/" not in by_path
        about = by_path["/about/"]
        assert any("/old-page/" in u for u in about.redirect_from)
        assert any("/old-page/" in u for u in about.aliases)

        # External CDN asset: fetched, stored under a host-namespaced path.
        logo_record = next(r for r in manifest.all() if "logo.png" in r.url)
        assert logo_record.status == Status.FETCHED.value
        assert logo_record.local_path == "raw/_external/127.0.0.2/logo.png"
        assert (raw_dir / "_external" / "127.0.0.2" / "logo.png").read_bytes() == b"CDN-LOGO-BYTES"

        # Internal assets stored mirroring their path under raw/.
        assert by_path["/wp-content/uploads/2024/bg.png"].local_path == (
            "raw/wp-content/uploads/2024/bg.png"
        )
        bg_bytes = (raw_dir / "wp-content/uploads/2024/bg.png").read_bytes()
        assert bg_bytes == b"BG-PNG-BYTES"
        assert by_path["/wp-content/uploads/2024/bg.png"].content_hash == hashlib.sha256(
            bg_bytes
        ).hexdigest()

        # No double-fetch of independently-pending records. "/about/" is
        # excluded here: requests' allow_redirects=True makes its own
        # internal GET to the redirect target while resolving /old-page/,
        # on top of /about/'s own direct fetch as a separately-linked page
        # -- incidental HTTP overhead from auto-follow, not a manifest bug.
        log = site.site_request_log
        for path in ("/", "/blog/post-1/", "/style.css", "/print.css"):
            assert log.count(path) == 1, f"{path} fetched {log.count(path)} times: {log}"


def test_crawl_honours_exclusions(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    with FixtureSite() as site:
        manifest = Manifest()
        manifest.get_or_create(site.site_base + "/secret/", discovered_via="test")
        profile = _profile_for(site)
        crawl_fixpoint(
            manifest,
            profile,
            requests.Session(),
            RateLimiter(0.0),
            FAST_CONFIG,
            raw_dir,
            compile_exclusions([r"/secret/"]),
        )
        record = manifest.get(site.site_base + "/secret/")
        assert record.status == Status.EXCLUDED.value
        assert site.site_request_log == []  # never actually fetched


def test_crawl_is_resumable_after_interruption(tmp_path: Path, monkeypatch):
    raw_dir = tmp_path / "raw"
    manifest_path = tmp_path / "manifest.json"

    with FixtureSite() as site:
        manifest = Manifest()
        manifest.get_or_create(site.site_base + "/", discovered_via="base_url")
        profile = _profile_for(site)

        import wpfreeze.crawl as crawl_module

        real_fetch = crawl_module.fetch_with_retries
        call_count = {"n": 0}

        def flaky_fetch(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise RuntimeError("simulated crash mid-crawl")
            return real_fetch(*args, **kwargs)

        monkeypatch.setattr(crawl_module, "fetch_with_retries", flaky_fetch)

        with pytest.raises(RuntimeError):
            crawl_fixpoint(
                manifest,
                profile,
                requests.Session(),
                RateLimiter(0.0),
                FAST_CONFIG,
                raw_dir,
                compile_exclusions([]),
                manifest_save_path=manifest_path,
            )

        # Manifest on disk reflects everything processed before the crash.
        reloaded = Manifest.load(manifest_path)
        assert len(reloaded) >= 1
        fetched_before_crash = {r.url for r in reloaded.all() if r.status == Status.FETCHED.value}

        monkeypatch.setattr(crawl_module, "fetch_with_retries", real_fetch)
        crawl_fixpoint(
            reloaded,
            profile,
            requests.Session(),
            RateLimiter(0.0),
            FAST_CONFIG,
            raw_dir,
            compile_exclusions([]),
            manifest_save_path=manifest_path,
        )

        # Everything reaches a terminal state; nothing already-fetched
        # before the crash was fetched twice.
        assert all(r.status != Status.PENDING.value for r in reloaded.all())
        log = site.site_request_log
        for url in fetched_before_crash:
            from urllib.parse import urlsplit

            path = urlsplit(url).path
            assert log.count(path) == 1


# ---------------------------------------------------------------------------
# store_bytes: a URL's path can be a strict prefix of another URL's path
# (e.g. .../v1.2.3 as a page, .../v1.2.3/LICENSE as a file beneath it) --
# this crashed a real run against a page linking to a GitHub blob URL.
# ---------------------------------------------------------------------------


def test_external_page_is_fetched_but_not_recursed_into(tmp_path: Path, monkeypatch):
    """The real amplification bug: once ANY external HTML page enters the
    manifest (however it got in), the crawler used to parse its content for
    further links exactly like an internal page -- with RENDER-kind links
    having no host boundary at all. On a real run this turned one stray
    external URL into 50,000+ pending records across 1,000+ unrelated
    hosts (that external page's own <img>/<script src> references cascaded
    into fetching that whole other site). External resources must be
    fetched and stored (still "localized") but never parsed for outbound
    links -- they're leaves, not crawl roots."""
    import wpfreeze.crawl as crawl_module
    from wpfreeze.fetch import FetchOutcome, FetchResult

    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))

    external_url = "https://other-site.example/"
    # Simulate this having already entered the manifest as a render asset
    # to localize (the exact mechanism doesn't matter -- the bug is that
    # fetching it at all used to cascade further).
    manifest.get_or_create(external_url, discovered_via="crawl:https://example.com/")

    external_html = (
        b"<html><body>"
        b'<a href="https://yet-another.example/page">outbound link</a>'
        b'<img src="https://yet-another.example/logo.png">'
        b"</body></html>"
    )

    def fake_fetch(url, session, rate_limiter, fetch_config):
        return FetchOutcome(
            category="success",
            http_status=200,
            attempts=1,
            result=FetchResult(
                status_code=200,
                content=external_html,
                headers={"Content-Type": "text/html"},
                content_type="text/html",
                final_url=url,
            ),
        )

    monkeypatch.setattr(crawl_module, "fetch_with_retries", fake_fetch)

    crawl_fixpoint(
        manifest, profile, requests.Session(), RateLimiter(0.0), FAST_CONFIG, raw_dir, compile_exclusions([])
    )

    urls = {r.url for r in manifest.all()}
    assert external_url in urls
    assert manifest.get(external_url).status == Status.FETCHED.value
    assert not any("yet-another.example" in u for u in urls)  # never recursed into


def test_store_bytes_file_then_child_demotes_earlier_file(tmp_path: Path):
    from wpfreeze.crawl import store_bytes

    raw_dir = tmp_path / "raw"
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    manifest = Manifest()
    parent_url = "https://example.com/blob/0.35.3"
    child_url = "https://example.com/blob/0.35.3/LICENSE"

    parent_record = manifest.get_or_create(parent_url)
    parent_record.local_path = store_bytes(parent_url, b"parent page bytes", raw_dir, profile, manifest)

    child_record = manifest.get_or_create(child_url)
    child_record.local_path = store_bytes(child_url, b"license text", raw_dir, profile, manifest)  # must not raise

    # Both files survive, with distinct content.
    assert (raw_dir.parent / parent_record.local_path).read_bytes() == b"parent page bytes"
    assert (raw_dir.parent / child_record.local_path).read_bytes() == b"license text"
    assert parent_record.local_path != child_record.local_path
    # The demotion moved the parent's file, so its manifest record was repointed.
    assert "0.35.3/" in parent_record.local_path


def test_store_bytes_child_then_parent_uses_leaf_name_for_parent(tmp_path: Path):
    from wpfreeze.crawl import store_bytes

    raw_dir = tmp_path / "raw"
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    manifest = Manifest()
    parent_url = "https://example.com/blob/0.35.3"
    child_url = "https://example.com/blob/0.35.3/LICENSE"

    child_record = manifest.get_or_create(child_url)
    child_record.local_path = store_bytes(child_url, b"license text", raw_dir, profile, manifest)

    parent_record = manifest.get_or_create(parent_url)
    parent_record.local_path = store_bytes(parent_url, b"parent page bytes", raw_dir, profile, manifest)  # must not raise

    assert (raw_dir.parent / child_record.local_path).read_bytes() == b"license text"
    assert (raw_dir.parent / parent_record.local_path).read_bytes() == b"parent page bytes"
    assert parent_record.local_path != child_record.local_path


def test_script_derived_external_url_is_not_queued(tmp_path: Path, monkeypatch):
    """CLAUDE-acquire.md scopes <script> scanning to internal hosts/uploads
    paths, and excludes script-derived matches from the "render even if
    external" allowance given to genuine src/CSS/preload/og:image contexts.
    A JS blob merely mentioning a third-party URL (license comment,
    source-map reference, tracking config) must not get fetched -- this
    is exactly what pulled in unrelated github.com/yahoo.com pages on a
    real run."""
    import wpfreeze.crawl as crawl_module
    from wpfreeze.fetch import FetchOutcome, FetchResult

    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    base_url = "https://example.com/"
    manifest.get_or_create(base_url, discovered_via="base_url")

    html = (
        b"<html><body>"
        b"<script>"
        b"// Bundled dependency: see https://github.com/paulmillr/es6-shim/blob/0.35.3/LICENSE\n"
        b'var real = "/wp-content/uploads/real.jpg";'
        b"</script>"
        b"</body></html>"
    )

    def fake_fetch(url, session, rate_limiter, fetch_config):
        return FetchOutcome(
            category="success",
            http_status=200,
            attempts=1,
            result=FetchResult(
                status_code=200,
                content=html,
                headers={"Content-Type": "text/html"},
                content_type="text/html",
                final_url=url,
            ),
        )

    monkeypatch.setattr(crawl_module, "fetch_with_retries", fake_fetch)

    crawl_fixpoint(
        manifest, profile, requests.Session(), RateLimiter(0.0), FAST_CONFIG, raw_dir, compile_exclusions([])
    )

    urls = {r.url for r in manifest.all()}
    assert not any("github.com" in u for u in urls)
    assert any("wp-content/uploads/real.jpg" in u for u in urls)


def test_store_bytes_no_collision_stores_at_literal_path(tmp_path: Path):
    from wpfreeze.crawl import store_bytes

    raw_dir = tmp_path / "raw"
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    manifest = Manifest()
    url = "https://example.com/wp-content/uploads/photo.jpg"

    local_path = store_bytes(url, b"jpeg bytes", raw_dir, profile, manifest)
    assert local_path == "raw/wp-content/uploads/photo.jpg"
    assert (raw_dir.parent / local_path).read_bytes() == b"jpeg bytes"
