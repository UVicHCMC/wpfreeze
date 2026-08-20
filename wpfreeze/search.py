"""Offline search: wire a captured WordPress site's own search form to a
local, fully client-side Pagefind index.

A static archive's WP search form is dead -- its `action` gets rewritten
like any other reference and just navigates to the local homepage with an
ignored `?s=` query string. `policy.strip_search_forms: false` already
lets a site owner keep the form rather than have it stripped; this module
makes the kept form *work*, without changing its appearance or adding any
visible UI until someone actually searches.

Three pieces, run in this order by `wpfreeze build`:

1. `apply_search` -- per page, tag the site's own search form(s) with
   `data-wpfreeze-search` (reusing `policy._is_search_form`'s exact
   fingerprint so the Python and JS sides agree by construction, not by
   coincidence), mark indexable content with `data-pagefind-body` per
   `search.body_selectors`, and inject a `<script type="module">` loader.
2. `write_search_asset` -- writes the fixed runtime JS once per build.
3. `run_pagefind_index` -- shells out to the `pagefind` Python package to
   build the actual index over the finished `site/` tree.

See CLAUDE-search.md for the full design, including why `body_selectors`
is a page-*exclusion* mechanism and not just a region-narrower, and why
the script tag has to be injected after link rewriting rather than
before it.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from wpfreeze.policy import _is_search_form

if TYPE_CHECKING:
    from wpfreeze.cli import SearchSettings

logger = logging.getLogger(__name__)

# Where write_search_asset puts the runtime JS, and where build.py's
# per-page loop computes a relative path to it from. /-rooted, like every
# other output_path in this codebase.
SEARCH_ASSET_PATH = "/assets/pagefind-search.js"
BUNDLE_SUBDIR = "pagefind"


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
.wpfreeze-search-results{display:none;position:relative;z-index:9999;
  max-height:60vh;overflow-y:auto;margin:.5em 0;padding:.75em 1em;
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
  let panel = form.nextElementSibling;
  if (panel && panel.classList.contains("wpfreeze-search-results")) return panel;
  panel = document.createElement("div");
  panel.className = "wpfreeze-search-results";
  panel.setAttribute("role", "region");
  panel.setAttribute("aria-live", "polite");
  panel.setAttribute("aria-label", "Search results");
  form.insertAdjacentElement("afterend", panel);
  return panel;
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
    the unresolved-reference count. See CLAUDE-search.md section 5.

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
        # No selectors configured: Pagefind indexes the whole <body> of
        # every page, so every page counts as "matched" for reporting.
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
    if result is None:
        lines.append("  index               : not built")
    elif result.ok:
        langs = ", ".join(result.languages) if result.languages else "unknown"
        lines.append(f"  index               : {result.pages_indexed} page(s), language(s): {langs}")
    else:
        lines.append(f"  index               : FAILED -- {result.error}")
    return "\n".join(lines)
