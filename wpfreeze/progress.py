"""A hand-rolled, single-line terminal progress display -- no `rich`/`tqdm`/
`blessed` dependency, per this project's "no dependencies without
justification" rule (see CLAUDE-freeze-ux.md Part 2c).

Correctness of the surrounding output comes before cuteness, in this order:

1. Only ever writes when `stream.isatty()` -- piped, redirected,
   backgrounded, or CI output gets nothing from this module at all, not
   even a degraded fallback. `NO_COLOR` and `--no-progress`/
   `WPFREEZE_NO_PROGRESS=1` (passed in as `enabled=False` by the caller)
   both force it off too.
2. Never interleaves with `logging`. `wpfreeze.cli._configure_logging`
   attaches an INFO console `StreamHandler` to the "wpfreeze" logger; a
   live spinner and INFO lines on the same stream would produce garbage.
   `Progress.__enter__` raises that handler to WARNING and restores it on
   `__exit__`; anything that still gets through at WARNING+ has the live
   line cleared before it prints and redrawn after (see
   `_wrap_handler_emit`), so a warning mid-crawl never lands mid-line.
3. Thread-safe: `crawl_fixpoint` ticks from N worker threads. Every counter
   read/write and render happens under one `threading.Lock`; there is no
   background render thread, since a daemon thread that outlives an
   exception is worse than a slightly jerky spinner.
4. Cheap: a tick is an integer increment plus a monotonic-clock compare
   against the last render, throttled to ~10/sec.
5. A `UnicodeEncodeError` from the decorative spinner glyph must never be
   able to kill a two-hour acquire -- every write is wrapped in a bare
   `try/except Exception` that disables the display permanently rather
   than propagating.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from types import TracebackType

# Ten-frame braille cycle -- Greg's call, 2026-08-25: it animates smoothly
# because every frame carries the same visual weight, unlike a mixed-glyph
# snowflake cycle, which flickers.
_SPINNER_UTF8 = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_ASCII = "-\\|/"

_RENDER_INTERVAL = 0.1  # ~10/sec cap, see module docstring point 4

_LOGGER_NAME = "wpfreeze"


def _uses_utf8(stream) -> bool:
    encoding = (getattr(stream, "encoding", None) or "").lower()
    return "utf-8" in encoding or "utf8" in encoding


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class Progress:
    """One live single-line display for the duration of a `with` block.
    Safe to use even when disabled (non-tty, --no-progress) -- every method
    becomes a no-op rather than the caller having to branch."""

    def __init__(
        self,
        label: str,
        total: int | None = None,
        stream=None,
        enabled: bool = True,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self._label = label
        self._total = total
        self._count = 0
        self._detail = ""
        self._frame = 0
        self._started = time.monotonic()
        self._last_render = 0.0
        self._line_len = 0  # length of the last-drawn line, for erase-to-end
        self._spinner = _SPINNER_UTF8 if _uses_utf8(self._stream) else _SPINNER_ASCII
        self._saved_handler_state: list[tuple[logging.Handler, int, object]] = []

        no_color_or_progress = os.environ.get("WPFREEZE_NO_PROGRESS") == "1"
        self._enabled = bool(enabled and not no_color_or_progress and self._safe_isatty())
        self._disabled_by_error = False

    def _safe_isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    @property
    def active(self) -> bool:
        return self._enabled and not self._disabled_by_error

    # -- logging interleave guard -------------------------------------------

    def __enter__(self) -> "Progress":
        if self.active:
            self._suspend_console_logging()
            self._render(force=True)  # paint the initial line even if nothing ticks (e.g. a spinner-only phase)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.active:
            self._clear_line()
            self._restore_console_logging()

    @contextmanager
    def suspend(self):
        """Tears the live display down for the duration of the `with`
        block -- e.g. around an `input()` prompt, which would otherwise be
        printed over/under a live spinner -- and puts it back exactly
        where it left off (frame count, elapsed clock) afterward. A no-op
        if the display isn't active (non-tty, already disabled by a write
        error, etc.)."""
        if not self.active:
            yield
            return
        self._clear_line()
        self._restore_console_logging()
        try:
            yield
        finally:
            self._suspend_console_logging()
            self._render(force=True)

    def _console_handlers(self) -> list[logging.Handler]:
        package_logger = logging.getLogger(_LOGGER_NAME)
        return [
            h
            for h in package_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]

    def _suspend_console_logging(self) -> None:
        for handler in self._console_handlers():
            self._saved_handler_state.append((handler, handler.level, handler.emit))
            handler.setLevel(logging.WARNING)
            handler.emit = self._wrap_handler_emit(handler, handler.emit)  # type: ignore[method-assign]

    def _restore_console_logging(self) -> None:
        for handler, level, emit in self._saved_handler_state:
            handler.setLevel(level)
            handler.emit = emit  # type: ignore[method-assign]
        self._saved_handler_state = []

    def _wrap_handler_emit(self, handler: logging.Handler, original_emit):
        def emit(record: logging.LogRecord) -> None:
            self._clear_line()
            original_emit(record)
            self._redraw()

        return emit

    # -- rendering ------------------------------------------------------------

    def _write(self, text: str) -> None:
        if self._disabled_by_error:
            return
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:
            self._disabled_by_error = True

    def _clear_line(self) -> None:
        if not self.active:
            return
        self._write("\r" + " " * self._line_len + "\r")
        self._line_len = 0

    def _redraw(self) -> None:
        if not self.active:
            return
        self._render(force=True)

    def _render(self, force: bool = False) -> None:
        if not self.active:
            return
        now = time.monotonic()
        if not force and (now - self._last_render) < _RENDER_INTERVAL:
            return
        self._last_render = now

        glyph = self._spinner[self._frame % len(self._spinner)]
        self._frame += 1
        elapsed = format_duration(now - self._started)

        if self._total:
            counter = f"{self._count}/{self._total}"
        elif self._count:
            counter = str(self._count)
        else:
            counter = ""

        parts = [glyph, self._label]
        if counter:
            parts.append(counter)
        if self._detail:
            parts.append(f"▸ {self._detail}")
        parts.append(elapsed)
        line = "  ".join(parts)

        pad = " " * max(0, self._line_len - len(line))
        self._write(f"\r{line}{pad}")
        self._line_len = len(line)

    # -- public API -------------------------------------------------------

    def phase(self, label: str, total: int | None = None) -> None:
        """Switches to a new named phase (e.g. inventory -> crawl ->
        wayback), resetting the counter but not the overall elapsed clock."""
        with self._lock:
            self._label = label
            self._total = total
            self._count = 0
            self._detail = ""
            self._render(force=True)

    def tick(self, n: int = 1, detail: str = "") -> None:
        with self._lock:
            self._count += n
            if detail:
                self._detail = detail
            self._render()

    def log(self, message: str) -> None:
        """Prints `message` above the live line -- for a caller that wants
        to say something mid-run without going through `logging` at all."""
        with self._lock:
            self._clear_line()
            self._write(message.rstrip("\n") + "\n")
            self._render(force=True)

    def finish(self, summary: str = "") -> None:
        with self._lock:
            self._clear_line()
            if summary:
                self._write(summary.rstrip("\n") + "\n")
