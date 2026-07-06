from __future__ import annotations

from pathlib import Path

import pytest

from wpfreeze.analyse import (
    apply_canonical_cascade,
    find_canonical_link,
    flag_ambiguous_canonical,
    flag_attachment_pages,
    flag_forms_and_plugin_markup,
    flag_hash_duplicates,
    flag_orphans_and_unlisted,
)
from wpfreeze.manifest import (
    FLAG_AMBIGUOUS_CANONICAL,
    FLAG_ATTACHMENT_PAGE,
    FLAG_CONTAINS_FORM,
    FLAG_HASH_DUPLICATE,
    FLAG_ORPHAN,
    FLAG_PLUGIN_MARKUP,
    FLAG_UNLISTED,
    Manifest,
    ManifestRecord,
    Status,
)
from wpfreeze.urlnorm import SiteProfile

PROFILE = SiteProfile(
    canonical_host="example.com",
    site_hosts=frozenset({"example.com"}),
    use_https=True,
    trailing_slash=True,
)


def _write_page(output_dir: Path, rel_path: str, html: str) -> str:
    full = output_dir / "raw" / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(html)
    return str(Path("raw") / rel_path)


def _fetched_record(url: str, local_path: str | None = None, content_type: str = "text/html") -> ManifestRecord:
    return ManifestRecord(
        url=url,
        status=Status.FETCHED.value,
        local_path=local_path,
        content_type=content_type,
    )


# ---------------------------------------------------------------------------
# orphan / unlisted
# ---------------------------------------------------------------------------


def test_orphan_flagged_when_inventory_only():
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/never-linked/", discovered_via="sitemap")
    record.status = Status.FETCHED.value
    flag_orphans_and_unlisted(manifest)
    assert FLAG_ORPHAN in record.flags
    assert FLAG_UNLISTED not in record.flags


def test_unlisted_flagged_when_crawl_only():
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/secret-page/", discovered_via="crawl:https://example.com/")
    record.status = Status.FETCHED.value
    flag_orphans_and_unlisted(manifest)
    assert FLAG_UNLISTED in record.flags
    assert FLAG_ORPHAN not in record.flags


def test_neither_flagged_when_both_sitemap_and_crawl():
    manifest = Manifest()
    record = manifest.get_or_create("https://example.com/normal/")
    record.add_discovered_via("sitemap")
    record.add_discovered_via("crawl:https://example.com/")
    flag_orphans_and_unlisted(manifest)
    assert FLAG_ORPHAN not in record.flags
    assert FLAG_UNLISTED not in record.flags


# ---------------------------------------------------------------------------
# forms / plugin markup
# ---------------------------------------------------------------------------


def test_contains_form_flag(tmp_path: Path):
    local_path = _write_page(tmp_path, "contact/index.html", "<html><body><form action='/x'></form></body></html>")
    manifest = Manifest()
    manifest.upsert(_fetched_record("https://example.com/contact/", local_path))
    flag_forms_and_plugin_markup(manifest, tmp_path / "raw")
    assert FLAG_CONTAINS_FORM in manifest.get("https://example.com/contact/").flags


def test_plugin_markup_flag_detected(tmp_path: Path):
    local_path = _write_page(
        tmp_path, "gallery/index.html", '<html><body><div class="ngg-gallery">x</div></body></html>'
    )
    manifest = Manifest()
    manifest.upsert(_fetched_record("https://example.com/gallery/", local_path))
    flag_forms_and_plugin_markup(manifest, tmp_path / "raw")
    assert FLAG_PLUGIN_MARKUP in manifest.get("https://example.com/gallery/").flags


def test_no_false_positive_flags_on_plain_page(tmp_path: Path):
    local_path = _write_page(tmp_path, "plain/index.html", "<html><body><p>Hello</p></body></html>")
    manifest = Manifest()
    manifest.upsert(_fetched_record("https://example.com/plain/", local_path))
    flag_forms_and_plugin_markup(manifest, tmp_path / "raw")
    assert manifest.get("https://example.com/plain/").flags == []


# ---------------------------------------------------------------------------
# attachment pages
# ---------------------------------------------------------------------------


def test_attachment_page_flagged_by_body_class(tmp_path: Path):
    local_path = _write_page(
        tmp_path, "photo-1/index.html", '<html><body class="attachment single-attachment">x</body></html>'
    )
    manifest = Manifest()
    manifest.upsert(_fetched_record("https://example.com/photo-1/", local_path))
    flag_attachment_pages(manifest, tmp_path / "raw")
    assert FLAG_ATTACHMENT_PAGE in manifest.get("https://example.com/photo-1/").flags


def test_attachment_page_flagged_by_media_inventory(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_fetched_record("https://example.com/photo-2/"))
    flag_attachment_pages(manifest, tmp_path / "raw", media_urls={"https://example.com/photo-2/"})
    assert FLAG_ATTACHMENT_PAGE in manifest.get("https://example.com/photo-2/").flags


# ---------------------------------------------------------------------------
# canonical cascade
# ---------------------------------------------------------------------------


def test_find_canonical_link():
    html = '<html><head><link rel="canonical" href="/real-page/"></head></html>'
    assert find_canonical_link(html) == "/real-page/"


def test_find_canonical_link_absent():
    assert find_canonical_link("<html><head></head></html>") is None


def test_canonical_cascade_folds_duplicate_into_declared_canonical(tmp_path: Path):
    dup_path = _write_page(
        tmp_path,
        "duplicate/index.html",
        '<html><head><link rel="canonical" href="https://example.com/real/"></head></html>',
    )
    manifest = Manifest()
    dup_record = manifest.get_or_create("https://example.com/duplicate/", discovered_via="crawl:https://example.com/")
    dup_record.status = Status.FETCHED.value
    dup_record.local_path = dup_path
    dup_record.content_type = "text/html"

    created_pending = apply_canonical_cascade(manifest, PROFILE, tmp_path / "raw")

    assert created_pending is True
    assert "https://example.com/duplicate/" not in manifest
    real = manifest.get("https://example.com/real/")
    assert real is not None
    assert real.status == Status.PENDING.value  # newly created, awaits a further crawl pass
    assert "https://example.com/duplicate/" in real.aliases


def test_canonical_cascade_ignores_self_referencing_canonical(tmp_path: Path):
    local_path = _write_page(
        tmp_path,
        "self/index.html",
        '<html><head><link rel="canonical" href="https://example.com/self/"></head></html>',
    )
    manifest = Manifest()
    manifest.upsert(_fetched_record("https://example.com/self/", local_path))
    created_pending = apply_canonical_cascade(manifest, PROFILE, tmp_path / "raw")
    assert created_pending is False
    assert "https://example.com/self/" in manifest


def test_canonical_cascade_ignores_external_canonical(tmp_path: Path):
    local_path = _write_page(
        tmp_path,
        "syndicated/index.html",
        '<html><head><link rel="canonical" href="https://elsewhere.com/original/"></head></html>',
    )
    manifest = Manifest()
    manifest.upsert(_fetched_record("https://example.com/syndicated/", local_path))
    created_pending = apply_canonical_cascade(manifest, PROFILE, tmp_path / "raw")
    assert created_pending is False
    assert "https://example.com/syndicated/" in manifest


# ---------------------------------------------------------------------------
# hash_duplicate / ambiguous_canonical
# ---------------------------------------------------------------------------


def test_hash_duplicate_prefers_non_resize_suffixed_canonical():
    manifest = Manifest()
    thumb = _fetched_record("https://example.com/wp-content/uploads/photo-150x150.jpg", content_type="image/jpeg")
    full = _fetched_record("https://example.com/wp-content/uploads/photo.jpg", content_type="image/jpeg")
    thumb.content_hash = full.content_hash = "deadbeef"
    thumb.first_seen = "2026-01-01T00:00:00+00:00"
    full.first_seen = "2026-01-02T00:00:00+00:00"  # later, but should still win: not resize-suffixed
    manifest.upsert(thumb)
    manifest.upsert(full)

    canonical_map = flag_hash_duplicates(manifest)

    assert canonical_map == {thumb.url: full.url}
    assert FLAG_HASH_DUPLICATE in thumb.flags
    assert FLAG_HASH_DUPLICATE not in full.flags


def test_hash_duplicate_falls_back_to_earliest_first_seen():
    manifest = Manifest()
    a = _fetched_record("https://example.com/wp-content/uploads/copy-a.jpg", content_type="image/jpeg")
    b = _fetched_record("https://example.com/wp-content/uploads/copy-b.jpg", content_type="image/jpeg")
    a.content_hash = b.content_hash = "cafef00d"
    a.first_seen = "2026-01-01T00:00:00+00:00"
    b.first_seen = "2026-01-02T00:00:00+00:00"
    manifest.upsert(a)
    manifest.upsert(b)

    canonical_map = flag_hash_duplicates(manifest)
    assert canonical_map == {b.url: a.url}


def test_no_hash_duplicate_for_singleton_hash():
    manifest = Manifest()
    record = _fetched_record("https://example.com/unique.jpg", content_type="image/jpeg")
    record.content_hash = "onlyone"
    manifest.upsert(record)
    assert flag_hash_duplicates(manifest) == {}
    assert record.flags == []


def test_ambiguous_canonical_flagged_for_undeclared_html_duplicates():
    manifest = Manifest()
    a = _fetched_record("https://example.com/page-a/", content_type="text/html")
    b = _fetched_record("https://example.com/page-b/", content_type="text/html")
    a.content_hash = b.content_hash = "samehash"
    a.first_seen = "2026-01-01T00:00:00+00:00"
    b.first_seen = "2026-01-02T00:00:00+00:00"
    manifest.upsert(a)
    manifest.upsert(b)

    canonical_map = flag_hash_duplicates(manifest)
    flag_ambiguous_canonical(manifest, canonical_map)

    assert FLAG_AMBIGUOUS_CANONICAL in a.flags
    assert FLAG_AMBIGUOUS_CANONICAL in b.flags


def test_ambiguous_canonical_not_flagged_for_images():
    manifest = Manifest()
    a = _fetched_record("https://example.com/img-a.jpg", content_type="image/jpeg")
    b = _fetched_record("https://example.com/img-b.jpg", content_type="image/jpeg")
    a.content_hash = b.content_hash = "samehash"
    manifest.upsert(a)
    manifest.upsert(b)

    canonical_map = flag_hash_duplicates(manifest)
    flag_ambiguous_canonical(manifest, canonical_map)

    assert FLAG_AMBIGUOUS_CANONICAL not in a.flags
    assert FLAG_AMBIGUOUS_CANONICAL not in b.flags
