# Extra config options

`wpfreeze wizard` writes a minimal config to get a site archived. Everything
below is optional -- add any of it to the YAML file the wizard wrote, by
hand, whenever you need it. For the complete, exhaustively annotated
reference of every key, see [`example-site.yaml`](example-site.yaml); this
is just the highlights, picked for what people tend to want right after
their first run.

## Tuning offline search

If you turned on `search.enabled` in the wizard (or add `search: {enabled:
true}` here later -- that one key is all it takes; `load_config` leaves the
site's search form unstripped for you automatically), these narrow what
actually gets indexed:

```yaml
search:
  enabled: true
  exclude_pages: ["/category/news.html"]
  acknowledged_thin_pages: ["/team/jane-doe.html"]
```

- `exclude_pages` drops specific pages from the index entirely (exact output
  paths, not URLs or globs).
- `acknowledged_thin_pages` marks a page that's short on purpose (a
  single-image portfolio item, a glossary entry) as reviewed and fine --
  it stays indexed and searchable, it just stops nagging the cleanup
  checklist.

## Widening the crawl to another host

A CDN, media subdomain, or second domain the site also serves content from:

```yaml
extra_hosts:
  - cdn.example.com
```

## Skipping more URLs

Extra regex patterns to exclude during crawl, beyond the WordPress-
infrastructure defaults the wizard already wrote:

```yaml
exclusions:
  - /wp-content/uploads/2019/
```

## Deploying a preview or production copy

```yaml
upload:
  remote: user@staging.example.com:/var/www/preview
  prod_remote: user@prod.example.com:/var/www/html
```

Powers `wpfreeze upload-script`, which writes `upload.sh`: the no-flag
default syncs the built site plus the human-facing reports to `remote`;
`--prod` syncs the site only, to `prod_remote`, after a confirmation prompt.

## What `freeze` runs

```yaml
freeze:
  steps: [acquire, build, validate, checklinks]
  unattended: true
```

`steps` defaults to `acquire, build, validate`. Add `checklinks` to also
check external links every run, or `upload-script` to regenerate
`upload.sh`.

`unattended` (default `false`) makes `freeze` behave in a real terminal
exactly as it already does when its output is piped: it never asks a
question. Both prompts go — the confirmation before resuming an existing
acquisition (it resumes), and the end-of-run offer to check external links
(it does not run, unless you listed `checklinks` above). For a genuinely
hands-off full run, set `unattended: true` **and** list `checklinks` in
`steps`. Leave it off if you are watching the run: being asked is the
point of the prompts.

## Crawl concurrency

```yaml
concurrency: 4
```

Number of worker threads fetching pages at once (default 2). Separate from
the wizard's politeness preset (`rate_limit`), which controls the delay
between each thread's requests, not how many threads there are.

## Pinning the HTML validator

```yaml
vnu_jar: /home/you/.cache/wpfreeze/vnu-20250101.jar
```

Leave it unset to auto-download and cache the latest `vnu.jar` instead.
