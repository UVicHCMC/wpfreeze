"""External link checking: which built pages link off-site to something
now broken.

Deliberately two-phase and decoupled, unlike verify_site's local-reference
check (which scans and resolves in one pass because both sides -- the
reference and the file it must resolve against -- are on local disk and
cheap to re-read every time). Here the two sides are not symmetric:
`extract_external_links` (scanning the built site for `<a href>` targets
that leave the site) is a cheap, offline, repeatable operation, while
`check_links` (asking each of those URLs whether it still resolves) is a
slow, network-bound one hitting hosts wpfreeze does not own or control.
Persisting the extraction to `external-links.json` (see write_links/
load_links) means a site owner can re-run the live check on a later day
-- link rot accrues after the archive is made, not just at build time --
without needing the built site on disk at all, let alone re-running
`build`. See cli.py's `run_checklinks` for both entry points.

Only `<a href>` is in scope: img/script/css externals are already handled
by build's own localization policy (see outputs.py), and `<a href>` is
what a site owner's reader would actually click.
"""
from __future__ import annotations

import html
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from wpfreeze.fetch import SUCCESS, FetchConfig, FetchOutcome, RateLimiter, fetch_with_retries
from wpfreeze.manifest import FLAG_AUTH_GATED, FLAG_RETRY_EXHAUSTED
from wpfreeze.urlnorm import SiteProfile

if TYPE_CHECKING:
    from wpfreeze.progress import Progress

logger = logging.getLogger(__name__)

LINKS_FILENAME = "external-links.json"
REPORT_BASENAME = "broken-external-links"

_SKIPPED_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")

# Same palette/heading treatment as cleanup.py's own _CSS, trimmed to what
# this document needs -- copied rather than imported so this module doesn't
# reach into cleanup.py's private constant (see cleanup.py's own comment
# for why that's the house style here).
_CSS = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; max-width: 60rem; }
a { color: #1a56c4; }
a:visited { color: #7a3fa0; }
a:hover { text-decoration: underline; }
h1, h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }
p { line-height: 1.5; }
ul { line-height: 1.6; }
code { font-family: ui-monospace, Menlo, Consolas, monospace; background: #f4f4f4; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }
@media (prefers-color-scheme: dark) {
  body { background: #1e1e1e; color: #ddd; }
  a { color: #7db3ff; }
  a:visited { color: #d3a6f0; }
  h1, h2 { border-bottom-color: #444; }
  code { background: #2a2a2a; }
}
"""


@dataclass
class ExternalLink:
    url: str
    pages: list[str] = field(default_factory=list)


@dataclass
class LinkCheckResult:
    url: str
    pages: list[str]
    ok: bool
    status: int | None
    reason: str | None


def extract_external_links(site_dir: Path, profile: SiteProfile) -> list[ExternalLink]:
    """Every `<a href>` in the built site that points off-site, grouped by
    target URL with the pages (site-relative output paths) that link to it.

    A link is "off-site" per `profile.in_scope` -- the same offline scope
    test `build` itself uses to decide what stays absolute -- not by
    inspecting build-report.json, so this works even if build-report.json
    is stale or missing (e.g. `build` ran with a different policy since)."""
    by_url: dict[str, list[str]] = {}
    for document in sorted(site_dir.rglob("*")):
        if not document.is_file() or document.suffix.lower() not in (".html", ".htm"):
            continue
        page = document.relative_to(site_dir).as_posix()
        text = document.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(text, "html5lib")
        for tag in soup.find_all("a"):
            href = (tag.get("href") or "").strip()
            if not href or href.startswith("#") or href.lower().startswith(_SKIPPED_SCHEMES):
                continue
            if href.startswith("//"):
                href = f"https:{href}"
            elif not urlsplit(href).scheme:
                continue  # relative -- internal by construction after build
            url = href.split("#", 1)[0]
            if profile.in_scope(url):
                continue
            pages = by_url.setdefault(url, [])
            if page not in pages:
                pages.append(page)
    return [ExternalLink(url=url, pages=sorted(pages)) for url, pages in sorted(by_url.items())]


def write_links(links: list[ExternalLink], output_dir: Path, base_url: str) -> Path:
    path = output_dir / LINKS_FILENAME
    data = {
        "base_url": base_url,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "links": [{"url": link.url, "pages": link.pages} for link in links],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def load_links(output_dir: Path) -> list[ExternalLink] | None:
    """The persisted extraction, or None if it doesn't exist or is
    unreadable -- callers treat both as "run the non-recheck path first",
    same convention as cleanup.py's _load_json."""
    path = output_dir / LINKS_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return [ExternalLink(url=entry["url"], pages=list(entry.get("pages", []))) for entry in data.get("links", [])]


def _describe_failure(outcome: FetchOutcome) -> str:
    if outcome.error is not None:
        return f"unreachable ({outcome.error.splitlines()[0][:120]})"
    if outcome.flag == FLAG_AUTH_GATED:
        return f"HTTP {outcome.http_status} (auth-gated -- may not actually be broken)"
    if outcome.flag == FLAG_RETRY_EXHAUSTED:
        return f"HTTP {outcome.http_status} (retries exhausted)"
    return f"HTTP {outcome.http_status}"


def check_links(
    links: list[ExternalLink],
    user_agent: str,
    rate_limit: float,
    workers: int,
    progress: "Progress | None" = None,
) -> list[LinkCheckResult]:
    """Ask each unique URL in `links` whether it still resolves, respecting
    the same per-host politeness/backoff `acquire` uses (see fetch.py) --
    these are hosts wpfreeze doesn't own, so a link-checker hammering them
    is exactly the behaviour that module's 429 handling exists to avoid."""
    fetch_config = FetchConfig(user_agent=user_agent)
    rate_limiter = RateLimiter(rate_limit)
    session = requests.Session()
    results: dict[str, LinkCheckResult] = {}
    if progress is not None:
        progress.phase("Checking external links", total=len(links))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(fetch_with_retries, link.url, session, rate_limiter, fetch_config): link
            for link in links
        }
        for future in as_completed(futures):
            link = futures[future]
            outcome = future.result()
            ok = outcome.category == SUCCESS
            results[link.url] = LinkCheckResult(
                url=link.url,
                pages=link.pages,
                ok=ok,
                status=outcome.http_status,
                reason=None if ok else _describe_failure(outcome),
            )
            if progress is not None:
                progress.tick(detail=link.url)
    return [results[link.url] for link in links]


def _broken_by_page(results: list[LinkCheckResult]) -> dict[str, list[LinkCheckResult]]:
    by_page: dict[str, list[LinkCheckResult]] = {}
    for result in results:
        if result.ok:
            continue
        for page in result.pages:
            by_page.setdefault(page, []).append(result)
    return by_page


def render_report_markdown(results: list[LinkCheckResult], checked_at: str) -> str:
    broken = [r for r in results if not r.ok]
    by_page = _broken_by_page(results)
    lines = [
        "# Broken external links",
        "",
        f"Checked {len(results)} unique external link(s) on {checked_at}.",
        "",
    ]
    if not broken:
        lines += ["No broken external links found.", ""]
        return "\n".join(lines)
    lines += [f"{len(broken)} broken, across {len(by_page)} page(s):", ""]
    for page in sorted(by_page):
        lines.append(f"## {page}")
        lines.append("")
        for result in sorted(by_page[page], key=lambda r: r.url):
            lines.append(f"- `{result.url}` -- {result.reason}")
        lines.append("")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return html.escape(text)


def render_report_html(results: list[LinkCheckResult], checked_at: str) -> str:
    broken = [r for r in results if not r.ok]
    by_page = _broken_by_page(results)
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        "<title>Broken external links</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>Broken external links</h1>",
        f"<p>Checked {len(results)} unique external link(s) on {_escape(checked_at)}.</p>",
    ]
    if not broken:
        parts.append("<p>No broken external links found.</p>")
    else:
        parts.append(f"<p>{len(broken)} broken, across {len(by_page)} page(s):</p>")
        for page in sorted(by_page):
            parts.append(f"<h2>{_escape(page)}</h2>")
            parts.append("<ul>")
            for result in sorted(by_page[page], key=lambda r: r.url):
                parts.append(
                    f'<li><code>{_escape(result.url)}</code> -- {_escape(result.reason or "")}</li>'
                )
            parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write_report(results: list[LinkCheckResult], output_dir: Path, checked_at: str) -> tuple[Path, Path]:
    md_path = output_dir / f"{REPORT_BASENAME}.md"
    html_path = output_dir / f"{REPORT_BASENAME}.html"
    md_path.write_text(render_report_markdown(results, checked_at), encoding="utf-8")
    html_path.write_text(render_report_html(results, checked_at), encoding="utf-8")
    return md_path, html_path
