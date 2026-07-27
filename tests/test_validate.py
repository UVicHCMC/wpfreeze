from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from wpfreeze.validate import (
    VnuUnavailable,
    ensure_vnu_jar,
    format_validation_summary,
    validate_site,
    write_validation_report,
)


def _vnu_message(url: str, message: str, msg_type: str = "error", extract: str = "") -> dict:
    return {"type": msg_type, "url": url, "message": message, "extract": extract}


def _fake_run(monkeypatch, *, stdout: str = "", stderr: str = "", returncode: int = 0, raises: Exception | None = None):
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if raises is not None:
            raise raises

        class _Result:
            pass

        result = _Result()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    monkeypatch.setattr("wpfreeze.validate.subprocess.run", _run)
    return calls


def _site_with_pages(tmp_path: Path, names: list[str]) -> Path:
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    for name in names:
        (site_dir / name).write_text("<!doctype html><title>x</title>", encoding="utf-8")
    return site_dir


def test_validate_site_groups_errors_by_message_across_pages(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html", "b.html"])
    resolved = site_dir.resolve()
    messages = {
        "messages": [
            _vnu_message(f"file:{resolved / 'a.html'}", "Attribute “fetchpriority” not allowed on element “img”."),
            _vnu_message(f"file:{resolved / 'b.html'}", "Attribute “fetchpriority” not allowed on element “img”."),
            _vnu_message(f"file:{resolved / 'a.html'}", "Duplicate ID “tip1”."),
        ]
    }
    _fake_run(monkeypatch, stdout=json.dumps(messages))

    report = validate_site(Path("vnu.jar"), site_dir)

    assert report.documents_checked == 2
    assert report.total_messages == 3
    assert len(report.issues) == 2
    # sorted by count descending
    assert report.issues[0].message.startswith("Attribute")
    assert report.issues[0].count == 2
    assert report.issues[0].pages == ["a.html", "b.html"]
    assert report.issues[1].count == 1
    assert not report.ok


def test_validate_site_reports_ok_when_no_errors(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    _fake_run(monkeypatch, stdout=json.dumps({"messages": []}))

    report = validate_site(Path("vnu.jar"), site_dir)

    assert report.ok
    assert report.total_messages == 0
    assert "no errors" in format_validation_summary(report)


def test_validate_site_ignores_non_error_messages(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    resolved = site_dir.resolve()
    messages = {
        "messages": [
            _vnu_message(f"file:{resolved / 'a.html'}", "some info notice", msg_type="info"),
        ]
    }
    _fake_run(monkeypatch, stdout=json.dumps(messages))

    report = validate_site(Path("vnu.jar"), site_dir)

    assert report.ok
    assert report.total_messages == 0


def test_missing_java_raises_vnu_unavailable(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    _fake_run(monkeypatch, raises=FileNotFoundError("java"))

    with pytest.raises(VnuUnavailable):
        validate_site(Path("vnu.jar"), site_dir)


def test_empty_stdout_raises_vnu_unavailable(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    _fake_run(monkeypatch, stdout="", stderr="Error: could not find or load main class", returncode=1)

    with pytest.raises(VnuUnavailable):
        validate_site(Path("vnu.jar"), site_dir)


def test_malformed_json_raises_vnu_unavailable(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    _fake_run(monkeypatch, stdout="not json")

    with pytest.raises(VnuUnavailable):
        validate_site(Path("vnu.jar"), site_dir)


def test_format_validation_summary_truncates_and_points_at_full_report(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    resolved = site_dir.resolve()
    messages = {
        "messages": [_vnu_message(f"file:{resolved / 'a.html'}", f"issue number {i}") for i in range(15)]
    }
    _fake_run(monkeypatch, stdout=json.dumps(messages))

    report = validate_site(Path("vnu.jar"), site_dir)
    summary = format_validation_summary(report)

    assert "15 distinct issue(s)" in summary
    assert "more distinct issue(s)" in summary


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content_bytes = content
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size: int):
        yield self.content_bytes

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, response=None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.requests = []

    def get(self, url, headers=None, stream=None, timeout=None, allow_redirects=None):
        self.requests.append({"url": url, "headers": headers or {}})
        if self.exc is not None:
            raise self.exc
        return self.response


def test_ensure_vnu_jar_downloads_when_no_cache(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    session = _FakeSession(
        response=_FakeResponse(200, content=b"fake-jar-bytes", headers={"ETag": '"abc"', "Last-Modified": "Mon"})
    )

    jar_path = ensure_vnu_jar(session=session, cache_dir=cache_dir)

    assert jar_path == cache_dir / "vnu.jar"
    assert jar_path.read_bytes() == b"fake-jar-bytes"
    meta = json.loads((cache_dir / "vnu.jar.meta.json").read_text())
    assert meta == {"etag": '"abc"', "last_modified": "Mon"}
    assert session.requests[0]["headers"] == {}  # no cached etag to send yet


def test_ensure_vnu_jar_uses_cache_on_304_and_sends_etag(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "vnu.jar").write_bytes(b"already-cached")
    (cache_dir / "vnu.jar.meta.json").write_text(json.dumps({"etag": '"abc"', "last_modified": "Mon"}))
    session = _FakeSession(response=_FakeResponse(304))

    jar_path = ensure_vnu_jar(session=session, cache_dir=cache_dir)

    assert jar_path.read_bytes() == b"already-cached"
    assert session.requests[0]["headers"] == {"If-None-Match": '"abc"'}


def test_ensure_vnu_jar_falls_back_to_cache_on_network_error(tmp_path: Path, caplog):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "vnu.jar").write_bytes(b"already-cached")
    session = _FakeSession(exc=requests.ConnectionError("offline"))

    jar_path = ensure_vnu_jar(session=session, cache_dir=cache_dir)

    assert jar_path.read_bytes() == b"already-cached"


def test_ensure_vnu_jar_raises_when_no_cache_and_network_fails(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    session = _FakeSession(exc=requests.ConnectionError("offline"))

    with pytest.raises(VnuUnavailable):
        ensure_vnu_jar(session=session, cache_dir=cache_dir)


def test_ensure_vnu_jar_raises_on_bad_status_with_no_cache(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    session = _FakeSession(response=_FakeResponse(404))

    with pytest.raises(VnuUnavailable):
        ensure_vnu_jar(session=session, cache_dir=cache_dir)


def test_write_validation_report_round_trips(tmp_path: Path):
    from wpfreeze.validate import ValidationIssue, ValidationReport

    report = ValidationReport(
        documents_checked=2,
        total_messages=2,
        issues=[ValidationIssue(message="boom", count=2, pages=["a.html", "b.html"], sample_extract="xyz")],
    )
    path = write_validation_report(report, tmp_path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["distinct_issues"] == 1
    assert data["issues"][0]["message"] == "boom"
    assert data["issues"][0]["pages"] == ["a.html", "b.html"]


def test_ensure_vnu_jar_falls_back_to_cache_when_the_download_drops_mid_stream(
    tmp_path: Path, caplog
):
    """The 32MB body stream is likelier to be interrupted than the
    handshake, but only the handshake used to be guarded -- so a dropped
    connection escaped as a raw ConnectionError past run_validate, which
    catches only VnuUnavailable."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "vnu.jar").write_bytes(b"already-cached")

    class _DropsMidStream:
        status_code = 200
        headers: dict = {}

        def iter_content(self, chunk_size: int):
            yield b"partial"
            raise requests.ConnectionError("connection reset")

        def close(self) -> None:
            pass

    class _Session:
        def get(self, *args, **kwargs):
            return _DropsMidStream()

    jar_path = ensure_vnu_jar(session=_Session(), cache_dir=cache_dir)

    assert jar_path == cache_dir / "vnu.jar"
    assert jar_path.read_bytes() == b"already-cached"  # untouched
    assert not (cache_dir / "vnu.jar.tmp").exists()  # partial file cleaned up


def test_ensure_vnu_jar_raises_vnu_unavailable_if_the_stream_drops_with_no_cache(tmp_path: Path):
    class _DropsMidStream:
        status_code = 200
        headers: dict = {}

        def iter_content(self, chunk_size: int):
            raise requests.ConnectionError("connection reset")

        def close(self) -> None:
            pass

    class _Session:
        def get(self, *args, **kwargs):
            return _DropsMidStream()

    with pytest.raises(VnuUnavailable):
        ensure_vnu_jar(session=_Session(), cache_dir=tmp_path / "cache")


def test_format_validation_summary_handles_an_issue_with_no_pages(monkeypatch, tmp_path: Path):
    """VNU messages need not carry a url; `pages` is built by skipping
    those, so it can legitimately be empty and must not be indexed."""
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    messages = {"messages": [{"type": "error", "message": "no url on this one", "extract": "x"}]}
    _fake_run(monkeypatch, stdout=json.dumps(messages))

    report = validate_site(Path("vnu.jar"), site_dir)

    summary = format_validation_summary(report)  # used to raise IndexError
    assert "no url on this one" in summary
    assert "0 page(s)" in summary
