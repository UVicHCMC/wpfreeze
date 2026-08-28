# Changelog

Notable changes to wpfreeze. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [semantic](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-08-28

First stable release. Development ran from 2026-07-06 over 101 commits; the
version number moves from `0.1.0` to `1.0.0` because the tool has been
validated against four real WordPress sites and the command surface is now
settled, not because anything changed on release day.

### What wpfreeze does

Acquires a complete, verified static archive of a WordPress site — pages and
assets — cross-checked against sitemap, REST API and WXR-export inventory, with
Wayback Machine recovery for content the live site no longer serves. Intended
for site owners retiring a site who need the result to *replace* the original.

### Commands

- `freeze` — run a project's whole declared sequence (acquire → build →
  validate) as one command, with a combined timing and artefact wrap-up.
- `acquire` — the crawl: inventory discovery, fixpoint crawl, Wayback recovery,
  analysis flags, output-path mapping, and `report.html`/`report.json`.
- `build` — rewrite a capture into a servable static site: local reference
  rewriting, concat-bundle reassembly, attachment-link retargeting, telemetry
  and form stripping, shared-stylesheet lifting, and inert-markup repair.
- `validate` — check the built site's HTML and CSS with VNU, and generate a
  cleanup checklist.
- `checklinks` — report external links that are now broken, grouped by the
  internal page that links to them.
- `search-index` — build the offline Pagefind index (also run automatically by
  `build` when `search.enabled` is set).
- `rescan` — re-parse stored bytes without re-crawling the site.
- `diagnose`, `report`, `status` — inspect an existing capture.
- `wizard` — interactive setup; bare `wpfreeze` gives a non-interactive
  overview, or an interactive project picker in a real terminal.
- `upload-script` — generate a deployment script with staging/`--local`/`--prod`
  modes.

### Notable capabilities

- **Offline search.** A Pagefind-backed search box replaces WordPress's own,
  which cannot work on a static archive — and which on a stock install does not
  search page content at all.
- **Content checks.** Thin and echoed-content detection, with
  `search.acknowledged_thin_pages` to stop repeat warnings becoming noise.
- **Politeness by design.** Per-host rate limiting shared across worker threads,
  429 backoff honouring `Retry-After`, and whole-host backoff on repeated
  401/403 lockouts.
- **Multisite awareness.** Acquisition is confined to `base_url`'s path on
  subdirectory installs, with `extra_hosts` for deliberate exceptions.
- **Honest reporting.** `report.html` explains gaps rather than hiding them, and
  a run that finished with missing content exits 1.

### Added in this release

- `wpfreeze --version`, reporting the installed distribution's version.
  `wpfreeze.__version__` now reads package metadata, so it can no longer drift
  from `pyproject.toml`.
- `freeze` asks before resuming an existing acquisition, showing the record
  count and age, and explains how to start fresh if you decline. Unattended runs
  are never prompted and resume as before.

### Fixed in this release

- `acquire`'s step timer counted time spent waiting at the interactive
  follow-up prompts: a 12-minute crawl was reported as `acquire 4h19m`. Offered
  steps are now timed individually, and a run ends with one wrap-up instead of
  three.
- The wizard printed doc pointers as bare relative filenames, which resolved to
  nothing when wpfreeze ran anywhere but a checkout. They now resolve to
  absolute paths when the docs are on disk beside the package — a checkout or
  an editable install. In a non-editable packaged install the docs are not
  shipped at all, so the pointer names the file and says it lives in the source
  distribution rather than printing a path that does not exist. Shipping the
  docs as package data would close this properly and is worth doing if wpfreeze
  is ever published.

### Known limitations

- WXR exports alone cannot rebuild a working site; see `WXR-LIMITATIONS.md`.
- VNU validation requires a JVM. It is offered only when `java` is on `PATH`.
- Sites behind aggressive CDN rate limiting may need a slower `rate_limit`.
- No CI. For a single-maintainer tool this is a deliberate omission.
