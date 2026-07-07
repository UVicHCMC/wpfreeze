I'm working on wpfreeze, a Python 3.12+ CLI tool that archives a WordPress
site (pages + assets) to a local copy, verifies completeness, recovers
missing content from the Wayback Machine, and produces a manifest + report.
The full spec is in CLAUDE-acquire.md at the repo root.

I need a plan (not code yet) for adding real concurrent fetching. Please
don't write any code — just produce a concrete implementation plan I can
review.

Current state:
- wpfreeze/cli.py's SiteConfig has a `concurrency: int = 2` field, parsed
  from YAML, but it is never read anywhere else in the codebase. It's dead
  config.
- wpfreeze/crawl.py's `crawl_fixpoint()` is the main crawl loop: it snapshots
  `manifest.by_status(Status.PENDING.value)`, then does a plain sequential
  `for record in pending:` calling `_process_one(...)` on each one at a
  time, saving the manifest to disk after every single record. It repeats
  this until no pending records remain (a true fixpoint, not a fixed
  number of passes, since processing a record can discover new pending
  URLs via manifest.get_or_create()).
- wpfreeze/fetch.py's `RateLimiter` is already thread-safe: it holds a
  `threading.Lock()` and enforces a minimum spacing between requests to
  the *same host*, tracked per-host in a dict. It was clearly built with
  concurrency in mind even though nothing concurrent calls it yet.
- wpfreeze/manifest.py's `Manifest` class is a plain wrapper around a
  dict of URL -> ManifestRecord, with no locking at all. `get_or_create()`,
  `resolve_redirect()`, and `save()` (atomic write via temp file + rename)
  all assume single-threaded access.
- wpfreeze/wayback.py has a structurally similar per-record recovery
  function (`_recover_one`) called from a similar sequential loop.
- CLAUDE-acquire.md explicitly says (of Stage 2, the crawl fixpoint): "use
  `concurrent.futures.ThreadPoolExecutor` if concurrency is warranted" —
  so building this is spec-sanctioned, just never implemented.

Known trouble spots I want your plan to address explicitly, not gloss over:
1. Manifest thread-safety: concurrent workers can both resolve a redirect
   to the same final URL, or both trigger the raw-storage path-collision
   repair logic in crawl.py's store_bytes() (which reads a file, deletes
   it, and rewrites it at a new path while scanning all manifest records
   to repoint one that pointed at the old path) — both are read-then-mutate
   sequences on shared state that need a real critical section, not just
   locking individual dict/list operations.
2. Today's crash-safety guarantee is "an interrupted run loses at most the
   one in-flight fetch," from saving after every processed record. Under
   concurrency this guarantee necessarily weakens to something like "loses
   up to `concurrency` in-flight fetches" — I want this trade-off named
   explicitly in the plan, not silently accepted.
3. Whether to keep the current "snapshot a batch, process it, re-snapshot"
   structure (simpler diff, some parallelism lost at the tail of each
   round) vs. a live work-stealing queue that feeds newly-discovered URLs
   to idle workers immediately (more efficient, more complex, more ways to
   get wrong).
4. A single `requests.Session` is currently shared across the whole crawl.
   Sharing one across threads is a common pattern (the underlying
   connection pool is thread-safe) but state this explicitly as a design
   decision rather than assuming it away.
5. RateLimiter needs no changes — say so plainly rather than re-deriving it.

Please also cover: how `concurrency` should flow from SiteConfig through
run_acquire/_run_to_settled into crawl_fixpoint (and whether
wayback.py's recovery loop should get the same treatment, or is
lower-priority given wayback_rate_limit is usually much stricter); what
new tests are needed (specifically: a redirect-race test where two
concurrently-processed records resolve to the same canonical URL, and a
storage-collision-under-concurrency test); and how a worker's exception
should propagate and stop the run in a way that keeps the on-disk manifest
in a valid, resumable state, matching how a sequential crash behaves today.
