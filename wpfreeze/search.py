"""Offline search: wire a captured WordPress site's own search form to a
local, fully client-side Pagefind index.

A static archive's WP search form is dead -- its `action` gets rewritten
like any other reference and just navigates to the local homepage with an
ignored `?s=` query string. `policy.strip_search_forms: false` already
lets a site owner keep the form rather than have it stripped; this module
makes the kept form *work*, without changing its appearance or adding any
visible UI until someone actually searches.

Five pieces, run in this order by `wpfreeze build`:

1. `apply_search` -- per page, tag the site's own search form(s) with
   `data-wpfreeze-search` (reusing `policy._is_search_form`'s exact
   fingerprint so the Python and JS sides agree by construction, not by
   coincidence), mark indexable content with `data-pagefind-body` per
   `search.body_selectors` (skipping pages listed in
   `search.exclude_pages` entirely), and inject a `<script type="module">`
   loader.
2. `write_search_asset` -- writes the fixed runtime JS once per build.
3. `run_pagefind_index` -- shells out to the `pagefind` Python package to
   build the actual index over the finished `site/` tree.
4. `scan_content_issues` -- re-parses the finished `site/` tree to flag
   two things `pages_without_body_match` can't see: a page that matched a
   selector but holds almost no text ("thin"), and a page whose indexed
   text is mostly *other* pages' text -- a WordPress archive/listing page
   echoing post teasers, most often. Read-only reporting, same as
   `pages_without_body_match`; never changes what gets indexed.

See the offline-search design notes for the offline-search design, and
the content-checks design notes for scan_content_issues's own design
(algorithm, calibrated constants, why pairwise containment was tried and
rejected) -- including why `body_selectors` is a page-*exclusion*
mechanism and not just a region-narrower, and why the script tag has to
be injected after link rewriting rather than before it.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from wpfreeze.policy import _PASSWORD_PROTECTED_MARKER, _is_search_form

if TYPE_CHECKING:
    from wpfreeze.cli import SearchSettings

logger = logging.getLogger(__name__)

# Where write_search_asset puts the runtime JS, and where build.py's
# per-page loop computes a relative path to it from. /-rooted, like every
# other output_path in this codebase.
SEARCH_ASSET_PATH = "/assets/pagefind-search.js"
BUNDLE_SUBDIR = "pagefind"

# scan_content_issues's constants -- calibrated against two real sites
# (two real WordPress sites), not guessed in the abstract.
# See the content-checks design notes sec 4c for the measured distribution
# behind these numbers (a clean gap between ~0.30 and ~0.50 on both,
# unrelated, sites) and sec 8 for why this is fixed-constant reporting,
# not a config knob, in v1.
SHINGLE_SIZE = 5
MIN_WORDS = 25
ECHO_THRESHOLD = 0.5

_PUNCT_RE = re.compile(r"[^\w\s]")


class SearchUnavailable(Exception):
    """The `pagefind` package is missing, or the indexer subprocess could
    not be run or its output could not be parsed -- a setup problem, not
    an indexing result. Mirrors validate.VnuUnavailable."""


@dataclass
class SearchStats:
    """Counts and per-page detail for the build summary and the cleanup
    checklist. Full lists, not samples: the checklist collapses anything
    over 10 items behind <details> automatically."""

    enabled: bool = False
    pages_seen: int = 0
    pages_with_body_match: int = 0
    pages_without_body_match: list[str] = field(default_factory=list)  # output paths
    # Pages skipped entirely by search.exclude_pages -- kept separate from
    # pages_without_body_match on purpose: that list means "the selector
    # should have matched and didn't, go look"; this one means "the owner
    # already decided about this page, nothing to review". See
    # the content-checks design notes sec 8a.
    pages_excluded_by_config: list[str] = field(default_factory=list)  # output paths
    forms_tagged: int = 0
    pages_without_form: list[str] = field(default_factory=list)  # output paths
    indexed_pages: int = 0
    languages: list[str] = field(default_factory=list)
    index_ok: bool = False
    index_error: str = ""


@dataclass(frozen=True)
class SearchIndexResult:
    ok: bool
    pages_indexed: int = 0
    languages: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class ThinContentPage:
    """A page scan_content_issues found indexed but holding almost no
    text -- matched a selector (so pages_without_body_match can't see it)
    but has fewer than MIN_WORDS words after ignore_selectors removal."""

    page: str  # output path
    word_count: int
    # True when `page` is listed in search.acknowledged_thin_pages -- a
    # site owner has already confirmed this page's shortness is by
    # design, not broken content. Still reported (see ContentIssues'
    # own docstring on why nothing here is ever silently dropped), but
    # cleanup.py's checklist stops counting it toward "needs a look".
    acknowledged: bool = False


@dataclass(frozen=True)
class EchoedPage:
    """A page whose indexed text is mostly shared with other pages --
    typically a WordPress archive/category/blog-listing page (native or
    hand-built) that re-embeds other pages' content as teasers. See
    the content-checks design notes sec 4b for the corpus-wide echo-
    fraction algorithm and why pairwise containment was tried and
    rejected (it missed the truncated-teaser case entirely)."""

    page: str  # output path -- the aggregator/duplicate page
    echo_fraction: float
    shingle_count: int  # denominator, for context in the report
    sources: list[str] = field(default_factory=list)  # output paths, most-contributing first
    # Longest contiguous echoed word run -- normalized (lowercased,
    # punctuation stripped), NOT the original text verbatim. Normalization
    # isn't token-preserving ("Al-Fazia," -> two normalized tokens), so
    # there's no cheap map back to the source string; good enough to let
    # an owner recognize the duplicated content, not attempted as true
    # verbatim. See _longest_echoed_run.
    sample: str = ""


@dataclass
class ContentIssues:
    """scan_content_issues's result. Deliberately NOT a field on
    SearchStats: run_search_index re-indexes an already-built site
    without running build_site's per-page loop at all, so it never has a
    SearchStats to extend. See the content-checks design notes sec 2."""

    # Pages that were actually indexed (thin + shingle-eligible) --
    # deliberately excludes pages extract_indexed_text returned None for
    # (not indexed at all; that's pages_without_body_match's business) and
    # pages policy.py emptied by removing a WordPress password prompt
    # (already reported under "Password-protected pages"; see
    # _PASSWORD_PROTECTED_MARKER). This is the denominator the design doc's sec 4c calibration note
    # needs: ">15% of pages flagged is evidence the threshold is wrong,
    # not that 15% of pages are broken" is unusable without it, which is
    # why format_content_issues_summary reports counts against this total
    # rather than bare counts.
    pages_scanned: int = 0
    thin_pages: list[ThinContentPage] = field(default_factory=list)
    echoed_pages: list[EchoedPage] = field(default_factory=list)


_ASSET_TEMPLATE = """\
// Written by `wpfreeze build`. Wires a captured WordPress site's own
// search form(s) to a local Pagefind index. Loaded as an ES module, so
// import.meta.url is this file's own URL and every path below is fixed
// regardless of which page loaded it -- the site stays portable between
// a document root and a subdirectory.
//
// Requires being served over http(s): dynamic module imports and the
// index's fetch()es are both blocked under file://.

const BUNDLE_DIR = new URL("../pagefind/", import.meta.url);
const SITE_ROOT = new URL("../", import.meta.url);
const MAX_RESULTS = 10;

let pagefind = null;
let loadError = null;

async function ensurePagefind() {
  if (pagefind || loadError) return pagefind;
  try {
    const mod = await import(new URL("pagefind.js", BUNDLE_DIR).href);
    await mod.options({ bundlePath: BUNDLE_DIR.href });
    await mod.init();
    pagefind = mod;
  } catch (err) {
    loadError = err;
    console.warn("wpfreeze search: Pagefind failed to load", err);
  }
  return pagefind;
}

function injectStyles() {
  if (document.getElementById("wpfreeze-search-style")) return;
  const style = document.createElement("style");
  style.id = "wpfreeze-search-style";
  // Layout and legibility only -- type and colour are inherited from the
  // theme wherever a value would be a guess. The panel needs its own
  // background because it can be dropped over arbitrary theme markup.
  style.textContent = `
.wpfreeze-search-results{display:none;position:absolute;top:100%;left:0;
  z-index:9999;width:max(20rem,100%);max-width:min(28rem,calc(100vw - 2rem));
  max-height:60vh;overflow-y:auto;margin:.5em 0 0;padding:.75em 1em;
  background:#fff;color:#222;border:1px solid rgba(0,0,0,.2);
  border-radius:3px;box-shadow:0 2px 8px rgba(0,0,0,.18);
  font-size:.9rem;line-height:1.4;text-align:left}
.wpfreeze-search-results[data-open="1"]{display:block}
.wpfreeze-search-results ol{list-style:none;margin:0;padding:0}
.wpfreeze-search-results li{margin:0 0 .75em}
.wpfreeze-search-results a{color:inherit;text-decoration:underline}
.wpfreeze-search-results mark{background:#ffe680;color:inherit}
.wpfreeze-search-status{margin:0 0 .5em;font-weight:600}
.wpfreeze-search-close{float:right;border:0;background:none;
  cursor:pointer;font-size:1.1em;line-height:1;color:inherit}`;
  document.head.appendChild(style);
}

function panelFor(form) {
  let panel = form.querySelector(".wpfreeze-search-results");
  if (panel) return panel;
  // Appended INSIDE the form (not as a sibling after it) and the form
  // itself made the positioning anchor: CSS position:absolute only
  // anchors to a positioned ANCESTOR, not a sibling, so a panel inserted
  // after the form could never correctly float relative to it. A header
  // search widget's own container is often only a couple hundred pixels
  // wide (a real captured site measured 141px) -- without this the panel
  // took on that width in normal document flow and pushed the rest of
  // the page down instead of floating above it. position:relative with
  // no offset is visually inert for a form that's normally block/
  // inline-block, so this doesn't change the form's own layout.
  if (getComputedStyle(form).position === "static") {
    form.style.position = "relative";
  }
  panel = document.createElement("div");
  panel.className = "wpfreeze-search-results";
  panel.setAttribute("role", "region");
  panel.setAttribute("aria-live", "polite");
  panel.setAttribute("aria-label", "Search results");
  form.appendChild(panel);
  return panel;
}

function positionPanel(form, panel) {
  // Flip to right-anchored when the form sits close enough to the right
  // edge that a left-anchored panel would run off-screen -- a header
  // search widget is commonly positioned near the right edge (measured
  // on a real site: 80px of room to the right of the form, well under
  // the panel's own ~320px minimum width). Re-checked on every open, not
  // cached, since the viewport can be resized between searches.
  const rect = form.getBoundingClientRect();
  const spaceRight = window.innerWidth - rect.left;
  if (spaceRight < 340) {
    panel.style.left = "auto";
    panel.style.right = "0";
  } else {
    panel.style.left = "0";
    panel.style.right = "auto";
  }
}

function localHref(url) {
  // Pagefind returns root-absolute paths ("/page.html"); resolve them
  // against the site root this script sits in, not the server root.
  return new URL("." + url, SITE_ROOT).href;
}

function render(panel, status, results) {
  panel.textContent = "";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "wpfreeze-search-close";
  close.setAttribute("aria-label", "Close search results");
  close.textContent = "×";
  close.addEventListener("click", () => panel.removeAttribute("data-open"));
  panel.appendChild(close);

  const heading = document.createElement("p");
  heading.className = "wpfreeze-search-status";
  heading.textContent = status;
  panel.appendChild(heading);

  if (results && results.length) {
    const list = document.createElement("ol");
    for (const item of results) {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = localHref(item.url);
      link.textContent = (item.meta && item.meta.title) || item.url;
      li.appendChild(link);
      const excerpt = document.createElement("p");
      // The only innerHTML in this file. Pagefind's excerpt is HTML with
      // <mark> tags and pre-encoded entities -- see its API docs.
      excerpt.innerHTML = item.excerpt;
      li.appendChild(excerpt);
      list.appendChild(li);
    }
    panel.appendChild(list);
  }
  panel.setAttribute("data-open", "1");
}

async function runSearch(form, input) {
  const panel = panelFor(form);
  positionPanel(form, panel);
  const query = (input.value || "").trim();
  if (!query) {
    render(panel, "Type something to search for.", null);
    return;
  }
  injectStyles();
  render(panel, "Searching…", null);
  const pf = await ensurePagefind();
  if (!pf) {
    render(panel, "Search is unavailable on this copy of the site.", null);
    return;
  }
  const search = await pf.search(query);
  const total = search.results.length;
  const data = await Promise.all(
    search.results.slice(0, MAX_RESULTS).map((r) => r.data())
  );
  const status = total
    ? `${total} result${total === 1 ? "" : "s"} for “${query}”` +
      (total > MAX_RESULTS ? ` (showing ${MAX_RESULTS})` : "")
    : `No results for “${query}”.`;
  render(panel, status, data);
}

function wire(form) {
  const input =
    form.querySelector('input[name="s"]') ||
    form.querySelector('input[type="search"]') ||
    form.querySelector('input[type="text"]');
  if (!input) return null;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch(form, input);
  });
  // Warm the index on intent, not on every page load: init() pulls the
  // wasm and metadata, which is wasted on a page nobody searches from.
  input.addEventListener("focus", ensurePagefind, { once: true });
  return input;
}

function start() {
  injectStyles();
  const forms = document.querySelectorAll("form[data-wpfreeze-search]");
  let first = null;
  forms.forEach((form) => {
    const input = wire(form);
    if (input && !first) first = { form, input };
  });
  // Honour a ?s= query string on load: a bookmark, or a submit that got
  // through before this script ran.
  const q = new URLSearchParams(window.location.search).get("s");
  if (q && first) {
    first.input.value = q;
    runSearch(first.form, first.input);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
"""


def apply_search(
    soup: BeautifulSoup,
    settings: SearchSettings,
    stats: SearchStats,
    script_src: str,
    page_output: str = "",
) -> None:
    """Apply the three build-time markup changes to one parsed page, in
    place. Must be called AFTER the page's references have already been
    rewritten to local paths -- the injected <script src=...> is already a
    correct relative local path, and if the rewriter sees it first it will
    try (and fail) to resolve it against the manifest lookup, inflating
    the unresolved-reference count. See the offline-search design notes section 5.

    `page_output` is only used to record output paths in the stats lists;
    it defaults to "" so tests can call this with a bare soup and no page
    identity, exactly as policy.apply_policy allows.
    """
    stats.pages_seen += 1

    tagged_here = 0
    for form in soup.find_all("form"):
        if _is_search_form(form):
            form["data-wpfreeze-search"] = "1"
            tagged_here += 1
    stats.forms_tagged += tagged_here
    if not tagged_here:
        stats.pages_without_form.append(page_output)

    if page_output in settings.exclude_pages:
        # Skip the body_selectors loop entirely: no data-pagefind-body
        # means this page drops out of the index the same way a genuine
        # selector-mismatch page already does (sitewide tagging). No new
        # exclusion mechanism -- this just opts a page out of the existing
        # one. Does not touch the form-tagging above: an excluded page can
        # still host a working search box, only its own content stops
        # being indexed. See the content-checks design notes sec 8a.
        stats.pages_excluded_by_config.append(page_output)
    else:
        matched_here = False
        for selector in settings.body_selectors:
            for element in soup.select(selector):
                element["data-pagefind-body"] = ""
                matched_here = True
        if settings.body_selectors:
            if matched_here:
                stats.pages_with_body_match += 1
            else:
                stats.pages_without_body_match.append(page_output)
        else:
            # No selectors configured: Pagefind indexes the whole <body>
            # of every page, so every page counts as "matched" for
            # reporting.
            stats.pages_with_body_match += 1

    script_tag = soup.new_tag("script", type="module", src=script_src)
    body = soup.find("body")
    (body or soup).append(script_tag)


def write_search_asset(site_dir: Path) -> Path:
    """Write the fixed runtime JS to site_dir/assets/pagefind-search.js,
    once per build. Content is identical for every site -- no per-page
    templating -- so this is called once, not per record."""
    path = site_dir / SEARCH_ASSET_PATH.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ASSET_TEMPLATE, encoding="utf-8")
    return path


def run_pagefind_index(
    site_dir: Path, settings: SearchSettings, timeout: float = 1800.0
) -> SearchIndexResult:
    """Run Pagefind's indexer over the finished site directory, mirroring
    validate._run_vnu's subprocess style. Deletes any existing
    site_dir/pagefind first: Pagefind writes content-hashed chunk files,
    and without this a stale build's chunks linger forever and end up
    rsynced to a staging host by upload.sh.
    """
    if importlib.util.find_spec("pagefind") is None:
        raise SearchUnavailable(
            "the `pagefind` package is not installed; run "
            "`pip install 'wpfreeze[search]'` (or, for a pipx install, "
            "`pipx inject wpfreeze 'pagefind[extended]'`)"
        )

    shutil.rmtree(site_dir / BUNDLE_SUBDIR, ignore_errors=True)

    cmd = [sys.executable, "-m", "pagefind", "--site", str(site_dir)]
    if settings.ignore_selectors:
        cmd += ["--exclude-selectors", ", ".join(settings.ignore_selectors)]
    if settings.force_language:
        cmd += ["--force-language", settings.force_language]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SearchUnavailable(f"pagefind indexing timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise SearchUnavailable(f"could not launch pagefind: {exc}") from exc

    logger.debug("pagefind stdout: %s", result.stdout)

    if result.returncode != 0:
        return SearchIndexResult(ok=False, error=result.stderr.strip() or f"exit code {result.returncode}")

    entry_path = site_dir / BUNDLE_SUBDIR / "pagefind-entry.json"
    try:
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        languages = entry.get("languages") or {}
        pages_indexed = sum(lang.get("page_count", 0) for lang in languages.values())
        return SearchIndexResult(ok=True, pages_indexed=pages_indexed, languages=tuple(languages.keys()))
    except (OSError, json.JSONDecodeError, AttributeError):
        # A successful run (returncode 0) whose entry file we can't read is
        # still a success -- report unknown counts, don't fail a run that
        # actually completed.
        return SearchIndexResult(ok=True)


def format_search_summary(stats: SearchStats, result: SearchIndexResult | None) -> str:
    if not stats.enabled:
        return ""
    lines = ["Search:"]
    forms_missing = len(stats.pages_without_form)
    lines.append(
        f"  search forms wired : {stats.forms_tagged} across "
        f"{stats.pages_seen - forms_missing} page(s)"
        + (f" ({forms_missing} page(s) have no form)" if forms_missing else "")
    )
    missing_body = len(stats.pages_without_body_match)
    lines.append(
        f"  content marked     : {stats.pages_with_body_match} page(s) matched a body "
        f"selector" + (f", {missing_body} did not (not searchable)" if missing_body else "")
    )
    excluded = len(stats.pages_excluded_by_config)
    if excluded:
        lines.append(f"  excluded by config : {excluded} page(s) (search.exclude_pages)")
    if result is None:
        lines.append("  index               : not built")
    elif result.ok:
        langs = ", ".join(result.languages) if result.languages else "unknown"
        lines.append(f"  index               : {result.pages_indexed} page(s), language(s): {langs}")
    else:
        lines.append(f"  index               : FAILED -- {result.error}")
    return "\n".join(lines)


def _normalize_words(text: str) -> list[str]:
    return _PUNCT_RE.sub(" ", text.lower()).split()


def _shingles(words: list[str], k: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + k]) for i in range(len(words) - k + 1)}


def _longest_echoed_run(words: list[str], echoed: set[tuple[str, ...]], k: int, max_chars: int = 200) -> str:
    """The longest contiguous run of words whose every k-word shingle is
    in `echoed`, joined back into text -- the sample shown in the report.
    A set intersection alone can't produce this (it has no notion of
    adjacency); this walks the page's own ordered word sequence instead.

    `words` is the caller's *normalized* word list (see _normalize_words),
    so the returned sample is normalized too -- lowercased, punctuation
    stripped -- not the original page text verbatim. See EchoedPage.sample
    for why that's an accepted limitation, not an oversight."""
    flags = [tuple(words[i : i + k]) in echoed for i in range(len(words) - k + 1)]
    best_start = best_len = run_start = run_len = 0
    for i, ok in enumerate(flags):
        if ok:
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_len = 0
    if best_len == 0:
        return ""
    sample = " ".join(words[best_start : best_start + best_len + k - 1])
    return sample if len(sample) <= max_chars else sample[:max_chars].rstrip() + "…"


def extract_indexed_text(
    soup: BeautifulSoup, settings: "SearchSettings", body_tagged_sitewide: bool
) -> str | None:
    """The text Pagefind will actually index for this page: the
    data-pagefind-body region(s) when the site uses them, else the whole
    <body>, minus anything settings.ignore_selectors would exclude.
    (ignore_selectors is mirrored here because --exclude-selectors only
    ever reaches the real indexer -- a shared 'related posts' widget the
    owner already excluded via ignore_selectors must not produce a false
    echo flag.)

    Returns None when the page is not indexed at all -- sitewide tagging
    is on (see scan_content_issues) and this page has no tagged region.
    That's pages_without_body_match's business, not this function's.
    Returns "" (not None) for a page that IS indexed but whose region
    holds no text after ignore_selectors removal: that's thin content,
    and the two cases must not be conflated by the caller.

    Operates on a deep copy of the matched region(s), so ignore_selectors
    removal never mutates the parsed soup -- this function only reads.

    get_text() excluding <script>/<style> content (rather than dumping
    their contents in as words) depends on bs4 classifying those as
    Script/Stylesheet NavigableString subclasses, added in bs4 4.9 --
    pinned in pyproject.toml. Confirmed directly against bs4 4.12.3 rather
    than assumed from changelog wording.
    """
    if body_tagged_sitewide:
        elements = soup.select("[data-pagefind-body]")
        if not elements:
            return None
    else:
        body = soup.find("body")
        elements = [body] if body is not None else [soup]

    parts = []
    for element in elements:
        clone = copy.copy(element)
        for ignore_selector in settings.ignore_selectors:
            for excluded in clone.select(ignore_selector):
                excluded.decompose()
        text = clone.get_text(separator=" ", strip=True)
        if text:
            parts.append(text)
    return " ".join(parts)


def _detect_sitewide_tagging(html_paths: list[Path]) -> bool:
    """Whether ANY page site-wide carries data-pagefind-body -- Pagefind's
    own sitewide rule (the offline-search design notes sec 2): if any page has the
    attribute, every page site-wide is restricted to tagged regions, and
    an untagged page is dropped from the index entirely rather than
    falling back to whole-body.

    A cheap substring pre-filter on raw file text (no parsing) rules out
    files that plainly can't match, since a real site either tags
    everywhere or nowhere -- but the substring alone is not proof: a page
    whose *visible text* happens to mention "data-pagefind-body" (this
    file's own documentation, say) would false-positive on substring
    matching alone. Only files containing the substring get parsed, and
    only the real attribute selector decides -- confirmed via the
    existing soup.select_one check, same as before. Never holds more than
    one parsed soup at a time; a full corpus held simultaneously measured
    at ~11x the raw HTML on real captures.
    """
    for path in html_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "data-pagefind-body" not in text:
            continue
        # html.parser, not html5lib (which build.py itself writes pages
        # with) -- deliberate, not an oversight: this only ever re-parses
        # build.py's own already-well-formed html5lib output, so the
        # stricter/slower parser buys nothing here, and this function
        # re-parses every file in the site at least once already.
        soup = BeautifulSoup(text, "html.parser")
        if soup.select_one("[data-pagefind-body]") is not None:
            return True
    return False


def scan_content_issues(
    site_dir: Path,
    settings: "SearchSettings",
    *,
    min_words: int = MIN_WORDS,
    shingle_size: int = SHINGLE_SIZE,
    echo_threshold: float = ECHO_THRESHOLD,
) -> ContentIssues:
    """Re-parses every HTML file already written to site_dir to see what
    Pagefind will actually index per page -- the same source of truth
    run_pagefind_index indexes from, not an in-memory approximation of
    it. Called from run_build (after build_site writes the site) and from
    run_search_index, so re-indexing after an ignore_selectors tweak gets
    fresh detection with no rebuild. (body_selectors/exclude_pages changes
    still need apply_search to re-tag markup, so those still need a
    rebuild -- same existing constraint as indexing itself.)

    See the content-checks design notes for the full design: sec 2 for why
    this reads the finished site_dir rather than in-memory build state and
    the sitewide-tagging correctness trap handled below, sec 4 for the
    thin-content and echo-fraction algorithms.
    """
    html_paths = sorted(p for p in site_dir.rglob("*.html") if p.is_file())

    body_tagged_sitewide = _detect_sitewide_tagging(html_paths)

    # Second pass: extract per page, one soup at a time -- never holding
    # more than one page's parsed DOM alongside the (much smaller)
    # accumulated word lists, unlike an earlier version of this function
    # that parsed every file up front and held every soup simultaneously.
    thin_pages: list[ThinContentPage] = []
    words_by_page: dict[str, list[str]] = {}
    for path in html_paths:
        try:
            # html.parser, not html5lib -- see _detect_sitewide_tagging's
            # comment on the same choice above.
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        except (OSError, UnicodeDecodeError):
            continue
        output_path = "/" + path.relative_to(site_dir).as_posix()
        body = soup.find("body")
        if body is not None and body.has_attr(_PASSWORD_PROTECTED_MARKER):
            # policy.py's _strip_forms removed a WordPress password prompt
            # here -- the prompt WAS this page's entire content, so an
            # empty/near-empty result is correct, not thin. Already
            # reported, with the same page list, under "Content
            # intentionally excised" -> "Password-protected pages"; skip
            # it here rather than double-report it as something to fix.
            continue
        text = extract_indexed_text(soup, settings, body_tagged_sitewide)
        if text is None:
            continue  # not indexed at all -- pages_without_body_match's business
        words = _normalize_words(text)
        if len(words) < min_words:
            acknowledged = output_path in settings.acknowledged_thin_pages
            thin_pages.append(ThinContentPage(page=output_path, word_count=len(words), acknowledged=acknowledged))
            continue
        words_by_page[output_path] = words

    shingles_by_page = {page: _shingles(words, shingle_size) for page, words in words_by_page.items()}
    index: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for page, shingles in shingles_by_page.items():
        for shingle in shingles:
            index[shingle].append(page)

    echoed_pages: list[EchoedPage] = []
    for page, shingles in shingles_by_page.items():
        if not shingles:
            # Guarded belt-and-suspenders: MIN_WORDS > SHINGLE_SIZE means
            # this shouldn't happen via the thin-content filter above, but
            # a caller passing custom min_words/shingle_size could still
            # hit it, and a page with no shingles can't be "echoed".
            continue
        echoed = {shingle for shingle in shingles if len(index[shingle]) > 1}
        fraction = len(echoed) / len(shingles)
        if fraction < echo_threshold:
            continue
        source_counts: dict[str, int] = defaultdict(int)
        for shingle in echoed:
            for other in index[shingle]:
                if other != page:
                    source_counts[other] += 1
        sources = [p for p, _ in sorted(source_counts.items(), key=lambda kv: -kv[1])]
        sample = _longest_echoed_run(words_by_page[page], echoed, shingle_size)
        echoed_pages.append(
            EchoedPage(
                page=page,
                echo_fraction=fraction,
                shingle_count=len(shingles),
                sources=sources,
                sample=sample,
            )
        )

    return ContentIssues(
        pages_scanned=len(words_by_page) + len(thin_pages),
        thin_pages=thin_pages,
        echoed_pages=echoed_pages,
    )


def format_content_issues_summary(issues: ContentIssues) -> str:
    """Two more lines in the same style/column width as
    format_search_summary -- callers print them as a continuation of that
    block (no repeated "Search:" header), see cli.py's run_build.

    Both counts are reported against issues.pages_scanned (not bare
    counts) because the design doc's sec 4c calibration guidance --
    ">15% of pages flagged is evidence the threshold is wrong, not that
    15% of pages are broken" -- is unusable without the denominator.
    """
    scanned = issues.pages_scanned
    acknowledged = sum(1 for p in issues.thin_pages if p.acknowledged)
    ack_note = f" ({acknowledged} acknowledged)" if acknowledged else ""
    lines = [
        f"  thin content       : {len(issues.thin_pages)} of {scanned} page(s) scanned, "
        f"under {MIN_WORDS} words{ack_note}"
    ]
    lines.append(
        f"  echoed content     : {len(issues.echoed_pages)} of {scanned} page(s) scanned, "
        "mostly duplicating other pages"
    )
    return "\n".join(lines)
