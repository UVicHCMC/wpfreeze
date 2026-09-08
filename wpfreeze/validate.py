"""Optional HTML/CSS validation gate over a built site, via the Nu Html
Checker (VNU: https://validator.github.io/validator/).

An earlier prototype (Ant + Saxon XSLT + VNU) had rewriting stages that
duplicate what manifest.json already records here, but its VNU validation
gate has no equivalent -- worth keeping, so this just calls the checker as
a subprocess rather than adopting the rest.

resolve_vnu() works out how to do that on this machine: a system `java`
plus the ~32 MB `vnu.jar`, or -- when there is no JVM -- the
self-contained `vnu.linux.zip` runtime image, which bundles its own. A
missing checker is never fatal to an archive; `wpfreeze freeze` skips the
validate step rather than failing when one cannot be obtained.

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
import shutil
import subprocess
import sys
import zipfile
from collections import defaultdict
from collections.abc import Sequence
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

# GitHub's release-alias URLs -- always redirect to whatever the current
# latest release actually is, so there is no version number to pin here.
# vnu.jar (~32 MB) needs a system JVM; vnu.linux.zip (~66 MB) unpacks to a
# self-contained runtime image that does not. See resolve_vnu().
VNU_LATEST_URL = "https://github.com/validator/validator/releases/download/latest/vnu.jar"
VNU_NATIVE_URL = "https://github.com/validator/validator/releases/download/latest/vnu.linux.zip"

# vnu.linux.zip's single top-level entry.
_NATIVE_IMAGE_DIRNAME = "vnu-runtime-image"


class VnuUnavailable(Exception):
    """VNU could not be obtained or run, or its output could not be parsed
    -- a setup/environment problem (no JVM and no reachable download, a bad
    `vnu_jar:` path, a corrupt jar), not a validation result."""


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

    logger.info("downloading the HTML checker vnu.jar (~32 MB) to %s -- first use, or a new release", jar_path)
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


def _unpack_runtime_image(zip_path: Path, cache_dir: Path) -> Path:
    """Unpack vnu.linux.zip (whose sole top-level entry is
    ``vnu-runtime-image/``) into ``cache_dir``, replacing any previous
    copy, and return the path to its ``bin/vnu`` launcher.

    Extraction goes via a ``.new`` staging directory so a failure part way
    through never leaves a half-written image where the old one was.
    ``zipfile`` does not restore unix permission bits, so the executable
    bit is reapplied wherever the archive recorded one -- without it the
    launcher chain (``bin/vnu`` -> ``bin/java`` -> ``lib/jspawnhelper``)
    comes out non-runnable.
    """
    image_dir = cache_dir / _NATIVE_IMAGE_DIRNAME
    staging = cache_dir / (_NATIVE_IMAGE_DIRNAME + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging)
            for info in zf.infolist():
                if (info.external_attr >> 16) & 0o111:
                    target = staging / info.filename
                    target.chmod(target.stat().st_mode | 0o111)
        new_image = staging / _NATIVE_IMAGE_DIRNAME
        if not new_image.is_dir():
            raise VnuUnavailable(f"vnu.linux.zip did not contain a {_NATIVE_IMAGE_DIRNAME}/ directory")
        if image_dir.exists():
            shutil.rmtree(image_dir)
        new_image.replace(image_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return image_dir / "bin" / "vnu"


def ensure_vnu_native(
    session: requests.Session | None = None,
    cache_dir: Path | None = None,
    url: str = VNU_NATIVE_URL,
    timeout: float = 30.0,
) -> Path:
    """Return a path to a working ``vnu`` launcher from the self-contained
    ``vnu.linux.zip`` runtime image -- no system JVM required -- downloading
    and unpacking it, or refreshing a cached copy, as needed.

    Same freshness model as :func:`ensure_vnu_jar` (a conditional GET
    against the saved ETag) and the same offline fallback: a stale unpacked
    image still beats no checker at all.

    Linux only -- the URL names one platform's build. wpfreeze is a
    Linux tool (see the classifiers in pyproject.toml), but this path is
    reached precisely when the machine has no JVM, and downloading 66 MB
    of Linux binaries to fail with "Exec format error" is a worse way to
    learn that than being told.
    """
    if not sys.platform.startswith("linux"):
        raise VnuUnavailable(
            "the self-contained checker wpfreeze can fetch (vnu.linux.zip) is Linux-only -- on "
            f"{sys.platform}, install a JRE (17+) able to run vnu.jar, or point `vnu_jar:` at a "
            "`vnu` executable"
        )
    cache_dir = cache_dir or _default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    image_dir = cache_dir / _NATIVE_IMAGE_DIRNAME
    launcher = image_dir / "bin" / "vnu"
    meta_path = cache_dir / "vnu.linux.zip.meta.json"
    session = session or requests.Session()

    meta = {}
    if meta_path.exists() and launcher.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}

    headers = {"If-None-Match": meta["etag"]} if meta.get("etag") else {}

    try:
        response = session.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        if launcher.exists():
            logger.warning("could not check for a newer bundled VNU (%s); using cached copy at %s", exc, launcher)
            return launcher
        raise VnuUnavailable(f"could not download vnu.linux.zip and no cached copy exists: {exc}") from exc

    if response.status_code == 304 and launcher.exists():
        response.close()
        logger.info("bundled VNU is up to date (%s)", meta.get("last_modified", "unknown release date"))
        return launcher

    if response.status_code != 200:
        response.close()
        if launcher.exists():
            logger.warning(
                "could not check for a newer bundled VNU (HTTP %d); using cached copy at %s",
                response.status_code,
                launcher,
            )
            return launcher
        raise VnuUnavailable(f"could not download vnu.linux.zip: HTTP {response.status_code}")

    logger.info(
        "downloading the self-contained HTML checker (~66 MB, bundles its own Java runtime) to %s "
        "-- first use, or a new release",
        image_dir,
    )
    tmp_zip = cache_dir / "vnu.linux.zip.part"
    try:
        with tmp_zip.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                fh.write(chunk)
    except requests.RequestException as exc:
        tmp_zip.unlink(missing_ok=True)
        if launcher.exists():
            logger.warning("download of a newer bundled VNU failed (%s); using cached copy at %s", exc, launcher)
            return launcher
        raise VnuUnavailable(f"could not download vnu.linux.zip: {exc}") from exc
    finally:
        response.close()

    try:
        _unpack_runtime_image(tmp_zip, cache_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        tmp_zip.unlink(missing_ok=True)
        if launcher.exists():
            logger.warning("newly downloaded vnu.linux.zip could not be unpacked (%s); using cached copy at %s", exc, launcher)
            return launcher
        raise VnuUnavailable(f"downloaded vnu.linux.zip could not be unpacked: {exc}") from exc
    tmp_zip.unlink(missing_ok=True)

    if not launcher.is_file():
        raise VnuUnavailable(
            f"vnu.linux.zip unpacked but no launcher at {launcher.relative_to(cache_dir)} -- "
            "the archive layout may have changed"
        )

    new_meta = {
        "etag": response.headers.get("ETag", ""),
        "last_modified": response.headers.get("Last-Modified", ""),
    }
    meta_path.write_text(json.dumps(new_meta), encoding="utf-8")
    logger.info("downloaded bundled VNU (%s) to %s", new_meta["last_modified"] or "unknown release date", image_dir)
    return launcher


def _vnu_command(spec: Path | str | Sequence[str]) -> list[str]:
    """Normalise a checker spec into an argv prefix. A ``.jar`` path
    becomes a ``java -jar`` invocation; any other single path is taken as a
    ``vnu`` executable (the launcher unpacked from ``vnu.linux.zip``, or a
    distro-packaged one) and run directly; a sequence is used verbatim.
    """
    if isinstance(spec, (str, Path)):
        path = Path(spec)
        if path.suffix.lower() == ".jar":
            return ["java", "-jar", str(path)]
        return [str(path)]
    return list(spec)


def _checker_failure(cmd: list[str], timeout: float = 60.0) -> str | None:
    """None if `cmd` can actually run VNU on this machine; otherwise a
    short reason why it cannot.

    A one-off `--version` run, because `java` being on PATH says nothing
    about whether *that* java can run *this* jar: a JVM older than the
    current release's target fails with an UnsupportedClassVersionError
    deep inside _run_vnu, by which point the validation has already been
    called a failure and the checker that would have worked was never
    tried. One JVM start (well under a second) per `validate`, against a
    step that then walks the whole site, buys resolve_vnu the right to
    promise that what it returns runs.
    """
    try:
        result = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as exc:
        return str(exc)
    except subprocess.TimeoutExpired:
        return f"no answer within {timeout:.0f}s"
    if result.returncode == 0:
        return None
    detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
    return detail[0] if detail else f"exit code {result.returncode}"


def resolve_vnu(
    pinned: Path | None = None,
    *,
    session: requests.Session | None = None,
    cache_dir: Path | None = None,
) -> list[str]:
    """Work out how to run VNU on this machine and return the argv prefix
    for it (``["java", "-jar", <jar>]`` or ``[<vnu launcher>]``), fetching
    a checker if none is cached. Raises :class:`VnuUnavailable` if no
    working checker can be obtained.

    Resolution order:

    1. An explicit ``vnu_jar:`` from the config. A path ending ``.jar`` is
       run with ``java`` (which must then be on ``PATH``); any other path
       is treated as a ``vnu`` executable and run directly.
    2. Otherwise, if ``java`` is on ``PATH`` *and can actually run the
       jar*: the auto-managed ~32 MB ``vnu.jar``
       (:func:`ensure_vnu_jar`), run with ``java``.
    3. Otherwise: the self-contained ~66 MB ``vnu.linux.zip`` runtime
       image (:func:`ensure_vnu_native`), which needs no system JVM.

    Every branch is verified with :func:`_checker_failure` before being
    returned, so a caller can treat a returned command as one that runs.
    That is what lets `wpfreeze freeze` decide whether to skip the step
    from the resolution result alone, rather than discovering a dead JVM
    only once validation is already under way.
    """
    if pinned is not None:
        pinned = Path(pinned)
        if not pinned.exists():
            raise VnuUnavailable(f"vnu_jar points at {pinned}, which does not exist")
        if pinned.suffix.lower() == ".jar":
            if shutil.which("java") is None:
                raise VnuUnavailable(
                    f"vnu_jar points at {pinned} but no `java` is on PATH to run it -- install a "
                    "JRE (17+), or point vnu_jar at a `vnu` executable instead"
                )
            cmd = ["java", "-jar", str(pinned)]
        elif not os.access(pinned, os.X_OK):
            raise VnuUnavailable(f"vnu_jar points at {pinned}, which is not an executable `vnu` launcher")
        else:
            cmd = [str(pinned)]
        # No silent fallback for a pin: someone who named a checker wants
        # that one, and a 66 MB download behind their back is not a fix.
        failure = _checker_failure(cmd)
        if failure is not None:
            raise VnuUnavailable(f"vnu_jar points at {pinned}, which could not be run: {failure}")
        return cmd

    if shutil.which("java") is not None:
        jar_cmd = ["java", "-jar", str(ensure_vnu_jar(session=session, cache_dir=cache_dir))]
        failure = _checker_failure(jar_cmd)
        if failure is None:
            return jar_cmd
        # A JVM too old for the current release, or a broken install. The
        # self-contained image carries its own runtime, so it is worth the
        # download rather than failing here.
        logger.warning(
            "the system java could not run vnu.jar (%s); falling back to the self-contained checker", failure
        )

    native_cmd = [str(ensure_vnu_native(session=session, cache_dir=cache_dir))]
    failure = _checker_failure(native_cmd)
    if failure is not None:
        raise VnuUnavailable(f"the self-contained checker at {native_cmd[0]} could not be run: {failure}")
    return native_cmd


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


def _run_vnu(vnu_cmd: list[str], site_dir: Path, args: tuple[str, ...] = _VNU_HTML_ARGS) -> list[dict]:
    """One VNU invocation over the whole site directory; VNU walks it
    itself (`--skip-non-html` restricts the walk to .html/.htm/.xhtml/.xht).
    Returns the raw `messages` list from its JSON report.

    `vnu_cmd` is the argv prefix from :func:`resolve_vnu` -- either
    `["java", "-jar", <jar>]` or `[<vnu launcher>]`.
    """
    try:
        result = subprocess.run(
            [*vnu_cmd, *args, str(site_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise VnuUnavailable(f"could not launch VNU ({' '.join(vnu_cmd)}): {exc}") from exc

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


def validate_site(vnu: Path | str | Sequence[str], site_dir: Path) -> ValidationReport:
    """Run VNU over every HTML document in `site_dir` and group the
    resulting errors by distinct message text.

    `vnu` is either an argv prefix from :func:`resolve_vnu`, or a bare path
    -- a `.jar` (run via `java`) or a `vnu` executable (run directly).
    """
    vnu_cmd = _vnu_command(vnu)
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

    messages = _run_vnu(vnu_cmd, resolved, _VNU_HTML_ARGS)
    if stylesheets_checked:
        messages += _run_vnu(vnu_cmd, resolved, _VNU_CSS_ARGS)

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
