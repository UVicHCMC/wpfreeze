"""Project name handling: validation and (see Part 2) resolution of a
project name to its site config.

A "project" is just a site config, addressed by a short name instead of
its file path -- see CLAUDE-freeze-ux.md Part 1/2. This module owns the
name-validation rule so `cli.load_config` and `wizard.build_config_dict`
apply exactly the same one, rather than each hand-rolling a regex.
"""
from __future__ import annotations

import re

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
