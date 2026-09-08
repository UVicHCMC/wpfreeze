from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import requests

from wpfreeze import validate as validate_mod
from wpfreeze.validate import (
    VnuUnavailable,
    ensure_vnu_jar,
    ensure_vnu_native,
    format_validation_summary,
    resolve_vnu,
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


# ---------------------------------------------------------------------------
# resolve_vnu: hybrid checker resolution (system java + jar, or bundled image)
# ---------------------------------------------------------------------------


def _checker_probe(monkeypatch, *failing: str):
    """Stub the `--version` probe resolve_vnu runs over each candidate:
    every command whose first argument is listed in `failing` reports a
    reason it cannot run, everything else reports None (works). Returns
    the list of probed commands, in order."""
    probed: list[list[str]] = []

    def _probe(cmd, timeout=60.0):
        probed.append(list(cmd))
        return "UnsupportedClassVersionError" if cmd[0] in failing else None

    monkeypatch.setattr(validate_mod, "_checker_failure", _probe)
    return probed


def test_resolve_vnu_pinned_jar_with_java(tmp_path: Path, monkeypatch):
    jar = tmp_path / "vnu.jar"
    jar.write_bytes(b"jar")
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: "/usr/bin/java")
    _checker_probe(monkeypatch)

    assert resolve_vnu(jar) == ["java", "-jar", str(jar)]


def test_resolve_vnu_pinned_jar_without_java_is_actionable(tmp_path: Path, monkeypatch):
    jar = tmp_path / "vnu.jar"
    jar.write_bytes(b"jar")
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: None)

    with pytest.raises(VnuUnavailable) as excinfo:
        resolve_vnu(jar)
    assert "no `java` is on PATH" in str(excinfo.value)


def test_resolve_vnu_pinned_executable_is_run_directly(tmp_path: Path, monkeypatch):
    vnu = tmp_path / "vnu"
    vnu.write_text("#!/bin/sh\n")
    vnu.chmod(0o755)
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: None)  # no java needed
    _checker_probe(monkeypatch)

    assert resolve_vnu(vnu) == [str(vnu)]


def test_resolve_vnu_pinned_path_that_does_not_exist(tmp_path: Path):
    with pytest.raises(VnuUnavailable) as excinfo:
        resolve_vnu(tmp_path / "nope")
    assert "does not exist" in str(excinfo.value)


def test_resolve_vnu_no_pin_prefers_system_java_and_jar(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(validate_mod, "ensure_vnu_jar", lambda **kw: Path("/cache/vnu.jar"))
    called_native = []
    monkeypatch.setattr(validate_mod, "ensure_vnu_native", lambda **kw: called_native.append(1))
    _checker_probe(monkeypatch)

    assert resolve_vnu(None) == ["java", "-jar", "/cache/vnu.jar"]
    assert called_native == []


def test_resolve_vnu_no_pin_no_java_falls_back_to_bundled_image(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(validate_mod, "ensure_vnu_native", lambda **kw: Path("/cache/vnu-runtime-image/bin/vnu"))
    called_jar = []
    monkeypatch.setattr(validate_mod, "ensure_vnu_jar", lambda **kw: called_jar.append(1))
    _checker_probe(monkeypatch)

    assert resolve_vnu(None) == ["/cache/vnu-runtime-image/bin/vnu"]
    assert called_jar == []



def test_resolve_vnu_falls_back_to_the_bundled_image_when_the_system_java_cannot_run_the_jar(
    tmp_path: Path, monkeypatch
):
    """`java` on PATH is not the same claim as `java` able to run the
    current vnu.jar -- a JVM older than the release's target fails with an
    UnsupportedClassVersionError. Before the probe this surfaced as a
    mid-validation failure (exit 2, and inside `freeze` an aborted run)
    with the self-contained image that would have worked never tried."""
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: "/usr/bin/java")
    monkeypatch.setattr(validate_mod, "ensure_vnu_jar", lambda **kw: Path("/cache/vnu.jar"))
    monkeypatch.setattr(validate_mod, "ensure_vnu_native", lambda **kw: Path("/cache/vnu-runtime-image/bin/vnu"))
    probed = _checker_probe(monkeypatch, "java")

    assert resolve_vnu(None) == ["/cache/vnu-runtime-image/bin/vnu"]
    assert probed == [
        ["java", "-jar", "/cache/vnu.jar"],
        ["/cache/vnu-runtime-image/bin/vnu"],
    ]


def test_resolve_vnu_raises_when_no_java_works_and_the_bundled_image_does_not_run_either(
    tmp_path: Path, monkeypatch
):
    """Nothing left to fall back to -- and this is the exception
    `_freeze_validate_step` turns into a skipped step rather than a failed
    freeze, so it has to be raised, not returned as a dead command."""
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(validate_mod, "ensure_vnu_native", lambda **kw: Path("/cache/vnu-runtime-image/bin/vnu"))
    _checker_probe(monkeypatch, "/cache/vnu-runtime-image/bin/vnu")

    with pytest.raises(VnuUnavailable) as excinfo:
        resolve_vnu(None)
    assert "could not be run" in str(excinfo.value)


def test_resolve_vnu_never_silently_replaces_a_pinned_checker(tmp_path: Path, monkeypatch):
    """A pin is a choice. If it cannot run, say so -- downloading 66 MB
    behind the operator's back is not a fix for a config they wrote."""
    jar = tmp_path / "vnu.jar"
    jar.write_bytes(b"jar")
    monkeypatch.setattr(validate_mod.shutil, "which", lambda name: "/usr/bin/java")
    called_native = []
    monkeypatch.setattr(validate_mod, "ensure_vnu_native", lambda **kw: called_native.append(1))
    _checker_probe(monkeypatch, "java")

    with pytest.raises(VnuUnavailable) as excinfo:
        resolve_vnu(jar)
    assert str(jar) in str(excinfo.value)
    assert "UnsupportedClassVersionError" in str(excinfo.value)
    assert called_native == []


def test_checker_failure_reports_a_reason_for_a_command_that_cannot_run(tmp_path: Path):
    """The probe itself, unstubbed: a path that is not executable."""
    assert validate_mod._checker_failure([str(tmp_path / "not-a-real-binary")]) is not None


def test_ensure_vnu_native_refuses_off_linux_rather_than_fetching_linux_binaries(tmp_path: Path, monkeypatch):
    """vnu.linux.zip is one platform's build. Downloading 66 MB of it to
    fail with "Exec format error" is a worse way to learn that than being
    told to install a JRE."""
    monkeypatch.setattr(validate_mod.sys, "platform", "darwin")
    session = _FakeSession(response=_FakeResponse(200, content=_fake_linux_zip()))

    with pytest.raises(VnuUnavailable) as excinfo:
        ensure_vnu_native(session=session, cache_dir=tmp_path / "cache")
    assert "Linux-only" in str(excinfo.value)
    assert session.requests == []


# ---------------------------------------------------------------------------
# ensure_vnu_native: download + unpack the self-contained runtime image
# ---------------------------------------------------------------------------


def _fake_linux_zip(launcher_body: bytes = b"#!/bin/sh\nexec java -m vnu ...\n") -> bytes:
    """A minimal stand-in for vnu.linux.zip: one top-level
    `vnu-runtime-image/` with an executable `bin/vnu` inside it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("vnu-runtime-image/bin/vnu")
        info.external_attr = 0o755 << 16
        zf.writestr(info, launcher_body)
        zf.writestr("vnu-runtime-image/lib/vnu.jar", b"payload")
    return buf.getvalue()


def test_ensure_vnu_native_downloads_and_unpacks_a_runnable_launcher(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    session = _FakeSession(
        response=_FakeResponse(200, content=_fake_linux_zip(), headers={"ETag": '"z1"', "Last-Modified": "Tue"})
    )

    launcher = ensure_vnu_native(session=session, cache_dir=cache_dir)

    assert launcher == cache_dir / "vnu-runtime-image" / "bin" / "vnu"
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111  # executable bit restored
    assert not (cache_dir / "vnu.linux.zip.part").exists()  # temp cleaned up
    meta = json.loads((cache_dir / "vnu.linux.zip.meta.json").read_text())
    assert meta == {"etag": '"z1"', "last_modified": "Tue"}


def test_ensure_vnu_native_uses_cache_on_304_and_sends_etag(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    image = cache_dir / "vnu-runtime-image" / "bin"
    image.mkdir(parents=True)
    (image / "vnu").write_text("cached")
    (cache_dir / "vnu.linux.zip.meta.json").write_text(json.dumps({"etag": '"z1"', "last_modified": "Tue"}))
    session = _FakeSession(response=_FakeResponse(304))

    launcher = ensure_vnu_native(session=session, cache_dir=cache_dir)

    assert launcher.read_text() == "cached"
    assert session.requests[0]["headers"] == {"If-None-Match": '"z1"'}


def test_ensure_vnu_native_falls_back_to_cache_when_offline(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    image = cache_dir / "vnu-runtime-image" / "bin"
    image.mkdir(parents=True)
    (image / "vnu").write_text("stale-but-usable")
    session = _FakeSession(exc=requests.ConnectionError("offline"))

    launcher = ensure_vnu_native(session=session, cache_dir=cache_dir)

    assert launcher.read_text() == "stale-but-usable"


def test_ensure_vnu_native_raises_when_no_cache_and_download_fails(tmp_path: Path):
    session = _FakeSession(exc=requests.ConnectionError("offline"))

    with pytest.raises(VnuUnavailable):
        ensure_vnu_native(session=session, cache_dir=tmp_path / "cache")


def test_ensure_vnu_native_raises_on_a_corrupt_zip_with_no_cache(tmp_path: Path):
    session = _FakeSession(response=_FakeResponse(200, content=b"not a zip at all"))

    with pytest.raises(VnuUnavailable) as excinfo:
        ensure_vnu_native(session=session, cache_dir=tmp_path / "cache")
    assert "unpack" in str(excinfo.value)


def test_ensure_vnu_native_replaces_a_previous_image_atomically(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    old = cache_dir / "vnu-runtime-image"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "vnu").write_text("old")
    (old / "stale-file").write_text("should be gone after refresh")
    session = _FakeSession(response=_FakeResponse(200, content=_fake_linux_zip(), headers={"ETag": '"z2"'}))

    launcher = ensure_vnu_native(session=session, cache_dir=cache_dir)

    assert launcher.read_bytes().startswith(b"#!/bin/sh")
    assert not (cache_dir / "vnu-runtime-image" / "stale-file").exists()
    assert not (cache_dir / "vnu-runtime-image.new").exists()


# ---------------------------------------------------------------------------
# _run_vnu / validate_site accept a resolved command list (native form)
# ---------------------------------------------------------------------------


def test_validate_site_accepts_a_native_command_list(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    calls = _fake_run(monkeypatch, stdout=json.dumps({"messages": []}))

    report = validate_site(["/cache/vnu-runtime-image/bin/vnu"], site_dir)

    assert report.ok
    assert calls[0][0] == "/cache/vnu-runtime-image/bin/vnu"
    assert "java" not in calls[0]
    assert "--skip-non-html" in calls[0]


def test_validate_site_infers_java_for_a_bare_jar_path(monkeypatch, tmp_path: Path):
    site_dir = _site_with_pages(tmp_path, ["a.html"])
    calls = _fake_run(monkeypatch, stdout=json.dumps({"messages": []}))

    validate_site(Path("/cache/vnu.jar"), site_dir)

    assert calls[0][:3] == ["java", "-jar", "/cache/vnu.jar"]
