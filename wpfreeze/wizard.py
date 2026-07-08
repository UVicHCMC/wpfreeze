"""No-args wizard mode: `wpfreeze` with no subcommand first looks in the
current directory for a site config with an existing, resumable run and
offers to pick that back up (see find_resumable_configs) -- and only if
there's none, or the user declines all of them, walks through building a
new config interactively: base URL, output directory, politeness, Wayback
recovery, and an optional WordPress XML export (WXR) to augment inventory
completeness -- then writes a normal site YAML and offers to dry-run and
run it immediately.

Every question here maps onto the same SiteConfig/YAML shape load_config()
already reads (see wpfreeze.cli) -- the file this writes is a completely
ordinary config afterward: --resume, report, and status all work on it
with no wizard involved.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

import yaml

if TYPE_CHECKING:
    from wpfreeze.cli import SiteConfig

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
    "3": ("Aggressive", 0.0),
}

# Wayback is a separate, shared, third-party service that bans impolite
# clients regardless of how aggressively you archive your own site -- so
# wayback_rate_limit always gets at least this floor, even when the
# Aggressive preset's own rate_limit is 0.
_MIN_WAYBACK_RATE_LIMIT = 3.0


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


def _ask_xml_backup(ask: Callable[[str], str], tell: Callable[[str], None]) -> str | None:
    has_export = _ask_yes_no(
        "Do you have a WordPress XML export (WXR) for this site? "
        "(wp-admin: Tools -> Export -> All content)",
        False,
        ask,
    )
    if not has_export:
        return None
    return _ask_required("Path to the WXR export file", ask)


def find_resumable_configs(directory: Path = Path(".")) -> list[tuple[Path, SiteConfig]]:
    """Site config YAML files directly in `directory` that already have a
    manifest.json at their output_dir -- i.e., a prior run that --resume
    would continue rather than refuse (see run_acquire's collision guard).

    Every *.yaml/*.yml in the directory is a candidate; anything that
    doesn't parse as a valid site config is silently skipped rather than
    raised, deliberately broadly -- most directories wpfreeze runs from
    will have unrelated YAML files, and probing them is not this
    function's business to fail loudly over.
    """
    from wpfreeze.cli import load_config  # deferred: cli imports this module

    candidates = []
    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    for path in paths:
        try:
            config = load_config(path)
        except Exception:
            continue
        if (config.output_dir / "manifest.json").exists():
            candidates.append((path, config))
    return candidates


def _describe_manifest(output_dir: Path) -> str:
    from wpfreeze.manifest import Manifest, Status

    manifest = Manifest.load(output_dir / "manifest.json")
    counts = Counter(r.status for r in manifest.all())
    fetched = counts.get(Status.FETCHED.value, 0) + counts.get(Status.FETCHED_WAYBACK.value, 0)
    pending = counts.get(Status.PENDING.value, 0) + counts.get(Status.RETRYING.value, 0)
    return f"{fetched} fetched, {pending} pending/retrying, {len(manifest)} total"


def _offer_resume(
    candidates: list[tuple[Path, SiteConfig]],
    ask: Callable[[str], str],
    tell: Callable[[str], None],
) -> tuple[bool, int]:
    """Returns (handled, exit_code). handled=True means a candidate was
    resumed and run_wizard should return exit_code immediately without
    falling through to the ordinary question flow."""
    from wpfreeze.cli import run_acquire

    if not candidates:
        return False, 0

    if len(candidates) == 1:
        path, config = candidates[0]
        summary = _describe_manifest(config.output_dir)
        resume = _ask_yes_no(
            f"Found an existing run: {path.name} ({config.base_url}) -- {summary}. Resume it?",
            True,
            ask,
        )
        if not resume:
            return False, 0
        chosen_path, chosen_config = path, config
    else:
        tell("Found existing runs that could be resumed:")
        for i, (path, config) in enumerate(candidates, start=1):
            summary = _describe_manifest(config.output_dir)
            tell(f"  {i}) {path.name} ({config.base_url}) -- {summary}")
        skip_choice = str(len(candidates) + 1)
        tell(f"  {skip_choice}) None of these -- start a new one")
        choice = _ask("Choose", skip_choice, ask)
        try:
            index = int(choice) - 1
        except ValueError:
            index = len(candidates)
        if not (0 <= index < len(candidates)):
            return False, 0
        chosen_path, chosen_config = candidates[index]

    tell(f"Resuming {chosen_path}...")
    exit_code = run_acquire(chosen_config, resume=True, dry_run=False)
    tell(f"Acquisition finished (exit code {exit_code}). See {chosen_config.output_dir}/report.html")
    return True, exit_code


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
    tell("  3) Aggressive (no delay between requests)")
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

    xml_backup_path = _ask_xml_backup(ask, tell)

    config_dict: dict = {
        "base_url": base_url,
        "output_dir": output_dir,
        "rate_limit": rate_limit,
        "wayback_rate_limit": max(_MIN_WAYBACK_RATE_LIMIT, round(rate_limit * 3, 2)),
        "exclusions": DEFAULT_EXCLUSIONS,
        "extra_hosts": [],
        "wayback": wayback_dict,
    }
    if xml_backup_path:
        config_dict["xml_backup"] = xml_backup_path

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

    handled, exit_code = _offer_resume(find_resumable_configs(), ask, tell)
    if handled:
        return exit_code

    config_dict, suggested_path = build_config_dict(ask=ask, tell=tell)

    config_path = Path(_ask("Save this config as", str(suggested_path), ask))
    config_path.write_text(yaml.safe_dump(config_dict, sort_keys=False), encoding="utf-8")
    tell(f"Wrote {config_path}")

    config = load_config(config_path)

    # A dry-run already writes manifest.json (it seeds and saves the
    # inventory even though it fetches nothing) -- if the user immediately
    # says yes to the real run next, that manifest already exists and
    # run_acquire's collision guard would refuse unless told to resume.
    # This isn't "resuming someone else's prior run"; it's the same
    # session's own dry-run, so auto-resuming here is correct -- the guard
    # still applies normally to a config whose output_dir already had an
    # unrelated manifest before the wizard ever touched it.
    dry_run_performed = False
    if _ask_yes_no("Run a dry-run now? (discovers URLs, fetches nothing)", True, ask):
        exit_code = run_acquire(config, resume=False, dry_run=True)
        tell(f"Dry run finished (exit code {exit_code}). See {config.output_dir}/report.html")
        dry_run_performed = True

    if _ask_yes_no("Run the real acquisition now?", False, ask):
        exit_code = run_acquire(config, resume=dry_run_performed, dry_run=False)
        if exit_code == 2:
            tell(
                f"Acquisition finished (exit code {exit_code}). Run "
                f"`wpfreeze acquire --config {config_path} --resume` to continue it, or "
                f"remove {config.output_dir} first to start fresh."
            )
        else:
            tell(f"Acquisition finished (exit code {exit_code}). See {config.output_dir}/report.html")
        return exit_code

    tell(f"When you're ready: wpfreeze acquire --config {config_path}")
    return 0
