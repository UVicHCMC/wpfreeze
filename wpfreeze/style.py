"""Minimal hand-rolled ANSI text styling -- no `rich`/`colorama`/`click`
dependency, same "no dependencies without justification" rule progress.py
follows for its spinner. Every helper degrades to plain text, unchanged,
whenever the target stream isn't a real terminal (piped, redirected,
captured by a test) or the standard `NO_COLOR` (https://no-color.org)
environment variable is set -- callers never need to branch on this
themselves.
"""
from __future__ import annotations

import os
import sys

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_COLORS = {
    "green": "\033[32m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "red": "\033[31m",
}


def supports_color(stream=None) -> bool:
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _style(text: str, *codes: str, stream=None) -> str:
    if not text or not codes or not supports_color(stream):
        return text
    return f"{''.join(codes)}{text}{_RESET}"


def bold(text: str, stream=None) -> str:
    return _style(text, _BOLD, stream=stream)


def dim(text: str, stream=None) -> str:
    return _style(text, _DIM, stream=stream)


def green(text: str, bold_too: bool = False, stream=None) -> str:
    return _style(text, _COLORS["green"], *((_BOLD,) if bold_too else ()), stream=stream)


def cyan(text: str, stream=None) -> str:
    return _style(text, _COLORS["cyan"], stream=stream)


def yellow(text: str, bold_too: bool = False, stream=None) -> str:
    return _style(text, _COLORS["yellow"], *((_BOLD,) if bold_too else ()), stream=stream)


def red(text: str, bold_too: bool = False, stream=None) -> str:
    return _style(text, _COLORS["red"], *((_BOLD,) if bold_too else ()), stream=stream)
