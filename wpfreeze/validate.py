"""Optional HTML/CSS validation gate over a built site, via the Nu Html
Checker (VNU: https://validator.github.io/validator/).

Evaluated as part of the `rescueTagSoup` spike (Ant + Saxon XSLT + VNU):
its rewriting stages duplicate what manifest.json already records, but
its VNU validation gate has no equivalent here. Not worth a fork -- just
call the jar.

Errors are grouped by message text, not reported per-occurrence. A
WordPress/Divi-style theme repeats the same handful of template-wide
markup defects (a builder-injected <style> in <body>, an obsolete "td
width" attribute) on every single page; reporting one of those 218 times
is the same false signal as the link-rewriter's raw "unresolved" count
being inflated by a shared nav linking to one dead page from every
template. What's worth a human's attention is the distinct issue and how
many pages it touches, not the raw occurrence count.

These are markup defects in the *original* site's theme/plugins -- not
something wpfreeze's rewriting introduced or can fix -- so this is
reported, not gated: it never affects `wpfreeze build`'s exit code.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

logger = logging.getLogger(__name__)

# Two passes, because VNU's directory walk is filtered by extension and the
# filters are mutually exclusive. Checking stylesheets is not optional
# extra credit: build.py lifts CSS repeated across pages out of the markup
# and into shared .css files, so an HTML-only pass would stop seeing
# defects it used to report simply because the bytes moved.
_VNU_BASE_ARGS = ("--format", "json", "--errors-only", "--stdout")
_VNU_HTML_ARGS = (*_VNU_BASE_ARGS, "--skip-non-html")
_VNU_CSS_ARGS = (*_VNU_BASE_ARGS, "--skip-non-css")
_MAX_ISSUES_SHOWN = 10

# GitHub's release-alias URL for the jar -- always resolves (redirect) to
# whatever the current latest release actually is, so there is no version
# number to pin here.
VNU_LATEST_URL = "https://github.com/validator/validator/releases/download/latest/vnu.jar"


class VnuUnavailable(Exception):
    """VNU could not be run or its output could not be parsed -- a setup
    problem (java missing, bad jar path), not a validation result."""


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return Path(base) / "wpfreeze" if base else Path.home() / ".cache" / "wpfreeze"


def ensure_vnu_jar(
    session: requests.Session | None = None,
    cache_dir: Path | None = None,
    url: str = VNU_LATEST_URL,
    timeout: float = 30.0,
) -> Path:
    """Return a path to a working vnu.jar, downloading or refreshing a
    cached copy as needed. Shared across all site configs -- the jar isn't
    site-specific -- and reused between runs via a conditional GET (an
    If-None-Match against the ETag saved alongside it), so a validate run
    that finds nothing new to fetch costs one small request, not a
    multi-megabyte re-download.

    Falls back to a stale cached copy (with a warning) if the freshness
    check itself fails -- e.g. offline -- since a slightly outdated
    checker still beats none at all.
    """
    cache_dir = cache_dir or _default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    jar_path = cache_dir / "vnu.jar"
    meta_path = cache_dir / "vnu.jar.meta.json"
    session = session or requests.Session()

    meta = {}
    if meta_path.exists() and jar_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}

    headers = {"If-None-Match": meta["etag"]} if meta.get("etag") else {}

    try:
        response = session.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        if jar_path.exists():
            logger.warning("could not check for a newer vnu.jar (%s); using cached copy at %s", exc, jar_path)
            return jar_path
        raise VnuUnavailable(f"could not download vnu.jar and no cached copy exists: {exc}") from exc

    if response.status_code == 304 and jar_path.exists():
        response.close()
        logger.info("vnu.jar is up to date (%s)", meta.get("last_modified", "unknown release date"))
        return jar_path

    if response.status_code != 200:
        response.close()
        if jar_path.exists():
            logger.warning(
                "could not check for a newer vnu.jar (HTTP %d); using cached copy at %s",
                response.status_code,
                jar_path,
            )
            return jar_path
        raise VnuUnavailable(f"could not download vnu.jar: HTTP {response.status_code}")

    tmp_path = jar_path.with_suffix(".jar.tmp")
    try:
        with tmp_path.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                fh.write(chunk)
    except requests.RequestException as exc:
        # Same fallback as the request above, for the same reason -- and this
        # is the likelier of the two to fire, since it spans a 32MB transfer
        # rather than one handshake. The cached jar is untouched until the
        # replace() below, so a stale checker is still available.
        tmp_path.unlink(missing_ok=True)
        if jar_path.exists():
            logger.warning("download of a newer vnu.jar failed (%s); using cached copy at %s", exc, jar_path)
            return jar_path
        raise VnuUnavailable(f"could not download vnu.jar: {exc}") from exc
    finally:
        response.close()
    tmp_path.replace(jar_path)

    new_meta = {
        "etag": response.headers.get("ETag", ""),
        "last_modified": response.headers.get("Last-Modified", ""),
    }
    meta_path.write_text(json.dumps(new_meta), encoding="utf-8")
    logger.info("downloaded latest vnu.jar (%s) to %s", new_meta["last_modified"] or "unknown release date", jar_path)
    return jar_path


@dataclass
class ValidationIssue:
    message: str
    count: int
    pages: list[str]
    sample_extract: str


@dataclass
class ValidationReport:
    documents_checked: int
    total_messages: int
    issues: list[ValidationIssue] = field(default_factory=list)
    documents_unreadable: list[str] = field(default_factory=list)
    stylesheets_checked: int = 0

    @property
    def documents_found(self) -> int:
        """HTML documents present in the site directory, whether or not VNU
        managed to read them."""
        return self.documents_checked + len(self.documents_unreadable)

    @property
    def ok(self) -> bool:
        return not self.issues and not self.documents_unreadable


def _run_vnu(vnu_jar: Path, site_dir: Path, args: tuple[str, ...] = _VNU_HTML_ARGS) -> list[dict]:
    """One JVM invocation over the whole site directory; VNU walks it
    itself (`--skip-non-html` restricts the walk to .html/.htm/.xhtml/.xht).
    Returns the raw `messages` list from its JSON report.
    """
    try:
        result = subprocess.run(
            ["java", "-jar", str(vnu_jar), *args, str(site_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise VnuUnavailable(f"could not launch java: {exc}") from exc

    # VNU exits non-zero when it reports document errors -- expected, not
    # itself a failure of the checker. An invocation failure (bad jar path,
    # java present but jar corrupt) instead produces no JSON on stdout.
    if not result.stdout.strip():
        detail = result.stderr.strip() or f"exit code {result.returncode}, no output"
        raise VnuUnavailable(f"vnu produced no output ({detail})")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        # VNU dies mid-run on some inputs (a file it cannot open throws an
        # uncaught FileNotFoundException) and leaves its JSON truncated
        # mid-structure. The reason is only ever on stderr, so include it:
        # "could not parse vnu output as JSON" alone sends the reader
        # looking for a bug in the parser rather than at their own file.
        detail = result.stderr.strip().splitlines()
        why = f" -- vnu said: {detail[0]}" if detail else ""
        raise VnuUnavailable(f"could not parse vnu output as JSON: {exc}{why}") from exc

    return data.get("messages", [])


_HTML_SUFFIXES = (".html", ".htm", ".xhtml", ".xht")
_CSS_SUFFIXES = (".css",)


def _scan_documents(site_dir: Path, suffixes: tuple[str, ...] = _HTML_SUFFIXES) -> tuple[int, list[str]]:
    """(readable documents, site-relative paths of unreadable ones).

    VNU is never asked which files it checked and its JSON carries no such
    list -- a clean document produces no messages at all, so the set cannot
    be reconstructed from the output either. The count therefore comes from
    the filesystem, which means it has to agree with what VNU will actually
    manage to open.

    Confirmed against VNU 26.7.22: a file it lacks permission to read kills
    the whole run with an uncaught java.io.FileNotFoundException, and the
    JSON on stdout is left truncated mid-structure. So an unreadable
    document is not a document that goes unchecked -- it is a document that
    stops every other document from being checked, and it must be caught
    before the JVM is launched or the only symptom is an unparseable
    report.

    Also filters to regular files: rglob("*") yields directories too, and a
    directory named `foo.html` is not a document.
    """
    checked = 0
    unreadable: list[str] = []
    for path in sorted(site_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if os.access(path, os.R_OK):
            checked += 1
        else:
            unreadable.append(path.relative_to(site_dir).as_posix())
    return checked, unreadable


def _page_relpath(file_url: str, site_dir: Path) -> str:
    path = Path(unquote(urlsplit(file_url).path))
    try:
        return path.relative_to(site_dir).as_posix()
    except ValueError:
        return path.as_posix()


def validate_site(vnu_jar: Path, site_dir: Path) -> ValidationReport:
    """Run VNU over every HTML document in `site_dir` and group the
    resulting errors by distinct message text.
    """
    resolved = site_dir.resolve()

    # Scan before launching the JVM: an unreadable document aborts the
    # entire VNU run, so finding it first turns "could not parse vnu output
    # as JSON" into something the reader can act on.
    documents_checked, documents_unreadable = _scan_documents(resolved, _HTML_SUFFIXES)
    stylesheets_checked, css_unreadable = _scan_documents(resolved, _CSS_SUFFIXES)
    documents_unreadable = documents_unreadable + css_unreadable
    if documents_unreadable:
        shown = ", ".join(documents_unreadable[:5])
        more = len(documents_unreadable) - 5
        raise VnuUnavailable(
            f"{len(documents_unreadable)} document(s) cannot be read, which aborts the whole "
            f"VNU run rather than skipping them: {shown}"
            + (f" (+{more} more)" if more > 0 else "")
            + ". Fix the permissions and re-run."
        )

    messages = _run_vnu(vnu_jar, resolved, _VNU_HTML_ARGS)
    if stylesheets_checked:
        messages += _run_vnu(vnu_jar, resolved, _VNU_CSS_ARGS)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for message in messages:
        if message.get("type") != "error":
            continue
        grouped[message["message"]].append(message)

    issues = [
        ValidationIssue(
            message=text,
            count=len(items),
            pages=sorted({_page_relpath(item["url"], resolved) for item in items if item.get("url")}),
            sample_extract=items[0].get("extract", ""),
        )
        for text, items in grouped.items()
    ]
    issues.sort(key=lambda issue: -issue.count)

    return ValidationReport(
        documents_checked=documents_checked,
        total_messages=sum(len(items) for items in grouped.values()),
        issues=issues,
        documents_unreadable=documents_unreadable,
        stylesheets_checked=stylesheets_checked,
    )


def write_validation_report(report: ValidationReport, output_dir: Path) -> Path:
    path = output_dir / "vnu-report.json"
    data = {
        "documents_found": report.documents_found,
        "documents_checked": report.documents_checked,
        "documents_unreadable": report.documents_unreadable,
        "stylesheets_checked": report.stylesheets_checked,
        "total_messages": report.total_messages,
        "distinct_issues": len(report.issues),
        "issues": [
            {
                "message": issue.message,
                "count": issue.count,
                "pages": issue.pages,
                "sample_extract": issue.sample_extract,
            }
            for issue in report.issues
        ],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def format_validation_summary(report: ValidationReport) -> str:
    lines = [
        "Validation (VNU):",
        f"  {report.documents_checked} of {report.documents_found} document(s) and "
        f"{report.stylesheets_checked} stylesheet(s) checked, "
        f"{report.total_messages} message(s), {len(report.issues)} distinct issue(s)",
    ]
    if report.documents_unreadable:
        # VNU skips these silently, so without saying so here the summary
        # would report a verdict it never actually reached for them.
        shown = ", ".join(report.documents_unreadable[:3])
        more = len(report.documents_unreadable) - 3
        lines.append(
            f"  {len(report.documents_unreadable)} document(s) could not be read and were "
            f"NOT checked: {shown}" + (f" (+{more} more)" if more > 0 else "")
        )
    if not report.issues:
        lines.append("  no errors")
        return "\n".join(lines)

    for issue in report.issues[:_MAX_ISSUES_SHOWN]:
        lines.append(
            f"  {issue.count:5d}x  {issue.message[:100]}  "
            f"({len(issue.pages)} page(s){f', e.g. {issue.pages[0]}' if issue.pages else ''})"
        )
    if len(report.issues) > _MAX_ISSUES_SHOWN:
        lines.append(f"  ... and {len(report.issues) - _MAX_ISSUES_SHOWN} more distinct issue(s); see vnu-report.json")
    return "\n".join(lines)
