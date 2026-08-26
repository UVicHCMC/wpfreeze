"""Stage 7 -- report generation: report.json and a single self-contained
report.html, derived from an existing manifest (plus stat()'ing raw/ for
byte sizes) and, for a dry run only, the inventory-source reachability map
`discover_inventory` returned -- no network, no refetching either way.
`wpfreeze report` regenerates the manifest-only half without running
`acquire` again; the dry-run readiness assessment is acquire-only, see
`assess_dry_run`'s own docstring for why.

See CLAUDE-acquire.md, "Stage 7 -- Report", and
CLAUDE-dry-run-readiness.md for the readiness assessment's own design.
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from wpfreeze.style import green, red, yellow

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


# ---------------------------------------------------------------------------
# Dry-run readiness assessment -- see CLAUDE-dry-run-readiness.md for the
# full design rationale, the measured thresholds below, and the six
# decisions (R5 cut, severity defined by actionability, etc.) this code
# implements without re-arguing.
# ---------------------------------------------------------------------------

DRY_RUN_DISCLAIMER = (
    "Based on inventory discovery only -- this doesn't check page content, "
    "forms, or how the site will actually render once built."
)

# What "inventory-sourced" means for this feature: a record that at least
# one of the three discovery sources (or the seeded base_url itself)
# claimed. Deliberately excludes crawl:/wayback: provenance -- see the
# resumed-manifest trap below, this is the one filter that makes every
# percentage in this module mean what it says.
_INVENTORY_PROVENANCE = frozenset({"sitemap", "rest_api", "xml_backup", "base_url"})

# (category label, pattern). Soft, suggestion-only signals about WordPress
# archive/attachment URL shapes -- deliberately NOT merged with
# wizard.DEFAULT_EXCLUSIONS, which is a hard "never fetch this" list of
# dead WordPress infrastructure. These are legitimate content some site
# owners want archived and others don't; see CLAUDE-dry-run-readiness.md
# Decision 5 for why the two lists must not become one.
_LOW_VALUE_ARCHIVE_PATTERNS = (
    ("attachment page", re.compile(r"/attachment/|[?&]attachment_id=")),
    ("tag archive", re.compile(r"/tag/|[?&]tag=")),
    ("category archive", re.compile(r"/category/|[?&]cat=")),
    ("author archive", re.compile(r"/author/|[?&]author=")),
    # Matches 0 inventory URLs on every real site measured so far --
    # paginated archives are found by crawling, not by any inventory
    # source, and that is expected, not a bug in the pattern. Kept because
    # a Yoast sitemap can list them. See the Measured baseline table in
    # CLAUDE-dry-run-readiness.md before "fixing" this to fire more.
    ("paginated archive", re.compile(r"/page/\d+")),
    ("date archive", re.compile(r"/\d{4}/\d{2}(/\d{2})?/?$")),
)


@dataclass(frozen=True)
class Finding:
    code: str  # stable machine id, e.g. "single_live_source" -- assert on this, not on message text
    severity: str  # "notice" | "concern" -- concern is what pushes the verdict to "attention"
    message: str  # what was observed
    suggestion: str  # what you might do about it


@dataclass(frozen=True)
class DryRunAssessment:
    verdict: str  # "ready" | "review" | "attention"
    headline: str
    findings: tuple[Finding, ...]


def _inventory_records(manifest: Manifest) -> list[ManifestRecord]:
    return [r for r in manifest.all() if _INVENTORY_PROVENANCE & set(r.discovered_via)]


def _low_value_archive_counts(urls: list[str]) -> tuple[int, dict[str, int]]:
    """Returns (count of distinct URLs matching at least one pattern, counts
    per category). The two can differ -- a URL matching two categories is
    counted once in the first, once in each in the second -- deliberately:
    the threshold check needs the former, the message's breakdown needs
    the latter."""
    matched_urls: set[str] = set()
    per_category: dict[str, int] = {}
    for label, pattern in _LOW_VALUE_ARCHIVE_PATTERNS:
        count = sum(1 for url in urls if pattern.search(url))
        if count:
            per_category[label] = count
    for url in urls:
        if any(pattern.search(url) for _, pattern in _LOW_VALUE_ARCHIVE_PATTERNS):
            matched_urls.add(url)
    return len(matched_urls), per_category


def assess_dry_run(
    manifest: Manifest,
    sources: dict[str, bool],
    *,
    xml_backup_configured: bool,
) -> DryRunAssessment:
    """A heuristic readiness verdict from a dry run's inventory discovery
    alone -- acquire-only (see write_report_json/write_report_html's
    `readiness` parameter), never regenerated by `wpfreeze report`: that
    command has no `sources` dict to work from (it reads manifest.json
    alone), and faking one back from `infer_inventory_sources_used` can't
    distinguish "reachable but contributed nothing" from "unreachable" --
    exactly the distinction R4's concern tier is built on. A regenerated
    verdict would silently disagree with the one `acquire` wrote, which is
    worse than not showing one. See CLAUDE-dry-run-readiness.md Decision 3.

    `xml_backup_configured` is deliberately a separate argument from
    `sources["xml_backup"]`: the latter is False both when no export is
    configured at all and when a configured one failed to read/parse, and
    R3 needs to tell those apart.
    """
    inventory_records = _inventory_records(manifest)
    total = len(inventory_records)
    findings: list[Finding] = []

    # R1 -- concern. Nothing beyond the seeded base_url itself.
    nothing_discovered = total <= 1
    if nothing_discovered:
        findings.append(
            Finding(
                code="nothing_discovered",
                severity="concern",
                message="Only the seeded base_url was found -- no sitemap, REST API, or "
                "XML export entry was kept.",
                suggestion="Check that base_url is correct and reachable in a browser, and "
                "that exclusions isn't matching everything.",
            )
        )

    # R2 -- concern. No inventory source reachable at all. Distinct from
    # R1: an XML export alone can seed real URLs with both live sources
    # unreachable, so the two can diverge in either direction.
    if not any(sources.values()):
        findings.append(
            Finding(
                code="no_inventory_source",
                severity="concern",
                message="No inventory source (sitemap, REST API, or XML export) was reachable.",
                suggestion="The crawl will work from the homepage's links alone, which can't "
                "be cross-checked for completeness; consider exporting a WXR from wp-admin "
                "(Tools -> Export) and setting xml_backup.",
            )
        )

    sitemap_ok = bool(sources.get("sitemap"))
    rest_ok = bool(sources.get("rest_api"))
    sitemap_count = sum(1 for r in inventory_records if "sitemap" in r.discovered_via)
    rest_count = sum(1 for r in inventory_records if "rest_api" in r.discovered_via)

    # R3 -- notice. Exactly one of sitemap/REST API reachable, no XML
    # export configured. Common and often fine (security plugins routinely
    # block /wp-json/) -- worded as an FYI, not an alarm. Suppressed when
    # R1 already fired: a single-source note is noise once "nothing was
    # found at all" is already the headline.
    if not nothing_discovered and sitemap_ok != rest_ok and not xml_backup_configured:
        live_label = "sitemap" if sitemap_ok else "REST API"
        findings.append(
            Finding(
                code="single_live_source",
                severity="notice",
                message=f"Only the {live_label} is reachable as a live inventory source.",
                suggestion="An XML export gives a second, independent list to cross-check "
                "against (wp-admin -> Tools -> Export).",
            )
        )

    # R4 -- concern (source_contributed_nothing) or notice
    # (source_underweight). The concern half is the highest-value rule
    # here and is free: `sources[key]` is set True before _seed runs, so
    # "reachable but yielded 0 in-scope URLs" is a real, cheap signal --
    # near-certain misconfiguration (wrong host/base_path/exclusions), not
    # a coincidence.
    for key, label, count in (("sitemap", "sitemap", sitemap_count), ("rest_api", "REST API", rest_count)):
        if sources.get(key) and count == 0:
            findings.append(
                Finding(
                    code="source_contributed_nothing",
                    severity="concern",
                    message=f"The {label} was reachable but contributed 0 URLs to the inventory.",
                    suggestion=f"Compare base_url against the URLs the {label} actually lists "
                    "-- a mismatched host (www. vs bare), a wrong base_path on a multisite "
                    "sub-path install, or an exclusions pattern matching everything are the "
                    "usual causes.",
                )
            )

    if sitemap_ok and rest_ok and sitemap_count > 0 and rest_count > 0:
        low_count, high_count = sorted((sitemap_count, rest_count))
        # Directional threshold, not symmetric: REST API > sitemap is the
        # normal case (REST exposes attachments/users no sitemap lists),
        # not a finding. Measured against five real sites before being
        # set at 10% -- see CLAUDE-dry-run-readiness.md's Measured baseline.
        if low_count / high_count < 0.10:
            if sitemap_count < rest_count:
                low_label, high_label = "sitemap", "REST API"
            else:
                low_label, high_label = "REST API", "sitemap"
            findings.append(
                Finding(
                    code="source_underweight",
                    severity="notice",
                    message=f"The {low_label} contributed {low_count} URLs against the "
                    f"{high_label}'s {high_count}.",
                    suggestion="That gap is usually a sitemap plugin listing only part of "
                    "the site, or (on a multisite sub-path install) a sitemap belonging to "
                    "the network root rather than to this site.",
                )
            )

    # R6 -- notice. A large, absolute-and-relative share of the inventory
    # is WordPress archive/attachment pages, not standalone content.
    # Suppressed when R1 already fired, same reasoning as R3.
    if not nothing_discovered:
        urls = [r.url for r in inventory_records]
        matched_total, per_category = _low_value_archive_counts(urls)
        if matched_total >= 20 and (matched_total / total) >= 0.10:
            breakdown = ", ".join(
                f"{count} {label}{'s' if count != 1 else ''}"
                for label, count in sorted(per_category.items(), key=lambda kv: -kv[1])
            )
            findings.append(
                Finding(
                    code="low_value_archives",
                    severity="notice",
                    message=f"{breakdown} were found in the inventory.",
                    suggestion="These are WordPress archive/attachment pages, not standalone "
                    "content -- worth excluding (see EXTRA-CONFIG-OPTIONS.md) if you don't "
                    "want them in the archive.",
                )
            )

    severities = {f.severity for f in findings}
    if "concern" in severities:
        verdict, headline = "attention", "Something looks wrong; worth fixing before crawling."
    elif "notice" in severities:
        verdict, headline = "review", "Worth a look before you commit to a crawl."
    else:
        verdict, headline = "ready", "Nothing to flag in the inventory."

    return DryRunAssessment(verdict=verdict, headline=headline, findings=tuple(findings))


def format_readiness(assessment: DryRunAssessment) -> str:
    """Console rendering -- a leading blank line so the caller can just
    print() this straight after the existing 'by source' line with no
    extra bookkeeping; see _run_acquire_locked's dry-run branch."""
    color_fn = {"ready": green, "review": yellow, "attention": red}[assessment.verdict]
    lines = ["", color_fn(f"Readiness: {assessment.headline}", bold_too=True)]
    for finding in assessment.findings:
        lines.append(f"  - {finding.message} {finding.suggestion}")
    lines.append(DRY_RUN_DISCLAIMER)
    return "\n".join(lines)


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
    *,
    readiness: DryRunAssessment | None = None,
) -> dict:
    return {
        "generated": utc_now(),
        "summary": build_summary(manifest, output_dir, run_started, run_finished),
        # Always present (dry-run and real-run alike) so the schema never
        # differs between them; null outside a dry run rather than a
        # stale/regenerated verdict -- see assess_dry_run's own docstring.
        "readiness": asdict(readiness) if readiness is not None else None,
        "records": [r.to_dict() for r in manifest.all()],
    }


def write_report_json(
    manifest: Manifest,
    output_dir: Path,
    path: Path,
    run_started: str | None = None,
    run_finished: str | None = None,
    *,
    readiness: DryRunAssessment | None = None,
) -> None:
    data = build_report_data(manifest, output_dir, run_started, run_finished, readiness=readiness)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# report.html -- single self-contained static file
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return html_module.escape("" if value is None else str(value), quote=True)


def _readiness_html(assessment: DryRunAssessment | None) -> str:
    """Emits nothing at all when `assessment` is None -- a real (non-dry)
    run's report, and a `wpfreeze report` regeneration, both pass None on
    purpose (see assess_dry_run's docstring), so this section simply
    doesn't exist on those reports rather than showing a stale verdict."""
    if assessment is None:
        return ""
    badge_class = {"ready": "badge-ok", "review": "badge-review", "attention": "badge-gap"}[assessment.verdict]
    if assessment.findings:
        items = "".join(
            f"<li><p>{_esc(finding.message)}</p>"
            f'<div class="category-help"><p><strong>What you might do:</strong> '
            f"{_esc(finding.suggestion)}</p></div></li>"
            for finding in assessment.findings
        )
        body = f'<ul class="readiness-findings">{items}</ul>'
    else:
        body = "<p>No findings.</p>"
    return f"""
    <section id="readiness">
      <h2>Dry-run readiness <span class="badge {badge_class}">{_esc(assessment.headline)}</span></h2>
      {body}
      <p class="section-intro">{_esc(DRY_RUN_DISCLAIMER)}</p>
    </section>
    """


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
.badge-review { background: #fff4d6; color: #7a5200; }
ul.readiness-findings { list-style: none; padding: 0; margin: 0.5rem 0 1rem; }
ul.readiness-findings li { margin-bottom: 0.8rem; }
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
  .badge-review { background: #4a3a12; color: #ffd27a; }
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


def build_report_html(
    manifest: Manifest,
    output_dir: Path,
    run_started: str | None = None,
    run_finished: str | None = None,
    *,
    readiness: DryRunAssessment | None = None,
) -> str:
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
{_readiness_html(readiness)}
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
    *,
    readiness: DryRunAssessment | None = None,
) -> None:
    path.write_text(
        build_report_html(manifest, output_dir, run_started, run_finished, readiness=readiness), encoding="utf-8"
    )
