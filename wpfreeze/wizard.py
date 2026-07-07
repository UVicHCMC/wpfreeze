"""No-args wizard mode: `wpfreeze` with no subcommand walks through building
a site config interactively -- base URL, output directory, politeness,
Wayback recovery, and database situation (none / live connection / "I have
a dump" via wpfreeze.dbsetup) -- then writes a normal site YAML and offers
to dry-run and run it immediately.

Every question here maps onto the same SiteConfig/YAML shape load_config()
already reads (see wpfreeze.cli) -- the file this writes is a completely
ordinary config afterward: --resume, report, and status all work on it
with no wizard involved.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import yaml

from wpfreeze import dbsetup

# Mirrors example-site.yaml's default exclusions -- kept in sync manually,
# see that file's comments for what each pattern covers.
DEFAULT_EXCLUSIONS = [
    r"\?replytocom=",
    r"/feed/?$",
    r"/comments/feed",
    r"/feed/(rss2?|atom|rdf)/?$",
    r"\?s=",
    r"/search/",
    r"/wp-json/",
    r"/wp-admin/",
    r"/wp-login",
    r"/xmlrpc\.php",
    r"/comment-page-\d+/",
]

RATE_PRESETS: dict[str, tuple[str, float]] = {
    "1": ("Gentle", 2.0),
    "2": ("Normal", 1.0),
    "3": ("Aggressive", 0.3),
}


def _ask(prompt_text: str, default: str, ask: Callable[[str], str]) -> str:
    suffix = f" [{default}]" if default else ""
    answer = ask(f"{prompt_text}{suffix}: ").strip()
    return answer or default


def _ask_required(prompt_text: str, ask: Callable[[str], str]) -> str:
    while True:
        answer = ask(f"{prompt_text}: ").strip()
        if answer:
            return answer


def _ask_yes_no(prompt_text: str, default: bool, ask: Callable[[str], str]) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{prompt_text} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def slugify_domain(base_url: str) -> str:
    host = urlsplit(base_url).hostname or "site"
    return host.replace(".", "-")


def _build_db_dict(ask: Callable[[str], str], tell: Callable[[str], None]) -> dict | str:
    tell("Do you have database access for this site?")
    tell("  1) No")
    tell("  2) Yes, I can connect directly")
    tell("  3) I have a dump file")
    choice = _ask("Choose", "1", ask)

    if choice == "2":
        host = _ask("DB host (blank to use a socket instead)", "", ask)
        db_dict: dict = {
            "name": _ask_required("DB name", ask),
            "user": _ask_required("DB user", ask),
            "password_env": _ask(
                "Environment variable holding the DB password", "WPFREEZE_DB_PASSWORD", ask
            ),
            "table_prefix": _ask("Table prefix", "wp_", ask),
        }
        if host:
            db_dict["host"] = host
        else:
            db_dict["socket"] = _ask("DB socket path", "/var/run/mysqld/mysqld.sock", ask)
        return db_dict

    if choice == "3":
        dump_path = Path(_ask_required("Path to your .sql or .sql.gz dump file", ask))
        tell("Setting up a local database from your dump...")
        plan = dbsetup.run_setup(dump_path=dump_path, ask=ask, tell=tell)
        return dbsetup.to_db_dict(plan)

    return "none"


def build_config_dict(
    ask: Callable[[str], str] = input,
    tell: Callable[[str], None] = print,
) -> tuple[dict, Path]:
    """Runs the question flow and returns (config_dict, suggested_yaml_path).
    Does not write anything -- callers decide when/whether to persist."""
    base_url = _ask_required("What site are we scraping? (base URL)", ask)
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"

    domain_slug = slugify_domain(base_url)
    output_dir = _ask("Where should the output go?", f"./output/{domain_slug}", ask)

    tell("How nice are we being to the server?")
    tell("  1) Gentle (2s between requests)")
    tell("  2) Normal (1s between requests) [default]")
    tell("  3) Aggressive (0.3s between requests)")
    choice = _ask("Choose", "2", ask)
    _, rate_limit = RATE_PRESETS.get(choice, RATE_PRESETS["2"])

    wayback_enabled = _ask_yes_no("Use Wayback Machine recovery for missing pages?", True, ask)
    wayback_dict: dict = {"enabled": wayback_enabled}
    if wayback_enabled:
        prefer_snapshots_near = _ask(
            "Prefer Wayback snapshots nearest to which date? (YYYY-MM-DD, blank = today)", "", ask
        )
        if prefer_snapshots_near:
            wayback_dict["prefer_snapshots_near"] = prefer_snapshots_near

    db_dict = _build_db_dict(ask, tell)

    config_dict: dict = {
        "base_url": base_url,
        "output_dir": output_dir,
        "rate_limit": rate_limit,
        "wayback_rate_limit": round(rate_limit * 3, 2),
        "exclusions": DEFAULT_EXCLUSIONS,
        "extra_hosts": [],
        "wayback": wayback_dict,
        "db": db_dict,
    }

    tell(
        "(Exclusions, extra_hosts, and user_agent were left at their "
        "defaults -- edit the written YAML directly if this site needs "
        "something different.)"
    )

    suggested_path = Path(f"{domain_slug}.yaml")
    return config_dict, suggested_path


def run_wizard(
    ask: Callable[[str], str] = input,
    tell: Callable[[str], None] = print,
) -> int:
    from wpfreeze.cli import load_config, run_acquire  # deferred: cli imports this module

    config_dict, suggested_path = build_config_dict(ask=ask, tell=tell)

    config_path = Path(_ask("Save this config as", str(suggested_path), ask))
    config_path.write_text(yaml.safe_dump(config_dict, sort_keys=False), encoding="utf-8")
    tell(f"Wrote {config_path}")

    config = load_config(config_path)

    if _ask_yes_no("Run a dry-run now? (discovers URLs, fetches nothing)", True, ask):
        exit_code = run_acquire(config, resume=False, dry_run=True)
        tell(f"Dry run finished (exit code {exit_code}). See {config.output_dir}/report.html")

    if _ask_yes_no("Run the real acquisition now?", False, ask):
        exit_code = run_acquire(config, resume=False, dry_run=False)
        tell(f"Acquisition finished (exit code {exit_code}). See {config.output_dir}/report.html")
        return exit_code

    tell(f"When you're ready: wpfreeze acquire --config {config_path}")
    return 0
