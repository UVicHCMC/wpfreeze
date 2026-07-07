# Concurrent fetching for the crawl fixpoint (ThreadPoolExecutor)

Implementation plan, written 2026-07-07 (Fable), reviewed and amended by
Sonnet 2026-07-07 after cross-checking against actual source (four fixes
folded in: narrower lock scope for `manifest.save()`, sha256-inside-lock
clarification, explicit internal locking for `store_bytes` and friends,
and always-use-the-executor for `workers=1`). Input brief:
`fable-concurrency-prompt.md`. Implement exactly this; where code
contradicts the plan, flag it rather than improvising silently.

## Context

`SiteConfig.concurrency` (`wpfreeze/cli.py:66,95`) is parsed from YAML but
never read — the crawl is fully sequential. `CLAUDE-acquire.md:27`
sanctions `concurrent.futures.ThreadPoolExecutor` for Stage 2, and
`RateLimiter` (`wpfreeze/fetch.py:60-81`) was already built thread-safe
(per-host spacing under a `threading.Lock`, sleep taken outside the lock)
in anticipation. This plan wires `concurrency` through to a real worker
pool while preserving the manifest's correctness and resumability.

**Honest expectations, to be stated in docs:** for a single-host site with
`rate_limit > 0`, same-host spacing still serializes fetches, so the gain
is only overlapping response-wait with the next slot's sleep. The real
wins are multi-host runs (site + CDN `extra_hosts` + external render
assets) and `rate_limit: 0` runs where fetch latency, not spacing,
dominates.

## Design decisions (the "why", settled up front)

1. **Batch rounds, not a live work queue.** Keep `crawl_fixpoint`'s
   existing shape: snapshot `manifest.by_status(PENDING)`, process that
   batch through an executor, wait for all futures, re-snapshot, repeat
   until the snapshot is empty. Termination reasoning stays identical to
   today's fixpoint. A live queue feeding newly-discovered URLs to idle
   workers immediately would need in-flight counting / sentinel shutdown
   logic — the classic deadlock/livelock bug farm — for a modest tail-of-
   round gain. Rejected for v1. (Rounds are large in practice: inventory
   seeds hundreds+ of URLs before the first crawl round.)

2. **One coarse re-entrant lock, owned by the Manifest.** Add
   `self.lock = threading.RLock()` in `Manifest.__init__`
   (`wpfreeze/manifest.py`). It guards *compound* read-then-mutate
   sequences, not individual dict ops. Do NOT add per-method locking to
   `Manifest`'s public methods — `resolve_redirect` + subsequent target
   mutation in `_record_success` spans multiple calls and must be atomic
   as a unit; method-level locks would give false confidence while still
   racing. RLock (not Lock) so `store_bytes` can acquire it defensively
   even when its caller already holds it (unit tests call `store_bytes`
   directly).

   The workload is I/O-bound: workers spend nearly all their time inside
   `fetch_with_retries`. Most of what the lock covers is microseconds of
   dict/list work, so the coarse lock costs no measurable parallelism —
   **except `manifest.save()`, which does not get this pass for free.**
   `save()` re-serializes *every* record in the manifest to JSON and
   writes it out on every single processed record; on a large site (we
   watched a real run hit 56,904 records earlier) that's real work, and
   done under this lock it would serialize every worker's bookkeeping
   behind the slowest save. Fix, inside `Manifest.save()`: build the
   `records` list (the part touching shared state) while holding the
   lock, then release it before `json.dump`/`os.replace` (the part that's
   actually slow). This keeps the exact "save after every record"
   guarantee — the JSON on disk is still fully current as of that
   record — while not holding the lock across the disk write.
   `store_bytes` (and `_make_ancestors_writable`/`_demote_file_to_directory`)
   must each take `manifest.lock` themselves, internally — they're called
   directly by existing unit tests with no surrounding lock at all, so
   they can't rely on always being invoked from an already-locked caller.

3. **Critical-section boundaries in `_process_one`** (`wpfreeze/crawl.py`):
   - OUTSIDE the lock: `fetch_with_retries` (the whole point) and
     `discover_links` (BeautifulSoup parse — the one genuinely
     CPU-costly non-network step; parse from `result.content` before
     locking). `content_kind` is a pure string check, cheap either way.
   - INSIDE the lock (one acquisition, in order): the staleness re-check
     (below), the excluded/retrying status flips, all of
     `_record_success` (**including its sha256 hash** — it's computed
     over already-in-memory bytes, microseconds, not worth a signature
     change to hoist out; redirect-chain `resolve_redirect` folding +
     `store_bytes` + target field writes), the `get_or_create` loop over
     pre-parsed links, and the per-record `manifest.save`.

   `store_bytes`' collision repair (`_make_ancestors_writable` /
   `_demote_file_to_directory`) mutates the filesystem AND rescans/
   repoints manifest records — it sits inside the same lock acquisition,
   which is what makes the parent/child path-collision repair safe under
   concurrency.

4. **Staleness guard fix (also fixes a latent sequential bug).** Today's
   guard in `crawl_fixpoint` — `if record.status != Status.PENDING.value:
   continue` — misses records that a redirect merge popped from the dict
   mid-round, because `resolve_redirect` never touches the popped
   record's `status`; the orphaned snapshot object still reads "pending"
   and gets re-fetched. Replace the claim check with, under the lock,
   *before* fetching:
   `manifest.get(record.url) is record and record.status == PENDING`.
   Skip otherwise. (Identity check, not equality — a re-created record at
   the same URL is a different live object.) Accept the small remaining
   window where a record is claimed, fetched, and meanwhile merged away —
   the post-fetch critical section re-runs `resolve_redirect`, which is
   idempotent (`add_alias`/`add_redirect_from` dedupe); worst case is one
   wasted fetch, never corruption.

5. **Crash-safety guarantee is explicitly weakened.** Today: save after
   every record → an interrupted run loses at most the one in-flight
   fetch. With N workers saving inside the lock after each completed
   record, an interrupt/crash loses at most the N in-flight fetches.
   Saves stay atomic (`tempfile.mkstemp` + `os.replace`, unchanged) and
   serialized by the lock, which also prevents `save`'s
   `[r.to_dict() for r in self._records.values()]` from racing a
   concurrent insert (`RuntimeError: dictionary changed size`). State the
   weakened guarantee in `crawl_fixpoint`'s docstring and in
   CLAUDE-acquire.md.

6. **Single shared `requests.Session`** across all workers — explicit
   decision, not an accident: urllib3's connection pool is thread-safe.
   In `run_acquire` (`wpfreeze/cli.py`), mount
   `HTTPAdapter(pool_connections=10, pool_maxsize=max(10, concurrency))`
   for both `http://` and `https://` so connections aren't discarded when
   workers exceed the default pool size of 10 (perf only, not
   correctness).

7. **`RateLimiter` needs zero changes.** Its reserve-slot-under-lock /
   sleep-outside-lock design already serializes same-host requests across
   N threads and lets different hosts proceed in parallel.

8. **Wayback recovery stays sequential — by reasoning, not laziness.**
   Every request `_recover_one` makes (CDX lookup and snapshot fetch)
   targets the same host, `web.archive.org`, and RateLimiter spaces
   same-host requests globally. N workers would immediately queue on the
   limiter for zero throughput gain. Add a short comment on
   `recover_via_wayback` saying exactly this so nobody "fixes" it later.

## Changes by file

- **`wpfreeze/manifest.py`** — `Manifest.__init__` gains
  `self.lock = threading.RLock()` (+ `import threading`). Docstring: the
  lock guards compound mutation sequences during concurrent crawling;
  single-threaded callers may ignore it. `save()` builds the `records`
  list under `self.lock`, then releases it before `json.dump`/
  `os.replace` (see design decision 2 — this is the one place the lock
  boundary is narrower than "the whole method"). `Manifest.load` needs no
  change (fresh instance gets a fresh lock; nothing serializes it).

- **`wpfreeze/crawl.py`** —
  - `crawl_fixpoint` gains a `workers: int = 1` parameter. Always builds
    a `ThreadPoolExecutor(max_workers=workers)` — even for `workers=1` —
    rather than branching to a separate sequential code path, so every
    existing test that calls `crawl_fixpoint` directly (most of
    `test_crawl.py`, via its default) exercises the same machinery the
    concurrent path uses, not a divergent legacy loop. Round loop:
    snapshot pending; if empty, break; else submit each record to the
    executor and drain with `concurrent.futures.as_completed`. One
    executor for the whole call (`with` block wrapping the round loop),
    not one per round, to avoid thread churn.
  - `_process_one` restructured per the critical-section boundaries and
    staleness guard above. `manifest.save` moves inside the lock (still
    once per processed record; note the narrower internal lock scope
    inside `save()` itself, above).
  - `store_bytes`, `_make_ancestors_writable`, `_demote_file_to_directory`
    each wrap their body in `with manifest.lock:` — required because
    existing unit tests call `store_bytes` directly with no external
    locking; the `RLock` choice means this is safe even when the call
    arrives already inside `_process_one`'s lock.
  - Exception policy: on the first future that raises (observed via
    `as_completed` — not necessarily the first submitted or first to
    fail in wall-clock time, just the first one we see), call
    `executor.shutdown(wait=True, cancel_futures=True)` — queued records
    are cancelled, in-flight workers finish their current record (their
    locked saves complete normally) — then re-raise that exception.
    On-disk manifest stays valid and resumable: unfinished records are
    still `pending`, matching sequential-crash semantics that
    `test_crawl_is_resumable_after_interruption` already pins down.

- **`wpfreeze/wayback.py`** — comment on `recover_via_wayback` explaining
  why it stays sequential (single-host, limiter-serialized). Optionally
  wrap `_recover_one`'s manifest mutations in `manifest.lock` for
  uniformity; harmless since nothing else runs concurrently with it.

- **`wpfreeze/cli.py`** —
  - `load_config`: raise `ConfigError` if `concurrency < 1` (explicit
    failure over silent clamping, consistent with the config's
    conscious-decision ethos).
  - `run_acquire`: mount the sized `HTTPAdapter`s on the session; pass
    `config.concurrency` through `_run_to_settled` into
    `crawl_fixpoint(workers=...)`.

- **`CLAUDE-acquire.md`** — brief note in the Stage 2 section: the
  ThreadPoolExecutor model implemented (batch rounds, coarse manifest
  lock, save-per-record retained) and the weakened crash guarantee ("at
  most `concurrency` in-flight fetches lost").

- **`example-site.yaml`** — existing `concurrency` comment already
  describes exactly this model (parallelizes across hosts, doesn't
  multiply per-host rate_limit); no change needed.

## Tests (`tests/test_crawl.py` unless noted)

Deterministic-race technique: monkeypatch `crawl_module.fetch_with_retries`
with a fake whose first N calls rendezvous on a `threading.Barrier(N)`
before returning, guaranteeing N workers are genuinely concurrent at the
moment of interest. Follow the existing fake-`FetchOutcome`/`FetchResult`
pattern already used in `test_script_derived_external_url_is_not_queued`
and `test_external_page_is_fetched_but_not_recursed_into`.

1. **Redirect race**: two pending URLs A and B whose fakes both report
   `final_url = C` (with redirect chains), barrier-synced, `workers=2`.
   Assert: exactly one live record for C; both A and B appear in its
   `redirect_from`/`aliases`; no exception; A and B no longer exist as
   independent records.
2. **Storage collision race**: parent URL `/blob/1.0` and child
   `/blob/1.0/LICENSE` fetched concurrently (barrier), `workers=2`.
   Assert both contents retrievable at their records' `local_path`s,
   paths distinct, no `NotADirectoryError`/`FileNotFoundError`.
3. **Parallelism is real**: N URLs on N distinct hosts, fake fetch sleeps
   ~0.25s each, `workers=N`, `rate_limit=0`. Assert wall time well under
   the sequential N×0.25s (generous margin — e.g. < 0.6×N×0.25 — to
   avoid CI flakes).
4. **Staleness guard**: after the redirect-race test's crawl, assert the
   fake fetch was called at most once per unique URL (no re-fetch of a
   merged-away snapshot entry).
5. **Exception propagation + resumability**: `workers=2`, fake fetch
   raises on the 3rd call. Assert `crawl_fixpoint` raises, the on-disk
   manifest loads cleanly, and a re-run with a healthy fetch completes —
   mirror of `test_crawl_is_resumable_after_interruption`.
6. **Config validation** (`tests/test_cli.py`): `concurrency: 0` →
   `ConfigError`; valid value plumbs through to `SiteConfig`.
7. **Existing suite as regression net**: the fixture-site integration
   tests build `SiteConfig` with the default `concurrency=2`, so once
   plumbed they exercise the executor path automatically. All 243
   existing tests must stay green.

## Verification

- `python -m pytest -q` — full suite (243 existing + new) green.
- Manual: `wpfreeze acquire --config site-c.yaml` fresh
  run with `concurrency: 2` (site + external render assets exercise
  multi-host parallelism); confirm completion, sane `report.html`, and
  `wpfreeze status` shows no stuck pending records. Then Ctrl-C a second
  fresh run mid-crawl and confirm `--resume` completes it.
- Sanity-compare wall-clock of the fixture-site integration test run
  before/after (informal; the timing test above is the enforced check).
