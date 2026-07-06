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
# point at (see CLAUDE-acquire.md, URL normalization / database inventory).
PERMALINK_QUERY_KEYS = frozenset({"p", "page_id", "attachment_id"})

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
    """

    canonical_host: str
    site_hosts: frozenset[str] = field(default_factory=frozenset)
    use_https: bool = True
    trailing_slash: bool = True

    def owns_host(self, host: str) -> bool:
        return host in self.site_hosts or host == self.canonical_host


def resolve_url(base: str, link: str) -> str:
    """Resolve a possibly-relative link against a base URL."""
    return urljoin(base, link)


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

    Steps (see CLAUDE-acquire.md, "URL normalization"):
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
