![wpfreeze logo](local/media/wpfreeze.png)
# wpfreeze

Acquires a complete, verified local copy of a WordPress site's pages and
assets, cross-checks that copy against the site's own inventory (sitemap,
REST API, and optionally a WordPress XML export), recovers anything missing
from the Wayback Machine, and produces a manifest and report describing
exactly what was captured, what wasn't, and what needs a human to look at
it.

It's built for the "I run this site and I want a durable, offline copy of
it" case — a retiring institutional site, a project wrapping up, a personal
blog you want outside WordPress's care and feeding. It is **not** a general
web scraper and makes no attempt to be polite to sites it doesn't own.

## What it does and doesn't do

- **Does**: fetch every page and asset live, verify completeness against
  multiple independent inventory sources, fall back to Wayback Machine
  snapshots for anything the live site can't serve, and write a
  self-contained `report.html` that tells you exactly what's missing and
  why.
- **Also does**: rebuild the raw capture into a servable static site
  (`wpfreeze build`). Internal links are rewritten to relative local paths,
  WordPress.com `/_static/` concat bundles are reassembled into local CSS/JS,
  attachment-page links are redirected to the media they wrap, and the whole
  tree is verified — every local reference is resolved against a real file on
  disk before the command returns. See "Building a servable site" below.
- **Does not**: execute JavaScript. Link discovery and rewriting are
  markup-based, so anything a script constructs at runtime (a gallery that
  builds image URLs in JS, a JS-only navigation path) is neither captured nor
  rewritten. This is a deliberate scope decision — no headless browser — and
  it's reported in the build's own known-limitations, not silently ignored.
- **Optionally does**: give the archived site a working search box again.
  The original WordPress search is dead on any static copy; with
  `search.enabled`, `build` indexes the site with
  [Pagefind](https://pagefind.app) and wires the theme's *own* existing
  search form to the local index — no new UI, no server, no third-party
  service, nothing leaves the browser. Opt-in and off by default; see
  "Offline search" below.
- **Optionally does**: check whether the pages you're keeping still link
  to something live. `wpfreeze checklinks` finds every external link in
  the built site and reports which internal pages point at ones that are
  now broken — see "Checking external links" below.

## Requirements

- **Linux only.** No Windows or macOS accommodation — this is a deliberate
  scope decision, not an oversight.
- **Python 3.12+.**
- No database of any kind. No PHP, no WordPress install, no MySQL. Just the
  target site's URL (reachable over HTTP/HTTPS) and, optionally, a WordPress
  XML export file.

## Installation

[`pipx`](https://pipx.pypa.io/) is the recommended way to install this — it
gives `wpfreeze` its own isolated environment automatically so it can't
collide with dependencies from any other Python project on your machine,
while still putting a plain `wpfreeze` command on your `PATH`.

If you don't already have `pipx`:

```bash
# Debian/Ubuntu
sudo apt install pipx

# Any Linux distro with Python already installed
python3 -m pip install --user pipx
pipx ensurepath
```

Then install wpfreeze itself:

```bash
pipx install git+https://github.com/<you>/wpfreeze.git
```

(Replace the URL with wherever this repo actually ends up — there's no
public host configured yet.)

Offline search (see "Offline search" below) needs one more package, which
a plain `pipx install` does not pull in — `pipx`'s isolated environment
needs `pipx inject`, not `pip install`, to add a package to an app it
already manages:

```bash
pipx inject wpfreeze 'pagefind[extended]'   # only if you want offline search
```

If you'd rather not use `pipx`, a plain `pip install git+https://...` works
identically, just without the automatic environment isolation.

### For development

```bash
git clone https://github.com/<you>/wpfreeze.git
cd wpfreeze
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pytest
```

## Quickstart

If the current directory has a site config with an existing, resumable
run (a `manifest.json` already at its `output_dir`), `wpfreeze wizard`
offers to pick that back up before anything else — say no, or there's
nothing to resume, and it falls through to the questions below. With more
than one resumable config around, you get a numbered list to pick from
instead.

Otherwise, run `wpfreeze wizard` and answer the questions:

```
$ wpfreeze wizard
What should this project be called?: www-example-com
What site are we scraping? (base URL): https://www.example.com
Where should the output go? [./output/www-example-com]:
How nice are we being to the server?
  1) Gentle (2s between requests)
  2) Normal (1s between requests) [default]
  3) Aggressive (no delay between requests)
Choose [2]:
Use Wayback Machine recovery for missing pages? [Y/n]:
Prefer Wayback snapshots nearest to which date? (YYYY-MM-DD, blank = today):
Do you have a WordPress XML export (WXR) for this site? (wp-admin: Tools -> Export -> All content) [y/N]:
Save this config as [www-example-com.yaml]:
Wrote www-example-com.yaml
Run a dry-run now? (discovers URLs, fetches nothing) [Y/n]:
...
Run the real acquisition now? [y/N]:
```

The wizard writes a normal YAML config and offers to run a dry-run (finds
every URL the site claims to have, fetches nothing — good for sizing up a
site before committing) and then the real acquisition, right there in the
same session. The file it writes is completely ordinary afterward — the
normal way to run wpfreeze against it from here on is:

```bash
wpfreeze freeze www-example-com
```

which runs the config's declared `freeze.steps` (acquire, build, validate
by default) end to end, with a live progress display and a single
timing/paths summary at the end — see "What a freeze run looks like"
below. Each step is also available on its own, no wizard or `freeze`
involved:

```bash
wpfreeze acquire www-example-com --resume
wpfreeze report  www-example-com
wpfreeze status  www-example-com
```

(`--config www-example-com.yaml` still works too — see "Projects" below.)
Everything the wizard doesn't ask about (`exclusions`,
`extra_hosts`, `user_agent`) is left at sensible defaults — edit the YAML
directly if a site needs something different there. See
[`example-site.yaml`](example-site.yaml) for every option, annotated.

### A WordPress XML export makes this more complete

If you have (or can generate) a WXR file — wp-admin's **Tools → Export → All
content**, or `wp export` via WP-CLI — point `xml_backup:` at it in the
config, or say yes when the wizard asks. It's parsed as a third, independent
inventory source alongside the sitemap and REST API: content some plugins
hide from both of those (private post types, custom post types, orphaned
attachments) still gets caught, with no database access of any kind
required. It's entirely optional — most real acquisitions won't have one,
and omitting it is completely normal, not a degraded mode.

## Running wpfreeze with no arguments

`wpfreeze` with no subcommand is a launcher for whatever's relevant in the
current directory right now — it is not the setup wizard itself (that's
the explicit `wpfreeze wizard`, above).

**In a real terminal**, it's an interactive picker: every `*.yaml`/`*.yml`
config in the directory, numbered, plus a trailing "Starting a new
site..." entry. Move with the arrow keys or press a row's number; Enter on
a config expands it in place to show its current status ("Not yet
acquired.", "Acquired: 519 fetched, 0 pending...", etc.) and the commands
relevant to that state — each one itself a numbered, selectable row.
Enter on a command runs it directly, with the right project name already
filled in. Esc/Left collapses a config back down; `q` quits without
running anything. Selecting "Starting a new site..." launches `wizard`
the same as typing it.

**Anywhere stdin/stdout isn't a real terminal** — piped output, CI, a
script capturing `wpfreeze`'s output — the exact same information prints
as plain, non-interactive text instead, in the same order the picker
would show it:

```
$ wpfreeze
wpfreeze -- static-archive WordPress sites

Found 2 site configs in this directory:

  landscapes  (https://site-c.example, landscapes.yaml)
    Acquired: 519 fetched, 0 pending, 519 total. Built: yes.
    wpfreeze status    landscapes
    wpfreeze build     landscapes   (safe to re-run any time)
    wpfreeze validate  landscapes

  new-site  (https://example.org, new-site.yaml)
    Not yet acquired.
    wpfreeze acquire new-site --dry-run
    wpfreeze acquire new-site

Starting a new site, or fixing a config that isn't loading? Run `wpfreeze wizard`
for a guided walkthrough, or see SETUP.md.
```

Neither mode reads stdin or runs anything unasked — the plain-text form
never does, and the picker only acts once a command row is actually
selected and launched.

## What a freeze run looks like

`wpfreeze freeze <project>` runs the whole sequence a config declares in
its `freeze.steps` key (default: `acquire`, `build`, `validate`), one
progress display per step, ending in a single combined summary:

```
$ wpfreeze freeze landscapes
⠹ Acquiring site-c.example   pages 412/1163  assets 2204  ▸ /about/staff/   3m12s
...
⠼ Validating site-c.example   (vnu, no progress available)   0m48s
Check external links now? (hits third-party hosts, can be slow) [y/N]

landscapes — done in 41m02s
  acquire   38m14s
  build      2m31s
  validate     17s

  Built site   output/landscapes/site/
  Report       output/landscapes/report.html
  Checklist    output/landscapes/cleanup-todo.html
  Logs         output/landscapes/logs/
```

The progress line only ever appears in a real terminal — piped output,
backgrounded, or CI still get the plain log lines they always have. A step
that returns exit `2` (refused/failed) stops the sequence there and marks
itself in the wrap-up; a step returning `1` (completed with gaps) doesn't
stop it but does make `freeze`'s own final exit code `1`. `checklinks`
stays out of the default sequence (it's slow and hits hosts wpfreeze
doesn't control) but is offered once the declared steps finish — default
answer is No, and a broken link found this way doesn't change `freeze`'s
exit code, the same way `validate`'s findings don't. Every other
subcommand (`acquire`, `build`, `validate`, `search-index`, `checklinks`)
shows this same progress line and prints its own brief wrap-up when run on
its own too.

Declare a different sequence, or add `checklinks`/`upload-script` to it,
in the config:

```yaml
freeze:
  steps: [acquire, build, validate, checklinks]
```

Permitted steps: `acquire`, `build`, `validate`, `diagnose`, `report`,
`checklinks`, `search-index`, `upload-script`. `freeze` never infers a step
from another config key — `upload-script` only runs if you list it, even
if `upload.remote` is set.

## CLI reference

Every subcommand below takes the project either way: a name (`landscapes`)
or `--config site.yaml`, never both. See "Projects" below for what a name
resolves against.

```
wpfreeze                                # no subcommand: interactive picker (plain summary if not a real terminal)
wpfreeze wizard                         # guided setup: resume an existing run, or build a new site config
wpfreeze freeze         <project>
wpfreeze acquire       <project> [--resume] [--dry-run]
wpfreeze build         <project> [--site-dir DIR] [--no-verify] [--no-todo]
wpfreeze validate      <project> [--site-dir DIR] [--no-todo]
wpfreeze diagnose      <project>
wpfreeze report        <project> [--html-only | --json-only]
wpfreeze status        <project>
wpfreeze rescan        <project> [--apply] [--profile-from-config]
wpfreeze upload-script <project> [--site-dir DIR]
wpfreeze search-index  <project> [--site-dir DIR]
wpfreeze checklinks    <project> [--site-dir DIR] [--recheck]
```

- **`wizard`** is the guided setup flow described in "Quickstart" above —
  resume an existing run if one's found, otherwise build a new site config
  by answering a few questions. Explicit and interactive only; bare
  `wpfreeze` (no subcommand) is the picker/summary above, not this.
- **`freeze`** runs a project's whole declared step sequence in one go —
  see "What a freeze run looks like" above. On an unknown project name (for
  `freeze` or any other subcommand here), offers to launch the wizard
  pre-seeded with that name, in a real terminal only; otherwise lists the
  known project names and exits `2`.
- **`acquire`** runs (or resumes) the full pipeline: inventory discovery,
  crawl-to-fixpoint, Wayback recovery, analysis, and report generation.
  Refuses to start over an existing `manifest.json` unless `--resume` is
  passed — that guard exists so a second `acquire` on the same output
  directory can never silently clobber a prior run's progress.
- **`--dry-run`** does inventory discovery and nothing else: no fetching,
  just a report of every URL the site claims to have. Good for sizing up a
  site before committing to a real run.
- **`build`** rewrites the capture into a servable static site under
  `<output_dir>/site` (or `--site-dir`). Network-free and non-destructive:
  it reads `raw/` and `manifest.json` and writes a separate tree, so it is
  safe to re-run. It self-verifies afterward — every local reference is
  resolved against a file on disk — unless `--no-verify` is passed. Also
  (re)generates the cleanup checklist (see "The cleanup checklist" below)
  unless `--no-todo` is passed, and, when `search.enabled` is set in the
  config, builds the offline search index (see "Offline search" below).
- **`validate`** checks the built site's HTML/CSS with the [Nu Html
  Checker](https://validator.github.io/validator/) and writes
  `vnu-report.json`. Informational only — these are defects in the
  original site's own theme/content, not something `build` caused or can
  fix, so it never fails the build over someone else's markup. Also
  regenerates the cleanup checklist unless `--no-todo` is passed.
- **`diagnose`** writes `diagnostics.json` — a compact capture-integrity
  summary (duplicate local paths, disk/manifest hash mismatches,
  content-type/URL-shape collisions) from the existing manifest, no
  network involved.
- **`report`** regenerates `report.html`/`report.json` from the existing
  manifest without touching the network at all.
- **`status`** prints a one-screen summary: counts by status, how much is
  still pending, whether gaps are present.
- **`rescan`** re-parses already-stored `raw/` bytes with today's
  extraction code and queues anything newly discovered as pending —
  closes the gap where a link-extraction fix can't reach content acquired
  before the fix existed, without a full re-crawl. Reports what it would
  queue by default; `--apply` actually writes it to `manifest.json`.
- **`upload-script`** writes `upload.sh`, one script covering staging,
  local-preview, and production — see "Previewing a build somewhere"
  below. Requires a built `site/`; writes the script regardless of whether
  `upload.remote`/`upload.prod_remote` are set (its `--local` mode needs
  neither), but exits non-zero with no `site/` to reference.
- **`search-index`** (re-)builds the Pagefind search index over an
  already-built site. `build` already does this automatically whenever
  `search.enabled` is set — this command exists for re-indexing after an
  `ignore_selectors`/`force_language` change without a full rebuild (it
  only re-runs the indexer, not the markup tagging step, so a
  `body_selectors`/`exclude_pages` change needs `build` to actually take
  effect). Requires both a built `site/` and `search.enabled: true` in the
  config; see "Offline search" below.
- **`checklinks`** scans the built site for external links (`<a href>`
  targets on another host) and checks whether each one still resolves,
  writing `broken-external-links.md`/`.html` grouped by the internal page
  each broken link was found on. Also (re)writes `external-links.json`,
  the page → external-URL list it just found — `--recheck` re-verifies
  that persisted list's liveness without re-scanning the built site (or
  even needing `site/` to still exist), so link rot can be checked again
  later without a rebuild. See "Checking external links" below.

**Exit codes**: `0` = complete, `1` = complete with gaps (see `report.html`'s
"Action required" section — expected content that couldn't be recovered
live or via Wayback), `2` = error or refused to run. For `build`, `1` means
the site was written but some references could not be resolved to a local
file (left pointing at the original site) or a local link is broken; the
build summary and verification report say which. For `freeze`, `2` means
some step in the sequence returned `2` (the sequence stopped there); `1`
means every step ran but at least one returned `1`. `Ctrl-C` prints a short
message and exits `130` instead of a raw traceback — the manifest is saved
incrementally during a crawl, so `--resume` can usually pick back up rather
than starting over.

## Projects

A "project" is just a site config, addressed by a short name instead of
its file path. `name:` in the YAML sets it explicitly; omit the key
entirely and it falls back to the config filename's stem (`landscapes.yaml`
→ `landscapes`) — no migration needed for a config that predates this.

Resolution, when you pass `wpfreeze <command> <project>`: first, does the
token look like a config path at all (ends in `.yaml`/`.yml`, contains a
path separator, or names a real file) — if so it's loaded directly, the
same as `--config` always has been. Otherwise it's matched case-
insensitively against every config's `name:` in the current directory,
then against every config's filename stem. Two configs claiming the same
name (or one's explicit name colliding with another's filename stem) is an
error naming both files, not a silent first match.

An unknown project name, in a real terminal, offers to start one instead
of just refusing — the same offer regardless of which subcommand you
typed, since `wpfreeze build newsite` for a project that doesn't exist yet
is exactly as plausible a first move as `wpfreeze freeze newsite`:

```
$ wpfreeze build newsite
There is no project called newsite. Would you like to start one? [Y/n]
```

Saying yes launches the wizard pre-seeded with that name (skipping the
"resume an existing run?" offer, which would be a non-sequitur for a name
that doesn't exist yet) and, at the end, offers to run the full freeze
right away in place of the usual separate acquire question. Saying no, or
running unattended (piped output, CI, a script), prints the list of what
*is* here and exits `2` without ever prompting:

```
$ wpfreeze build nope
There is no project called nope.
Projects in this directory: site-a, landscapes, site-b
```

## What you get

Inside `output_dir`:

```
raw/               fetched files, byte-for-byte as retrieved
manifest.json      the spine -- one record per canonical resource
report.html        single self-contained static report (open it in a browser)
report.json        the manifest plus summary statistics
logs/              one file per run, full DEBUG-level detail
site/              servable static site (only after `wpfreeze build`)
site/pagefind/     search index -- only when `search: enabled: true` (see "Offline search")
site/assets/pagefind-search.js  the search-wiring script -- same condition
build-report.json  build's own stats -- unresolved refs, verification, what policy stripped
vnu-report.json    HTML/CSS issues from `wpfreeze validate`
diagnostics.json   capture-integrity summary from `wpfreeze diagnose`
cleanup-todo.md    human-readable punch list synthesized from the three reports above
cleanup-todo.html  the same, styled like report.html, with clickable links -- see below
upload.sh          only after `wpfreeze upload-script` -- see "Previewing a build somewhere"
preview/           only after `upload.sh --local` -- site + reports, assembled for local browsing
external-links.json       only after `wpfreeze checklinks` -- the page -> external-URL list it found
broken-external-links.md  only after `wpfreeze checklinks` -- broken links, grouped by internal page
broken-external-links.html  the same, styled like report.html
```

`report.html` and `cleanup-todo.html` have no external dependencies — no
CDN scripts, no fonts, no tracking — each is one file you can hand to
anyone.

## The cleanup checklist

`build-report.json`/`vnu-report.json`/`diagnostics.json` are machine
reports; `cleanup-todo.md`/`.html` turn them into a short, human-readable
punch list of what's actually worth a site owner's attention, regenerated
automatically whenever `build` or `validate` runs (skip with `--no-todo`).
It covers only what becomes available *after* those commands run —
`report.html` still owns the acquisition-side gap breakdown (missing
pages, auth-gated content, orphaned URLs).

Sections, in order:

- **Capture integrity** — duplicate local paths, disk/manifest hash
  mismatches, and similar anomalies from `diagnose`, flagged first since
  they usually mean two things silently collided during acquisition.
- **Broken references** — reference the build couldn't resolve to a local
  file, left pointing at the original site. Each links two ways: the
  broken target to the still-live original (worth comparing while it's up),
  and the referring page to its local built copy.
- **Local link verification** — links that *were* rewritten to a local
  path that doesn't exist on disk (broken within the archive itself,
  distinct from the above).
- **Content intentionally excised** — what `build`'s content policy
  removed (see "Building a servable site" below), broken out by category
  (comment, password, search, newsletter, other). Forms specifically get a
  per-page list, since a removed `<form>` can leave a heading or button
  behind describing nothing; comment, password, and newsletter forms are
  called out separately and deprioritized — a comment or Divi newsletter
  form's caption is removed automatically (see below), and a password
  form's removal empties a page that had nothing else in it to begin with
  (WordPress's own password-protected-post prompt — wpfreeze never had
  access to what's actually behind the password) — so none of the three
  needs a manual check the way everything else in this section does.
- **Search forms left in place** — only appears when `strip_search_forms:
  false` is set. A per-page list of search forms that were deliberately
  *not* removed, for a site owner reimplementing search rather than
  losing it — distinct from the section above since nothing was actually
  excised here.
- **Site markup/content quirks** — pre-existing HTML/CSS defects in the
  original theme/content that `validate` catches. Not something `build`
  introduced or fixes automatically — the point of a static archive is
  that these files are now yours to hand-edit directly.

Every list in the document is complete — nothing is ever truncated —
and any list longer than 10 items collapses behind a `<details>` in the
HTML version so a 500-item list doesn't dominate the page while still
being one click away.

## Building a servable site

`wpfreeze build` turns the raw capture into a static site you can host on any
plain web server or browse straight off disk:

```bash
wpfreeze build site
```

It reads `raw/` and `manifest.json` and writes `<output_dir>/site` without
touching the capture, so re-running is always safe. In that tree:

- Internal links, `src`/`srcset`, CSS `url()`, and inline styles are
  rewritten to **relative** local paths, so the site works whether it's
  served from a domain root, hosted under a subdirectory, or opened as a
  `file://` path.
- Cache-buster query strings (`?ver=`, `?m=`, `?cssminify=`) are matched to
  the single canonical file the capture holds, rather than left dangling.
- WordPress.com `/_static/??…` concat bundles are reassembled into local
  CSS/JS files under `assets/bundles/`.
- Links to WordPress attachment pages are redirected to the media they wrap.
- The acquisition-generated redirect map is copied to `site/.htaccess`.
- **Live-web machinery is stripped** so the archive is genuinely
  self-contained: analytics and tag managers (third-party *and* the
  self-hosted analytics plugins WordPress serves from its own domain),
  `<form>` elements (dead or leaky on a static site), and dead RSS/Atom feed
  links. All on by default and configurable per site — see the `policy:`
  block in [`example-site.yaml`](example-site.yaml). Counts of what was
  removed appear in the build summary.
- **A WordPress core comment form's caption goes with it, not just the
  form.** Detected by its `id="respond"`/`class="comment-respond"`
  wrapper — WordPress's own hardcoded markup, not something a theme's
  caption text can hide from — so a themed "Submit a Comment" is caught
  exactly like the default "Leave a Reply". Any in-page link left pointing
  at that removed id (WordPress's own "N comments" post-meta link) is
  fixed too; the "N comments" text itself is removed by default, or kept
  for a nonzero count when `strip_comment_counts: false` — see
  `example-site.yaml`.
- **WordPress's password-protected-post prompt is detected and removed
  like any other form** — via core's own fixed `class="post-password-form"`
  (baked into `get_the_password_form()`, theme-independent), and reported
  in its own "Password-protected pages" checklist category rather than
  lumped in with generic form removals or (if the page ends up empty, as
  it always does — the prompt was the page's entire content) misreported
  as thin content: wpfreeze never captured what's actually behind the
  password, so there's genuinely nothing to check.
- **Search forms are recognized and removed like any other form by
  default** — detected by `role="search"` or a `name="s"` input, both
  WordPress's own conventions regardless of theme (unlike class names:
  Divi's own search widget uses none of the ones WordPress core's default
  template does). Set `strip_search_forms: false` to leave them
  completely untouched instead, for a site owner planning to wire up a
  replacement (a static index, a hosted search service) rather than just
  lose search entirely — this doesn't make the form functional as-is,
  just raw material to repurpose; see `example-site.yaml` for the exact
  caveat. Pages with one left this way are listed in the cleanup
  checklist's own "Search forms left in place" section.
- **Divi's newsletter/subscribe module goes with its caption, not just
  the form.** Detected by the module's own hardcoded `et_pb_newsletter`
  class (Divi's markup, not something a provider — Mailchimp, Constant
  Contact, ... — or theme skin changes), so the whole module is removed
  rather than leaving an empty box behind. When Divi's own "no title, no
  description" classes confirm the module has no caption of its own, the
  preceding text module standing in as one is removed too — but only when
  that sibling is a bare heading with nothing else in it, so a real
  content section next to an unrelated newsletter module is never
  mistaken for its caption.
- **Third-party iframe embeds (YouTube, Vimeo, Google Maps, social
  embeds) are left pointing at the live original**, not downloaded — an
  iframe embeds a whole foreign application that depends on live JS/API
  calls to function, so a static snapshot of it would just be a dead
  imitation. A same-site or same-multisite-network iframe embed is still
  captured and localized normally.

References the capture never got are left pointing at the original site —
honestly broken rather than silently dead — and counted in the summary. By
default `build` then verifies its own output, resolving every local
reference against a real file on disk and reporting any that are broken —
only files this run itself wrote, so a file you've placed in `site/`
yourself (to view alongside the build through your own local server, say)
is never scanned as if it were part of the build.

**Not rewritten**: URLs that only exist inside executed JavaScript (a
gallery that assembles image paths at runtime, a JS-only nav). Discovery and
rewriting are markup-based by design — there is no headless browser — so
those keep pointing at the live site.

**Telemetry stripping is coverage-based**, and coverage has two edges. A
tracker loaded from a host the blocklist doesn't know, or inline tracking
code matching none of the known signatures, will survive — widen coverage
per site with `telemetry_extra_hosts`. And a `<script>` that matches a
tracking signature is removed whole; if a site ever fused a tracking call
into a script that also did real work, that script goes too. This isn't seen
on real WordPress (tracking is injected as its own dedicated blocks), but
it's the deliberate cost of stripping everything rather than editing script
internals.

## Previewing a build somewhere

`wpfreeze upload-script` writes `upload.sh` into `output_dir` — a small,
hand-editable script covering three ways to get a build somewhere it can
be looked at, one script, one mode per invocation:

```bash
wpfreeze upload-script site
cd <output_dir>
./upload.sh           # staging: site + reports, to upload.remote
./upload.sh --local   # local preview: site + reports, under ./preview/, no network
./upload.sh --prod    # production: site only, to upload.prod_remote, after a y/N confirmation
```

Both the `wpfreeze upload-script` step and actually running `upload.sh`
(in whichever mode) are always explicit — `build` never generates or runs
any of this itself just because a config has a remote set.

**Staging (no flag)** and **`--local`** both bundle `site/`'s contents
with the reports a site owner would want to read alongside it —
`cleanup-todo.html`, `report.html`, and `broken-external-links.html` when
present (not the `.md`/`.json` versions) — so post-build, pre-production
review can happen either on a shared staging host or straight off disk
(`cd preview && python3 -m http.server`), same bundle either way. Staging
needs `upload.remote` set in the config; `--local` needs nothing set at
all.

**`--prod`** is different on purpose: it syncs `site/` *only* — no
reports, they're for review, never for the live site — to
`upload.prod_remote`, and pauses for a `y/N` confirmation first, since
it's the one mode meant to touch a real, live production host. Both
`--prod` and the staging mode run `rsync -av --delete`, so anything
already at that destination that isn't part of this build gets
**deleted**, not just overwritten — worth being sure of what's there
before confirming. Neither remote is required to write the script itself
— an unconfigured mode just errors clearly, naming which config key to
add, when you actually try to run it.

See [`example-site.yaml`](example-site.yaml) for both `upload.remote` and
`upload.prod_remote`.

## Offline search

A static archive's WordPress search form is dead by default — its
`action` gets rewritten like any other reference and just navigates to
the local homepage with an ignored `?s=` query string. Setting
`search.enabled: true` makes `wpfreeze build` give it back a real,
fully client-side search, powered by [Pagefind](https://pagefind.app):
the theme's *own* existing search form is wired up as-is, with no new
visible UI until someone actually searches. It needs the form to survive
the build in the first place, so `policy.strip_search_forms: false` (or
`policy.strip_forms: false`) has to be set too — `wpfreeze` refuses to
start with a `ConfigError` if you enable search without it, rather than
silently building a site with no search box to wire up:

```yaml
policy:
  strip_search_forms: false
search:
  enabled: true
```

`build` then does two things: writes `site/assets/pagefind-search.js`
(the wiring script) and a `<script>` tag on every page, and runs
Pagefind's indexer over the finished site, writing `site/pagefind/`. Both
also happen automatically every time you re-run `build`; `wpfreeze
search-index` re-runs just the indexing step, for tuning selectors
without a full rebuild.

**If the capture turns out to have no search form at all** (rare — most
sites that reach this point have one, since the `ConfigError` above
already means one existed to keep from stripping), `build` warns rather
than silently indexing a site nobody can search from:

```
Search is enabled, but this capture has no search form.
  wpfreeze tags the site's own search form (role="search", or an
  input named "s") and wires it to the index. None of the 1163 pages
  built has one, so the index would be built with nothing to reach it.
  The pages would still be indexed -- there would just be no search box.
```

In a real terminal it also asks `Build search anyway? [y/N]` — default No
skips the (otherwise wasted) Pagefind subprocess for this run and prints
the exact one-line edit to make that permanent
(`search: enabled: false`). An unattended run (piped, CI, backgrounded)
proceeds exactly as before, warning only. If you know the site has no
search form and don't want to be asked again while keeping search enabled
(say, you're planning to add a search box to the template by hand), set
`search.acknowledged_no_forms: true` — suppresses the warning and prompt
entirely, indexing proceeds as if a form existed.

**`search.body_selectors`** — a list of CSS selectors marking where a
page's real content lives — does two things at once, and the second is
easy to miss: it narrows *what* gets indexed on a matching page, but it
also means **any page matching none of the selectors is dropped from the
index entirely** (that's Pagefind's own behaviour, not something wpfreeze
adds). That's a feature, not a bug — it is the only way to keep
category/tag archives and attachment pages out of search results — but a
too-narrow selector, or one that a subset of templates don't use, silently
empties part of the index. The build reports the miss count, and the
cleanup checklist lists every affected page under "Pages not covered by
search", so the failure is visible rather than a mystery.

Omitting `body_selectors` from the config entirely defaults to
`["body.wp-singular .entry-content"]` — WordPress core's own
`body_class()`/`the_content()` conventions, which most non-page-builder
themes follow. This was found necessary against a real site, not assumed:
indexing the whole `<body>` let WordPress's own archive/category/blog-
listing templates (which re-embed each post's `.entry-content` as a
teaser) compete with the real page in results — a search could return the
same post 4-5 times over under different archive-page titles.
`body.wp-singular` excludes those listing pages outright (it's WP core's
own singular-vs-archive body class); `.entry-content` narrows further and
also excludes WordPress's own comment thread. A page-builder theme that
never emits `.entry-content` still fails safely — those pages just show up
in "Pages not covered by search" rather than silently mismatching. Set
`body_selectors: []` explicitly to opt back into the old whole-`<body>`
behaviour, or your own selector(s) to override the default outright.

**`search.ignore_selectors`** excludes elements from indexing even inside
otherwise-indexed content (a repeated "related posts" widget, say).
**`search.force_language`** collapses Pagefind's per-`<html lang>` index
split into one index — needed if the theme is inconsistent about
emitting `lang`, which otherwise silently returns no results from
whichever pages fell into the wrong index.

**`search.exclude_pages`** drops specific pages from the index outright,
by exact output path (e.g. `["/blog.html"]`, not a URL and not a glob) —
for the case `body_selectors` structurally can't handle: a hand-built
page-builder "archive" or "blog" page that is otherwise indistinguishable
by selector from a real content page, so it stays indexed and echoes
other pages' content into search results (see the two checks below,
which will name exactly this kind of page). No load-time validation, same
as `body_selectors`/`ignore_selectors` — a path that matches nothing is a
silent no-op, which is self-correcting because the page keeps showing up
in the report until the path is right.

**`search.acknowledged_thin_pages`** marks specific pages (same exact-
output-path format as `exclude_pages`) as reviewed and confirmed
legitimately short, rather than broken or truncated — for content that's
supposed to be brief, like a single-image portfolio item. Unlike
`exclude_pages`, an acknowledged page is **not** removed from the index:
it stays fully searchable, only the cleanup checklist's "needs a look"
push for it is suppressed. It's still listed every build under its own
"Acknowledged" heading in the "Pages with thin content" section — nothing
here is ever silently dropped from the report, only deprioritized.

**Two more checks run automatically alongside indexing**, reported the
same way as the "not covered by search" gap above — informational only,
never changing what gets indexed:

- **Thin content** — a page matched a selector but holds almost no text
  once indexed (under 25 words), so it's present in search but adds noise
  rather than findable content. A blind spot the coverage check above
  can't see, since the selector *did* match. If a flagged page is
  genuinely short by design (a single-image portfolio item, a glossary
  entry) rather than broken, add its exact output path to `search.
  acknowledged_thin_pages` — unlike `exclude_pages`, it stays indexed and
  searchable; the checklist just stops pushing "needs a look" for it
  specifically. Still listed every build, under its own "Acknowledged"
  heading, so it's never silently hidden.
- **Echoed content** — a page's indexed text is mostly shared with other
  pages, almost always because it's an archive/category/blog-listing page
  (WordPress-native or hand-built) whose content is assembled from other
  pages' teasers. Left indexed, one real piece of content competes with
  itself in results. The checklist names the page, its likely sources, and
  a sample of the shared text; if `body_selectors`/`ignore_selectors` can't
  exclude it (the hand-built-listing-page case), add it to
  `search.exclude_pages`.

`wpfreeze search-index` re-runs both checks too (they only need the
already-built markup, same as re-indexing itself), but reports them to
the console only — the cleanup checklist is a `build` artefact and isn't
regenerated by `search-index`.

**Resolving a flagged "echoed content" page.** Most flags are legitimate
site structure — a hub/listing page, or a page intentionally summarizing
others — not a bug worth fixing. Don't exclude everything the checklist
names; read each one:

1. Open `cleanup-todo.html`'s "Pages that mostly duplicate other pages"
   section, not just the `Search:` summary's count — it names the page,
   its likely source(s), and a sample of the shared text.
2. Check whether the page is linked from anywhere else in the built
   site: `grep -rl 'href="[^"]*that-page.html"' site/ --include="*.html"`.
   One that nothing else links to is reachable only by guessing the
   URL — a strong sign of a stray draft, not real content.
3. Compare its content against the source page(s) the report named.
   Near-identical text, with a different WordPress page-ID if that's
   visible in the markup, confirms a genuine duplicate rather than a
   coincidence.
4. If it's a genuine orphaned duplicate, add its exact output path — the
   same string the checklist already printed — to `search.exclude_pages`,
   then run `wpfreeze build` again. `search-index` alone won't pick this
   up: `exclude_pages` changes the markup-tagging step, not just the
   indexer, and only a full `build` re-tags markup.
5. Confirm: the `Search:` summary's new `excluded by config` count, and
   a live search for a term that used to surface the duplicate.

**Resolving a flagged "thin content" page.** Same principle — read before
acting, don't blanket-acknowledge every page the checklist names:

1. Open the page and confirm it's genuinely, deliberately short (a
   single-image portfolio item, a one-line glossary entry) rather than
   broken — a `body_selectors` match that grabbed the wrong element, or
   real content a plugin hid from extraction, looks thin for a different
   reason and is worth fixing at the source instead.
2. Add its exact output path to `search.acknowledged_thin_pages`, then
   run `wpfreeze search-index site` — unlike
   `exclude_pages`, this doesn't touch markup tagging, so re-indexing
   alone is enough to see it reflected in the console's `thin content`
   line and its `(N acknowledged)` count. The cleanup checklist itself is
   a `build` artefact, though (`search-index` never refreshes it) — run
   `wpfreeze build` too before checking `cleanup-todo.html`.
3. Confirm: the page still appears in "Pages with thin content", now
   under "Acknowledged" instead of the unacknowledged list above it, and
   no longer pushes the checklist's "worth a look" headline on its own.

Indexing needs the `pagefind` Python package, which is *not* installed by
a plain `wpfreeze` install (see "Installation" above for the `pipx
inject` command) — `build`/`search-index` fail with an actionable message
naming the install command if it's missing, not a bare traceback.

**Search does not work over `file://`.** The rest of a built site opens
fine straight from a file manager; search specifically does not, because
both the module import and the index's own `fetch()` calls are blocked
under `file://`. It needs to be served over real HTTP — even the
simplest local server is enough:

```bash
cd <output_dir>/site && python3 -m http.server
```

If a search box looks broken while you're browsing the site locally by
double-clicking `index.html`, this is almost always why.

## Checking external links

`wpfreeze build` only verifies *local* references — a link to another
site is left alone either way, and wpfreeze has no idea whether it's still
live. `wpfreeze checklinks site` finds those out: it scans
the built site for every `<a href>` pointing at another host, checks each
one, and writes `broken-external-links.md`/`.html`, listing only the
internal pages that link to something now broken (a page with no dead
external links doesn't appear at all).

It's two phases, run together by default:

1. **Scan** the built site for external links, grouped by unique target
   URL with every page that links to it, and save that list to
   `external-links.json`.
2. **Check** each URL in that list and write the report.

`--recheck` runs only the second phase, against whatever
`external-links.json` already has on disk — no re-scan, and the built
`site/` doesn't even need to still be present. Link rot accrues after the
archive is made, not just at build time, so this is how you check again
weeks or months later without re-running `build`:

```bash
wpfreeze checklinks site            # scan + check
wpfreeze checklinks site --recheck  # check again later, no rescan needed
```

Checking uses the same per-host politeness/backoff as `acquire` (see
`rate_limit`/`concurrency` in the config) — these are hosts wpfreeze
doesn't own, so an impolite link-checker is exactly the failure mode that
backoff logic exists to avoid. A `401`/`403` is reported as "auth-gated",
not flatly "broken" — that status can mean a real login wall rather than
a dead page, so it's worth a human glance rather than an automatic verdict.

Exit code `1` means at least one broken link was found; `0` means none
were (including "no external links at all").

## Behaviour worth knowing about

- **Rate limiting is per-host, global across all workers**, not multiplied
  by `concurrency` — two workers hitting the same host still only issue one
  request per `rate_limit` interval between them. `concurrency` exists so
  `rate_limit` has something to parallelize across (an extra CDN host, the
  Wayback Machine running alongside the live site), not to go faster
  against one host.
- **"Aggressive" means zero delay against the live site**, on the theory
  that you're the site owner archiving your own content and politeness
  toward yourself isn't the concern. If the site runs an aggressive
  security/firewall plugin, this can trip its own lockout defenses — see
  the next point.
- **Automatic lockout backoff.** A run of 5 consecutive 401/403 responses
  from the same host is treated as a likely site-side lockout (a security
  plugin banning your IP after too many rapid requests) rather than a
  handful of individually private pages, and that whole host gets backed
  off for an escalating cooldown (60s, doubling on repeat, capped at 30
  minutes) — not just the one request that tripped it, since concurrent
  workers would otherwise keep hammering it regardless. This is always
  logged at `WARNING` level to the console when it happens, so a run that
  suddenly slows down tells you why instead of leaving you guessing.
- **The Wayback Machine is always throttled independently and more
  conservatively** than the live site, regardless of how aggressive you set
  the main `rate_limit` — it's a separate, shared, third-party service that
  bans impolite clients no matter how fast you're allowed to go against
  your own site.
- **No headless browser.** Link discovery is attribute/markup-based (every
  `src`/`href`/`srcset`, `data-*` attributes, inline styles, CSS `url()`,
  and a conservative regex scan of `<script>` blocks for gallery/slider
  plugin JSON). If a site has a JavaScript-only rendering path that never
  appears anywhere in markup or scripts, it won't be found. This is
  documented in the report's known-limitations, not silently swallowed.

## Known limitations

- Linux only, by design.
- No headless browser / JS execution — so URLs built at runtime in
  JavaScript are neither captured nor rewritten (see "Building a servable
  site").
- No scheduling or recurring collection — each `acquire` is a single
  (resumable) run against one site config, not a cron-style repeating job.
- Offline search (`search.enabled`) does not work when the built site is
  opened via `file://` — it needs to be served over real HTTP (see
  "Offline search").

## License

[GNU General Public License v3.0 or later](LICENSE).
