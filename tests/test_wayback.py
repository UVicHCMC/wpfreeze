from __future__ import annotations

import contextlib
import http.server
import json
import threading
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from wpfreeze.fetch import FetchConfig, RateLimiter
from wpfreeze.manifest import FLAG_AUTH_GATED, Manifest, ManifestRecord, Source, Status
from wpfreeze.urlnorm import SiteProfile
from wpfreeze.wayback import (
    Snapshot,
    cdx_query_url,
    choose_snapshot,
    parse_cdx_response,
    recover_via_wayback,
    wayback_fetch_url,
)

FAST_CONFIG = FetchConfig(max_attempts=2, timeout=2.0, backoff_base=0.01)

PROFILE = SiteProfile(
    canonical_host="example.com",
    site_hosts=frozenset({"example.com"}),
    use_https=True,
    trailing_slash=True,
)


# ---------------------------------------------------------------------------
# Pure functions: CDX parsing, snapshot selection, URL building
# ---------------------------------------------------------------------------


def test_parse_cdx_response_fixture():
    # Shaped like a real captured CDX API response.
    raw = json.dumps(
        [
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            ["com,example)/foo", "20200101000000", "https://example.com/foo", "text/html", "200", "ABC123", "1000"],
            ["com,example)/foo", "20210601000000", "https://example.com/foo", "text/html", "404", "DEF456", "500"],
            ["com,example)/foo", "20220301000000", "https://example.com/foo", "text/html", "200", "GHI789", "1100"],
        ]
    )
    snapshots = parse_cdx_response(raw)
    assert snapshots == [
        Snapshot("20200101000000", "200", "https://example.com/foo"),
        Snapshot("20210601000000", "404", "https://example.com/foo"),
        Snapshot("20220301000000", "200", "https://example.com/foo"),
    ]


def test_parse_cdx_response_empty():
    assert parse_cdx_response("[]") == []


def test_parse_cdx_response_malformed_header():
    assert parse_cdx_response(json.dumps([["not", "the", "right", "fields"]])) == []


def test_choose_snapshot_picks_nearest_200_ignoring_other_statuses():
    snapshots = [
        Snapshot("20200101000000", "200", "u"),
        Snapshot("20210615000000", "404", "u"),  # closer in time but not 200
        Snapshot("20220301000000", "200", "u"),
    ]
    chosen = choose_snapshot(snapshots, date(2021, 6, 1))
    # Distances: 2020-01-01 is ~517 days away; 2022-03-01 is ~273 days away.
    assert chosen.timestamp == "20220301000000"


def test_choose_snapshot_none_when_no_200():
    snapshots = [Snapshot("20200101000000", "404", "u"), Snapshot("20210101000000", "500", "u")]
    assert choose_snapshot(snapshots, date(2021, 1, 1)) is None


def test_choose_snapshot_none_when_empty():
    assert choose_snapshot([], date(2021, 1, 1)) is None


def test_cdx_query_url_encodes_target():
    url = cdx_query_url("https://example.com/foo?x=1", cdx_api="https://fake/cdx")
    assert url.startswith("https://fake/cdx?")
    assert "url=https%3A%2F%2Fexample.com%2Ffoo%3Fx%3D1" in url
    assert "output=json" in url


def test_wayback_fetch_url_uses_id_suffix():
    url = wayback_fetch_url("20200101000000", "https://example.com/foo", wayback_base="https://fake")
    assert url == "https://fake/web/20200101000000id_/https://example.com/foo"


# ---------------------------------------------------------------------------
# Integration: fake CDX + snapshot endpoint
# ---------------------------------------------------------------------------


class _WaybackHandler(http.server.BaseHTTPRequestHandler):
    cdx_responses: dict[str, str] = {}
    snapshots: dict[str, tuple[int, str, bytes]] = {}

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/cdx/search/cdx":
            qs = parse_qs(parsed.query)
            target_url = qs.get("url", [""])[0]
            body = self.cdx_responses.get(target_url, "[]").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/web/"):
            entry = self.snapshots.get(self.path)
            if entry is None:
                body = b"snapshot not found"
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            status, ctype, body = entry
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def _make_handler(cdx_responses, snapshots):
    return type(
        "WaybackHandler",
        (_WaybackHandler,),
        {"cdx_responses": cdx_responses, "snapshots": snapshots},
    )


@contextlib.contextmanager
def run_fake_wayback(cdx_responses, snapshots):
    handler = _make_handler(cdx_responses, snapshots)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _cdx_json(url: str, entries: list[tuple[str, str]]) -> str:
    """entries: list of (timestamp, statuscode)."""
    rows = [["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]]
    for ts, status in entries:
        rows.append(["key", ts, url, "text/html", status, "digest", "100"])
    return json.dumps(rows)


def test_recover_via_wayback_success_and_discovers_links(tmp_path: Path):
    target_url = "https://example.com/missing-page/"
    snapshot_ts = "20220301000000"
    html = b'<html><body><a href="https://example.com/found-via-wayback/">x</a></body></html>'

    cdx_responses = {target_url: _cdx_json(target_url, [(snapshot_ts, "200")])}
    snapshots = {f"/web/{snapshot_ts}id_/{target_url}": (200, "text/html", html)}

    with run_fake_wayback(cdx_responses, snapshots) as fake_base:
        manifest = Manifest()
        record = manifest.get_or_create(target_url)
        record.status = Status.RETRYING.value
        record.add_flag(FLAG_AUTH_GATED)

        recover_via_wayback(
            manifest,
            PROFILE,
            requests.Session(),
            RateLimiter(0.0),
            FAST_CONFIG,
            tmp_path / "raw",
            date(2022, 3, 1),
            cdx_api=fake_base + "/cdx/search/cdx",
            wayback_base=fake_base,
        )

        record = manifest.get(target_url)
        assert record.status == Status.FETCHED_WAYBACK.value
        assert record.source == Source.WAYBACK.value
        assert record.wayback_url == f"{fake_base}/web/{snapshot_ts}id_/{target_url}"
        assert record.content_hash is not None

        discovered = manifest.get("https://example.com/found-via-wayback/")
        assert discovered is not None
        assert discovered.discovered_via == [f"wayback:{target_url}"]


def test_recover_via_wayback_no_snapshot_marks_missing_for_internal(tmp_path: Path):
    target_url = "https://example.com/never-archived/"
    cdx_responses = {target_url: "[]"}

    with run_fake_wayback(cdx_responses, {}) as fake_base:
        manifest = Manifest()
        record = manifest.get_or_create(target_url)
        record.status = Status.RETRYING.value

        recover_via_wayback(
            manifest,
            PROFILE,
            requests.Session(),
            RateLimiter(0.0),
            FAST_CONFIG,
            tmp_path / "raw",
            date(2022, 3, 1),
            cdx_api=fake_base + "/cdx/search/cdx",
            wayback_base=fake_base,
        )

        record = manifest.get(target_url)
        assert record.status == Status.MISSING.value


def test_recover_via_wayback_no_snapshot_marks_external_unfetchable(tmp_path: Path):
    target_url = "https://cdn.example.net/asset.png"  # not in PROFILE.site_hosts
    cdx_responses = {target_url: "[]"}

    with run_fake_wayback(cdx_responses, {}) as fake_base:
        manifest = Manifest()
        record = manifest.get_or_create(target_url)
        record.status = Status.RETRYING.value

        recover_via_wayback(
            manifest,
            PROFILE,
            requests.Session(),
            RateLimiter(0.0),
            FAST_CONFIG,
            tmp_path / "raw",
            date(2022, 3, 1),
            cdx_api=fake_base + "/cdx/search/cdx",
            wayback_base=fake_base,
        )

        record = manifest.get(target_url)
        assert record.status == Status.EXTERNAL_UNFETCHABLE.value


def test_recover_via_wayback_snapshot_fetch_fails_marks_missing(tmp_path: Path):
    target_url = "https://example.com/broken-snapshot/"
    snapshot_ts = "20220301000000"
    cdx_responses = {target_url: _cdx_json(target_url, [(snapshot_ts, "200")])}
    # No entry registered for the snapshot path -> the fake server 404s it.

    with run_fake_wayback(cdx_responses, {}) as fake_base:
        manifest = Manifest()
        record = manifest.get_or_create(target_url)
        record.status = Status.RETRYING.value

        recover_via_wayback(
            manifest,
            PROFILE,
            requests.Session(),
            RateLimiter(0.0),
            FAST_CONFIG,
            tmp_path / "raw",
            date(2022, 3, 1),
            cdx_api=fake_base + "/cdx/search/cdx",
            wayback_base=fake_base,
        )

        record = manifest.get(target_url)
        assert record.status == Status.MISSING.value
