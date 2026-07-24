"""Content policy: strip live-web machinery that has no place in an archive.

A faithfully-captured WordPress page still carries three things that make a
poor offline copy, none of which the link rewriter touches because none of
them are *broken* in a resolve-against-disk sense:

- **Telemetry.** Analytics beacons and tag managers that fire on every view.
  An archive served from your own disk should not phone home to Google,
  Facebook, or WordPress.com, nor log a reader's IP with a third party.
- **Forms.** Subscribe, comment, and search forms whose actions are dead in
  a static context -- or worse, still-live endpoints that silently leak a
  reader's email or comment to a third party while looking like they work.
- **Feeds.** `<link rel="alternate">` pointers to RSS/Atom endpoints that
  cease to exist the moment the site comes down.
- **WordPress protocol-discovery links.** `<link>` tags advertising
  machinery that requires a live PHP backend -- `rel="pingback"` and
  `rel="EditURI"` (both point at `xmlrpc.php`), `rel="https://api.w.org/"`
  and `rel="alternate" type="application/json"` (the REST API), oEmbed
  discovery, and `rel="shortlink"` (the page's own `?p=<id>` form). None of
  these are rendered or used by a browser; all are dead the moment the live
  site comes down. `rel="canonical"` is left alone -- unlike these, it is a
  genuine, still-meaningful self-reference, not protocol machinery.

This module removes all four, by default, on the reasoning that the point
of the exercise is a durable, self-contained copy. Every removal is counted
and surfaced in the build summary -- nothing is stripped silently.

On the target population (self-hosted WordPress, not WordPress.com),
telemetry is injected by plugins -- Google Site Kit, MonsterInsights, GA for
WordPress, "Insert Headers and Footers", Jetpack -- as *dedicated* markup:
an external `<script src>`, a self-contained inline `<script>` block, a
`<noscript>` tracking iframe. That is why whole-element removal is both
sufficient and safe here in practice: WordPress does not fuse tracking into
a theme's own application JavaScript the way a hand-built single-page app
might.

Two honest limits follow from that, neither silently swallowed:

- **What aggression risks.** A no-src `<script>` matching a telemetry
  signature is removed whole, tracking-only or not. If a site ever put a
  tracking call in the same block as real behaviour, that block goes too.
  Not observed on real WordPress captures, but it is the price of "no
  telemetry at all" and is stated in the build's known-limitations.
- **What it cannot reach.** Telemetry loaded from a host not on the
  blocklist, or inline code that matches none of the signatures, stays.
  The blocklist and signature list are the coverage; widen them per-site
  via the `telemetry_extra_hosts` config key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

# --- Telemetry host blocklist ---------------------------------------------
#
# Matched as a substring of a reference's hostname, so "google-analytics.com"
# also catches "www.google-analytics.com" and "ssl.google-analytics.com".
# Vendor-agnostic and deliberately broad: adding a host here is cheap and the
# cost of missing a tracker (it fires forever in the archive) is higher than
# the cost of an over-match (a resource that would not have loaded anyway).
_TELEMETRY_HOSTS: frozenset[str] = frozenset(
    {
        # Google: Analytics, Tag Manager, Ads, DoubleClick.
        "google-analytics.com",
        "googletagmanager.com",
        "googleadservices.com",
        "googlesyndication.com",
        "doubleclick.net",
        "g.doubleclick.net",
        # Meta / Facebook pixel.
        "connect.facebook.net",
        "facebook.com/tr",
        # WordPress.com / Jetpack / Automattic (present when a self-hosted
        # site runs Jetpack Stats).
        "stats.wp.com",
        "pixel.wp.com",
        "s.pubmine.com",
        # Other common analytics/heatmap/attribution vendors.
        "static.hotjar.com",
        "script.hotjar.com",
        "cdn.segment.com",
        "bat.bing.com",
        "clarity.ms",
        "snap.licdn.com",
        "analytics.tiktok.com",
        "matomo.cloud",
        "cdn.mxpnl.com",  # Mixpanel
        "quantserve.com",
        "scorecardresearch.com",
    }
)

# --- Telemetry path/filename signatures -----------------------------------
#
# Same-host tracking. On self-hosted WordPress the analytics *plugin* serves
# its own tracking JavaScript from the site's own domain
# (/wp-content/plugins/google-analytics-for-wordpress/.../frontend-gtag.min.js
# is MonsterInsights), so a host blocklist cannot catch it -- the host is the
# site itself. These match against the reference's path instead. Each is a
# plugin slug or a distinctive filename specific enough that a match means
# tracking, not an ordinary asset that merely lives near one.
_TELEMETRY_PATH_SIGNATURES: tuple[str, ...] = (
    "google-analytics-for-wordpress",  # MonsterInsights
    "google-analytics",  # generic GA scripts / other GA plugins
    "googlesitekit",  # Google Site Kit
    "wp-analytify",
    "/analytify",
    "frontend-gtag",
    "/gtag.js",
    "/gtag/js",
    "/ga.js",
    "/matomo.js",
    "/piwik.js",
    "/matomo/",
    "/piwik/",
    "wp-statistics",  # self-hosted analytics plugin
    "facebook-pixel",
)

# --- Inline telemetry signatures ------------------------------------------
#
# A no-src <script> whose body contains any of these is treated as a
# dedicated telemetry block and removed whole. Chosen to be specific enough
# that they don't appear in ordinary application code: bare "ga(" is
# deliberately excluded (too collision-prone) in favour of the qualified
# "ga('create'"/"ga('send'" forms.
_TELEMETRY_INLINE_SIGNATURES: tuple[str, ...] = (
    "www.googletagmanager.com/gtm.js",
    "gtag(",
    "GoogleAnalyticsObject",
    "_gaq.push",
    "ga('create'",
    "ga('send'",
    "__gaTracker",
    "__gtagTracker",
    "fbq('init'",
    "fbq('track'",
    "facebook.com/tr",
    "_stq.push",  # WordPress.com stats
    "_hjSettings",  # Hotjar
    "hj('",
    "_paq.push",  # Matomo
    "clarity('",  # Microsoft Clarity
    "MonsterInsights",  # GA-for-WordPress plugin config block
    "mi_track_user",
    "wp_analytify",
)

# --- Feed link types ------------------------------------------------------
_FEED_LINK_TYPES: frozenset[str] = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
)

# --- WordPress protocol-discovery link rels/types --------------------------
# rel values matched directly (each is its own dedicated protocol marker,
# not shared with anything legitimate a build should keep).
_WP_META_LINK_RELS: frozenset[str] = frozenset(
    {"pingback", "shortlink", "edituri", "https://api.w.org/"}
)
# type values matched only alongside rel="alternate" -- application/json is
# WordPress's own REST self-representation; the other two are oEmbed
# discovery. Bare rel="alternate" for other types (e.g. feeds, or a
# stylesheet's alternate) is untouched.
_WP_META_LINK_ALTERNATE_TYPES: frozenset[str] = frozenset(
    {"application/json", "application/json+oembed", "text/xml+oembed"}
)


@dataclass
class Policy:
    """What to strip from each page. All four default on: the whole point
    of a build is a self-contained archive."""

    strip_telemetry: bool = True
    strip_forms: bool = True
    strip_feeds: bool = True
    strip_wp_meta_links: bool = True
    # Site-specific trackers to add to the built-in host blocklist.
    telemetry_extra_hosts: list[str] = field(default_factory=list)
    # Escape hatch: hostnames never stripped even if otherwise matched.
    telemetry_keep_hosts: list[str] = field(default_factory=list)

    @property
    def any_enabled(self) -> bool:
        return (
            self.strip_telemetry
            or self.strip_forms
            or self.strip_feeds
            or self.strip_wp_meta_links
        )

    @classmethod
    def from_config(cls, raw: dict | None) -> "Policy":
        raw = raw or {}
        return cls(
            strip_telemetry=bool(raw.get("strip_telemetry", True)),
            strip_forms=bool(raw.get("strip_forms", True)),
            strip_feeds=bool(raw.get("strip_feeds", True)),
            strip_wp_meta_links=bool(raw.get("strip_wp_meta_links", True)),
            telemetry_extra_hosts=list(raw.get("telemetry_extra_hosts", []) or []),
            telemetry_keep_hosts=list(raw.get("telemetry_keep_hosts", []) or []),
        )

    def telemetry_hosts(self) -> frozenset[str]:
        return _TELEMETRY_HOSTS | frozenset(self.telemetry_extra_hosts)


@dataclass
class PolicyStats:
    telemetry_removed: int = 0
    forms_removed: int = 0
    feeds_removed: int = 0
    wp_meta_links_removed: int = 0


def _is_telemetry_ref(url: str, blocked: frozenset[str], kept: list[str]) -> bool:
    """True if a src/href points at telemetry -- by third-party host, or by
    a path signature that catches trackers served from the site's own host."""
    host = urlsplit(url).netloc.lower()
    if any(k and k.lower() in host for k in kept):
        return False
    if any(b in host for b in blocked):
        return True
    path = urlsplit(url).path.lower()
    return any(sig in path for sig in _TELEMETRY_PATH_SIGNATURES)


def _strip_telemetry(soup: BeautifulSoup, policy: Policy, stats: PolicyStats) -> None:
    blocked = policy.telemetry_hosts()
    kept = policy.telemetry_keep_hosts

    # External references: script/img/iframe by src, link by href, plus the
    # <noscript> tracking iframe GTM injects for no-JS clients.
    for tag in soup.find_all(["script", "img", "iframe"]):
        ref = tag.get("src")
        if ref and _is_telemetry_ref(ref, blocked, kept):
            tag.decompose()
            stats.telemetry_removed += 1
    for tag in soup.find_all("link"):
        ref = tag.get("href")
        if ref and _is_telemetry_ref(ref, blocked, kept):
            tag.decompose()
            stats.telemetry_removed += 1

    # Inline telemetry blocks: a no-src <script> that is a dedicated tracker.
    for tag in soup.find_all("script"):
        if tag.get("src"):
            continue
        body = tag.string or tag.get_text() or ""
        if any(sig in body for sig in _TELEMETRY_INLINE_SIGNATURES):
            tag.decompose()
            stats.telemetry_removed += 1


def _strip_forms(soup: BeautifulSoup, stats: PolicyStats) -> None:
    # On a static archive no <form> submits usefully: comment and search
    # forms hit dead endpoints, and subscribe forms leak to a third party.
    # Remove the element outright (decision B: remove, not neuter).
    for form in soup.find_all("form"):
        form.decompose()
        stats.forms_removed += 1


def _strip_feeds(soup: BeautifulSoup, stats: PolicyStats) -> None:
    # <link rel="alternate" type="application/rss+xml"> and friends in the
    # document head. Visible "Subscribe via RSS" anchors in body content are
    # left alone -- they are content, and removing body text is a more
    # invasive edit than clearing dead <head> metadata.
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        link_type = (link.get("type") or "").lower()
        if "alternate" in [r.lower() for r in rel] and link_type in _FEED_LINK_TYPES:
            link.decompose()
            stats.feeds_removed += 1


def _strip_wp_meta_links(soup: BeautifulSoup, stats: PolicyStats) -> None:
    # Dead self-referential WordPress protocol-discovery <link> tags -- see
    # the module docstring for what each rel/type points at. None are
    # rendered or used by a browser; rel="canonical" is a real, still-
    # meaningful self-reference and is deliberately not matched here.
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        rel_lower = {r.lower() for r in rel}
        link_type = (link.get("type") or "").lower()
        if rel_lower & _WP_META_LINK_RELS:
            link.decompose()
            stats.wp_meta_links_removed += 1
        elif "alternate" in rel_lower and link_type in _WP_META_LINK_ALTERNATE_TYPES:
            link.decompose()
            stats.wp_meta_links_removed += 1


def apply_policy(soup: BeautifulSoup, policy: Policy, stats: PolicyStats) -> None:
    """Apply the content policy to a parsed document, in place."""
    if policy.strip_telemetry:
        _strip_telemetry(soup, policy, stats)
    if policy.strip_forms:
        _strip_forms(soup, stats)
    if policy.strip_feeds:
        _strip_feeds(soup, stats)
    if policy.strip_wp_meta_links:
        _strip_wp_meta_links(soup, stats)
