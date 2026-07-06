from __future__ import annotations

import json
from pathlib import Path

import pytest

from wpfreeze.manifest import (
    FLAG_AUTH_GATED,
    FLAG_HASH_DUPLICATE,
    FLAG_ORPHAN,
    Manifest,
    ManifestRecord,
    Status,
)


def test_get_or_create_is_idempotent():
    manifest = Manifest()
    a = manifest.get_or_create("https://example.com/", discovered_via="sitemap")
    b = manifest.get_or_create("https://example.com/", discovered_via="crawl:https://example.com/x")
    assert a is b
    assert len(manifest) == 1
    assert a.discovered_via == ["sitemap", "crawl:https://example.com/x"]


def test_discovered_via_dedup():
    record = ManifestRecord(url="https://example.com/")
    record.add_discovered_via("sitemap")
    record.add_discovered_via("sitemap")
    assert record.discovered_via == ["sitemap"]


def test_alias_dedup_and_excludes_self():
    record = ManifestRecord(url="https://example.com/")
    record.add_alias("https://example.com/")  # self, ignored
    record.add_alias("https://example.com/?p=1")
    record.add_alias("https://example.com/?p=1")  # dup, ignored
    assert record.aliases == ["https://example.com/?p=1"]


def test_flag_dedup():
    record = ManifestRecord(url="https://example.com/")
    record.add_flag(FLAG_ORPHAN)
    record.add_flag(FLAG_ORPHAN)
    assert record.flags == [FLAG_ORPHAN]


def test_is_gap_by_status():
    record = ManifestRecord(url="https://example.com/x", status=Status.MISSING.value)
    assert record.is_gap()

    record2 = ManifestRecord(url="https://example.com/y", status=Status.FETCHED.value)
    assert not record2.is_gap()


def test_is_gap_by_flag():
    record = ManifestRecord(url="https://example.com/x", status=Status.FETCHED.value)
    record.add_flag(FLAG_AUTH_GATED)
    assert record.is_gap()


def test_is_gap_excludes_editorial_flags():
    """hash_duplicate/orphan etc. flag content worth reviewing, not a gap."""
    record = ManifestRecord(url="https://example.com/x", status=Status.FETCHED.value)
    record.add_flag(FLAG_HASH_DUPLICATE)
    record.add_flag(FLAG_ORPHAN)
    assert not record.is_gap()


def test_manifest_has_gaps():
    manifest = Manifest()
    manifest.upsert(ManifestRecord(url="https://example.com/ok", status=Status.FETCHED.value))
    assert not manifest.has_gaps()
    manifest.upsert(ManifestRecord(url="https://example.com/bad", status=Status.MISSING.value))
    assert manifest.has_gaps()


def test_round_trip_preserves_all_fields(tmp_path: Path):
    manifest = Manifest()
    record = ManifestRecord(
        url="https://example.com/foo/",
        aliases=["https://example.com/foo"],
        status=Status.FETCHED_WAYBACK.value,
        http_status=200,
        source="wayback",
        wayback_url="https://web.archive.org/web/20200101000000id_/https://example.com/foo/",
        content_hash="a" * 64,
        content_type="text/html",
        local_path="raw/foo/index.html",
        output_path="/foo.html",
        discovered_via=["sitemap", "crawl:https://example.com/"],
        redirect_from=["https://example.com/foo"],
        flags=["orphan"],
        first_seen="2026-01-01T00:00:00+00:00",
        last_fetched="2026-01-02T00:00:00+00:00",
        fetch_attempts=2,
    )
    manifest.upsert(record)

    manifest_path = tmp_path / "manifest.json"
    manifest.save(manifest_path)

    reloaded = Manifest.load(manifest_path)
    assert len(reloaded) == 1
    reloaded_record = reloaded.get("https://example.com/foo/")
    assert reloaded_record is not None
    assert reloaded_record.to_dict() == record.to_dict()


def test_save_is_atomic_no_leftover_temp_files(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(ManifestRecord(url="https://example.com/"))
    manifest_path = tmp_path / "manifest.json"
    manifest.save(manifest_path)

    leftovers = [p for p in tmp_path.iterdir() if p.name != "manifest.json"]
    assert leftovers == []


def test_save_failure_leaves_original_untouched(tmp_path: Path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    original = Manifest()
    original.upsert(ManifestRecord(url="https://example.com/"))
    original.save(manifest_path)
    original_bytes = manifest_path.read_bytes()

    broken = Manifest()
    broken.upsert(ManifestRecord(url="https://example.com/new"))

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-write")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(RuntimeError):
        broken.save(manifest_path)

    assert manifest_path.read_bytes() == original_bytes
    leftovers = [p for p in tmp_path.iterdir() if p.name != "manifest.json"]
    assert leftovers == []


def test_load_missing_file_returns_empty_manifest(tmp_path: Path):
    manifest = Manifest.load(tmp_path / "does-not-exist.json")
    assert len(manifest) == 0


def test_by_status_filters():
    manifest = Manifest()
    manifest.upsert(ManifestRecord(url="https://example.com/a", status=Status.FETCHED.value))
    manifest.upsert(ManifestRecord(url="https://example.com/b", status=Status.MISSING.value))
    manifest.upsert(ManifestRecord(url="https://example.com/c", status=Status.FETCHED.value))

    fetched = manifest.by_status(Status.FETCHED.value)
    assert {r.url for r in fetched} == {"https://example.com/a", "https://example.com/c"}


def test_resume_never_resets_existing_record_to_pending():
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/", discovered_via="sitemap")
    record.status = Status.FETCHED.value
    record.content_hash = "deadbeef"

    # A later discovery pass re-encountering the same URL (e.g. via crawl)
    # must not clobber the fetched state.
    same = manifest.get_or_create("https://example.com/", discovered_via="crawl:https://example.com/")
    assert same.status == Status.FETCHED.value
    assert same.content_hash == "deadbeef"
