# Getting started

Two easy on-ramps, plus the fully manual path for when you want full control.

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
Do you have database access for this site?
  1) No
  2) Yes, I can connect directly
  3) I have a dump file
Choose [1]:
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

If you choose "I have a dump file" at the database question, the wizard
hands off to `setup-db` below automatically.

## `wpfreeze setup-db` — import a SQL dump into a local database

Most real-world targets are on hosting where the production database is
never reachable directly. If all you have is a `.sql`/`.sql.gz` export
from your host's backup tool, this gets you from that file to a running,
scoped, localhost-only MariaDB with the dump imported:

```
$ wpfreeze setup-db --dump ./site-export.sql.gz
```

It will:
1. Check that `mariadb-server`/`mariadb-client` are installed and running,
   asking before installing or starting anything (`--yes` skips the
   prompts).
2. Warn (not block) if the server isn't bound to localhost only.
3. Create a database and a scoped user that can only touch that one
   database — never your live site's database, never anything else on the
   box.
4. Import the dump (handles `.sql` and `.sql.gz`) and run a quick
   `SELECT COUNT(*)` to confirm it actually worked.
5. Print the `db:` YAML block to paste into your site config, plus the
   `export WPFREEZE_STAGE_DB_PASSWORD=...` line for your shell profile.

Run it without `--dump` and it'll ask for the path interactively. See
`--help` for the full set of flags (`--db-name`, `--stage-user`,
`--stage-password`, `--table-prefix`, `--socket`).

The resulting database is a completely normal, persistent local MySQL/
MariaDB target — `wpfreeze acquire` uses it through the same live `db:`
connection path as any directly-reachable database. There is no
dump-specific code in the acquisition pipeline itself.

## Fully manual path

Copy `example-site.yaml`, fill in the values, and run:

```
wpfreeze acquire --config your-site.yaml
```

See that file's comments for every option, and `CLAUDE-acquire.md` for the
full spec this tool implements.
