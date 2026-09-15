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

    assert links == [ExternalLink(url="https://other.example/x", pages=["index.html"], texts=["x"])]


def test_extract_records_the_wording_of_each_link(tmp_path: Path):
    """The owner-tasks worksheet shows what a link *said* beside the
    address it points at: an owner recognises the words long before the
    URL. Distinct wordings for one target are kept in first-seen order,
    capped, and each one truncated."""
    _write_html(
        tmp_path,
        "about.html",
        '<a href="https://other.example/x">  the  Stationers\u2019 Register </a> '
        '<a href="https://other.example/x">the same place, said differently</a> '
        '<a href="https://other.example/x">the same place, said differently</a> '
        f'<a href="https://other.example/long">{"word " * 60}</a>',
    )

    links = {link.url: link for link in extract_external_links(tmp_path, _PROFILE)}

    assert links["https://other.example/x"].texts == [
        "the Stationers\u2019 Register",  # whitespace collapsed
        "the same place, said differently",  # deduplicated
    ]
    long_text = links["https://other.example/long"].texts[0]
    assert len(long_text) == 120 and long_text.endswith("\u2026")


def test_extract_falls_back_to_alt_and_title_for_a_wordless_link(tmp_path: Path):
    _write_html(
        tmp_path,
        "index.html",
        '<a href="https://other.example/img"><img src="/x.png" alt="the cover"></a> '
        '<a href="https://other.example/icon" title="our old blog"><span></span></a> '
        '<a href="https://other.example/none"><span></span></a>',
    )

    links = {link.url: link.texts for link in extract_external_links(tmp_path, _PROFILE)}

    assert links["https://other.example/img"] == ["the cover"]
    assert links["https://other.example/icon"] == ["our old blog"]
    assert links["https://other.example/none"] == []  # nothing to say, so nothing shown


def test_links_have_texts_distinguishes_an_extraction_from_before_the_field(tmp_path: Path):
    from wpfreeze.linkcheck import links_have_texts

    (tmp_path / "external-links.json").write_text(
        json.dumps({"links": [{"url": "https://other.example/x", "pages": ["a.html"]}]}),
        encoding="utf-8",
    )
    old = load_links(tmp_path)

    assert old == [ExternalLink(url="https://other.example/x", pages=["a.html"], texts=[])]
    assert links_have_texts(old) is False
    assert links_have_texts([ExternalLink(url="u", pages=[], texts=["x"])]) is True


def test_classify_error_names_the_failure_from_the_full_error_string(tmp_path: Path):
    """`reason` is truncated at 120 characters, usually mid-exception-name,
    so the owner-facing report cannot re-derive this after the fact --
    which is why it is classified here, where the whole string is still
    in hand. The samples are real, from janellejenstad and landscapes."""
    from wpfreeze.linkcheck import _classify_error

    cases = {
        "dns": "HTTPConnectionPool(host='bnb.bl.uk', port=80): Max retries exceeded with "
               "url: / (Caused by NameResolutionError(\"...[Errno -2] Name or service not known\"))",
        "tls": "HTTPSConnectionPool(host='csdh-schn.org', port=443): Max retries exceeded "
               "with url: / (Caused by SSLError(SSLCertVerificationError(1, 'certificate "
               "verify failed: unable to get local issuer certificate')))",
        "timeout": "HTTPConnectionPool(host='metalib.uvic.ca', port=80): Max retries "
                   "exceeded with url: / (Caused by ConnectTimeoutError(...))",
        "refused": "('Connection aborted.', RemoteDisconnected('Remote end closed "
                   "connection without response'))",
        "redirect_loop": "TooManyRedirects('Exceeded 30 redirects.')",
        "unreachable": "something nobody has seen before",
    }
    for expected, error in cases.items():
        assert _classify_error(error) == expected, error


def test_check_links_records_the_failure_kind(tmp_path: Path, monkeypatch):
    from wpfreeze import linkcheck

    outcomes = {
        "https://gone.example/": FetchOutcome(
            category=WAYBACK_CANDIDATE, http_status=None, attempts=1,
            error="HTTPConnectionPool(host='gone.example', port=443): NameResolutionError",
        ),
        "https://locked.example/": FetchOutcome(
            category=WAYBACK_CANDIDATE, http_status=403, attempts=1, flag=FLAG_AUTH_GATED
        ),
        "https://broken.example/": FetchOutcome(
            category=WAYBACK_CANDIDATE, http_status=500, attempts=3, flag=FLAG_RETRY_EXHAUSTED
        ),
        "https://fine.example/": FetchOutcome(category=SUCCESS, http_status=200, attempts=1),
    }
    monkeypatch.setattr(
        linkcheck, "fetch_with_retries", lambda url, *a, **k: outcomes[url]
    )

    results = linkcheck.check_links(
        [ExternalLink(url=url, pages=["a.html"]) for url in outcomes],
        user_agent="x", rate_limit=0.0, workers=1,
    )

    assert {r.url: r.kind for r in results} == {
        "https://gone.example/": "dns",
        "https://locked.example/": "auth",
        "https://broken.example/": "http",
        "https://fine.example/": None,  # nothing went wrong, nothing to classify
    }


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
            url="https://gone.example/", pages=["a.html", "b.html"], ok=False, status=404,
            reason="HTTP 404", texts=["gone"], kind="http",
        ),
    ]

    path = write_results_json(results, tmp_path, "https://example.com", "2026-08-24T00:00:00+00:00")
    loaded = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "broken-external-links.json"
    assert loaded["base_url"] == "https://example.com"
    assert loaded["checked_at"] == "2026-08-24T00:00:00+00:00"
    assert loaded["results"] == [
        {
            "url": "https://ok.example/", "pages": ["a.html"], "ok": True, "status": 200,
            "reason": None, "texts": [], "kind": None,
        },
        {
            "url": "https://gone.example/",
            "pages": ["a.html", "b.html"],
            "ok": False,
            "status": 404,
            "reason": "HTTP 404",
            "texts": ["gone"],
            "kind": "http",
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


def test_extract_skips_a_malformed_href_without_raising(tmp_path: Path):
    """checklinks scans hrefs from the built markup, and a link the build
    deliberately left alone is still sitting there. An unbalanced bracket is
    not a URL urlsplit will parse (see urlnorm.safe_urlsplit), so it must be
    passed over rather than aborting the whole link check."""
    _write_html(
        tmp_path,
        "index.html",
        '<a href="https://other.example/dead">dead</a> '
        '<a href="http://[broken">malformed</a> '
        '<a href="//]">also malformed</a>',
    )

    links = extract_external_links(tmp_path, _PROFILE)

    assert [link.url for link in links] == ["https://other.example/dead"]
