from __future__ import annotations

from pathlib import Path

from wpfreeze.dedupe import DedupeStats, InlineCssIndex, extract_shared_css


def _passthrough(css: str, page_url: str, output_path: str) -> str:
    """Stand-in for the build's CSS rewriter: no url() to resolve."""
    return css


def _page(site: Path, rel: str, body: str) -> str:
    """Write a page and return its site-relative output path."""
    dest = site / rel.lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    return "/" + rel.lstrip("/")


def _big(marker: str = "a") -> str:
    return f".{marker}{{color:red}}" + "/*pad*/" * 4000  # ~28 KB


def test_a_block_repeated_across_pages_is_lifted_into_one_file(tmp_path: Path):
    site = tmp_path / "site"
    css = _big()
    style = f"<style>{css}</style>"
    index = InlineCssIndex()
    for i in range(6):
        out = _page(site, f"p{i}.html", f"<p>x</p>{style}")
        index.record(css, style, out, f"https://s/p{i}/")

    stats = DedupeStats()
    extract_shared_css(index, site, _passthrough, stats)

    assert stats.blocks_extracted == 1
    assert stats.pages_updated == 6
    shared = list((site / "assets" / "css").glob("shared-*.css"))
    assert len(shared) == 1
    assert shared[0].read_text() == css

    page = (site / "p0.html").read_text()
    assert "<style>" not in page
    assert 'rel="stylesheet"' in page and shared[0].name in page


def test_the_link_replaces_the_style_in_place_so_cascade_order_is_unchanged(tmp_path: Path):
    """The whole reason this is safe where relocating to <head> is not:
    cascade precedence follows document order, so the replacement has to
    sit exactly where the <style> did."""
    site = tmp_path / "site"
    css = _big()
    style = f"<style>{css}</style>"
    index = InlineCssIndex()
    for i in range(6):
        out = _page(site, f"p{i}.html", f"<p>before</p>{style}<p>after</p>")
        index.record(css, style, out, f"https://s/p{i}/")

    extract_shared_css(index, site, _passthrough, DedupeStats())

    page = (site / "p0.html").read_text()
    assert page.index("before") < page.index("stylesheet") < page.index("after")


def test_a_block_on_a_single_page_is_left_alone(tmp_path: Path):
    site = tmp_path / "site"
    css = _big()
    style = f"<style>{css}</style>"
    index = InlineCssIndex()
    out = _page(site, "only.html", style)
    index.record(css, style, out, "https://s/only/")

    stats = DedupeStats()
    extract_shared_css(index, site, _passthrough, stats)

    assert stats.blocks_extracted == 0
    assert stats.redundant_bytes_seen == 0
    assert "<style>" in (site / "only.html").read_text()


def test_small_repeated_blocks_stay_inline_but_are_still_reported(tmp_path: Path):
    """A shared file plus a request costs more than a few hundred repeated
    bytes -- but the owner should still be told the duplication exists."""
    site = tmp_path / "site"
    css = ".tiny{color:red}"
    style = f"<style>{css}</style>"
    index = InlineCssIndex()
    for i in range(5):
        out = _page(site, f"p{i}.html", style)
        index.record(css, style, out, f"https://s/p{i}/")

    stats = DedupeStats()
    extract_shared_css(index, site, _passthrough, stats)

    assert stats.blocks_extracted == 0
    assert stats.blocks_below_threshold == 1
    assert stats.redundant_bytes_seen > 0  # reported, not hidden
    assert "<style>" in (site / "p0.html").read_text()


def test_a_page_dependent_block_is_never_merged(tmp_path: Path):
    """Identical source CSS whose url() targets resolve differently per
    page must not be merged -- doing so would silently repoint assets."""
    site = tmp_path / "site"
    css = _big()
    style = f"<style>{css}</style>"

    def _page_dependent(css_text: str, page_url: str, output_path: str) -> str:
        return css_text + f"/*{page_url}*/"

    index = InlineCssIndex()
    for i in range(6):
        out = _page(site, f"p{i}.html", style)
        index.record(css, style, out, f"https://s/p{i}/")

    stats = DedupeStats()
    extract_shared_css(index, site, _page_dependent, stats)

    assert stats.blocks_extracted == 0
    assert stats.blocks_context_dependent == 1
    assert not (site / "assets" / "css").exists()
    assert "<style>" in (site / "p0.html").read_text()


def test_hrefs_are_relative_to_each_pages_own_depth(tmp_path: Path):
    site = tmp_path / "site"
    css = _big()
    style = f"<style>{css}</style>"
    index = InlineCssIndex()
    for rel in ("top.html", "a/mid.html", "a/b/deep.html", "c/x.html", "d/y.html", "e/z.html"):
        out = _page(site, rel, style)
        index.record(css, style, out, "https://s/" + rel)

    extract_shared_css(index, site, _passthrough, DedupeStats())

    assert 'href="assets/css/shared-' in (site / "top.html").read_text()
    assert 'href="../assets/css/shared-' in (site / "a" / "mid.html").read_text()
    assert 'href="../../assets/css/shared-' in (site / "a" / "b" / "deep.html").read_text()


def test_shared_filenames_are_content_addressed_so_rebuilds_are_stable(tmp_path: Path):
    site = tmp_path / "site"
    css = _big()
    style = f"<style>{css}</style>"

    names = []
    for _ in range(2):
        index = InlineCssIndex()
        for i in range(6):
            out = _page(site, f"p{i}.html", style)
            index.record(css, style, out, f"https://s/p{i}/")
        extract_shared_css(index, site, _passthrough, DedupeStats())
        names.append(sorted(p.name for p in (site / "assets" / "css").glob("*.css")))

    assert names[0] == names[1]


def test_dedupe_can_be_turned_off(tmp_path: Path):
    """Default-on like the policy strips, but a site owner who wants the
    bytes exactly as the CMS emitted them can say so."""
    from wpfreeze.policy import Policy

    assert Policy().dedupe_inline_css is True
    assert Policy.from_config({"dedupe_inline_css": False}).dedupe_inline_css is False
