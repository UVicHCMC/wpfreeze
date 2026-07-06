from __future__ import annotations

import json
from pathlib import Path

import pytest

from wpfreeze.manifest import (
    FLAG_AUTH_GATED,
    FLAG_ORPHAN,
    Manifest,
    ManifestRecord,
    Source,
    Status,
)
from wpfreeze.report import (
    _referrers,
    _wayback_snapshot_date,
    build_report_data,
    build_report_html,
    build_summary,
    infer_inventory_sources_used,
    write_report_html,
    write_report_json,
)


def _record(url, **kwargs) -> ManifestRecord:
    return ManifestRecord(url=url, **kwargs)


def test_infer_inventory_sources_used():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/a/", discovered_via="sitemap")
    manifest.get_or_create("https://example.com/b/", discovered_via="database")
    manifest.get_or_create("https://example.com/c/", discovered_via="crawl:https://example.com/a/")
    sources = infer_inventory_sources_used(manifest)
    assert sources == {
        "sitemap": True,
        "rest_api": False,
        "database": True,
        "crawl": True,
        "wayback": False,
    }


def test_referrers_extracted_from_discovered_via():
    record = _record("https://example.com/x/")
    record.add_discovered_via("crawl:https://example.com/")
    record.add_discovered_via("wayback:https://example.com/old/")
    record.add_discovered_via("sitemap")
    assert _referrers(record) == ["https://example.com/", "https://example.com/old/"]


def test_wayback_snapshot_date_parses_timestamp():
    url = "https://web.archive.org/web/20220301123456id_/https://example.com/"
    assert _wayback_snapshot_date(url) == "2022-03-01"


def test_wayback_snapshot_date_handles_missing_or_malformed():
    assert _wayback_snapshot_date(None) == ""
    assert _wayback_snapshot_date("https://example.com/not-a-wayback-url") == ""


def test_build_summary_counts_and_bytes(tmp_path: Path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "index.html").write_bytes(b"x" * 100)
    (tmp_path / "raw" / "about.html").write_bytes(b"y" * 50)

    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value, source=Source.LIVE.value, local_path="raw/index.html"))
    manifest.upsert(_record("https://example.com/about/", status=Status.FETCHED.value, source=Source.LIVE.value, local_path="raw/about.html"))
    manifest.upsert(_record("https://example.com/gone/", status=Status.MISSING.value))

    summary = build_summary(manifest, tmp_path, run_started="2026-01-01T00:00:00+00:00", run_finished="2026-01-01T01:00:00+00:00")
    assert summary["total_records"] == 3
    assert summary["status_counts"] == {Status.FETCHED.value: 2, Status.MISSING.value: 1}
    assert summary["total_bytes"] == 150
    assert summary["has_gaps"] is True
    assert summary["run_started"] == "2026-01-01T00:00:00+00:00"


def test_build_summary_no_gaps_when_all_fetched(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    summary = build_summary(manifest, tmp_path)
    assert summary["has_gaps"] is False


def test_build_report_data_is_json_serializable(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    data = build_report_data(manifest, tmp_path)
    serialized = json.dumps(data)  # must not raise
    assert "records" in data
    assert "summary" in data


def test_write_report_json_round_trips(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    out_path = tmp_path / "report.json"
    write_report_json(manifest, tmp_path, out_path)
    loaded = json.loads(out_path.read_text())
    assert loaded["summary"]["total_records"] == 1
    assert loaded["records"][0]["url"] == "https://example.com/"


# ---------------------------------------------------------------------------
# report.html
# ---------------------------------------------------------------------------


def test_report_html_escapes_url_content(tmp_path: Path):
    manifest = Manifest()
    malicious_url = 'https://example.com/"><script>alert(1)</script>'
    manifest.upsert(_record(malicious_url, status=Status.FETCHED.value))
    html = build_report_html(manifest, tmp_path)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_html_action_required_shown_when_present(tmp_path: Path):
    manifest = Manifest()
    record = _record("https://example.com/secret/", status=Status.RETRYING.value)
    record.add_flag(FLAG_AUTH_GATED)
    manifest.upsert(record)
    html = build_report_html(manifest, tmp_path)
    assert "Auth-gated" in html
    assert "id=\"action-auth_gated\"" in html


def test_report_html_action_required_hidden_when_empty(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    html = build_report_html(manifest, tmp_path)
    assert "Nothing needs action." in html


def test_report_html_has_filter_controls_and_data_attributes(tmp_path: Path):
    manifest = Manifest()
    record = _record("https://example.com/orphan/", status=Status.FETCHED.value)
    record.add_flag(FLAG_ORPHAN)
    manifest.upsert(record)
    html = build_report_html(manifest, tmp_path)
    assert 'id="filter-status"' in html
    assert 'id="filter-flag"' in html
    assert 'data-status="fetched"' in html
    assert 'data-flags="orphan"' in html


def test_report_html_no_external_resources(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    html = build_report_html(manifest, tmp_path)
    assert "http://" not in html.replace("https://example.com/", "")  # no external CDN links
    assert "<link " not in html
    assert "src=\"http" not in html


def test_write_report_html_writes_file(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    out_path = tmp_path / "report.html"
    write_report_html(manifest, tmp_path, out_path)
    assert out_path.exists()
    assert "<html" in out_path.read_text()
