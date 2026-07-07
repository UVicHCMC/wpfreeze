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
