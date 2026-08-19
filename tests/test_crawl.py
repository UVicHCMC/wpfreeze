from __future__ import annotations

import hashlib
import threading
import time
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


def test_admit_link_leaves_a_cross_origin_iframe_uncaptured():
    """iframe[src] is RENDER-kind (see extract._RENDER_ATTRS) but gets its
    own narrower "owned" gate in admit_link -- a genuine third-party embed
    (YouTube, Vimeo, ...) is never queued, so build.py's rewriter leaves it
    pointing at the real, live original instead of a downloaded,
    non-functional snapshot of the embed page. A same-site or
    same-network-sibling embed is still admitted, same as any other RENDER
    reference -- only unrelated hosts are held back."""
    from wpfreeze.crawl import admit_link
    from wpfreeze.extract import RENDER, ExtractedLink

    profile = SiteProfile(
        canonical_host="example.com", site_hosts=frozenset({"example.com", "sibling.example.com"})
    )

    cross_origin = ExtractedLink("https://www.youtube.com/embed/abc123", RENDER, "iframe[src]")
    assert admit_link(cross_origin, profile) is None

    same_site = ExtractedLink("https://example.com/embedded-page/", RENDER, "iframe[src]")
    assert admit_link(same_site, profile) is not None

    network_sibling = ExtractedLink("https://sibling.example.com/widget/", RENDER, "iframe[src]")
    assert admit_link(network_sibling, profile) is not None


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
        assert logo_record.local_path == "raw/_external/http_127.0.0.2/logo.png"
        assert (raw_dir / "_external" / "http_127.0.0.2" / "logo.png").read_bytes() == b"CDN-LOGO-BYTES"

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


def test_store_bytes_distinct_query_strings_on_root_path_do_not_collide(tmp_path: Path):
    """A real run had 36 WordPress `?attachment_id=N` permalinks plus the
    true homepage all sharing the literal path "/" -- normalize_url keeps
    attachment_id (and similar) for identity, but local_path_for used to
    ignore the query entirely, so every fetch silently overwrote the same
    raw/index.html and the manifest's own content_hash for the homepage
    record drifted out of sync with what was actually on disk."""
    from wpfreeze.crawl import store_bytes

    raw_dir = tmp_path / "raw"
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    manifest = Manifest()

    home_record = manifest.get_or_create("https://example.com/")
    home_record.local_path = store_bytes(
        "https://example.com/", b"real homepage", raw_dir, profile, manifest
    )

    attachment_record = manifest.get_or_create("https://example.com/?attachment_id=4353")
    attachment_record.local_path = store_bytes(
        "https://example.com/?attachment_id=4353", b"orphaned attachment page", raw_dir, profile, manifest
    )

    assert home_record.local_path != attachment_record.local_path
    assert (raw_dir.parent / home_record.local_path).read_bytes() == b"real homepage"
    assert (raw_dir.parent / attachment_record.local_path).read_bytes() == b"orphaned attachment page"


def test_store_bytes_external_host_http_and_https_do_not_collide(tmp_path: Path):
    """normalize_url upgrades http -> https for the site's *own* host, but
    never touches an external host's scheme -- so http://fonts.googleapis.com/css
    and https://fonts.googleapis.com/css are genuinely distinct manifest
    records. Found colliding on disk (same raw/_external/<host>/<path>,
    last write silently winning) via the new `diagnose` command's very
    first real-world run."""
    from wpfreeze.crawl import store_bytes

    raw_dir = tmp_path / "raw"
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    manifest = Manifest()

    https_record = manifest.get_or_create("https://fonts.googleapis.com/css")
    https_record.local_path = store_bytes(
        "https://fonts.googleapis.com/css", b"https version", raw_dir, profile, manifest
    )

    http_record = manifest.get_or_create("http://fonts.googleapis.com/css")
    http_record.local_path = store_bytes(
        "http://fonts.googleapis.com/css", b"http version", raw_dir, profile, manifest
    )

    assert https_record.local_path != http_record.local_path
    assert (raw_dir.parent / https_record.local_path).read_bytes() == b"https version"
    assert (raw_dir.parent / http_record.local_path).read_bytes() == b"http version"


# ---------------------------------------------------------------------------
# Concurrency (workers > 1): deterministic races via a barrier-synced fake
# fetch, guaranteeing genuinely concurrent workers at the moment of
# interest rather than hoping for a scheduling accident.
# ---------------------------------------------------------------------------


def _fetch_outcome(final_url: str, content: bytes = b"ok", content_type: str = "text/plain"):
    from wpfreeze.fetch import FetchOutcome, FetchResult

    return FetchOutcome(
        category="success",
        http_status=200,
        attempts=1,
        result=FetchResult(
            status_code=200,
            content=content,
            headers={"Content-Type": content_type},
            content_type=content_type,
            final_url=final_url,
            redirect_chain=[],
        ),
    )


def test_concurrent_redirect_race_merges_into_one_target(tmp_path: Path, monkeypatch):
    """Two independently-pending URLs whose own fetches both redirect to
    the same final URL, processed by two workers at once (barrier-synced).
    Also checks the staleness guard: neither URL's fake fetch is ever
    called more than once, even though resolving one redirect can only
    happen after both fetches return (guarded by the barrier)."""
    import wpfreeze.crawl as crawl_module

    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(
        canonical_host="example.com", site_hosts=frozenset({"example.com"}), trailing_slash=False
    )

    url_a = "https://example.com/a"
    url_b = "https://example.com/b"
    url_c = "https://example.com/c"
    manifest.get_or_create(url_a, discovered_via="base_url")
    manifest.get_or_create(url_b, discovered_via="base_url")

    barrier = threading.Barrier(2, timeout=5)
    call_counts: dict[str, int] = {}
    counts_lock = threading.Lock()

    def fake_fetch(url, session, rate_limiter, fetch_config):
        with counts_lock:
            call_counts[url] = call_counts.get(url, 0) + 1
        barrier.wait()
        return _fetch_outcome(url_c)

    monkeypatch.setattr(crawl_module, "fetch_with_retries", fake_fetch)

    crawl_module.crawl_fixpoint(
        manifest, profile, requests.Session(), RateLimiter(0.0), FAST_CONFIG, raw_dir,
        compile_exclusions([]), workers=2,
    )

    assert manifest.get(url_a) is None
    assert manifest.get(url_b) is None
    target = manifest.get(url_c)
    assert target is not None
    assert target.status == Status.FETCHED.value
    assert set(target.redirect_from) == {url_a, url_b}
    assert set(target.aliases) == {url_a, url_b}
    assert call_counts == {url_a: 1, url_b: 1}  # neither ever re-fetched


def test_concurrent_storage_collision_parent_and_child_paths(tmp_path: Path, monkeypatch):
    """Parent/child path-prefix collision (see store_bytes), but with both
    URLs fetched by concurrent workers instead of sequentially."""
    import wpfreeze.crawl as crawl_module

    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(
        canonical_host="example.com", site_hosts=frozenset({"example.com"}), trailing_slash=False
    )

    parent_url = "https://example.com/blob/1.0"
    child_url = "https://example.com/blob/1.0/LICENSE"
    manifest.get_or_create(parent_url, discovered_via="base_url")
    manifest.get_or_create(child_url, discovered_via="base_url")

    barrier = threading.Barrier(2, timeout=5)

    def fake_fetch(url, session, rate_limiter, fetch_config):
        barrier.wait()
        content = b"parent page bytes" if url == parent_url else b"license text"
        return _fetch_outcome(url, content=content)

    monkeypatch.setattr(crawl_module, "fetch_with_retries", fake_fetch)

    crawl_module.crawl_fixpoint(
        manifest, profile, requests.Session(), RateLimiter(0.0), FAST_CONFIG, raw_dir,
        compile_exclusions([]), workers=2,
    )

    parent_record = manifest.get(parent_url)
    child_record = manifest.get(child_url)
    assert parent_record.status == Status.FETCHED.value
    assert child_record.status == Status.FETCHED.value
    assert (raw_dir.parent / parent_record.local_path).read_bytes() == b"parent page bytes"
    assert (raw_dir.parent / child_record.local_path).read_bytes() == b"license text"
    assert parent_record.local_path != child_record.local_path


def test_concurrency_actually_parallelizes_across_hosts(tmp_path: Path, monkeypatch):
    """Not just correctness -- proof the workers run concurrently at all.
    N URLs on N distinct hosts, each fake fetch takes ~0.25s; sequential
    processing would take N*0.25s, concurrent should take roughly 0.25s."""
    import wpfreeze.crawl as crawl_module

    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    n = 5
    hosts = [f"host{i}.example.com" for i in range(n)]
    # Only host[0] is "the site" -- the rest are genuinely external hosts.
    # normalize_url folds every *owned* host to the profile's single
    # canonical_host, so declaring all N as owned would collapse these
    # back down to one URL and defeat the point of this test.
    profile = SiteProfile(canonical_host=hosts[0], site_hosts=frozenset({hosts[0]}))
    urls = [f"https://{h}/" for h in hosts]
    for url in urls:
        manifest.get_or_create(url, discovered_via="base_url")

    def fake_fetch(url, session, rate_limiter, fetch_config):
        time.sleep(0.25)
        return _fetch_outcome(url)

    monkeypatch.setattr(crawl_module, "fetch_with_retries", fake_fetch)

    started = time.monotonic()
    crawl_module.crawl_fixpoint(
        manifest, profile, requests.Session(), RateLimiter(0.0), FAST_CONFIG, raw_dir,
        compile_exclusions([]), workers=n,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.6 * n * 0.25, f"took {elapsed}s -- workers do not appear to run concurrently"
    assert all(manifest.get(u).status == Status.FETCHED.value for u in urls)


def test_concurrent_crawl_exception_propagates_and_stays_resumable(tmp_path: Path, monkeypatch):
    """Mirrors test_crawl_is_resumable_after_interruption, but with
    workers=2: a mid-crawl exception must still leave the on-disk manifest
    valid, and a subsequent resume with a healthy fetch must complete."""
    import wpfreeze.crawl as crawl_module

    raw_dir = tmp_path / "raw"
    manifest_path = tmp_path / "manifest.json"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    urls = [f"https://example.com/page{i}" for i in range(5)]
    for url in urls:
        manifest.get_or_create(url, discovered_via="base_url")

    call_count = {"n": 0}
    count_lock = threading.Lock()

    def flaky_fetch(url, session, rate_limiter, fetch_config):
        with count_lock:
            call_count["n"] += 1
            n = call_count["n"]
        if n == 3:
            raise RuntimeError("simulated crash mid-crawl")
        return _fetch_outcome(url)

    monkeypatch.setattr(crawl_module, "fetch_with_retries", flaky_fetch)

    with pytest.raises(RuntimeError):
        crawl_module.crawl_fixpoint(
            manifest, profile, requests.Session(), RateLimiter(0.0), FAST_CONFIG, raw_dir,
            compile_exclusions([]), manifest_save_path=manifest_path, workers=2,
        )

    reloaded = Manifest.load(manifest_path)
    assert len(reloaded) == 5

    def healthy_fetch(url, session, rate_limiter, fetch_config):
        return _fetch_outcome(url)

    monkeypatch.setattr(crawl_module, "fetch_with_retries", healthy_fetch)
    crawl_module.crawl_fixpoint(
        reloaded, profile, requests.Session(), RateLimiter(0.0), FAST_CONFIG, raw_dir,
        compile_exclusions([]), manifest_save_path=manifest_path, workers=2,
    )

    assert all(r.status != Status.PENDING.value for r in reloaded.all())


def test_css_outside_base_path_is_still_parsed_for_its_own_references():
    """On a multisite subdirectory install the whole theme lives under the
    network-wide /wp-content/, outside base_path. Confining the CSS parse
    gate to in_scope() fetched those stylesheets but never read them, so
    every font and background image they reference went undiscovered.
    CSS cannot cascade the way HTML can -- extract_from_css emits only
    RENDER links -- so owned-host is the right bar for it."""
    from wpfreeze.crawl import _should_parse_for_links
    from wpfreeze.urlnorm import SiteProfile

    profile = SiteProfile(
        canonical_host="example.com",
        site_hosts=frozenset({"example.com"}),
        base_path="/courses/",
    )
    shared_css = "https://example.com/wp-content/themes/divi/style.css"
    assert not profile.in_scope(shared_css)
    assert _should_parse_for_links("css", shared_css, "example.com", profile)


def test_html_outside_base_path_is_never_parsed_for_links():
    """The cascade guard: an out-of-scope HTML page must stay a leaf, or
    its hyperlinks pull in another site's whole graph."""
    from wpfreeze.crawl import _should_parse_for_links
    from wpfreeze.urlnorm import SiteProfile

    profile = SiteProfile(
        canonical_host="example.com",
        site_hosts=frozenset({"example.com"}),
        base_path="/courses/",
    )
    sibling = "https://example.com/siblinglab/index.html"
    assert not _should_parse_for_links("html", sibling, "example.com", profile)
    assert _should_parse_for_links("html", "https://example.com/courses/a/", "example.com", profile)


def test_css_on_an_unowned_host_is_still_a_leaf():
    from wpfreeze.crawl import _should_parse_for_links
    from wpfreeze.urlnorm import SiteProfile

    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    assert not _should_parse_for_links("css", "https://cdn.other.net/x.css", "cdn.other.net", profile)
    assert not _should_parse_for_links(None, "https://example.com/x.bin", "example.com", profile)
