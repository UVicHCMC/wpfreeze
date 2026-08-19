"""Fixpoint crawl: fetch the manifest's pending queue, extract links,
discover more pending URLs, and repeat until none remain.

See CLAUDE-acquire.md, "Stage 2 -- Crawl to fixpoint".
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    raw_dir; any other host is namespaced raw_dir/_external/<scheme>_<host>/
    <path> so two different hosts sharing a path shape can never collide.
    The scheme is folded into that namespace too, not just the host:
    normalize_url upgrades http -> https for the site's *own* host when it
    serves https, but never touches an external host's scheme, so
    "https://fonts.googleapis.com/css" and "http://fonts.googleapis.com/css"
    are genuinely distinct manifest records (found colliding on disk on a
    real run's very first diagnose pass).

    normalize_url (urlnorm.py) deliberately keeps a handful of WordPress
    identity query params (p=/page_id=/attachment_id=/author=/cat=/tag=/
    taxonomy=/term=) that a pretty-permalink-less page still needs -- so
    e.g. "/?attachment_id=1029" and "/?attachment_id=4353" are genuinely
    distinct manifest records with distinct content, both sharing the
    literal path "/". Any surviving query string is fed into the filename
    so those records never collide on disk (a real run had 36 such
    attachment URLs plus the true homepage all silently overwriting one
    shared raw/index.html, last write invisibly winning).
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if path == "" or path.endswith("/"):
        path += "index.html"
    path = path.lstrip("/")
    if parsed.query:
        query_hash = hashlib.sha256(parsed.query.encode()).hexdigest()[:10]
        as_path = Path(path)
        path = str(as_path.with_name(f"{as_path.stem}__q-{query_hash}{as_path.suffix}"))
    if profile.owns_host(host):
        return raw_dir / path
    return raw_dir / "_external" / f"{parsed.scheme.lower()}_{host}" / path


def content_kind(content_type: str | None, url: str) -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _HTML_CONTENT_TYPES or url.endswith((".html", ".htm")):
        return "html"
    if ctype == _CSS_CONTENT_TYPE or url.endswith(".css"):
        return "css"
    return None


_COLLISION_LEAF_NAME = "_wpfreeze_leaf"


def store_bytes(url: str, content: bytes, raw_dir: Path, profile: SiteProfile, manifest: Manifest) -> str:
    """Store `content` at the path `local_path_for` computes for `url`.

    Real sites routinely have one URL's path be a strict prefix of
    another's (e.g. .../v1.2.3 as a page in its own right, and
    .../v1.2.3/LICENSE as a file beneath it) -- a literal path-preserving
    mirror can't represent both a leaf file and a directory at the same
    name. When that collision is detected, in either direction, the
    earlier arrival is moved one level down to a fixed disambiguating leaf
    name and its manifest record's local_path is repointed to match, so
    nothing already written is lost or orphaned.
    """
    # Each of store_bytes/_make_ancestors_writable/_demote_file_to_directory
    # takes manifest.lock itself rather than trusting callers to already
    # hold it -- unit tests call store_bytes directly with no external
    # locking, and the RLock makes nested acquisition from _process_one's
    # already-locked context safe too.
    with manifest.lock:
        local_path = local_path_for(url, raw_dir, profile)
        _make_ancestors_writable(local_path.parent, raw_dir, manifest)
        if local_path.is_dir():
            # An earlier URL was a child of this one and already claimed this
            # exact path as a directory; this URL's own bytes get the leaf name.
            local_path = local_path / _COLLISION_LEAF_NAME
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        return str(Path("raw") / local_path.relative_to(raw_dir))


def _make_ancestors_writable(directory: Path, raw_dir: Path, manifest: Manifest) -> None:
    """Ensure every path component from raw_dir down to `directory` is
    either absent or already a directory, demoting any earlier file found
    blocking the way."""
    with manifest.lock:
        current = raw_dir
        for part in directory.relative_to(raw_dir).parts:
            current = current / part
            if current.is_file():
                _demote_file_to_directory(current, raw_dir, manifest)


def _demote_file_to_directory(path: Path, raw_dir: Path, manifest: Manifest) -> None:
    """An earlier URL's bytes are sitting exactly where a later URL now
    needs a directory. Move the earlier file one level down to
    _COLLISION_LEAF_NAME and repoint whichever manifest record pointed at
    it, so both URLs keep their content."""
    with manifest.lock:
        content = path.read_bytes()
        old_rel = str(Path("raw") / path.relative_to(raw_dir))
        path.unlink()
        path.mkdir(parents=True)
        new_path = path / _COLLISION_LEAF_NAME
        new_path.write_bytes(content)
        new_rel = str(Path("raw") / new_path.relative_to(raw_dir))
        for record in manifest.all():
            if record.local_path == old_rel:
                record.local_path = new_rel
                break


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
    a redirect_from/alias. Returns the record that now holds the content.

    If the redirect target already holds content from its own earlier
    fetch, that content is left alone -- first successful write wins, not
    last. Without this, two genuinely distinct pages whose URLs collide
    onto the same normalized identity (a URL-normalization gap, or the
    site itself redirecting an unrelated page to the same target)
    silently overwrite each other depending on unpredictable
    fetch-scheduling order. Seen on a real crawl: an orphaned WordPress
    attachment page that the live site redirects to the site root
    clobbered the actual homepage's already-fetched content this way.
    """
    if final_url != record.url:
        hops = [record.url] + [normalize_url(h, profile) for h in result.redirect_chain]
        for hop in dict.fromkeys(hops):
            if hop != final_url:
                manifest.resolve_redirect(hop, final_url)
        target = manifest.get(final_url)
        assert target is not None
        if target.status in (Status.FETCHED.value, Status.FETCHED_WAYBACK.value):
            return target
    else:
        target = record

    target.content_hash = hashlib.sha256(result.content).hexdigest()
    target.content_type = result.content_type
    target.local_path = store_bytes(final_url, result.content, raw_dir, profile, manifest)
    target.status = Status.FETCHED.value
    target.source = Source.LIVE.value
    return target


def discover_links(html_or_css: bytes, final_url: str, kind: str) -> list:
    text = html_or_css.decode("utf-8", errors="replace")
    if kind == "html":
        return extract_from_html(text, final_url)
    return extract_from_css(text, final_url)


def _should_parse_for_links(
    kind: str | None, final_url: str, final_host: str, profile: SiteProfile
) -> bool:
    """Whether a fetched resource may be parsed for further references.

    HTML is confined to the site being archived. An out-of-scope HTML page
    admitted here becomes a crawl root of its own, and its hyperlinks
    cascade into fetching another site's entire graph -- a real run
    amplified one such external page into 50,000+ pending records across
    1,000+ unrelated hosts. External HTML is still fetched and stored
    (satisfying "render even if external, localize it") but stays a leaf.

    CSS is confined only to owned hosts, not to base_path. It cannot
    cascade the way HTML does: extract_from_css emits nothing but RENDER
    links, and RENDER links are already deliberately unconfined (see
    _process_one). Holding CSS to base_path bought no safety and lost
    real assets -- on a multisite subdirectory install the whole theme
    lives under the network-wide /wp-content/, so its stylesheets were
    fetched but never read, and every font and background image they
    reference went undiscovered.
    """
    if kind is None:
        return False
    if kind == "css":
        return profile.owns_host(final_host)
    return profile.in_scope(final_url)


def admit_link(link, profile: SiteProfile) -> str | None:
    """Normalized URL if `link` may become a manifest record, else None.

    The one gate deciding which extracted links are worth queuing at all --
    shared between the live crawl (_process_one, below) and `rescan`, so a
    stored capture re-parsed offline is admitted exactly as it would have
    been during the original crawl.

    External hyperlink targets do not enter as pending -- and on a
    multisite subdirectory install, neither do links into sibling sites,
    which share the hostname but are not the site being archived. RENDER
    links are deliberately NOT confined this way: an image a page embeds
    from a sibling site (or from the network-wide /wp-content/) is part of
    how this page looks, so it is still fetched and localized under
    "render even if external".

    Per CLAUDE-acquire.md, "Link extraction": <script> scanning is scoped
    to internal hosts/uploads paths -- unlike genuine src/CSS/preload/
    og:image contexts, a script-derived match is only a URL-shaped-string
    heuristic (JS comments, license/source-map mentions, tracking config)
    and gets no "render even if external" allowance.

    iframe[src] gets a narrower version of the same treatment: admitted
    when `owned` (a same-site or same-network-sibling embed -- genuinely
    capturable content, no different from any other RENDER reference), but
    not for a genuine third-party host. `owned`, not `in_scope`, on purpose:
    the sibling-site case above is exactly what a strict in_scope check
    would wrongly reject here too. Unlike script's heuristic match, an
    iframe target is a real, deliberate embed either way -- the reason to
    hold it back is not confidence in the extraction, it's that a
    third-party document (YouTube, Vimeo, Google Maps, a social embed)
    depends on live JS/API calls no static snapshot can reproduce; saving
    one anyway produces a dead embed in the build, not a working one.
    """
    normalized = normalize_url(link.url, profile)
    host = urlsplit(normalized).hostname or ""
    owned = profile.owns_host(host)
    if link.kind == HYPERLINK and not profile.in_scope(normalized):
        return None
    if link.context.startswith("script:") and not owned:
        return None
    if link.context == "iframe[src]" and not owned:
        return None
    return normalized


def _process_one(
    record: ManifestRecord,
    manifest: Manifest,
    profile: SiteProfile,
    session: requests.Session,
    rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
    raw_dir: Path,
    exclusions: list[re.Pattern],
    manifest_save_path: Path | None = None,
) -> None:
    """Fetch and record the result for one pending record.

    Two separate `manifest.lock` acquisitions, with the actual network
    fetch happening between them (never under the lock): a pre-fetch
    check that this record is still live and pending -- another worker
    may have already folded it away as a redirect hop since this round's
    snapshot was taken -- and a post-fetch block recording the outcome.
    There is deliberately no re-check between fetching and recording: if
    a record gets merged away while its fetch is in flight, the post-fetch
    bookkeeping (resolve_redirect, add_alias/add_redirect_from) is
    idempotent, so the worst case is one wasted fetch, never corruption.
    """
    url = record.url

    with manifest.lock:
        if manifest.get(url) is not record or record.status != Status.PENDING.value:
            return  # claimed or merged away by another worker already
        if is_excluded(url, exclusions):
            record.status = Status.EXCLUDED.value
            logger.info("excluded %s", url)
            if manifest_save_path is not None:
                manifest.save(manifest_save_path)
            return

    outcome = fetch_with_retries(url, session, rate_limiter, fetch_config)
    logger.info("fetched %s -> %s (%d attempt(s))", url, outcome.http_status, outcome.attempts)

    # Discover links from the fetched content outside the lock -- the
    # BeautifulSoup parse is the one genuinely CPU-costly non-network step
    # here. content_kind/discover_links are pure functions of the fetch
    # result, not the manifest record, so this needs no lock at all.
    discovered_links: list = []
    final_url: str | None = None
    if outcome.category == SUCCESS:
        result = outcome.result
        assert result is not None
        final_url = normalize_url(result.final_url, profile)
        final_host = urlsplit(final_url).hostname or ""
        kind = content_kind(result.content_type, final_url)
        if _should_parse_for_links(kind, final_url, final_host, profile):
            # See _should_parse_for_links for why the two kinds differ.
            discovered_links = discover_links(result.content, final_url, kind)

    with manifest.lock:
        record.fetch_attempts += outcome.attempts
        record.last_fetched = utc_now()
        record.http_status = outcome.http_status

        if outcome.category == SUCCESS:
            assert final_url is not None
            _record_success(record, outcome.result, final_url, manifest, profile, raw_dir)
            for link in discovered_links:
                normalized = admit_link(link, profile)
                if normalized is None:
                    continue
                manifest.get_or_create(normalized, discovered_via=f"crawl:{final_url}")
        elif outcome.category == WAYBACK_CANDIDATE:
            record.status = Status.RETRYING.value
            if outcome.flag:
                record.add_flag(outcome.flag)

        if manifest_save_path is not None:
            manifest.save(manifest_save_path)


def crawl_fixpoint(
    manifest: Manifest,
    profile: SiteProfile,
    session: requests.Session,
    rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
    raw_dir: Path,
    exclusions: list[re.Pattern],
    manifest_save_path: Path | None = None,
    workers: int = 1,
) -> None:
    """Process every pending record, discovering more as links are
    extracted, until no pending records remain. Not a fixed number of
    passes -- a true fixpoint loop.

    If `manifest_save_path` is given, the manifest is saved atomically
    after every processed record. With `workers` concurrent fetchers, an
    interrupted run loses at most the `workers` fetches that were still
    in flight at the moment of interruption -- weaker than the sequential
    "at most one" guarantee, but every save is still a fully consistent,
    atomically-written snapshot.

    Always runs through a ThreadPoolExecutor, even for the default
    `workers=1`, rather than branching to a separate sequential loop, so
    there's exactly one code path to trust regardless of worker count.
    """
    with ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            pending = manifest.by_status(Status.PENDING.value)
            if not pending:
                break
            futures = [
                executor.submit(
                    _process_one,
                    record,
                    manifest,
                    profile,
                    session,
                    rate_limiter,
                    fetch_config,
                    raw_dir,
                    exclusions,
                    manifest_save_path,
                )
                for record in pending
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                # Stop scheduling new work immediately; let anything
                # already running finish (its locked save completes
                # normally) rather than tearing down mid-write.
                executor.shutdown(wait=True, cancel_futures=True)
                raise
