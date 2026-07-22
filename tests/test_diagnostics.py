from __future__ import annotations

from pathlib import Path

from wpfreeze.diagnostics import build_diagnostics
from wpfreeze.manifest import Manifest, Status


def _fetched(manifest: Manifest, url: str, local_path: str, content: bytes, **extra) -> None:
    record = manifest.get_or_create(url)
    record.status = Status.FETCHED.value
    record.http_status = 200
    record.local_path = local_path
    record.content_type = extra.pop("content_type", "text/html")
    import hashlib

    record.content_hash = extra.pop("content_hash", hashlib.sha256(content).hexdigest())
    for key, value in extra.items():
        setattr(record, key, value)


def test_counts_and_pending_reflect_manifest(tmp_path: Path):
    manifest = Manifest()
    _fetched(manifest, "https://example.com/", "raw/index.html", b"home")
    manifest.get_or_create("https://example.com/missing/").status = Status.MISSING.value

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    assert diagnostics["counts"] == {"fetched": 1, "missing": 1}
    assert diagnostics["pending"] == 0
    assert diagnostics["has_gaps"] is True


def test_bare_directory_gaps_are_called_out_separately(tmp_path: Path):
    manifest = Manifest()
    real_gap = manifest.get_or_create("https://example.com/wp-content/uploads/2019/gone.pdf")
    real_gap.status = Status.MISSING.value
    real_gap.http_status = 404
    js_config_leak = manifest.get_or_create("https://example.com/wp-content/mu-plugins/some-plugin/src/build/")
    js_config_leak.status = Status.MISSING.value
    js_config_leak.http_status = 403

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    assert diagnostics["gaps"]["total"] == 2
    assert diagnostics["gaps"]["bare_directory_count"] == 1
    assert diagnostics["gaps"]["bare_directory_sample"] == [
        "https://example.com/wp-content/mu-plugins/some-plugin/src/build/"
    ]


def test_duplicate_local_paths_flagged(tmp_path: Path):
    """Exactly the local_path_for query-string collision this check exists
    to catch immediately instead of requiring a by-hand trace."""
    manifest = Manifest()
    (tmp_path / "raw").mkdir()
    _fetched(manifest, "https://example.com/", "raw/index.html", b"home")
    _fetched(manifest, "https://example.com/?attachment_id=42", "raw/index.html", b"attachment")

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    assert len(diagnostics["duplicate_local_paths"]) == 1
    collision = diagnostics["duplicate_local_paths"][0]
    assert collision["local_path"] == "raw/index.html"
    assert set(collision["urls"]) == {"https://example.com/", "https://example.com/?attachment_id=42"}


def test_disk_hash_mismatch_detected_when_file_overwritten_after_the_fact(tmp_path: Path):
    """The direct symptom of the homepage-content-corruption bug: the
    manifest's own content_hash (recorded at fetch time) no longer matches
    what's actually on disk, because something else wrote over the file
    afterward."""
    manifest = Manifest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "index.html").write_bytes(b"<html><title>real homepage</title></html>")
    _fetched(manifest, "https://example.com/", "raw/index.html", b"<html><title>real homepage</title></html>")
    # Something else overwrites the file after this record's hash was recorded.
    (tmp_path / "raw" / "index.html").write_bytes(b"<html><title>wrong page</title></html>")

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    assert len(diagnostics["disk_hash_mismatches"]) == 1
    assert diagnostics["disk_hash_mismatches"][0]["url"] == "https://example.com/"
    assert diagnostics["disk_hash_mismatches"][0]["issue"] == "hash_mismatch"


def test_disk_hash_matches_when_nothing_overwrote_the_file(tmp_path: Path):
    manifest = Manifest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "index.html").write_bytes(b"<html>fine</html>")
    _fetched(manifest, "https://example.com/", "raw/index.html", b"<html>fine</html>")

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    assert diagnostics["disk_hash_mismatches"] == []


def test_homepage_check_finds_record_via_alias_and_extracts_title(tmp_path: Path):
    """The configured base_url can differ from the canonical URL the site
    actually settled on (e.g. non-www vs www) -- the homepage check must
    still find it via the record's aliases, not just an exact URL match."""
    manifest = Manifest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "index.html").write_bytes(b"<html><head><title>Home - Example</title></head></html>")
    _fetched(manifest, "https://www.example.com/", "raw/index.html", b"<html><head><title>Home - Example</title></head></html>")
    manifest.get("https://www.example.com/").add_alias("https://example.com/")

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    homepage = diagnostics["homepage"]
    assert homepage["found"] is True
    assert homepage["url"] == "https://www.example.com/"
    assert homepage["title"] == "Home - Example"


def test_homepage_check_reports_not_found_when_no_record_matches(tmp_path: Path):
    manifest = Manifest()
    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)
    assert diagnostics["homepage"] == {"found": False}


def test_log_issues_counts_warning_error_critical_lines(tmp_path: Path):
    log_path = tmp_path / "run.log"
    log_path.write_text(
        "2026-07-10 12:00:00,000 DEBUG wpfreeze.fetch: attempt 1 for https://example.com/ -> 200\n"
        "2026-07-10 12:00:01,000 WARNING wpfreeze.inventory: stripped 3 XML-invalid character(s)\n"
        "2026-07-10 12:00:02,000 WARNING wpfreeze.inventory: stripped 3 XML-invalid character(s)\n"
        "2026-07-10 12:00:03,000 CRITICAL wpfreeze.cli: acquire crashed with an unhandled exception\n",
        encoding="utf-8",
    )
    manifest = Manifest()

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", log_path)

    log_issues = diagnostics["log_issues"]
    assert log_issues["available"] is True
    assert log_issues["total"] == 2  # two distinct messages, one repeated
    counts_by_line = {issue["count"] for issue in log_issues["issues"]}
    assert 2 in counts_by_line  # the repeated WARNING
    assert 1 in counts_by_line  # the CRITICAL


def test_log_issues_reports_unavailable_when_no_log_path(tmp_path: Path):
    manifest = Manifest()
    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)
    assert diagnostics["log_issues"] == {"available": False, "issues": [], "total": 0, "truncated": False}


def test_directory_like_url_serving_non_html_is_flagged(tmp_path: Path):
    """The silent-corruption case: WordPress.com /_static/?? bundles all
    normalize onto a bare directory URL, Wayback serves *a* real archived
    bundle for it, and the record looks like a clean success."""
    manifest = Manifest()
    _fetched(manifest, "https://example.com/", "raw/index.html", b"home")
    collided = manifest.get_or_create("https://s1.wp.com/_static/")
    collided.status = Status.FETCHED_WAYBACK.value
    collided.http_status = 200
    collided.local_path = "raw/_external/https_s1.wp.com/_static/index.html"
    collided.content_type = "text/css;charset=utf-8"

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    flagged = diagnostics["content_type_shape_mismatches"]
    assert [f["url"] for f in flagged] == ["https://s1.wp.com/_static/"]
    assert flagged[0]["status"] == "fetched_wayback"
    assert flagged[0]["content_type"] == "text/css"


def test_directory_like_html_and_file_like_assets_are_not_flagged(tmp_path: Path):
    """Directory URLs serving HTML are just pages, and assets with a real
    filename carry their own identity -- neither can be this collision."""
    manifest = Manifest()
    _fetched(manifest, "https://example.com/about/", "raw/about/index.html", b"<html>")
    _fetched(
        manifest,
        "https://example.com/wp-content/style.css",
        "raw/wp-content/style.css",
        b"body{}",
        content_type="text/css",
    )
    no_content_type = manifest.get_or_create("https://example.com/gone/")
    no_content_type.status = Status.MISSING.value

    diagnostics = build_diagnostics(manifest, tmp_path, "https://example.com/", None)

    assert diagnostics["content_type_shape_mismatches"] == []
