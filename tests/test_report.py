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
    DRY_RUN_DISCLAIMER,
    DryRunAssessment,
    Finding,
    _referrers,
    _wayback_snapshot_date,
    assess_dry_run,
    build_report_data,
    build_report_html,
    build_summary,
    format_readiness,
    infer_inventory_sources_used,
    write_report_html,
    write_report_json,
)


def _record(url, **kwargs) -> ManifestRecord:
    return ManifestRecord(url=url, **kwargs)


def test_infer_inventory_sources_used():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/a/", discovered_via="sitemap")
    manifest.get_or_create("https://example.com/b/", discovered_via="xml_backup")
    manifest.get_or_create("https://example.com/c/", discovered_via="crawl:https://example.com/a/")
    sources = infer_inventory_sources_used(manifest)
    assert sources == {
        "sitemap": True,
        "rest_api": False,
        "xml_backup": True,
        "crawl": True,
        "wayback": False,
    }


def test_referrers_extracted_from_discovered_via():
    record = _record("https://example.com/x/")
    record.add_discovered_via("crawl:https://example.com/")
    record.add_discovered_via("wayback:https://example.com/old/")
    record.add_discovered_via("sitemap")
    assert _referrers(record) == ["https://example.com/", "https://example.com/old/"]


def test_report_html_referrers_collapsed_behind_details(tmp_path: Path):
    """Referrers are only useful for a deep dive -- must render collapsed
    behind <details> (no `open` attribute), not as a bare inline list that
    crowds the row and reads as if it were part of the URL column."""
    record = _record("https://example.com/gone/", status=Status.MISSING.value)
    record.add_discovered_via("crawl:https://example.com/a/")
    record.add_discovered_via("crawl:https://example.com/b/")
    manifest = Manifest()
    manifest.upsert(record)

    html = build_report_html(manifest, tmp_path)

    assert "2 referrers" in html
    assert '<ul class="referrer-list">' in html
    assert 'href="https://example.com/a/"' in html
    assert 'href="https://example.com/b/"' in html
    # The referrers <details> must not be forced open.
    referrers_start = html.find("<details><summary>2 referrers")
    assert referrers_start != -1
    assert not html[max(0, referrers_start - 20):referrers_start].rstrip().endswith("open")


def test_report_html_no_referrers_shows_dash(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/gone/", status=Status.MISSING.value))
    html = build_report_html(manifest, tmp_path)
    assert "<td>-</td>" in html


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


def test_report_html_all_details_elements_start_closed(tmp_path: Path):
    """Every <details> in the report -- category sections, referrers,
    discovered_via -- must start collapsed. None should carry the `open`
    attribute."""
    manifest = Manifest()
    auth_record = _record("https://example.com/secret/", status=Status.RETRYING.value)
    auth_record.add_flag(FLAG_AUTH_GATED)
    auth_record.add_discovered_via("crawl:https://example.com/")
    manifest.upsert(auth_record)

    html = build_report_html(manifest, tmp_path)

    assert "<details open" not in html
    assert html.count("<details") >= 2  # category section + at least one referrers/sources block


def test_report_html_action_required_categories_explain_meaning_and_action(tmp_path: Path):
    """Every category with at least one record must show both an
    explanation of what it means and a suggestion for what to do about
    it, not just a bare table of URLs."""
    manifest = Manifest()
    auth_record = _record("https://example.com/secret/", status=Status.RETRYING.value)
    auth_record.add_flag(FLAG_AUTH_GATED)
    manifest.upsert(auth_record)
    manifest.upsert(_record("https://example.com/gone/", status=Status.MISSING.value))

    html = build_report_html(manifest, tmp_path)

    assert "What this means:" in html
    assert "What you might do:" in html
    assert "credentials or permissions" in html  # auth_gated explanation
    assert "recovered live or from the" in html  # missing explanation
    assert 'class="category-help"' in html


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


def test_report_html_full_manifest_discovered_via_collapsed_behind_details(tmp_path: Path):
    """Same treatment as the Action-required referrers column: discovered_via
    can list many provenance entries (inventory-source labels plus
    crawl:/wayback: entries) and must not crowd the Full manifest row as a
    bare inline string."""
    record = _record("https://example.com/x/", status=Status.FETCHED.value)
    record.add_discovered_via("sitemap")
    record.add_discovered_via("crawl:https://example.com/a/")
    manifest = Manifest()
    manifest.upsert(record)

    html = build_report_html(manifest, tmp_path)

    assert "2 sources" in html
    full_manifest_start = html.find('id="full-manifest"')
    assert "<ul class=\"referrer-list\"><li>sitemap</li>" in html[full_manifest_start:]
    details_pos = html.find("<details><summary>2 sources", full_manifest_start)
    assert details_pos != -1
    assert not html[max(0, details_pos - 20):details_pos].rstrip().endswith("open")


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


# ---------------------------------------------------------------------------
# Dry-run readiness assessment
# ---------------------------------------------------------------------------


def _seed(manifest: Manifest, provenance: str, urls) -> None:
    for url in urls:
        manifest.get_or_create(url, discovered_via=provenance)


def test_assess_dry_run_r1_fires_when_nothing_beyond_base_url():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    sources = {"sitemap": False, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    codes = {f.code for f in assessment.findings}
    assert "nothing_discovered" in codes
    assert assessment.verdict == "attention"


def test_assess_dry_run_r1_does_not_fire_with_real_content():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    manifest.get_or_create("https://example.com/about/", discovered_via="sitemap")
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    codes = {f.code for f in assessment.findings}
    assert "nothing_discovered" not in codes


def test_assess_dry_run_handles_a_single_record_without_dividing_by_zero():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    sources = {"sitemap": False, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)  # must not raise
    assert assessment.verdict == "attention"


def test_assess_dry_run_r2_fires_when_no_source_reachable():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    sources = {"sitemap": False, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    codes = {f.code for f in assessment.findings}
    assert "no_inventory_source" in codes


def test_assess_dry_run_r2_does_not_fire_when_xml_backup_alone_seeded_urls():
    """R2 and R1 can diverge: an XML export can seed real URLs even with
    both live sources unreachable."""
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "xml_backup", [f"https://example.com/post-{i}/" for i in range(5)])
    sources = {"sitemap": False, "rest_api": False, "xml_backup": True}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    codes = {f.code for f in assessment.findings}
    assert "no_inventory_source" not in codes
    assert "nothing_discovered" not in codes


def test_assess_dry_run_r3_fires_for_a_single_live_source_with_no_xml_backup():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/page-{i}/" for i in range(10)])
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    finding = next(f for f in assessment.findings if f.code == "single_live_source")
    assert finding.severity == "notice"
    assert "sitemap" in finding.message


def test_assess_dry_run_r3_suppressed_when_xml_backup_is_configured():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/page-{i}/" for i in range(10)])
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    codes = {f.code for f in assessment.findings}
    assert "single_live_source" not in codes


def test_assess_dry_run_r3_suppressed_when_r1_already_fired():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    codes = {f.code for f in assessment.findings}
    assert "nothing_discovered" in codes
    assert "single_live_source" not in codes


def test_assess_dry_run_r4_concern_when_reachable_source_contributes_nothing():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "rest_api", [f"https://example.com/post-{i}/" for i in range(10)])
    sources = {"sitemap": True, "rest_api": True, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    finding = next(f for f in assessment.findings if f.code == "source_contributed_nothing")
    assert finding.severity == "concern"
    assert "sitemap" in finding.message
    assert assessment.verdict == "attention"


def test_assess_dry_run_r4_notice_when_one_source_is_under_ten_percent_of_the_other():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/sitemap-{i}/" for i in range(5)])
    _seed(manifest, "rest_api", [f"https://example.com/rest-{i}/" for i in range(100)])
    sources = {"sitemap": True, "rest_api": True, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    finding = next(f for f in assessment.findings if f.code == "source_underweight")
    assert finding.severity == "notice"
    assert "sitemap" in finding.message and "5" in finding.message and "100" in finding.message
    assert assessment.verdict == "review"


def test_assess_dry_run_r4_no_finding_for_an_ordinary_ratio():
    """30/100 = 30% -- REST API routinely returns more than the sitemap
    (attachments, users) without that being a problem; the rule is
    directional and thresholded, not a bare "they differ" check."""
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/sitemap-{i}/" for i in range(30)])
    _seed(manifest, "rest_api", [f"https://example.com/rest-{i}/" for i in range(100)])
    sources = {"sitemap": True, "rest_api": True, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    codes = {f.code for f in assessment.findings}
    assert "source_underweight" not in codes
    assert "source_contributed_nothing" not in codes


def test_assess_dry_run_r6_fires_for_a_large_share_of_low_value_archive_urls():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/post-{i}/" for i in range(70)])
    _seed(
        manifest, "sitemap",
        [f"https://example.com/2020/01/photo-{i}/attachment/" for i in range(30)],
    )
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    finding = next(f for f in assessment.findings if f.code == "low_value_archives")
    assert finding.severity == "notice"
    assert "attachment page" in finding.message
    assert "30" in finding.message


def test_assess_dry_run_r6_suppressed_below_the_twenty_url_floor():
    """A small site with a genuinely high percentage of tag pages must not
    trip R6 -- the floor exists precisely so a tiny site's one tag archive
    doesn't read as a real finding."""
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/post-{i}/" for i in range(7)])
    manifest.get_or_create("https://example.com/tag/news/", discovered_via="sitemap")
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    codes = {f.code for f in assessment.findings}
    assert "low_value_archives" not in codes


def test_assess_dry_run_r6_suppressed_below_the_ten_percent_threshold():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/post-{i}/" for i in range(500)])
    _seed(manifest, "sitemap", [f"https://example.com/tag/topic-{i}/" for i in range(25)])
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    codes = {f.code for f in assessment.findings}
    assert "low_value_archives" not in codes


def test_assess_dry_run_r6_suppressed_when_r1_already_fired():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    sources = {"sitemap": False, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=False)
    codes = {f.code for f in assessment.findings}
    assert "nothing_discovered" in codes
    assert "low_value_archives" not in codes


def test_assess_dry_run_ignores_crawl_only_records_from_a_resumed_manifest():
    """A dry run re-run over an output dir that already holds a completed
    capture loads that run's full manifest (no collision guard on a dry
    run) -- crawl:-discovered records must not dilute the inventory-only
    percentages this feature depends on. Without the filter, 20 matched
    URLs out of 526 total (~3.8%) would sit under R6's 10% threshold;
    filtered to the 26 real inventory records, it's ~76.9% and fires."""
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/post-{i}/" for i in range(5)])
    _seed(manifest, "sitemap", [f"https://example.com/tag/topic-{i}/" for i in range(20)])
    _seed(
        manifest,
        "crawl:https://example.com/post-0/",
        [f"https://example.com/wp-content/uploads/img-{i}.jpg" for i in range(500)],
    )
    sources = {"sitemap": True, "rest_api": False, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    codes = {f.code for f in assessment.findings}
    assert "low_value_archives" in codes


def test_assess_dry_run_concern_beats_notice_in_verdict():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    # sitemap reachable but contributes nothing -- concern
    _seed(manifest, "rest_api", [f"https://example.com/post-{i}/" for i in range(70)])
    _seed(manifest, "rest_api", [f"https://example.com/tag/topic-{i}/" for i in range(30)])
    sources = {"sitemap": True, "rest_api": True, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    severities = {f.severity for f in assessment.findings}
    assert "concern" in severities and "notice" in severities
    assert assessment.verdict == "attention"


def test_assess_dry_run_ready_verdict_with_no_findings():
    manifest = Manifest()
    manifest.get_or_create("https://example.com/", discovered_via="base_url")
    _seed(manifest, "sitemap", [f"https://example.com/post-{i}/" for i in range(50)])
    _seed(manifest, "rest_api", [f"https://example.com/rest-{i}/" for i in range(60)])
    sources = {"sitemap": True, "rest_api": True, "xml_backup": False}
    assessment = assess_dry_run(manifest, sources, xml_backup_configured=True)
    assert assessment.findings == ()
    assert assessment.verdict == "ready"
    assert assessment.headline == "Nothing to flag in the inventory."


def test_format_readiness_includes_headline_findings_and_disclaimer():
    assessment = DryRunAssessment(
        verdict="review",
        headline="Worth a look before you commit to a crawl.",
        findings=(
            Finding(
                code="single_live_source",
                severity="notice",
                message="Only the sitemap is reachable as a live inventory source.",
                suggestion="An XML export gives a second list to cross-check against.",
            ),
        ),
    )
    text = format_readiness(assessment)
    assert "Worth a look before you commit to a crawl." in text
    assert "Only the sitemap is reachable" in text
    assert "An XML export gives a second list" in text
    assert DRY_RUN_DISCLAIMER in text


def test_format_readiness_ready_has_no_finding_bullets():
    assessment = DryRunAssessment(verdict="ready", headline="Nothing to flag in the inventory.", findings=())
    text = format_readiness(assessment)
    assert "Nothing to flag in the inventory." in text
    assert "  - " not in text
    assert DRY_RUN_DISCLAIMER in text


def test_write_report_json_readiness_null_when_not_passed(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    out_path = tmp_path / "report.json"
    write_report_json(manifest, tmp_path, out_path)
    loaded = json.loads(out_path.read_text())
    assert loaded["readiness"] is None


def test_write_report_json_readiness_populated_when_passed(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    assessment = DryRunAssessment(verdict="ready", headline="Nothing to flag in the inventory.", findings=())
    out_path = tmp_path / "report.json"
    write_report_json(manifest, tmp_path, out_path, readiness=assessment)
    loaded = json.loads(out_path.read_text())
    assert loaded["readiness"]["verdict"] == "ready"
    assert loaded["readiness"]["findings"] == []


def test_build_report_html_no_readiness_section_when_none(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    html = build_report_html(manifest, tmp_path)
    assert 'id="readiness"' not in html


def test_build_report_html_readiness_section_shown_when_present(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    assessment = DryRunAssessment(
        verdict="attention",
        headline="Something looks wrong; worth fixing before crawling.",
        findings=(
            Finding(
                code="nothing_discovered",
                severity="concern",
                message="Only the seeded base_url was found.",
                suggestion="Check base_url.",
            ),
        ),
    )
    html = build_report_html(manifest, tmp_path, readiness=assessment)
    assert 'id="readiness"' in html
    assert "badge-gap" in html
    assert "Only the seeded base_url was found." in html
    # DRY_RUN_DISCLAIMER itself isn't a substring of the escaped HTML (its
    # apostrophe becomes &#x27;) -- check the punctuation-free prefix instead.
    assert "Based on inventory discovery only" in html


def test_build_report_html_readiness_review_uses_badge_review(tmp_path: Path):
    manifest = Manifest()
    manifest.upsert(_record("https://example.com/", status=Status.FETCHED.value))
    assessment = DryRunAssessment(verdict="review", headline="Worth a look.", findings=())
    html = build_report_html(manifest, tmp_path, readiness=assessment)
    assert "badge-review" in html
