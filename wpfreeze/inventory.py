"""Inventory stage: assemble the authoritative URL set before crawling.

See CLAUDE-acquire.md, "Stage 1 -- Inventory". Parsing logic here is pure
and network-free by design (the doc's test strategy requires the SQL seam
and the sitemap/REST parsing to be unit-testable against fixtures); the
network-orchestrating functions call out to wpfreeze.fetch for the actual
HTTP requests.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests
from lxml import etree

from wpfreeze.fetch import SUCCESS, FetchConfig, RateLimiter, fetch_with_retries
from wpfreeze.manifest import Manifest

logger = logging.getLogger(__name__)

REST_COLLECTIONS = ("posts", "pages", "media", "categories", "tags", "users")

_SITEMAP_ROBOTS_RE = re.compile(r"^\s*sitemap:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# robots.txt / sitemap discovery and parsing
# ---------------------------------------------------------------------------


def parse_robots_sitemaps(robots_txt: str) -> list[str]:
    """Extract every `Sitemap:` directive from a robots.txt body."""
    return [m.group(1) for m in _SITEMAP_ROBOTS_RE.finditer(robots_txt)]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_sitemap_xml(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """Parse a sitemap XML document.

    Returns (page_urls, nested_sitemap_urls): a <urlset> yields page URLs
    with no nested sitemaps; a <sitemapindex> yields nested sitemap URLs
    with no page URLs. Namespace-agnostic (matches by local tag name) since
    WordPress core and plugin sitemaps vary in namespace declarations.
    """
    root = etree.fromstring(xml_bytes)
    root_name = _local_name(root.tag)

    page_urls: list[str] = []
    nested_sitemaps: list[str] = []

    if root_name == "sitemapindex":
        for sitemap_el in root:
            if _local_name(sitemap_el.tag) != "sitemap":
                continue
            for loc_el in sitemap_el:
                if _local_name(loc_el.tag) == "loc" and loc_el.text:
                    nested_sitemaps.append(loc_el.text.strip())
    elif root_name == "urlset":
        for url_el in root:
            if _local_name(url_el.tag) != "url":
                continue
            for loc_el in url_el:
                if _local_name(loc_el.tag) == "loc" and loc_el.text:
                    page_urls.append(loc_el.text.strip())

    return page_urls, nested_sitemaps


# ---------------------------------------------------------------------------
# WordPress REST API pagination
# ---------------------------------------------------------------------------


def rest_collection_url(base_url: str, collection: str, page: int = 1, per_page: int = 100) -> str:
    base_url = base_url.rstrip("/")
    return f"{base_url}/wp-json/wp/v2/{collection}?per_page={per_page}&page={page}"


def parse_rest_total_pages(headers: dict[str, str]) -> int:
    """Read X-WP-TotalPages (case-insensitive header lookup), default 1."""
    for key, value in headers.items():
        if key.lower() == "x-wp-totalpages":
            try:
                return max(1, int(value))
            except ValueError:
                return 1
    return 1


def extract_links_from_rest_items(items: list[dict]) -> list[str]:
    """Pull each item's public permalink out of a WP REST API page of
    results. Items expose their permalink at `link`; user objects may
    instead expose it at `link` too (WP core sets this consistently)."""
    urls: list[str] = []
    for item in items:
        link = item.get("link")
        if link:
            urls.append(link)
    return urls


# ---------------------------------------------------------------------------
# WXR (WordPress eXtended RSS export) inventory
# ---------------------------------------------------------------------------

# XML 1.0's valid character ranges: #x9 | #xA | #xD | [#x20-#xD7FF] |
# [#xE000-#xFFFD] | [#x10000-#x10FFFF]. Real-world WXR exports can carry
# characters outside these ranges (pasted-in Word/PDF content is a common
# source) -- lxml refuses to parse a document containing them at all.
_VALID_XML_CHARS_RE = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")

POST_TYPE_ALLOWLIST = frozenset({"post", "page", "attachment"})


def _strip_invalid_xml_chars(text: str) -> tuple[str, int]:
    """Remove characters outside XML 1.0's valid ranges. lxml's
    `recover=True` "fixes" a document containing them too, but silently
    drops whole malformed subtrees -- unacceptable for a tool whose entire
    point is catching content other inventory sources hide. Sanitizing
    first and parsing strictly instead recovers every item. Returns
    (cleaned_text, count_removed) so callers can log when this actually
    did something."""
    return _VALID_XML_CHARS_RE.subn("", text)


@dataclass(frozen=True)
class WxrAuthor:
    author_id: str
    login: str


@dataclass(frozen=True)
class WxrCategory:
    term_id: str
    nicename: str


@dataclass(frozen=True)
class WxrItem:
    post_id: str
    post_type: str
    status: str
    link: str
    creator: str
    categories: list[tuple[str, str]]  # (domain, nicename)


@dataclass(frozen=True)
class WxrDocument:
    authors: list[WxrAuthor]
    categories: list[WxrCategory]
    items: list[WxrItem]


@dataclass(frozen=True)
class WxrInventoryItem:
    url: str
    discovered_via: str = "xml_backup"


def _child_text(element, local_name: str) -> str:
    for child in element:
        if _local_name(child.tag) == local_name:
            return (child.text or "").strip()
    return ""


def _parse_wxr_author(element) -> WxrAuthor:
    return WxrAuthor(
        author_id=_child_text(element, "author_id"),
        login=_child_text(element, "author_login"),
    )


def _parse_wxr_category(element) -> WxrCategory:
    return WxrCategory(
        term_id=_child_text(element, "term_id"),
        nicename=_child_text(element, "category_nicename"),
    )


def _parse_wxr_item(element) -> WxrItem:
    categories = [
        (child.get("domain", ""), child.get("nicename", ""))
        for child in element
        if _local_name(child.tag) == "category"
    ]
    return WxrItem(
        post_id=_child_text(element, "post_id"),
        post_type=_child_text(element, "post_type"),
        status=_child_text(element, "status"),
        link=_child_text(element, "link"),
        creator=_child_text(element, "creator"),
        categories=categories,
    )


def parse_wxr_xml(xml_bytes: bytes) -> WxrDocument:
    """Parse a WordPress eXtended RSS (WXR) export -- wp-admin's
    Tools > Export. Namespace-agnostic by local tag name (see
    _local_name), the same style as parse_sitemap_xml, plus a
    sanitization pass (see _strip_invalid_xml_chars) before a strict
    parse."""
    text = xml_bytes.decode("utf-8", errors="replace")
    cleaned, stripped_count = _strip_invalid_xml_chars(text)
    if stripped_count:
        logger.warning(
            "stripped %d XML-invalid character(s) from WXR export before parsing",
            stripped_count,
        )
    root = etree.fromstring(cleaned.encode("utf-8"))
    channel = next(child for child in root if _local_name(child.tag) == "channel")

    authors: list[WxrAuthor] = []
    categories: list[WxrCategory] = []
    items: list[WxrItem] = []
    for child in channel:
        name = _local_name(child.tag)
        if name == "author":
            authors.append(_parse_wxr_author(child))
        elif name == "category":
            categories.append(_parse_wxr_category(child))
        elif name == "item":
            items.append(_parse_wxr_item(child))
    return WxrDocument(authors=authors, categories=categories, items=items)


def _is_kept_published(item: WxrItem) -> bool:
    """Type-conditional publish check. Attachments are never
    wp:status=publish in WordPress -- they inherit their parent's status,
    and 'inherit' is their permanent, normal state -- so a uniform
    status=='publish' filter (which is what the DB-based inventory this
    replaces actually did) silently drops every attachment. Do not
    "simplify" this back to a uniform check."""
    if item.post_type not in POST_TYPE_ALLOWLIST:
        return False
    if item.post_type == "attachment":
        return item.status == "inherit"
    return item.status == "publish"


def wxr_post_urls(document: WxrDocument) -> list[WxrInventoryItem]:
    """Allow-listed, kept items' <link> verbatim -- WP's own exporter
    already resolved the fallback-vs-pretty-permalink question, so there
    is nothing to reconstruct here (contrast the old DB path, which had
    to hand-build ?p=/?attachment_id= itself)."""
    return [
        WxrInventoryItem(item.link)
        for item in document.items
        if _is_kept_published(item) and item.link
    ]


def wxr_term_urls(base_url: str, document: WxrDocument) -> list[WxrInventoryItem]:
    """Category assignments need a nicename -> numeric term_id
    cross-reference against the top-level <wp:category> blocks for the
    ?cat={id} fallback; tags and custom taxonomies use their slug
    directly, no id needed."""
    base = base_url.rstrip("/")
    nicename_to_id = {category.nicename: category.term_id for category in document.categories}
    seen: set[str] = set()
    out: list[WxrInventoryItem] = []
    for item in document.items:
        if not _is_kept_published(item):
            continue
        for domain, nicename in item.categories:
            if domain == "category":
                term_id = nicename_to_id.get(nicename)
                if term_id is None:
                    logger.warning(
                        "WXR category %r has no top-level <wp:category> entry; skipping",
                        nicename,
                    )
                    continue
                url = f"{base}/?cat={term_id}"
            elif domain == "post_tag":
                url = f"{base}/?tag={quote(nicename)}"
            else:
                url = f"{base}/?taxonomy={quote(domain)}&term={quote(nicename)}"
            if url not in seen:
                seen.add(url)
                out.append(WxrInventoryItem(url))
    return out


def wxr_author_urls(base_url: str, document: WxrDocument) -> list[WxrInventoryItem]:
    """?author={id}, restricted to authors of at least one kept published
    post/page -- mirrors the old SQL's "authors with published posts"
    scope. Attachments excluded: dc:creator on an attachment is the
    uploader, not a content author in the sense that scope meant."""
    base = base_url.rstrip("/")
    login_to_id = {author.login: author.author_id for author in document.authors}
    seen: set[str] = set()
    out: list[WxrInventoryItem] = []
    for item in document.items:
        if item.post_type not in ("post", "page") or item.status != "publish":
            continue
        author_id = login_to_id.get(item.creator)
        if author_id is None:
            logger.warning(
                "WXR dc:creator %r has no top-level <wp:author> entry; skipping",
                item.creator,
            )
            continue
        if author_id not in seen:
            seen.add(author_id)
            out.append(WxrInventoryItem(f"{base}/?author={author_id}"))
    return out


def discover_wxr(manifest: Manifest, base_url: str, xml_backup_path: Path) -> bool:
    """Seed manifest from a WXR export file. No network -- a local file
    read plus pure XML parsing. Returns whether the file was readable and
    parseable (mirrors discover_sitemaps/discover_rest_api's boolean
    convention for the report's inventory-source-availability line)."""
    try:
        xml_bytes = Path(xml_backup_path).read_bytes()
    except OSError:
        logger.warning("configured xml_backup not readable: %s", xml_backup_path)
        return False
    try:
        document = parse_wxr_xml(xml_bytes)
    except etree.XMLSyntaxError:
        logger.warning("failed to parse xml_backup as WXR: %s", xml_backup_path)
        return False
    for item in wxr_post_urls(document):
        manifest.get_or_create(item.url, discovered_via=item.discovered_via)
    for item in wxr_term_urls(base_url, document):
        manifest.get_or_create(item.url, discovered_via=item.discovered_via)
    for item in wxr_author_urls(base_url, document):
        manifest.get_or_create(item.url, discovered_via=item.discovered_via)
    return True


# ---------------------------------------------------------------------------
# Database inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DbConfig:
    host: str | None = None
    socket: str | None = None
    port: int | None = None
    name: str = ""
    user: str = ""
    password: str | None = None
    table_prefix: str = "wp_"


@dataclass(frozen=True)
class DbInventoryItem:
    url: str
    discovered_via: str = "database"


def run_mysql_query(db_config: DbConfig, sql: str, binary: str = "mysql") -> str:
    """Execute `sql` via the mysql/mariadb client and return its raw
    --batch --raw tab-separated output.

    This is the sole subprocess seam -- kept tiny and isolated so tests can
    exercise the parsing/query-building logic against fixtured output
    without a live database. Credentials never touch the command line: a
    temporary defaults file is written (mode 0600) and passed via
    --defaults-extra-file, then removed.
    """
    defaults_lines = ["[client]"]
    if db_config.user:
        defaults_lines.append(f"user={db_config.user}")
    if db_config.password is not None:
        defaults_lines.append(f"password={db_config.password}")
    if db_config.host:
        defaults_lines.append(f"host={db_config.host}")
    if db_config.socket:
        defaults_lines.append(f"socket={db_config.socket}")
    if db_config.port:
        defaults_lines.append(f"port={db_config.port}")

    fd, tmp_name = tempfile.mkstemp(prefix="wpfreeze-db-", suffix=".cnf")
    tmp_path = Path(tmp_name)
    try:
        tmp_path.chmod(0o600)
        tmp_path.write_text("\n".join(defaults_lines) + "\n", encoding="utf-8")
        result = subprocess.run(
            [binary, f"--defaults-extra-file={tmp_path}", "--batch", "--raw", db_config.name],
            input=sql,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    finally:
        tmp_path.unlink(missing_ok=True)


def parse_batch_output(raw: str) -> list[list[str]]:
    """Parse `mysql --batch --raw` tab-separated output into rows,
    skipping the header line."""
    lines = raw.splitlines()
    if not lines:
        return []
    return [line.split("\t") for line in lines[1:] if line]


def build_post_urls(base_url: str, rows: list[list[str]]) -> list[DbInventoryItem]:
    """rows: [(id, post_type)] for published posts/pages/attachments.
    Attachments get ?attachment_id=, everything else gets ?p=."""
    base_url = base_url.rstrip("/")
    items = []
    for row in rows:
        post_id, post_type = row[0], row[1]
        if post_type == "attachment":
            items.append(DbInventoryItem(f"{base_url}/?attachment_id={post_id}"))
        else:
            items.append(DbInventoryItem(f"{base_url}/?p={post_id}"))
    return items


def build_term_urls(base_url: str, rows: list[list[str]]) -> list[DbInventoryItem]:
    """rows: [(term_id, slug, taxonomy)]."""
    base_url = base_url.rstrip("/")
    items = []
    for row in rows:
        term_id, slug, taxonomy = row[0], row[1], row[2]
        if taxonomy == "category":
            items.append(DbInventoryItem(f"{base_url}/?cat={term_id}"))
        elif taxonomy == "post_tag":
            items.append(DbInventoryItem(f"{base_url}/?tag={quote(slug)}"))
        else:
            items.append(
                DbInventoryItem(f"{base_url}/?taxonomy={quote(taxonomy)}&term={quote(slug)}")
            )
    return items


def build_author_urls(base_url: str, rows: list[list[str]]) -> list[DbInventoryItem]:
    """rows: [(user_id,)] for authors with published posts."""
    base_url = base_url.rstrip("/")
    return [DbInventoryItem(f"{base_url}/?author={row[0]}") for row in rows]


POSTS_SQL_TEMPLATE = (
    "SELECT ID, post_type FROM {prefix}posts WHERE post_status = 'publish';"
)
TERMS_SQL_TEMPLATE = (
    "SELECT t.term_id, t.slug, tt.taxonomy FROM {prefix}terms t "
    "JOIN {prefix}term_taxonomy tt ON tt.term_id = t.term_id "
    "WHERE tt.count > 0;"
)
AUTHORS_SQL_TEMPLATE = (
    "SELECT DISTINCT u.ID FROM {prefix}users u "
    "JOIN {prefix}posts p ON p.post_author = u.ID "
    "WHERE p.post_status = 'publish';"
)


def build_inventory_queries(table_prefix: str) -> dict[str, str]:
    return {
        "posts": POSTS_SQL_TEMPLATE.format(prefix=table_prefix),
        "terms": TERMS_SQL_TEMPLATE.format(prefix=table_prefix),
        "authors": AUTHORS_SQL_TEMPLATE.format(prefix=table_prefix),
    }


# ---------------------------------------------------------------------------
# Network orchestration: seed a Manifest from every configured source
# ---------------------------------------------------------------------------


def discover_sitemaps(
    manifest: Manifest,
    base_url: str,
    session: requests.Session,
    rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
) -> bool:
    """Seed `manifest` from robots.txt's Sitemap: pointers plus the
    conventional wp-sitemap.xml/sitemap_index.xml locations, recursing
    through sitemap indexes to a fixpoint. Returns whether any sitemap was
    reachable at all (for the report's inventory-source-availability line).
    """
    base = base_url.rstrip("/")
    to_visit: list[str] = []

    robots_outcome = fetch_with_retries(f"{base}/robots.txt", session, rate_limiter, fetch_config)
    if robots_outcome.category == SUCCESS:
        robots_text = robots_outcome.result.content.decode("utf-8", errors="replace")
        to_visit.extend(parse_robots_sitemaps(robots_text))
    to_visit.append(f"{base}/wp-sitemap.xml")
    to_visit.append(f"{base}/sitemap_index.xml")

    visited: set[str] = set()
    found_any = False
    while to_visit:
        sitemap_url = to_visit.pop()
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        outcome = fetch_with_retries(sitemap_url, session, rate_limiter, fetch_config)
        if outcome.category != SUCCESS:
            logger.info("sitemap unavailable: %s", sitemap_url)
            continue
        try:
            pages, nested = parse_sitemap_xml(outcome.result.content)
        except Exception:
            logger.warning("failed to parse sitemap XML: %s", sitemap_url)
            continue
        found_any = True
        for page_url in pages:
            manifest.get_or_create(page_url, discovered_via="sitemap")
        for nested_url in nested:
            if nested_url not in visited:
                to_visit.append(nested_url)
    return found_any


def discover_rest_api(
    manifest: Manifest,
    base_url: str,
    session: requests.Session,
    rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
) -> bool:
    """Seed `manifest` from every WP REST API collection, paginated to
    exhaustion. Degrades gracefully per collection: an unavailable or
    blocked endpoint is logged and skipped, not a hard failure. Returns
    whether any collection was reachable."""
    found_any = False
    for collection in REST_COLLECTIONS:
        page = 1
        total_pages = 1
        while page <= total_pages:
            url = rest_collection_url(base_url, collection, page=page)
            outcome = fetch_with_retries(url, session, rate_limiter, fetch_config)
            if outcome.category != SUCCESS:
                logger.info("REST API collection unavailable: %s", collection)
                break
            total_pages = parse_rest_total_pages(outcome.result.headers)
            try:
                items = json.loads(outcome.result.content)
            except ValueError:
                logger.warning("failed to parse REST API response: %s", url)
                break
            found_any = True
            for item_url in extract_links_from_rest_items(items):
                manifest.get_or_create(item_url, discovered_via="rest_api")
            page += 1
    return found_any


def discover_database(manifest: Manifest, base_url: str, db_config: DbConfig) -> None:
    """Seed `manifest` from the database inventory: published posts/pages/
    attachments, non-empty terms, and authors with published posts --
    entered as their query-string permalink fallback (see
    CLAUDE-acquire.md Stage 1) for redirect-following to resolve."""
    queries = build_inventory_queries(db_config.table_prefix)

    posts_raw = run_mysql_query(db_config, queries["posts"])
    for item in build_post_urls(base_url, parse_batch_output(posts_raw)):
        manifest.get_or_create(item.url, discovered_via=item.discovered_via)

    terms_raw = run_mysql_query(db_config, queries["terms"])
    for item in build_term_urls(base_url, parse_batch_output(terms_raw)):
        manifest.get_or_create(item.url, discovered_via=item.discovered_via)

    authors_raw = run_mysql_query(db_config, queries["authors"])
    for item in build_author_urls(base_url, parse_batch_output(authors_raw)):
        manifest.get_or_create(item.url, discovered_via=item.discovered_via)


def discover_inventory(
    manifest: Manifest,
    base_url: str,
    db_config: DbConfig | None,
    session: requests.Session,
    rate_limiter: RateLimiter,
    fetch_config: FetchConfig,
) -> dict[str, bool]:
    """Stage 1 top level: seed `manifest` from every configured inventory
    source. Returns which sources were reachable, for the report."""
    sitemap_ok = discover_sitemaps(manifest, base_url, session, rate_limiter, fetch_config)
    rest_ok = discover_rest_api(manifest, base_url, session, rate_limiter, fetch_config)
    if db_config is not None:
        discover_database(manifest, base_url, db_config)
    return {"sitemap": sitemap_ok, "rest_api": rest_ok, "database": db_config is not None}
