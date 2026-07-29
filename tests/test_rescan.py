from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import requests

from wpfreeze.crawl import compile_exclusions, crawl_fixpoint, store_bytes
from wpfreeze.extract import HYPERLINK, ExtractedLink
from wpfreeze.fetch import FetchConfig, RateLimiter
from wpfreeze.manifest import Manifest, Status
from wpfreeze.rescan import RescanStats, rescan
from wpfreeze.urlnorm import SiteProfile

from fixture_site import FixtureSite

FAST_CONFIG = FetchConfig(max_attempts=2, timeout=2.0, backoff_base=0.01)


def _fixture_profile() -> SiteProfile:
    return SiteProfile(
        canonical_host="127.0.0.1",
        site_hosts=frozenset({"127.0.0.1"}),
        use_https=False,
        trailing_slash=True,
    )


def _crawl_fixture(site: FixtureSite, raw_dir: Path) -> Manifest:
    manifest = Manifest()
    manifest.get_or_create(site.site_base + "/", discovered_via="base_url")
    crawl_fixpoint(
        manifest,
        _fixture_profile(),
        requests.Session(),
        RateLimiter(0.0),
        FAST_CONFIG,
        raw_dir,
        compile_exclusions([]),
    )
    return manifest


def _store(
    url: str,
    content: bytes,
    content_type: str,
    raw_dir: Path,
    profile: SiteProfile,
    manifest: Manifest,
    status: str = Status.FETCHED.value,
):
    """Directly populate a fetched record's bytes/hash/local_path, mirroring
    what crawl._record_success/wayback._recover_one do -- for tests that
    only care about rescan's own behaviour, not a full crawl."""
    record = manifest.get_or_create(url)
    record.content_type = content_type
    record.content_hash = hashlib.sha256(content).hexdigest()
    record.local_path = store_bytes(url, content, raw_dir, profile, manifest)
    record.status = status
    return record


# ---------------------------------------------------------------------------
# Core behaviour against a real crawl
# ---------------------------------------------------------------------------


def test_rescan_after_a_real_crawl_finds_nothing_new(tmp_path: Path):
    """A rescan with unchanged extraction code against a capture that
    extraction code already produced must queue zero new records -- this
    is the headline idempotency guarantee (no bytes on disk have changed,
    and current code parsed them once already, during the crawl)."""
    raw_dir = tmp_path / "raw"
    with FixtureSite() as site:
        manifest = _crawl_fixture(site, raw_dir)
        before_urls = {r.url for r in manifest.all()}

        stats = rescan(manifest, _fixture_profile(), raw_dir)

        assert stats.records_created == 0
        assert {r.url for r in manifest.all()} == before_urls


def test_rescan_is_idempotent_across_consecutive_runs(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    with FixtureSite() as site:
        manifest = _crawl_fixture(site, raw_dir)
        profile = _fixture_profile()

        rescan(manifest, profile, raw_dir)
        second = rescan(manifest, profile, raw_dir)

        assert second.records_created == 0
        assert second.provenance_only_hits == second.links_admitted


def test_rescan_queues_a_reference_only_todays_extraction_code_finds(tmp_path: Path, monkeypatch):
    """Simulates the actual feature: the crawl already ran (stored bytes
    are fixed), then an extraction improvement lands and rescan re-parses
    those *same* bytes with the *new* logic, finding something the
    original crawl's extraction missed."""
    import wpfreeze.rescan as rescan_module

    raw_dir = tmp_path / "raw"
    with FixtureSite() as site:
        manifest = _crawl_fixture(site, raw_dir)
        profile = _fixture_profile()
        about_url = site.site_base + "/about/"
        new_url = site.site_base + "/newly-discovered/"
        assert manifest.get(about_url) is not None
        assert manifest.get(new_url) is None

        real_discover_links = rescan_module.discover_links

        def patched(content: bytes, final_url: str, kind: str):
            links = real_discover_links(content, final_url, kind)
            if final_url == about_url:
                links = [*links, ExtractedLink(new_url, HYPERLINK, "test:injected")]
            return links

        monkeypatch.setattr(rescan_module, "discover_links", patched)

        stats = rescan(manifest, profile, raw_dir)

        assert stats.records_created == 1
        new_record = manifest.get(new_url)
        assert new_record is not None
        assert new_record.status == Status.PENDING.value
        assert new_record.discovered_via == [f"rescan:{about_url}"]


def test_rescan_never_mutates_an_existing_record(tmp_path: Path, monkeypatch):
    """Beyond discovered_via growing on the page that yielded a new
    reference, no field of any existing record may change -- rescan only
    ever adds."""
    import wpfreeze.rescan as rescan_module

    raw_dir = tmp_path / "raw"
    with FixtureSite() as site:
        manifest = _crawl_fixture(site, raw_dir)
        profile = _fixture_profile()
        about_url = site.site_base + "/about/"

        real_discover_links = rescan_module.discover_links

        def patched(content: bytes, final_url: str, kind: str):
            links = real_discover_links(content, final_url, kind)
            if final_url == about_url:
                links = [*links, ExtractedLink(site.site_base + "/fresh/", HYPERLINK, "test:injected")]
            return links

        monkeypatch.setattr(rescan_module, "discover_links", patched)

        before = {r.url: r.to_dict() for r in manifest.all()}
        rescan(manifest, profile, raw_dir)
        after = {r.url: r.to_dict() for r in manifest.all()}

        # discovered_via may grow -- get_or_create legitimately appends
        # "rescan:<source>" provenance even to an already-known target
        # (I6 permits this explicitly) -- but every other field on every
        # pre-existing record must be untouched, and any new entry must be
        # rescan's own provenance tag, never a crawl:/wayback: one.
        for url, before_dict in before.items():
            after_dict = dict(after[url])
            before_discovered = before_dict["discovered_via"]
            after_discovered = after_dict.pop("discovered_via")
            before_dict = dict(before_dict)
            before_dict.pop("discovered_via")
            assert after_dict == before_dict
            assert after_discovered[: len(before_discovered)] == before_discovered
            for new_entry in after_discovered[len(before_discovered) :]:
                assert new_entry.startswith("rescan:")


# ---------------------------------------------------------------------------
# Parse-scope gate (mirrors test_crawl.py's _should_parse_for_links tests)
# ---------------------------------------------------------------------------


def test_rescan_leaves_external_html_as_a_leaf(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))

    external_html = b'<html><body><a href="https://other.example/unseen/">x</a></body></html>'
    _store("https://other-site.example/", external_html, "text/html", raw_dir, profile, manifest)

    stats = rescan(manifest, profile, raw_dir)

    assert stats.skipped_not_in_parse_scope == 1
    assert stats.records_created == 0
    assert manifest.get("https://other.example/unseen/") is None


def test_rescan_parses_owned_host_css_outside_base_path(tmp_path: Path):
    """Multisite subdirectory shape: CSS living under the network-wide
    /wp-content/, outside base_path, is still on an owned host and must be
    parsed -- CSS can't cascade the way HTML can."""
    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(
        canonical_host="example.com", site_hosts=frozenset({"example.com"}), base_path="/courses/"
    )

    css = b"body { background: url(/wp-content/uploads/hero.jpg); }"
    _store(
        "https://example.com/wp-content/themes/demo/style.css",
        css,
        "text/css",
        raw_dir,
        profile,
        manifest,
    )

    stats = rescan(manifest, profile, raw_dir)

    assert stats.records_created == 1
    assert manifest.get("https://example.com/wp-content/uploads/hero.jpg") is not None


def test_rescan_leaves_html_outside_base_path_as_a_leaf(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(
        canonical_host="example.com", site_hosts=frozenset({"example.com"}), base_path="/courses/"
    )

    sibling_html = b'<html><body><a href="/rocketry/members/">Members</a></body></html>'
    _store(
        "https://example.com/rocketry/", sibling_html, "text/html", raw_dir, profile, manifest
    )

    stats = rescan(manifest, profile, raw_dir)

    assert stats.skipped_not_in_parse_scope == 1
    assert manifest.get("https://example.com/rocketry/members/") is None


# ---------------------------------------------------------------------------
# Integrity gates
# ---------------------------------------------------------------------------


def test_rescan_skips_records_not_in_a_fetched_status(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    manifest.get_or_create("https://example.com/still-pending/")

    stats = rescan(manifest, profile, raw_dir)

    assert stats.skipped_wrong_status == 1
    assert stats.records_parsed == 0


def test_rescan_skips_missing_file_on_disk(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    record = _store(
        "https://example.com/page/", b"<html></html>", "text/html", raw_dir, profile, manifest
    )
    (raw_dir.parent / record.local_path).unlink()

    stats = rescan(manifest, profile, raw_dir)

    assert stats.skipped_missing_file == 1
    assert stats.records_parsed == 0


def test_rescan_skips_content_hash_mismatch(tmp_path: Path):
    """The manifest's own belief about what this record holds is wrong --
    parsing it anyway would attribute newly-found links to content the
    manifest doesn't actually have."""
    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    url = "https://example.com/page/"
    record = _store(
        url,
        b'<html><body><a href="/found-me/">x</a></body></html>',
        "text/html",
        raw_dir,
        profile,
        manifest,
    )
    (raw_dir.parent / record.local_path).write_bytes(b"<html>different bytes now</html>")

    stats = rescan(manifest, profile, raw_dir)

    assert stats.skipped_hash_mismatch == 1
    assert stats.hash_mismatch_urls == [url]
    assert stats.records_parsed == 0
    assert manifest.get("https://example.com/found-me/") is None


def test_rescan_reports_examined_count_partitions_exhaustively(tmp_path: Path):
    """records_examined should always equal the sum of every skip bucket
    plus records_parsed -- an accounting self-check, not a behavioural
    requirement on its own, but a cheap way to catch a record silently
    falling through every gate uncounted."""
    raw_dir = tmp_path / "raw"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    manifest.get_or_create("https://example.com/pending/")  # skipped_wrong_status
    _store(
        "https://example.com/asset.png", b"binary", "image/png", raw_dir, profile, manifest
    )  # skipped_wrong_kind
    _store(
        "https://example.com/page/", b"<html></html>", "text/html", raw_dir, profile, manifest
    )  # parsed

    stats = rescan(manifest, profile, raw_dir)

    accounted = (
        stats.skipped_wrong_status
        + stats.skipped_wrong_kind
        + stats.skipped_not_in_parse_scope
        + stats.skipped_missing_file
        + stats.skipped_hash_mismatch
        + stats.records_parsed
    )
    assert accounted == stats.records_examined == 3


# ---------------------------------------------------------------------------
# Report-only: no manifest mutation is a return-value guarantee, not a
# side-effecting one -- rescan() itself always mutates the in-memory
# Manifest it's given (report-vs-apply is the CLI's job, see test_cli.py);
# this just confirms the pure function never writes to disk itself.
# ---------------------------------------------------------------------------


def test_rescan_itself_never_touches_manifest_json(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    manifest_path = tmp_path / "manifest.json"
    manifest = Manifest()
    profile = SiteProfile(canonical_host="example.com", site_hosts=frozenset({"example.com"}))
    _store(
        "https://example.com/page/",
        b'<html><body><a href="/x/">x</a></body></html>',
        "text/html",
        raw_dir,
        profile,
        manifest,
    )
    manifest.save(manifest_path)
    before_bytes = manifest_path.read_bytes()

    rescan(manifest, profile, raw_dir)

    assert manifest_path.read_bytes() == before_bytes
