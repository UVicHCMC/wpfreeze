"""CLI entry point: argument parsing, YAML config loading/validation, and
pipeline orchestration for `wpfreeze acquire|report|status`.

See CLAUDE-acquire.md, "CLI" and "Configuration (YAML)".
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml

from wpfreeze.analyse import (
    apply_canonical_cascade,
    flag_ambiguous_canonical,
    flag_attachment_pages,
    flag_forms_and_plugin_markup,
    flag_hash_duplicates,
    flag_orphans_and_unlisted,
    flag_xml_unresolved,
)
from wpfreeze.build import (
    build_site,
    format_build_summary,
    format_verify_summary,
    verify_site,
    write_build_report,
)
from wpfreeze.cleanup import write_cleanup_todo, write_cleanup_todo_html
from wpfreeze.crawl import compile_exclusions, crawl_fixpoint
from wpfreeze.diagnostics import build_diagnostics, format_diagnostics_summary, write_diagnostics
from wpfreeze.fetch import DEFAULT_USER_AGENT, FetchConfig, RateLimiter
from wpfreeze.inventory import discover_inventory
from wpfreeze.linkcheck import check_links, extract_external_links, load_links, write_links, write_report
from wpfreeze.manifest import Manifest, Status, utc_now
from wpfreeze.outputs import compute_output_paths, generate_redirects_htaccess
from wpfreeze.policy import Policy
from wpfreeze.report import write_report_html, write_report_json
from wpfreeze.rescan import format_rescan_summary, rescan
from wpfreeze.runlock import RunLock, RunLockHeld
from wpfreeze.search import (
    SearchUnavailable,
    format_content_issues_summary,
    format_search_summary,
    run_pagefind_index,
    scan_content_issues,
)
from wpfreeze.upload import write_upload_script
from wpfreeze.urlnorm import SiteProfile, scope_profile_from_config
from wpfreeze.validate import (
    VnuUnavailable,
    ensure_vnu_jar,
    format_validation_summary,
    validate_site,
    write_validation_report,
)
from wpfreeze.wayback import recover_via_wayback

logger = logging.getLogger(__name__)

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class WaybackSettings:
    enabled: bool = True
    prefer_snapshots_near: date = field(default_factory=date.today)


@dataclass(frozen=True)
class UploadSettings:
    # rsync destinations for upload.sh -- e.g. "user@host:/var/www/html".
    # Neither is used by `build`, which has no upload-related side effects;
    # both are read only by `wpfreeze upload-script`/`upload.sh` itself.
    #
    # `remote` is the staging/preview destination: upload.sh's default,
    # no-flag invocation syncs the built site plus its human-facing reports
    # (report.html, cleanup-todo.html, broken-external-links.html) there,
    # for a site owner to read alongside the pages they describe.
    remote: str | None = None
    # `prod_remote` is the real production destination: upload.sh --prod
    # syncs the built site *only* -- no reports -- there, after a
    # confirmation prompt (see upload.py's _TEMPLATE for why the asymmetry:
    # reports are meant for review, never for the live site). Independent
    # of `remote` -- either, both, or neither can be set; upload.sh always
    # gets written regardless (see run_upload_script), since its --local
    # mode needs no remote at all.
    prod_remote: str | None = None


# load_config's default for `search.body_selectors` when a site's config
# omits the key entirely -- WP core's own body_class()/the_content()
# conventions (wp-singular vs. archive/category/blog, wrapped in
# .entry-content), which hold for most non-page-builder themes. Real-world
# discovery, not a guess made in the abstract: found by comparing an
# untuned site-a.example index against a tuned one -- the untuned
# default let WordPress's own archive/category/blog-listing templates
# (which re-embed each post's full .entry-content as a teaser, also
# wrapped in its own <article>) compete with the real page in results,
# e.g. a "grants" query returning the same post's excerpt 5 times over
# under different archive-page titles. `body.wp-singular` is the load-
# bearing half -- it is WP core's own singular/archive distinction and
# excludes those listing pages outright; `.entry-content` narrows further
# within a matching page and also happens to exclude WordPress's own
# comment thread (`#comments` sits outside it). An explicit
# `body_selectors: []` in a site's config opts back into the old
# whole-<body> behaviour; leaving the key out entirely gets this instead.
# NOT a live per-site detection scheme -- CLAUDE-search.md sec 13
# explicitly rules that out ("same class of problem as guessing a
# theme's content container by name") and asks for exactly this instead:
# "a structural default plus an explicit per-site escape hatch, with the
# checklist telling the owner when the default is hurting them." A page-
# builder theme (Divi, Elementor) that never emits `.entry-content` still
# fails safely -- the page just matches nothing and shows up in the
# cleanup checklist's "Pages not covered by search", not silently.
DEFAULT_BODY_SELECTORS: tuple[str, ...] = ("body.wp-singular .entry-content",)


@dataclass(frozen=True)
class SearchSettings:
    # Master switch for offline search (Pagefind). Off by default -- see
    # CLAUDE-search.md. Requires a search form to actually survive the
    # build; see load_config's own check just below.
    enabled: bool = False
    # CSS selectors marking a page's real content for indexing. Beyond
    # narrowing *what* gets indexed on a matching page, setting this to
    # anything non-empty makes Pagefind index ONLY pages that match at
    # least one selector -- every other page silently drops out of the
    # index. That is the only per-page exclusion mechanism available (it
    # is how category/tag/attachment chaff gets kept out), and also the
    # footgun: a too-narrow selector empties the index quietly. The build
    # reports the miss count; see search.SearchStats. Bare-constructed
    # default is the empty tuple (whole-<body> fallback) -- load_config
    # applies DEFAULT_BODY_SELECTORS instead when a config omits the key,
    # so this dataclass default only governs direct construction (tests,
    # or callers that bypass load_config).
    body_selectors: tuple[str, ...] = ()
    # CSS selectors to exclude from indexing even inside indexed content
    # (e.g. a repeated "related posts" widget). Passed straight to
    # Pagefind's --exclude-selectors -- no markup mutation needed.
    ignore_selectors: tuple[str, ...] = ()
    # Collapses Pagefind's per-<html lang> index split into one index.
    # Needed when a theme is inconsistent about emitting `lang`, which
    # otherwise silently returns no results from whichever pages fell
    # into the un-forced index.
    force_language: str | None = None
    # Exact output paths (e.g. "/blog.html", matching pages_without_body_
    # match's own format -- not a URL, not a glob) to drop from the index
    # outright, regardless of body_selectors. For pages body_selectors
    # structurally cannot exclude -- a hand-built page-builder "archive"
    # page that is wp-singular with a real .entry-content, indistinguishable
    # by selector from a page that must stay indexed. No load-time
    # validation: like body_selectors/ignore_selectors, an entry that
    # matches nothing is a silent no-op, self-correcting because the page
    # keeps surfacing in scan_content_issues's echo report. See
    # CLAUDE-search-content-checks.md sec 8a.
    exclude_pages: tuple[str, ...] = ()
    # Exact output paths (same format as exclude_pages) of pages already
    # reviewed and confirmed to be legitimately short by design -- a
    # single-image portfolio item, a glossary entry -- rather than broken
    # or truncated content. Unlike exclude_pages, an acknowledged page
    # stays indexed and searchable; only the cleanup checklist's "needs a
    # look" push for it is suppressed. scan_content_issues still reports
    # it under "Pages with thin content" (audit trail, not a silent
    # dismissal), just no longer counted toward that section's
    # needs_attention. No load-time validation, same self-correcting
    # precedent as exclude_pages: an entry that no longer matches a real
    # thin page is simply unused, not an error.
    acknowledged_thin_pages: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteConfig:
    base_url: str
    output_dir: Path
    # Short handle for this project -- what project-addressed commands
    # (`wpfreeze build landscapes`) match against. Non-defaulted would break
    # every existing positional SiteConfig(...) construction in the tests;
    # load_config always sets it for real, defaulting to the config
    # filename's stem when the YAML omits `name:` (see load_config).
    name: str = ""
    rate_limit: float = 1.0
    wayback_rate_limit: float = 3.0
    concurrency: int = 2
    exclusions: list[str] = field(default_factory=list)
    extra_hosts: list[str] = field(default_factory=list)
    user_agent: str = DEFAULT_USER_AGENT
    wayback: WaybackSettings = field(default_factory=WaybackSettings)
    xml_backup: Path | None = None  # optional: a WordPress XML export (WXR) to augment inventory
    policy: Policy = field(default_factory=Policy)  # content stripping for `build`
    vnu_jar: Path | None = None  # optional: pin a specific vnu.jar; unset auto-downloads/caches the latest
    upload: UploadSettings = field(default_factory=UploadSettings)  # upload.sh destination; see `upload-script`
    search: SearchSettings = field(default_factory=SearchSettings)  # offline search (Pagefind); see `search-index`


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def load_config(path: Path) -> SiteConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    from wpfreeze.projects import validate_name  # deferred: avoid a module-level cycle

    # An explicit `name:` (even an invalid one) is validated as given -- an
    # explicit empty string is a mistake to report, not something to
    # silently paper over with the filename-stem fallback. Only an *absent*
    # key falls back; the stem of a real path is always a usable name.
    name_raw = raw.get("name")
    if name_raw is not None:
        try:
            name = validate_name(str(name_raw))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    else:
        name = Path(path).stem

    wayback_raw = raw.get("wayback") or {}
    prefer_near_raw = wayback_raw.get("prefer_snapshots_near")

    concurrency = int(raw.get("concurrency", 2))
    if concurrency < 1:
        raise ConfigError(f"concurrency must be >= 1, got {concurrency}")

    xml_backup_raw = raw.get("xml_backup")

    policy = Policy.from_config(raw.get("policy"))

    search_raw = raw.get("search") or {}
    body_selectors_raw = search_raw.get("body_selectors")
    # Key absent entirely -> DEFAULT_BODY_SELECTORS. Key present, even as
    # an explicit empty list, is a deliberate opt-out and must be honoured
    # as literally "nothing" (whole-<body> fallback), not silently
    # promoted to the default.
    body_selectors = DEFAULT_BODY_SELECTORS if body_selectors_raw is None else tuple(body_selectors_raw or [])
    search = SearchSettings(
        enabled=bool(search_raw.get("enabled", False)),
        body_selectors=body_selectors,
        ignore_selectors=tuple(search_raw.get("ignore_selectors", []) or []),
        force_language=search_raw.get("force_language"),
        exclude_pages=tuple(search_raw.get("exclude_pages", []) or []),
        acknowledged_thin_pages=tuple(search_raw.get("acknowledged_thin_pages", []) or []),
    )
    if search.enabled and policy.strip_forms and policy.strip_search_forms:
        raise ConfigError(
            "search.enabled: true requires the site's search forms to survive the "
            "build; set policy.strip_search_forms: false (or policy.strip_forms: "
            "false) -- otherwise there is no form left to wire up."
        )

    return SiteConfig(
        base_url=raw["base_url"].rstrip("/") + "/",
        output_dir=Path(raw["output_dir"]),
        name=name,
        rate_limit=float(raw.get("rate_limit", 1.0)),
        wayback_rate_limit=float(raw.get("wayback_rate_limit", 3.0)),
        concurrency=concurrency,
        exclusions=list(raw.get("exclusions", []) or []),
        extra_hosts=list(raw.get("extra_hosts", []) or []),
        user_agent=raw.get("user_agent", DEFAULT_USER_AGENT),
        wayback=WaybackSettings(
            enabled=bool(wayback_raw.get("enabled", True)),
            prefer_snapshots_near=_parse_date(prefer_near_raw) if prefer_near_raw else date.today(),
        ),
        xml_backup=Path(xml_backup_raw) if xml_backup_raw else None,
        policy=policy,
        vnu_jar=Path(raw["vnu_jar"]) if raw.get("vnu_jar") else None,
        upload=UploadSettings(
            remote=(raw.get("upload") or {}).get("remote"),
            prod_remote=(raw.get("upload") or {}).get("prod_remote"),
        ),
        search=search,
    )


# ---------------------------------------------------------------------------
# Site probing: establish the SiteProfile once, before anything else fetches
# ---------------------------------------------------------------------------


def _looks_like_ip_or_bare_host(host: str) -> bool:
    return bool(_IP_RE.match(host)) or host.count(".") == 0 or ":" in host


def _probe_https(host: str, port_suffix: str, session: requests.Session, headers: dict, timeout: float) -> bool:
    # HEAD, not GET: only the status code is used, and this is the same
    # URL the real crawl fetches (and tracks) moments later -- a GET here
    # is a full, silent, untracked re-fetch of the homepage that never
    # shows up in the logs. Seen in practice against a site running a
    # full-page cache plugin: hitting "/" twice in quick succession this
    # way is the only thing that made the homepage's URL different from
    # every other URL in the crawl, and its cached content ended up wrong.
    try:
        resp = session.head(f"https://{host}{port_suffix}/", headers=headers, timeout=timeout, allow_redirects=True)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def _probe_www(
    host: str, use_https: bool, port_suffix: str, session: requests.Session, headers: dict, timeout: float
) -> tuple[str, str]:
    """Returns (canonical_host, alternate_www_host)."""
    if _looks_like_ip_or_bare_host(host):
        return host, ""
    scheme = "https" if use_https else "http"
    alternate = host[4:] if host.startswith("www.") else f"www.{host}"
    try:
        # HEAD: only resp.url (the final host after any redirect) is used.
        resp = session.head(f"{scheme}://{alternate}{port_suffix}/", headers=headers, timeout=timeout, allow_redirects=True)
        final_host = urlsplit(resp.url).hostname or host
        return final_host, alternate
    except requests.RequestException:
        return host, alternate


def _probe_trailing_slash(base_url: str, session: requests.Session, headers: dict, timeout: float) -> bool:
    parsed = urlsplit(base_url)
    path = parsed.path or "/"
    if path == "/" or not path.endswith("/"):
        return True  # nothing informative to probe against the bare root; WP's common default
    probe_url = f"{parsed.scheme}://{parsed.netloc}{path.rstrip('/')}"
    try:
        # HEAD: only resp.url (whether a trailing slash got added) is used.
        resp = session.head(probe_url, headers=headers, timeout=timeout, allow_redirects=True)
        return urlsplit(resp.url).path.endswith("/")
    except requests.RequestException:
        return True


def probe_site(
    base_url: str,
    session: requests.Session,
    user_agent: str,
    extra_hosts: list[str],
    timeout: float = 10.0,
) -> SiteProfile:
    """Establish this run's SiteProfile with a handful of one-time probes:
    does the site serve https, does it prefer www or non-www, does it
    prefer a trailing slash. See CLAUDE-acquire.md, "URL normalization"."""
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    port_suffix = f":{parsed.port}" if parsed.port else ""
    headers = {"User-Agent": user_agent}

    use_https = _probe_https(host, port_suffix, session, headers, timeout)
    canonical_host, alternate_host = _probe_www(host, use_https, port_suffix, session, headers, timeout)
    trailing_slash = _probe_trailing_slash(base_url, session, headers, timeout)

    site_hosts = frozenset({h for h in (host, canonical_host, alternate_host, *extra_hosts) if h})
    # base_url is "/"-terminated by load_config, so this is too; fall back to
    # "/" for a bare origin, which makes in_scope() degrade to owns_host().
    base_path = parsed.path or "/"
    if not base_path.endswith("/"):
        base_path += "/"
    if base_path != "/":
        logger.info(
            "site scope: %s under %s (a subdirectory install -- sibling sites on "
            "this host are out of scope)",
            canonical_host,
            base_path,
        )
    return SiteProfile(
        canonical_host=canonical_host,
        site_hosts=site_hosts,
        use_https=use_https,
        trailing_slash=trailing_slash,
        base_path=base_path,
        extra_hosts=frozenset(h.lower() for h in extra_hosts if h),
    )


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def _describe_inventory_sources(manifest, sources: dict[str, bool]) -> str:
    """One line naming what each inventory source actually contributed.

    A record can be claimed by several sources at once -- that overlap is the
    whole point of cross-checking them -- so these counts deliberately sum to
    more than the manifest total.
    """
    labels = {"sitemap": "sitemap", "rest_api": "REST API", "xml_backup": "XML export"}
    parts = []
    for key, label in labels.items():
        if not sources.get(key):
            continue
        count = sum(1 for record in manifest.all() if key in record.discovered_via)
        parts.append(f"{label} {count}")
    if not parts:
        return "no inventory source reachable -- homepage only"
    return ", ".join(parts) + " (sources overlap; a URL can come from several)"


def _run_to_settled(manifest, profile, config, session, rate_limiter, wayback_rate_limiter, fetch_config, raw_dir, exclusions, manifest_path) -> None:
    """Interleave crawl -> Wayback recovery -> canonical cascade until no
    pending work remains (Stage 2/4 joint fixpoint, see CLAUDE-acquire.md)."""
    while True:
        crawl_fixpoint(
            manifest, profile, session, rate_limiter, fetch_config, raw_dir, exclusions,
            manifest_save_path=manifest_path, workers=config.concurrency,
        )
        if config.wayback.enabled:
            recover_via_wayback(
                manifest, profile, session, wayback_rate_limiter, fetch_config, raw_dir,
                config.wayback.prefer_snapshots_near, manifest_save_path=manifest_path,
            )
        cascade_created_pending = apply_canonical_cascade(manifest, profile, raw_dir)
        if not manifest.by_status(Status.PENDING.value) and not cascade_created_pending:
            break
    manifest.save(manifest_path)


def run_acquire(config: SiteConfig, resume: bool, dry_run: bool) -> int:
    _configure_logging(config.output_dir)
    output_dir = config.output_dir
    raw_dir = output_dir / "raw"
    manifest_path = output_dir / "manifest.json"

    if manifest_path.exists() and not resume and not dry_run:
        print(
            f"A manifest already exists at {manifest_path}.\n"
            "Pass --resume to continue it, or remove the output directory to start fresh."
        )
        return 2

    lock = RunLock(output_dir)
    try:
        lock.acquire()
    except RunLockHeld as exc:
        print(str(exc))
        return 2
    try:
        return _run_acquire_locked(config, resume, dry_run, output_dir, raw_dir, manifest_path)
    finally:
        lock.release()


def _run_acquire_locked(
    config: SiteConfig, resume: bool, dry_run: bool, output_dir: Path, raw_dir: Path, manifest_path: Path
) -> int:
    manifest = Manifest.load(manifest_path) if manifest_path.exists() else Manifest()

    session = requests.Session()
    # A single Session is shared across all worker threads -- deliberate,
    # not an oversight: urllib3's connection pool underneath is
    # thread-safe. Size it for the configured concurrency so connections
    # aren't discarded once workers exceed the default pool size of 10.
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10, pool_maxsize=max(10, config.concurrency)
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    fetch_config = FetchConfig(user_agent=config.user_agent)
    rate_limiter = RateLimiter(config.rate_limit)
    wayback_rate_limiter = RateLimiter(config.wayback_rate_limit)

    run_started = utc_now()
    profile = probe_site(config.base_url, session, config.user_agent, config.extra_hosts)
    manifest.site_profile = profile

    # Compiled before discovery, not after: inventory sources are seeded
    # through the same filter the crawl uses, so an excluded or out-of-scope
    # URL never becomes a record at all rather than becoming one and being
    # marked `excluded` later.
    exclusions = compile_exclusions(config.exclusions)

    manifest.get_or_create(config.base_url, discovered_via="base_url")
    sources = discover_inventory(
        manifest, config.base_url, profile, exclusions, config.xml_backup,
        session, rate_limiter, fetch_config,
    )
    manifest.save(manifest_path)

    if dry_run:
        write_report_json(manifest, output_dir, output_dir / "report.json", run_started, utc_now())
        write_report_html(manifest, output_dir, output_dir / "report.html", run_started, utc_now())
        print(f"Dry run: {len(manifest)} URL(s) discovered, nothing fetched.")
        print(f"  by source: {_describe_inventory_sources(manifest, sources)}")
        return 0

    _run_to_settled(manifest, profile, config, session, rate_limiter, wayback_rate_limiter, fetch_config, raw_dir, exclusions, manifest_path)

    flag_orphans_and_unlisted(manifest)
    flag_forms_and_plugin_markup(manifest, raw_dir)
    flag_attachment_pages(manifest, raw_dir)
    flag_xml_unresolved(manifest)
    canonical_map = flag_hash_duplicates(manifest)
    flag_ambiguous_canonical(manifest, canonical_map)
    compute_output_paths(manifest, profile, canonical_map)
    manifest.save(manifest_path)

    run_finished = utc_now()
    write_report_json(manifest, output_dir, output_dir / "report.json", run_started, run_finished)
    write_report_html(manifest, output_dir, output_dir / "report.html", run_started, run_finished)
    (output_dir / "redirects.htaccess").write_text(generate_redirects_htaccess(manifest), encoding="utf-8")

    _maybe_offer_diagnostics(manifest, output_dir, config.base_url)
    _maybe_offer_build_and_validate(config)

    return 1 if manifest.has_gaps() else 0


def _latest_log_path(output_dir: Path) -> Path | None:
    """Every wpfreeze invocation -- including `status`/`report`/`diagnose`
    itself -- calls _configure_logging and so creates its own log file,
    almost always empty. The most recently *modified* file is therefore
    usually a trivial stub from whatever command ran last, not the actual
    acquire run; skip empty files to find the real one."""
    log_files = sorted((output_dir / "logs").glob("*.log"))
    for path in reversed(log_files):
        if path.stat().st_size > 0:
            return path
    return None


def _maybe_offer_diagnostics(manifest: Manifest, output_dir: Path, base_url: str) -> None:
    """Only prompts in a real interactive terminal -- this runs at the end
    of `acquire`, which is routinely launched unattended/backgrounded, and
    a blocking input() there would hang a run that already finished."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    try:
        answer = input("Generate a diagnostics report for this run? [Y/n] ").strip().lower()
    except EOFError:
        return
    if answer not in ("", "y", "yes"):
        return
    run_diagnose_for(manifest, output_dir, base_url)


def _maybe_offer_build_and_validate(config: SiteConfig) -> None:
    """Same reasoning as _maybe_offer_diagnostics: only prompts in a real
    interactive terminal, since `acquire` is routinely launched unattended
    and a blocking input() here would hang a run that already finished.

    Two separate prompts, not one bundled offer: `build` is disk-only,
    deterministic, and fast, so there is no real downside to offering it
    unconditionally. `validate` needs a JVM plus a downloaded vnu.jar --
    the one of the two that can actually fail in a given environment -- so
    it is only offered if `java` is on PATH at all, and only after a
    successful build (nothing to validate otherwise).
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    try:
        answer = input("Build the static site from this capture? [Y/n] ").strip().lower()
    except EOFError:
        return
    if answer not in ("", "y", "yes"):
        return
    if run_build(config, None, verify=True) == 2:
        return  # run_build already printed why (e.g. no manifest found)

    if shutil.which("java") is None:
        print("Java not found on PATH; skipping the offer to validate (VNU needs a JVM).")
        return
    try:
        answer = input("Validate the built site's HTML/CSS with VNU? [Y/n] ").strip().lower()
    except EOFError:
        return
    if answer not in ("", "y", "yes"):
        return
    run_validate(config, None)


def run_diagnose_for(manifest: Manifest, output_dir: Path, base_url: str) -> Path:
    diagnostics = build_diagnostics(manifest, output_dir, base_url, _latest_log_path(output_dir))
    path = write_diagnostics(diagnostics, output_dir)
    print(format_diagnostics_summary(diagnostics))
    print(f"Full detail: {path}")
    return path


def run_diagnose(config: SiteConfig) -> int:
    manifest_path = config.output_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest found at {manifest_path}; run `wpfreeze acquire` first.")
        return 2
    manifest = Manifest.load(manifest_path)
    run_diagnose_for(manifest, config.output_dir, config.base_url)
    return 0


def run_report(config: SiteConfig, html_only: bool, json_only: bool) -> int:
    manifest_path = config.output_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest found at {manifest_path}; run `wpfreeze acquire` first.")
        return 2
    manifest = Manifest.load(manifest_path)
    if not json_only:
        write_report_html(manifest, config.output_dir, config.output_dir / "report.html")
    if not html_only:
        write_report_json(manifest, config.output_dir, config.output_dir / "report.json")
    return 1 if manifest.has_gaps() else 0


def run_status(config: SiteConfig) -> int:
    manifest_path = config.output_dir / "manifest.json"
    if not manifest_path.exists():
        print("No manifest found; nothing has been acquired yet.")
        return 0
    manifest = Manifest.load(manifest_path)
    counts = Counter(r.status for r in manifest.all())

    print(f"Manifest: {manifest_path}")
    print(f"Total records: {len(manifest)}")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    pending = counts.get(Status.PENDING.value, 0) + counts.get(Status.RETRYING.value, 0)
    print(f"Pending/awaiting resolution: {pending}")
    print(f"Gaps present (would exit 1): {'yes' if manifest.has_gaps() else 'no'}")
    return 0


def run_rescan(config: SiteConfig, apply: bool, profile_from_config: bool) -> int:
    """Re-parse stored raw/ bytes with today's extraction code and queue
    any newly-discovered reference as a new pending record.

    Makes no network calls and does not run Stage 5 analysis or
    regenerate reports -- see wpfreeze.rescan's module docstring. Report-
    only by default; --apply is required to write, and leaves the
    manifest in exactly the state an interrupted `acquire --resume`
    already knows how to finish (some records pending, everything else
    untouched) -- `acquire --resume` is the second half of this feature,
    not something rescan reimplements.
    """
    manifest_path = config.output_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest found at {manifest_path}; run `wpfreeze acquire` first.")
        return 2

    manifest = Manifest.load(manifest_path)
    raw_dir = config.output_dir / "raw"

    if manifest.site_profile is not None:
        profile = manifest.site_profile
    else:
        # Pre-schema-2 manifest: the profile the crawl actually probed
        # (use_https/trailing_slash/canonical_host) was never persisted,
        # so it must be guessed from config alone -- and a wrong guess
        # normalizes discovered links to different keys than the crawl
        # used, flooding the manifest with spurious duplicate pending
        # records. See rescan.rescan's docstring.
        profile = scope_profile_from_config(config.base_url, config.extra_hosts)
        print(
            "** No persisted site profile on this manifest (captured before schema "
            "version 2) -- guessing use_https/trailing_slash/canonical_host from "
            "config alone. If the real site prefers www, prefers no trailing "
            "slash, or doesn't serve https, this WILL normalize discovered links "
            "to different keys than the original crawl and flood the manifest "
            "with spurious duplicate pending records. Run `acquire --resume` once "
            "to persist the real profile before trusting rescan's output on this "
            "capture. **"
        )
        if apply and not profile_from_config:
            print(
                "Refusing to --apply against a legacy manifest with no persisted "
                "profile. Pass --profile-from-config to proceed anyway (not "
                "recommended), or run `acquire --resume` once first to persist "
                "the real profile."
            )
            return 2

    lock = None
    if apply:
        lock = RunLock(config.output_dir)
        try:
            lock.acquire()
        except RunLockHeld as exc:
            print(str(exc))
            return 2

    try:
        stats = rescan(manifest, profile, raw_dir)
        print(format_rescan_summary(stats))
        if apply:
            manifest.save(manifest_path)
            print(
                f"{stats.records_created} new pending record(s) written to {manifest_path}.\n"
                "The manifest is now derived-stale -- output_path, flags, and the "
                "hash-duplicate canonical map don't reflect the new records yet. "
                "Run `acquire --resume` next, not `build`."
            )
        else:
            print("Report only -- nothing written. Pass --apply to queue these records.")
    finally:
        if lock is not None:
            lock.release()

    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def run_build(config: SiteConfig, site_dir: Path | None, verify: bool, write_todo: bool = True) -> int:
    """Emit the rewritten, servable site from an existing capture.

    Reads only; writes only into the site directory. Never touches raw/,
    so it is safe to re-run as often as the policy flags change.
    """
    manifest_path = config.output_dir / "manifest.json"
    if not manifest_path.exists():
        print("No manifest found; run `wpfreeze acquire` first.")
        return 2
    manifest = Manifest.load(manifest_path)
    target = site_dir or (config.output_dir / "site")
    # Captured before build_site writes anything, so verify_site can tell
    # this run's own output apart from whatever else might already be
    # sitting in `target` (a file the user placed there for their own
    # purposes, say) -- see verify_site's written_after docstring.
    build_started = time.time()
    stats = build_site(
        manifest, config.output_dir, target, config.policy, config.base_url, config.extra_hosts, config.search
    )
    print(format_build_summary(stats))
    print(f"Site written to {target}")

    # Verification runs before the report is written, not after: its
    # findings belong in build-report.json alongside the rewriting stats,
    # or the cleanup checklist cannot see them (see write_build_report).
    broken = 0
    verify_report = None
    if verify:
        verify_report = verify_site(target, written_after=build_started)
        print(format_verify_summary(verify_report))
        broken = len(verify_report.broken)

    # After verify, before write_build_report: the index result has to
    # land in build-report.json (via stats.search, folded into asdict(stats))
    # alongside the rewriting stats, or the cleanup checklist (regenerated
    # moments later) has no way to see it.
    search_result = None
    content_issues = None
    if config.search.enabled:
        try:
            search_result = run_pagefind_index(target, config.search)
        except SearchUnavailable as exc:
            print(f"Search index could not be built: {exc}")
            return 2
        stats.search.index_ok = search_result.ok
        stats.search.indexed_pages = search_result.pages_indexed
        stats.search.languages = list(search_result.languages)
        stats.search.index_error = search_result.error
        print(format_search_summary(stats.search, search_result))
        # Reads the markup build_site just wrote, independent of whether
        # indexing itself succeeded -- runs regardless of search_result.ok.
        content_issues = scan_content_issues(target, config.search)
        print(format_content_issues_summary(content_issues))

    write_build_report(stats, config.output_dir, verify_report, content_issues)

    if write_todo:
        _announce_cleanup_todo(config.output_dir)

    # Unresolved references, broken local links, and a failed search index
    # are all real (if partial) failures to finish the job, and mirror
    # acquire's "complete with gaps" exit code. A site shipped with a
    # hijacked search form and no index behind it is a failed build, not a
    # warning.
    return 1 if (stats.unresolved or broken or (search_result is not None and not search_result.ok)) else 0


def run_validate(config: SiteConfig, site_dir: Path | None, write_todo: bool = True) -> int:
    """Check the built site's HTML/CSS with VNU.

    No config needed to enable this: unless `vnu_jar` pins a specific jar,
    a cached copy of the latest release is fetched/refreshed automatically
    (see validate.ensure_vnu_jar) into a directory shared across configs,
    since the checker isn't site-specific.

    Informational only: these are markup defects in the site's own
    theme/plugins, not something wpfreeze's rewriting caused or can fix,
    so this never fails the build over someone else's markup -- unlike
    `build`'s own broken-reference check, it always exits 0 once it has
    successfully run.
    """
    target = site_dir or (config.output_dir / "site")
    if not target.exists():
        print(f"No built site found at {target}; run `wpfreeze build` first.")
        return 2

    try:
        vnu_jar = config.vnu_jar or ensure_vnu_jar()
        report = validate_site(vnu_jar, target)
    except VnuUnavailable as exc:
        print(f"VNU validation could not run: {exc}")
        return 2

    print(format_validation_summary(report))
    path = write_validation_report(report, config.output_dir)
    print(f"Full detail: {path}")

    if write_todo:
        _announce_cleanup_todo(config.output_dir)

    return 0


def _announce_cleanup_todo(output_dir: Path) -> None:
    """Regenerate the cleanup-todo doc (markdown and HTML) from whatever of
    build-report.json/vnu-report.json/diagnostics.json exist in
    `output_dir`, and tell the user where it landed. Always runs (not
    gated on a tty) -- this is output, not a prompt, and the whole point
    is that nobody has to remember to go looking for it."""
    path = write_cleanup_todo(output_dir)
    html_path = write_cleanup_todo_html(output_dir)
    if path is not None:
        print(f"Cleanup checklist: {path}" + (f" ({html_path.name})" if html_path is not None else ""))


def run_upload_script(config: SiteConfig, site_dir: Path | None) -> int:
    """Write upload.sh so a site owner can get the built site somewhere --
    a staging remote, a local preview, or (deliberately, separately) a real
    production deploy, see upload.py's own module docstring for the three
    modes -- explicit and separate from `build` on purpose: nothing about
    generating or running this script happens automatically just because a
    remote is set in the config. Running the script itself, in whichever
    mode, is a further, separate step the user takes by hand; this command
    only ever writes it. Written unconditionally, even with neither
    `upload.remote` nor `upload.prod_remote` set -- the script's --local
    mode needs neither.
    """
    target = site_dir or (config.output_dir / "site")
    if not target.exists():
        print(f"No built site found at {target}; run `wpfreeze build` first.")
        return 2

    try:
        site_rel = target.relative_to(config.output_dir).as_posix()
    except ValueError:
        # A --site-dir outside output_dir: upload.sh's relative `cp -a
        # {site_rel}/.` would not resolve from output_dir. Rare enough
        # (the default --site-dir is always output_dir/site) not to be
        # worth a portable-path workaround.
        print(f"{target} is not inside {config.output_dir}; can't write a script relative to it.")
        return 2

    path = write_upload_script(config.output_dir, config.upload.remote, config.upload.prod_remote, site_rel)
    print(f"Upload script: {path}")
    if not config.upload.remote:
        print("  (no upload.remote set -- the default/staging mode will error until it is)")
    if not config.upload.prod_remote:
        print("  (no upload.prod_remote set -- --prod will error until it is)")
    return 0


def run_search_index(config: SiteConfig, site_dir: Path | None) -> int:
    """Re-index an already-built site with Pagefind, without a full
    `wpfreeze build`. `build` already runs this automatically when
    `search.enabled` is set -- this command exists for re-indexing after an
    `ignore_selectors`/`force_language` change without re-running the whole
    build. It only re-runs the indexer, not the markup-tagging step
    (apply_search, inside build_site's page loop) -- a `body_selectors` or
    `exclude_pages` change needs `build` to actually take effect; running
    this command alone would index the *old* tagging. Unlike
    `upload-script`, this is not purely deferrable: a built site with
    search's markup wired in and no index behind it is broken, not just
    unpreviewed. It stays gated behind the explicit `search.enabled` (plus
    the ConfigError in load_config), so nobody gets it by accident.
    """
    if not config.search.enabled:
        print("`search: enabled: true` is not set in the config; nothing to index.")
        return 2

    target = site_dir or (config.output_dir / "site")
    if not target.exists():
        print(f"No built site found at {target}; run `wpfreeze build` first.")
        return 2

    try:
        result = run_pagefind_index(target, config.search)
    except SearchUnavailable as exc:
        print(f"Search index could not be built: {exc}")
        return 2

    # Not format_search_summary: that also reports per-page form/content
    # coverage computed during build_site's own loop, which this command
    # doesn't re-run (the markup is already there from the last `build`).
    if result.ok:
        langs = ", ".join(result.languages) if result.languages else "unknown"
        print(f"Search index: {result.pages_indexed} page(s), language(s): {langs}")
    else:
        print(f"Search index FAILED: {result.error}")

    # scan_content_issues re-reads the finished site_dir, same as
    # run_pagefind_index does -- it needs no per-page build state, so it
    # runs here too and picks up any ignore_selectors change immediately.
    # Console-only: cleanup-todo.{md,html} is a build artefact this
    # command never touches, so the checklist is not refreshed here.
    content_issues = scan_content_issues(target, config.search)
    print(format_content_issues_summary(content_issues))
    print("(cleanup-todo.md/.html not refreshed -- run `wpfreeze build` to update it)")

    return 0 if result.ok else 1


def run_checklinks(config: SiteConfig, site_dir: Path | None, recheck: bool) -> int:
    """Find every external `<a href>` the built site contains and check
    whether it still resolves, writing `broken-external-links.md`/`.html`
    grouped by the internal page each broken link was found on.

    Two phases, independently runnable (see linkcheck.py's module
    docstring for why): without `--recheck`, scans the built site and
    (re)writes `external-links.json`, the persisted page -> external-URL
    list, before checking it. With `--recheck`, skips the scan entirely
    and re-verifies the URLs already in `external-links.json` -- so link
    rot can be re-checked on a later day without the built site even
    being present, let alone re-running `build`.
    """
    if recheck:
        links = load_links(config.output_dir)
        if links is None:
            print(
                f"No {config.output_dir / 'external-links.json'} found; run "
                "`wpfreeze checklinks` (without --recheck) first."
            )
            return 2
    else:
        target = site_dir or (config.output_dir / "site")
        if not target.exists():
            print(f"No built site found at {target}; run `wpfreeze build` first.")
            return 2
        profile = scope_profile_from_config(config.base_url, config.extra_hosts)
        links = extract_external_links(target, profile)
        path = write_links(links, config.output_dir, config.base_url)
        print(f"Found {len(links)} unique external link(s) across the built site; saved to {path}")

    if not links:
        print("No external links to check.")
        return 0

    results = check_links(links, user_agent=config.user_agent, rate_limit=config.rate_limit, workers=config.concurrency)
    broken = [r for r in results if not r.ok]
    checked_at = datetime.now(timezone.utc).isoformat()
    md_path, html_path = write_report(results, config.output_dir, checked_at)
    print(f"Checked {len(results)} external link(s): {len(broken)} broken.")
    print(f"Report: {md_path} / {html_path}")
    return 1 if broken else 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wpfreeze")
    # Not required: no subcommand at all prints the non-interactive overview
    # (see print_overview in wpfreeze.wizard) -- `wizard` below is the
    # interactive flow that used to be the no-args default.
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser(
        "wizard", help="interactive setup: resume an existing run, or build a new site config"
    )

    acquire_p = subparsers.add_parser("acquire", help="run (or resume) the full acquisition pipeline")
    acquire_p.add_argument("--config", required=True, type=Path)
    acquire_p.add_argument("--resume", action="store_true")
    acquire_p.add_argument("--dry-run", action="store_true")

    report_p = subparsers.add_parser("report", help="regenerate reports from the existing manifest")
    report_p.add_argument("--config", required=True, type=Path)
    format_group = report_p.add_mutually_exclusive_group()
    format_group.add_argument("--html-only", action="store_true")
    format_group.add_argument("--json-only", action="store_true")

    status_p = subparsers.add_parser("status", help="print a one-screen manifest summary")
    status_p.add_argument("--config", required=True, type=Path)

    build_p = subparsers.add_parser(
        "build", help="rewrite the capture into a servable static site"
    )
    build_p.add_argument("--config", required=True, type=Path)
    build_p.add_argument(
        "--site-dir", type=Path, default=None, help="output directory (default: <output_dir>/site)"
    )
    build_p.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the post-build check that every local reference resolves on disk",
    )
    build_p.add_argument(
        "--no-todo",
        action="store_true",
        help="skip regenerating cleanup-todo.md/.html, the human-readable punch list synthesized "
        "from build/validate/diagnose's reports",
    )

    diagnose_p = subparsers.add_parser(
        "diagnose", help="write a compact debugging summary (diagnostics.json) from the existing manifest"
    )
    diagnose_p.add_argument("--config", required=True, type=Path)

    validate_p = subparsers.add_parser(
        "validate", help="check the built site's HTML/CSS with the Nu Html Checker (VNU)"
    )
    validate_p.add_argument("--config", required=True, type=Path)
    validate_p.add_argument(
        "--site-dir", type=Path, default=None, help="site directory to check (default: <output_dir>/site)"
    )
    validate_p.add_argument(
        "--no-todo",
        action="store_true",
        help="skip regenerating cleanup-todo.md/.html, the human-readable punch list synthesized "
        "from build/validate/diagnose's reports",
    )

    rescan_p = subparsers.add_parser(
        "rescan",
        help="re-parse stored raw/ bytes with current extraction code; queue new references as pending",
    )
    rescan_p.add_argument("--config", required=True, type=Path)
    rescan_p.add_argument(
        "--apply", action="store_true", help="write queued records to manifest.json (default: report only)"
    )
    rescan_p.add_argument(
        "--profile-from-config",
        action="store_true",
        help="allow --apply against a manifest with no persisted site profile (schema < 2), "
        "reconstructing it from config alone -- not recommended, see the warning this prints",
    )

    upload_script_p = subparsers.add_parser(
        "upload-script",
        help="write upload.sh: staging sync by default, --local for a no-network preview folder, "
        "--prod for a reports-free production sync -- writes the script only, never runs it",
    )
    upload_script_p.add_argument("--config", required=True, type=Path)
    upload_script_p.add_argument(
        "--site-dir", type=Path, default=None, help="site directory to reference (default: <output_dir>/site)"
    )

    search_index_p = subparsers.add_parser(
        "search-index",
        help="(re-)build the Pagefind search index over an already-built site (needs `search: "
        "enabled: true`) -- `build` already does this automatically; use this to re-index after "
        "a selector change without a full rebuild",
    )
    search_index_p.add_argument("--config", required=True, type=Path)
    search_index_p.add_argument(
        "--site-dir", type=Path, default=None, help="site directory to reference (default: <output_dir>/site)"
    )

    checklinks_p = subparsers.add_parser(
        "checklinks",
        help="find external links on the built site and check whether they're still live, writing "
        "broken-external-links.md/.html grouped by the page each one was found on",
    )
    checklinks_p.add_argument("--config", required=True, type=Path)
    checklinks_p.add_argument(
        "--site-dir", type=Path, default=None, help="site directory to scan (default: <output_dir>/site)"
    )
    checklinks_p.add_argument(
        "--recheck",
        action="store_true",
        help="re-check the external links already saved in external-links.json instead of re-scanning "
        "the built site -- works without the built site present",
    )

    return parser


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _configure_logging(output_dir: Path) -> None:
    """Per CLAUDE-acquire.md, output_dir gets a logs/ directory alongside
    raw/ and the reports. Console stays at INFO (meaningful progress); the
    file captures DEBUG (one line per fetch and below).

    Handlers are attached directly to the "wpfreeze" package logger with
    propagate=False, rather than relying on logging.basicConfig() on the
    root logger -- basicConfig() silently no-ops if the root logger
    already has handlers (as test runners and other host processes often
    arrange), which would otherwise leave wpfreeze's own loggers filtered
    out at an inherited level regardless of what we ask for here.
    """
    package_logger = logging.getLogger("wpfreeze")
    package_logger.handlers.clear()  # idempotent if main() runs more than once in-process
    package_logger.setLevel(logging.DEBUG)
    package_logger.propagate = False

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    package_logger.addHandler(console_handler)

    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    package_logger.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    """Top-level entry point (see pyproject.toml's console_scripts).

    Wraps _dispatch in a KeyboardInterrupt handler so a Ctrl-C anywhere
    downstream -- mid-crawl, mid-wizard-prompt, anywhere -- prints one
    short line instead of a raw traceback. 130 is the conventional
    128+SIGINT exit code for an interrupted process.

    Any other uncaught exception is logged (not just printed to stderr)
    before re-raising -- a long unattended run's stderr is easy to lose
    (backgrounded, piped, terminal closed), and without this the crawl's
    own log file, which is otherwise the durable record of what happened,
    would have no trace at all of why it died.
    """
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:
        print(
            "\nInterrupted. The manifest is saved incrementally during a "
            "crawl, so an `acquire` run can usually pick back up with "
            "--resume rather than starting over."
        )
        return 130
    except Exception:
        logging.getLogger("wpfreeze").critical("acquire crashed with an unhandled exception", exc_info=True)
        raise


def _dispatch(argv: list[str] | None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    if not raw_argv:
        # Interactive picker in a real terminal; the old plain-text
        # overview otherwise (piped output, CI, a script capturing
        # `wpfreeze`'s stdout) -- same isatty gate _maybe_offer_diagnostics/
        # _maybe_offer_build_and_validate already use for "don't block on
        # input() when nothing's there to answer it".
        if sys.stdin.isatty() and sys.stdout.isatty():
            from wpfreeze.picker import run_picker

            return run_picker()

        from wpfreeze.wizard import print_overview

        return print_overview()

    args = build_arg_parser().parse_args(raw_argv)

    if args.command == "wizard":
        from wpfreeze.wizard import run_wizard

        return run_wizard()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc))
        return 2
    except (OSError, yaml.YAMLError, KeyError) as exc:
        print(f"Failed to load config {args.config}: {exc}")
        return 2

    _configure_logging(config.output_dir)

    if args.command == "acquire":
        return run_acquire(config, resume=args.resume, dry_run=args.dry_run)
    if args.command == "report":
        return run_report(config, html_only=args.html_only, json_only=args.json_only)
    if args.command == "status":
        return run_status(config)
    if args.command == "build":
        return run_build(config, args.site_dir, verify=not args.no_verify, write_todo=not args.no_todo)
    if args.command == "diagnose":
        return run_diagnose(config)
    if args.command == "validate":
        return run_validate(config, args.site_dir, write_todo=not args.no_todo)
    if args.command == "rescan":
        return run_rescan(config, apply=args.apply, profile_from_config=args.profile_from_config)
    if args.command == "upload-script":
        return run_upload_script(config, args.site_dir)
    if args.command == "search-index":
        return run_search_index(config, args.site_dir)
    if args.command == "checklinks":
        return run_checklinks(config, args.site_dir, recheck=args.recheck)

    return 2


if __name__ == "__main__":
    sys.exit(main())
