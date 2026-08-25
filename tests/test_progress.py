from __future__ import annotations

import io
import logging

from wpfreeze.progress import Progress


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
