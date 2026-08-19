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
run (a `manifest.json` already at its `output_dir`), `wpfreeze` offers to
pick that back up before anything else — say no, or there's nothing to
resume, and it falls through to the questions below. With more than one
resumable config around, you get a numbered list to pick from instead.

Otherwise, run `wpfreeze` with no arguments and answer the questions:

```
$ wpfreeze
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
same session. The file it writes is completely ordinary afterward:

```bash
wpfreeze acquire --config www-example-com.yaml --resume
wpfreeze report  --config www-example-com.yaml
wpfreeze status  --config www-example-com.yaml
```

no wizard involved. Everything the wizard doesn't ask about (`exclusions`,
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

## CLI reference

```
wpfreeze                                        # no subcommand: interactive wizard
wpfreeze acquire       --config site.yaml [--resume] [--dry-run]
wpfreeze build         --config site.yaml [--site-dir DIR] [--no-verify] [--no-todo]
wpfreeze validate      --config site.yaml [--site-dir DIR] [--no-todo]
wpfreeze diagnose      --config site.yaml
wpfreeze report        --config site.yaml [--html-only | --json-only]
wpfreeze status        --config site.yaml
wpfreeze rescan        --config site.yaml [--apply] [--profile-from-config]
wpfreeze upload-script --config site.yaml [--site-dir DIR]
```

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
  unless `--no-todo` is passed.
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
- **`upload-script`** writes `upload.sh` for pushing the built site
  somewhere a site owner can preview it — see "Previewing a build
  somewhere" below. Requires both a built `site/` and `upload.remote` in
  the config; writes nothing and exits non-zero without either.

**Exit codes**: `0` = complete, `1` = complete with gaps (see `report.html`'s
"Action required" section — expected content that couldn't be recovered
live or via Wayback), `2` = error or refused to run. For `build`, `1` means
the site was written but some references could not be resolved to a local
file (left pointing at the original site) or a local link is broken; the
build summary and verification report say which. `Ctrl-C` prints a short
message and exits `130` instead of a raw traceback — the manifest is saved
incrementally during a crawl, so `--resume` can usually pick back up rather
than starting over.

## What you get

Inside `output_dir`:

```
raw/               fetched files, byte-for-byte as retrieved
manifest.json      the spine -- one record per canonical resource
report.html        single self-contained static report (open it in a browser)
report.json        the manifest plus summary statistics
logs/              one file per run, full DEBUG-level detail
site/              servable static site (only after `wpfreeze build`)
build-report.json  build's own stats -- unresolved refs, verification, what policy stripped
vnu-report.json    HTML/CSS issues from `wpfreeze validate`
diagnostics.json   capture-integrity summary from `wpfreeze diagnose`
cleanup-todo.md    human-readable punch list synthesized from the three reports above
cleanup-todo.html  the same, styled like report.html, with clickable links -- see below
upload.sh          only after `wpfreeze upload-script` -- see "Previewing a build somewhere"
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
  removed (see "Building a servable site" below), broken out by category.
  Forms specifically get a per-page list, since a removed `<form>` can
  leave a heading or button behind describing nothing; comment forms are
  called out separately and deprioritized, since their caption is removed
  automatically (see below) and doesn't need a manual check the way
  everything else in this section does.
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
wpfreeze build --config site.yaml
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
hand-editable `rsync` script for pushing the built site somewhere a site
owner can look at it. It's for a staging/preview copy, not a production
deploy, and needs `upload.remote` set in the config (see
[`example-site.yaml`](example-site.yaml)):

```bash
wpfreeze upload-script --config site.yaml
cd <output_dir> && ./upload.sh
```

Both steps are always explicit. `build` never generates or runs this
script itself just because a config happens to have `upload.remote` set —
generating it, and separately, actually running it, are things you do on
purpose when you're ready. The script itself runs `rsync -av --delete`,
so anything already at the destination that isn't part of this build gets
**deleted**, not just overwritten — worth being sure of what's there
before running it. It copies `site/`'s contents plus `cleanup-todo.html`
and `report.html` (not the `.md`/`.json` versions).

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

## License

[GNU General Public License v3.0 or later](LICENSE).
