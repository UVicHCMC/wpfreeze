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
