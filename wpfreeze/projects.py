"""Project name handling: validation, and resolution of a project name (or
a config path, for backward compatibility) to its site config.

A "project" is just a site config, addressed by a short name instead of
its file path. This module owns the name-validation rule so
`cli.load_config` and `wizard.build_config_dict` apply exactly the same
one, rather than each hand-rolling a regex.

`list_projects`/`resolve_project` import `wizard`/`cli` inside their own
bodies, never at module scope: `cli.load_config` and `wizard.py` (module
scope) both import from this module, so a module-level import here in the
other direction would be a cycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wpfreeze.cli import SiteConfig

# Letters, digits, dash, underscore, dot -- no slashes. The name is
# interpolated into a filesystem path (output_dir, the config filename)
# and used to resolve a filename, so "/", "\", and ".." must never be
# accepted.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_name(name: str) -> str:
    """Returns `name` unchanged if it's a usable project name, otherwise
    raises ValueError with a message fit to surface to a user directly."""
    if not NAME_RE.match(name):
        raise ValueError(
            f"{name!r} isn't a usable project name -- letters, digits, dash, "
            "underscore, and dot only, and it can't start with one of those "
            "punctuation characters."
        )
    if ".." in name:
        raise ValueError(f"{name!r} isn't a usable project name -- '..' is not allowed.")
    return name


def normalize_name(name: str) -> str:
    """Lowercased, trimmed form used only for case-insensitive comparison
    -- never for display or path construction."""
    return name.strip().lower()


class ProjectNotFound(Exception):
    def __init__(self, token: str, known_names: list[str]) -> None:
        self.token = token
        self.known_names = known_names
        names = ", ".join(known_names) if known_names else "(none found)"
        super().__init__(f"There is no project called {token}.\nProjects in this directory: {names}")


class AmbiguousProject(Exception):
    def __init__(self, token: str, paths: list[Path]) -> None:
        self.token = token
        self.paths = paths
        listed = ", ".join(str(p) for p in paths)
        super().__init__(f"{token!r} matches more than one project config: {listed}")


@dataclass(frozen=True)
class Project:
    name: str
    path: Path  # the config file
    config: "SiteConfig"


def list_projects(directory: Path = Path(".")) -> list[Project]:
    """Every parseable site config directly in `directory`, as `Project`s.
    Wraps `wizard.scan_configs` -- the same directory scan `print_overview`/
    the picker already use -- rather than reimplementing it."""
    from wpfreeze.wizard import scan_configs  # deferred: see module docstring

    valid, _invalid = scan_configs(directory)
    return [Project(name=config.name, path=path, config=config) for path, config in valid]


def _looks_like_path(token: str, directory: Path) -> Path | None:
    """Returns the config path `token` names, if it looks like one at all
    (ends in .yaml/.yml, contains a path separator, or names a real file --
    checked both as given and relative to `directory`) -- otherwise None,
    meaning `token` should be resolved as a project name instead."""
    if token.endswith((".yaml", ".yml")) or "/" in token or "\\" in token:
        return Path(token)
    candidate = Path(token)
    if candidate.is_file():
        return candidate
    candidate = directory / token
    if candidate.is_file():
        return candidate
    return None


def resolve_project(token: str, directory: Path = Path(".")) -> Project:
    """Resolves `token` to a Project: as a config path (kept working for
    copy-pasted old `--config`-style commands), then a case-insensitive
    match on `config.name`, then a case-insensitive match on the config
    filename's stem. Raises ProjectNotFound if nothing matches, or
    AmbiguousProject if more than one config in `directory` claims the same
    name (whether via an explicit `name:` or its filename stem -- both are
    checked together, so one config's explicit name colliding with a
    different config's filename stem is caught too, not just two configs
    sharing the same explicit name)."""
    from wpfreeze.cli import load_config  # deferred: see module docstring

    path = _looks_like_path(token, directory)
    if path is not None:
        config = load_config(path)
        return Project(name=config.name, path=path, config=config)

    projects = list_projects(directory)
    norm_token = normalize_name(token)
    matches: dict[Path, Project] = {}
    for project in projects:
        if normalize_name(project.name) == norm_token or normalize_name(project.path.stem) == norm_token:
            matches[project.path] = project

    if not matches:
        raise ProjectNotFound(token, sorted({project.name for project in projects}))
    if len(matches) > 1:
        raise AmbiguousProject(token, sorted(matches))
    return next(iter(matches.values()))
