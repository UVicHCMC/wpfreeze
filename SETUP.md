# Getting started

The easy on-ramp, plus the fully manual path for when you want full control.

## `wpfreeze wizard` (recommended for a first run)

If the current directory has a site config YAML with an existing,
resumable run (a `manifest.json` already sitting at its `output_dir`),
`wpfreeze wizard` offers to pick that back up first:

```
$ wpfreeze wizard
Found an existing run: www-example-com.yaml (https://www.example.com/) -- 5747 fetched, 76 pending/retrying, 5846 total. Resume it? [Y/n]
```

Say yes and it resumes immediately, no further questions asked. Say no
(or there's nothing to resume) and it falls through to the ordinary
question flow. With more than one resumable config in the directory,
you get a numbered list to choose from instead, plus a "none of these"
option.

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
Enable offline search? (adds a Pagefind-powered search box to the archived site) [y/N]:
Save this config as [www-example-com.yaml]:
Wrote www-example-com.yaml
See EXTRA-CONFIG-OPTIONS.md for other options you can add to it by hand.
Run a dry-run now? (discovers URLs, fetches nothing) [Y/n]:
...
Run the real acquisition now? [y/N]:
```

It writes a normal site config YAML (the same shape documented in
`example-site.yaml`) and offers to dry-run and then run the real
acquisition immediately. The written file is ordinary afterward — the
normal way to run it from here on is `wpfreeze freeze www-example-com`
(runs the config's declared `freeze.steps`, default acquire/build/validate,
end to end); each step is also available on its own, e.g.
`wpfreeze acquire www-example-com --resume`, `wpfreeze report`, and
`wpfreeze status`, all with no wizard involved. Exclusions, `extra_hosts`,
and `user_agent` are left at their shipped defaults; edit the YAML
directly if a site needs something different there -- `EXTRA-CONFIG-OPTIONS.md`
has a short list of what people tend to add first.

If you answer yes to the WXR-export question, you'll be asked for the
path to the file; it's written into the config as an optional
`xml_backup:` key that augments the sitemap/REST API inventory the same
way a live database connection did in an earlier design, but as a plain
file requiring no local database setup at all.

## Fully manual path

Copy `example-site.yaml`, fill in the values, and run:

```
wpfreeze freeze your-site
```

(or run each step yourself -- `wpfreeze acquire your-site`, `wpfreeze build
your-site`, `wpfreeze validate your-site` -- or address it by path instead
of by its `name:` with `wpfreeze acquire --config your-site.yaml`)

See that file's comments for every option, and `CLAUDE-acquire.md` for the
full spec this tool implements.
