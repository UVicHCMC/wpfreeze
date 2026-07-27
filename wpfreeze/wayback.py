"""Wayback Machine recovery for records that couldn't be fetched live.

See CLAUDE-acquire.md, "Stage 4 -- Wayback recovery". Fetches the
original bytes via the CDX API's `id_` timestamp suffix -- never
Wayback's own rewritten HTML, which would poison Module 2's link
rewriting downstream.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

from wpfreeze.crawl import _should_parse_for_links, content_kind, discover_links, store_bytes
from wpfreeze.extract import HYPERLINK
from wpfreeze.fetch import SUCCESS, FetchConfig, RateLimiter, fetch_with_retries
from wpfreeze.manifest import Manifest, ManifestRecord, Source, Status
from wpfreeze.urlnorm import SiteProfile, normalize_url

logger = logging.getLogger(__name__)

DEFAULT_CDX_API = "https://web.archive.org/cdx/search/cdx"
DEFAULT_WAYBACK_BASE = "https://web.archive.org"


@dataclass(frozen=True)
class Snapshot:
    timestamp: str
    status_code: str
    original_url: str


def cdx_query_url(url: str, cdx_api: str = DEFAULT_CDX_API) -> str:
    return f"{cdx_api}?{urlencode({'url': url, 'output': 'json'})}"


def parse_cdx_response(raw_json: str) -> list[Snapshot]:
    """Parse the CDX API's array-of-arrays JSON: row 0 is the field
    header, every subsequent row is one snapshot."""
    data = json.loads(raw_json)
    if not data:
        return []
    header = data[0]
    try:
        ts_idx = header.index("timestamp")
        status_idx = header.index("statuscode")
        original_idx = header.index("original")
    except ValueError:
        logger.warning("unexpected CDX response header: %r", header)
        return []
    return [
        Snapshot(timestamp=row[ts_idx], status_code=row[status_idx], original_url=row[original_idx])
        for row in data[1:]
    ]


def choose_snapshot(snapshots: list[Snapshot], prefer_near: date) -> Snapshot | None:
    """Pick the snapshot with status 200 nearest `prefer_near`."""
    candidates = [s for s in snapshots if s.status_code == "200"]
    if not candidates:
        return None
    target = datetime(prefer_near.year, prefer_near.month, prefer_near.day)

    def distance(s: Snapshot) -> float:
        return abs((datetime.strptime(s.timestamp, "%Y%m%d%H%M%S") - target).total_seconds())

    return min(candidates, key=distance)


def wayback_fetch_url(timestamp: str, original_url: str, wayback_base: str = DEFAULT_WAYBACK_BASE) -> str:
    """The id_ suffix requests the original bytes, not Wayback's
    link-rewritten replay HTML."""
    return f"{wayback_base}/web/{timestamp}id_/{original_url}"


def _mark_unrecoverable(record: ManifestRecord, profile: SiteProfile) -> None:
    host = (urlsplit(record.url).hostname or "").lower()
    if profile.owns_host(host):
        record.status = Status.MISSING.value
    else:
        record.status = Status.EXTERNAL_UNFETCHABLE.value


def recover_via_wayback(
    manifest: Manifest,
    profile: SiteProfile,
    session: requests.Session,
    wayback_rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
    raw_dir: Path,
    prefer_snapshots_near: date,
    manifest_save_path: Path | None = None,
    cdx_api: str = DEFAULT_CDX_API,
    wayback_base: str = DEFAULT_WAYBACK_BASE,
) -> None:
    """Attempt Wayback recovery for every Status.RETRYING record (Stage 3's
    Wayback candidates). Successful recoveries become fetched_wayback and
    feed newly-discovered links back into the manifest as pending, so this
    interleaves with a further crawl_fixpoint pass (Stage 2/4 joint
    fixpoint) at the caller's discretion.

    Deliberately sequential, unlike crawl_fixpoint -- every request here
    (CDX lookup and snapshot fetch) targets the same host, web.archive.org,
    and RateLimiter already serializes same-host requests globally. Worker
    threads would just queue on that single limiter for zero throughput
    gain; this isn't an oversight, don't "fix" it with a ThreadPoolExecutor.

    `cdx_api`/`wayback_base` default to the real Wayback Machine endpoints;
    tests override them to point at a local fake.
    """
    candidates = manifest.by_status(Status.RETRYING.value)
    for record in candidates:
        _recover_one(
            record,
            manifest,
            profile,
            session,
            wayback_rate_limiter,
            fetch_config,
            raw_dir,
            prefer_snapshots_near,
            cdx_api,
            wayback_base,
        )
        if manifest_save_path is not None:
            manifest.save(manifest_save_path)


def _recover_one(
    record: ManifestRecord,
    manifest: Manifest,
    profile: SiteProfile,
    session: requests.Session,
    wayback_rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
    raw_dir: Path,
    prefer_snapshots_near: date,
    cdx_api: str = DEFAULT_CDX_API,
    wayback_base: str = DEFAULT_WAYBACK_BASE,
) -> None:
    cdx_outcome = fetch_with_retries(
        cdx_query_url(record.url, cdx_api), session, wayback_rate_limiter, fetch_config
    )
    if cdx_outcome.category != SUCCESS:
        logger.info("CDX lookup failed for %s", record.url)
        _mark_unrecoverable(record, profile)
        return

    try:
        snapshots = parse_cdx_response(cdx_outcome.result.content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        # Seen in practice: the CDX API returns HTTP 200 with a non-JSON
        # (often empty) body when it's rate-limiting or otherwise unhappy,
        # rather than a retriable error status -- so fetch_with_retries
        # reports SUCCESS and this is the first point that can detect it.
        logger.info("CDX response was not valid JSON for %s", record.url)
        _mark_unrecoverable(record, profile)
        return

    chosen = choose_snapshot(snapshots, prefer_snapshots_near)
    if chosen is None:
        logger.info("no usable Wayback snapshot for %s", record.url)
        _mark_unrecoverable(record, profile)
        return

    snapshot_url = wayback_fetch_url(chosen.timestamp, record.url, wayback_base)
    fetch_outcome = fetch_with_retries(snapshot_url, session, wayback_rate_limiter, fetch_config)
    if fetch_outcome.category != SUCCESS:
        logger.info("Wayback snapshot fetch failed for %s (%s)", record.url, snapshot_url)
        _mark_unrecoverable(record, profile)
        return

    result = fetch_outcome.result
    record.content_hash = hashlib.sha256(result.content).hexdigest()
    record.content_type = result.content_type
    record.local_path = store_bytes(record.url, result.content, raw_dir, profile, manifest)
    record.status = Status.FETCHED_WAYBACK.value
    record.source = Source.WAYBACK.value
    record.wayback_url = snapshot_url
    logger.info("recovered %s from Wayback snapshot %s", record.url, chosen.timestamp)

    kind = content_kind(record.content_type, record.url)
    recovered_host = (urlsplit(record.url).hostname or "").lower()
    if _should_parse_for_links(kind, record.url, recovered_host, profile):
        # See crawl.py::_should_parse_for_links -- HTML is confined to the
        # site being archived (or a sibling page recovered from Wayback
        # becomes a crawl root of its own and cascades into that other
        # site's whole graph); CSS only has to be on an owned host.
        for link in discover_links(result.content, record.url, kind):
            normalized = normalize_url(link.url, profile)
            host = (urlsplit(normalized).hostname or "").lower()
            owned = profile.owns_host(host)
            if link.kind == HYPERLINK and not profile.in_scope(normalized):
                continue
            if link.context.startswith("script:") and not owned:
                continue  # see crawl.py::_process_one for why
            manifest.get_or_create(normalized, discovered_via=f"wayback:{record.url}")
