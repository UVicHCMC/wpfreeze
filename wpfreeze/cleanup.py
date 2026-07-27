"""Cleanup-todo synthesis: turn `build`/`validate`/`diagnose`'s machine
reports into a short, human-readable punch list. Written automatically
whenever `build` or `validate` runs (see cli.py's `_announce_cleanup_todo`)
so a site owner is told "here's what's left" without having to remember to
go looking for it.

Deliberately does not duplicate report.py's acquisition-gap breakdown
(missing/auth_gated/orphan/... categories, already generated automatically
at the end of every `acquire` with its own prose). This covers only what
becomes available *after* build/validate run: broken references the build
couldn't resolve, capture-integrity anomalies from `diagnose`, and the
site's own pre-existing markup/content quirks VNU catches.

All three inputs -- build-report.json, vnu-report.json, diagnostics.json --
are optional and independently present or absent on disk; whatever is
missing is noted, not treated as an error, so this can be regenerated
after only `build`, only `validate`, or both, in either order.
"""
from __future__ import annotations

import json
from pathlib import Path

_MAX_LISTED = 15


def _load_json(path: Path) -> dict | None:
    """A report, or None if it is absent, unreadable, malformed, or simply
    not a JSON object. Every caller treats None as "this step hasn't run",
    which is the useful reading for all four cases -- but the annotation
    has to hold, or the `is None` checks pass a list straight through to a
    `.get()` call."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _integrity_section(diagnostics: dict | None) -> tuple[list[str], bool]:
    """Returns (markdown lines, whether anything here needs attention)."""
    if diagnostics is None:
        return (
            [
                "## Capture integrity",
                "",
                "`wpfreeze diagnose` hasn't been run for this capture, so the duplicate-file "
                "and content-hash checks haven't happened. Not urgent, but worth doing once: "
                "`wpfreeze diagnose --config <your-config>.yaml`.",
                "",
            ],
            False,
        )

    dup = diagnostics.get("duplicate_local_paths", [])
    mismatches = diagnostics.get("disk_hash_mismatches", [])
    shape = diagnostics.get("content_type_shape_mismatches", [])
    # `or {}` not a default arg: the key can be present and null.
    homepage_found = (diagnostics.get("homepage") or {}).get("found", True)

    problems = []
    if dup:
        problems.append(f"{len(dup)} file(s) were claimed by more than one URL -- one silently overwrote another.")
    if mismatches:
        problems.append(f"{len(mismatches)} file(s) on disk don't match what the manifest recorded fetching.")
    if shape:
        problems.append(f"{len(shape)} directory-shaped URL(s) served non-HTML content -- likely a collision.")
    if not homepage_found:
        problems.append("No manifest record matches the site's own homepage URL.")

    if not problems:
        return ([], False)

    lines = [
        "## Capture integrity -- look at this first",
        "",
        "These usually mean two different pages or files silently collided during "
        "acquisition, not just a missing page. See diagnostics.json for detail.",
        "",
    ]
    lines.extend(f"- {p}" for p in problems)
    lines.append("")
    return (lines, True)


def _broken_reference_section(build_report: dict | None) -> tuple[list[str], bool]:
    if build_report is None:
        return (
            [
                "## Broken references",
                "",
                "`wpfreeze build` hasn't been run yet, so references haven't been checked "
                "against what was actually captured.",
                "",
            ],
            False,
        )

    unresolved = build_report.get("unresolved", 0)
    if not unresolved:
        return ([], False)

    samples = build_report.get("unresolved_samples", [])
    lines = [
        "## Broken references",
        "",
        f"{unresolved} reference(s) on the built site never resolved to a local file and "
        "were left pointing at the original site. Once that site is gone, these become "
        "dead links -- worth checking now while the original is still up to compare "
        "against.",
        "",
    ]
    for page, value in samples[:_MAX_LISTED]:
        lines.append(f"- `{value}` (linked from `{page}`)")
    remaining = unresolved - min(len(samples), _MAX_LISTED)
    if remaining > 0:
        lines.append(f"- ...and {remaining} more (see build-report.json)")
    lines.append("")
    return (lines, True)


def _verification_section(build_report: dict | None) -> tuple[list[str], bool]:
    """Broken *local* links -- references that were rewritten to a path
    inside the built site which does not exist on disk. Distinct from the
    unresolved references above (those were never rewritten at all), and a
    different remedy: these are wrong within the archive itself.
    """
    if build_report is None:
        return ([], False)

    verification = build_report.get("verification")
    if verification is None:
        return (
            [
                "## Local link verification",
                "",
                "The build ran with `--no-verify`, so nothing checked that the rewritten "
                "links actually resolve to files on disk. Re-run `wpfreeze build` without "
                "that flag to check.",
                "",
            ],
            False,
        )

    broken = verification.get("broken", 0)
    if not broken:
        return ([], False)

    samples = verification.get("broken_samples", [])
    lines = [
        "## Local link verification",
        "",
        f"{broken} link(s) inside the built site point at a file that isn't there. Unlike "
        "the unresolved references above, these were rewritten to a local path -- the path "
        "just doesn't exist, so they are broken within the archive itself and will not fix "
        "themselves by keeping the original site up.",
        "",
    ]
    for sample in samples[:_MAX_LISTED]:
        source = sample.get("source", "?")
        reference = sample.get("reference", "?")
        reason = sample.get("reason", "")
        lines.append(f"- `{reference}` in `{source}`" + (f" -- {reason}" if reason else ""))
    remaining = broken - min(len(samples), _MAX_LISTED)
    if remaining > 0:
        lines.append(f"- ...and {remaining} more (see build-report.json)")
    lines.append("")
    return (lines, True)


def _markup_quirks_section(vnu_report: dict | None) -> list[str]:
    if vnu_report is None:
        return [
            "## Site markup/content quirks",
            "",
            "`wpfreeze validate` hasn't been run against the built site, so HTML/CSS "
            "issues in the original theme or content (missing alt text, heading order, "
            "invalid inline styles, and the like) haven't been checked for. Run "
            "`wpfreeze validate --config <your-config>.yaml` to see them.",
            "",
        ]

    issues = vnu_report.get("issues", [])
    if not issues:
        return [
            "## Site markup/content quirks",
            "",
            "None found -- VNU reported a clean bill of health on the built site's HTML/CSS.",
            "",
        ]

    lines = [
        "## Site markup/content quirks",
        "",
        f"{len(issues)} distinct issue(s), {vnu_report.get('total_messages', 0)} total, across "
        f"{vnu_report.get('documents_checked', 0)} page(s) checked. These predate the archive -- "
        "defects in the original site's theme or content, not something wpfreeze introduced, "
        "and not something wpfreeze edits automatically. The upside of a static archive: "
        "these files are now yours to hand-edit directly, without needing the original CMS.",
        "",
    ]
    for issue in issues[:_MAX_LISTED]:
        pages = issue.get("pages", [])
        example = pages[0] if pages else "?"
        count = issue.get("count", 0)
        message = issue.get("message", "(no message recorded)")
        lines.append(f"- **{count}x** {message} ({len(pages)} page(s), e.g. `{example}`)")
    if len(issues) > _MAX_LISTED:
        lines.append(f"- ...and {len(issues) - _MAX_LISTED} more distinct issue(s) (see vnu-report.json)")
    lines.append("")
    return lines


def build_cleanup_todo(output_dir: Path) -> str | None:
    """Synthesize the cleanup-todo markdown from whatever of
    build-report.json/vnu-report.json/diagnostics.json exist in
    `output_dir`. Returns None only if none of the three exist -- there is
    nothing yet to say."""
    build_report = _load_json(output_dir / "build-report.json")
    vnu_report = _load_json(output_dir / "vnu-report.json")
    diagnostics = _load_json(output_dir / "diagnostics.json")
    if build_report is None and vnu_report is None and diagnostics is None:
        return None

    integrity_lines, integrity_needs_attention = _integrity_section(diagnostics)
    broken_lines, broken_needs_attention = _broken_reference_section(build_report)
    verify_lines, verify_needs_attention = _verification_section(build_report)
    markup_lines = _markup_quirks_section(vnu_report)
    markup_needs_attention = bool(vnu_report and vnu_report.get("issues"))
    broken_needs_attention = broken_needs_attention or verify_needs_attention

    # An all-clear may only be given for checks that actually ran. A section
    # whose report is absent contributes False to `needs_attention` exactly
    # like a section that ran and found nothing -- so without this the
    # headline cheerfully certifies work never performed, directly
    # contradicting the "hasn't been run yet" prose in its own body.
    unrun = [
        name
        for name, report in (
            ("build", build_report),
            ("validate", vnu_report),
            ("diagnose", diagnostics),
        )
        if report is None
    ]

    if integrity_needs_attention or broken_needs_attention:
        headline = "This capture has a handful of things worth a look before you call it done."
    elif markup_needs_attention:
        headline = (
            "This capture looks structurally sound -- what's left is polish in the "
            "original site's own markup, not anything wpfreeze got wrong."
        )
    elif unrun:
        headline = (
            f"Nothing to flag from the checks that have run -- but {' and '.join(unrun)} "
            f"{'has' if len(unrun) == 1 else 'have'} not, so this is not yet a clean bill "
            "of health. See the sections below."
        )
    else:
        headline = "This capture looks clean: nothing unresolved, no integrity anomalies, no markup issues found."

    lines = [
        "# Cleanup checklist",
        "",
        headline,
        "",
        "For the acquisition-side gap breakdown (missing pages, auth-gated content, "
        "orphaned/unlisted URLs, and what to do about each) see report.html in the same "
        "directory -- this document covers only what became available after "
        "`build`/`validate` ran.",
        "",
    ]
    lines.extend(integrity_lines)
    lines.extend(broken_lines)
    lines.extend(verify_lines)
    lines.extend(markup_lines)
    return "\n".join(lines).rstrip() + "\n"


def write_cleanup_todo(output_dir: Path) -> Path | None:
    """Write the cleanup-todo doc if there's anything to say yet; returns
    the path, or None if neither `build` nor `validate` nor `diagnose`
    have produced any report in `output_dir` yet."""
    content = build_cleanup_todo(output_dir)
    if content is None:
        return None
    path = output_dir / "cleanup-todo.md"
    path.write_text(content, encoding="utf-8")
    return path
