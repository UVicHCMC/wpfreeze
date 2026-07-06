from __future__ import annotations

import contextlib
import http.server
import threading
import time
from collections import defaultdict

import pytest
import requests

from wpfreeze.fetch import (
    SUCCESS,
    WAYBACK_CANDIDATE,
    FetchConfig,
    RateLimiter,
    fetch_with_retries,
)
from wpfreeze.manifest import FLAG_AUTH_GATED, FLAG_ODD_RESPONSE, FLAG_RETRY_EXHAUSTED


class _ScriptedHandlerBase(http.server.BaseHTTPRequestHandler):
    scripts: dict[str, list[int]] = {}
    sleep_paths: dict[str, float] = {}
    redirect_paths: dict[str, str] = {}
    call_counts: dict[str, int]

    def do_GET(self):  # noqa: N802 (stdlib method name)
        path = self.path
        if path in self.sleep_paths:
            time.sleep(self.sleep_paths[path])
        if path in self.redirect_paths and self.call_counts[path] == 0:
            self.call_counts[path] += 1
            self.send_response(302)
            self.send_header("Location", self.redirect_paths[path])
            self.end_headers()
            return
        statuses = self.scripts.get(path)
        if statuses:
            idx = min(self.call_counts[path], len(statuses) - 1)
            status = statuses[idx]
            self.call_counts[path] += 1
        else:
            status = 200
        body = b"ok"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # silence default stderr logging
        pass


def _make_handler(scripts=None, sleep_paths=None, redirect_paths=None):
    return type(
        "ScriptedHandler",
        (_ScriptedHandlerBase,),
        {
            "scripts": scripts or {},
            "sleep_paths": sleep_paths or {},
            "redirect_paths": redirect_paths or {},
            "call_counts": defaultdict(int),
        },
    )


@contextlib.contextmanager
def run_server(handler_class):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


FAST_CONFIG = FetchConfig(max_attempts=4, timeout=1.0, backoff_base=0.01)


def test_immediate_success():
    handler = _make_handler(scripts={"/ok": [200]})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/ok", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == SUCCESS
    assert outcome.http_status == 200
    assert outcome.attempts == 1
    assert outcome.result is not None
    assert outcome.result.content == b"ok"


def test_retries_transient_5xx_then_succeeds():
    handler = _make_handler(scripts={"/flaky": [500, 503, 200]})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/flaky", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == SUCCESS
    assert outcome.attempts == 3


def test_exhausts_retries_on_persistent_5xx_becomes_wayback_candidate():
    handler = _make_handler(scripts={"/down": [500, 500, 500, 500, 500]})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/down", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == WAYBACK_CANDIDATE
    assert outcome.flag == FLAG_RETRY_EXHAUSTED
    assert outcome.http_status == 500
    assert outcome.attempts == FAST_CONFIG.max_attempts


def test_404_is_wayback_candidate_with_no_retry():
    handler = _make_handler(scripts={"/missing": [404, 200]})  # 200 would prove a retry happened
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/missing", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == WAYBACK_CANDIDATE
    assert outcome.flag is None
    assert outcome.http_status == 404
    assert outcome.attempts == 1


def test_410_is_wayback_candidate():
    handler = _make_handler(scripts={"/gone": [410]})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/gone", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == WAYBACK_CANDIDATE
    assert outcome.flag is None
    assert outcome.http_status == 410


@pytest.mark.parametrize("status", [401, 403])
def test_auth_gated_statuses(status):
    handler = _make_handler(scripts={"/private": [status]})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/private", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == WAYBACK_CANDIDATE
    assert outcome.flag == FLAG_AUTH_GATED
    assert outcome.http_status == status
    assert outcome.attempts == 1  # no retry for auth-gated


def test_odd_response_status_flagged():
    handler = _make_handler(scripts={"/teapot": [418]})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/teapot", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == WAYBACK_CANDIDATE
    assert outcome.flag == FLAG_ODD_RESPONSE
    assert outcome.http_status == 418


def test_connection_timeout_becomes_retry_exhausted():
    config = FetchConfig(max_attempts=2, timeout=0.05, backoff_base=0.01)
    handler = _make_handler(sleep_paths={"/slow": 0.5})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/slow", requests.Session(), RateLimiter(0.0), config)
    assert outcome.category == WAYBACK_CANDIDATE
    assert outcome.flag == FLAG_RETRY_EXHAUSTED
    assert outcome.http_status is None
    assert outcome.error is not None
    assert outcome.attempts == 2


def test_redirect_chain_recorded():
    handler = _make_handler(redirect_paths={"/old": "/new"}, scripts={"/new": [200]})
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/old", requests.Session(), RateLimiter(0.0), FAST_CONFIG)
    assert outcome.category == SUCCESS
    assert outcome.result.final_url == base + "/new"
    assert outcome.result.redirect_chain == [base + "/old"]


def test_rate_limiter_enforces_spacing_per_host():
    limiter = RateLimiter(0.2)
    start = time.monotonic()
    limiter.wait("example.com")
    limiter.wait("example.com")
    limiter.wait("example.com")
    elapsed = time.monotonic() - start
    # Three calls at 0.2s spacing: the 2nd and 3rd each wait ~0.2s.
    assert elapsed >= 0.35


def test_rate_limiter_independent_per_host():
    limiter = RateLimiter(1.0)
    start = time.monotonic()
    limiter.wait("a.example.com")
    limiter.wait("b.example.com")
    elapsed = time.monotonic() - start
    # Different hosts shouldn't serialize against each other.
    assert elapsed < 0.5
