from __future__ import annotations

from wpfreeze.extract import HYPERLINK, RENDER, extract_from_css, extract_from_html

BASE = "https://example.com/blog/post/"


def links_by_context(html: str, context_prefix: str):
    return [l for l in extract_from_html(html, BASE) if l.context.startswith(context_prefix)]


def test_a_href_is_hyperlink():
    links = extract_from_html('<a href="/other/">link</a>', BASE)
    assert links == [
        _link("https://example.com/other/", HYPERLINK, "a[href]"),
    ]


def test_img_src_is_render():
    links = extract_from_html('<img src="/img/photo.jpg">', BASE)
    assert links[0].kind == RENDER
    assert links[0].url == "https://example.com/img/photo.jpg"
    assert links[0].context == "img[src]"


def test_srcset_parses_multiple_candidates_with_descriptors():
    html = (
        '<img src="/img/photo.jpg" '
        'srcset="/img/photo-480.jpg 480w, /img/photo-800.jpg 800w, /img/photo-1200.jpg 1200w">'
    )
    links = extract_from_html(html, BASE)
    srcset_urls = [l.url for l in links if l.context == "img[srcset]"]
    assert srcset_urls == [
        "https://example.com/img/photo-480.jpg",
        "https://example.com/img/photo-800.jpg",
        "https://example.com/img/photo-1200.jpg",
    ]
    assert all(l.kind == RENDER for l in links if l.context == "img[srcset]")


def test_picture_source_srcset():
    html = '<picture><source srcset="/img/a.webp 1x, /img/a-2x.webp 2x"><img src="/img/a.jpg"></picture>'
    links = extract_from_html(html, BASE)
    urls = {l.url for l in links}
    assert "https://example.com/img/a.webp" in urls
    assert "https://example.com/img/a-2x.webp" in urls
    assert "https://example.com/img/a.jpg" in urls


def test_video_poster_and_src_are_render():
    html = '<video src="/vid/movie.mp4" poster="/img/poster.jpg"></video>'
    links = extract_from_html(html, BASE)
    contexts = {l.context: l for l in links}
    assert contexts["video[src]"].kind == RENDER
    assert contexts["video[poster]"].kind == RENDER
    assert contexts["video[poster]"].url == "https://example.com/img/poster.jpg"


def test_object_data_is_render():
    links = extract_from_html('<object data="/embeds/thing.svg"></object>', BASE)
    assert links[0].kind == RENDER
    assert links[0].context == "object[data]"


def test_iframe_src_is_render():
    # Extraction still treats it like any other src-like context (RENDER,
    # unconfined here); the third-party-embed problem is handled downstream
    # by crawl.admit_link's narrower "owned" gate for iframe[src] -- see the
    # comment above extract._RENDER_ATTRS -- not by extraction itself.
    links = extract_from_html('<iframe src="https://www.youtube.com/embed/abc123"></iframe>', BASE)
    assert links[0].kind == RENDER
    assert links[0].context == "iframe[src]"
    assert links[0].url == "https://www.youtube.com/embed/abc123"


def test_form_action_is_hyperlink():
    links = extract_from_html('<form action="/search-results/"></form>', BASE)
    assert links[0].kind == HYPERLINK
    assert links[0].context == "form[action]"


def test_cite_attributes_are_hyperlinks():
    html = (
        '<blockquote cite="/source-1/">x</blockquote>'
        '<q cite="/source-2/">y</q>'
        '<ins cite="/source-3/">z</ins>'
        '<del cite="/source-4/">w</del>'
    )
    links = extract_from_html(html, BASE)
    assert all(l.kind == HYPERLINK for l in links)
    assert {l.url for l in links} == {
        "https://example.com/source-1/",
        "https://example.com/source-2/",
        "https://example.com/source-3/",
        "https://example.com/source-4/",
    }


def test_data_src_lazy_load_attrs_are_render():
    html = (
        '<img data-src="/wp-content/uploads/2024/lazy.jpg" '
        'data-lazy-src="/wp-content/uploads/2024/lazy2.jpg" '
        'data-large-file="https://example.com/wp-content/uploads/2024/full.jpg" '
        'data-bg="/wp-content/uploads/2024/bg.png">'
    )
    links = extract_from_html(html, BASE)
    render_links = [l for l in links if l.kind == RENDER]
    urls = {l.url for l in render_links}
    assert "https://example.com/wp-content/uploads/2024/lazy.jpg" in urls
    assert "https://example.com/wp-content/uploads/2024/lazy2.jpg" in urls
    assert "https://example.com/wp-content/uploads/2024/full.jpg" in urls
    assert "https://example.com/wp-content/uploads/2024/bg.png" in urls


def test_data_attr_embedded_url_within_larger_string_is_found():
    html = '<div data-full-url="Click here: https://example.com/wp-content/uploads/big.png please">x</div>'
    links = extract_from_html(html, BASE)
    assert any(l.url == "https://example.com/wp-content/uploads/big.png" for l in links)


def test_non_url_data_attrs_are_ignored():
    html = '<div data-count="5" data-align="left" data-enabled="true"></div>'
    links = extract_from_html(html, BASE)
    assert links == []


def test_meta_og_image_and_twitter_image_are_render():
    html = (
        '<meta property="og:image" content="/img/share.jpg">'
        '<meta name="twitter:image" content="/img/tw.jpg">'
    )
    links = extract_from_html(html, BASE)
    assert all(l.kind == RENDER for l in links)
    assert {l.url for l in links} == {
        "https://example.com/img/share.jpg",
        "https://example.com/img/tw.jpg",
    }


def test_meta_og_url_is_hyperlink():
    links = extract_from_html('<meta property="og:url" content="/canonical-page/">', BASE)
    assert links[0].kind == HYPERLINK


def test_link_rel_icon_preload_apple_touch_stylesheet_are_render():
    html = (
        '<link rel="icon" href="/favicon.ico">'
        '<link rel="apple-touch-icon" href="/apple.png">'
        '<link rel="preload" href="/font.woff2">'
        '<link rel="stylesheet" href="/style.css">'
    )
    links = extract_from_html(html, BASE)
    assert all(l.kind == RENDER for l in links)
    assert len(links) == 4


def test_link_rel_canonical_next_prev_are_hyperlinks():
    html = (
        '<link rel="canonical" href="/page/">'
        '<link rel="next" href="/page/2/">'
        '<link rel="prev" href="/page/0/">'
    )
    links = extract_from_html(html, BASE)
    assert all(l.kind == HYPERLINK for l in links)
    assert len(links) == 3


def test_inline_style_attribute_url_is_render():
    html = '<div style="background-image: url(\'/img/bg.jpg\')"></div>'
    links = extract_from_html(html, BASE)
    assert links[0].kind == RENDER
    assert links[0].url == "https://example.com/img/bg.jpg"


def test_style_block_urls_are_render():
    html = """
    <style>
        .hero { background: url("/img/hero.jpg"); }
        @import url('/css/extra.css');
        @import "/css/plain-import.css";
    </style>
    """
    links = extract_from_html(html, BASE)
    urls = {l.url for l in links}
    assert "https://example.com/img/hero.jpg" in urls
    assert "https://example.com/css/extra.css" in urls
    assert "https://example.com/css/plain-import.css" in urls
    assert all(l.kind == RENDER for l in links)


def test_script_json_blob_urls_are_render():
    html = """
    <script type="application/json">
    {"gallery": [{"full": "https://example.com/wp-content/uploads/1.jpg", "thumb": "/wp-content/uploads/1-thumb.jpg"}]}
    </script>
    """
    links = extract_from_html(html, BASE)
    urls = {l.url for l in links}
    assert "https://example.com/wp-content/uploads/1.jpg" in urls
    assert "https://example.com/wp-content/uploads/1-thumb.jpg" in urls
    assert all(l.context == "script:json" for l in links)


def test_script_plain_js_with_embedded_url_uses_regex_fallback():
    html = """
    <script>
    var slider = { image: "/wp-content/uploads/slide1.jpg", autoplay: true };
    </script>
    """
    links = extract_from_html(html, BASE)
    assert any(l.url == "https://example.com/wp-content/uploads/slide1.jpg" for l in links)
    assert all(l.context == "script:regex" for l in links)


def test_script_regex_false_positive_does_not_crash_extraction():
    """The URL-shaped regex fallback over raw <script> text has no real
    host validation, so on non-WordPress sites with heavy minified JS it
    will occasionally match ordinary JS syntax that merely looks
    protocol-relative -- e.g. array-index code like `//list[0]/x` -- which
    urlsplit rejects as an invalid IPv6 host. This must be dropped, not
    raise and kill the whole crawl (this exact shape crashed a real run
    against microsoft.com)."""
    html = """
    <script>
    var real = "/wp-content/uploads/real.jpg";
    var garbage = list[0]//not/a/real/host;
    </script>
    """
    links = extract_from_html(html, BASE)  # must not raise
    urls = {l.url for l in links}
    assert "https://example.com/wp-content/uploads/real.jpg" in urls
    assert not any("[0]" in url for url in urls)


def test_script_regex_ignores_cache_exclusion_glob_patterns():
    """Cache/PWA plugins routinely embed a sitewide inline <script> array
    of glob-style cache-exclusion paths (e.g. '/wp-content/uploads/*/').
    The URL-shaped regex fallback must not match past the literal '*' --
    without this, every such glob entry gets queued as if it were a real
    page, wasting a fetch + Wayback-lookup cycle on each one for every
    single page on the site (this exact pattern set was seen on a real
    crawl)."""
    html = """
    <script>
    var excludeFromCache = [
      "/wp-content/uploads/*/",
      "/wp-content/plugins/*/",
      "/wp-content/themes/Divi/*/"
    ];
    </script>
    """
    links = extract_from_html(html, BASE)
    urls = {l.url for l in links}
    assert not any("*" in url for url in urls)


def test_bare_directory_reference_ignored_in_script_regex_json_and_data_attr():
    """A plugin's JS "base path" config value -- a directory reference with
    no filename at all, meant to have filenames concatenated onto it at
    runtime (a webpack publicPath, a CDN assetsUrl, Jetpack's per-feature
    settings, WordPress.com's `_static` concatenator) -- looks identical
    to a real URL to a heuristic scanner, but a real fetchable resource
    always has a filename+extension. On a real WordPress.com-hosted site,
    one sitewide Jetpack settings blob alone produced 9 of these across
    both the script:regex and script:json paths (~70% of that run's
    reported "missing" count), each one a 403/404 on a bare directory,
    not a real gap."""
    html = """
    <script>
    var JETPACK_MU_WPCOM_SETTINGS = {"assetsUrl":"https://s1.wp.com/wp-content/mu-plugins/jetpack-mu-wpcom-plugin/moon/src/build/"};
    </script>
    <script type="application/json">
    {"baseUrl": "https://s0.wp.com/wp-content/mu-plugins/wpcom-smileys/twemoji/2/72x72/", "real": "https://example.com/wp-content/uploads/1.jpg"}
    </script>
    <div data-background-folder="/wp-content/uploads/backgrounds/"></div>
    """
    links = extract_from_html(html, BASE)
    urls = {l.url for l in links}
    assert not any(url.endswith("/") for url in urls)
    assert "https://example.com/wp-content/uploads/1.jpg" in urls


def test_implausibly_long_data_attribute_blob_ignored():
    """A data-* attribute holding a large base64 blob (e.g. a Figma paste
    handler's `<!--(figma)...-->`-wrapped clipboard metadata, stamped onto
    a Divi Toggle module's content when pasted from a Figma frame) is not a
    URL, but a heuristic scanner has no way to know that other than
    structurally: base64 data contains "//" by chance often enough that a
    multi-KB chunk of it gets matched as a protocol-relative URL and queued
    for a fetch that can only ever fail. Seen on a real site: two ~20-30KB
    "URLs" like this, both correctly unfetchable but wasted retries and
    cluttered the gap report."""
    blob = "A" * 40 + "//" + ("B" * 80 + "/") * 400
    html = f'<div data-buffer="&lt;!--(figma){blob}--&gt;"></div>'
    links = extract_from_html(html, BASE)
    assert links == []


def test_skipped_schemes_are_not_extracted():
    html = (
        '<a href="mailto:someone@example.com">mail</a>'
        '<a href="tel:+15551234567">call</a>'
        '<a href="javascript:void(0)">js</a>'
        '<a href="#top">anchor</a>'
        '<img src="data:image/png;base64,AAAA">'
    )
    links = extract_from_html(html, BASE)
    assert links == []


def test_extract_from_css_url_and_import_quoted_and_unquoted():
    css = """
    .a { background: url(/img/one.png); }
    .b { background: url('/img/two.png'); }
    .c { background: url("/img/three.png"); }
    @import url(/css/four.css);
    @import "/css/five.css";
    """
    links = extract_from_css(css, "https://example.com/assets/style.css")
    urls = {l.url for l in links}
    assert urls == {
        "https://example.com/img/one.png",
        "https://example.com/img/two.png",
        "https://example.com/img/three.png",
        "https://example.com/css/four.css",
        "https://example.com/css/five.css",
    }
    assert all(l.kind == RENDER for l in links)


def test_relative_urls_resolve_against_base():
    links = extract_from_html('<a href="../other-post/">x</a>', BASE)
    assert links[0].url == "https://example.com/blog/other-post/"


def _link(url, kind, context):
    from wpfreeze.extract import ExtractedLink

    return ExtractedLink(url, kind, context)


# --- WordPress.com /_static/ concat bundles -------------------------------
#
# The bundle URL carries its component list in the query string, which
# normalize_url discards as a cache-buster -- collapsing every bundle on a
# host onto a bare "https://host/_static/". Expanding at extraction time is
# what keeps those components addressable at all.

def _bundle_url(paths: list[str], host: str = "https://example.com") -> str:
    import base64
    import zlib

    packed = zlib.compress(",".join(paths).encode("utf-8"))
    body = base64.b64encode(packed).decode("ascii").rstrip("=")
    return f"{host}/_static/??-{body}&cssminify=yes"


def test_static_bundle_expands_to_components_and_drops_the_bundle_url():
    from wpfreeze.extract import decode_static_bundle

    paths = ["/wp-content/themes/pub/x/style.css", "/wp-content/mu-plugins/y/widget.css"]
    url = _bundle_url(paths)

    assert decode_static_bundle(url) == [
        "https://example.com/wp-content/themes/pub/x/style.css",
        "https://example.com/wp-content/mu-plugins/y/widget.css",
    ]

    links = extract_from_html(f'<link rel="stylesheet" href="{url}">', BASE)
    assert [l.url for l in links] == [
        "https://example.com/wp-content/themes/pub/x/style.css",
        "https://example.com/wp-content/mu-plugins/y/widget.css",
    ]
    assert all(l.kind == RENDER for l in links)
    assert all(l.context == "link[rel=stylesheet]->static-bundle" for l in links)


def test_static_bundle_components_resolve_against_the_bundles_own_host():
    """A bundle served from a CDN host bundles that host's assets, not the
    page's -- resolving against base_url would silently retarget all of them."""
    from wpfreeze.extract import decode_static_bundle

    url = _bundle_url(["/wp-content/mu-plugins/likes/queuehandler.js"], host="https://s1.wp.com")
    assert decode_static_bundle(url) == ["https://s1.wp.com/wp-content/mu-plugins/likes/queuehandler.js"]


def test_static_bundle_plain_uncompressed_form_is_supported():
    from wpfreeze.extract import decode_static_bundle

    url = "https://example.com/_static/??/wp-content/a.css,/wp-content/b.css"
    assert decode_static_bundle(url) == [
        "https://example.com/wp-content/a.css",
        "https://example.com/wp-content/b.css",
    ]


def test_static_bundle_components_may_carry_their_own_cache_busters():
    from wpfreeze.extract import decode_static_bundle

    url = _bundle_url(["/wp-content/mu-plugins/z.css?m=1681832297j"])
    assert decode_static_bundle(url) == ["https://example.com/wp-content/mu-plugins/z.css?m=1681832297j"]


def test_undecodable_static_bundle_is_left_alone_rather_than_dropped():
    """Better to keep one unfetchable URL (which shows up honestly as a gap)
    than to silently discard a reference we failed to understand."""
    from wpfreeze.extract import decode_static_bundle

    url = "https://example.com/_static/??-not-valid-base64-zlib!!"
    assert decode_static_bundle(url) is None

    links = extract_from_html(f'<link rel="stylesheet" href="{url}">', BASE)
    assert [l.url for l in links] == [url]


def test_ordinary_urls_are_untouched_by_bundle_expansion():
    from wpfreeze.extract import decode_static_bundle

    assert decode_static_bundle("https://example.com/_static/") is None
    assert decode_static_bundle("https://example.com/style.css?ver=1") is None
    assert decode_static_bundle("https://example.com/a/_static/b.css") is None


# --- extension-less asset-tree base paths ---------------------------------

def test_extensionless_asset_tree_paths_in_script_blobs_are_dropped():
    """Divi's inline config emits base paths its JS appends filenames to at
    runtime. They are not resources: fetched, they 403."""
    html = (
        '<script>var et = {"images_uri":"/wp-content/themes/Divi/images",'
        '"builder_uri":"/wp-content/themes/Divi/includes/builder/images"};</script>'
    )
    assert extract_from_html(html, BASE) == []


def test_extensionless_paths_outside_asset_trees_are_kept():
    """An extension-less path elsewhere is an ordinary pretty permalink --
    dropping those would discard real pages."""
    html = '<script>var cfg = {"next":"https://example.com/about/team"};</script>'
    urls = [l.url for l in extract_from_html(html, BASE)]
    assert urls == ["https://example.com/about/team"]


def test_real_assets_inside_asset_trees_are_still_extracted():
    html = '<script>var cfg = {"logo":"/wp-content/uploads/2019/05/logo.png"};</script>'
    urls = [l.url for l in extract_from_html(html, BASE)]
    # Both the wp-path and asset-extension scanners match this, so it is
    # emitted twice; dedup is get_or_create's job, not extraction's.
    assert set(urls) == {"https://example.com/wp-content/uploads/2019/05/logo.png"}


def test_extensionless_asset_tree_path_in_a_real_href_is_unaffected():
    """The bare-directory heuristic guards the JS/data-* scanners only; a
    genuine href/src attribute is an explicit reference and always kept."""
    links = extract_from_html('<a href="/wp-content/themes/Divi/images">x</a>', BASE)
    assert [l.url for l in links] == ["https://example.com/wp-content/themes/Divi/images"]
