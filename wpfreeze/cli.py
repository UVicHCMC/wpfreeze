"""CLI entry point: argument parsing, YAML config loading/validation, and
pipeline orchestration for `wpfreeze acquire|report|status`.

See CLAUDE-acquire.md, "CLI" and "Configuration (YAML)".
"""
from __future__ import annotations

import argparse
import logging
import os
import re
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
    flag_db_unresolved,
    flag_forms_and_plugin_markup,
    flag_hash_duplicates,
    flag_orphans_and_unlisted,
)
from wpfreeze.crawl import compile_exclusions, crawl_fixpoint
from wpfreeze.fetch import DEFAULT_USER_AGENT, FetchConfig, RateLimiter
from wpfreeze.inventory import DbConfig, discover_inventory
from wpfreeze.manifest import Manifest, Status, utc_now
from wpfreeze.outputs import compute_output_paths, generate_redirects_htaccess
from wpfreeze.report import write_report_html, write_report_json
from wpfreeze.urlnorm import SiteProfile
from wpfreeze.wayback import recover_via_wayback

logger = logging.getLogger(__name__)

DB_MISSING_MESSAGE = (
    "Do you have database access for this site? If yes, add a `db:` block "
    "(see example-site.yaml); if no, set `db: none`."
)

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
    db: DbConfig | None = None  # None means the config said `db: none`


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def load_config(path: Path) -> SiteConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    if "db" not in raw:
        raise ConfigError(DB_MISSING_MESSAGE)

    db_config = _parse_db_config(raw["db"])
    wayback_raw = raw.get("wayback") or {}
    prefer_near_raw = wayback_raw.get("prefer_snapshots_near")

    return SiteConfig(
        base_url=raw["base_url"].rstrip("/") + "/",
        output_dir=Path(raw["output_dir"]),
        rate_limit=float(raw.get("rate_limit", 1.0)),
        wayback_rate_limit=float(raw.get("wayback_rate_limit", 3.0)),
        concurrency=int(raw.get("concurrency", 2)),
        exclusions=list(raw.get("exclusions", []) or []),
        extra_hosts=list(raw.get("extra_hosts", []) or []),
        user_agent=raw.get("user_agent", DEFAULT_USER_AGENT),
        wayback=WaybackSettings(
            enabled=bool(wayback_raw.get("enabled", True)),
            prefer_snapshots_near=_parse_date(prefer_near_raw) if prefer_near_raw else date.today(),
        ),
        db=db_config,
    )


def _parse_db_config(db_raw) -> DbConfig | None:
    if db_raw is None or db_raw == "none":
        return None
    password = db_raw.get("password")
    if password is None and db_raw.get("password_env"):
        password = os.environ.get(db_raw["password_env"])
    return DbConfig(
        host=db_raw.get("host"),
        socket=db_raw.get("socket"),
        port=db_raw.get("port"),
        name=db_raw.get("name", ""),
        user=db_raw.get("user", ""),
        password=password,
        table_prefix=db_raw.get("table_prefix", "wp_"),
    )


# ---------------------------------------------------------------------------
# Site probing: establish the SiteProfile once, before anything else fetches
# ---------------------------------------------------------------------------


def _looks_like_ip_or_bare_host(host: str) -> bool:
    return bool(_IP_RE.match(host)) or host.count(".") == 0 or ":" in host


def _probe_https(host: str, port_suffix: str, session: requests.Session, headers: dict, timeout: float) -> bool:
    try:
        resp = session.get(f"https://{host}{port_suffix}/", headers=headers, timeout=timeout, allow_redirects=True)
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
        resp = session.get(f"{scheme}://{alternate}{port_suffix}/", headers=headers, timeout=timeout, allow_redirects=True)
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
        resp = session.get(probe_url, headers=headers, timeout=timeout, allow_redirects=True)
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
    return SiteProfile(
        canonical_host=canonical_host,
        site_hosts=site_hosts,
        use_https=use_https,
        trailing_slash=trailing_slash,
    )


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def _run_to_settled(manifest, profile, config, session, rate_limiter, wayback_rate_limiter, fetch_config, raw_dir, exclusions, manifest_path) -> None:
    """Interleave crawl -> Wayback recovery -> canonical cascade until no
    pending work remains (Stage 2/4 joint fixpoint, see CLAUDE-acquire.md)."""
    while True:
        crawl_fixpoint(manifest, profile, session, rate_limiter, fetch_config, raw_dir, exclusions, manifest_save_path=manifest_path)
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
    fetch_config = FetchConfig(user_agent=config.user_agent)
    rate_limiter = RateLimiter(config.rate_limit)
    wayback_rate_limiter = RateLimiter(config.wayback_rate_limit)

    run_started = utc_now()
    profile = probe_site(config.base_url, session, config.user_agent, config.extra_hosts)

    manifest.get_or_create(config.base_url, discovered_via="base_url")
    discover_inventory(manifest, config.base_url, config.db, session, rate_limiter, fetch_config)
    manifest.save(manifest_path)

    if dry_run:
        write_report_json(manifest, output_dir, output_dir / "report.json", run_started, utc_now())
        write_report_html(manifest, output_dir, output_dir / "report.html", run_started, utc_now())
        print(f"Dry run: {len(manifest)} URL(s) discovered, nothing fetched.")
        return 0

    exclusions = compile_exclusions(config.exclusions)
    _run_to_settled(manifest, profile, config, session, rate_limiter, wayback_rate_limiter, fetch_config, raw_dir, exclusions, manifest_path)

    flag_orphans_and_unlisted(manifest)
    flag_forms_and_plugin_markup(manifest, raw_dir)
    flag_attachment_pages(manifest, raw_dir)
    flag_db_unresolved(manifest)
    canonical_map = flag_hash_duplicates(manifest)
    flag_ambiguous_canonical(manifest, canonical_map)
    compute_output_paths(manifest, profile, canonical_map)
    manifest.save(manifest_path)

    run_finished = utc_now()
    write_report_json(manifest, output_dir, output_dir / "report.json", run_started, run_finished)
    write_report_html(manifest, output_dir, output_dir / "report.html", run_started, run_finished)
    (output_dir / "redirects.htaccess").write_text(generate_redirects_htaccess(manifest), encoding="utf-8")

    return 1 if manifest.has_gaps() else 0


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

    # Own argparse parser (wpfreeze.dbsetup.build_arg_parser); registered
    # here with REMAINDER only so `wpfreeze --help` lists it -- main()
    # intercepts "setup-db" before this parser ever sees its flags.
    setup_db_p = subparsers.add_parser(
        "setup-db", help="interactively import a SQL dump into a local scoped database"
    )
    setup_db_p.add_argument("rest", nargs=argparse.REMAINDER)

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
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    if not raw_argv:
        from wpfreeze.wizard import run_wizard

        return run_wizard()

    if raw_argv[0] == "setup-db":
        from wpfreeze.dbsetup import interactive_main

        return interactive_main(raw_argv[1:])

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

    return 2


if __name__ == "__main__":
    sys.exit(main())
