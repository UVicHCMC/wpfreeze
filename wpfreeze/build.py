"""Module 2: turn an acquired capture into a servable static site.

Acquisition saves bytes exactly as fetched and records, per resource, the
output path it *should* occupy -- but nothing rewrites the markup, so every
link in `raw/` still points at the live site. This module closes that gap:
it reads manifest.json, rewrites every internal reference to a relative
path, and emits a tree that works over plain HTTP, from a subdirectory, or
straight off the filesystem.

Two things here are less obvious than they look.

**Lookup has to be tiered.** WordPress markup asks for `style.css?ver=6.4`,
`script.js?m=1720530689i`, `x.css?cssminify=yes` -- cache-busters
addressing bytes the manifest holds under one canonical URL. Matching on
the exact query resolves under a third of a real site's references;
falling back through "permalink keys only" and then "no query" takes the
same capture past two thirds. The tiers deliberately mirror
urlnorm.PERMALINK_QUERY_KEYS rather than inventing a second rule, because
divergence between what acquisition canonicalized and what the rewriter
looks up is exactly how references get silently dropped.

**Links are emitted relative, not root-relative.** Root-relative is easier
to compute but only works when the site is served from a domain root;
relative paths survive file:// browsing and hosting under a subdirectory,
which is most of the point of a durable offline copy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from posixpath import relpath as posix_relpath
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from wpfreeze.manifest import Manifest, Status
from wpfreeze.urlnorm import PERMALINK_QUERY_KEYS

logger = logging.getLogger(__name__)

_FETCHED_STATUSES = frozenset({Status.FETCHED.value, Status.FETCHED_WAYBACK.value})

# Values that are not references to fetchable resources at all.
_SKIP_PREFIXES = ("data:", "mailto:", "tel:", "javascript:", "sms:", "about:", "#")

# The same attribute surface extract.py discovers over, in editable form.
_URL_ATTRS: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "area": ("href",),
    "form": ("action",),
    "blockquote": ("cite",),
    "q": ("cite",),
    "ins": ("cite",),
    "del": ("cite",),
    "img": ("src", "longdesc"),
    "source": ("src",),
    "script": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "audio": ("src",),
    "video": ("src", "poster"),
    "track": ("src",),
    "object": ("data",),
    "link": ("href",),
    "input": ("src",),
}
_SRCSET_TAGS = ("img", "source")

_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)

_HTML_TYPES = ("text/html", "application/xhtml+xml")


@dataclass
class BuildStats:
    """Counts over every reference examined, for the build summary."""

    rewritten: int = 0
    left_absolute: int = 0
    unresolved: int = 0
    skipped: int = 0
    pages: int = 0
    assets: int = 0
    unresolved_samples: list[tuple[str, str]] = field(default_factory=list)

    @property
    def considered(self) -> int:
        return self.rewritten + self.left_absolute + self.unresolved


def lookup_variants(url: str) -> list[str]:
    """Every spelling of `url` worth trying against the lookup, most
    specific first.

    Over-generates deliberately: the map only ever contains real keys, so a
    spelling that never occurs simply misses. Under-generating is the
    expensive direction -- each missed spelling is a reference left pointing
    at a site that may not exist much longer.
    """
    parts = urlsplit(url)
    if not parts.netloc:
        return [url]

    # Ordered, not set-based: the URL's own spelling must be tried before
    # any generated alternative, or an exact match can lose to a variant
    # that happens to hash first.
    hosts = [parts.netloc]
    hosts.append(parts.netloc[4:] if parts.netloc.startswith("www.") else "www." + parts.netloc)

    path = parts.path or "/"
    paths = [path]
    if path.endswith("/"):
        if len(path) > 1:
            paths.append(path.rstrip("/"))
    elif "." not in path.rsplit("/", 1)[-1]:
        paths.append(path + "/")

    schemes = [parts.scheme] if parts.scheme in ("http", "https") else []
    schemes += [s for s in ("https", "http") if s not in schemes]

    queries = [parts.query]
    if parts.query:
        kept = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k in PERMALINK_QUERY_KEYS
            ]
        )
        queries.append(kept)
        if kept:
            queries.append("")

    out: list[str] = []
    for query in dict.fromkeys(queries):
        for scheme in schemes:
            for host in dict.fromkeys(hosts):
                for candidate in dict.fromkeys(paths):
                    out.append(urlunsplit((scheme, host, candidate, query, "")))
    return list(dict.fromkeys(out))


def build_lookup(manifest: Manifest) -> dict[str, str]:
    """Map every spelling of every fetched resource to its output_path.

    Only successfully fetched records are included: a reference to
    something the capture never got should stay visibly broken rather than
    silently point at a file that isn't there.
    """
    lookup: dict[str, str] = {}

    def add(url: str, output_path: str) -> None:
        for variant in lookup_variants(url):
            lookup.setdefault(variant, output_path)

    for record in manifest.all():
        if record.status not in _FETCHED_STATUSES or not record.output_path:
            continue
        add(record.url, record.output_path)
        for alias in record.aliases:
            add(alias, record.output_path)
    return lookup


def relative_link(from_output_path: str, to_output_path: str) -> str:
    """Relative href from one output_path to another; both are /-rooted."""
    from_dir = from_output_path.rsplit("/", 1)[0] or "/"
    return posix_relpath(to_output_path.lstrip("/"), from_dir.lstrip("/") or ".")


class LinkRewriter:
    """Rewrites references in one document at a time, against a prebuilt
    lookup. Stateless per document apart from the shared stats counter."""

    def __init__(self, lookup: dict[str, str], stats: BuildStats):
        self.lookup = lookup
        self.stats = stats

    def resolve(self, value: str, page_url: str, page_output: str) -> str | None:
        """Return the relative replacement for `value`, or None to leave it
        exactly as it was."""
        raw = value.strip()
        if not raw or raw.lower().startswith(_SKIP_PREFIXES):
            self.stats.skipped += 1
            return None

        fragment = ""
        if "#" in raw:
            raw, _, frag = raw.partition("#")
            fragment = "#" + frag
            if not raw:  # a pure in-page anchor
                self.stats.skipped += 1
                return None

        try:
            absolute = urljoin(page_url, raw)
        except ValueError:
            self.stats.unresolved += 1
            return None

        for candidate in lookup_variants(absolute):
            target = self.lookup.get(candidate)
            if target is not None:
                self.stats.rewritten += 1
                return relative_link(page_output, target) + fragment

        if _different_host(absolute, page_url):
            self.stats.left_absolute += 1
        else:
            self.stats.unresolved += 1
            if len(self.stats.unresolved_samples) < 50:
                self.stats.unresolved_samples.append((page_url, value[:120]))
        return None

    def rewrite_srcset(self, value: str, page_url: str, page_output: str) -> str:
        out = []
        for entry in value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(None, 1)
            replacement = self.resolve(parts[0], page_url, page_output)
            url = replacement if replacement else parts[0]
            out.append(url + (" " + parts[1] if len(parts) > 1 else ""))
        return ", ".join(out)

    def rewrite_css(self, text: str, page_url: str, page_output: str) -> str:
        def substitute(match: re.Match) -> str:
            quote, url = match.group(1), match.group(2)
            replacement = self.resolve(url, page_url, page_output)
            return f"url({quote}{replacement or url}{quote})"

        return _CSS_URL_RE.sub(substitute, text)

    def rewrite_html(self, html: str, page_url: str, page_output: str) -> str:
        soup = BeautifulSoup(html, "lxml")

        for tag_name, attrs in _URL_ATTRS.items():
            for tag in soup.find_all(tag_name):
                for attr in attrs:
                    value = tag.get(attr)
                    if value:
                        replacement = self.resolve(value, page_url, page_output)
                        if replacement:
                            tag[attr] = replacement

        for tag_name in _SRCSET_TAGS:
            for tag in soup.find_all(tag_name):
                if tag.get("srcset"):
                    tag["srcset"] = self.rewrite_srcset(tag["srcset"], page_url, page_output)

        for tag in soup.find_all(style=True):
            tag["style"] = self.rewrite_css(tag["style"], page_url, page_output)

        for tag in soup.find_all("style"):
            if tag.string:
                tag.string = self.rewrite_css(tag.string, page_url, page_output)

        return str(soup)


def _different_host(absolute: str, page_url: str) -> bool:
    def bare(host: str) -> str:
        return host[4:] if host.startswith("www.") else host

    host = urlsplit(absolute).netloc
    return bool(host) and bare(host) != bare(urlsplit(page_url).netloc)


def build_site(manifest: Manifest, output_dir: Path, site_dir: Path) -> BuildStats:
    """Emit the rewritten site under `site_dir`.

    Non-destructive with respect to `raw/`: every document is read from the
    capture and written to a separate tree, so a build can be re-run as
    often as needed without ever touching the acquired bytes.
    """
    lookup = build_lookup(manifest)
    stats = BuildStats()
    rewriter = LinkRewriter(lookup, stats)
    logger.info("build: %d lookup keys", len(lookup))

    for record in manifest.all():
        if record.status not in _FETCHED_STATUSES or not record.output_path:
            continue
        source = output_dir / record.local_path if record.local_path else None
        if source is None or not source.exists():
            logger.warning("build: missing file on disk for %s", record.url)
            continue

        content_type = (record.content_type or "").split(";")[0].strip().lower()
        is_html = content_type in _HTML_TYPES
        is_css = content_type == "text/css" or source.suffix.lower() == ".css"

        destination = site_dir / record.output_path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)

        if is_html:
            stats.pages += 1
            text = source.read_text(encoding="utf-8", errors="replace")
            destination.write_text(
                rewriter.rewrite_html(text, record.url, record.output_path),
                encoding="utf-8",
            )
        elif is_css:
            stats.assets += 1
            text = source.read_text(encoding="utf-8", errors="replace")
            destination.write_text(
                rewriter.rewrite_css(text, record.url, record.output_path),
                encoding="utf-8",
            )
        else:
            stats.assets += 1
            destination.write_bytes(source.read_bytes())

    return stats


def format_build_summary(stats: BuildStats) -> str:
    considered = stats.considered or 1
    lines = [
        "Build:",
        f"  {stats.pages} page(s), {stats.assets} asset(s) written",
        f"  references rewritten to local : {stats.rewritten} ({stats.rewritten / considered:.1%})",
        f"  left absolute (external)      : {stats.left_absolute} ({stats.left_absolute / considered:.1%})",
        f"  unresolved                    : {stats.unresolved} ({stats.unresolved / considered:.1%})",
    ]
    if stats.unresolved:
        lines.append("  (unresolved references are left pointing at the original site)")
    return "\n".join(lines)
