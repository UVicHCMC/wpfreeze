"""Integration-level coverage for the 429/backoff path: a real, multi-
threaded `run_acquire` (real crawl_fixpoint, real worker threads) against
a real local HTTP server that actually returns 429 with a real
Retry-After header -- not fetch_with_retries/RateLimiter exercised in
isolation, which tests/test_fetch.py already covers exhaustively (single
429, cooldown escalation, Retry-After parsing, per-host independence,
interaction with 401/403 streaks, sibling-URL backoff propagation).

What this closes: whether the actual acquire pipeline wires the
RateLimiter's host-wide backoff through correctly, end to end, rather
than just the underlying mechanism being correct on its own. Still not
the same as triggering a 429 from a real, live WordPress host under real
network conditions -- deliberately not attempted here, since provoking
that against a real site not under our control would be inconsiderate
to the site owner and contrary to wpfreeze's own politeness design.
That remains a standing gap in coverage.
"""
from __future__ import annotations

import time
from pathlib import Path

from fixture_site import FixtureSite
from wpfreeze.manifest import Manifest, Status


def _config_for(site: FixtureSite, output_dir: Path):
    from wpfreeze.cli import SiteConfig, WaybackSettings

    return SiteConfig(
        base_url=site.site_base + "/",
        output_dir=output_dir,
        rate_limit=0.0,
        wayback_rate_limit=0.0,
        exclusions=[],
        wayback=WaybackSettings(enabled=False),
    )


def test_run_acquire_backs_off_and_retries_a_real_429_through_the_full_pipeline(
    tmp_path: Path, capsys
):
    """/about/ 429s once (with a real Retry-After: 3 header) then succeeds.
    Retry-After (3s) is set deliberately larger than fetch_with_retries'
    own fixed first-retry backoff (backoff_base=1.0, so ~1s) -- if the
    crawl finishes in ~1s, only the function's own per-call backoff fired
    and the RateLimiter's host-wide Retry-After cooldown was never
    actually engaged; only a ~3s elapsed time proves the real mechanism,
    not just *a* delay, ran through the real pipeline.

    Uses capsys, not caplog: `_configure_logging` (cli.py) attaches
    handlers directly to the "wpfreeze" logger with propagate=False (so a
    test runner's own root-logger handlers can't filter it out) -- which
    also means caplog's default root-attached capture never sees it. The
    console handler is a plain StreamHandler (stderr), which capsys does
    see.
    """
    from wpfreeze.cli import run_acquire

    with FixtureSite(
        status_scripts={"/about/": [429, 200]},
        extra_headers={"/about/": {"Retry-After": "3"}},
    ) as site:
        config = _config_for(site, tmp_path / "out")
        start = time.monotonic()
        exit_code = run_acquire(config, resume=False, dry_run=False)
        elapsed = time.monotonic() - start

        assert exit_code in (0, 1)  # must not crash; /secret/ (403) already makes 1 a gap

        manifest = Manifest.load(config.output_dir / "manifest.json")
        about = manifest.get(site.site_base + "/about/")
        assert about is not None
        assert about.status == Status.FETCHED.value  # succeeded after the retry, not abandoned

        console = capsys.readouterr().err
        assert "429" in console and "backing off" in console and "Retry-After" in console
        assert elapsed >= 2.5  # the Retry-After cooldown genuinely elapsed, not just backoff_base
