"""Two entry points live here, split by whether they touch stdin.

`print_overview` is what bare `wpfreeze` (no subcommand) runs: a
non-interactive, side-effect-free summary of every site config in the
current directory and the commands relevant to each one's state, plus a
pointer to `wizard`/SETUP.md for starting from scratch. Never reads
stdin, never runs anything -- safe to run just to look, or from a
non-interactive context.

`run_wizard` is the interactive flow, behind the explicit `wpfreeze
wizard` subcommand (it used to be the no-args default; the split exists
because the old default action-oriented behavior -- auto-offering to
resume a run, blocking on stdin -- is a bad fit for "what's here?"). It
first looks in the current directory for a site config with an existing,
resumable run and offers to pick that back up (see find_resumable_configs)
-- and only if there's none, or the user declines all of them, walks
through building a new config interactively: base URL, output directory,
politeness, Wayback recovery, and an optional WordPress XML export (WXR)
to augment inventory completeness -- then writes a normal site YAML and
offers to dry-run and run it immediately.

Every question `run_wizard` asks maps onto the same SiteConfig/YAML shape
load_config() already reads (see wpfreeze.cli) -- the file it writes is a
completely ordinary config afterward: --resume, report, and status all
work on it with no wizard involved.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
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
    valid, _invalid = scan_configs(directory)
    return [(path, config) for path, config in valid if (config.output_dir / "manifest.json").exists()]


def scan_configs(directory: Path = Path(".")) -> tuple[list[tuple[Path, SiteConfig]], list[Path]]:
    """Every *.yaml/*.yml in `directory`, split into (site configs that
    parse, paths that don't). Unlike find_resumable_configs, keeps every
    valid config regardless of whether acquisition has ever run -- a
    config nobody has acquired yet is exactly as relevant to an overview
    as one mid-run -- and, unlike that function, does not silently drop
    what fails to parse: `print_overview`'s whole purpose is telling
    someone what's going on in this directory, and staying quiet about a
    YAML file that looks like it should be a config but isn't works
    against that.
    """
    from wpfreeze.cli import load_config  # deferred: cli imports this module

    valid: list[tuple[Path, SiteConfig]] = []
    invalid: list[Path] = []
    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    for path in paths:
        try:
            config = load_config(path)
        except Exception:
            invalid.append(path)
            continue
        valid.append((path, config))
    return valid, invalid


def _manifest_counts(output_dir: Path) -> tuple[int, int, int]:
    """(fetched, pending/retrying, total) records in the manifest at
    `output_dir` -- the shared counting logic behind both
    `_describe_manifest`'s one-line summary and `print_overview`'s
    per-config state (resumable vs. complete)."""
    from wpfreeze.manifest import Manifest, Status

    manifest = Manifest.load(output_dir / "manifest.json")
    counts = Counter(r.status for r in manifest.all())
    fetched = counts.get(Status.FETCHED.value, 0) + counts.get(Status.FETCHED_WAYBACK.value, 0)
    pending = counts.get(Status.PENDING.value, 0) + counts.get(Status.RETRYING.value, 0)
    return fetched, pending, len(manifest)


def _describe_manifest(output_dir: Path) -> str:
    fetched, pending, total = _manifest_counts(output_dir)
    return f"{fetched} fetched, {pending} pending/retrying, {total} total"


@dataclass(frozen=True)
class RecommendedCommand:
    """One `wpfreeze <subcommand> --config <name> ...` a site owner would
    plausibly run next, given a config's current state. `argv_extra` is
    real argv (e.g. `("--resume",)`) appended to the invocation -- both
    `_print_commands` (rendering) and `wpfreeze.picker` (launching) build
    off the same tuple, so the two can never drift apart the way a single
    printed suffix string invited. `note` is a display-only aside (e.g.
    "   (safe to re-run any time)") with no argv meaning at all.
    """

    subcommand: str
    argv_extra: tuple[str, ...] = ()
    note: str = ""

    def argv(self, config_name: str) -> list[str]:
        return [self.subcommand, "--config", config_name, *self.argv_extra]


@dataclass(frozen=True)
class ConfigStatus:
    """One site config's current state and what to do about it -- the
    shared data both `print_overview` (plain text) and `wpfreeze.picker`
    (interactive) render from, so the two never describe a config
    differently."""

    path: Path
    config: "SiteConfig"
    status_lines: tuple[str, ...]
    commands: tuple[RecommendedCommand, ...] = ()


def describe_configs(directory: Path = Path(".")) -> tuple[list[ConfigStatus], list[Path]]:
    """Every parseable site config in `directory`, each with its current
    status text and recommended next commands, plus the paths that didn't
    parse -- the full computation bare `wpfreeze` needs, before either
    `print_overview` or `wpfreeze.picker` decides how to display it. Reads
    each config's `manifest.json`/`site/` off disk; runs nothing, writes
    nothing.
    """
    valid, invalid = scan_configs(directory)
    statuses: list[ConfigStatus] = []
    for path, config in valid:
        manifest_path = config.output_dir / "manifest.json"
        site_dir = config.output_dir / "site"
        if not manifest_path.exists():
            status_lines = ("Not yet acquired.",)
            commands = (RecommendedCommand("acquire", ("--dry-run",)), RecommendedCommand("acquire"))
        else:
            fetched, pending, total = _manifest_counts(config.output_dir)
            status_lines = (
                f"Acquired: {fetched} fetched, {pending} pending, {total} total. "
                f"Built: {'yes' if site_dir.exists() else 'no'}.",
            )
            if pending:
                commands = (RecommendedCommand("acquire", ("--resume",)),)
            else:
                commands_list = [
                    RecommendedCommand("status"),
                    RecommendedCommand("build", note="   (safe to re-run any time)"),
                ]
                if site_dir.exists():
                    commands_list.append(RecommendedCommand("validate"))
                    # upload-script is worth recommending regardless of
                    # whether either remote is configured now -- its
                    # --local mode needs neither (see upload.py).
                    commands_list.append(RecommendedCommand("upload-script"))
                commands = tuple(commands_list)
        statuses.append(ConfigStatus(path, config, status_lines, commands))
    return statuses, invalid


def _print_commands(tell: Callable[[str], None], config_name: str, entries: tuple[RecommendedCommand, ...]) -> None:
    """Print one `wpfreeze <subcommand> --config <config_name> <argv_extra><note>`
    line per entry, with subcommand names padded so every line's
    `--config` column lines up.
    """
    width = max(len(cmd.subcommand) for cmd in entries)
    for cmd in entries:
        extra = f" {' '.join(cmd.argv_extra)}" if cmd.argv_extra else ""
        tell(f"    wpfreeze {cmd.subcommand:<{width}} --config {config_name}{extra}{cmd.note}")


def print_overview(directory: Path = Path("."), tell: Callable[[str], None] = print) -> int:
    """Bare `wpfreeze` (no subcommand) when stdout/stdin isn't a real
    terminal: a non-interactive, side-effect-free summary of every site
    config in `directory` and the commands relevant to each one's current
    state, plus a pointer to getting started from scratch. Never reads
    stdin and never runs anything. In a real terminal, `wpfreeze.picker`'s
    interactive picker is used instead -- see `_dispatch` in cli.py -- but
    both render from the same `describe_configs` data, so this remains a
    faithful, scriptable equivalent of what the picker shows.
    """
    statuses, invalid = describe_configs(directory)

    tell("wpfreeze -- static-archive WordPress sites")
    tell("")

    if statuses:
        noun = "config" if len(statuses) == 1 else "configs"
        tell(f"Found {len(statuses)} site {noun} in this directory:")
        tell("")
        for status in statuses:
            tell(f"  {status.path.name}  ({status.config.base_url})")
            for line in status.status_lines:
                tell(f"    {line}")
            _print_commands(tell, status.path.name, status.commands)
            tell("")

    if invalid:
        noun = "file" if len(invalid) == 1 else "files"
        verb = "doesn't" if len(invalid) == 1 else "don't"
        names = ", ".join(p.name for p in invalid)
        tell(f"Found {len(invalid)} YAML {noun} that {verb} look like a valid site config: {names}")
        tell("")

    tell("Starting a new site, or fixing a config that isn't loading? Run `wpfreeze wizard`")
    tell("for a guided walkthrough, or see SETUP.md.")

    return 0


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
