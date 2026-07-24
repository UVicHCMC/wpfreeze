from __future__ import annotations

import hashlib
from pathlib import Path

from wpfreeze.build import (
    BuildStats,
    LinkRewriter,
    build_lookup,
    build_site,
    lookup_variants,
    relative_link,
)
from wpfreeze.manifest import Manifest, Status

BASE = "https://example.com"


def _fetched(
    manifest: Manifest,
    url: str,
    output_path: str,
    body: bytes = b"",
    local_path: str | None = None,
    content_type: str = "text/html",
    status: str = Status.FETCHED.value,
):
    record = manifest.get_or_create(url)
    record.status = status
    record.http_status = 200
    record.output_path = output_path
    record.local_path = local_path or ("raw" + output_path)
    record.content_type = content_type
    record.content_hash = hashlib.sha256(body).hexdigest()
    return record


def _write(output_dir: Path, record, body: bytes) -> None:
    path = output_dir / record.local_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


# --- lookup ---------------------------------------------------------------


def test_cache_buster_queries_fall_back_to_the_canonical_record():
    """The single highest-value behaviour in this module: WordPress asks for
    ?ver=/?m=/?cssminify= variants of files the manifest holds canonically.
    Exact-query matching alone resolves under a third of a real site."""
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/wp-content/style.css", "/wp-content/style.css", content_type="text/css")
    lookup = build_lookup(manifest)

    for asked in (
        f"{BASE}/wp-content/style.css?ver=6.4",
        f"{BASE}/wp-content/style.css?m=1720530689i",
        f"{BASE}/wp-content/style.css?m=1&cssminify=yes",
    ):
        assert any(v in lookup for v in lookup_variants(asked)), asked


def test_permalink_query_keys_are_not_collapsed_away():
    """?p=/?cat= identify distinct archive pages -- they must not fall back
    to the bare path the way a cache-buster does. Falling back would
    resolve them to whatever unrelated record already occupies that bare
    path (e.g. the homepage) instead of merely leaving them unresolved."""
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    lookup = build_lookup(manifest)

    variants = lookup_variants(f"{BASE}/?cat=5")
    assert variants[0] == f"{BASE}/?cat=5"
    assert f"{BASE}/" not in variants


def test_identity_query_alias_does_not_hijack_the_bare_path():
    """Real-world case: an author-archive page's ugly-permalink alias
    (?author=6) sits at the bare site root path with no path of its own.
    Its lookup entry must never claim the homepage's own bare-URL spelling
    -- every "Home" link on the site would otherwise resolve to the author
    archive instead, regardless of manifest ordering."""
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/author/michael/", "/author/michael.html")
    manifest.get_or_create(f"{BASE}/author/michael/").add_alias(f"{BASE}/?author=6")
    _fetched(manifest, f"{BASE}/", "/index.html")

    lookup = build_lookup(manifest)
    assert lookup[f"{BASE}/"] == "/index.html"


def test_scheme_www_and_trailing_slash_spellings_all_resolve():
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/about/", "/about.html")
    lookup = build_lookup(manifest)

    for asked in (
        "https://example.com/about/",
        "https://example.com/about",
        "http://example.com/about/",
        "https://www.example.com/about/",
    ):
        assert any(v in lookup for v in lookup_variants(asked)), asked


def test_only_fetched_records_enter_the_lookup():
    """A reference to something the capture never got must stay visibly
    broken rather than point at a file that isn't there."""
    manifest = Manifest()
    missing = manifest.get_or_create(f"{BASE}/gone/")
    missing.status = Status.MISSING.value
    missing.output_path = "/gone.html"
    assert build_lookup(manifest) == {}


# --- relative links -------------------------------------------------------


def test_relative_link_depth():
    assert relative_link("/index.html", "/about.html") == "about.html"
    assert relative_link("/a/b.html", "/style.css") == "../style.css"
    assert relative_link("/a/b/c.html", "/assets/x.png") == "../../assets/x.png"
    assert relative_link("/a/b.html", "/a/c.html") == "c.html"


# --- rewriting ------------------------------------------------------------


def _rewriter(manifest: Manifest) -> tuple[LinkRewriter, BuildStats]:
    stats = BuildStats()
    return LinkRewriter(build_lookup(manifest), stats), stats


def test_internal_links_become_relative_and_survive_depth():
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    _fetched(manifest, f"{BASE}/about/team/", "/about/team.html")
    rewriter, stats = _rewriter(manifest)

    html = f'<a href="{BASE}/">home</a><a href="/about/team/">team</a>'
    out = rewriter.rewrite_html(html, f"{BASE}/about/team/", "/about/team.html")

    assert 'href="../index.html"' in out
    assert 'href="team.html"' in out
    assert stats.rewritten == 2


def test_fragments_are_preserved_across_rewriting():
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/about/", "/about.html")
    rewriter, _ = _rewriter(manifest)

    out = rewriter.rewrite_html(f'<a href="{BASE}/about/#staff">x</a>', f"{BASE}/", "/index.html")
    assert 'href="about.html#staff"' in out


def test_external_references_are_left_absolute():
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    rewriter, stats = _rewriter(manifest)

    html = '<a href="https://other.example.org/page">out</a>'
    assert rewriter.rewrite_html(html, f"{BASE}/", "/index.html").count("other.example.org") == 1
    assert stats.left_absolute == 1
    assert stats.rewritten == 0


def test_unresolved_internal_reference_is_left_alone_and_counted():
    """Leaving it pointing at the live site is the honest failure: it keeps
    working until the site goes away, and the count says how many there are."""
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    rewriter, stats = _rewriter(manifest)

    html = f'<a href="{BASE}/never-captured/">x</a>'
    out = rewriter.rewrite_html(html, f"{BASE}/", "/index.html")

    assert "/never-captured/" in out
    assert stats.unresolved == 1


def test_skipped_schemes_are_untouched():
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    rewriter, stats = _rewriter(manifest)

    html = '<a href="mailto:x@example.com">m</a><a href="#top">t</a><img src="data:image/gif;base64,AAA">'
    out = rewriter.rewrite_html(html, f"{BASE}/", "/index.html")

    assert "mailto:x@example.com" in out and 'href="#top"' in out and "data:image/gif" in out
    assert stats.skipped == 3
    assert stats.rewritten == 0


def test_srcset_candidates_are_rewritten_with_descriptors_intact():
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    for size in (480, 800):
        _fetched(
            manifest,
            f"{BASE}/wp-content/uploads/p-{size}.jpg",
            f"/wp-content/uploads/p-{size}.jpg",
            content_type="image/jpeg",
        )
    rewriter, _ = _rewriter(manifest)

    html = '<img srcset="/wp-content/uploads/p-480.jpg 480w, /wp-content/uploads/p-800.jpg 800w">'
    out = rewriter.rewrite_html(html, f"{BASE}/", "/index.html")

    assert "wp-content/uploads/p-480.jpg 480w" in out
    assert "wp-content/uploads/p-800.jpg 800w" in out


def test_css_url_and_inline_style_are_rewritten():
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    _fetched(manifest, f"{BASE}/wp-content/bg.png", "/wp-content/bg.png", content_type="image/png")
    rewriter, _ = _rewriter(manifest)

    css = rewriter.rewrite_css("body{background:url('/wp-content/bg.png?ver=2')}", f"{BASE}/style.css", "/style.css")
    assert "url('wp-content/bg.png')" in css

    out = rewriter.rewrite_html(
        '<div style="background:url(/wp-content/bg.png)"></div>', f"{BASE}/", "/index.html"
    )
    assert "wp-content/bg.png" in out


# --- end to end -----------------------------------------------------------


def test_build_site_emits_a_tree_with_no_broken_local_references(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()

    home = _fetched(manifest, f"{BASE}/", "/index.html")
    about = _fetched(manifest, f"{BASE}/about/", "/about.html")
    style = _fetched(
        manifest, f"{BASE}/wp-content/style.css", "/wp-content/style.css", content_type="text/css"
    )
    logo = _fetched(
        manifest, f"{BASE}/wp-content/logo.png", "/wp-content/logo.png", content_type="image/png"
    )

    _write(output_dir, home, f'<link rel="stylesheet" href="{BASE}/wp-content/style.css?ver=3">'
                             f'<a href="{BASE}/about/">about</a>'.encode())
    _write(output_dir, about, f'<a href="{BASE}/">home</a><img src="/wp-content/logo.png">'.encode())
    _write(output_dir, style, b"body{background:url('/wp-content/logo.png')}")
    _write(output_dir, logo, b"\x89PNG")

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.pages == 2 and stats.assets == 2
    assert stats.unresolved == 0
    assert (site_dir / "index.html").exists()
    assert (site_dir / "wp-content/logo.png").read_bytes() == b"\x89PNG"

    # The cache-busted stylesheet reference resolved to the real file.
    assert 'href="wp-content/style.css"' in (site_dir / "index.html").read_text()
    # A page one level down reaches back up correctly.
    assert 'src="wp-content/logo.png"' in (site_dir / "about.html").read_text()


def test_build_site_never_writes_into_the_capture(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    original = f'<a href="{BASE}/">home</a>'.encode()
    _write(output_dir, home, original)

    build_site(manifest, output_dir, site_dir)

    assert (output_dir / home.local_path).read_bytes() == original


# --- verification ---------------------------------------------------------
#
# verify_site knows nothing about the manifest or the rewriter: it re-reads
# the emitted tree and resolves against real files. A check sharing the
# assumptions of the code it checks agrees with bugs instead of catching them.

from wpfreeze.build import verify_site  # noqa: E402


def _site(tmp_path: Path, files: dict[str, str]) -> Path:
    site = tmp_path / "site"
    for name, body in files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return site


def test_verify_passes_when_every_local_reference_resolves(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": '<a href="about.html">a</a><link rel="stylesheet" href="css/s.css">',
        "about.html": '<a href="index.html">home</a><img src="img/x.png">',
        "css/s.css": "body{background:url('../img/x.png')}",
        "img/x.png": "PNG",
    })

    report = verify_site(site)

    assert report.ok
    assert report.broken == []
    assert report.checked == 5


def test_verify_reports_a_missing_target_with_its_source(tmp_path: Path):
    site = _site(tmp_path, {"a/page.html": '<img src="../img/gone.png">'})

    report = verify_site(site)

    assert len(report.broken) == 1
    broken = report.broken[0]
    assert broken.source == "a/page.html"
    assert broken.target == "img/gone.png"
    assert broken.reason == "missing"


def test_verify_flags_references_escaping_the_site_root(tmp_path: Path):
    site = _site(tmp_path, {"page.html": '<img src="../../etc/passwd">'})

    report = verify_site(site)

    assert [b.reason for b in report.broken] == ["escapes site root"]


def test_verify_ignores_external_and_non_resource_references(tmp_path: Path):
    site = _site(tmp_path, {
        "page.html": '<a href="https://example.org/x">e</a><a href="//cdn.example.org/y">p</a>'
                     '<a href="mailto:a@b.c">m</a><a href="#top">t</a>',
    })

    report = verify_site(site)

    assert report.ok
    assert report.external == 2
    assert report.skipped == 2
    assert report.checked == 0


def test_verify_treats_query_strings_as_a_static_server_would(tmp_path: Path):
    """A static server ignores the query: the file either exists or doesn't."""
    site = _site(tmp_path, {
        "page.html": '<link rel="stylesheet" href="s.css?ver=2">',
        "s.css": "body{}",
    })

    assert verify_site(site).ok


def test_verify_checks_srcset_candidates_and_css_files(tmp_path: Path):
    site = _site(tmp_path, {
        "page.html": '<img srcset="a.png 480w, missing.png 800w">',
        "a.png": "PNG",
        "s.css": "body{background:url(gone.png)}",
    })

    report = verify_site(site)

    assert {b.target for b in report.broken} == {"missing.png", "gone.png"}


# --- concat bundle reassembly ---------------------------------------------

import base64  # noqa: E402
import zlib  # noqa: E402


def _bundle(paths: list[str], host: str = BASE) -> str:
    body = base64.b64encode(zlib.compress(",".join(paths).encode())).decode().rstrip("=")
    return f"{host}/_static/??-{body}&cssminify=yes"


def test_bundle_is_reassembled_in_order_from_captured_components(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    first = _fetched(manifest, f"{BASE}/wp-content/a.css", "/wp-content/a.css", content_type="text/css")
    second = _fetched(manifest, f"{BASE}/wp-content/b.css", "/wp-content/b.css", content_type="text/css")

    url = _bundle(["/wp-content/a.css", "/wp-content/b.css"])
    _write(output_dir, home, f'<link rel="stylesheet" href="{url}">'.encode())
    _write(output_dir, first, b"body{color:red}")
    _write(output_dir, second, b"body{color:blue}")

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.bundles_reassembled == 1
    bundles = list((site_dir / "assets/bundles").glob("*.css"))
    assert len(bundles) == 1
    text = bundles[0].read_text()
    # Cascade order preserved: the later component must win.
    assert text.index("color:red") < text.index("color:blue")
    assert "../assets/bundles" not in (site_dir / "index.html").read_text()
    assert "assets/bundles" in (site_dir / "index.html").read_text()


def test_bundle_css_urls_are_rebased_onto_the_bundles_location(tmp_path: Path):
    """A component's bytes move to /assets/bundles/, so a relative url()
    inside it resolves from there -- not from where the component lived."""
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    css = _fetched(
        manifest, f"{BASE}/wp-content/themes/x/style.css",
        "/wp-content/themes/x/style.css", content_type="text/css",
    )
    font = _fetched(
        manifest, f"{BASE}/wp-content/themes/x/f.woff", "/wp-content/themes/x/f.woff",
        content_type="font/woff",
    )

    url = _bundle(["/wp-content/themes/x/style.css"])
    _write(output_dir, home, f'<link rel="stylesheet" href="{url}">'.encode())
    _write(output_dir, css, b"@font-face{src:url(f.woff)}")
    _write(output_dir, font, b"WOFF")

    build_site(manifest, output_dir, site_dir)

    bundle = next((site_dir / "assets/bundles").glob("*.css"))
    assert "url(../../wp-content/themes/x/f.woff)" in bundle.read_text()
    assert verify_site(site_dir).ok


def test_uncaptured_bundle_components_are_noted_not_silently_dropped(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    present = _fetched(manifest, f"{BASE}/wp-content/a.css", "/wp-content/a.css", content_type="text/css")

    url = _bundle(["/wp-content/a.css", "/wp-content/never-fetched.css"])
    _write(output_dir, home, f'<link rel="stylesheet" href="{url}">'.encode())
    _write(output_dir, present, b"body{color:red}")

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.bundles_reassembled == 1
    assert stats.bundle_components_missing == 1
    bundle = next((site_dir / "assets/bundles").glob("*.css"))
    assert "not captured" in bundle.read_text() and "never-fetched.css" in bundle.read_text()


def test_bundle_with_nothing_captured_is_left_alone(tmp_path: Path):
    """Better an honest broken reference than an empty file pretending to
    be the site's stylesheet."""
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    url = _bundle(["/wp-content/gone.css"])
    _write(output_dir, home, f'<link rel="stylesheet" href="{url}">'.encode())

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.bundles_reassembled == 0
    assert not (site_dir / "assets/bundles").exists()
    assert "/_static/??" in (site_dir / "index.html").read_text()


def test_same_bundle_on_many_pages_is_written_once(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    url = _bundle(["/wp-content/a.css"])
    css = _fetched(manifest, f"{BASE}/wp-content/a.css", "/wp-content/a.css", content_type="text/css")
    _write(output_dir, css, b"body{}")
    for name in ("", "one/", "two/"):
        page = _fetched(manifest, f"{BASE}/{name}", f"/{name or 'index'}".rstrip("/") + ".html")
        _write(output_dir, page, f'<link rel="stylesheet" href="{url}">'.encode())

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.bundles_reassembled == 1
    assert len(list((site_dir / "assets/bundles").glob("*"))) == 1


def test_javascript_bundles_get_a_js_extension(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    js = _fetched(
        manifest, f"{BASE}/wp-content/a.js", "/wp-content/a.js",
        content_type="application/javascript",
    )
    url = _bundle(["/wp-content/a.js"])
    _write(output_dir, home, f'<script src="{url}"></script>'.encode())
    _write(output_dir, js, b"var a=1;")

    build_site(manifest, output_dir, site_dir)

    assert len(list((site_dir / "assets/bundles").glob("*.js"))) == 1


def test_unresolved_css_urls_are_absolutised_when_the_css_is_relocated(tmp_path: Path):
    """Bundling moves a component's bytes into /assets/bundles/. A relative
    url() that resolved under the component's own directory would silently
    start resolving under the bundle directory, so it must be absolutised
    rather than left alone."""
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    css = _fetched(
        manifest, f"{BASE}/wp-content/plugins/p/style.css",
        "/wp-content/plugins/p/style.css", content_type="text/css",
    )
    url = _bundle(["/wp-content/plugins/p/style.css"])
    _write(output_dir, home, f'<link rel="stylesheet" href="{url}">'.encode())
    # icons/x.svg was never captured, so it cannot be rewritten to a local path
    _write(output_dir, css, b".a{background:url(icons/x.svg)}")

    build_site(manifest, output_dir, site_dir)

    bundle = next((site_dir / "assets/bundles").glob("*.css")).read_text()
    assert f"url({BASE}/wp-content/plugins/p/icons/x.svg)" in bundle
    assert "url(icons/x.svg)" not in bundle


def test_unrelocated_css_leaves_unresolved_urls_untouched(tmp_path: Path):
    """A file staying where it was served from keeps its references as
    written -- they still point at the live site and still work."""
    manifest = Manifest()
    _fetched(manifest, f"{BASE}/", "/index.html")
    rewriter, _ = _rewriter(manifest)

    out = rewriter.rewrite_css(
        ".a{background:url(icons/x.svg)}", f"{BASE}/wp-content/s.css", "/wp-content/s.css"
    )
    assert "url(icons/x.svg)" in out


# --- attachment page retargeting & redirects ------------------------------

from wpfreeze.manifest import FLAG_ATTACHMENT_PAGE  # noqa: E402


def _attachment_page(manifest: Manifest, url: str, media_href: str) -> object:
    """An attachment wrapper: no output_path, HTML linking the shown image
    to the full media file."""
    record = manifest.get_or_create(url)
    record.status = Status.FETCHED.value
    record.http_status = 200
    record.output_path = None
    record.local_path = "raw/att/" + url.rstrip("/").rsplit("/", 1)[-1] + ".html"
    record.content_type = "text/html"
    record.content_hash = ""
    record.add_flag(FLAG_ATTACHMENT_PAGE)
    return record


def test_links_to_attachment_pages_retarget_to_the_media_file(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()

    home = _fetched(manifest, f"{BASE}/", "/index.html")
    media = _fetched(
        manifest, f"{BASE}/wp-content/uploads/2014/spring.jpg",
        "/wp-content/uploads/2014/spring.jpg", content_type="image/jpeg",
    )
    att = _attachment_page(manifest, f"{BASE}/news/story/attachment/spring/", media.url)

    _write(output_dir, home, f'<a href="{BASE}/news/story/attachment/spring/">photo</a>'.encode())
    _write(output_dir, media, b"JPG")
    (output_dir / att.local_path).parent.mkdir(parents=True, exist_ok=True)
    (output_dir / att.local_path).write_text(
        f'<a href="{media.url}"><img src="{BASE}/wp-content/uploads/2014/spring-300x200.jpg"></a>'
    )

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.attachment_links_retargeted == 1
    out = (site_dir / "index.html").read_text()
    assert 'href="wp-content/uploads/2014/spring.jpg"' in out
    assert "attachment" not in out
    # The wrapper page itself is never written.
    assert not (site_dir / "news").exists()


def test_attachment_map_ignores_wrappers_whose_media_was_not_captured(tmp_path: Path):
    """No confident target means the link stays honestly unresolved rather
    than pointing at a file that isn't there."""
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    att = _attachment_page(manifest, f"{BASE}/story/attachment/gone/", f"{BASE}/wp-content/uploads/gone.jpg")

    _write(output_dir, home, f'<a href="{BASE}/story/attachment/gone/">x</a>'.encode())
    (output_dir / att.local_path).parent.mkdir(parents=True, exist_ok=True)
    (output_dir / att.local_path).write_text(
        f'<a href="{BASE}/wp-content/uploads/gone.jpg"><img src="x"></a>'
    )

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.attachment_links_retargeted == 0
    assert stats.unresolved == 1
    assert "/story/attachment/gone/" in (site_dir / "index.html").read_text()


def test_redirects_htaccess_is_copied_into_the_site_as_dot_htaccess(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    _write(output_dir, home, b"<html></html>")
    (output_dir / "redirects.htaccess").write_text("Redirect 301 /old /index.html\n")

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.redirects_copied is True
    assert (site_dir / ".htaccess").read_text() == "Redirect 301 /old /index.html\n"


def test_build_without_a_redirects_file_does_not_fail(tmp_path: Path):
    output_dir, site_dir = tmp_path / "capture", tmp_path / "site"
    manifest = Manifest()
    home = _fetched(manifest, f"{BASE}/", "/index.html")
    _write(output_dir, home, b"<html></html>")

    stats = build_site(manifest, output_dir, site_dir)

    assert stats.redirects_copied is False
    assert not (site_dir / ".htaccess").exists()
