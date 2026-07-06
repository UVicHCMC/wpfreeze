"""Inventory stage: assemble the authoritative URL set before crawling.

See CLAUDE-acquire.md, "Stage 1 -- Inventory". Parsing logic here is pure
and network-free by design (the doc's test strategy requires the SQL seam
and the sitemap/REST parsing to be unit-testable against fixtures); the
network-orchestrating functions call out to wpfreeze.fetch for the actual
HTTP requests.
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from lxml import etree

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
