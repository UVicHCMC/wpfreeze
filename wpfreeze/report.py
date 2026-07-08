"""Stage 7 -- report generation: report.json and a single self-contained
report.html, derived purely from an existing manifest (plus stat()'ing
raw/ for byte sizes) -- no network, no refetching. This is exactly what
`wpfreeze report` regenerates without running `acquire` again.

See CLAUDE-acquire.md, "Stage 7 -- Report".
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from wpfreeze.manifest import (
    FLAG_AUTH_GATED,
    FLAG_CONTAINS_FORM,
    FLAG_ODD_RESPONSE,
    FLAG_ORPHAN,
    FLAG_PLUGIN_MARKUP,
    FLAG_UNLISTED,
    Manifest,
    ManifestRecord,
    Status,
    utc_now,
)

logger = logging.getLogger(__name__)

_WAYBACK_TS_RE = re.compile(r"/web/(\d{14})id_/")

ACTION_REQUIRED_CATEGORIES = (
    (
        "missing",
        "Missing (no live or Wayback copy)",
        lambda m: m.by_status(Status.MISSING.value),
        "This URL was expected to exist (from the sitemap, REST API, or XML backup, or a "
        "link found while crawling), but its content couldn't be recovered live or from "
        "the Wayback Machine.",
        "Check the URL directly in a browser -- it may have been deliberately deleted, "
        "moved, or renamed. If it's important, look for another backup source (a staging "
        "site, XML export, or the site administrator) before this content is gone "
        "for good.",
    ),
    (
        "external_unfetchable",
        "External resources that couldn't be localised",
        lambda m: m.by_status(Status.EXTERNAL_UNFETCHABLE.value),
        "An asset or embed hosted on a different site (not the one being archived) "
        "that couldn't be fetched live, with no usable Wayback snapshot either.",
        "Often expected for embedded players (e.g. Vimeo, YouTube) that block requests "
        "without a real browser session -- the embed will likely still work fine on the "
        "live site. If it's something important, like a CDN-hosted image, consider "
        "downloading it manually and adding it to the archive by hand.",
    ),
    (
        "auth_gated",
        "Auth-gated (401/403)",
        lambda m: _by_flag(m, FLAG_AUTH_GATED),
        "The server responded with 401 or 403 -- the content exists but requires "
        "credentials or permissions wpfreeze doesn't have.",
        "If you have valid login credentials for the site, you can fetch this manually "
        "while authenticated and add it to the archive yourself. Otherwise, this is "
        "likely intentionally restricted content that may not need archiving at all.",
    ),
    (
        "odd_response",
        "Odd HTTP responses",
        lambda m: _by_flag(m, FLAG_ODD_RESPONSE),
        "The server returned a status code that didn't fit any of the well-understood "
        "categories (not a normal success, redirect, 404/410, or 401/403).",
        "Worth opening the URL directly in a browser to see what's actually happening -- "
        "could be a custom error page, a maintenance message, a rate limit, or an "
        "endpoint (like a form handler) that only responds to POST requests.",
    ),
    (
        "contains_form",
        "Pages with forms",
        lambda m: _by_flag(m, FLAG_CONTAINS_FORM),
        "The page archived successfully, but it contains an HTML form (e.g. contact, "
        "search, or comment form).",
        "Forms won't function in a static archive since there's no server to process "
        "submissions. This is informational, not a failure -- worth a look if the form "
        "matters, so you can add a note or a substitute (e.g. a mailto: link) later.",
    ),
    (
        "plugin_markup",
        "Pages with dynamic plugin markup",
        lambda m: _by_flag(m, FLAG_PLUGIN_MARKUP),
        "The page archived successfully, but it contains markup from an interactive "
        "plugin (e.g. a slider or gallery) whose full behaviour depends on JavaScript "
        "or plugin code that a static archive won't reproduce.",
        "Content is captured, but some interactive behaviour may look different once "
        "static. Worth spot-checking these specific pages once the archive is rendered.",
    ),
    (
        "orphan",
        "Orphans (in inventory, never crawled)",
        lambda m: _by_flag(m, FLAG_ORPHAN),
        "The site's own inventory (sitemap, REST API, or XML backup) says this URL "
        "exists, but nothing else on the site links to it, so the crawler never "
        "reached it independently.",
        "Usually harmless -- often an old or deliberately unlinked page. Worth a quick "
        "check if it looks like something that should still be reachable; a stale "
        "sitemap may be worth flagging to the site owner separately.",
    ),
    (
        "unlisted",
        "Unlisted (crawled, absent from inventory)",
        lambda m: _by_flag(m, FLAG_UNLISTED),
        "The crawler found and fetched this URL by following a link on the site, but "
        "it doesn't appear in the sitemap, REST API, or XML backup inventory.",
        "Usually fine -- many internal assets (images, scripts) were never meant to be "
        "in a content inventory. Worth a second look only if the URL looks like real "
        "content that should have been listed.",
    ),
)


def _by_flag(manifest: Manifest, flag: str) -> list[ManifestRecord]:
    return [r for r in manifest.all() if flag in r.flags]


def _file_size(output_dir: Path, local_path: str | None) -> int:
    if not local_path:
        return 0
    try:
        return (output_dir / local_path).stat().st_size
    except OSError:
        return 0


def _referrers(record: ManifestRecord) -> list[str]:
    """Where a crawl/Wayback-discovered record was linked from, mined out
    of its discovered_via provenance entries (crawl:<url> / wayback:<url>)."""
    refs = []
    for prov in record.discovered_via:
        if prov.startswith("crawl:") or prov.startswith("wayback:"):
            refs.append(prov.split(":", 1)[1])
    return refs


def _wayback_snapshot_date(wayback_url: str | None) -> str:
    if not wayback_url:
        return ""
    match = _WAYBACK_TS_RE.search(wayback_url)
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").date().isoformat()
    except ValueError:
        return ""


def infer_inventory_sources_used(manifest: Manifest) -> dict[str, bool]:
    provenance: set[str] = set()
    for record in manifest.all():
        provenance.update(record.discovered_via)
    return {
        "sitemap": "sitemap" in provenance,
        "rest_api": "rest_api" in provenance,
        "xml_backup": "xml_backup" in provenance,
        "crawl": any(p.startswith("crawl:") for p in provenance),
        "wayback": any(p.startswith("wayback:") for p in provenance),
    }


def build_summary(
    manifest: Manifest,
    output_dir: Path,
    run_started: str | None = None,
    run_finished: str | None = None,
) -> dict:
    records = manifest.all()
    return {
        "total_records": len(records),
        "status_counts": dict(Counter(r.status for r in records)),
        "source_counts": dict(Counter(r.source for r in records)),
        "total_bytes": sum(_file_size(output_dir, r.local_path) for r in records),
        "run_started": run_started,
        "run_finished": run_finished,
        "inventory_sources_used": infer_inventory_sources_used(manifest),
        "has_gaps": manifest.has_gaps(),
    }


def build_report_data(
    manifest: Manifest,
    output_dir: Path,
    run_started: str | None = None,
    run_finished: str | None = None,
) -> dict:
    return {
        "generated": utc_now(),
        "summary": build_summary(manifest, output_dir, run_started, run_finished),
        "records": [r.to_dict() for r in manifest.all()],
    }


def write_report_json(
    manifest: Manifest,
    output_dir: Path,
    path: Path,
    run_started: str | None = None,
    run_finished: str | None = None,
) -> None:
    data = build_report_data(manifest, output_dir, run_started, run_finished)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# report.html -- single self-contained static file
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return html_module.escape("" if value is None else str(value), quote=True)


def _summary_html(summary: dict) -> str:
    status_rows = "".join(
        f"<tr><td>{_esc(status)}</td><td>{_esc(count)}</td></tr>"
        for status, count in sorted(summary["status_counts"].items())
    )
    source_rows = "".join(
        f"<tr><td>{_esc(source)}</td><td>{_esc(count)}</td></tr>"
        for source, count in sorted(summary["source_counts"].items())
    )
    inventory_rows = "".join(
        f"<tr><td>{_esc(name)}</td><td>{'yes' if used else 'no'}</td></tr>"
        for name, used in summary["inventory_sources_used"].items()
    )
    total_mb = summary["total_bytes"] / (1024 * 1024)
    return f"""
    <section id="summary">
      <h2>Summary</h2>
      <p><strong>{_esc(summary['total_records'])}</strong> records total,
         <strong>{total_mb:.2f} MB</strong> archived.
         {'<span class="badge badge-gap">Archive incomplete</span>' if summary['has_gaps'] else '<span class="badge badge-ok">No gaps</span>'}
      </p>
      <p>Run: {_esc(summary.get('run_started') or 'unknown')} &rarr; {_esc(summary.get('run_finished') or 'unknown')}</p>
      <div class="summary-tables">
        <table><caption>By status</caption><tbody>{status_rows}</tbody></table>
        <table><caption>By source</caption><tbody>{source_rows}</tbody></table>
        <table><caption>Inventory sources used</caption><tbody>{inventory_rows}</tbody></table>
      </div>
    </section>
    """


def _referrers_cell_html(record: ManifestRecord) -> str:
    """Collapsed by default behind <details> -- referrers are only of
    interest for a deep dive, and a long inline comma-joined list (some
    pages are linked from dozens of others) otherwise crowds the row and
    reads as if it were part of the URL column itself."""
    refs = _referrers(record)
    if not refs:
        return "-"
    items = "".join(f'<li><a href="{_esc(r)}">{_esc(r)}</a></li>' for r in refs)
    noun = "referrer" if len(refs) == 1 else "referrers"
    return f"<details><summary>{len(refs)} {noun}</summary><ul class=\"referrer-list\">{items}</ul></details>"


def _record_row(record: ManifestRecord, extra_cols_html: list[str]) -> str:
    """`extra_cols_html` entries are trusted, already-escaped/safe HTML for
    one <td> each -- unlike the fixed leading columns above, which escape
    their own plain-text values."""
    cells = [
        f'<td><a href="{_esc(record.url)}">{_esc(record.url)}</a></td>',
        f"<td>{_esc(record.status)}</td>",
        f"<td>{_esc(record.http_status)}</td>",
        f'<td>{_esc(", ".join(record.flags))}</td>',
    ]
    cells.extend(f"<td>{c}</td>" for c in extra_cols_html)
    return "<tr>" + "".join(cells) + "</tr>"


def _action_required_html(manifest: Manifest) -> str:
    sections = []
    for key, title, selector, meaning, suggestion in ACTION_REQUIRED_CATEGORIES:
        records = selector(manifest)
        if not records:
            continue
        rows = "".join(
            _record_row(r, [_referrers_cell_html(r)]) for r in records
        )
        sections.append(
            f"""
            <details id="action-{key}">
              <summary>{_esc(title)} ({len(records)})</summary>
              <div class="category-help">
                <p><strong>What this means:</strong> {_esc(meaning)}</p>
                <p><strong>What you might do:</strong> {_esc(suggestion)}</p>
              </div>
              <table>
                <thead><tr><th>URL</th><th>Status</th><th>HTTP</th><th>Flags</th><th>Referrers</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </details>
            """
        )
    body = "".join(sections) if sections else "<p>Nothing needs action.</p>"
    return f"""
    <section id="action-required">
      <h2>Action required</h2>
      <p class="section-intro">Each category below explains what it means and what you
         might want to do about it -- expand or collapse with the summary line.</p>
      {body}
    </section>
    """


def _wayback_html(manifest: Manifest) -> str:
    records = manifest.by_status(Status.FETCHED_WAYBACK.value)
    if not records:
        return '<section id="wayback"><h2>Wayback-sourced pages</h2><p>None.</p></section>'
    rows = "".join(
        f"<tr><td><a href=\"{_esc(r.url)}\">{_esc(r.url)}</a></td>"
        f"<td>{_esc(_wayback_snapshot_date(r.wayback_url))}</td>"
        f'<td><a href="{_esc(r.wayback_url)}">{_esc(r.wayback_url)}</a></td></tr>'
        for r in records
    )
    return f"""
    <section id="wayback">
      <h2>Wayback-sourced pages ({len(records)})</h2>
      <table>
        <thead><tr><th>URL</th><th>Snapshot date</th><th>Snapshot</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
    """


def _discovered_via_cell_html(record: ManifestRecord) -> str:
    """Same collapsed-by-default treatment as the referrers column in
    Action required -- discovered_via is a broader provenance list (plain
    inventory-source labels like "sitemap"/"rest_api" alongside
    crawl:/wayback: referrer entries, not just page URLs), but just as
    easy to let crowd a row when there are many."""
    entries = record.discovered_via
    if not entries:
        return "-"
    items = "".join(f"<li>{_esc(e)}</li>" for e in entries)
    noun = "source" if len(entries) == 1 else "sources"
    return f'<details><summary>{len(entries)} {noun}</summary><ul class="referrer-list">{items}</ul></details>'


def _full_manifest_html(manifest: Manifest) -> str:
    records = manifest.all()
    rows = []
    for r in records:
        flags_attr = _esc(" ".join(r.flags))
        rows.append(
            f'<tr data-status="{_esc(r.status)}" data-flags="{flags_attr}">'
            f'<td>{_esc(r.url)}</td>'
            f"<td>{_esc(r.status)}</td>"
            f"<td>{_esc(r.source)}</td>"
            f"<td>{_esc(r.http_status)}</td>"
            f'<td>{_esc(", ".join(r.flags))}</td>'
            f"<td>{_discovered_via_cell_html(r)}</td>"
            f"<td>{_esc(r.output_path)}</td>"
            "</tr>"
        )
    statuses = sorted({r.status for r in records})
    status_options = "".join(f'<option value="{_esc(s)}">{_esc(s)}</option>' for s in statuses)
    flags = sorted({f for r in records for f in r.flags})
    flag_options = "".join(f'<option value="{_esc(f)}">{_esc(f)}</option>' for f in flags)

    return f"""
    <section id="full-manifest">
      <h2>Full manifest ({len(records)})</h2>
      <div class="filters">
        <input id="filter-text" type="search" placeholder="Filter by URL substring...">
        <select id="filter-status"><option value="">All statuses</option>{status_options}</select>
        <select id="filter-flag"><option value="">All flags</option>{flag_options}</select>
      </div>
      <table id="manifest-table">
        <thead><tr><th>URL</th><th>Status</th><th>Source</th><th>HTTP</th><th>Flags</th><th>Discovered via</th><th>Output path</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </section>
    """


_CSS = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }
a { color: #1a56c4; }
a:visited { color: #7a3fa0; }
a:hover { text-decoration: underline; }
h1, h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; }
caption { text-align: left; font-weight: 600; margin-bottom: 0.25rem; }
th, td { border: 1px solid #ddd; padding: 0.35rem 0.6rem; text-align: left; font-size: 0.9rem; }
th { background: #f4f4f4; }
.summary-tables { display: flex; gap: 2rem; flex-wrap: wrap; }
.summary-tables table { width: auto; min-width: 200px; }
.badge { padding: 0.2rem 0.6rem; border-radius: 1rem; font-size: 0.85rem; }
.badge-gap { background: #fde2e2; color: #7a1212; }
.badge-ok { background: #e2fde3; color: #14591c; }
details { margin-bottom: 1rem; }
summary { cursor: pointer; font-weight: 600; }
.filters { margin-bottom: 0.5rem; display: flex; gap: 0.5rem; }
.filters input, .filters select { padding: 0.3rem; }
.section-intro { color: #555; font-size: 0.9rem; margin-top: -0.5rem; }
.category-help {
  background: #f4f7fb; border-left: 3px solid #6a8fc7; border-radius: 0 4px 4px 0;
  padding: 0.5rem 0.9rem; margin: 0.4rem 0 0.8rem; font-size: 0.9rem;
}
.category-help p { margin: 0.3rem 0; }
td details { margin-bottom: 0; }
td details summary { font-weight: 400; font-size: 0.85rem; color: #3a5a8c; }
ul.referrer-list { margin: 0.3rem 0 0; padding-left: 1.2rem; max-height: 200px; overflow-y: auto; }
ul.referrer-list li { margin: 0.15rem 0; }
@media (prefers-color-scheme: dark) {
  body { background: #1e1e1e; color: #ddd; }
  a { color: #7db3ff; }
  a:visited { color: #d3a6f0; }
  th { background: #2a2a2a; }
  th, td { border-color: #444; }
  .badge-gap { background: #4a1f1f; color: #ff9d9d; }
  .badge-ok { background: #1f4a22; color: #9dffa3; }
  .section-intro { color: #aaa; }
  .category-help { background: #253044; border-left-color: #6a8fc7; }
  td details summary { color: #8fb3e8; }
}
"""

_JS = """
(function () {
  var textInput = document.getElementById('filter-text');
  var statusSelect = document.getElementById('filter-status');
  var flagSelect = document.getElementById('filter-flag');
  var rows = document.querySelectorAll('#manifest-table tbody tr');

  function apply() {
    var text = textInput.value.toLowerCase();
    var status = statusSelect.value;
    var flag = flagSelect.value;
    rows.forEach(function (row) {
      var matchesText = !text || row.children[0].textContent.toLowerCase().indexOf(text) !== -1;
      var matchesStatus = !status || row.getAttribute('data-status') === status;
      var rowFlags = (row.getAttribute('data-flags') || '').split(' ');
      var matchesFlag = !flag || rowFlags.indexOf(flag) !== -1;
      row.style.display = (matchesText && matchesStatus && matchesFlag) ? '' : 'none';
    });
  }

  textInput.addEventListener('input', apply);
  statusSelect.addEventListener('change', apply);
  flagSelect.addEventListener('change', apply);
})();
"""


def build_report_html(manifest: Manifest, output_dir: Path, run_started: str | None = None, run_finished: str | None = None) -> str:
    summary = build_summary(manifest, output_dir, run_started, run_finished)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>wpfreeze acquisition report</title>
<style>{_CSS}</style>
</head>
<body>
<h1>wpfreeze acquisition report</h1>
{_summary_html(summary)}
{_action_required_html(manifest)}
{_wayback_html(manifest)}
{_full_manifest_html(manifest)}
<script>{_JS}</script>
</body>
</html>
"""


def write_report_html(
    manifest: Manifest,
    output_dir: Path,
    path: Path,
    run_started: str | None = None,
    run_finished: str | None = None,
) -> None:
    path.write_text(build_report_html(manifest, output_dir, run_started, run_finished), encoding="utf-8")
