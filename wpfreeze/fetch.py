"""Fetcher: rate-limited, retrying HTTP client with failure classification.

See the acquisition design notes, "Stage 3 -- Failure classification and retry":
transient errors (timeout/connection error/429/5xx) retry with exponential
backoff up to a configurable attempt limit; everything terminal --
including exhausted retries -- becomes a Wayback candidate, distinguished
only by a flag explaining why (see wpfreeze.manifest.FLAG_*).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from wpfreeze.manifest import FLAG_AUTH_GATED, FLAG_ODD_RESPONSE, FLAG_RETRY_EXHAUSTED

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "wpfreeze/0.1 (+https://example.invalid/wpfreeze-contact)"

SUCCESS = "success"
WAYBACK_CANDIDATE = "wayback_candidate"

_TRANSIENT_STATUSES = {429}
_LOCKOUT_STATUSES = {401, 403}

# A run of this many consecutive 401/403 responses from the same host is
# treated as a likely site-side lockout (e.g. a security plugin banning
# this IP after too many rapid requests) rather than a handful of
# individually private pages -- ordinary auth-gated content doesn't
# usually cluster into a long unbroken run against a crawler that's
# hitting many different URLs. See RateLimiter.note_response.
#
# 429 uses the same cooldown/cooldown_max escalation but NOT the same
# threshold: a 429 is the server's own unambiguous "you are being rate
# limited" signal (unlike 401/403, which could just be one legitimately
# private page), so it backs the whole host off starting from the very
# first occurrence rather than waiting for a run of them. See
# RateLimiter.note_response -- found via
# a real crawl of a live WordPress site, where `rate_limit: 0.0` plus
# concurrency=2 tripped the host's rate limiting on ~31% of its pages,
# and the pre-existing lockout mechanism (401/403-only) never noticed
# because nothing tracked repeated 429s at all.
DEFAULT_LOCKOUT_THRESHOLD = 5
DEFAULT_LOCKOUT_COOLDOWN = 60.0
DEFAULT_LOCKOUT_COOLDOWN_MAX = 1800.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (RFC 9110 sec 10.2.3): either an integer
    number of seconds, or an HTTP-date. Returns None if absent or
    unparseable -- callers fall back to their own default cooldown rather
    than trusting a header that isn't actually usable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


@dataclass(frozen=True)
class FetchConfig:
    user_agent: str = DEFAULT_USER_AGENT
    rate_limit: float = 1.0
    max_attempts: int = 4
    timeout: float = 15.0
    backoff_base: float = 1.0


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    content: bytes
    headers: dict[str, str]
    content_type: str | None
    final_url: str
    redirect_chain: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FetchOutcome:
    category: str  # SUCCESS or WAYBACK_CANDIDATE
    http_status: int | None
    attempts: int
    flag: str | None = None
    result: FetchResult | None = None
    error: str | None = None


class RateLimiter:
    """Global per-host throttle shared across worker threads.

    `rate_limit` is the minimum spacing between requests to the same host,
    enforced across *all* callers regardless of how many workers are
    concurrently fetching -- concurrency parallelizes across hosts, not
    within one. Note that `rate_limit: 0.0` in a site's config disables
    only *this* spacing; it does not disable the 429/lockout backoff
    below, which is deliberately not configurable off (see note_response).

    Also tracks two kinds of trouble per host via note_response, both
    pushing that host's next-allowed time forward by a cooldown (doubling
    on each further episode of the *same* kind for the same host, capped
    at `lockout_cooldown_max`) -- backing the *host* off, not just the one
    fetch that tripped it, since concurrent workers on the same host would
    otherwise keep hammering it for the duration of the cooldown
    regardless. This reuses `wait`'s existing per-host `_next_allowed`
    bookkeeping rather than a separate blocking mechanism, so every future
    `wait(host)` call -- from any worker, including retries of the same
    URL still inside its own fetch_with_retries loop -- naturally respects
    it:

    - A run of `lockout_threshold` consecutive 401/403 responses (a likely
      site-side lockout, e.g. a security plugin banning this IP, rather
      than a handful of individually private pages -- ordinary auth-gated
      content doesn't usually cluster into a long unbroken run against a
      crawler hitting many different URLs).
    - Any single 429 (HTTP's own "you are being rate limited" status --
      unlike 401/403 this needs no run-length threshold, because there is
      no ambiguous "maybe it's just one private page" case to guard
      against). Honours a `Retry-After` header when the server sends one,
      clamped to `lockout_cooldown_max`; falls back to the same escalating
      cooldown otherwise.

    `lockout_threshold=None` switches the 401/403 rule off entirely while
    leaving 429 handling intact. That is right for `checklinks` and wrong
    for `acquire`, and the difference is whose site is being fetched. When
    acquiring, a run of denials from the site you are copying really does
    mean you have been banned and should stop. When checking outbound
    links, 403 is an ordinary answer from a third-party host -- bot
    protection, a login wall, a paywall -- and is a result to report, not a
    signal to slow down. Treating it as a lockout made checklinks
    unusable against any bot-protected host (2026-08-28).
    """

    def __init__(
        self,
        rate_limit: float,
        lockout_threshold: int | None = DEFAULT_LOCKOUT_THRESHOLD,
        lockout_cooldown: float = DEFAULT_LOCKOUT_COOLDOWN,
        lockout_cooldown_max: float = DEFAULT_LOCKOUT_COOLDOWN_MAX,
    ) -> None:
        self._rate_limit = rate_limit
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}
        self._lockout_threshold = lockout_threshold
        self._lockout_cooldown = lockout_cooldown
        self._lockout_cooldown_max = lockout_cooldown_max
        self._consecutive_denied: dict[str, int] = {}
        self._lockout_strikes: dict[str, int] = {}
        self._rate_limit_strikes: dict[str, int] = {}

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            next_allowed = self._next_allowed.get(host, now)
            sleep_for = max(0.0, next_allowed - now)
            self._next_allowed[host] = max(now, next_allowed) + self._rate_limit
        if sleep_for > 0:
            time.sleep(sleep_for)

    def _back_off_host(self, host: str, cooldown: float) -> None:
        """Push `host` out to at least `cooldown` seconds from now. Caller
        must hold self._lock.

        Takes a maximum rather than adding, so repeated reports never stack.
        Adding was a real defect: note_response fires once per *attempt* and
        once per *worker*, so several threads seeing the same 429 -- or a
        long run of 403s from one host -- each piled another cooldown onto
        an already-future time. checklinks against a WordPress.com site hit
        exactly this: ~500 bot-blocked /log-in URLs, escalating to the
        1800s cap, accumulated a 48-hour backlog on one host and looked to
        the operator like an infinite hang (2026-08-28).
        """
        now = time.monotonic()
        self._next_allowed[host] = max(self._next_allowed.get(host, now), now + cooldown)

    def note_response(self, host: str, status: int, retry_after: float | None = None) -> None:
        """Record an HTTP status for `host`, called once per attempt (see
        fetch_with_retries) rather than only on the terminal one, so a 429
        or a lockout streak backs the host off for concurrent siblings
        immediately rather than only after this URL's own retries are
        exhausted. `retry_after` (seconds) is honoured for a 429; for
        anything else it is ignored. Any status other than 401/403 resets
        the consecutive-denial count (429 does not reset the *lockout*
        streak specifically -- the two are tracked independently and don't
        interact). Backing a host off logs a warning -- user-visible on
        the console via the standard "wpfreeze" logger, deliberately loud
        (WARNING, not INFO/DEBUG) since it changes the pace of the run."""
        warn_msg: str | None = None
        with self._lock:
            if status == 429:
                strikes = self._rate_limit_strikes.get(host, 0)
                self._rate_limit_strikes[host] = strikes + 1
                if retry_after is not None:
                    # Clamped to the same ceiling the default backoff uses.
                    # An uncapped Retry-After lets any remote host stall the
                    # run for as long as it likes -- `Retry-After: 86400` is
                    # a legal answer and would park that host for a day.
                    cooldown = min(retry_after, self._lockout_cooldown_max)
                    source = "Retry-After"
                    if retry_after > self._lockout_cooldown_max:
                        source = f"Retry-After {retry_after:.0f}s, capped"
                else:
                    cooldown = min(self._lockout_cooldown * (2**strikes), self._lockout_cooldown_max)
                    source = "default backoff"
                self._back_off_host(host, cooldown)
                warn_msg = (
                    f"429 (rate limited) from {host} -- backing off {cooldown:.1f}s "
                    f"({source}) before {host} is fetched again."
                )
            elif self._lockout_threshold is None:
                # Lockout detection disabled -- see the constructor.
                pass
            elif status not in _LOCKOUT_STATUSES:
                self._consecutive_denied[host] = 0
            else:
                count = self._consecutive_denied.get(host, 0) + 1
                self._consecutive_denied[host] = count
                if count >= self._lockout_threshold:
                    self._consecutive_denied[host] = 0
                    strikes = self._lockout_strikes.get(host, 0)
                    self._lockout_strikes[host] = strikes + 1
                    cooldown = min(self._lockout_cooldown * (2**strikes), self._lockout_cooldown_max)
                    self._back_off_host(host, cooldown)
                    warn_msg = self._lockout_message(count, host, cooldown)
        if warn_msg is not None:
            logger.warning(warn_msg)

    @staticmethod
    def _lockout_message(count: int, host: str, cooldown: float) -> str:
        return (
            f"{count} consecutive 401/403 responses from {host} -- this looks like a "
            f"site-side lockout (e.g. a security plugin blocking this IP), not "
            f"individually private pages. Backing off {cooldown:.1f}s before {host} "
            f"is fetched again."
        )


def _is_transient(status_code: int) -> bool:
    return status_code in _TRANSIENT_STATUSES or 500 <= status_code < 600


def fetch_with_retries(
    url: str,
    session: requests.Session,
    rate_limiter: RateLimiter,
    config: FetchConfig,
) -> FetchOutcome:
    """Fetch `url`, retrying transient failures with exponential backoff.

    Returns a FetchOutcome classifying the terminal state: SUCCESS with a
    FetchResult, or WAYBACK_CANDIDATE with a flag explaining why (None for
    a plain 404/410, FLAG_AUTH_GATED for 401/403, FLAG_RETRY_EXHAUSTED for
    a transient error that never recovered, FLAG_ODD_RESPONSE otherwise).

    Every response (not just the terminal one) is reported to
    rate_limiter.note_response -- a 429 on attempt 1 of 4 must back the
    host off before attempt 2's own rate_limiter.wait(host) call, not
    just for other URLs, but for the retries this same call is about to
    make. Without that, this function's own local exponential backoff
    (config.backoff_base doubling, a few seconds total) is what a real
    site's rate limiting exhausted in production -- see fetch.py's
    RateLimiter docstring.
    """
    host = urlsplit(url).hostname or ""
    attempt = 0
    response: requests.Response | None = None
    error: str | None = None

    while True:
        attempt += 1
        rate_limiter.wait(host)
        error = None
        try:
            response = session.get(
                url,
                headers={"User-Agent": config.user_agent},
                timeout=config.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            error = str(exc)
            logger.debug("attempt %d for %s raised %s", attempt, url, error)
        else:
            logger.debug("attempt %d for %s -> %s", attempt, url, response.status_code)
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            rate_limiter.note_response(host, response.status_code, retry_after=retry_after)

        transient = error is not None or _is_transient(response.status_code)
        if not transient:
            break
        if attempt >= config.max_attempts:
            break
        backoff = config.backoff_base * (2 ** (attempt - 1))
        time.sleep(backoff)

    if error is not None:
        return FetchOutcome(
            category=WAYBACK_CANDIDATE,
            http_status=None,
            attempts=attempt,
            flag=FLAG_RETRY_EXHAUSTED,
            error=error,
        )

    assert response is not None  # error is None => a response was received
    status = response.status_code
    # note_response already called per-attempt above (including for this
    # terminal response) -- not called again here.

    if 200 <= status < 300:
        result = FetchResult(
            status_code=status,
            content=response.content,
            headers=dict(response.headers),
            content_type=response.headers.get("Content-Type"),
            final_url=response.url,
            redirect_chain=[h.url for h in response.history],
        )
        return FetchOutcome(category=SUCCESS, http_status=status, attempts=attempt, result=result)

    if _is_transient(status):
        # Retries exhausted while still seeing a transient status.
        return FetchOutcome(
            category=WAYBACK_CANDIDATE,
            http_status=status,
            attempts=attempt,
            flag=FLAG_RETRY_EXHAUSTED,
        )

    if status in (404, 410):
        return FetchOutcome(category=WAYBACK_CANDIDATE, http_status=status, attempts=attempt)

    if status in (401, 403):
        return FetchOutcome(
            category=WAYBACK_CANDIDATE, http_status=status, attempts=attempt, flag=FLAG_AUTH_GATED
        )

    return FetchOutcome(
        category=WAYBACK_CANDIDATE, http_status=status, attempts=attempt, flag=FLAG_ODD_RESPONSE
    )
