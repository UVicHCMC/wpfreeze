"""Upload-script generation: a ready-to-run script for getting a built
site somewhere a site owner can look at it -- three ways, one script:

- (no flag) sync the site plus its human-facing reports to a staging/
  preview remote (`upload.remote`), for reading alongside the pages they
  describe.
- `--local` assemble the same site+reports bundle under `<output_dir>/
  preview/` with no network at all, for browsing straight off disk.
- `--prod` sync the site *only* -- no reports, they're for review, never
  for the live site -- to a real production remote (`upload.prod_remote`),
  after a confirmation prompt (the one branch that runs `rsync --delete`
  against a host meant to stay up).

The destinations live in the site's own YAML config, so they don't have to
be hand-edited into a script for every site.

Deliberately just a page of parameterized bash, not a Python upload
command: it's meant to be run standalone (from a CI job, or by someone
with rsync but no wpfreeze install), and to stay hand-editable -- anyone
with unusual needs (a different sync tool, an extra exclude pattern) can
just open upload.sh and change it, exactly as they could the
hand-maintained script this replaces.

Written only by the explicit `wpfreeze upload-script` command, never as a
side effect of `build` -- generating the script and running it (in
whichever mode) are both things the user does on purpose, when they're
ready, not something that happens because a config file happens to have a
remote set. Written unconditionally, even with neither remote configured
-- `--local` needs neither, so there is always something useful for this
script to do.
"""
from __future__ import annotations

from pathlib import Path

# Sentinel tokens rather than str.format()/string.Template placeholders --
# the script body below is already dense with bash's own "{...}" (function
# bodies, ${1:-}) and "$..." (variable expansion) syntax, either of which
# would have to be escaped constantly against a real templating mechanism.
# Plain .replace() on tokens that can't appear in bash sidesteps that
# entirely.
_TEMPLATE = """\
#!/usr/bin/env bash
set -euo pipefail

REMOTE="__REMOTE__"
PROD_REMOTE="__PROD_REMOTE__"
SITE_REL="__SITE_REL__"
REPORT_FILES="cleanup-todo.html report.html broken-external-links.html"

usage() {
    echo "Usage: $0 [--local | --prod]" >&2
    echo "  (no flag)  sync site + reports to the staging remote (upload.remote)" >&2
    echo "  --local    assemble site + reports under ./preview/, no network" >&2
    echo "  --prod     sync site only (no reports) to the production remote (upload.prod_remote)" >&2
}

MODE="staging"
case "${1:-}" in
    "") ;;
    --local) MODE="local" ;;
    --prod) MODE="prod" ;;
    *) usage; exit 1 ;;
esac

copy_reports() {
    # Not every report exists on every build -- broken-external-links.html
    # only appears after `wpfreeze checklinks`, for instance -- so each is
    # copied only if present rather than failing the whole script over one
    # that hasn't been generated yet.
    for f in $REPORT_FILES; do
        if [ -f "$f" ]; then
            cp "$f" "$1/"
        fi
    done
}

if [ "$MODE" = "local" ]; then
    DEST="preview"
    rm -rf "$DEST"
    mkdir -p "$DEST"
    cp -a "$SITE_REL/." "$DEST/"
    copy_reports "$DEST"
    echo "Local preview assembled at $DEST/ -- e.g. cd $DEST && python3 -m http.server"
    exit 0
fi

if [ "$MODE" = "prod" ]; then
    if [ -z "$PROD_REMOTE" ]; then
        echo "No upload.prod_remote set in the config; re-run 'wpfreeze upload-script' after adding it." >&2
        exit 1
    fi
    read -r -p "This will overwrite $PROD_REMOTE with the current build (site only, no reports). Continue? [y/N] " REPLY
    case "$REPLY" in
        y|Y|yes|YES|Yes) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    cp -a "$SITE_REL/." "$TMP/"
    rsync -av --delete "$TMP/" "$PROD_REMOTE/"
    exit 0
fi

# staging (default, no flag)
if [ -z "$REMOTE" ]; then
    echo "No upload.remote set in the config; re-run 'wpfreeze upload-script' after adding it." >&2
    exit 1
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cp -a "$SITE_REL/." "$TMP/"
copy_reports "$TMP"
rsync -av --delete "$TMP/" "$REMOTE/"
"""


def write_upload_script(
    output_dir: Path, remote: str | None, prod_remote: str | None = None, site_rel: str = "site"
) -> Path:
    """Write upload.sh into `output_dir`, executable, unconditionally --
    even with both `remote` and `prod_remote` unset, since the script's
    `--local` mode needs neither. Each remote-using mode checks its own
    variable at run time instead and errors clearly if that mode's remote
    was never configured.

    `site_rel` is the built site's location relative to `output_dir` (the
    default "site" matches `build`'s own default `--site-dir`); the script
    always assumes it is run from `output_dir` itself.
    """
    content = (
        _TEMPLATE.replace("__REMOTE__", remote or "")
        .replace("__PROD_REMOTE__", prod_remote or "")
        .replace("__SITE_REL__", site_rel)
    )
    path = output_dir / "upload.sh"
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path
