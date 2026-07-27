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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
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
from wpfreeze.cleanup import write_cleanup_todo
from wpfreeze.crawl import compile_exclusions, crawl_fixpoint
from wpfreeze.diagnostics import build_diagnostics, format_diagnostics_summary, write_diagnostics
from wpfreeze.fetch import DEFAULT_USER_AGENT, FetchConfig, RateLimiter
from wpfreeze.inventory import discover_inventory
from wpfreeze.manifest import Manifest, Status, utc_now
from wpfreeze.outputs import compute_output_paths, generate_redirects_htaccess
from wpfreeze.policy import Policy
from wpfreeze.report import write_report_html, write_report_json
from wpfreeze.urlnorm import SiteProfile
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
class SiteConfig:
    base_url: str
    output_dir: Path
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


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def load_config(path: Path) -> SiteConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    wayback_raw = raw.get("wayback") or {}
    prefer_near_raw = wayback_raw.get("prefer_snapshots_near")

    concurrency = int(raw.get("concurrency", 2))
    if concurrency < 1:
        raise ConfigError(f"concurrency must be >= 1, got {concurrency}")

    xml_backup_raw = raw.get("xml_backup")

    return SiteConfig(
        base_url=raw["base_url"].rstrip("/") + "/",
        output_dir=Path(raw["output_dir"]),
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
        policy=Policy.from_config(raw.get("policy")),
        vnu_jar=Path(raw["vnu_jar"]) if raw.get("vnu_jar") else None,
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
    stats = build_site(
        manifest, config.output_dir, target, config.policy, config.base_url, config.extra_hosts
    )
    print(format_build_summary(stats))
    print(f"Site written to {target}")

    # Verification runs before the report is written, not after: its
    # findings belong in build-report.json alongside the rewriting stats,
    # or the cleanup checklist cannot see them (see write_build_report).
    broken = 0
    verify_report = None
    if verify:
        verify_report = verify_site(target)
        print(format_verify_summary(verify_report))
        broken = len(verify_report.broken)
    write_build_report(stats, config.output_dir, verify_report)

    if write_todo:
        _announce_cleanup_todo(config.output_dir)

    # Unresolved references and broken local links are both real (if
    # partial) failures to finish the job, and mirror acquire's "complete
    # with gaps" exit code.
    return 1 if (stats.unresolved or broken) else 0


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
    """Regenerate the cleanup-todo doc from whatever of
    build-report.json/vnu-report.json/diagnostics.json exist in
    `output_dir`, and tell the user where it landed. Always runs (not
    gated on a tty) -- this is output, not a prompt, and the whole point
    is that nobody has to remember to go looking for it."""
    path = write_cleanup_todo(output_dir)
    if path is not None:
        print(f"Cleanup checklist: {path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wpfreeze")
    # Not required: no subcommand at all launches the interactive wizard
    # (see run_wizard in wpfreeze.wizard).
    subparsers = parser.add_subparsers(dest="command", required=False)

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
        help="skip regenerating cleanup-todo.md, the human-readable punch list synthesized "
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
        help="skip regenerating cleanup-todo.md, the human-readable punch list synthesized "
        "from build/validate/diagnose's reports",
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
        from wpfreeze.wizard import run_wizard

        return run_wizard()

    args = build_arg_parser().parse_args(raw_argv)

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

    return 2


if __name__ == "__main__":
    sys.exit(main())
