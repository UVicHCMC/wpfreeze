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

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString

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
#
# Note on "shortlink": apply_policy runs immediately before the rewriter
# (build.py), so this tag is gone before any reference in it is resolved.
# inventory.register_shortlink_aliases still earns its place -- it covers
# raw ?p=<id> links in body content, which policy does not touch -- but
# neither is redundant just because the other exists. Don't remove one on
# the strength of the other.
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

# --- Comment-form fingerprints ---------------------------------------------
#
# WordPress core's comment_form() (wp-includes/comment-template.php) hardcodes
# this wrapper -- a heading, the form, and a "Cancel reply" link, all inside
# <div id="respond" class="comment-respond"> -- regardless of what a theme
# does with the surrounding page. A theme can (and often does) override the
# heading's *text* via comment_form()'s $args, but the id="respond"/
# class="comment-respond" wrapper and id="commentform"/class="comment-form"
# form itself are not part of that args array -- they're baked into the
# function. So detecting the wrapper, not the caption text, catches a themed
# "Submit a Comment" heading exactly as reliably as the untouched default
# "Leave a Reply", without any language- or theme-specific guessing.
_COMMENT_WRAPPER_IDS: frozenset[str] = frozenset({"respond"})
_COMMENT_WRAPPER_CLASSES: frozenset[str] = frozenset({"comment-respond"})
_COMMENT_FORM_IDS: frozenset[str] = frozenset({"commentform"})
_COMMENT_FORM_CLASSES: frozenset[str] = frozenset({"comment-form"})
# How far up from a <form> to look for its wrapper. Generous for real markup
# (the wrapper is almost always the form's direct parent) but bounded so a
# pathologically deep tree can't turn this into an unbounded walk.
_COMMENT_WRAPPER_MAX_DEPTH = 5

# A lone separator character between post-meta items ("Feb 1, 2018 | News |
# 0 comments") -- matched so removing the last item in the line doesn't
# leave a dangling "|" with nothing after it. Deliberately only whitespace
# plus one punctuation character: a real content text node should never
# match this by accident.
_META_SEPARATOR_RE = re.compile(r"^\s*[|/•·]\s*$")

# --- Search-form fingerprints -----------------------------------------------
#
# Unlike comment forms, WordPress core doesn't hardcode a wrapper around a
# search form -- get_search_form() just returns the <form> itself, and a
# theme is free to wrap it in whatever markup it likes. But the <form>'s own
# attributes are still reliable: role="search" is the accessibility
# convention core's default template sets, and name="s" is WordPress's own
# canonical query-var for search -- both survive even in themes (Divi
# included) that don't call get_search_form() at all and hand-roll their own
# markup with their own class names instead. Either alone is sufficient;
# neither is something a non-search form would plausibly carry.
_SEARCH_INPUT_NAME = "s"


@dataclass
class Policy:
    """What to strip from each page. All four default on: the whole point
    of a build is a self-contained archive."""

    strip_telemetry: bool = True
    strip_forms: bool = True
    strip_feeds: bool = True
    strip_wp_meta_links: bool = True
    # Only meaningful when strip_forms removes a comment form's wrapper (see
    # _comment_wrapper): whether the "N comments" post-meta blurb built
    # around the now-dead #respond link is removed outright (True, the
    # default -- matches strip_forms's own "no live-web machinery" stance),
    # or -- when False -- kept as plain text for any page with a nonzero
    # count (the comment thread, if captured, is still real content someone
    # may want the count for) while still unwrapping the dead link either
    # way. A "0 comments" blurb is always removed regardless of this flag:
    # it's noise, not information, in either mode.
    strip_comment_counts: bool = True
    # Only meaningful when strip_forms removes a recognized search form
    # (see _is_search_form). True (default) removes it like any other
    # form -- it can't submit anywhere useful on a static archive. False
    # leaves it completely untouched: for a site owner planning to wire up
    # a replacement (Google Custom Search, a static index, ...) rather
    # than just lose search entirely. Note this does NOT make the form
    # functional -- its action attribute still gets rewritten like any
    # other reference, so submitting it as-is just navigates to the local
    # homepage with an ignored ?s= query string. It's raw material to
    # repurpose, not a working search box. Pages with a form left this way
    # are listed in the cleanup checklist so they're easy to find again.
    strip_search_forms: bool = True
    # Lift <style> blocks repeated verbatim across pages into shared files.
    # Not a strip -- nothing is removed, the same CSS is served from one
    # place instead of hundreds. See dedupe.py.
    dedupe_inline_css: bool = True
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
            strip_comment_counts=bool(raw.get("strip_comment_counts", True)),
            strip_search_forms=bool(raw.get("strip_search_forms", True)),
            dedupe_inline_css=bool(raw.get("dedupe_inline_css", True)),
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
    # Which pages had a form excised, how many, and the page's own output
    # path (so the cleanup checklist can link straight to the local built
    # copy). Unlike the other three strips (telemetry, feeds, WP protocol-
    # discovery links) -- all <head> or third-party removals invisible in
    # the rendered page -- a removed <form> can leave a heading, label, or
    # "Subscribe" button behind with nothing under it any more. Per-page
    # detail is what a site owner needs to go check those pages, not just
    # a global count (see cleanup.py). Keyed by page_url; each value is
    # {"count": int, "output_path": str, "categories": {"comment": int, ...}}.
    # "comment" forms had their caption/cancel-link wrapper removed with
    # them (see _comment_wrapper) and so are lower-priority to check by
    # hand; "search" forms are recognized but get no special wrapper
    # handling (see _is_search_form); subscribe, contact, and anything
    # unrecognized still bucket under "other" for now.
    forms_removed_pages: dict[str, dict] = field(default_factory=dict)
    # In-page anchors that pointed at an id a form-wrapper removal just took
    # with it (WordPress's own "N comments" post-meta link, most commonly,
    # via #respond) -- the link itself unwrapped (dead_fragment_links_removed)
    # or, when it sits in a "comments-number" blurb, the whole blurb removed
    # instead (comment_count_blurbs_removed). See _clean_dead_fragment_links.
    dead_fragment_links_removed: int = 0
    comment_count_blurbs_removed: int = 0
    # Which pages had a search form recognized but left in place, per
    # strip_search_forms: false -- same shape as forms_removed_pages'
    # values, minus "categories" (always search, that's the only reason an
    # entry exists here). Tracked separately from forms_removed_pages
    # because nothing was actually removed; reporting it as an excision
    # would say something false.
    search_forms_kept_pages: dict[str, dict] = field(default_factory=dict)


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


def _comment_wrapper(form):
    """The nearest ancestor matching WordPress core's comment_form() wrapper
    (id="respond"/class="comment-respond"), or None if `form` isn't inside
    one -- either because it's some other kind of form, or because whatever
    generated it didn't use core's wrapper. Bounded to
    _COMMENT_WRAPPER_MAX_DEPTH ancestors; the wrapper is almost always the
    form's direct parent in practice.
    """
    node = form.parent
    for _ in range(_COMMENT_WRAPPER_MAX_DEPTH):
        name = getattr(node, "name", None)
        if name in (None, "[document]", "html"):
            return None
        if node.get("id") in _COMMENT_WRAPPER_IDS:
            return node
        if set(node.get("class") or []) & _COMMENT_WRAPPER_CLASSES:
            return node
        node = node.parent
    return None


def _is_comment_form(form) -> bool:
    """True if the <form> itself carries WordPress core's comment-form
    id/class -- a fallback for the (presumably rare) case its wrapper isn't
    present but the form's own attributes are."""
    if form.get("id") in _COMMENT_FORM_IDS:
        return True
    return bool(set(form.get("class") or []) & _COMMENT_FORM_CLASSES)


_COMMENT_COUNT_DIGITS_RE = re.compile(r"\d+")


def _comment_count(text: str) -> int | None:
    """The first integer found in a "comments-number" blurb's text ("0
    comments", "2 Comments", ...), or None if it doesn't contain one (a
    theme writing "No comments yet" or "One comment" with no numeral --
    presumably rare, but not something to guess a value for)."""
    match = _COMMENT_COUNT_DIGITS_RE.search(text)
    return int(match.group()) if match else None


def _remove_comments_number_blurb(wrapper, stats: PolicyStats) -> None:
    prev = wrapper.previous_sibling
    if isinstance(prev, NavigableString) and _META_SEPARATOR_RE.match(str(prev)):
        prev.extract()
    wrapper.decompose()
    stats.comment_count_blurbs_removed += 1


def _clean_dead_fragment_links(
    soup: BeautifulSoup, removed_ids: set[str], stats: PolicyStats, strip_comment_counts: bool
) -> None:
    """An in-page anchor (`href="...#id"`) pointing at an id a form-wrapper
    removal just took with it is now a dead link to nowhere -- most often
    WordPress's own "N comments" post-meta blurb (`class="comments-number"`),
    which core themes link to the comment form's #respond id. The link
    itself is always unwrapped (dropped, visible text kept) when its blurb
    is not removed outright, since a link to a place that no longer exists
    is unconditionally wrong regardless of what it says.

    Whether the blurb itself goes depends on `strip_comment_counts`: True
    removes it unconditionally (a frozen count is stale information on a
    static archive regardless of its value); False keeps it when it reports
    a nonzero count (the comment thread, if captured, is still real content
    someone may want the count for) and removes it only when the count
    parses as zero -- "0 comments" is noise either way. Unparseable text
    (see _comment_count) is treated as "has comments": the safer of the two
    wrong guesses, since it only risks leaving a stale count rather than
    deleting a real one.
    """
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        fragment = href.rsplit("#", 1)[-1] if "#" in href else None
        if not fragment or fragment not in removed_ids:
            continue

        wrapper = anchor.find_parent(class_="comments-number")
        if wrapper is not None and (strip_comment_counts or _comment_count(wrapper.get_text()) == 0):
            _remove_comments_number_blurb(wrapper, stats)
        else:
            anchor.unwrap()
            stats.dead_fragment_links_removed += 1


def _is_search_form(form) -> bool:
    """True if `form` is a WordPress search form -- see the fingerprint
    note above _SEARCH_INPUT_NAME for why role="search" or a name="s"
    input, either alone, is enough regardless of theme."""
    if (form.get("role") or "").strip().lower() == "search":
        return True
    return form.find("input", attrs={"name": _SEARCH_INPUT_NAME}) is not None


def _strip_forms(soup: BeautifulSoup, policy: "Policy", stats: PolicyStats, page_url: str, page_output: str) -> None:
    # On a static archive no <form> submits usefully: comment and search
    # forms hit dead endpoints, and subscribe forms leak to a third party.
    # Remove the element outright (decision B: remove, not neuter). A
    # recognized comment form takes its whole wrapper with it -- heading and
    # "Cancel reply" link included -- rather than leaving an orphaned
    # caption behind; see the fingerprint note above _COMMENT_WRAPPER_IDS.
    handled: set[int] = set()  # id() of <form> tags already removed via a wrapper
    count = 0
    categories: dict[str, int] = {}
    removed_ids: set[str] = set()
    kept_search_count = 0

    for form in soup.find_all("form"):
        if id(form) in handled:
            continue

        wrapper = _comment_wrapper(form)
        if wrapper is not None or _is_comment_form(form):
            category = "comment"
        elif _is_search_form(form):
            category = "search"
        else:
            category = "other"

        if category == "search" and not policy.strip_search_forms:
            # Left in place, not removed -- see strip_search_forms's own
            # comment for why (a site owner may want to reimplement search
            # rather than just lose it). Tracked separately below, not as
            # an excision, since nothing was actually excised.
            kept_search_count += 1
            continue

        target = wrapper if wrapper is not None else form

        # A wrapper can (rarely, malformed markup) contain more than one
        # <form>; mark every form inside it handled before decomposing so a
        # later loop iteration doesn't touch an already-detached node.
        inner_forms = target.find_all("form") if target is not form else [form]
        for inner in inner_forms:
            handled.add(id(inner))

        # Record the id(s) being removed before decomposing -- an in-page
        # anchor elsewhere on this page may point at one of them (see
        # _clean_dead_fragment_links) and would otherwise be left dangling.
        for node in (target, form):
            node_id = node.get("id")
            if node_id:
                removed_ids.add(node_id)

        target.decompose()
        count += 1
        categories[category] = categories.get(category, 0) + 1

    if removed_ids:
        _clean_dead_fragment_links(soup, removed_ids, stats, policy.strip_comment_counts)

    if count:
        stats.forms_removed += count
        if page_url:
            entry = stats.forms_removed_pages.setdefault(
                page_url, {"count": 0, "output_path": page_output, "categories": {}}
            )
            entry["count"] += count
            for category, n in categories.items():
                entry["categories"][category] = entry["categories"].get(category, 0) + n

    if kept_search_count and page_url:
        entry = stats.search_forms_kept_pages.setdefault(
            page_url, {"count": 0, "output_path": page_output}
        )
        entry["count"] += kept_search_count


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


def apply_policy(
    soup: BeautifulSoup, policy: Policy, stats: PolicyStats, page_url: str = "", page_output: str = ""
) -> None:
    """Apply the content policy to a parsed document, in place.

    `page_url`/`page_output` are only used to key/link forms_removed_pages;
    omit them (as the unit tests do, one document at a time with no page
    identity of interest) and forms still get stripped and counted, just
    not attributed to a page.
    """
    if policy.strip_telemetry:
        _strip_telemetry(soup, policy, stats)
    if policy.strip_forms:
        _strip_forms(soup, policy, stats, page_url, page_output)
    if policy.strip_feeds:
        _strip_feeds(soup, stats)
    if policy.strip_wp_meta_links:
        _strip_wp_meta_links(soup, stats)
