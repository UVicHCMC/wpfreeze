# Changelog

Notable changes to wpfreeze. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `owner-tasks.html` now leads each item with the replacement-address box
  rather than hiding it behind the dropdown: pasting an address selects
  "I have a new address for this" by itself, and clearing it takes the
  item back to unanswered, so the common case never involves the dropdown.
- **WordPress.com's injected action bar is now stripped at build time**
  (`policy.strip_wpcom_actionbar`, on by default): the floating strip of
  Sign up / Log in / Copy shortlink / Report this content / View post in
  Reader / Manage subscriptions, plus the loader and `actionbardata`
  scripts that go with it. It is per-page and therefore the largest single
  source of external links in a WordPress.com capture — measured on a real
  248-page site, it was 688 of 1154 unique external targets, so a
  `checklinks` run over that archive drops by 60%. Matched on
  `id="actionbar"` plus a corroborating `actnbr-` class or wordpress.com /
  wp.me link, never the id alone; the "Website Powered by WordPress.com"
  footer credit is attribution and is left alone.
- The owner worksheet says what went wrong in plain English — "This
  website no longer exists", "The page is gone. The website is still
  there, but this page is not" — instead of the urllib line it used to
  show a site owner ("unreachable (HTTPConnectionPool(host='bnb.bl.uk',
  port=80): Max retries exceeded..."). That line stays in
  `broken-external-links.md`/`.html`/`.json`, whose reader is the
  archivist. `checklinks` now records a `kind` per failed link (`dns`,
  `tls`, `timeout`, `refused`, `redirect_loop`, `auth`, `http`,
  `unreachable`), classified from the whole error string at check time —
  `reason` is truncated at 120 characters, usually mid-exception-name, so
  it cannot be classified afterwards.
- Each link's wording in the worksheet is a Google search for itself,
  opened in a new tab, so an owner can go looking for where the page moved
  and paste the new address straight back into the box above it.
- Dead outbound links show the wording they are linked under, beside the
  address. `checklinks` records it during extraction, so
  `external-links.json` and `broken-external-links.json` both gained a
  `texts` field (a bounded list of the distinct wordings a target is
  linked under, falling back to a wrapped image's alt text or the link's
  title). An `external-links.json` written before this field is re-scanned
  once by the next `--recheck` that can see `site/`, which also reports
  any links that have appeared or disappeared since it was written.
- The owner worksheet's progress count now counts only the decisions the
  owner actually owes. Auth-gated links arrive pre-answered "leave as is",
  and counting them opened janellejenstad's worksheet at "256 of 347
  handled" before the owner had touched it. The review panel summarises
  them in one line instead of listing them.
- The worksheet's name field and its only **Save my answers** button now
  live in the sticky bar, with the block that used to hold them at the
  foot of the page removed. There is one save affordance and nothing to
  scroll to the bottom for.
- The worksheet no longer carries a "things we already handled" footnote.
  Filtered findings are the archivist's business, not the owner's:
  `wpfreeze owner-tasks` prints the count on the console instead.

- `wpfreeze validate` no longer requires a system Java. When `java` is on
  `PATH` *and can run the jar* it still fetches the ~32 MB `vnu.jar`;
  otherwise it fetches the validator project's self-contained
  `vnu.linux.zip` (~66 MB, bundles its own runtime). The chosen checker is
  verified with a `--version` run before use, so a JVM too old for the
  current release falls back to the self-contained build instead of
  failing mid-validation. Both cache under `~/.cache/wpfreeze/`, and each
  logs a line before a download starts. The `vnu_jar:` config key now
  accepts either a `.jar` or a path to a `vnu` executable; a pinned
  checker that cannot run is reported, never silently replaced.
- The annotated `example-site.yaml` now ships inside the package
  (`wpfreeze/example-site.yaml`), so an installed copy has it on disk
  rather than only a checkout. `wpfreeze wizard` prints its path; in a
  checkout it also moved under `wpfreeze/`. The `SETUP.md` /
  `EXTRA-CONFIG-OPTIONS.md` pointers now resolve to a GitHub URL when
  those (deliberately unpackaged) files aren't beside the install.

### Fixed

- **A WordPress multisite subdirectory install now builds an `index.html`.**
  Previously only a site at the domain root got one: a site at
  `example.com/subsite/` had its home page written to `site/subsite.html` —
  a lone file sitting beside the `site/subsite/` directory holding every
  other page — so the built site had no `index.html` anywhere and browsing
  the archive root gave nothing. The site's own root now maps to
  `/index.html`; sibling paths keep their prefix, so the tree still mirrors
  the live site's URLs. `redirects.htaccess` gains a matching rule ahead of
  the generic directory rule, so an archive redeployed at its original path
  still serves its home page.

- `policy.strip_login_links` is now actually read from config. The flag was
  documented in the README and set in `example-site.yaml`, but
  `Policy.from_config` never read it, so `strip_login_links: false` was
  silently ignored and login links were always unwrapped.

- `wpfreeze freeze` no longer aborts with exit `2` when no HTML checker can
  be obtained (no JVM and nothing cached). The `validate` step is marked
  `skipped` in the wrap-up and the sequence continues — `validate` is
  informational and `acquire`/`build` have already produced the archive.
  Run on its own, `wpfreeze validate` still exits `2` in that situation.

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

- `build` unwraps WordPress login and admin links — `wp-login.php`,
  `/wp-admin/`, and WordPress.com's hosted `/log-in`. Dead plumbing on an
  archive in the same sense as the forms and feed links `build` already
  strips: nobody can log in to a static copy, and following one sends a
  visitor to the live site's login screen. The visible text is kept; only the
  destination goes. Off with `policy: {strip_login_links: false}`.
- `freeze.unattended: true` makes `freeze` behave in a terminal exactly as it
  does when piped — it never prompts. Set it together with `checklinks` in
  `freeze.steps` for a genuinely hands-off full run.

### Fixed in this release

- `acquire`'s step timer counted time spent waiting at the interactive
  follow-up prompts: a 12-minute crawl was reported as `acquire 4h19m`. Offered
  steps are now timed individually, and a run ends with one wrap-up instead of
  three.
- `checklinks` could stall indefinitely on a host that serves 403 to bots.
  Two compounding causes: a run of 401/403 responses was read as a site-side
  lockout and escalated the host's cooldown to the 1800s cap, and each backoff
  was *added* to an already-future time rather than taking a maximum, so the
  cooldowns accumulated without bound. Checking a WordPress.com site, whose
  every page carries a bot-blocked `/log-in` link, built a 48-hour backlog on
  one host. Backoffs no longer accumulate — which also fixes concurrent workers
  stacking one shared 429 — and `checklinks` no longer treats 401/403 as a
  lockout at all: from a third-party host that is an ordinary answer and a
  result to report, not a signal to slow down. `acquire` keeps the lockout
  detection, where a run of denials really does mean the site has banned you.
- A server-supplied `Retry-After` was honoured without an upper bound, so any
  remote host could park a run for as long as it liked. It is now clamped to
  the same ceiling the default backoff already used.
- The wizard printed doc pointers as bare relative filenames, which resolved to
  nothing when wpfreeze ran anywhere but a checkout. They now resolve to
  absolute paths when the docs are on disk beside the package — a checkout or
  an editable install. In a non-editable packaged install the docs are not
  shipped at all, so the pointer names the file and says it lives in the source
  distribution rather than printing a path that does not exist. Shipping the
  docs as package data would close this properly and is worth doing if wpfreeze
  is ever published.

### Known limitations

- A WXR export alone cannot rebuild a working site: it carries post/page
  content and metadata, not media files, the theme, or rendered markup.
  wpfreeze uses it only as a third inventory source to cross-check the
  live crawl against.
- VNU validation requires a JVM. It is offered only when `java` is on `PATH`.
  (Lifted after 1.0.0 -- see Unreleased.)
- Sites behind aggressive CDN rate limiting may need a slower `rate_limit`.
- No CI. For a single-maintainer tool this is a deliberate omission.
