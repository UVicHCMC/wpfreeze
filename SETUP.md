# Getting started

The easy on-ramp, plus the fully manual path for when you want full control.

## `wpfreeze` — the wizard (recommended for a first run)

Run `wpfreeze` with no arguments and answer the questions:

```
$ wpfreeze
What site are we scraping? (base URL): https://www.example.com
Where should the output go? [./output/www-example-com]:
How nice are we being to the server?
  1) Gentle (2s between requests)
  2) Normal (1s between requests) [default]
  3) Aggressive (0.3s between requests)
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

It writes a normal site config YAML (the same shape documented in
`example-site.yaml`) and offers to dry-run and then run the real
acquisition immediately. The written file is ordinary afterward —
`wpfreeze acquire --config www-example-com.yaml --resume`,
`wpfreeze report`, and `wpfreeze status` all work on it with no wizard
involved. Exclusions, `extra_hosts`, and `user_agent` are left at their
shipped defaults; edit the YAML directly if a site needs something
different there.

If you answer yes to the WXR-export question, you'll be asked for the
path to the file; it's written into the config as an optional
`xml_backup:` key that augments the sitemap/REST API inventory the same
way a live database connection did in an earlier design, but as a plain
file requiring no local database setup at all.

## Fully manual path

Copy `example-site.yaml`, fill in the values, and run:

```
wpfreeze acquire --config your-site.yaml
```

See that file's comments for every option, and `CLAUDE-acquire.md` for the
full spec this tool implements.
