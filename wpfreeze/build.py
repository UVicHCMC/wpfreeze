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

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from posixpath import relpath as posix_relpath
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from wpfreeze.extract import decode_static_bundle
from wpfreeze.manifest import FLAG_ATTACHMENT_PAGE, Manifest, ManifestRecord, Status
from wpfreeze.normalize import NormalizeStats, apply_normalizations
from wpfreeze.policy import Policy, PolicyStats, apply_policy
from wpfreeze.urlnorm import PERMALINK_QUERY_KEYS, scope_profile_from_config

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
    bundles_reassembled: int = 0
    bundle_components_missing: int = 0
    attachment_links_retargeted: int = 0
    redirects_copied: bool = False
    policy: PolicyStats = field(default_factory=PolicyStats)
    normalize: NormalizeStats = field(default_factory=NormalizeStats)
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
        # Only ever falls back to the bare (query-less) spelling when kept
        # is itself empty -- i.e. every param was cache-buster/tracking
        # noise. If a permalink-identity key (cat=/author=/p=/...) survived
        # into `kept`, the bare form must NOT be offered: it would resolve
        # this archive/post to whatever unrelated record happens to already
        # occupy that bare path (typically the homepage), not merely leave
        # it unresolved. A real crawl hit this exactly: an author-archive
        # page's `?author=6` alias was silently claiming the site root's
        # lookup entry, sending every "Home" nav link to the wrong page.
        queries.append(kept)

    out: list[str] = []
    for query in dict.fromkeys(queries):
        for scheme in schemes:
            for host in dict.fromkeys(hosts):
                for candidate in dict.fromkeys(paths):
                    out.append(urlunsplit((scheme, host, candidate, query, "")))
    return list(dict.fromkeys(out))


def build_record_lookup(manifest: Manifest) -> dict[str, ManifestRecord]:
    """Map every spelling of every fetched resource to its manifest record.

    Only successfully fetched records are included: a reference to
    something the capture never got should stay visibly broken rather than
    silently point at a file that isn't there.
    """
    lookup: dict[str, ManifestRecord] = {}

    def add(url: str, record: ManifestRecord) -> None:
        for variant in lookup_variants(url):
            lookup.setdefault(variant, record)

    for record in manifest.all():
        if record.status not in _FETCHED_STATUSES or not record.output_path:
            continue
        add(record.url, record)
        for alias in record.aliases:
            add(alias, record)
    return lookup


def build_lookup(manifest: Manifest) -> dict[str, str]:
    """Map every spelling of every fetched resource to its output_path."""
    return {url: record.output_path for url, record in build_record_lookup(manifest).items()}


def build_attachment_media_map(
    manifest: Manifest, output_dir: Path, lookup: dict[str, str]
) -> dict[str, str]:
    """Map each attachment wrapper page's URL to the output_path of the
    media file it displays.

    WordPress generates one HTML page per uploaded image. Acquisition
    deliberately gives these no output_path of their own (see
    outputs.compute_output_paths) on the understanding that Module 2
    redirects inbound links straight to the media -- a link to an image
    should reach the image, not a wrapper page that no longer exists.

    The wrapper displays its image as `<a href="full.jpg"><img ...></a>`,
    linking the shown (often resized) image to the full-size file; that
    anchor is the reliable signal (94% of real attachment pages on the test
    corpus, versus 18% for guessing from the URL slug). Only anchors whose
    target was actually captured are used, so a link never retargets to
    something absent.
    """
    media_map: dict[str, str] = {}
    for record in manifest.all():
        if FLAG_ATTACHMENT_PAGE not in record.flags or not record.local_path:
            continue
        source = output_dir / record.local_path
        if not source.exists():
            continue
        soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="replace"), "html5lib")
        for anchor in soup.select('a[href*="/wp-content/uploads/"]'):
            if not anchor.find("img"):
                continue
            for variant in lookup_variants(anchor["href"]):
                target = lookup.get(variant)
                if target is not None:
                    for page_variant in lookup_variants(record.url):
                        media_map.setdefault(page_variant, target)
                    break
            if any(v in media_map for v in lookup_variants(record.url)):
                break
    return media_map


def relative_link(from_output_path: str, to_output_path: str) -> str:
    """Relative href from one output_path to another; both are /-rooted."""
    from_dir = from_output_path.rsplit("/", 1)[0] or "/"
    return posix_relpath(to_output_path.lstrip("/"), from_dir.lstrip("/") or ".")


BUNDLE_DIR = "/assets/bundles"


class BundleReassembler:
    """Rebuilds a WordPress.com /_static/?? concat bundle as one local file.

    Acquisition expands these into their component resources (see
    extract.decode_static_bundle), so by build time the pieces are in the
    manifest but the bundle URL itself refers to nothing. Reassembling
    restores what the page actually asked for.

    Concatenated into a single file rather than emitted as N separate
    <link>s, because WordPress themes routinely depend on later components
    overriding earlier ones and splitting the bundle would preserve neither
    that order nor the single-request shape the markup was built around.

    The subtle part is CSS: a component's bytes move from
    /wp-content/themes/x/style.css to /assets/bundles/<hash>.css, so any
    relative url() inside it must be re-resolved against the *bundle's*
    location or every font and background image in it breaks.
    """

    def __init__(
        self,
        records: dict[str, ManifestRecord],
        output_dir: Path,
        site_dir: Path,
        stats: BuildStats,
    ):
        self.records = records
        self.output_dir = output_dir
        self.site_dir = site_dir
        self.stats = stats
        self.rewriter: LinkRewriter | None = None  # set once, avoids a cycle at construction
        self._cache: dict[str, str | None] = {}

    def _find(self, url: str) -> ManifestRecord | None:
        for variant in lookup_variants(url):
            record = self.records.get(variant)
            if record is not None:
                return record
        return None

    def output_path_for(self, url: str) -> str | None:
        """The site-relative path of the reassembled bundle, or None if
        `url` isn't a bundle or nothing it names was ever captured."""
        components = decode_static_bundle(url)
        if components is None:
            return None
        if url in self._cache:
            return self._cache[url]
        self._cache[url] = None  # guards against re-entry via a component's own CSS
        result = self._synthesise(url, components)
        self._cache[url] = result
        return result

    def _synthesise(self, url: str, components: list[str]) -> str | None:
        # Extension first: the output path is needed to rewrite component
        # CSS, and that path depends on the kind of bundle this is.
        suffix = ".css" if any(c.split("?")[0].endswith(".css") for c in components) else ".js"
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + suffix
        output_path = f"{BUNDLE_DIR}/{name}"
        comment = "/* %s */\n" if suffix == ".css" else "// %s\n"

        chunks: list[str] = []
        missing = 0
        for component in components:
            record = self._find(component)
            source = (
                self.output_dir / record.local_path
                if record is not None and record.local_path
                else None
            )
            if source is None or not source.exists():
                missing += 1
                chunks.append(comment % f"wpfreeze: not captured -- {component}")
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            if suffix == ".css" and self.rewriter is not None:
                text = self.rewriter.rewrite_css(text, component, output_path, relocated=True)
            chunks.append(comment % component)
            chunks.append(text)
            chunks.append("\n")

        if missing == len(components):
            return None  # nothing recoverable; leave the reference alone

        destination = self.site_dir / output_path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("".join(chunks), encoding="utf-8")

        self.stats.bundles_reassembled += 1
        self.stats.bundle_components_missing += missing
        return output_path


class LinkRewriter:
    """Rewrites references in one document at a time, against a prebuilt
    lookup. Stateless per document apart from the shared stats counter."""

    def __init__(
        self,
        lookup: dict[str, str],
        stats: BuildStats,
        bundler: BundleReassembler | None = None,
        attachment_media: dict[str, str] | None = None,
        base_url: str | None = None,
        extra_hosts: tuple[str, ...] | list[str] = (),
    ):
        self.lookup = lookup
        self.stats = stats
        self.bundler = bundler
        self.attachment_media = attachment_media or {}

        # base_url establishes this acquisition's own scope (host + path,
        # for a multisite subdirectory install) so an out-of-scope
        # reference -- a sibling subsite that stays live independently of
        # this one's static replacement -- is correctly classified as
        # `left_absolute` rather than `unresolved` (a same-site gap this
        # capture failed to resolve). None (no base_url supplied, e.g. an
        # older manifest or a test with no site context) falls back to a
        # plain host comparison against the referring page -- exactly the
        # pre-multisite-confinement behaviour, still correct for an
        # ordinary single-site run where there is no sibling-subsite case
        # to get wrong.
        #
        # The scope test itself is SiteProfile.in_scope, not a local
        # reimplementation. An earlier version open-coded the host/path
        # comparison here and silently diverged from the crawl-time
        # predicate on four axes: it never knew about extra_hosts (so a
        # configured CDN's genuinely-unresolved assets were written off as
        # `left_absolute`, hiding real gaps), and it compared raw netloc,
        # so an explicit :443 or an uppercase hostname read as external.
        self._profile = scope_profile_from_config(base_url, extra_hosts) if base_url else None

    def _out_of_scope(self, absolute: str, page_url: str) -> bool:
        if self._profile is None:
            return _different_host(absolute, page_url)
        return not self._profile.in_scope(absolute)

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

        # Bundles first: the bundle URL is never in the lookup (acquisition
        # expands it away), so ordinary resolution would always miss.
        if self.bundler is not None:
            bundle_target = self.bundler.output_path_for(absolute)
            if bundle_target is not None:
                self.stats.rewritten += 1
                return relative_link(page_output, bundle_target) + fragment

        for candidate in lookup_variants(absolute):
            target = self.lookup.get(candidate)
            if target is not None:
                self.stats.rewritten += 1
                return relative_link(page_output, target) + fragment

        # Attachment wrapper pages have no output_path of their own, so they
        # never appear in the lookup above; a link to one is redirected to
        # the media it displays instead. The fragment is dropped: it named
        # an anchor in the wrapper page that no longer exists.
        for candidate in lookup_variants(absolute):
            target = self.attachment_media.get(candidate)
            if target is not None:
                self.stats.rewritten += 1
                self.stats.attachment_links_retargeted += 1
                return relative_link(page_output, target)

        if self._out_of_scope(absolute, page_url):
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

    def rewrite_css(
        self, text: str, page_url: str, page_output: str, *, relocated: bool = False
    ) -> str:
        """Rewrite url() references in CSS.

        `relocated` must be set when the text is being moved to a different
        directory than the one it was served from -- concat bundles being
        the case in point. Leaving an unresolved reference untouched is the
        right call for a file staying put (it keeps pointing at the live
        site, which works until the site goes), but for relocated text it
        silently *changes meaning*: a relative "images/x.svg" that used to
        resolve under the component's own directory would start resolving
        under /assets/bundles/, pointing at a file that never existed
        there. Absolutising instead preserves the original target, and a
        real bundle rebuild caught exactly this -- 37 icon and font
        references quietly retargeted.
        """

        def substitute(match: re.Match) -> str:
            quote, url = match.group(1), match.group(2)
            replacement = self.resolve(url, page_url, page_output)
            if replacement:
                return f"url({quote}{replacement}{quote})"
            if relocated and url.strip() and not url.strip().lower().startswith(_SKIP_PREFIXES):
                return f"url({quote}{urljoin(page_url, url.strip())}{quote})"
            return f"url({quote}{url}{quote})"

        return _CSS_URL_RE.sub(substitute, text)

    def rewrite_html(self, html: str, page_url: str, page_output: str) -> str:
        soup = BeautifulSoup(html, "html5lib")
        self.rewrite_soup(soup, page_url, page_output)
        return str(soup)

    def rewrite_soup(self, soup: BeautifulSoup, page_url: str, page_output: str) -> None:
        """Rewrite every reference in an already-parsed document, in place.

        Split out from rewrite_html so build_site can parse once, rewrite,
        and then apply content policy to the same tree before serialising --
        rather than parse-serialise-reparse."""
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


def _bare_host(netloc: str) -> str:
    return netloc[4:] if netloc.startswith("www.") else netloc


def _different_host(absolute: str, page_url: str) -> bool:
    host = urlsplit(absolute).netloc
    return bool(host) and _bare_host(host) != _bare_host(urlsplit(page_url).netloc)


def build_site(
    manifest: Manifest,
    output_dir: Path,
    site_dir: Path,
    policy: Policy | None = None,
    base_url: str | None = None,
    extra_hosts: tuple[str, ...] | list[str] = (),
) -> BuildStats:
    """Emit the rewritten site under `site_dir`.

    Non-destructive with respect to `raw/`: every document is read from the
    capture and written to a separate tree, so a build can be re-run as
    often as needed without ever touching the acquired bytes.

    `policy` controls content stripping (telemetry, forms, feeds); the
    default strips all three. Pass a Policy with fields disabled, or None to
    accept the defaults.

    `base_url` establishes this acquisition's own scope for classifying
    unresolved references on a multisite subdirectory install -- see
    `LinkRewriter.__init__`. Omit it (older manifests) to fall back to a
    plain host comparison.
    """
    policy = policy or Policy()
    records = build_record_lookup(manifest)
    lookup = {url: record.output_path for url, record in records.items()}
    attachment_media = build_attachment_media_map(manifest, output_dir, lookup)
    stats = BuildStats()
    bundler = BundleReassembler(records, output_dir, site_dir, stats)
    rewriter = LinkRewriter(lookup, stats, bundler, attachment_media, base_url, extra_hosts)
    bundler.rewriter = rewriter
    logger.info("build: %d lookup keys, %d attachment redirects", len(lookup), len(attachment_media))

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
            soup = BeautifulSoup(text, "html5lib")
            # Policy runs first, on the original markup: telemetry references
            # still carry their true host here (e.g. googletagmanager.com),
            # whereas rewriting localizes them into host-less paths -- a
            # tracker bucketed to /assets/js/external/fbevents.js has no host
            # left to match on. Strip, then rewrite what remains.
            if policy.any_enabled:
                apply_policy(soup, policy, stats.policy)
            # After policy (which only removes) and before rewriting, so the
            # rewriter never sees an attribute normalization is about to
            # delete.
            apply_normalizations(soup, stats.normalize)
            rewriter.rewrite_soup(soup, record.url, record.output_path)
            destination.write_text(str(soup), encoding="utf-8")
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

    # The redirect map is an acquisition product (directory->file rules plus
    # ?attachment_id=/alias 301s); it only helps if it travels with the site
    # it describes, so copy it into the tree Apache would actually serve.
    redirects = output_dir / "redirects.htaccess"
    if redirects.exists():
        (site_dir / ".htaccess").write_bytes(redirects.read_bytes())
        stats.redirects_copied = True

    return stats


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
#
# Deliberately independent of everything above: it re-reads the emitted tree
# from disk and resolves references against real files, knowing nothing about
# the manifest, the lookup, or which rewrite rule produced a given path. A
# check that shares its assumptions with the code it checks will agree with a
# bug rather than catch it.


@dataclass(frozen=True)
class BrokenReference:
    source: str  # site-relative path of the document holding the reference
    reference: str  # the attribute value as written
    target: str  # where it resolved to, site-relative
    reason: str


@dataclass
class VerifyReport:
    checked: int = 0
    external: int = 0
    skipped: int = 0
    documents: int = 0
    broken: list[BrokenReference] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.broken


def _iter_document_references(text: str, is_css: bool):
    """Yield every reference in one document, as written."""
    if is_css:
        for match in _CSS_URL_RE.finditer(text):
            yield match.group(2)
        return

    soup = BeautifulSoup(text, "html5lib")
    for tag_name, attrs in _URL_ATTRS.items():
        for tag in soup.find_all(tag_name):
            for attr in attrs:
                value = tag.get(attr)
                if value:
                    yield value
    for tag_name in _SRCSET_TAGS:
        for tag in soup.find_all(tag_name):
            for entry in (tag.get("srcset") or "").split(","):
                entry = entry.strip()
                if entry:
                    yield entry.split()[0]
    for tag in soup.find_all(style=True):
        for match in _CSS_URL_RE.finditer(tag["style"]):
            yield match.group(2)
    for tag in soup.find_all("style"):
        if tag.string:
            for match in _CSS_URL_RE.finditer(tag.string):
                yield match.group(2)


def verify_site(site_dir: Path) -> VerifyReport:
    """Resolve every local reference in the emitted tree against disk.

    A reference is checked as a static server would serve it: query strings
    and fragments are ignored (a file either exists at that path or does
    not), and percent-encoding is decoded first.
    """
    from posixpath import normpath
    from urllib.parse import unquote

    report = VerifyReport()

    for document in sorted(site_dir.rglob("*")):
        if not document.is_file() or document.suffix.lower() not in (".html", ".htm", ".css"):
            continue
        report.documents += 1
        relative_dir = document.parent.relative_to(site_dir).as_posix()
        text = document.read_text(encoding="utf-8", errors="replace")

        for reference in _iter_document_references(text, document.suffix.lower() == ".css"):
            value = reference.strip()
            if not value or value.lower().startswith(_SKIP_PREFIXES):
                report.skipped += 1
                continue
            if urlsplit(value).scheme or value.startswith("//"):
                report.external += 1
                continue

            report.checked += 1
            path = unquote(urlsplit(value).path)
            if not path:
                continue  # a bare query or fragment: same document
            base = "" if relative_dir == "." else relative_dir
            resolved = normpath(f"{base}/{path}" if base else path.lstrip("/"))

            source = document.relative_to(site_dir).as_posix()
            if resolved.startswith(".."):
                report.broken.append(
                    BrokenReference(source, value, resolved, "escapes site root")
                )
            elif not (site_dir / resolved).exists():
                report.broken.append(BrokenReference(source, value, resolved, "missing"))

    return report


def format_verify_summary(report: VerifyReport) -> str:
    import collections

    lines = [
        "Verification:",
        f"  {report.documents} document(s), {report.checked} local reference(s) checked",
        f"  {report.external} external, {report.skipped} skipped (mailto/data/anchors)",
    ]
    if report.ok:
        lines.append("  no broken local references")
        return "\n".join(lines)

    lines.append(f"  ** {len(report.broken)} BROKEN local reference(s) **")
    by_reason = collections.Counter(b.reason for b in report.broken)
    for reason, count in by_reason.most_common():
        lines.append(f"     {count} {reason}")
    grouped = collections.Counter(
        b.target.rsplit(".", 1)[-1][:12] if "." in b.target.rsplit("/", 1)[-1] else "(no extension)"
        for b in report.broken
    )
    lines.append(f"     by target type: {dict(grouped.most_common(6))}")
    for broken in report.broken[:5]:
        lines.append(f"       {broken.reference[:70]}  in {broken.source}")
    return "\n".join(lines)


_MAX_BROKEN_SAMPLES = 50


def write_build_report(
    stats: BuildStats, output_dir: Path, verify: "VerifyReport | None" = None
) -> Path:
    """Persist build stats to build-report.json, mirroring
    validate.write_validation_report. Makes this run's findings --
    notably unresolved_samples, the highest-signal field for a later
    cleanup summary -- available to a step invoked separately in time or
    process (e.g. `wpfreeze validate` run well after `wpfreeze build`, or
    the cleanup-todo synthesis reading back whatever is on disk).

    `verify` carries verify_site's outcome, which has exactly that
    property and was previously printed to the console and discarded. A
    build whose references all rewrote cleanly but whose local links do
    not resolve on disk exits non-zero -- and without this the cleanup
    checklist, regenerated moments later in the same command, had no way
    to know and cheerfully reported the capture clean. None means
    verification did not run (`--no-verify`), which is distinct from
    running and finding nothing.
    """
    path = output_dir / "build-report.json"
    data = asdict(stats)
    data["verification"] = (
        None
        if verify is None
        else {
            "documents": verify.documents,
            "checked": verify.checked,
            "external": verify.external,
            "skipped": verify.skipped,
            "broken": len(verify.broken),
            "broken_samples": [
                {"source": b.source, "reference": b.reference, "target": b.target, "reason": b.reason}
                for b in verify.broken[:_MAX_BROKEN_SAMPLES]
            ],
        }
    )
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def format_build_summary(stats: BuildStats) -> str:
    considered = stats.considered or 1
    lines = [
        "Build:",
        f"  {stats.pages} page(s), {stats.assets} asset(s) written",
        *(
            [
                f"  {stats.bundles_reassembled} concat bundle(s) reassembled"
                + (
                    f" ({stats.bundle_components_missing} component(s) not captured)"
                    if stats.bundle_components_missing
                    else ""
                )
            ]
            if stats.bundles_reassembled
            else []
        ),
        f"  references rewritten to local : {stats.rewritten} ({stats.rewritten / considered:.1%})",
        f"  left absolute (external)      : {stats.left_absolute} ({stats.left_absolute / considered:.1%})",
        f"  unresolved                    : {stats.unresolved} ({stats.unresolved / considered:.1%})",
    ]
    if stats.unresolved:
        lines.append("  (unresolved references are left pointing at the original site)")
    p = stats.policy
    if p.telemetry_removed or p.forms_removed or p.feeds_removed or p.wp_meta_links_removed:
        lines.append(
            f"  stripped: {p.telemetry_removed} telemetry, "
            f"{p.forms_removed} form(s), {p.feeds_removed} feed link(s), "
            f"{p.wp_meta_links_removed} WP protocol-discovery link(s)"
        )
    n = stats.normalize
    if n.total:
        lines.append(
            f"  markup repaired: {n.word_artifacts_unwrapped} Word artifact(s), "
            f"{n.obsolete_attrs_removed} obsolete attribute(s), "
            f"{n.control_chars_stripped} forbidden control char(s), "
            f"{n.invalid_dimensions_removed} invalid dimension(s), "
            f"{n.boolean_attrs_normalized} boolean attribute(s)"
        )
    return "\n".join(lines)
