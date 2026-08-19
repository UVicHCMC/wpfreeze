"""Upload-script generation: a ready-to-run rsync script for pushing a
built site somewhere a site owner can look at it -- a staging/preview
location, not a production deployment. The destination lives in the
site's own YAML config (`upload.remote`), so it doesn't have to be
hand-edited into a script for every site.

Deliberately just a couple of lines of parameterized bash, not a Python
upload command: it's meant to be run standalone (from a CI job, or by
someone with rsync but no wpfreeze install), and to stay hand-editable --
anyone with unusual needs (a different sync tool, an extra exclude
pattern) can just open upload.sh and change it, exactly as they could the
hand-maintained script this replaces.

Written only by the explicit `wpfreeze upload-script` command, never as a
side effect of `build` -- generating the script and running it are both
things the user does on purpose, when they're ready, not something that
happens because a config file happens to have a remote set.
"""
from __future__ import annotations

from pathlib import Path

_TEMPLATE = """\
#!/usr/bin/env bash
set -euo pipefail

REMOTE="{remote}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cp -a {site_rel}/. "$TMP/"
cp cleanup-todo.html report.html "$TMP/"

rsync -av --delete \\
    "$TMP/" \\
    "$REMOTE/"
"""


def write_upload_script(output_dir: Path, remote: str | None, site_rel: str = "site") -> Path | None:
    """Write upload.sh into `output_dir`, executable, or write nothing and
    return None if `remote` (from the config's `upload.remote`) is unset --
    there is no sensible destination to fall back to.

    `site_rel` is the built site's location relative to `output_dir` (the
    default "site" matches `build`'s own default `--site-dir`); the script
    always assumes it is run from `output_dir` itself.
    """
    if not remote:
        return None
    path = output_dir / "upload.sh"
    path.write_text(_TEMPLATE.format(remote=remote, site_rel=site_rel), encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path
