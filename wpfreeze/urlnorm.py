"""URL normalization: canonical form for every URL before it touches the manifest.

Pure, no-network module. Site-specific facts that require a live probe
(does the site serve https? does it redirect www -> non-www or vice
versa? does it prefer a trailing slash?) are captured once at startup
into a SiteProfile and passed in here -- this module never makes a
request itself.
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_UNRESERVED = set(string.ascii_letters + string.digits + "-._~")

# Query-string keys that encode a WordPress permalink fallback and are
# worth keeping until a pretty permalink is known for the content they
# point at.
# author/cat/tag/taxonomy/term are WordPress's own core fallback query
# vars for archive pages (the same ones the WXR inventory's
# wxr_author_urls/wxr_term_urls build, e.g. "?author=6", "?cat=5",
# "?taxonomy=foo&term=bar") -- stripping them collapses a real, distinct
# archive page down to the bare site root, colliding its identity with
# the homepage. Seen on a real crawl: a dozen author/category/tag
# archives all merged into "the homepage" this way, and the last one
# processed silently overwrote the real homepage's fetched content.
PERMALINK_QUERY_KEYS = frozenset(
    {"p", "page_id", "attachment_id", "author", "cat", "tag", "taxonomy", "term"}
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


@dataclass(frozen=True)
class SiteProfile:
    """Facts about a target site, established once at startup by probing it.

    canonical_host: the hostname the site itself redirects to (e.g. the
        site might 301 www.example.com -> example.com, or vice versa).
    site_hosts: every hostname that should be folded into canonical_host
        (normally {host, "www."+host} plus any configured extra_hosts).
    use_https: whether the site actually serves https; if False, http
        URLs for this site are left alone rather than upgraded.
    trailing_slash: whether the site prefers a trailing slash on
        directory-like paths (no dot in the final segment). Probed once
        against the site's own redirect behaviour.
    base_path: the path component of base_url, always "/"-terminated.
        "/" for an ordinary site at a domain root; "/subsite/" when the
        target is one site of a WordPress multisite network living in a
        subdirectory. See in_scope().
    """

    canonical_host: str
    site_hosts: frozenset[str] = field(default_factory=frozenset)
    use_https: bool = True
    trailing_slash: bool = True
    base_path: str = "/"
    extra_hosts: frozenset[str] = field(default_factory=frozenset)

    def owns_host(self, host: str) -> bool:
        return host in self.site_hosts or host == self.canonical_host

    def in_scope(self, url: str) -> bool:
        """Whether `url` belongs to the site being archived -- right host,
        and under base_url's path.

        The path half only bites on a multisite subdirectory install, where
        base_path is something like "/courses/": there, owns_host alone is
        not containment, because every sibling site on the network shares the
        hostname. With base_path == "/" this is exactly owns_host.

        The base itself counts as in scope with or without its trailing
        slash. base_path is always "/"-terminated (see probe_site), but a
        site that prefers no trailing slash normalizes its own root to
        "/courses" -- and a bare textual prefix test against
        "/courses/" would drop the subsite's front page, the single most
        important URL in the capture. That combination is not exotic:
        trailing_slash is only ever probed (and so only ever False) on a
        subdirectory install, which is the only case where base_path isn't
        "/", so the two conditions always arrive together.
        """
        host = urlsplit(url).hostname or ""
        if not self.owns_host(host):
            return False
        if host in self.extra_hosts:
            # base_path confines only the host that carries sibling subsites.
            # A configured extra host is a CDN serving this site's uploads
            # from its own root -- it has no sibling-subsite problem, so a
            # path prefix drawn from base_url means nothing there. Ordinary
            # single-site runs already treat extra hosts this way (base_path
            # is "/", so the prefix test is vacuous); without this, the same
            # config line would quietly mean something different depending on
            # whether the target happened to be a subdirectory install.
            return True
        path = urlsplit(url).path
        return path.startswith(self.base_path) or path == self.base_path.rstrip("/")


def scope_profile_from_config(base_url: str, extra_hosts: tuple[str, ...] | list[str] = ()) -> SiteProfile:
    """A SiteProfile good enough to answer in_scope(), built from config
    alone with no network probing.

    `build` and `validate` run offline, potentially long after the original
    site is gone, so probe_site is not available to them -- and the
    manifest does not persist the probed profile. But in_scope() consults
    only owns_host() and base_path, both of which config already
    determines: use_https and trailing_slash affect normalize_url, not
    scope, so their defaults here are irrelevant.

    Deriving this from config rather than from the capture does mean that
    editing base_url or extra_hosts between `acquire` and `build`
    reclassifies an existing capture. That is the intended reading --
    config is the statement of what "this site" means -- but it is the
    reason to persist the real profile in the manifest if that ever stops
    being true.
    """
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    hosts = {h for h in (host, bare, f"www.{bare}" if bare else "") if h}
    hosts.update(h.lower() for h in extra_hosts if h)

    base_path = parts.path or "/"
    if not base_path.endswith("/"):
        base_path += "/"

    return SiteProfile(
        canonical_host=bare,
        site_hosts=frozenset(hosts),
        base_path=base_path,
        extra_hosts=frozenset(h.lower() for h in extra_hosts if h),
    )


def resolve_url(base: str, link: str) -> str | None:
    """Resolve a possibly-relative link against a base URL.

    Returns None if `link` isn't parseable as a URL at all. This matters
    for the best-effort URL-shaped-string scanners over <script> bodies and
    data-* attributes (extract.py) -- a loose regex over arbitrary JS text
    will occasionally match something that merely looks URL-shaped (e.g.
    JS array-index syntax like `//foo[i]`), which urlsplit rejects as a
    malformed IPv6 host. One bad match on one page must not crash the run.
    """
    try:
        return urljoin(base, link)
    except ValueError:
        return None


def _decode_unreserved_percent_encodings(s: str) -> str:
    """Decode %XX sequences that represent RFC 3986 unreserved characters;
    uppercase the hex digits of any encoded triples left alone."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "%" and i + 2 < n and _is_hex(s[i + 1]) and _is_hex(s[i + 2]):
            hex_pair = s[i + 1 : i + 3]
            decoded = chr(int(hex_pair, 16))
            if decoded in _UNRESERVED:
                out.append(decoded)
            else:
                out.append("%" + hex_pair.upper())
            i += 3
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _is_hex(ch: str) -> bool:
    return ch in "0123456789abcdefABCDEF"


def _resolve_dot_segments_and_slashes(path: str) -> str:
    """Collapse duplicate slashes and resolve '.'/'..' segments, preserving
    a trailing slash if one was present."""
    had_trailing_slash = path.endswith("/")
    segments = path.split("/")
    resolved: list[str] = []
    for seg in segments:
        if seg in ("", "."):
            continue
        if seg == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(seg)
    new_path = "/" + "/".join(resolved)
    if had_trailing_slash and not new_path.endswith("/"):
        new_path += "/"
    return new_path


def _is_directory_like(path: str) -> bool:
    last_segment = path.rsplit("/", 1)[-1]
    return "." not in last_segment


def normalize_url(
    url: str,
    profile: SiteProfile,
    *,
    pretty_permalink_known: bool = False,
) -> str:
    """Return the canonical form of `url` given a probed SiteProfile.

    Steps:
    - lowercase scheme/host; upgrade http -> https for the site's own
      host(s), only if the site is known to serve https
    - strip the fragment always
    - strip query strings except p=/page_id=/attachment_id=, and only
      keep those until a pretty permalink is known
    - resolve ./.. segments, collapse duplicate slashes, decode unreserved
      percent-encodings
    - fold www/non-www hosts to the site's canonical host
    - normalize trailing slash to the site's own preference, but only for
      directory-like paths on the site's own host
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port

    owned = profile.owns_host(host)
    if owned:
        # Fold to the canonical host -- but only hosts that are *aliases* of
        # it (the www/non-www pair the site itself redirects between). A
        # configured extra host is a genuinely different server, usually a
        # CDN; rewriting cdn.example.com to example.com invents a URL that
        # does not exist and points every asset at the wrong origin. Never
        # observed in the wild only because no config has ever set
        # extra_hosts -- they are all [], which makes this a no-op for every
        # capture taken so far.
        if host not in profile.extra_hosts:
            host = profile.canonical_host
        if profile.use_https:
            scheme = "https"

    netloc = host
    if port is not None:
        # A port is "default" (and so dropped) if it matches the default
        # for either the original scheme or the scheme we normalized to --
        # otherwise an explicit ":80" surviving an http->https upgrade
        # would wrongly be kept as a non-default port.
        default_ports = {_DEFAULT_PORTS.get(parsed.scheme.lower()), _DEFAULT_PORTS.get(scheme)}
        if str(port) not in default_ports:
            netloc = f"{host}:{port}"

    path = _decode_unreserved_percent_encodings(parsed.path)
    path = _resolve_dot_segments_and_slashes(path)
    if owned and path and _is_directory_like(path):
        if profile.trailing_slash and not path.endswith("/"):
            path += "/"
        elif not profile.trailing_slash and path.endswith("/") and path != "/":
            path = path.rstrip("/")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if pretty_permalink_known:
        kept_pairs: list[tuple[str, str]] = []
    else:
        kept_pairs = [(k, v) for k, v in query_pairs if k in PERMALINK_QUERY_KEYS]
    query = urlencode(kept_pairs)

    return urlunsplit((scheme, netloc, path, query, ""))
