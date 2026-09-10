from __future__ import annotations

import json
from pathlib import Path

import pytest

from wpfreeze.fetch import SUCCESS, WAYBACK_CANDIDATE, FetchOutcome
from wpfreeze.linkcheck import (
    ExternalLink,
    LinkCheckResult,
    check_links,
    extract_external_links,
    load_links,
    render_report_html,
    render_report_markdown,
    write_links,
    write_report,
    write_results_json,
)
from wpfreeze.manifest import FLAG_AUTH_GATED, FLAG_RETRY_EXHAUSTED
from wpfreeze.urlnorm import scope_profile_from_config

_PROFILE = scope_profile_from_config("https://example.com")


def _write_html(path: Path, name: str, body: str) -> None:
    (path / name).write_text(f"<html><body>{body}</body></html>", encoding="utf-8")


def test_extract_finds_external_links_grouped_by_page(tmp_path: Path):
    _write_html(
        tmp_path,
        "about.html",
        '<a href="https://other.example/dead">dead</a> '
        '<a href="https://other.example/dead">dead again</a> '
        '<a href="/local">local</a>',
    )
    _write_html(tmp_path, "team.html", '<a href="https://other.example/dead">dead</a>')

    links = extract_external_links(tmp_path, _PROFILE)

    assert len(links) == 1
    assert links[0].url == "https://other.example/dead"
    assert links[0].pages == ["about.html", "team.html"]


def test_extract_ignores_internal_links_and_non_http_schemes(tmp_path: Path):
    _write_html(
        tmp_path,
        "index.html",
        '<a href="/about.html">about</a> '
        '<a href="https://example.com/team.html">team via absolute self-link</a> '
        '<a href="mailto:someone@example.com">mail</a> '
        '<a href="#section">anchor</a>',
    )

    assert extract_external_links(tmp_path, _PROFILE) == []


def test_extract_resolves_protocol_relative_links(tmp_path: Path):
    _write_html(tmp_path, "index.html", '<a href="//other.example/x">x</a>')

    links = extract_external_links(tmp_path, _PROFILE)

    assert links == [ExternalLink(url="https://other.example/x", pages=["index.html"])]


def test_write_and_load_links_round_trip(tmp_path: Path):
    links = [ExternalLink(url="https://other.example/x", pages=["a.html", "b.html"])]

    path = write_links(links, tmp_path, "https://example.com")
    loaded = json.loads(path.read_text(encoding="utf-8"))

    assert loaded["base_url"] == "https://example.com"
    assert load_links(tmp_path) == links


def test_load_links_returns_none_when_absent(tmp_path: Path):
    assert load_links(tmp_path) is None


def test_check_links_classifies_success_and_failure(tmp_path: Path, monkeypatch):
    links = [
        ExternalLink(url="https://ok.example/", pages=["a.html"]),
        ExternalLink(url="https://gone.example/", pages=["a.html", "b.html"]),
        ExternalLink(url="https://timeout.example/", pages=["c.html"]),
    ]

    def _fake_fetch(url, session, rate_limiter, config):
        if url == "https://ok.example/":
            return FetchOutcome(category=SUCCESS, http_status=200, attempts=1)
        if url == "https://gone.example/":
            return FetchOutcome(category=WAYBACK_CANDIDATE, http_status=404, attempts=1)
        return FetchOutcome(
            category=WAYBACK_CANDIDATE,
            http_status=None,
            attempts=2,
            flag=FLAG_RETRY_EXHAUSTED,
            error="Connection timed out",
        )

    monkeypatch.setattr("wpfreeze.linkcheck.fetch_with_retries", _fake_fetch)

    results = check_links(links, user_agent="test-agent", rate_limit=0.0, workers=4)

    by_url = {r.url: r for r in results}
    assert by_url["https://ok.example/"].ok
    assert not by_url["https://gone.example/"].ok
    assert by_url["https://gone.example/"].reason == "HTTP 404"
    assert not by_url["https://timeout.example/"].ok
    assert "unreachable" in by_url["https://timeout.example/"].reason
    assert by_url["https://gone.example/"].pages == ["a.html", "b.html"]


def test_check_links_flags_auth_gated_distinctly(monkeypatch):
    links = [ExternalLink(url="https://locked.example/", pages=["a.html"])]

    def _fake_fetch(url, session, rate_limiter, config):
        return FetchOutcome(category=WAYBACK_CANDIDATE, http_status=403, attempts=1, flag=FLAG_AUTH_GATED)

    monkeypatch.setattr("wpfreeze.linkcheck.fetch_with_retries", _fake_fetch)

    [result] = check_links(links, user_agent="test-agent", rate_limit=0.0, workers=1)

    assert not result.ok
    assert "auth-gated" in result.reason


def test_report_groups_broken_links_by_page():
    results = [
        LinkCheckResult(url="https://ok.example/", pages=["a.html"], ok=True, status=200, reason=None),
        LinkCheckResult(
            url="https://gone.example/", pages=["a.html", "b.html"], ok=False, status=404, reason="HTTP 404"
        ),
    ]

    md = render_report_markdown(results, "2026-08-24T00:00:00+00:00")
    html = render_report_html(results, "2026-08-24T00:00:00+00:00")

    assert "## a.html" in md and "## b.html" in md
    assert "https://gone.example/" in md
    assert "https://ok.example/" not in md  # only broken links are listed
    assert "<h2>a.html</h2>" in html and "<h2>b.html</h2>" in html


def test_report_reports_nothing_broken():
    results = [LinkCheckResult(url="https://ok.example/", pages=["a.html"], ok=True, status=200, reason=None)]

    md = render_report_markdown(results, "2026-08-24T00:00:00+00:00")

    assert "No broken external links found." in md


def test_write_report_writes_both_files(tmp_path: Path):
    results = [
        LinkCheckResult(url="https://gone.example/", pages=["a.html"], ok=False, status=404, reason="HTTP 404")
    ]

    md_path, html_path = write_report(results, tmp_path, "2026-08-24T00:00:00+00:00")

    assert md_path.exists() and html_path.exists()
    assert "a.html" in md_path.read_text(encoding="utf-8")


def test_write_results_json_round_trips_every_field(tmp_path: Path):
    results = [
        LinkCheckResult(url="https://ok.example/", pages=["a.html"], ok=True, status=200, reason=None),
        LinkCheckResult(
            url="https://gone.example/", pages=["a.html", "b.html"], ok=False, status=404, reason="HTTP 404"
        ),
    ]

    path = write_results_json(results, tmp_path, "https://example.com", "2026-08-24T00:00:00+00:00")
    loaded = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "broken-external-links.json"
    assert loaded["base_url"] == "https://example.com"
    assert loaded["checked_at"] == "2026-08-24T00:00:00+00:00"
    assert loaded["results"] == [
        {"url": "https://ok.example/", "pages": ["a.html"], "ok": True, "status": 200, "reason": None},
        {
            "url": "https://gone.example/",
            "pages": ["a.html", "b.html"],
            "ok": False,
            "status": 404,
            "reason": "HTTP 404",
        },
    ]


def test_write_results_json_keeps_null_status_and_reason(tmp_path: Path):
    results = [
        LinkCheckResult(
            url="https://timeout.example/", pages=["c.html"], ok=False, status=None, reason="unreachable (timeout)"
        )
    ]

    path = write_results_json(results, tmp_path, "https://example.com", "2026-08-24T00:00:00+00:00")
    [entry] = json.loads(path.read_text(encoding="utf-8"))["results"]

    assert entry["status"] is None
    assert entry["reason"] == "unreachable (timeout)"
