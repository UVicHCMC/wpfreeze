"""Rescan: re-parse already-stored bytes with today's extraction code.

There is no way to re-apply a link-extraction improvement to bytes
already sitting under raw/ -- every such fix currently costs a full
re-crawl of the origin. `rescan` closes that gap: it re-reads every
fetched HTML/CSS record's stored bytes, re-runs discover_links, and
queues any reference that isn't already a manifest record as a new
pending one, using exactly the crawl's own admission filter
(crawl.admit_link) and parse-eligibility gate (crawl._should_parse_for_links).

Deliberately does nothing else. It makes zero network calls, fetches
nothing, and does not run Stage 5 analysis (flag_*, compute_output_paths)
or regenerate reports -- the manifest it leaves behind is in exactly the
state an interrupted `acquire --resume` already knows how to pick up:
some records pending, everything else untouched. `acquire --resume` is
the second half of this feature, not a separate command to reinvent.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from wpfreeze.crawl import _should_parse_for_links, admit_link, content_kind, discover_links
from wpfreeze.manifest import Manifest, ManifestRecord, Status
from wpfreeze.urlnorm import SiteProfile

logger = logging.getLogger(__name__)

_RESCANNABLE_STATUSES = frozenset({Status.FETCHED.value, Status.FETCHED_WAYBACK.value})


@dataclass
class RescanStats:
    """Full accounting of one rescan pass. Every examined record lands in
    exactly one of the `skipped_*`/`records_parsed` buckets, so the two
    should always sum to `records_examined` -- useful as a self-check."""

    records_examined: int = 0
    records_parsed: int = 0
    skipped_wrong_status: int = 0
    skipped_wrong_kind: int = 0
    skipped_not_in_parse_scope: int = 0
    skipped_missing_file: int = 0
    skipped_hash_mismatch: int = 0
    links_extracted: int = 0
    links_admitted: int = 0
    records_created: int = 0
    provenance_only_hits: int = 0
    hash_mismatch_urls: list[str] = field(default_factory=list)


def _read_bytes_if_trustworthy(
    record: ManifestRecord, raw_dir: Path, stats: RescanStats
) -> bytes | None:
    """The stored bytes for `record`, or None if they can't be trusted.

    A manifest/disk content_hash mismatch means the manifest's own belief
    about what this record holds is wrong -- diagnostics.py already treats
    this as a first-class integrity defect (see its disk_hash_mismatches
    check). Parsing it anyway would attribute newly-discovered links to
    content the manifest doesn't actually have.
    """
    if not record.local_path:
        stats.skipped_missing_file += 1
        return None
    path = raw_dir.parent / record.local_path
    if not path.exists():
        stats.skipped_missing_file += 1
        return None
    content = path.read_bytes()
    if record.content_hash and hashlib.sha256(content).hexdigest() != record.content_hash:
        stats.skipped_hash_mismatch += 1
        stats.hash_mismatch_urls.append(record.url)
        return None
    return content


def rescan(manifest: Manifest, profile: SiteProfile, raw_dir: Path) -> RescanStats:
    """Re-parse every fetched HTML/CSS record's stored bytes and queue any
    newly-discovered reference as a new pending manifest record.

    Pure and offline: makes no network calls, and the only manifest
    mutation is via `Manifest.get_or_create` -- which never resets an
    existing record to pending and resolves through known redirects
    rather than resurrecting them (see manifest.py). No existing record's
    status/content_hash/local_path/flags/etc. is ever touched here.

    Resolution is always against `record.url`, never an entry in
    `aliases`/`redirect_from` -- both writers that populate `local_path`
    (crawl._record_success, wayback._recover_one) store the fetched bytes
    keyed by `record.url` itself, so it is always the right base for
    resolving the relative links found inside those bytes.

    `profile` must be the SiteProfile the original crawl actually probed
    (persisted on the manifest as of schema version 2) -- reconstructing
    one from config alone can disagree with the crawl on canonical host,
    trailing-slash preference, or https support, which would normalize
    the very same discovered link to a different key than the crawl used
    and flood the manifest with spurious duplicate pending records. See
    urlnorm.scope_profile_from_config's own docstring.
    """
    stats = RescanStats()
    for record in list(manifest.all()):
        stats.records_examined += 1

        if record.status not in _RESCANNABLE_STATUSES:
            stats.skipped_wrong_status += 1
            continue

        kind = content_kind(record.content_type, record.url)
        if kind is None:
            stats.skipped_wrong_kind += 1
            continue

        host = (urlsplit(record.url).hostname or "").lower()
        if not _should_parse_for_links(kind, record.url, host, profile):
            # External HTML stays a leaf here exactly as it does during
            # the live crawl -- see _should_parse_for_links for why.
            stats.skipped_not_in_parse_scope += 1
            continue

        content = _read_bytes_if_trustworthy(record, raw_dir, stats)
        if content is None:
            continue

        stats.records_parsed += 1
        links = discover_links(content, record.url, kind)
        stats.links_extracted += len(links)

        for link in links:
            normalized = admit_link(link, profile)
            if normalized is None:
                continue
            stats.links_admitted += 1

            before = len(manifest)
            manifest.get_or_create(normalized, discovered_via=f"rescan:{record.url}")
            if len(manifest) > before:
                stats.records_created += 1
            else:
                stats.provenance_only_hits += 1

    return stats


def format_rescan_summary(stats: RescanStats) -> str:
    lines = ["Rescan:"]
    lines.append(
        f"  {stats.records_examined} record(s) examined, {stats.records_parsed} re-parsed"
    )
    skipped = (
        stats.skipped_wrong_status
        + stats.skipped_wrong_kind
        + stats.skipped_not_in_parse_scope
        + stats.skipped_missing_file
        + stats.skipped_hash_mismatch
    )
    if skipped:
        lines.append(
            f"  skipped: {stats.skipped_wrong_status} wrong status, "
            f"{stats.skipped_wrong_kind} not html/css, "
            f"{stats.skipped_not_in_parse_scope} out of parse scope, "
            f"{stats.skipped_missing_file} missing on disk, "
            f"{stats.skipped_hash_mismatch} manifest/disk hash mismatch"
        )
    lines.append(
        f"  {stats.links_extracted} link(s) extracted, {stats.links_admitted} admitted"
    )
    lines.append(
        f"  {stats.records_created} new pending record(s) queued, "
        f"{stats.provenance_only_hits} already known (provenance added only)"
    )
    if stats.hash_mismatch_urls:
        lines.append(f"  ** {len(stats.hash_mismatch_urls)} hash mismatch(es) -- see diagnose **")
    return "\n".join(lines)
