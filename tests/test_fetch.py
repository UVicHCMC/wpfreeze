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


# ---------------------------------------------------------------------------
# Lockout detection: a run of 401/403s backs the whole host off
# ---------------------------------------------------------------------------


def test_note_response_resets_streak_on_non_lockout_status():
    limiter = RateLimiter(0.0, lockout_threshold=3, lockout_cooldown=10.0)
    limiter.note_response("example.com", 403)
    limiter.note_response("example.com", 403)
    limiter.note_response("example.com", 200)  # breaks the streak
    limiter.note_response("example.com", 403)
    # Only 1 consecutive denial since the reset -- nowhere near the
    # threshold of 3, so no cooldown should have been applied.
    start = time.monotonic()
    limiter.wait("example.com")
    assert time.monotonic() - start < 0.1


def test_note_response_backs_off_whole_host_on_lockout_threshold(caplog):
    limiter = RateLimiter(0.0, lockout_threshold=3, lockout_cooldown=0.3)
    with caplog.at_level("WARNING", logger="wpfreeze.fetch"):
        limiter.note_response("example.com", 403)
        limiter.note_response("example.com", 401)
        limiter.note_response("example.com", 403)  # 3rd consecutive -- crosses threshold

    assert any("lockout" in r.message.lower() for r in caplog.records)
    assert any("example.com" in r.message for r in caplog.records)

    # A worker calling wait() right after -- even a different one than
    # whichever fetch tripped the threshold -- must be held back by the
    # cooldown, not just the fetch that happened to trip it.
    start = time.monotonic()
    limiter.wait("example.com")
    assert time.monotonic() - start >= 0.25


def test_note_response_does_not_affect_other_hosts():
    limiter = RateLimiter(0.0, lockout_threshold=2, lockout_cooldown=5.0)
    limiter.note_response("locked-out.example.com", 403)
    limiter.note_response("locked-out.example.com", 403)  # trips lockout for this host only
    start = time.monotonic()
    limiter.wait("other.example.com")
    assert time.monotonic() - start < 0.1


def test_note_response_escalates_cooldown_on_repeated_lockouts():
    limiter = RateLimiter(0.0, lockout_threshold=2, lockout_cooldown=0.2)
    limiter.note_response("example.com", 403)
    limiter.note_response("example.com", 403)  # 1st lockout episode, cooldown ~0.2s

    start = time.monotonic()
    limiter.wait("example.com")
    first_cooldown = time.monotonic() - start
    assert first_cooldown >= 0.15

    limiter.note_response("example.com", 403)
    limiter.note_response("example.com", 403)  # 2nd episode for this host -- should double

    start = time.monotonic()
    limiter.wait("example.com")
    second_cooldown = time.monotonic() - start
    assert second_cooldown >= first_cooldown * 1.5


def test_fetch_with_retries_reports_lockout_after_consecutive_auth_gated(caplog):
    handler = _make_handler(scripts={"/a": [403], "/b": [403], "/c": [403]})
    # A generous cooldown relative to the real network round-trips this
    # test makes -- the cooldown clock starts ticking inside note_response
    # during the /c fetch, not when this test later measures elapsed time,
    # so it needs enough margin to survive that gap plus test overhead.
    limiter = RateLimiter(0.0, lockout_threshold=3, lockout_cooldown=1.0)
    with run_server(handler) as base:
        with caplog.at_level("WARNING", logger="wpfreeze.fetch"):
            fetch_with_retries(base + "/a", requests.Session(), limiter, FAST_CONFIG)
            fetch_with_retries(base + "/b", requests.Session(), limiter, FAST_CONFIG)
            fetch_with_retries(base + "/c", requests.Session(), limiter, FAST_CONFIG)

    assert any("lockout" in r.message.lower() for r in caplog.records)
    # The next fetch to this host -- a different URL again -- is held
    # back by the cooldown before its request is even sent.
    start = time.monotonic()
    fetch_with_retries(f"{base}/d", requests.Session(), limiter, FAST_CONFIG)
    assert time.monotonic() - start >= 0.5


# ---------------------------------------------------------------------------
# 429 detection: unlike 401/403, a single occurrence backs the host off --
# see wpfreeze/fetch.py's RateLimiter docstring for why no threshold applies.
# ---------------------------------------------------------------------------


def test_note_response_backs_off_host_on_single_429(caplog):
    limiter = RateLimiter(0.0, lockout_cooldown=0.3)
    with caplog.at_level("WARNING", logger="wpfreeze.fetch"):
        limiter.note_response("example.com", 429)  # one 429 is enough, no threshold

    assert any("429" in r.message for r in caplog.records)
    assert any("example.com" in r.message for r in caplog.records)

    start = time.monotonic()
    limiter.wait("example.com")
    assert time.monotonic() - start >= 0.25


def test_note_response_429_honours_retry_after():
    limiter = RateLimiter(0.0, lockout_cooldown=100.0)  # would fail the test if used
    limiter.note_response("example.com", 429, retry_after=0.3)

    start = time.monotonic()
    limiter.wait("example.com")
    elapsed = time.monotonic() - start
    assert 0.2 <= elapsed < 5.0  # honoured the short Retry-After, not the 100s default


def test_note_response_429_escalates_default_cooldown_without_retry_after():
    limiter = RateLimiter(0.0, lockout_cooldown=0.2)
    limiter.note_response("example.com", 429)  # 1st episode, ~0.2s

    start = time.monotonic()
    limiter.wait("example.com")
    first_cooldown = time.monotonic() - start
    assert first_cooldown >= 0.15

    limiter.note_response("example.com", 429)  # 2nd episode -- should double

    start = time.monotonic()
    limiter.wait("example.com")
    second_cooldown = time.monotonic() - start
    assert second_cooldown >= first_cooldown * 1.5


def test_note_response_429_does_not_affect_other_hosts():
    limiter = RateLimiter(0.0, lockout_cooldown=5.0)
    limiter.note_response("limited.example.com", 429)
    start = time.monotonic()
    limiter.wait("other.example.com")
    assert time.monotonic() - start < 0.1


def test_note_response_429_does_not_break_401_403_streak(caplog):
    # A 429 arriving between two 403s must not reset the consecutive-401/
    # 403 count -- that streak and the 429 mechanism are tracked
    # independently. Proven by checking for the *lockout* message
    # specifically, not just "something backed off" -- a 429 alone would
    # also cause a backoff and could mask a broken streak otherwise.
    limiter = RateLimiter(0.0, lockout_threshold=2, lockout_cooldown=0.2)
    with caplog.at_level("WARNING", logger="wpfreeze.fetch"):
        limiter.note_response("example.com", 403)
        limiter.note_response("example.com", 429)  # must not break the 403 streak
        limiter.note_response("example.com", 403)  # 2nd consecutive 403 -- crosses threshold

    assert any("lockout" in r.message.lower() for r in caplog.records)


def test_note_response_401_403_does_not_affect_429_strikes():
    # Symmetric to the above: a 401/403 in between must not reset the 429
    # escalation counter either. Drains each episode's cooldown via wait()
    # before triggering the next, same pattern as
    # test_note_response_escalates_cooldown_on_repeated_lockouts -- so the
    # second measurement isn't muddied by leftover backoff from the first.
    limiter = RateLimiter(0.0, lockout_cooldown=0.2)
    limiter.note_response("example.com", 429)  # 1st 429 episode, ~0.2s

    start = time.monotonic()
    limiter.wait("example.com")
    first_cooldown = time.monotonic() - start
    assert first_cooldown >= 0.15

    limiter.note_response("example.com", 401)  # must not reset 429 strikes
    limiter.note_response("example.com", 429)  # 2nd 429 episode -- should double

    start = time.monotonic()
    limiter.wait("example.com")
    second_cooldown = time.monotonic() - start
    assert second_cooldown >= first_cooldown * 1.5


def test_fetch_with_retries_backs_off_host_on_429_for_sibling_urls(caplog):
    handler = _make_handler(scripts={"/a": [429, 429, 429, 429], "/b": [200]})
    # 429 escalates every one of /a's own 4 attempts (no threshold, unlike
    # 401/403) -- capped low so this test doesn't spend 1+2+4+8s draining
    # an uncapped escalation before it even gets to the /b assertion.
    limiter = RateLimiter(0.0, lockout_cooldown=0.2, lockout_cooldown_max=0.8)
    with run_server(handler) as base:
        with caplog.at_level("WARNING", logger="wpfreeze.fetch"):
            fetch_with_retries(base + "/a", requests.Session(), limiter, FAST_CONFIG)
        assert any("429" in r.message for r in caplog.records)

        # /b never returned anything but 200, but it shares the host --
        # the very first 429 on /a's attempt 1 must already be backing
        # /b off too, without /b itself ever seeing a 429. /a's last
        # (4th) attempt leaves a ~0.8s residual cooldown behind (capped
        # escalation); 0.3s threshold gives comfortable margin against
        # timing jitter while still proving it's not just noise.
        start = time.monotonic()
        outcome = fetch_with_retries(base + "/b", requests.Session(), limiter, FAST_CONFIG)
    assert time.monotonic() - start >= 0.3
    assert outcome.category == SUCCESS


def test_fetch_with_retries_429_still_exhausts_and_flags_retry_exhausted():
    handler = _make_handler(scripts={"/limited": [429, 429, 429, 429]})
    # Tiny cooldown so the test doesn't itself take the full production
    # 60s default while still exercising the real code path end to end.
    limiter = RateLimiter(0.0, lockout_cooldown=0.05, lockout_cooldown_max=0.05)
    with run_server(handler) as base:
        outcome = fetch_with_retries(base + "/limited", requests.Session(), limiter, FAST_CONFIG)
    assert outcome.category == WAYBACK_CANDIDATE
    assert outcome.flag == FLAG_RETRY_EXHAUSTED
    assert outcome.http_status == 429
    assert outcome.attempts == FAST_CONFIG.max_attempts


@pytest.mark.parametrize(
    ("header", "expected_min", "expected_max"),
    [
        ("2", 1.9, 2.1),
        ("0", 0.0, 0.1),
        (None, None, None),
        ("not-a-number-or-date", None, None),
    ],
)
def test_parse_retry_after_seconds_and_invalid(header, expected_min, expected_max):
    from wpfreeze.fetch import _parse_retry_after

    result = _parse_retry_after(header)
    if expected_min is None:
        assert result is None
    else:
        assert expected_min <= result <= expected_max


def test_parse_retry_after_http_date_in_future():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    from wpfreeze.fetch import _parse_retry_after

    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    result = _parse_retry_after(format_datetime(future, usegmt=True))
    assert result is not None
    assert 25.0 <= result <= 30.5


def test_parse_retry_after_http_date_in_past_clamps_to_zero():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    from wpfreeze.fetch import _parse_retry_after

    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    result = _parse_retry_after(format_datetime(past, usegmt=True))
    assert result == 0.0


def test_repeated_backoffs_do_not_accumulate_without_bound():
    """Regression. _back_off_host used to add each cooldown to
    an already-future next_allowed, so a long run of denials from one host
    stacked cooldowns without limit. checklinks against a WordPress.com
    site hit it: ~500 bot-blocked /log-in URLs returning 403 built a
    48-hour backlog on one host, which read as an infinite hang.

    Escalation by strike count is deliberate and stays; what must not
    happen is unbounded accumulation past the cap.
    """
    limiter = RateLimiter(0.0, lockout_threshold=5, lockout_cooldown=60.0, lockout_cooldown_max=1800.0)
    for _ in range(500):
        limiter.note_response("wordpress.com", 403)

    backlog = limiter._next_allowed["wordpress.com"] - time.monotonic()
    assert backlog <= 1800.0, f"cooldowns accumulated past the cap: {backlog:.0f}s"


def test_concurrent_reports_of_one_429_do_not_stack():
    """Several workers each report the same 429 for the same host; they
    should agree on one cooldown, not add one apiece."""
    limiter = RateLimiter(0.0, lockout_cooldown_max=1800.0)
    for _ in range(4):
        limiter.note_response("example.com", 429, retry_after=30.0)

    backlog = limiter._next_allowed["example.com"] - time.monotonic()
    assert 25.0 <= backlog <= 35.0, f"expected ~30s, got {backlog:.1f}s"


def test_retry_after_is_capped():
    """An uncapped Retry-After lets any remote host park the run for as
    long as it likes; 86400 is a legal value."""
    limiter = RateLimiter(0.0, lockout_cooldown_max=1800.0)
    limiter.note_response("example.com", 429, retry_after=86400.0)

    backlog = limiter._next_allowed["example.com"] - time.monotonic()
    assert backlog <= 1800.0, f"Retry-After was not capped: {backlog:.0f}s"


def test_lockout_detection_can_be_disabled_for_link_checking():
    """checklinks deliberately fetches third-party hosts, where 403 is an
    ordinary answer (bot protection, login walls) and a result to report
    rather than a signal to back off. 429 handling must survive."""
    limiter = RateLimiter(0.0, lockout_threshold=None, lockout_cooldown=60.0)
    for _ in range(50):
        limiter.note_response("wordpress.com", 403)
    start = time.monotonic()
    limiter.wait("wordpress.com")
    assert time.monotonic() - start < 0.1, "403s must not back off a host for checklinks"

    limiter.note_response("wordpress.com", 429, retry_after=0.3)
    start = time.monotonic()
    limiter.wait("wordpress.com")
    assert time.monotonic() - start >= 0.25, "429 backoff must still apply"


def test_check_links_disables_lockout_detection():
    """The wiring, not just the capability."""
    import wpfreeze.linkcheck as linkcheck

    captured = {}
    real = linkcheck.RateLimiter

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(linkcheck, "RateLimiter", _spy):
        linkcheck.check_links([], user_agent="ua", rate_limit=0.0, workers=1)
    assert captured.get("lockout_threshold", "missing") is None
