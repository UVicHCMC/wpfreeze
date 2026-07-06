"""Stage 5 -- post-crawl analysis and flagging.

See CLAUDE-acquire.md, "Stage 5 -- Analysis and flags" and "Canonical URL
cascade". Operates purely on an already-crawled Manifest plus the stored
bytes under raw/ -- no network.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from wpfreeze.manifest import (
    FLAG_AMBIGUOUS_CANONICAL,
    FLAG_ATTACHMENT_PAGE,
    FLAG_CONTAINS_FORM,
    FLAG_DB_UNRESOLVED,
    FLAG_HASH_DUPLICATE,
    FLAG_ORPHAN,
    FLAG_PLUGIN_MARKUP,
    FLAG_UNLISTED,
    Manifest,
    ManifestRecord,
    Status,
)
from wpfreeze.urlnorm import SiteProfile, normalize_url

logger = logging.getLogger(__name__)

_FETCHED_STATUSES = {Status.FETCHED.value, Status.FETCHED_WAYBACK.value}

# A small, documented, extensible list of dynamic-plugin markup markers
# (see CLAUDE-acquire.md Stage 5). Add more (class/id/script-handle
# patterns for other gallery/slider/form plugins) as real sites surface
# them -- keep each entry's provenance obvious from its key.
PLUGIN_MARKUP_PATTERNS: dict[str, list[re.Pattern]] = {
    "nextgen_gallery": [re.compile(r"ngg-gallery"), re.compile(r"ngg_images")],
    "jetpack_slideshow": [re.compile(r"jetpack-slideshow")],
    "contact_form_7": [re.compile(r"wpcf7")],
    "gravity_forms": [re.compile(r"gform_wrapper"), re.compile(r"gform_\d+")],
    "revolution_slider": [re.compile(r"rev_slider")],
    "smart_slider": [re.compile(r"n2-ss-slider")],
    "elementor": [re.compile(r"elementor-widget")],
    "wpforms": [re.compile(r"wpforms-form")],
}

_WP_RESIZE_SUFFIX_RE = re.compile(r"-\d+x\d+(?=\.\w+$)")


def _read_html(record: ManifestRecord, raw_dir: Path) -> str | None:
    if record.status not in _FETCHED_STATUSES or not record.local_path:
        return None
    content_type = (record.content_type or "").split(";")[0].strip().lower()
    if content_type and content_type not in ("text/html", "application/xhtml+xml"):
        return None
    path = raw_dir.parent / record.local_path
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def find_canonical_link(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("link", rel="canonical")
    if tag is None:
        return None
    href = tag.get("href")
    return href.strip() if href else None


def flag_orphans_and_unlisted(manifest: Manifest) -> None:
    """orphan: known from inventory (sitemap/rest_api/database), never
    reached by crawl. unlisted: reached by crawl, absent from every
    inventory source."""
    for record in manifest.all():
        sources = record.discovered_via
        from_inventory = any(s in ("sitemap", "rest_api", "database") for s in sources)
        from_crawl = any(s.startswith("crawl:") or s.startswith("wayback:") for s in sources)
        if from_inventory and not from_crawl:
            record.add_flag(FLAG_ORPHAN)
        elif from_crawl and not from_inventory:
            record.add_flag(FLAG_UNLISTED)


def flag_db_unresolved(manifest: Manifest) -> None:
    """A database-inventory URL that never resolved to a servable page:
    stronger signal than a plain `missing` since the database, not just a
    sitemap or crawl, asserts this content should exist."""
    for record in manifest.all():
        if record.status == Status.MISSING.value and "database" in record.discovered_via:
            record.add_flag(FLAG_DB_UNRESOLVED)


def flag_forms_and_plugin_markup(manifest: Manifest, raw_dir: Path) -> None:
    for record in manifest.all():
        html = _read_html(record, raw_dir)
        if html is None:
            continue
        if re.search(r"<form[\s>]", html, re.IGNORECASE):
            record.add_flag(FLAG_CONTAINS_FORM)
        for patterns in PLUGIN_MARKUP_PATTERNS.values():
            if any(p.search(html) for p in patterns):
                record.add_flag(FLAG_PLUGIN_MARKUP)
                break


def flag_attachment_pages(manifest: Manifest, raw_dir: Path, media_urls: set[str] | None = None) -> None:
    """Attachment wrapper pages: detected via body class "attachment", or
    membership in the REST media inventory (`media_urls`, if supplied)."""
    media_urls = media_urls or set()
    for record in manifest.all():
        if record.url in media_urls:
            record.add_flag(FLAG_ATTACHMENT_PAGE)
            continue
        html = _read_html(record, raw_dir)
        if html is None:
            continue
        soup = BeautifulSoup(html, "lxml")
        body = soup.find("body")
        if body is not None:
            classes = body.get("class") or []
            if "attachment" in classes:
                record.add_flag(FLAG_ATTACHMENT_PAGE)


def apply_canonical_cascade(manifest: Manifest, profile: SiteProfile, raw_dir: Path) -> bool:
    """Tier 1 of the canonical cascade: a page whose <link rel="canonical">
    names a different same-site URL is folded into that URL as a "soft
    redirect" -- same mechanism as an HTTP redirect (Manifest.resolve_redirect),
    just discovered from content rather than a 3xx status. If the
    canonical target isn't already a manifest record, it's created pending
    so a further crawl pass fetches it for real.

    Tiers 2 (sitemap) and 3 (post-redirect final URL) need no separate
    handling here: sitemap-discovered URLs already feed the same
    normalized manifest key, and the post-redirect final URL is already
    what Stage 2 keys every record on.

    Returns True if any new pending record was created (caller should run
    another crawl_fixpoint pass before treating the manifest as settled).
    """
    created_pending = False
    for record in list(manifest.all()):
        html = _read_html(record, raw_dir)
        if html is None:
            continue
        canonical_href = find_canonical_link(html)
        if not canonical_href:
            continue
        canonical_url = normalize_url(canonical_href, profile)
        host = (urlsplit(canonical_url).hostname or "").lower()
        if canonical_url == record.url or not profile.owns_host(host):
            continue
        existed = canonical_url in manifest
        manifest.resolve_redirect(record.url, canonical_url)
        if not existed:
            created_pending = True
    return created_pending


def _choose_hash_duplicate_canonical(records: list[ManifestRecord]) -> ManifestRecord:
    """Exclude WordPress resize-suffixed paths (-WIDTHxHEIGHT before the
    extension) if a non-suffixed member exists; otherwise earliest
    first_seen. See CLAUDE-acquire.md, hash_duplicate flag."""
    non_suffixed = [r for r in records if not _WP_RESIZE_SUFFIX_RE.search(urlsplit(r.url).path)]
    pool = non_suffixed or records
    return min(pool, key=lambda r: r.first_seen)


def flag_hash_duplicates(manifest: Manifest) -> dict[str, str]:
    """Group fetched records by content_hash; flag every non-canonical
    member hash_duplicate. Returns {non_canonical_url: canonical_url} for
    Stage 6 to collapse output paths onto.
    """
    groups: dict[str, list[ManifestRecord]] = {}
    for record in manifest.all():
        if record.status in _FETCHED_STATUSES and record.content_hash:
            groups.setdefault(record.content_hash, []).append(record)

    canonical_by_url: dict[str, str] = {}
    for records in groups.values():
        if len(records) < 2:
            continue
        canonical = _choose_hash_duplicate_canonical(records)
        for record in records:
            if record is canonical:
                continue
            record.add_flag(FLAG_HASH_DUPLICATE)
            canonical_by_url[record.url] = canonical.url
    return canonical_by_url


def flag_ambiguous_canonical(manifest: Manifest, hash_duplicate_canonical: dict[str, str]) -> None:
    """Any HTML page left sharing a content_hash with another *after* the
    canonical cascade and hash_duplicate grouping -- i.e. no explicit
    canonical relationship was ever declared between them -- is flagged
    for human review rather than guessed at."""
    duplicate_urls = set(hash_duplicate_canonical) | set(hash_duplicate_canonical.values())
    for record in manifest.all():
        if record.url not in duplicate_urls:
            continue
        content_type = (record.content_type or "").split(";")[0].strip().lower()
        if content_type in ("text/html", "application/xhtml+xml"):
            record.add_flag(FLAG_AMBIGUOUS_CANONICAL)
