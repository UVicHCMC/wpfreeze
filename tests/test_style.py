from __future__ import annotations

import io

from wpfreeze.style import bold, cyan, dim, green, red, supports_color, yellow


class _FakeTTYStream(io.StringIO):
    def __init__(self, isatty: bool = True):
        super().__init__()
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def test_bold_wraps_text_on_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeTTYStream(isatty=True)
    result = bold("Wrote config.yaml", stream=stream)
    assert result != "Wrote config.yaml"
    assert "Wrote config.yaml" in result  # substring survives -- callers `in`-match on it
    assert result.endswith("\033[0m")


def test_bold_is_a_noop_on_a_non_tty():
    stream = _FakeTTYStream(isatty=False)
    assert bold("Wrote config.yaml", stream=stream) == "Wrote config.yaml"


def test_all_helpers_are_noops_under_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = _FakeTTYStream(isatty=True)
    for fn in (bold, dim, cyan):
        assert fn("text", stream=stream) == "text"
    for fn in (green, yellow, red):
        assert fn("text", stream=stream) == "text"
        assert fn("text", bold_too=True, stream=stream) == "text"


def test_empty_string_is_never_styled(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeTTYStream(isatty=True)
    assert bold("", stream=stream) == ""


def test_supports_color_false_when_stream_isatty_raises():
    class _Broken:
        def isatty(self):
            raise OSError("no tty")

    assert supports_color(_Broken()) is False


def test_green_bold_too_includes_both_codes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeTTYStream(isatty=True)
    result = green("done", bold_too=True, stream=stream)
    assert "\033[32m" in result
    assert "\033[1m" in result


def test_yellow_and_red_bold_too_include_both_codes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _FakeTTYStream(isatty=True)
    yellow_result = yellow("review", bold_too=True, stream=stream)
    assert "\033[33m" in yellow_result and "\033[1m" in yellow_result
    red_result = red("attention", bold_too=True, stream=stream)
    assert "\033[31m" in red_result and "\033[1m" in red_result
