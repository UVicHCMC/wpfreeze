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
- **Does not**: rewrite HTML to work as a standalone static site. Fetched
  files are saved byte-for-byte as retrieved; `manifest.json` records the
  *intended* output path for each resource, but nothing currently rewrites
  internal links to point at those paths. Turning the raw capture into a
  servable static site is a separate job this tool doesn't do (yet).

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

Run `wpfreeze` with no arguments and answer the questions:

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
wpfreeze acquire --config site.yaml [--resume] [--dry-run]
wpfreeze report  --config site.yaml [--html-only | --json-only]
wpfreeze status  --config site.yaml
```

- **`acquire`** runs (or resumes) the full pipeline: inventory discovery,
  crawl-to-fixpoint, Wayback recovery, analysis, and report generation.
  Refuses to start over an existing `manifest.json` unless `--resume` is
  passed — that guard exists so a second `acquire` on the same output
  directory can never silently clobber a prior run's progress.
- **`--dry-run`** does inventory discovery and nothing else: no fetching,
  just a report of every URL the site claims to have. Good for sizing up a
  site before committing to a real run.
- **`report`** regenerates `report.html`/`report.json` from the existing
  manifest without touching the network at all.
- **`status`** prints a one-screen summary: counts by status, how much is
  still pending, whether gaps are present.

**Exit codes**: `0` = complete, `1` = complete with gaps (see `report.html`'s
"Action required" section — expected content that couldn't be recovered
live or via Wayback), `2` = error or refused to run. `Ctrl-C` prints a short
message and exits `130` instead of a raw traceback — the manifest is saved
incrementally during a crawl, so `--resume` can usually pick back up rather
than starting over.

## What you get

Inside `output_dir`:

```
raw/            fetched files, byte-for-byte as retrieved
manifest.json   the spine -- one record per canonical resource
report.html     single self-contained static report (open it in a browser)
report.json     the manifest plus summary statistics
logs/           one file per run, full DEBUG-level detail
```

`report.html` has no external dependencies — no CDN scripts, no fonts, no
tracking — it's one file you can hand to anyone.

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
- No markup rewriting — see "What it does and doesn't do" above.
- No headless browser / JS execution.
- No scheduling or recurring collection — each `acquire` is a single
  (resumable) run against one site config, not a cron-style repeating job.

## License

[GNU General Public License v3.0 or later](LICENSE).
