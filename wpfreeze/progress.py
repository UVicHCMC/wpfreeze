"""A hand-rolled, single-line terminal progress display -- no `rich`/`tqdm`/
`blessed` dependency, per this project's "no dependencies without
justification" rule (see the freeze UX design notes Part 2c).

Correctness of the surrounding output comes before cuteness, in this order:

1. Only ever writes when `stream.isatty()` -- piped, redirected,
   backgrounded, or CI output gets nothing from this module at all, not
   even a degraded fallback. `WPFREEZE_NO_PROGRESS=1` forces the whole
   display off too. `NO_COLOR` (see wpfreeze.style) is narrower: it only
   turns the finish() checkmark plain, since colour is the only thing
   this module uses it for -- the spinner glyphs themselves carry no
   colour to disable.
2. Never interleaves with `logging`. `wpfreeze.cli._configure_logging`
   attaches an INFO console `StreamHandler` to the "wpfreeze" logger; a
   live spinner and INFO lines on the same stream would produce garbage.
   `Progress.__enter__` raises that handler to WARNING and restores it on
   `__exit__`; anything that still gets through at WARNING+ has the live
   line cleared before it prints and redrawn after (see
   `_wrap_handler_emit`), so a warning mid-crawl never lands mid-line.
3. Thread-safe: `crawl_fixpoint` ticks from N worker threads, and a single
   background repaint thread (see point 4) also renders concurrently with
   all of them. Every counter read/write and render happens under one
   `threading.Lock`. That background thread is scoped tightly to one
   `with Progress(...)` block's lifetime -- started at the end of
   `__enter__`, always stopped and `join()`-ed at the very top of
   `__exit__` (before anything else there, including on the exception
   path -- `__exit__` still runs when the block's body raises) via a
   `threading.Event` it wakes on rather than sleeps blindly through, so
   shutdown is immediate, not a timeout wait. `daemon=True` besides, so
   even a `join()` that somehow never got a chance to run cannot keep the
   process alive. A deliberate reversal of the original "no background
   thread" design here, made because tick-driven-only
   redraws made slow-cadence phases like inventory discovery (~1 tick/sec)
   look completely frozen rather than "slightly jerky."
4. Cheap: a tick is an integer increment plus a monotonic-clock compare
   against the last render, throttled to ~10/sec -- the same throttle the
   background thread's own wake interval is tuned to, so the two driving
   sources (real ticks, and the timer) never fight over how often the
   line actually repaints.
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

from wpfreeze.style import green

# Ten-frame braille cycle -- chosen because it animates smoothly
# because every frame carries the same visual weight, unlike a mixed-glyph
# snowflake cycle, which flickers.
_SPINNER_UTF8 = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_ASCII = "-\\|/"

# What replaces the spinner glyph on finish() -- a clear "done" signal
# rather than the spinner just stopping wherever its last frame happened to
# land, which read as frozen/stuck rather than complete.
_DONE_GLYPH_UTF8 = "✓"
_DONE_GLYPH_ASCII = "OK"

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
        self._done_glyph = _DONE_GLYPH_UTF8 if _uses_utf8(self._stream) else _DONE_GLYPH_ASCII
        self._saved_handler_state: list[tuple[logging.Handler, int, object]] = []

        no_color_or_progress = os.environ.get("WPFREEZE_NO_PROGRESS") == "1"
        self._enabled = bool(enabled and not no_color_or_progress and self._safe_isatty())
        self._disabled_by_error = False
        # Set by finish() -- once the done checkmark is drawn, nothing may
        # draw a spinner frame over it again. Deliberately NOT folded into
        # `active`: __exit__/suspend() still need to restore the console
        # logging handler regardless of this flag, only the spinner glyph
        # itself is affected (see _render's own guard).
        self._finished = False
        # Set for the duration of suspend()'s yielded block -- stops the
        # background repaint thread from drawing a spinner frame over
        # whatever the caller is doing with the terminal (typically an
        # input() prompt) during that window. See _render's own guard.
        self._suspended = False
        # Background repaint thread -- see the module docstring's point 3.
        # None until __enter__ starts it (only when active); __exit__
        # always stops and joins it via this event, never a bare sleep.
        self._render_thread: threading.Thread | None = None
        self._stop_render_thread = threading.Event()

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
            with self._lock:
                self._render(force=True)  # paint the initial line even if nothing ticks (e.g. a spinner-only phase)
            self._render_thread = threading.Thread(target=self._background_render_loop, daemon=True)
            self._render_thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Stopped first, unconditionally, before anything else here
        # (including on the exception path) -- nothing may still be
        # repainting once __exit__ starts tearing the display down.
        if self._render_thread is not None:
            self._stop_render_thread.set()
            self._render_thread.join()
            self._render_thread = None
        if self.active:
            with self._lock:
                self._clear_line()
            self._restore_console_logging()

    def _background_render_loop(self) -> None:
        """Repaints the live line on a timer, independent of how often real
        work ticks -- without this, a slow-cadence phase (inventory
        discovery: roughly one tick per HTTP request) looks completely
        frozen between ticks rather than spinning. Wakes on `_stop_
        render_thread` instead of sleeping blindly, so __exit__'s join()
        returns immediately rather than waiting out a stale timeout."""
        while not self._stop_render_thread.wait(_RENDER_INTERVAL):
            with self._lock:
                self._render()

    @contextmanager
    def suspend(self):
        """Tears the live display down for the duration of the `with`
        block -- e.g. around an `input()` prompt, which would otherwise be
        printed over/under a live spinner -- and puts it back exactly
        where it left off (frame count, elapsed clock) afterward. A no-op
        if the display isn't active (non-tty, already disabled by a write
        error, etc.).

        Sets `_suspended` for the duration of the yielded block, under the
        lock, so the background repaint thread (running the whole time --
        nothing pauses it here) can't redraw a spinner frame over whatever
        the caller is doing with the terminal in the meantime; see
        `_render`'s own guard. The `_clear_line()`/final `_render()` calls
        are lock-protected too, for the same reason -- they used to be
        safe unlocked since nothing else rendered concurrently, which
        stopped being true the moment a background thread existed."""
        if not self.active:
            yield
            return
        with self._lock:
            self._suspended = True
            self._clear_line()
        self._restore_console_logging()
        try:
            yield
        finally:
            self._suspend_console_logging()
            with self._lock:
                self._suspended = False
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
            # Lock-protected since the background repaint thread (see
            # module docstring point 3) now touches the same line/stream
            # state continuously for the whole life of the display -- an
            # unlocked clear/redraw here used to be safe only because
            # nothing else rendered concurrently, which stopped being true
            # the moment that thread started existing.
            with self._lock:
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
        # Once finish() has run, the line is already closed with a real
        # newline and self._line_len is already 0 -- nothing left to erase,
        # and writing the bare \r pair anyway (e.g. from __exit__, which
        # still runs unconditionally) would just be a wasted flush.
        if not self.active or self._finished:
            return
        self._write("\r" + " " * self._line_len + "\r")
        self._line_len = 0

    def _redraw(self) -> None:
        if not self.active:
            return
        self._render(force=True)

    def _render(self, force: bool = False) -> None:
        if not self.active or self._finished or self._suspended:
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
        """Redraws the live line one last time with the spinner glyph
        replaced by a done checkmark, closed with a real newline rather
        than the bare `\\r` every other render uses. Call this before any
        plain `print()` a caller is about to make right after the tracked
        work finishes -- without it, that `print()` lands on the same
        terminal row the spinner never got a chance to vacate (nothing
        else clears/redraws that row until `__exit__`, which by then runs
        too late), producing exactly the "animation looks stuck, then the
        next line runs into it" glitch this exists to prevent. A no-op,
        same as every other render call, when the display isn't active.

        Idempotent and terminal: a second call draws nothing further, and
        no later tick()/phase()/suspend() can put a spinner frame back on
        screen after this -- see _render's own `_finished` guard. A
        subsequent `with Progress(...)` block's __exit__ still restores
        the suspended console-logging handler regardless (that check is
        not gated on `_finished`), only the visual redraw is suppressed.
        """
        with self._lock:
            if self.active and not self._finished:
                elapsed = format_duration(time.monotonic() - self._started)
                visible = f"{self._done_glyph}  {self._label}  {elapsed}"
                glyph = green(self._done_glyph, bold_too=True, stream=self._stream)
                pad = " " * max(0, self._line_len - len(visible))
                self._write(f"\r{glyph}  {self._label}  {elapsed}{pad}\n")
                self._line_len = 0
            self._finished = True
            if summary:
                self._write(summary.rstrip("\n") + "\n")
