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
    ("missing", "Missing (no live or Wayback copy)", lambda m: m.by_status(Status.MISSING.value)),
    (
        "external_unfetchable",
        "External resources that couldn't be localized",
        lambda m: m.by_status(Status.EXTERNAL_UNFETCHABLE.value),
    ),
    ("auth_gated", "Auth-gated (401/403)", lambda m: _by_flag(m, FLAG_AUTH_GATED)),
    ("odd_response", "Odd HTTP responses", lambda m: _by_flag(m, FLAG_ODD_RESPONSE)),
    ("contains_form", "Pages with forms", lambda m: _by_flag(m, FLAG_CONTAINS_FORM)),
    ("plugin_markup", "Pages with dynamic plugin markup", lambda m: _by_flag(m, FLAG_PLUGIN_MARKUP)),
    ("orphan", "Orphans (in inventory, never crawled)", lambda m: _by_flag(m, FLAG_ORPHAN)),
    ("unlisted", "Unlisted (crawled, absent from inventory)", lambda m: _by_flag(m, FLAG_UNLISTED)),
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
        "database": "database" in provenance,
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


def _record_row(record: ManifestRecord, extra_cols: list[str]) -> str:
    cells = [
        f'<td><a href="{_esc(record.url)}">{_esc(record.url)}</a></td>',
        f"<td>{_esc(record.status)}</td>",
        f"<td>{_esc(record.http_status)}</td>",
        f'<td>{_esc(", ".join(record.flags))}</td>',
    ]
    cells.extend(f"<td>{_esc(c)}</td>" for c in extra_cols)
    return "<tr>" + "".join(cells) + "</tr>"


def _action_required_html(manifest: Manifest) -> str:
    sections = []
    for key, title, selector in ACTION_REQUIRED_CATEGORIES:
        records = selector(manifest)
        if not records:
            continue
        rows = "".join(
            _record_row(r, [", ".join(_referrers(r)) or "-"]) for r in records
        )
        sections.append(
            f"""
            <details id="action-{key}" open>
              <summary>{_esc(title)} ({len(records)})</summary>
              <table>
                <thead><tr><th>URL</th><th>Status</th><th>HTTP</th><th>Flags</th><th>Referrers</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </details>
            """
        )
    body = "".join(sections) if sections else "<p>Nothing needs action.</p>"
    return f'<section id="action-required"><h2>Action required</h2>{body}</section>'


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
            f'<td>{_esc(", ".join(r.discovered_via))}</td>'
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
@media (prefers-color-scheme: dark) {
  body { background: #1e1e1e; color: #ddd; }
  th { background: #2a2a2a; }
  th, td { border-color: #444; }
  .badge-gap { background: #4a1f1f; color: #ff9d9d; }
  .badge-ok { background: #1f4a22; color: #9dffa3; }
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
