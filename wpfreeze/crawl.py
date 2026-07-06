"""Fixpoint crawl: fetch the manifest's pending queue, extract links,
discover more pending URLs, and repeat until none remain.

See CLAUDE-acquire.md, "Stage 2 -- Crawl to fixpoint".
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

from wpfreeze.extract import HYPERLINK, extract_from_css, extract_from_html
from wpfreeze.fetch import SUCCESS, WAYBACK_CANDIDATE, FetchConfig, FetchResult, RateLimiter, fetch_with_retries
from wpfreeze.manifest import Manifest, ManifestRecord, Source, Status, utc_now
from wpfreeze.urlnorm import SiteProfile, normalize_url

logger = logging.getLogger(__name__)

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_CSS_CONTENT_TYPE = "text/css"


def compile_exclusions(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p) for p in patterns]


def is_excluded(url: str, exclusions: list[re.Pattern]) -> bool:
    return any(p.search(url) for p in exclusions)


def local_path_for(url: str, raw_dir: Path, profile: SiteProfile) -> Path:
    """Where fetched bytes for `url` are stored on disk.

    The site's own host mirrors its path structure directly under
    raw_dir; any other host is namespaced raw_dir/_external/<host>/<path>
    so two different hosts sharing a path shape can never collide.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if path == "" or path.endswith("/"):
        path += "index.html"
    path = path.lstrip("/")
    if profile.owns_host(host):
        return raw_dir / path
    return raw_dir / "_external" / host / path


def _content_kind(content_type: str | None, url: str) -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _HTML_CONTENT_TYPES or url.endswith((".html", ".htm")):
        return "html"
    if ctype == _CSS_CONTENT_TYPE or url.endswith(".css"):
        return "css"
    return None


def _store_bytes(url: str, content: bytes, raw_dir: Path, profile: SiteProfile) -> str:
    local_path = local_path_for(url, raw_dir, profile)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content)
    return str(Path("raw") / local_path.relative_to(raw_dir))


def _record_success(
    record: ManifestRecord,
    result: FetchResult,
    final_url: str,
    manifest: Manifest,
    profile: SiteProfile,
    raw_dir: Path,
) -> ManifestRecord:
    """Populate the correct record (following redirects to their target)
    with a successful fetch's data, and fold every redirect hop into it as
    a redirect_from/alias. Returns the record that now holds the content."""
    if final_url != record.url:
        hops = [record.url] + [normalize_url(h, profile) for h in result.redirect_chain]
        for hop in dict.fromkeys(hops):
            if hop != final_url:
                manifest.resolve_redirect(hop, final_url)
        target = manifest.get(final_url)
        assert target is not None
    else:
        target = record

    target.content_hash = hashlib.sha256(result.content).hexdigest()
    target.content_type = result.content_type
    target.local_path = _store_bytes(final_url, result.content, raw_dir, profile)
    target.status = Status.FETCHED.value
    target.source = Source.LIVE.value
    return target


def _discover_links(html_or_css: bytes, final_url: str, kind: str) -> list:
    text = html_or_css.decode("utf-8", errors="replace")
    if kind == "html":
        return extract_from_html(text, final_url)
    return extract_from_css(text, final_url)


def _process_one(
    record: ManifestRecord,
    manifest: Manifest,
    profile: SiteProfile,
    session: requests.Session,
    rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
    raw_dir: Path,
    exclusions: list[re.Pattern],
) -> None:
    url = record.url

    if is_excluded(url, exclusions):
        record.status = Status.EXCLUDED.value
        logger.info("excluded %s", url)
        return

    outcome = fetch_with_retries(url, session, rate_limiter, fetch_config)
    record.fetch_attempts += outcome.attempts
    record.last_fetched = utc_now()
    record.http_status = outcome.http_status
    logger.info("fetched %s -> %s (%d attempt(s))", url, outcome.http_status, outcome.attempts)

    if outcome.category == SUCCESS:
        result = outcome.result
        assert result is not None
        final_url = normalize_url(result.final_url, profile)
        target = _record_success(record, result, final_url, manifest, profile, raw_dir)

        kind = _content_kind(target.content_type, final_url)
        if kind is not None:
            for link in _discover_links(result.content, final_url, kind):
                normalized = normalize_url(link.url, profile)
                host = urlsplit(normalized).hostname or ""
                owned = profile.owns_host(host)
                if link.kind == HYPERLINK and not owned:
                    continue  # external hyperlink targets do not enter as pending
                manifest.get_or_create(normalized, discovered_via=f"crawl:{final_url}")
        return

    if outcome.category == WAYBACK_CANDIDATE:
        record.status = Status.RETRYING.value
        if outcome.flag:
            record.add_flag(outcome.flag)
        return


def crawl_fixpoint(
    manifest: Manifest,
    profile: SiteProfile,
    session: requests.Session,
    rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
    raw_dir: Path,
    exclusions: list[re.Pattern],
    manifest_save_path: Path | None = None,
) -> None:
    """Process every pending record, discovering more as links are
    extracted, until no pending records remain. Not a fixed number of
    passes -- a true fixpoint loop.

    If `manifest_save_path` is given, the manifest is saved atomically
    after every processed record, so an interrupted run loses at most the
    one in-flight fetch.
    """
    while True:
        pending = manifest.by_status(Status.PENDING.value)
        if not pending:
            break
        for record in pending:
            if record.status != Status.PENDING.value:
                continue  # already resolved as part of a redirect merge this pass
            _process_one(record, manifest, profile, session, rate_limiter, fetch_config, raw_dir, exclusions)
            if manifest_save_path is not None:
                manifest.save(manifest_save_path)
