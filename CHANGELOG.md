# Changelog

Notable changes to wpfreeze. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.8] — 2026-11-26

### Changed

- **`acquire` writes the capture's diagnostics itself** (`diagnostics.json`)
  instead of asking whether to. It takes about a second. The cleanup
  checklist's capture-integrity section reads this file, so a separate
  `diagnose` run is no longer needed to keep the checklist current.
  `wpfreeze diagnose` still regenerates it on demand.

## [1.3.7] — 2026-11-26

### Changed

- **`build-report.json` records which wpfreeze version made the build**
  (`wpfreeze_version`). On disk, a build made before a fix looks exactly
  as current as one made after it. This makes "rebuild to pick up fixes"
  answerable.

## [1.3.6] — 2026-11-26

### Fixed

- **`checklinks` on a site with no external links now records that it
  ran.** It printed "No external links to check." and wrote nothing, which
  on disk looks the same as never having checked. It now writes empty
  results and refreshes the owner worksheet like any other check.

## [1.3.5] — 2026-11-26

### Fixed

- **A rebuild now refreshes the owner worksheet.** `owner-tasks.html` uses
  both the link check's results and the build report, but only
  `checklinks` regenerated it. After a rebuild, the worksheet meant for the
  site owner could be out of date while looking current. Once `checklinks`
  has run at least once, every `build` now refreshes it too.

## [1.3.4] — 2026-11-26

### Fixed

- **Links to other sites go to those sites, not to stored copies.** Some
  invisible attributes that come along with pasted content were read as
  links to files the page needs: Gmail's `data-saferedirecturl` and
  CKEditor's `data-cke-saved-href`. Acquisition then downloaded Google's
  redirect pages and other sites' pages into the capture. The build
  pointed visitors at those frozen copies instead of the real pages: a
  publisher's book page on one site, and a newspaper op-ed, a survey and
  YouTube videos in a video block on another. Acquisition now ignores
  these attributes, the build removes them from pages, and the build never
  stores another site's page or links to a copy of one. Existing captures
  are fixed by rebuilding; no new `acquire` is needed.

## [1.3.3] — 2026-11-26

### Fixed

- **More links to WordPress attachment pages now reach the file.** The
  build points a link to an attachment page (WordPress's one-page-per-upload
  wrapper) at the file itself. It only recognised an image linked to its
  full-size version, so links to a PDF or audio attachment, which WordPress
  shows as a plain link, were left pointing at the original site. So were
  `?attachment_id=N` links, which reach the same page by its ID. Both now
  resolve. Only a file that was actually captured is ever used, and the
  page header, where the site logo lives, is never searched. On one real
  site this fixed 7 of 43 unresolved links.

## [1.3.2] — 2026-11-26

### Fixed

- **`upload.sh` no longer uploads `.claude/` folders.** Claude Code leaves
  a `.claude/` folder in whatever directory it runs a command from. One in
  `site/` was copied to the server along with the site, and nothing warned
  about it: the build's stale-file listing counts files, and the folder
  held only an empty subfolder. Every mode now removes `.claude/` folders,
  at any depth, from its copy of the site before syncing, and the next
  sync deletes any already on the server. Rerun `wpfreeze upload-script`
  to rewrite an existing `upload.sh`.

## [1.3.1] — 2026-11-26

### Fixed

- **Comment counts in Divi's blog and archive listings are now removed
  too.** Each post in those listings ends its byline with a plain-text
  `| 0 Comments`. The build removed the "N comments" count only from a
  post's own page, where it links to the comment form, so the listing
  counts survived. That included "0 Comments", which is documented as
  always removed. They now follow the same rule: a zero count always goes,
  and every count goes with `strip_comment_counts: true`, the default.

## [1.3.0] — 2026-11-26

### Added

- **`customize.py` can add CSS.** `site.add_css(css, pages=None)` appends a
  `<style>` block to the end of each page's `<head>`, after the theme's own
  stylesheets, to fix a layout bug in the original theme across the whole
  site. See "Customizing a build" in the README.

## [1.2.0] — 2026-11-26

### Added

- **A site owner's last-minute edits now survive every rebuild.** Put them
  in `<output_dir>/customize.py`: remove elements by CSS selector, delete a
  page along with every link and redirect to it, unwrap links, or replace
  one passage of HTML. `build` runs the script straight after writing the
  pages, so verification, the search index and the reports all describe
  the customized site. Set `customize: true` in the site's config to run it
  every time, or `false` to never run it. With the key absent, `build`
  asks, and if it can't ask, it skips the script and says so. Any edit that
  matches nothing stops the build with the site unchanged, and every edit
  prints what it matched. See "Customizing a build" in the README.

## [1.1.2] — 2026-11-25

### Fixed

- **Lightbox image lists no longer show up as missing files.** 1.0.7 taught
  the build to split the lightbox's `lightboxSrcset` list into separate
  addresses, but acquisition still requested the whole list as one address.
  Each request 404'd and was recorded as a missing file: 125 of the 166
  "missing" entries on one real site. The images themselves were captured
  all along. Acquisition now splits the list too. The bogus entries go with
  the next fresh `acquire`.

## [1.1.1] — 2026-11-25

### Fixed

- **The WordPress image lightbox no longer creates fake "missing" pages.**
  The lightbox marks up each image with attributes for WordPress's own
  scripts, such as `data-wp-bind--srcset="state.enlargedSrcset"`. Their
  values name script settings, not addresses, but wpfreeze read that one as
  a link. During acquisition it requested `<page>/state.enlargedSrcset`
  once per page with a lightbox and recorded each 404 as a missing page (30
  of 166 on one real site), and the build counted them again as unresolved
  references. These attributes are now skipped, except `data-wp-context`,
  which can hold real image addresses. A rebuild clears them from the build
  report; the missing-page records go with the next fresh `acquire`.

## [1.1.0] — 2026-11-25

### Added

- **Outlook "Safe Links" are replaced by the address they wrap.** A link
  copied out of an Outlook email into a post arrives as
  `https://<region>.safelinks.protection.outlook.com/?url=…&data=…`. It
  reaches the right page only while Microsoft keeps honouring the wrapper,
  and its `data=` carries the original recipient's email address. `build`
  now writes the wrapped address instead, follows a Safe Link inside a Safe
  Link (from a forwarded email), and leaves anything whose wrapped address
  isn't a plain web link alone. On by default; turn off with
  `policy: {unwrap_safelinks: false}`.

## [1.0.7] — 2026-11-25

### Fixed

- **Enlarged lightbox images come from the archive, not the original site.**
  WordPress's image lightbox lists each image's sizes in page data as one
  string (`a.jpg 1200w, a-300x205.jpg 300w, …`). The rewriter tried to
  match that whole string as one address, which never works, so every
  enlarged image kept loading from the live site and would break once it
  is gone. A value whose name contains "srcset" is now rewritten one
  address at a time, the same way as an `srcset` attribute. On a real
  capture this rewrote 632 image addresses and cleared 133 of the build
  report's unresolved references.

## [1.0.6] — 2026-11-25

### Fixed

- **Files with non-ASCII names are reachable in the built site.** A file
  whose address contains a character such as `©`, a narrow no-break space
  or Japanese text was written to disk under its percent-encoded name,
  `%C2%A9-photo.jpg`. A browser or web server decodes the link before
  looking, asks for `©-photo.jpg`, and gets a 404. These are what
  `checklinks` listed as "missing": 211 files across three real sites,
  plus every page with a non-ASCII address. Files are now written under
  their real name and linked by the encoded form. A plain `build` fixes an
  existing capture, no re-acquire needed. Links written in a different
  spelling of the same address (raw Unicode, or lowercase `%e3…`) now
  resolve as well.

## [1.0.5] — 2026-11-25

### Fixed

- **Pages with non-ASCII slugs are no longer lost from the capture.**
  WordPress writes such a slug both ways, raw (`/bcヘリテージ…/`) and
  percent-encoded (`/bc%E3%83%98…/`), and wpfreeze kept them as two
  different URLs. Fetching one spelling reported the other as its final
  address, the canonical link pointed back again, and the page's manifest
  record was folded into itself and deleted. The page was fetched but never
  built, and no report listed it as missing: 21 pages of a Japanese/English
  site and one of another. URLs are now always percent-encoded, so both
  spellings are one URL, and a redirect that loops back to its own record
  leaves that record alone. Re-run `acquire` for a site with such slugs:
  an existing manifest is already missing those pages.

## [1.0.4] — 2026-11-24

### Fixed

- **Inline scripts on WordPress 6.9+ sites no longer break in the built
  site.** WordPress ends every inline script with a `//# sourceURL=…`
  comment, and the link rewriter read `//#` as a protocol-relative URL
  with no host, resolved it to the page itself, and replaced it with
  `index.html# sourceURL=…`. That removed the comment marker and turned
  each such script into a syntax error, taking down every plugin
  configured through one (theme settings, social sharing, page caching
  and so on). Seen on 640 built pages across three real sites. A
  protocol-relative URL now has to start with a host.
- **Import maps keep working after the rewrite.** A rewritten address for
  a file in the same directory (`wp-includes/js/…`) is a bare specifier
  inside a `<script type="importmap">`, and the browser rejects bare
  specifiers as targets. That broke WordPress's own script modules, such
  as the image lightbox. Those addresses now start with `./`.

## [1.0.3] — 2026-11-23

### Changed

- **`checklinks` always re-scans the built site when it is there**, and
  falls back to the saved `external-links.json` only once `site/` is
  gone, saying when that list was made. Previously `--recheck` chose the
  saved list even with `site/` present, and the saved list can be older
  than the site it describes: on a real WordPress.com archive it carried
  228 links that existed only in a build replaced hours later, all of
  which were checked and handed to the site owner. Scanning takes
  seconds and is offline, so trusting a possibly stale list gained
  nothing. A re-scan whose set of links differs from the saved list now
  says how many were added and how many are gone. A `--site-dir` that
  doesn't exist is an error, never a silent switch to the saved list.
  `--recheck` is still accepted so existing scripts keep running, but it
  does nothing now except print a note saying so.

## [1.0.2] — 2026-11-23

### Fixed

- **A rebuild no longer leaves the previous build's output behind.**
  `build` wrote into the existing `site/` and never removed anything, so
  a file an earlier build produced and this one doesn't (a page whose
  output path changed, a bundle whose content hash moved, the whole
  Pagefind index after `search.enabled` was turned off) stayed there and
  shipped with the site. Verification only scans the current run's own
  output, so nothing reported it. This came up for real: 1.0.1's
  subdirectory-install fix moved reseaufranco's home page from
  `site/reseaufranco.html` to `site/index.html`, and the old copy shipped
  until it was removed by hand. `build` now records every file it writes
  in `build-files.json` (beside `site/`, never in it) and, on the next
  build, deletes only files from that record that this build didn't
  write *and* that are unchanged since (same size and mtime). A stale
  file that has been hand-edited is kept and listed. A file no build
  recorded writing, including anything put there by hand, is never
  deleted, only listed in the build summary and in `build-report.json`'s
  new `prune` section. Because the first build after upgrading has no
  record to go on, it deletes nothing.

## [1.0.1] — 2026-09-16

A colleague's rebuild crashed on the pre-fix `is_wp_admin_infrastructure`
code after they upgraded, indistinguishable by `--version` from a build
that had the fix — the version number had not moved since 1.0.0 despite
five behavioural commits landing under it. From here, every commit that
adds a `Fixed`/`Changed` entry bumps this file and `pyproject.toml`'s
`version` together, so `--version` is always a true answer to "do I have
this fix".

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

- **A stray square bracket in a page's JavaScript no longer kills the whole
  build.** Finding references inside inline `<script>` blocks means
  pattern-matching for things that look like URLs, then inspecting each
  candidate — and Python's `urlsplit` does not merely give a poor answer for
  one shape, it refuses outright: an unbalanced `[` reads as a malformed IPv6
  literal and raises `ValueError("Invalid IPv6 URL")`. A fragment like `//]`
  closing a regex literal is ordinary in minified JavaScript. Every call site
  was written expecting an answer, so the refusal propagated out and aborted
  the build *after* the crawl had already finished. Unparseable strings are
  now filtered out like any other false positive, via a new
  `urlnorm.safe_urlsplit`.

  `resolve_url` had guarded the extraction entry point against precisely this
  since it was written; the crash came in through the doors that bypass it.
  An audit of every URL-parsing call reachable from page markup closed the
  rest: `SiteProfile.in_scope` (asked by crawl, build, checklinks *and*
  rescan — the widest reach of any of them), `lookup_variants` (raw anchor
  hrefs in the attachment-page scan), `verify_site` (re-reads references the
  rewriter deliberately left alone), `policy._is_telemetry_ref` and
  `policy._is_wpcom_actionbar` (both run on every build), and
  `linkcheck.extract_external_links`, where a malformed href was additionally
  being reported as a broken external link rather than passed over.

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
