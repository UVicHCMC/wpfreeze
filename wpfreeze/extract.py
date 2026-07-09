"""Link extraction: pull every URL a page renders or links to out of HTML/CSS.

See CLAUDE-acquire.md, "Link extraction", for the attribute/context list
and the render-vs-hyperlink distinction this module encodes: src-like
contexts, CSS urls, preloads, and og:image are renderable (localized even
if external); href on <a> is a hyperlink (followed only if internal).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from wpfreeze.urlnorm import resolve_url

logger = logging.getLogger(__name__)

HYPERLINK = "hyperlink"
RENDER = "render"

_SKIPPED_SCHEMES = ("data:", "mailto:", "tel:", "javascript:", "#")

# URL-shaped heuristics used for data-* attributes and <script> bodies,
# where a value may be a bare URL, or a URL buried in a JS/JSON blob. The
# character class excludes *{} on top of the obvious delimiters: real URL
# paths never contain a literal glob-wildcard character, but cache/PWA
# plugins routinely embed exclusion-glob arrays (e.g.
# '/wp-content/uploads/*/') in a sitewide inline <script> block -- without
# this exclusion those get matched whole and queued as if they were real
# pages, wasting a live-fetch + Wayback-lookup cycle on each one (seen on
# a real crawl, though it degrades to a one-time cost, not a hang).
_ABSOLUTE_OR_PROTOCOL_RELATIVE_RE = re.compile(r"(?:https?:)?//[^\s\"'<>\\*{}]+", re.IGNORECASE)
_WP_PATH_RE = re.compile(r"/wp-(?:content|includes)/[^\s\"'<>\\*{}]+", re.IGNORECASE)
_ASSET_EXT_RE = re.compile(
    r"/[^\s\"'<>\\*{}]+\.(?:jpe?g|png|gif|webp|svg|bmp|ico|mp4|webm|mp3|pdf|css|js|woff2?|ttf|eot)"
    r"(?:\?[^\s\"'<>\\*{}]*)?",
    re.IGNORECASE,
)
_URL_SHAPED_PATTERNS = (_ABSOLUTE_OR_PROTOCOL_RELATIVE_RE, _WP_PATH_RE, _ASSET_EXT_RE)

_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT_PLAIN_RE = re.compile(r"""@import\s+["']([^"']+)["']""", re.IGNORECASE)

# tag -> attributes that are hyperlinks (internal-only navigation targets).
_HYPERLINK_ATTRS = {
    "a": ("href",),
    "form": ("action",),
    "blockquote": ("cite",),
    "q": ("cite",),
    "ins": ("cite",),
    "del": ("cite",),
}

# tag -> attributes that are renderable resources (localize even if external).
_RENDER_ATTRS = {
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "script": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "audio": ("src",),
    "video": ("src", "poster"),
    "track": ("src",),
    "object": ("data",),
}

_LINK_REL_RENDER = {"icon", "apple-touch-icon", "preload", "stylesheet"}
_LINK_REL_HYPERLINK = {"canonical", "next", "prev"}

_META_RENDER_PROPERTIES = {"og:image", "twitter:image"}
_META_HYPERLINK_PROPERTIES = {"og:url"}


@dataclass(frozen=True)
class ExtractedLink:
    url: str
    kind: str  # HYPERLINK or RENDER
    context: str  # e.g. "a[href]", "img[srcset]", "script:json" -- for debugging/tests


def _is_url_shaped(value: str) -> bool:
    """True if `value`, taken as a whole, looks like a URL (as opposed to
    a URL merely appearing somewhere inside a larger string)."""
    value = value.strip()
    return value.startswith(("http://", "https://", "//", "/"))


def _find_url_shaped_strings(text: str) -> list[str]:
    """Best-effort URL scan over text that isn't valid JSON on its own
    (a non-JSON <script> body, or a data-* attribute holding a JS object
    literal -- Elementor's inline config is the common source of both).

    JSON string escaping writes "/" as "\\/" -- always valid JSON, and in
    a JS string literal it's merely a superfluous escaped "/", so
    unescaping it first is safe either way. Skipping this step breaks
    the regexes below: their character class excludes backslash, so each
    "\\/" mid-path is a hard stop and the match re-anchors at the next
    "/", silently dropping everything before it (e.g. a real
    ".../wp-content/uploads/2023/09/photo.jpg" collapses to just
    "/photo.jpg", which then resolves against the page URL as a
    domain-root URL that never existed).
    """
    text = text.replace("\\/", "/")
    found: list[str] = []
    for pattern in _URL_SHAPED_PATTERNS:
        found.extend(m.group(0) for m in pattern.finditer(text))
    return found


def _walk_json_for_urls(obj) -> list[str]:
    urls: list[str] = []
    if isinstance(obj, str):
        if _is_url_shaped(obj):
            urls.append(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            urls.extend(_walk_json_for_urls(value))
    elif isinstance(obj, list):
        for value in obj:
            urls.extend(_walk_json_for_urls(value))
    return urls


def _parse_srcset(value: str) -> list[str]:
    """Parse an HTML srcset attribute into its candidate URLs, ignoring
    width/density descriptors."""
    urls: list[str] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split()
        if parts:
            urls.append(parts[0])
    return urls


def extract_from_css(css_text: str, base_url: str) -> list[ExtractedLink]:
    """Extract url(...) and @import targets from CSS text (a <style> block,
    an inline style attribute, or a fetched .css file)."""
    links: list[ExtractedLink] = []
    for match in _CSS_URL_RE.finditer(css_text):
        raw = match.group(2).strip()
        if raw and not raw.startswith("data:"):
            resolved = resolve_url(base_url, raw)
            if resolved is not None:
                links.append(ExtractedLink(resolved, RENDER, "css:url()"))
    for match in _CSS_IMPORT_PLAIN_RE.finditer(css_text):
        raw = match.group(1).strip()
        if raw:
            resolved = resolve_url(base_url, raw)
            if resolved is not None:
                links.append(ExtractedLink(resolved, RENDER, "css:@import"))
    return links


def extract_from_html(html: str, base_url: str) -> list[ExtractedLink]:
    """Extract every URL an HTML page links to or renders, resolved against
    base_url and classified as HYPERLINK or RENDER."""
    soup = BeautifulSoup(html, "lxml")
    links: list[ExtractedLink] = []

    def add(raw: str, kind: str, context: str) -> None:
        raw = (raw or "").strip()
        if raw and not raw.startswith(_SKIPPED_SCHEMES):
            resolved = resolve_url(base_url, raw)
            if resolved is not None:
                links.append(ExtractedLink(resolved, kind, context))

    for tag in soup.find_all(True):
        name = tag.name

        for attr in _HYPERLINK_ATTRS.get(name, ()):
            value = tag.get(attr)
            if value:
                add(value, HYPERLINK, f"{name}[{attr}]")

        for attr in _RENDER_ATTRS.get(name, ()):
            value = tag.get(attr)
            if not value:
                continue
            if attr == "srcset":
                for url in _parse_srcset(value):
                    add(url, RENDER, f"{name}[srcset]")
            else:
                add(value, RENDER, f"{name}[{attr}]")

        if name == "link":
            rel = tag.get("rel") or []
            if isinstance(rel, str):
                rel = rel.split()
            rel_set = {r.lower() for r in rel}
            href = tag.get("href")
            if href:
                label = "|".join(sorted(rel_set)) or "?"
                if rel_set & _LINK_REL_RENDER:
                    add(href, RENDER, f"link[rel={label}]")
                elif rel_set & _LINK_REL_HYPERLINK:
                    add(href, HYPERLINK, f"link[rel={label}]")

        if name == "meta":
            prop = (tag.get("property") or tag.get("name") or "").lower()
            content = tag.get("content")
            if content:
                if prop in _META_RENDER_PROPERTIES:
                    add(content, RENDER, f"meta[{prop}]")
                elif prop in _META_HYPERLINK_PROPERTIES:
                    add(content, HYPERLINK, f"meta[{prop}]")

        style_attr = tag.get("style")
        if style_attr:
            for match in _CSS_URL_RE.finditer(style_attr):
                add(match.group(2), RENDER, f"{name}[style]")

        if name == "style" and tag.string:
            for css_link in extract_from_css(tag.string, base_url):
                links.append(ExtractedLink(css_link.url, css_link.kind, f"style:{css_link.context}"))

        if name == "script":
            script_text = tag.string or ""
            script_type = (tag.get("type") or "").lower()
            handled_as_json = False
            if not script_type or script_type in ("application/json", "application/ld+json"):
                try:
                    data = json.loads(script_text)
                except (ValueError, TypeError):
                    pass
                else:
                    handled_as_json = True
                    for url in _walk_json_for_urls(data):
                        add(url, RENDER, "script:json")
            if not handled_as_json and script_text:
                for url in _find_url_shaped_strings(script_text):
                    add(url, RENDER, "script:regex")

        # data-* attributes: URL-shaped values only (this is where gallery
        # and lazy-load plugins hide their images). A data-*srcset*
        # attribute (data-srcset, data-lazy-srcset, ...) holds the same
        # comma-separated "url descriptor" list as the real srcset
        # attribute, not a single URL -- must go through the same
        # splitter, or the whole blob (commas, width descriptors, and
        # all its candidate URLs concatenated) gets queued as one
        # "URL" and mangled further by normalize_url's slash collapsing.
        for attr_name, attr_value in tag.attrs.items():
            if not attr_name.startswith("data-") or not isinstance(attr_value, str):
                continue
            if "srcset" in attr_name.lower():
                for url in _parse_srcset(attr_value):
                    add(url, RENDER, f"{name}[{attr_name}]")
            elif _is_url_shaped(attr_value):
                add(attr_value.strip(), RENDER, f"{name}[{attr_name}]")
            else:
                for url in _find_url_shaped_strings(attr_value):
                    add(url, RENDER, f"{name}[{attr_name}]")

    return links
