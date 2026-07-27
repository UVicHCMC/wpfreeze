"""Lift inline <style> blocks that repeat verbatim across pages into shared
stylesheet files.

WordPress themes -- Divi especially -- inline the same compiled CSS into
every page. On real captures that is not a rounding error: 93% of
landscapes' 51 MB of inline CSS is redundant, and a single 88 KB block
appears verbatim on 232 pages. A site meant to *replace* the original
should not ship 20 MB of one repeated stylesheet.

**Why this is safe when relocating a <style> to <head> is not.** Cascade
precedence follows the document order of the <link>/<style> elements, so
swapping one for the other *at the same position* is cascade-neutral --
nothing moves. Relocating to <head> changes document order and can change
which rule wins. As a bonus the swap also fixes the "style not allowed as
child of body" error, because <link rel=stylesheet> is body-ok in the HTML
spec while <style> is not: the same defect, fixed by the safe route.

Two conditions guard extraction:

- **Exact match only.** Divi emits per-page variants; merging
  near-identical blocks would mean diffing rule sets and guessing which
  differences matter. Exact matches already capture the bulk.
- **Context independence.** A block's url() targets are resolved relative
  to the page carrying it, so the same source block on two pages can point
  at two different files. A block is only extracted when it rewrites to
  byte-identical CSS from every page that carries it -- proven, not
  assumed, by rewriting it once per page and comparing.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Below this, a shared file plus an HTTP request costs more than the bytes
# it saves. Measured on redundancy -- (copies - 1) * size -- not raw size.
MIN_REDUNDANT_BYTES = 50 * 1024

SHARED_CSS_DIR = "/assets/css"


@dataclass
class DedupeStats:
    blocks_extracted: int = 0
    #  Distinct pages touched, not (block, page) pairs -- one page commonly
    #  carries several extracted blocks.
    pages_updated: int = 0
    bytes_saved: int = 0
    #  Redundancy found, whether or not it was extracted -- so a site owner
    #  is told about duplication that fell below the threshold or was
    #  context-dependent, rather than it silently not being addressed.
    redundant_bytes_seen: int = 0
    blocks_below_threshold: int = 0
    blocks_context_dependent: int = 0


@dataclass
class _Occurrence:
    output_path: str      # site-relative path of the page holding it
    page_url: str         # the page's original URL, for url() resolution
    emitted_text: str     # exactly what was written, for byte replacement


@dataclass
class InlineCssIndex:
    """Records every inline <style> block seen during a build."""

    raw_by_hash: dict[str, str] = field(default_factory=dict)
    occurrences: dict[str, list[_Occurrence]] = field(default_factory=lambda: defaultdict(list))

    def record(self, raw_css: str, emitted_text: str, output_path: str, page_url: str) -> None:
        if not raw_css.strip():
            return
        digest = hashlib.sha256(raw_css.encode("utf-8")).hexdigest()[:12]
        self.raw_by_hash.setdefault(digest, raw_css)
        self.occurrences[digest].append(_Occurrence(output_path, page_url, emitted_text))


def _shared_output_path(digest: str) -> str:
    return f"{SHARED_CSS_DIR}/shared-{digest}.css"


def _relative_href(from_output_path: str, to_output_path: str) -> str:
    from posixpath import relpath

    from_dir = from_output_path.rsplit("/", 1)[0] or "/"
    return relpath(to_output_path, from_dir)


def extract_shared_css(
    index: InlineCssIndex,
    site_dir: Path,
    rewrite_css,
    stats: DedupeStats,
    min_redundant_bytes: int = MIN_REDUNDANT_BYTES,
) -> None:
    """Write shared stylesheets and repoint the pages that carried them.

    `rewrite_css(css_text, page_url, output_path)` is the build's own CSS
    rewriter, passed in so this module does not need to know how references
    are resolved -- and so it can be given a throwaway stats object, since
    these rewrites are bookkeeping and must not inflate the build's own
    reference counts.
    """
    touched_pages: set[str] = set()
    for digest, occurrences in index.occurrences.items():
        raw = index.raw_by_hash[digest]
        size = len(raw.encode("utf-8"))
        redundant = (len(occurrences) - 1) * size
        if redundant <= 0:
            continue
        stats.redundant_bytes_seen += redundant

        if redundant < min_redundant_bytes:
            stats.blocks_below_threshold += 1
            continue

        shared_path = _shared_output_path(digest)
        # Rewrite the block once *per carrying page*, all targeting the
        # shared file's location. Identical results prove the block does not
        # depend on which page it sits in; differing results mean its url()
        # targets resolve differently per page, and merging would silently
        # repoint assets.
        rewritten = {rewrite_css(raw, occ.page_url, shared_path) for occ in occurrences}
        if len(rewritten) != 1:
            stats.blocks_context_dependent += 1
            continue
        shared_css = rewritten.pop()

        destination = site_dir / shared_path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(shared_css, encoding="utf-8")

        updated_pages = set()
        for occ in occurrences:
            page_file = site_dir / occ.output_path.lstrip("/")
            if not page_file.exists():
                continue
            html = page_file.read_text(encoding="utf-8", errors="replace")
            if occ.emitted_text not in html:
                continue
            href = _relative_href(occ.output_path, shared_path)
            link = f'<link rel="stylesheet" href="{href}">'
            # Replace every occurrence: if a page carries the same block
            # twice, both positions become links to the same file and the
            # cascade is unchanged at each.
            html = html.replace(occ.emitted_text, link)
            page_file.write_text(html, encoding="utf-8")
            updated_pages.add(occ.output_path)

        if updated_pages:
            stats.blocks_extracted += 1
            touched_pages |= updated_pages
            stats.bytes_saved += redundant
    stats.pages_updated = len(touched_pages)
