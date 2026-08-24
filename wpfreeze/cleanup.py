"""Cleanup-todo synthesis: turn `build`/`validate`/`diagnose`'s machine
reports into a short, human-readable punch list. Written automatically
whenever `build` or `validate` runs (see cli.py's `_announce_cleanup_todo`)
so a site owner is told "here's what's left" without having to remember to
go looking for it.

Deliberately does not duplicate report.py's acquisition-gap breakdown
(missing/auth_gated/orphan/... categories, already generated automatically
at the end of every `acquire` with its own prose). This covers only what
becomes available *after* build/validate run: broken references the build
couldn't resolve, content the build's own policy deliberately excised
(forms, telemetry, feeds, ...), capture-integrity anomalies from
`diagnose`, and the site's own pre-existing markup/content quirks VNU
catches.

All three inputs -- build-report.json, vnu-report.json, diagnostics.json --
are optional and independently present or absent on disk; whatever is
missing is noted, not treated as an error, so this can be regenerated
after only `build`, only `validate`, or both, in either order.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

# Not a truncation cap -- every list in this document is printed in full.
# Used only to decide when a list is long enough to (a) earn an explanatory
# aside in the prose and (b) render collapsed behind <details> in the HTML
# version (see _markdown_to_html); the .md version ignores it entirely.
_LONG_LIST_THRESHOLD = 10

# Same palette/heading treatment as report.py's _CSS, trimmed to what this
# document actually uses (no tables/filters/badges here) so the two reports
# read as one family without cleanup.py importing report.py's private constant.
_CSS = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; max-width: 60rem; }
a { color: #1a56c4; }
a:visited { color: #7a3fa0; }
a:hover { text-decoration: underline; }
h1, h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }
p { line-height: 1.5; }
ul { line-height: 1.6; }
code { font-family: ui-monospace, Menlo, Consolas, monospace; background: #f4f4f4; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }
details { margin-bottom: 1rem; }
summary { cursor: pointer; font-weight: 600; }
details ul { margin-top: 0.5rem; }
@media (prefers-color-scheme: dark) {
  body { background: #1e1e1e; color: #ddd; }
  a { color: #7db3ff; }
  a:visited { color: #d3a6f0; }
  h1, h2 { border-bottom-color: #444; }
  code { background: #2a2a2a; }
}
"""


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


def _site_link(output_path: str) -> str:
    """A path relative to cleanup-todo's own location (output_dir) into the
    built site, or "" if there's no output path to link to (an older
    build-report.json/vnu-report.json, or a sample build.py couldn't
    attribute to a page). Always `site/...`: cleanup-todo.md/.html and
    site/ are siblings inside output_dir regardless of how the result is
    served -- opened directly via file://, or served from a webserver
    rooted at output_dir or at site/ itself."""
    if not output_path:
        return ""
    return f"site/{output_path.lstrip('/')}"


def _linked(label: str, href: str) -> str:
    """Wrap `label` (already backtick-quoted markdown text) in a markdown
    link to `href`, or return it unchanged if there's nothing to link to."""
    return f"[{label}]({href})" if href else label


def _normalize_unresolved_sample(entry) -> dict:
    """build.py's unresolved_samples entries as a stable dict, regardless
    of which historical shape a given build-report.json used: a dict (the
    current format, carrying page_output/target for linking), or the
    (page, value[, context]) tuples/lists two earlier generations of this
    module wrote before those existed."""
    if isinstance(entry, dict):
        return {
            "page": entry.get("page", "?"),
            "value": entry.get("value", "?"),
            "context": entry.get("context", ""),
            "page_output": entry.get("page_output", ""),
            "target": entry.get("target", ""),
        }
    page = entry[0] if len(entry) > 0 else "?"
    value = entry[1] if len(entry) > 1 else "?"
    context = entry[2] if len(entry) > 2 else ""
    return {"page": page, "value": value, "context": context, "page_output": "", "target": ""}


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
    # Older build-report.json files stored (page, value[, context]) tuples;
    # build.py now records a dict with two more fields (page_output, target)
    # so a bullet can link to the local built copy and the live original --
    # normalize both shapes to the same keys, with "" for anything an older
    # report doesn't have (meaning: don't link that part, same as before
    # this existed).
    normalized = [_normalize_unresolved_sample(entry) for entry in samples]
    # Identical (page, value, context) triples are the same broken reference
    # appearing more than once in the markup (a repeated nav link, say) --
    # collapse them rather than printing the same line twice.
    deduped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in normalized:
        key = (entry["page"], entry["value"], entry["context"])
        if key not in seen:
            seen.add(key)
            deduped.append(entry)

    # Context is only shown where it's the sole way to tell two bullets
    # with the same (page, value) apart -- e.g. two different links on one
    # page that both happen to point at the same dead URL. For the common
    # case of a single occurrence it would be pure noise, so it's left out
    # to keep the list short.
    pair_counts: dict[tuple[str, str], int] = {}
    for entry in deduped:
        pair_key = (entry["page"], entry["value"])
        pair_counts[pair_key] = pair_counts.get(pair_key, 0) + 1

    for entry in deduped:
        page, value, context = entry["page"], entry["value"], entry["context"]
        suffix = f', text: "{context}"' if context and pair_counts[(page, value)] > 1 else ""
        value_label = _linked(f"`{value}`", entry["target"])
        page_label = _linked(f"`{page}`", _site_link(entry["page_output"]))
        lines.append(f"- {value_label} (linked from {page_label}{suffix})")
    # `samples` is unbounded going forward (build.py no longer caps
    # unresolved_samples), but a build-report.json written before that
    # change can still have fewer samples than `unresolved` -- keep this
    # as a fallback for stale reports rather than assuming it can't happen.
    remaining = unresolved - len(samples)
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
    for sample in samples:
        source = sample.get("source", "")
        reference = sample.get("reference", "?")
        reason = sample.get("reason", "")
        # `source` is already a site-relative output path (build.py's
        # verify_site walks the built tree, not the manifest), so it can
        # link straight to the local copy with no extra data needed.
        source_label = _linked(f"`{source or '?'}`", _site_link(source))
        lines.append(f"- `{reference}` in {source_label}" + (f" -- {reason}" if reason else ""))
    # `samples` is unbounded going forward (build.py no longer caps
    # broken_samples), but a build-report.json written before that change
    # can still have fewer samples than `broken` -- keep this as a fallback
    # for stale reports rather than assuming it can't happen.
    remaining = broken - len(samples)
    if remaining > 0:
        lines.append(f"- ...and {remaining} more (see build-report.json)")
    lines.append("")
    return (lines, True)


# Order to present form categories in, and what to say about each -- see
# policy.py's _strip_forms/_comment_wrapper for how "comment" is detected
# and why it's the one category whose caption is removed automatically,
# _is_password_form for how "password" is detected, and _is_search_form
# for how "search" is detected. Categories beyond those three (subscribe,
# contact, ...) don't exist yet; anything not recognized buckets as
# "other" until they do.
_FORM_CATEGORY_ORDER = ("comment", "password", "search", "other", "unspecified")
_FORM_CATEGORY_LABEL = {
    "comment": "Comment forms",
    "password": "Password-protected pages",
    "search": "Search forms",
    "other": "Other forms",
    "unspecified": "Forms (from a build made before categorization existed)",
}


def _pages_by_category(forms_pages: dict, category: str) -> dict[str, tuple[int, str]]:
    """`{page: (count, output_path)}` for one category, across all pages in
    forms_removed_pages. Older build-report.json shapes (bare int per page,
    or a dict with no "categories" key) can't be split by category at all --
    their whole count lands under "unspecified" rather than being guessed at
    or silently dropped."""
    result: dict[str, tuple[int, str]] = {}
    for page, info in forms_pages.items():
        if not isinstance(info, dict):
            info = {"count": info}
        output_path = info.get("output_path", "")
        categories = info.get("categories") or {}
        if not categories:
            if category == "unspecified":
                result[page] = (info.get("count", 0), output_path)
            continue
        count = categories.get(category, 0)
        if count:
            result[page] = (count, output_path)
    return result


def _form_category_lines(
    category: str,
    pages: dict[str, tuple[int, str]],
    dead_fragment_links: int = 0,
    comment_count_blurbs: int = 0,
) -> list[str]:
    total = sum(count for count, _ in pages.values())
    label = _FORM_CATEGORY_LABEL[category]
    if category == "comment":
        para = (
            f"**{label}** -- {total} across {len(pages)} page(s). The caption (e.g. \"Leave a "
            "Reply\") and \"Cancel reply\" link were removed along with the form itself, so "
            "these shouldn't need a manual check -- lower priority than the rest of this section."
        )
        extras = []
        if dead_fragment_links:
            extras.append(f"{dead_fragment_links} now-dead in-page link(s) unwrapped")
        if comment_count_blurbs:
            extras.append(f"{comment_count_blurbs} stale \"N comments\" blurb(s) removed")
        if extras:
            para += " Also cleaned up on the same pages: " + ", ".join(extras) + "."
        para += " Full list:"
    elif category == "password":
        para = (
            f"**{label}** -- {total} across {len(pages)} page(s). WordPress itself gates these "
            "posts behind a password; the prompt was the only thing in the page's content region "
            "to begin with, so removing it (dead on a static archive -- there's no live "
            "wp-login.php/wp-pass.php to post to) leaves nothing behind. wpfreeze never had "
            "access to what's actually behind the password. This is not thin or broken content "
            "and there's nothing to fix -- these pages are excluded from \"Pages with thin "
            "content\" below for the same reason. Full list:"
        )
    elif category == "search":
        para = (
            f"**{label}** -- {total} across {len(pages)} page(s), removed like any other form -- it "
            "can't submit anywhere useful on a static archive either. If you're planning to wire up "
            "a replacement rather than just lose search entirely, set `strip_search_forms: false` "
            "in the config and re-run `wpfreeze build` to leave these in place as a starting point. "
            "Or set `search: enabled: true` (alongside `strip_search_forms: false`) and `build` will "
            "wire these forms up itself, indexed with a local Pagefind index -- see \"Offline search\" "
            "in the README. Full list:"
        )
    elif category == "other":
        para = (
            f"**{label}** -- {total} across {len(pages)} page(s): subscribe, contact, or "
            "unrecognized. Only the form was removed -- a heading, label, or \"Subscribe\" button "
            "around it wasn't touched and may now describe nothing. Worth a look:"
        )
    else:
        para = (
            f"**{label}** -- {total} across {len(pages)} page(s). Re-run `wpfreeze build` to sort "
            "these into categories."
        )
    lines = [para, ""]
    # Sorted with multi-form pages first: on a real site nearly every page
    # carries the theme's one boilerplate comment form, so a page with more
    # than one is the more likely outlier worth spotting early in a long list.
    for page, (count, output_path) in sorted(pages.items(), key=lambda kv: (-kv[1][0], kv[0])):
        page_label = _linked(f"`{page}`", _site_link(output_path))
        lines.append(f"- {page_label}" + (f" ({count} form(s))" if count > 1 else ""))
    lines.append("")
    return lines


def _excised_content_section(build_report: dict | None) -> tuple[list[str], bool]:
    """Content the build policy deliberately removed (see policy.py).
    Forms get a per-category, per-page list -- unlike the other three
    strips, a removed <form> can leave a heading, label, or "Subscribe"
    button behind with nothing under it any more. Comment and password
    forms are the exception: a comment form's caption is removed with it
    (see policy.py's _comment_wrapper), and a password form's removal
    empties a page that had nothing else in it to begin with (see
    policy.py's _is_password_form) -- both are broken out and
    deprioritized rather than treated the same as everything else. The
    other three excision types
    (telemetry, feeds, WP protocol-discovery links) are dead <head>/script
    references with nothing rendered either way; a total is enough, listing
    pages would just be noise.
    """
    if build_report is None:
        return ([], False)

    policy = build_report.get("policy") or {}
    forms_removed = policy.get("forms_removed", 0)
    forms_pages = policy.get("forms_removed_pages") or {}
    telemetry = policy.get("telemetry_removed", 0)
    feeds = policy.get("feeds_removed", 0)
    wp_meta = policy.get("wp_meta_links_removed", 0)
    dead_fragment_links = policy.get("dead_fragment_links_removed", 0)
    comment_count_blurbs = policy.get("comment_count_blurbs_removed", 0)
    if not (forms_removed or telemetry or feeds or wp_meta):
        return ([], False)

    lines = ["## Content intentionally excised", ""]
    # Comment forms are auto-cleaned (caption + cancel-link removed with
    # them, see policy.py's _comment_wrapper) and password forms removed
    # nothing but a dead prompt (see policy.py's _is_password_form) --
    # neither needs a manual check the way the rest of this section does,
    # so only some other category should push the "worth a look" headline.
    needs_attention = False
    if forms_removed:
        lines.append(
            f"{forms_removed} form(s) across {len(forms_pages)} page(s) were removed -- none of "
            "them could submit anywhere useful on a static archive."
        )
        lines.append("")
        for cat in _FORM_CATEGORY_ORDER:
            pages = _pages_by_category(forms_pages, cat)
            if pages:
                extra_args = (dead_fragment_links, comment_count_blurbs) if cat == "comment" else (0, 0)
                lines.extend(_form_category_lines(cat, pages, *extra_args))
                if cat not in ("comment", "password"):
                    needs_attention = True

    other = []
    if telemetry:
        other.append(f"{telemetry} telemetry/analytics reference(s)")
    if feeds:
        other.append(f"{feeds} feed link(s)")
    if wp_meta:
        other.append(f"{wp_meta} WP protocol-discovery link(s)")
    if other:
        lines.append(
            "Also removed, with nothing visible either way (dead `<head>`/script "
            "references, not rendered content): " + ", ".join(other) + "."
        )
        lines.append("")

    return (lines, needs_attention)


def _search_forms_kept_section(build_report: dict | None) -> tuple[list[str], bool]:
    """Search forms recognized but deliberately left in place -- only
    populated when `strip_search_forms: false` is set (see policy.py's
    search_forms_kept_pages). Deliberately not part of "Content
    intentionally excised": nothing was removed here, so reporting it
    there would say something false.

    Two outcomes: with `search.enabled: true` and a successful index
    (search.index_ok in the same build report), these forms are actually
    wired up and working -- say so, and there's nothing to flag. Otherwise
    "left in place" doesn't mean "still works" -- see policy.py's own
    strip_search_forms comment for the caveat this section repeats, and
    flag it for a look.
    """
    if build_report is None:
        return ([], False)

    policy = build_report.get("policy") or {}
    kept_pages = policy.get("search_forms_kept_pages") or {}
    if not kept_pages:
        return ([], False)

    def _count(info) -> int:
        return info.get("count", 0) if isinstance(info, dict) else info

    total = sum(_count(info) for info in kept_pages.values())
    search = build_report.get("search") or {}
    if search.get("index_ok"):
        indexed_pages = search.get("indexed_pages", 0)
        lines = [
            "## Search forms left in place",
            "",
            f"{total} search form(s) across {len(kept_pages)} page(s) were left untouched "
            "(`strip_search_forms: false`) and are wired up to a local Pagefind index "
            f"covering {indexed_pages} page(s) (`search: enabled: true`) -- these are "
            "working search boxes, not just raw material. Nothing to do here.",
            "",
        ]
        return (lines, False)

    lines = [
        "## Search forms left in place",
        "",
        f"{total} search form(s) across {len(kept_pages)} page(s) were left untouched "
        "(`strip_search_forms: false`) instead of being removed. This does not make them "
        "functional -- the form's `action` still points at the local homepage once rewritten, "
        "so submitting it as-is does nothing useful. It's raw material for wiring up a "
        "replacement -- set `search: enabled: true` and `wpfreeze build` will index the site "
        "with Pagefind and wire these forms up itself (see \"Offline search\" in the README) -- "
        "not a working search box on its own:",
        "",
    ]
    for page, info in sorted(kept_pages.items(), key=lambda kv: (-_count(kv[1]), kv[0])):
        output_path = info.get("output_path", "") if isinstance(info, dict) else ""
        page_label = _linked(f"`{page}`", _site_link(output_path))
        count = _count(info)
        lines.append(f"- {page_label}" + (f" ({count} form(s))" if count > 1 else ""))
    lines.append("")

    return (lines, True)


def _search_coverage_section(build_report: dict | None) -> tuple[list[str], bool]:
    """Pages search.enabled left out of reach, in one of two distinct ways
    -- explained in prose so a reader knows which fix applies:

    - not indexed at all (no `data-pagefind-body` match against
      `search.body_selectors`) -- usually a too-narrow selector, or an
      archive/attachment page the owner meant to exclude;
    - indexed, but with no search form on the page to reach it from -- a
      template that omits the theme's search widget.

    Emitted only when search is enabled and at least one list is
    non-empty; a page can appear in both.
    """
    if build_report is None:
        return ([], False)

    search = build_report.get("search") or {}
    if not search.get("enabled"):
        return ([], False)

    missing_body = search.get("pages_without_body_match") or []
    missing_form = search.get("pages_without_form") or []
    if not missing_body and not missing_form:
        return ([], False)

    lines = ["## Pages not covered by search", ""]
    if missing_body:
        lines.append(
            f"**Absent from the index** -- {len(missing_body)} page(s) matched none of "
            "`search.body_selectors`, so Pagefind never indexed them: nobody can find them "
            "by searching. Usually a too-narrow selector, or an archive/attachment page you "
            "meant to exclude on purpose. If this list looks wrong, check `body_selectors` "
            "against these pages' actual markup:"
        )
        lines.append("")
        for output_path in sorted(missing_body):
            lines.append(f"- {_linked(f'`{output_path}`', _site_link(output_path))}")
        lines.append("")
    if missing_form:
        lines.append(
            f"**In the index but unreachable** -- {len(missing_form)} page(s) have no "
            "recognized search form, so even though their content is indexed there's nothing "
            "on the page to search from. Usually a template that omits the theme's search "
            "widget:"
        )
        lines.append("")
        for output_path in sorted(missing_form):
            lines.append(f"- {_linked(f'`{output_path}`', _site_link(output_path))}")
        lines.append("")

    return (lines, True)


def _thin_content_section(build_report: dict | None) -> tuple[list[str], bool]:
    """Pages scan_content_issues found indexed but holding almost no text
    -- a blind spot pages_without_body_match can't see, since the selector
    DID match. See CLAUDE-search-content-checks.md sec 4a. `content_issues`
    is None when search is disabled or scan_content_issues hasn't run
    (an older build-report.json); an empty list when it ran and found
    nothing to flag -- both produce no section, deliberately not
    distinguished here the same way the other search sections don't.
    """
    if build_report is None:
        return ([], False)
    content_issues = build_report.get("content_issues")
    if not content_issues:
        return ([], False)
    thin_pages = content_issues.get("thin_pages") or []
    if not thin_pages:
        return ([], False)

    lines = [
        "## Pages with thin content",
        "",
        f"{len(thin_pages)} page(s) matched a body selector but hold almost no text once "
        "indexed -- present in search, but adding noise rather than findable content:",
        "",
    ]
    for entry in sorted(thin_pages, key=lambda e: (e.get("word_count", 0), e.get("page", ""))):
        output_path = entry.get("page", "")
        word_count = entry.get("word_count", 0)
        page_label = _linked(f"`{output_path}`", _site_link(output_path))
        lines.append(f"- {page_label} ({word_count} word(s))")
    lines.append("")

    return (lines, True)


def _echoed_content_section(build_report: dict | None) -> tuple[list[str], bool]:
    """Pages scan_content_issues found mostly duplicating other pages --
    typically a WordPress archive/category/blog-listing page (native or
    hand-built) whose indexed text is assembled from other pages' content,
    so one piece of content competes with itself in search results. See
    CLAUDE-search-content-checks.md sec 4b for the echo-fraction algorithm.
    """
    if build_report is None:
        return ([], False)
    content_issues = build_report.get("content_issues")
    if not content_issues:
        return ([], False)
    echoed_pages = content_issues.get("echoed_pages") or []
    if not echoed_pages:
        return ([], False)

    lines = [
        "## Pages that mostly duplicate other pages",
        "",
        f"{len(echoed_pages)} page(s) have indexed text that is mostly shared with other "
        "pages -- usually an archive/category/blog-listing page assembled from other pages' "
        "content, native WordPress template or hand-built. Leaving these indexed means one "
        "piece of content competes with itself in results. If a page here can't be excluded "
        "with `search.body_selectors`/`ignore_selectors` (a hand-built listing page is often "
        "structurally identical to a real one), add its path to `search.exclude_pages`:",
        "",
    ]
    for entry in sorted(echoed_pages, key=lambda e: -(e.get("echo_fraction") or 0)):
        output_path = entry.get("page", "")
        fraction = entry.get("echo_fraction") or 0
        sources = entry.get("sources") or []
        sample = entry.get("sample", "")
        page_label = _linked(f"`{output_path}`", _site_link(output_path))
        source_labels = ", ".join(_linked(f"`{s}`", _site_link(s)) for s in sources[:3])
        detail = f"{fraction * 100:.0f}% shared"
        if source_labels:
            detail += f" with {source_labels}"
            if len(sources) > 3:
                detail += f" (+{len(sources) - 3} more)"
        line = f"- {page_label} -- {detail}"
        if sample:
            line += f': "{sample}"'
        lines.append(line)
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
    for issue in issues:
        pages = issue.get("pages", [])
        example = pages[0] if pages else ""
        count = issue.get("count", 0)
        message = issue.get("message", "(no message recorded)")
        # `pages` are already site-relative output paths (validate.py's
        # _page_relpath), so this links straight to the local copy.
        example_label = _linked(f"`{example or '?'}`", _site_link(example))
        lines.append(f"- **{count}x** {message} ({len(pages)} page(s), e.g. {example_label})")
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
    excised_lines, excised_needs_attention = _excised_content_section(build_report)
    kept_search_lines, kept_search_needs_attention = _search_forms_kept_section(build_report)
    coverage_lines, coverage_needs_attention = _search_coverage_section(build_report)
    thin_content_lines, thin_content_needs_attention = _thin_content_section(build_report)
    echoed_content_lines, echoed_content_needs_attention = _echoed_content_section(build_report)
    markup_lines = _markup_quirks_section(vnu_report)
    markup_needs_attention = bool(vnu_report and vnu_report.get("issues"))
    broken_needs_attention = (
        broken_needs_attention
        or verify_needs_attention
        or excised_needs_attention
        or kept_search_needs_attention
        or coverage_needs_attention
        or thin_content_needs_attention
        or echoed_content_needs_attention
    )

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
    lines.extend(excised_lines)
    lines.extend(kept_search_lines)
    lines.extend(coverage_lines)
    lines.extend(thin_content_lines)
    lines.extend(echoed_content_lines)
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


def _inline_markdown_to_html(text: str) -> str:
    """Escape `text`, then re-open the three inline spans this module's
    markdown actually uses: **bold**, `code`, and [text](url) links.
    Escaping first means content inside a span is safe even if it contains
    HTML-special characters (a URL with a bare `&`, say) -- including
    inside an href, where the escaped form (`&amp;`) is exactly what's
    correct in an HTML attribute anyway. The markdown delimiters themselves
    (`*`, backtick, `[`/`]`/`(`/`)`) aren't touched by html.escape, so the
    substitutions still find them afterwards. Links open in a new tab: this
    document is meant to be clicked through item by item without losing
    your place."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', escaped)
    return escaped


def _markdown_to_html(markdown: str) -> str:
    """Render cleanup-todo's markdown body to HTML. Deliberately not a
    general markdown renderer -- covers only the subset build_cleanup_todo
    emits: '# '/'## ' headings, blank-line-separated paragraphs, '- '
    bullet lists, and inline **bold**/`code`/[text](url) spans.

    Nothing here is ever cut short -- every list item build_cleanup_todo
    produced is rendered. A list longer than _LONG_LIST_THRESHOLD is
    wrapped in a collapsed <details> instead, so a 500-item list doesn't
    dominate the page while still being one click away, not gone.
    """
    body: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            body.append(f"<p>{_inline_markdown_to_html(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if not list_items:
            return
        items_html = "\n".join(f"<li>{_inline_markdown_to_html(item)}</li>" for item in list_items)
        if len(list_items) > _LONG_LIST_THRESHOLD:
            body.append(
                f"<details><summary>{len(list_items)} items -- click to expand</summary>\n"
                f"<ul>\n{items_html}\n</ul></details>"
            )
        else:
            body.append(f"<ul>\n{items_html}\n</ul>")
        list_items.clear()

    for line in markdown.split("\n"):
        if line.startswith("## "):
            flush_paragraph()
            flush_list()
            body.append(f"<h2>{_inline_markdown_to_html(line[3:])}</h2>")
        elif line.startswith("# "):
            flush_paragraph()
            flush_list()
            body.append(f"<h1>{_inline_markdown_to_html(line[2:])}</h1>")
        elif line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:])
        elif line.strip() == "":
            flush_paragraph()
            flush_list()
        else:
            paragraph.append(line)
    flush_paragraph()
    flush_list()
    return "\n".join(body)


def build_cleanup_todo_html(output_dir: Path) -> str | None:
    """The same content as build_cleanup_todo, as a standalone HTML page
    styled like report.html. Returns None under the same condition:
    nothing in `output_dir` for either format to report yet."""
    markdown = build_cleanup_todo(output_dir)
    if markdown is None:
        return None
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cleanup checklist</title>
<style>{_CSS}</style>
</head>
<body>
{_markdown_to_html(markdown)}
</body>
</html>
"""


def write_cleanup_todo_html(output_dir: Path) -> Path | None:
    """Write the cleanup-todo HTML page if there's anything to say yet;
    returns the path, or None if neither `build` nor `validate` nor
    `diagnose` have produced any report in `output_dir` yet."""
    content = build_cleanup_todo_html(output_dir)
    if content is None:
        return None
    path = output_dir / "cleanup-todo.html"
    path.write_text(content, encoding="utf-8")
    return path
