from __future__ import annotations

import io
import logging
import threading
import time

from wpfreeze.progress import _SPINNER_UTF8, Progress


class _FakeTTYStream(io.StringIO):
    def __init__(self, isatty: bool = True, encoding: str = "utf-8"):
        super().__init__()
        self._isatty = isatty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._isatty

    @property
    def encoding(self) -> str:
        return self._encoding


class _RaisingStream:
    """write() always raises -- for the UnicodeEncodeError-must-not-
    propagate test."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> None:
        raise UnicodeEncodeError("ascii", text, 0, 1, "boom")

    def flush(self) -> None:
        pass


def test_non_tty_stream_writes_nothing():
    stream = _FakeTTYStream(isatty=False)
    progress = Progress("Acquiring example.com", total=10, stream=stream)
    with progress:
        progress.tick()
        progress.tick()
    assert stream.getvalue() == ""


def test_tty_stream_ticks_produce_carriage_return_writes_no_stray_newlines():
    stream = _FakeTTYStream(isatty=True)
    progress = Progress("Acquiring example.com", total=10, stream=stream)
    with progress:
        progress.tick(force := 1)  # noqa: F841 -- just a tick
        progress._render(force=True)

    output = stream.getvalue()
    assert output  # something was written
    assert "\r" in output
    # No bare newline should appear until finish()/log() explicitly writes one.
    assert "\n" not in output


def test_context_manager_restores_console_handler_level():
    package_logger = logging.getLogger("wpfreeze")
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    package_logger.handlers.append(console_handler)
    try:
        stream = _FakeTTYStream(isatty=True)
        progress = Progress("Building", stream=stream)
        with progress:
            assert console_handler.level == logging.WARNING
        assert console_handler.level == logging.INFO
    finally:
        package_logger.handlers.remove(console_handler)


def test_suspend_clears_line_and_restores_logging_then_resumes():
    package_logger = logging.getLogger("wpfreeze")
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    package_logger.handlers.append(console_handler)
    try:
        stream = _FakeTTYStream(isatty=True)
        progress = Progress("Building", stream=stream)
        with progress:
            progress.tick()
            with progress.suspend():
                # Logging is back to normal for the duration of the prompt.
                assert console_handler.level == logging.INFO
            # And re-suspended (and redrawn) once the prompt is done.
            assert console_handler.level == logging.WARNING
    finally:
        package_logger.handlers.remove(console_handler)


def test_suspend_is_a_noop_when_not_active():
    stream = _FakeTTYStream(isatty=False)  # inactive: not a tty
    progress = Progress("Building", stream=stream)
    with progress:
        with progress.suspend():
            pass  # must not raise


def test_unicode_encode_error_disables_display_without_propagating():
    stream = _RaisingStream()
    progress = Progress("Acquiring example.com", stream=stream)
    with progress:
        progress.tick()  # must not raise
        progress.tick()
    assert progress._disabled_by_error is True


def test_disabled_when_not_a_tty_even_if_enabled_true():
    stream = _FakeTTYStream(isatty=False)
    progress = Progress("Acquiring", stream=stream, enabled=True)
    assert progress.active is False


def test_disabled_via_env_var(monkeypatch):
    monkeypatch.setenv("WPFREEZE_NO_PROGRESS", "1")
    stream = _FakeTTYStream(isatty=True)
    progress = Progress("Acquiring", stream=stream)
    assert progress.active is False


# ---------------------------------------------------------------------------
# finish(): the done checkmark, and why it must be immune to anything that
# runs after it (see progress.py's own comment on _finished vs. active)
# ---------------------------------------------------------------------------


def test_finish_writes_a_checkmark_terminated_with_a_real_newline(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeTTYStream(isatty=True)
    progress = Progress("Discovering inventory", stream=stream)
    with progress:
        progress.tick()
        progress.finish()

    output = stream.getvalue()
    assert "✓" in output
    # Closed with a real newline -- a caller's plain print() right after
    # this must land on a fresh row, not the spinner's old \r-anchored one.
    assert output.endswith("\n")
    # Green + bold, not plain text -- the checkmark is meant to read as a
    # clear "done" signal, distinct from the spinner glyphs it replaces.
    assert "\033[32m" in output and "\033[1m" in output


def test_finish_is_a_noop_under_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = _FakeTTYStream(isatty=True)
    progress = Progress("Discovering inventory", stream=stream)
    with progress:
        progress.finish()
    assert "\033[" not in stream.getvalue()
    assert "✓" in stream.getvalue()


def test_finish_uses_ascii_checkmark_fallback_on_a_non_utf8_stream(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeTTYStream(isatty=True, encoding="ascii")
    progress = Progress("Discovering inventory", stream=stream)
    with progress:
        progress.finish()
    assert "OK" in stream.getvalue()
    assert "✓" not in stream.getvalue()


def test_finish_is_idempotent_and_blocks_any_later_spinner_redraw(monkeypatch):
    """The real bug this exists to fix: a print() landing on the spinner's
    still-live row because nothing cleared it first. finish() must be the
    last thing that can ever draw a spinner frame for this instance -- not
    just clear once -- or a later tick()/suspend() resume could put a
    fresh spinner frame back on screen after the checkmark, reintroducing
    exactly that glitch."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeTTYStream(isatty=True)
    progress = Progress("Discovering inventory", stream=stream)
    with progress:
        progress.finish()
        before = stream.getvalue()
        progress.tick()  # must not draw anything further
        progress.phase("Crawling")  # ditto
        progress.finish()  # second call: must not draw a second checkmark
    assert stream.getvalue() == before


def test_finish_still_lets_exit_restore_console_logging(monkeypatch):
    """finish() must not short-circuit __exit__'s logging restore -- only
    the visual redraw is meant to become inert afterward (see progress.py's
    comment on why _finished is not folded into `active`)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    package_logger = logging.getLogger("wpfreeze")
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    package_logger.handlers.append(console_handler)
    try:
        stream = _FakeTTYStream(isatty=True)
        progress = Progress("Building", stream=stream)
        with progress:
            progress.finish()
            assert console_handler.level == logging.WARNING
        assert console_handler.level == logging.INFO
    finally:
        package_logger.handlers.remove(console_handler)


def test_finish_on_inactive_progress_is_a_noop():
    stream = _FakeTTYStream(isatty=False)  # inactive: not a tty
    progress = Progress("Building", stream=stream)
    with progress:
        progress.finish()  # must not raise
    assert stream.getvalue() == ""


# ---------------------------------------------------------------------------
# Background repaint thread: the animation must advance on its own, on a
# timer, independent of how often (or rarely) real work actually ticks --
# see the module docstring's point 3 for why this exists (a slow-cadence
# phase like inventory discovery used to look completely frozen between
# real ticks, ~1/sec).
# ---------------------------------------------------------------------------


def test_background_thread_advances_the_spinner_with_no_ticks_at_all():
    stream = _FakeTTYStream(isatty=True)
    progress = Progress("Discovering inventory", stream=stream)
    with progress:
        # Long enough for several _RENDER_INTERVAL (0.1s)-paced background
        # wakes with nothing ever calling tick()/phase()/log().
        time.sleep(0.35)
    output = stream.getvalue()
    frames_seen = {ch for ch in output if ch in _SPINNER_UTF8}
    assert len(frames_seen) >= 2, f"expected multiple distinct spinner frames, got {frames_seen!r}"


def test_background_thread_is_stopped_and_joined_on_exit():
    stream = _FakeTTYStream(isatty=True)
    before = set(threading.enumerate())
    with Progress("Discovering inventory", stream=stream):
        during = threading.enumerate()
        assert len(during) == len(before) + 1
    after = set(threading.enumerate())
    # __exit__ joins the thread before returning -- nothing left dangling,
    # regardless of how long the background wake interval is.
    assert after == before


def test_background_thread_is_stopped_and_joined_even_when_the_block_raises():
    stream = _FakeTTYStream(isatty=True)
    before = set(threading.enumerate())
    try:
        with Progress("Discovering inventory", stream=stream):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert set(threading.enumerate()) == before


def test_no_background_thread_when_not_active():
    stream = _FakeTTYStream(isatty=False)  # inactive: not a tty
    before = set(threading.enumerate())
    with Progress("Discovering inventory", stream=stream):
        assert set(threading.enumerate()) == before  # no thread started at all


def test_suspend_blocks_background_redraws_for_its_duration():
    """The background thread keeps running through suspend() (nothing
    pauses the thread itself) -- what must actually stop is it drawing a
    spinner frame over whatever the caller is doing with the terminal, e.g.
    an input() prompt, for the whole yielded window."""
    stream = _FakeTTYStream(isatty=True)
    progress = Progress("Building", stream=stream)
    with progress:
        with progress.suspend():
            before = len(stream.getvalue())
            time.sleep(0.35)  # several background-thread wake intervals
            after = len(stream.getvalue())
            assert after == before, "background thread wrote during suspend()"
