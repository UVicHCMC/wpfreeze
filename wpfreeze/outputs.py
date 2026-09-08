"""Stage 6 -- output path mapping and redirects.htaccess generation.
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from wpfreeze.manifest import FLAG_ATTACHMENT_PAGE, Manifest, ManifestRecord, Status
from wpfreeze.urlnorm import SiteProfile

logger = logging.getLogger(__name__)

_FETCHED_STATUSES = {Status.FETCHED.value, Status.FETCHED_WAYBACK.value}

_PAGINATION_RE = re.compile(r"^(?P<base>.*)/page/(?P<num>\d+)$")

_FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
_CSS_EXTS = {".css"}
_JS_EXTS = {".js", ".mjs"}
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico"}


class OutputPathCollisionError(Exception):
    """Two internal resources mapped to the same output_path. Should be
    impossible under this scheme -- surfaced loudly rather than letting
    one silently overwrite the other."""


def internal_output_path(url_path: str) -> str:
    """Map an internal URL path to its intended output file path.

    /foo/bar/ -> /foo/bar.html; / -> /index.html; archive pagination
    /category/essays/page/2/ -> /category/essays/page-2.html; anything
    already file-like (a dot in the final segment) keeps its path
    unchanged. Directory-likeness is judged the same way regardless of
    whether the URL happens to carry a trailing slash -- the site's own
    trailing-slash preference (probed into SiteProfile) must not change
    Module 1's own output convention, so a no-trailing-slash site's
    "/foo/bar" still becomes "/foo/bar.html", not "/foo/bar" verbatim.
    """
    stripped = url_path.rstrip("/")
    if stripped == "":
        return "/index.html"

    pagination_match = _PAGINATION_RE.match(stripped)
    if pagination_match:
        return f"{pagination_match.group('base')}/page-{pagination_match.group('num')}.html"

    last_segment = stripped.rsplit("/", 1)[-1]
    if "." in last_segment:
        return url_path  # already file-like; assets keep their exact path

    return stripped + ".html"


def external_asset_bucket(filename: str) -> str | None:
    """Well-known local folder for a renderable external asset, by
    extension; None means "no known bucket, use the host-namespaced
    catch-all"."""
    ext = PurePosixPath(filename).suffix.lower()
    if ext in _FONT_EXTS:
        return "/assets/fonts/"
    if ext in _CSS_EXTS:
        return "/assets/css/external/"
    if ext in _JS_EXTS:
        return "/assets/js/external/"
    if ext in _IMG_EXTS:
        return "/assets/img/external/"
    return None


def external_output_path(url: str, content_hash: str, disambiguate: bool = False) -> str:
    """Bucket an external renderable asset by type. On a same-bucket
    filename collision between two different hosts, `disambiguate=True`
    appends a short content_hash prefix rather than colliding silently."""
    parsed = urlsplit(url)
    host = parsed.hostname or "unknown-host"
    filename = PurePosixPath(parsed.path).name or "index"

    if disambiguate:
        stem = PurePosixPath(filename).stem
        suffix = PurePosixPath(filename).suffix
        filename = f"{stem}-{content_hash[:8]}{suffix}"

    bucket = external_asset_bucket(filename)
    if bucket is not None:
        return bucket + filename
    return f"/assets/external/{host}/{filename}"


def _assign_output_path(
    record: ManifestRecord,
    profile: SiteProfile,
    internal_seen: dict[str, str],
    external_seen: dict[str, str],
) -> None:
    if FLAG_ATTACHMENT_PAGE in record.flags:
        record.output_path = None
        return

    host = (urlsplit(record.url).hostname or "").lower()
    if profile.owns_host(host):
        url_path = urlsplit(record.url).path or "/"
        output_path = internal_output_path(url_path)
        existing = internal_seen.get(output_path)
        if existing is not None and existing != record.url:
            raise OutputPathCollisionError(f"{record.url} and {existing} both map to {output_path}")
        internal_seen[output_path] = record.url
        record.output_path = output_path
        return

    candidate = external_output_path(record.url, record.content_hash or "")
    if candidate in external_seen and external_seen[candidate] != record.url:
        candidate = external_output_path(record.url, record.content_hash or "", disambiguate=True)
    external_seen[candidate] = record.url
    record.output_path = candidate


def compute_output_paths(
    manifest: Manifest,
    profile: SiteProfile,
    hash_duplicate_canonical: dict[str, str],
) -> None:
    """Populate output_path for every fetched record. Attachment pages get
    None (Module 2 rewrites inbound links to the media file directly).
    Hash-duplicate-group members (see analyse.flag_hash_duplicates) take
    their canonical member's output_path rather than computing their own.
    """
    records = [r for r in manifest.all() if r.status in _FETCHED_STATUSES]
    canonicals = [r for r in records if r.url not in hash_duplicate_canonical]
    dupes = [r for r in records if r.url in hash_duplicate_canonical]

    internal_seen: dict[str, str] = {}
    external_seen: dict[str, str] = {}

    for record in canonicals:
        _assign_output_path(record, profile, internal_seen, external_seen)

    for record in dupes:
        canonical_record = manifest.get(hash_duplicate_canonical[record.url])
        if canonical_record is not None and canonical_record.output_path is not None:
            record.output_path = canonical_record.output_path
        else:
            _assign_output_path(record, profile, internal_seen, external_seen)


def generate_redirects_htaccess(manifest: Manifest) -> str:
    """Mechanical directory->file RewriteRules first, then explicit
    Redirect 301 lines for aliases/redirect origins/?p=-style permalinks."""
    lines = [
        "# wpfreeze generated redirect map",
        "",
        "# --- Mechanical rules: directory request -> explicit output file ---",
        "RewriteEngine On",
        "RewriteCond %{REQUEST_FILENAME} !-f",
        r"RewriteRule ^(.*)/$ /$1.html [L]",
        "",
        "# --- Explicit redirects: aliases and legacy permalinks ---",
    ]

    for record in sorted(manifest.all(), key=lambda r: r.url):
        if record.status not in _FETCHED_STATUSES or not record.output_path:
            continue
        canonical_path = urlsplit(record.url).path or "/"
        origins = sorted(set(record.aliases) | set(record.redirect_from))
        for origin in origins:
            origin_path = urlsplit(origin).path or "/"
            origin_query = urlsplit(origin).query
            request_path = origin_path if not origin_query else f"{origin_path}?{origin_query}"
            if origin_path == canonical_path and not origin_query:
                continue
            lines.append(f"Redirect 301 {request_path} {record.output_path}")

    return "\n".join(lines) + "\n"
