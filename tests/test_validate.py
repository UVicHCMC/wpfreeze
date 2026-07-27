from __future__ import annotations

import json
import shutil
import subprocess
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


def test_an_unreadable_document_fails_the_run_with_an_actionable_message(
    monkeypatch, tmp_path: Path
):
    """Confirmed against VNU 26.7.22: a file it cannot open kills the whole
    run with an uncaught FileNotFoundException and leaves stdout truncated
    mid-JSON. The only symptom used to be "could not parse vnu output as
    JSON", which points the reader at the parser rather than at their own
    file permissions."""
    site_dir = _site_with_pages(tmp_path, ["good.html", "locked.html"])
    (site_dir / "locked.html").chmod(0o000)
    try:
        _fake_run(monkeypatch, stdout=json.dumps({"messages": []}))
        with pytest.raises(VnuUnavailable) as excinfo:
            validate_site(Path("vnu.jar"), site_dir)
        assert "locked.html" in str(excinfo.value)
        assert "cannot be read" in str(excinfo.value)
    finally:
        (site_dir / "locked.html").chmod(0o644)


def test_truncated_vnu_output_surfaces_what_vnu_said_on_stderr(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    _fake_run(
        monkeypatch,
        stdout='{"messages":[{"type":"error"',
        stderr="java.io.FileNotFoundException: /x/y.html (Permission denied)",
    )
    with pytest.raises(VnuUnavailable) as excinfo:
        validate_site(Path("vnu.jar"), site_dir)
    assert "FileNotFoundException" in str(excinfo.value)


def test_directories_named_like_documents_are_not_counted(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["real.html"])
    (site_dir / "notadoc.html").mkdir()
    _fake_run(monkeypatch, stdout=json.dumps({"messages": []}))

    report = validate_site(Path("vnu.jar"), site_dir)
    assert report.documents_found == 1


@pytest.mark.skipif(shutil.which("java") is None, reason="VNU needs a JVM")
@pytest.mark.skipif(
    not (Path.home() / ".cache/wpfreeze/vnu.jar").exists(), reason="no cached vnu.jar"
)
def test_against_the_real_vnu_jar(tmp_path: Path):
    """End-to-end against the actual checker: a genuine markup error is
    found, and an unreadable sibling is reported as unchecked rather than
    folded into a clean result."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "good.html").write_text(
        "<!DOCTYPE html><html lang=en><head><title>t</title></head><body><p>ok</p></body></html>"
    )
    (site / "bad.html").write_text(
        "<!DOCTYPE html><html lang=en><head><title>t</title></head><body><img src=x.png></body></html>"
    )
    report = validate_site(Path.home() / ".cache/wpfreeze/vnu.jar", site)

    assert report.documents_found == 2
    assert report.documents_checked == 2
    assert any("alt" in issue.message for issue in report.issues)
    assert "1 of" not in format_validation_summary(report)


def test_stylesheets_are_checked_as_well_as_documents(monkeypatch, tmp_path: Path):
    """build.py lifts CSS repeated across pages out of the markup and into
    shared .css files. An HTML-only pass would stop reporting defects it
    used to catch, purely because the bytes moved -- the report would look
    cleaner without the site being any better."""
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    (site_dir / "shared.css").write_text("body{background-repeat-y:no-repeat}", encoding="utf-8")

    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        payload = {"messages": []}
        if "--skip-non-css" in cmd:
            payload = {
                "messages": [
                    {
                        "type": "error",
                        "url": f"file:{site_dir / 'shared.css'}",
                        "message": 'CSS: "background-repeat-y": Property doesn\'t exist.',
                        "extract": "background-repeat-y",
                    }
                ]
            }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    report = validate_site(Path("vnu.jar"), site_dir)

    assert len(calls) == 2, "expected an HTML pass and a CSS pass"
    assert any("--skip-non-html" in c for c in calls)
    assert any("--skip-non-css" in c for c in calls)
    assert report.stylesheets_checked == 1
    assert any("background-repeat-y" in i.message for i in report.issues)
    assert "shared.css" in report.issues[0].pages


def test_no_css_pass_is_run_when_the_site_has_no_stylesheets(monkeypatch, tmp_path: Path):
    """VNU emits nothing at all when its walk matches no files, which
    _run_vnu treats as an invocation failure -- so don't invoke it."""
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"messages": []}), "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    report = validate_site(Path("vnu.jar"), site_dir)
    assert len(calls) == 1
    assert report.stylesheets_checked == 0


def test_an_unreadable_stylesheet_is_caught_too(tmp_path: Path, monkeypatch):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    locked = site_dir / "locked.css"
    locked.write_text("a{}", encoding="utf-8")
    locked.chmod(0o000)
    try:
        _fake_run(monkeypatch, stdout=json.dumps({"messages": []}))
        with pytest.raises(VnuUnavailable) as excinfo:
            validate_site(Path("vnu.jar"), site_dir)
        assert "locked.css" in str(excinfo.value)
    finally:
        locked.chmod(0o644)
