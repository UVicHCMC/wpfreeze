"""Fetcher: rate-limited, retrying HTTP client with failure classification.

See CLAUDE-acquire.md, "Stage 3 -- Failure classification and retry":
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
from urllib.parse import urlsplit

import requests

from wpfreeze.manifest import FLAG_AUTH_GATED, FLAG_ODD_RESPONSE, FLAG_RETRY_EXHAUSTED

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "wpfreeze/0.1 (+https://example.invalid/wpfreeze-contact)"

SUCCESS = "success"
WAYBACK_CANDIDATE = "wayback_candidate"

_TRANSIENT_STATUSES = {429}


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
    within one.
    """

    def __init__(self, rate_limit: float) -> None:
        self._rate_limit = rate_limit
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            next_allowed = self._next_allowed.get(host, now)
            sleep_for = max(0.0, next_allowed - now)
            self._next_allowed[host] = max(now, next_allowed) + self._rate_limit
        if sleep_for > 0:
            time.sleep(sleep_for)


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
