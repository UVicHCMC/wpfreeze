from __future__ import annotations

from bs4 import BeautifulSoup

from wpfreeze.policy import Policy, PolicyStats, apply_policy


def _apply(html: str, policy: Policy | None = None) -> tuple[str, PolicyStats]:
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, policy or Policy(), stats)
    return str(soup), stats


# --- telemetry: external references ---------------------------------------


def test_external_analytics_scripts_are_removed_by_host():
    html = (
        '<head>'
        '<script src="https://www.googletagmanager.com/gtm.js?id=G-ABC"></script>'
        '<script src="https://connect.facebook.net/en_US/fbevents.js"></script>'
        '<script src="/wp-content/themes/x/app.js"></script>'
        '</head>'
    )
    out, stats = _apply(html)
    assert stats.telemetry_removed == 2
    assert "googletagmanager" not in out and "facebook" not in out
    assert "themes/x/app.js" in out  # the site's own script is untouched


def test_tracking_pixels_and_noscript_iframes_are_removed():
    html = (
        '<body>'
        '<img src="https://pixel.wp.com/b.gif?v=1" width="1" height="1">'
        '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=G-A"></iframe></noscript>'
        '<img src="/wp-content/uploads/photo.jpg">'
        '</body>'
    )
    out, stats = _apply(html)
    assert stats.telemetry_removed == 2
    assert "pixel.wp.com" not in out and "googletagmanager" not in out
    assert "uploads/photo.jpg" in out


def test_keep_hosts_overrides_the_blocklist():
    html = '<script src="https://stats.wp.com/w.js"></script>'
    policy = Policy(telemetry_keep_hosts=["stats.wp.com"])
    out, stats = _apply(html, policy)
    assert stats.telemetry_removed == 0
    assert "stats.wp.com" in out


def test_extra_hosts_extends_the_blocklist():
    html = '<script src="https://analytics.example-vendor.com/t.js"></script>'
    policy = Policy(telemetry_extra_hosts=["analytics.example-vendor.com"])
    _, stats = _apply(html, policy)
    assert stats.telemetry_removed == 1


# --- telemetry: inline blocks ---------------------------------------------


def test_inline_gtag_and_monsterinsights_blocks_are_removed():
    html = (
        '<head>'
        "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
        "gtag('config','G-ABC');</script>"
        "<script>var mi_version='10.2.2';var mi_track_user=true;var MonsterInsightsDefaultLocations={};</script>"
        '<script>var siteConfig={theme:"x"};</script>'
        '</head>'
    )
    out, stats = _apply(html)
    assert stats.telemetry_removed == 2
    assert "gtag" not in out and "MonsterInsights" not in out
    assert "siteConfig" in out  # ordinary inline config survives


def test_bare_ga_call_is_not_treated_as_telemetry():
    """`ga(` alone is too collision-prone; only the qualified forms match."""
    html = '<script>function ga(x){return x*2;} ga(21);</script>'
    _, stats = _apply(html)
    assert stats.telemetry_removed == 0


def test_signature_bearing_block_is_removed_whole_even_if_mixed():
    """'No telemetry at all' means a block matching a signature goes whole,
    tracking-only or not. Documented as the price of aggressive stripping;
    not observed on real WordPress captures, where tracking is its own block."""
    html = (
        "<script>initCarousel();gtag('event','view');renderMenu();"
        "for(var i=0;i<10;i++){build(i);}</script>"
    )
    out, stats = _apply(html)
    assert stats.telemetry_removed == 1
    assert "gtag" not in out and "initCarousel" not in out


# --- forms ----------------------------------------------------------------


def test_all_forms_are_removed():
    html = (
        '<div>'
        '<form action="https://subscribe.wordpress.com/" method="post"><input name="email"></form>'
        '<form action="/?s=" method="get"><input name="s"></form>'
        '<p>real content</p>'
        '</div>'
    )
    out, stats = _apply(html)
    assert stats.forms_removed == 2
    assert "<form" not in out
    assert "real content" in out


def test_forms_are_kept_when_strip_forms_is_disabled():
    html = '<form action="/x"><input></form>'
    _, stats = _apply(html, Policy(strip_forms=False))
    assert stats.forms_removed == 0


def test_removed_forms_are_attributed_to_their_page():
    soup = BeautifulSoup(
        '<form action="/subscribe"><input name="email"></form><form action="/x"><input></form>', "lxml"
    )
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/contact/", "/contact.html")
    assert stats.forms_removed == 2
    assert stats.forms_removed_pages == {
        "https://s/contact/": {"count": 2, "output_path": "/contact.html", "categories": {"other": 2}}
    }


def test_forms_removed_without_a_page_url_are_not_attributed():
    soup = BeautifulSoup('<form action="/x"><input></form>', "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats)
    assert stats.forms_removed == 1
    assert stats.forms_removed_pages == {}


# --- comment-form wrapper removal -------------------------------------------


def test_comment_form_wrapper_is_removed_whole_default_wp_markup():
    html = (
        '<div id="respond" class="comment-respond">'
        '<h3 id="reply-title" class="comment-reply-title">Leave a Reply '
        '<small><a id="cancel-comment-reply-link" href="#" style="display:none;">Cancel reply</a></small></h3>'
        '<form action="/wp-comments-post.php" method="post" id="commentform" class="comment-form">'
        '<input name="comment"></form>'
        "</div>"
    )
    out, stats = _apply(html)
    assert stats.forms_removed == 1
    assert "<form" not in out
    assert "Leave a Reply" not in out
    assert "Cancel reply" not in out
    assert "respond" not in out


def test_comment_form_wrapper_is_removed_with_themed_caption_text():
    # A theme overriding comment_form()'s title_reply arg changes only the
    # heading's text, not the wrapper's id/class -- the whole point of
    # detecting the wrapper instead of the caption string.
    html = (
        '<div id="respond" class="comment-respond">'
        '<h3 id="reply-title" class="comment-reply-title">Submit a Comment</h3>'
        '<form action="/wp-comments-post.php" method="post" id="commentform" class="comment-form">'
        '<input name="comment"></form>'
        "</div>"
    )
    out, stats = _apply(html)
    assert stats.forms_removed == 1
    assert "<form" not in out
    assert "Submit a Comment" not in out


def test_comment_form_without_a_wrapper_falls_back_to_form_only_removal():
    # No id="respond" ancestor -- a non-standard comment implementation, or
    # just the form's own attributes surviving without the wrapper. Removed,
    # but nothing outside the form itself is touched (nothing to safely take
    # with it).
    html = '<div><h3>Leave a Reply</h3><form id="commentform" class="comment-form"><input></form></div>'
    out, stats = _apply(html)
    assert stats.forms_removed == 1
    assert "<form" not in out
    assert "Leave a Reply" in out


def test_unrelated_form_is_categorized_as_other():
    html = '<form action="/subscribe"><input name="email"></form>'
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/", "/index.html")
    assert stats.forms_removed_pages["https://s/"]["categories"] == {"other": 1}


def test_comment_form_is_categorized_as_comment():
    html = (
        '<div id="respond">'
        '<form id="commentform" class="comment-form"><input></form>'
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/post/", "/post.html")
    assert stats.forms_removed_pages["https://s/post/"]["categories"] == {"comment": 1}


def test_mixed_categories_on_one_page_are_both_counted():
    html = (
        '<div id="respond"><form id="commentform" class="comment-form"><input></form></div>'
        '<form action="/?s="><input name="s"></form>'
    )
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/post/", "/post.html")
    entry = stats.forms_removed_pages["https://s/post/"]
    assert entry["count"] == 2
    assert entry["categories"] == {"comment": 1, "search": 1}


def test_wrapper_containing_two_forms_does_not_double_count_or_crash():
    # Malformed/unusual markup: two <form>s inside one comment-respond
    # wrapper. Both are removed as a side effect of removing the one
    # wrapper -- counted as a single removal (the wrapper), not two -- with
    # no crash from a later loop iteration touching the second form after
    # it's already been detached by the first.
    html = (
        '<div id="respond" class="comment-respond">'
        '<form id="commentform" class="comment-form"><input></form>'
        '<form class="comment-form"><input></form>'
        "</div>"
    )
    out, stats = _apply(html)
    assert stats.forms_removed == 1
    assert "<form" not in out
    assert "respond" not in out


# --- search-form detection and strip_search_forms ---------------------------


def test_search_form_detected_by_role_search():
    html = '<form role="search" method="get" action="/"><input type="search" name="q"></form>'
    out, stats = _apply(html)
    assert "<form" not in out
    assert stats.forms_removed_pages == {}  # no page_url passed via _apply


def test_search_form_detected_by_name_s_input_without_role():
    # Divi's own search widget: no role="search" was found on some themes'
    # variants -- name="s" alone must be sufficient.
    html = '<form class="et-search-form" method="get" action="/"><input type="search" name="s"></form>'
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/purpose/", "/purpose.html")
    assert stats.forms_removed_pages["https://s/purpose/"]["categories"] == {"search": 1}


def test_search_form_categorized_as_search_not_other():
    html = '<form role="search" method="get" action="/"><input type="search" name="s"></form>'
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/", "/index.html")
    assert stats.forms_removed_pages["https://s/"]["categories"] == {"search": 1}


def test_strip_search_forms_false_leaves_the_form_untouched():
    html = '<form role="search" method="get" action="/"><input type="search" name="s"></form>'
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(strip_search_forms=False), stats, "https://s/purpose/", "/purpose.html")

    assert "<form" in str(soup)
    assert stats.forms_removed == 0
    assert stats.forms_removed_pages == {}
    assert stats.search_forms_kept_pages == {
        "https://s/purpose/": {"count": 1, "output_path": "/purpose.html"}
    }


def test_strip_search_forms_false_still_strips_comment_forms():
    html = (
        '<div id="respond"><form id="commentform" class="comment-form"><input></form></div>'
        '<form role="search" method="get" action="/"><input type="search" name="s"></form>'
    )
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(strip_search_forms=False), stats, "https://s/post/", "/post.html")

    out = str(soup)
    assert 'class="comment-form"' not in out
    assert 'role="search"' in out
    assert stats.forms_removed_pages["https://s/post/"]["categories"] == {"comment": 1}
    assert stats.search_forms_kept_pages["https://s/post/"]["count"] == 1


def test_strip_search_forms_false_with_no_page_url_still_skips_removal():
    html = '<form role="search" method="get" action="/"><input type="search" name="s"></form>'
    out, stats = _apply(html, Policy(strip_search_forms=False))
    assert "<form" in out
    assert stats.search_forms_kept_pages == {}  # nothing to key by, but not removed either


def test_form_with_neither_signal_is_not_search():
    html = '<form action="/subscribe"><input name="email"></form>'
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(strip_search_forms=False), stats, "https://s/", "/index.html")
    # Not recognized as search, so strip_search_forms=False doesn't spare it.
    assert stats.forms_removed_pages["https://s/"]["categories"] == {"other": 1}
    assert stats.search_forms_kept_pages == {}


# --- password-protected-post detection --------------------------------------


def test_password_form_is_categorized_as_password_and_removed():
    html = (
        '<div class="entry-content">'
        '<form action="https://s.example/wp-login.php?action=postpass" '
        'class="post-password-form" method="post">'
        '<p>This content is password protected.</p>'
        '</form>'
        '</div>'
    )
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s.example/secret/", "/secret.html")
    out = str(soup)
    assert "<form" not in out
    assert stats.forms_removed_pages["https://s.example/secret/"]["categories"] == {"password": 1}


def test_password_form_removal_marks_body_for_content_checks():
    html = (
        '<form action="/wp-pass.php" class="post-password-form" method="post">'
        '<p>Enter password.</p>'
        '</form>'
    )
    out, stats = _apply(html)
    assert 'data-wpfreeze-password-protected="1"' in out


def test_page_without_a_password_form_is_not_marked():
    out, stats = _apply('<p>Ordinary content, no forms at all.</p>')
    assert "data-wpfreeze-password-protected" not in out


def test_password_form_not_confused_with_search_form():
    # Belt and suspenders: a password form has neither role="search" nor a
    # name="s" input, so ordering the checks after comment/before search in
    # _strip_forms doesn't matter for correctness -- confirm it anyway.
    html = '<form class="post-password-form" method="post"><input name="post_password"></form>'
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/", "/index.html")
    assert stats.forms_removed_pages["https://s/"]["categories"] == {"password": 1}


# --- newsletter-module fingerprint (Divi) ------------------------------------


def _newsletter_module(extra_classes: str = "") -> str:
    return (
        f'<div class="et_pb_module et_pb_newsletter {extra_classes}">'
        '<div class="et_pb_newsletter_description"></div>'
        '<div class="et_pb_newsletter_form">'
        '<form method="post" class="et_pb_newsletter_custom_fields">'
        '<input name="et_pb_signup_email"></form></div></div>'
    )


def test_newsletter_module_and_caption_removed_when_divi_flags_no_description():
    html = (
        '<div class="et_pb_module et_pb_text"><div class="et_pb_text_inner">'
        '<h2><strong>Subscribe to our newsletter to stay up to date.</strong></h2>'
        "</div></div>"
        + _newsletter_module(
            "et_pb_newsletter_description_no_title et_pb_newsletter_description_no_content"
        )
    )
    out, stats = _apply(html)
    assert stats.forms_removed == 1
    assert stats.newsletter_captions_removed == 1
    assert "<form" not in out
    assert "et_pb_newsletter" not in out
    assert "Subscribe to our newsletter" not in out


def test_newsletter_caption_left_alone_when_module_has_its_own_description():
    # No et_pb_newsletter_description_no_title/_no_content classes -- Divi's
    # own signal that this instance already has a title/description of its
    # own, so the preceding heading is unrelated content, not a caption.
    html = (
        '<div class="et_pb_module et_pb_text"><div class="et_pb_text_inner">'
        "<h2>Unrelated section heading</h2>"
        "</div></div>"
        + _newsletter_module()
    )
    out, stats = _apply(html)
    assert stats.forms_removed == 1
    assert stats.newsletter_captions_removed == 0
    assert "<form" not in out
    assert "Unrelated section heading" in out


def test_newsletter_caption_sibling_with_real_content_is_not_removed():
    # Sibling has a heading AND a paragraph -- a real content section, not a
    # bare caption, so it must survive even though the no-description flags
    # are set.
    html = (
        '<div class="et_pb_module et_pb_text"><div class="et_pb_text_inner">'
        "<h2>Get Involved</h2><p>Read more about the project here.</p>"
        "</div></div>"
        + _newsletter_module(
            "et_pb_newsletter_description_no_title et_pb_newsletter_description_no_content"
        )
    )
    out, stats = _apply(html)
    assert stats.forms_removed == 1
    assert stats.newsletter_captions_removed == 0
    assert "Get Involved" in out
    assert "Read more about the project here." in out


def test_newsletter_module_without_wrapper_class_falls_back_to_other():
    html = '<form action="/subscribe" class="et_pb_newsletter_custom_fields"><input name="email"></form>'
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/", "/index.html")
    assert stats.forms_removed_pages["https://s/"]["categories"] == {"other": 1}


def test_newsletter_form_is_categorized_as_newsletter():
    html = _newsletter_module()
    soup = BeautifulSoup(html, "lxml")
    stats = PolicyStats()
    apply_policy(soup, Policy(), stats, "https://s/", "/index.html")
    assert stats.forms_removed_pages["https://s/"]["categories"] == {"newsletter": 1}


# --- dead in-page links left by comment-form removal ------------------------


def test_comments_number_blurb_is_removed_with_its_dead_link():
    html = (
        '<p class="post-meta">'
        '<span class="published">Feb 1, 2018</span> | '
        '<a href="../category/news.html">News</a> | '
        '<span class="comments-number"><a href="1942-part-4.html#respond">0 comments</a></span>'
        "</p>"
        '<div id="respond" class="comment-respond">'
        '<form id="commentform" class="comment-form"><input></form></div>'
    )
    out, stats = _apply(html)
    assert "comments-number" not in out
    assert "0 comments" not in out
    assert "#respond" not in out
    assert stats.comment_count_blurbs_removed == 1
    assert stats.dead_fragment_links_removed == 0
    # the separator before the removed blurb shouldn't leave a dangling "|"
    assert "News</a> |" not in out
    assert "News</a>" in out


def test_dead_fragment_link_without_comments_number_wrapper_is_unwrapped_not_removed():
    html = (
        '<p>See <a href="post.html#respond">the discussion</a> below.</p>'
        '<div id="respond"><form id="commentform" class="comment-form"><input></form></div>'
    )
    out, stats = _apply(html)
    assert "#respond" not in out
    assert "See the discussion below." in out  # text kept, only the dead <a> dropped
    assert "<a" not in out
    assert stats.dead_fragment_links_removed == 1
    assert stats.comment_count_blurbs_removed == 0


def test_links_to_ids_not_removed_are_left_alone():
    html = (
        '<a href="#toc">Table of contents</a>'
        '<div id="respond"><form id="commentform" class="comment-form"><input></form></div>'
    )
    out, stats = _apply(html)
    assert '<a href="#toc">Table of contents</a>' in out
    assert stats.dead_fragment_links_removed == 0
    assert stats.comment_count_blurbs_removed == 0


def test_no_forms_means_no_fragment_cleanup_runs():
    html = '<a href="#respond">stray anchor, no comment form on this page</a>'
    out, stats = _apply(html)
    assert '<a href="#respond">' in out
    assert stats.dead_fragment_links_removed == 0


def _comments_number_page(count_text: str) -> str:
    return (
        '<p class="post-meta">'
        '<span class="published">Mar 26, 2021</span> | '
        '<a href="news.html">News</a> | '
        f'<span class="comments-number"><a href="post.html#respond">{count_text}</a></span>'
        "</p>"
        '<div id="respond" class="comment-respond">'
        '<form id="commentform" class="comment-form"><input></form></div>'
    )


def test_strip_comment_counts_false_keeps_nonzero_count_but_still_fixes_the_link():
    out, stats = _apply(_comments_number_page("2 comments"), Policy(strip_comment_counts=False))
    assert '<span class="comments-number">2 comments</span>' in out
    assert "#respond" not in out
    assert stats.comment_count_blurbs_removed == 0
    assert stats.dead_fragment_links_removed == 1


def test_strip_comment_counts_false_still_removes_a_zero_count():
    out, stats = _apply(_comments_number_page("0 comments"), Policy(strip_comment_counts=False))
    assert "0 comments" not in out
    assert "comments-number" not in out
    assert stats.comment_count_blurbs_removed == 1
    assert stats.dead_fragment_links_removed == 0


def test_strip_comment_counts_true_removes_the_blurb_regardless_of_count():
    out, stats = _apply(_comments_number_page("2 comments"), Policy(strip_comment_counts=True))
    assert "2 comments" not in out
    assert stats.comment_count_blurbs_removed == 1


def test_strip_comment_counts_false_with_unparseable_text_keeps_it():
    # No leading digit -- can't tell zero from nonzero, so the safer guess
    # (keep it) wins over guessing zero and deleting real information.
    out, stats = _apply(_comments_number_page("No comments yet"), Policy(strip_comment_counts=False))
    assert "No comments yet" in out
    assert "#respond" not in out
    assert stats.comment_count_blurbs_removed == 0
    assert stats.dead_fragment_links_removed == 1


def test_comment_count_helper_parses_leading_digits():
    from wpfreeze.policy import _comment_count

    assert _comment_count("0 comments") == 0
    assert _comment_count("2 Comments") == 2
    assert _comment_count("1 comment") == 1
    assert _comment_count("No comments yet") is None


# --- feeds ----------------------------------------------------------------


def test_feed_alternate_links_are_removed():
    html = (
        '<head>'
        '<link rel="alternate" type="application/rss+xml" href="https://s/feed/">'
        '<link rel="alternate" type="application/atom+xml" href="https://s/feed/atom/">'
        '<link rel="canonical" href="https://s/page/">'
        '<link rel="stylesheet" href="/s.css">'
        '</head>'
    )
    out, stats = _apply(html)
    assert stats.feeds_removed == 2
    assert "rss+xml" not in out and "atom+xml" not in out
    assert 'rel="canonical"' in out and "s.css" in out  # non-feed links kept


def test_visible_rss_anchor_in_body_is_left_alone():
    """Body content is not a feed <link>; removing it would edit the page's
    visible text."""
    html = '<body><a href="https://s/feed/">Subscribe via RSS</a></body>'
    out, stats = _apply(html)
    assert stats.feeds_removed == 0
    assert "Subscribe via RSS" in out


# --- WordPress protocol-discovery links ------------------------------------


def test_wp_protocol_discovery_links_are_removed():
    html = (
        '<head>'
        '<link rel="pingback" href="https://s/xmlrpc.php">'
        '<link rel="EditURI" type="application/rsd+xml" href="https://s/xmlrpc.php?rsd">'
        '<link rel="https://api.w.org/" href="https://s/wp-json/">'
        '<link rel="alternate" type="application/json" href="https://s/wp-json/wp/v2/pages/1">'
        '<link rel="alternate" title="oEmbed (JSON)" type="application/json+oembed" href="https://s/wp-json/oembed/1.0/embed?url=x">'
        '<link rel="alternate" title="oEmbed (XML)" type="text/xml+oembed" href="https://s/wp-json/oembed/1.0/embed?url=x&format=xml">'
        "<link rel='shortlink' href='https://s/?p=1'>"
        '<link rel="canonical" href="https://s/page/">'
        '<link rel="stylesheet" href="/s.css">'
        '<link rel="alternate" type="application/rss+xml" href="https://s/feed/">'
        '</head>'
    )
    out, stats = _apply(html)
    assert stats.wp_meta_links_removed == 7
    assert "xmlrpc.php" not in out
    assert "api.w.org" not in out
    assert "wp-json" not in out
    assert "shortlink" not in out
    # untouched: canonical, a real stylesheet, and feeds (own flag/counter)
    assert 'rel="canonical"' in out and "s.css" in out
    assert stats.feeds_removed == 1


def test_wp_meta_link_stripping_can_be_disabled():
    html = "<head><link rel='shortlink' href='https://s/?p=1'></head>"
    out, stats = _apply(html, Policy(strip_wp_meta_links=False))
    assert stats.wp_meta_links_removed == 0
    assert "shortlink" in out


# --- config ---------------------------------------------------------------


def test_policy_defaults_strip_everything():
    p = Policy()
    assert p.strip_telemetry and p.strip_forms and p.strip_feeds and p.strip_wp_meta_links


def test_policy_from_config_reads_yaml_shaped_dict():
    p = Policy.from_config(
        {
            "strip_telemetry": True,
            "strip_forms": False,
            "strip_feeds": True,
            "strip_wp_meta_links": False,
            "telemetry_extra_hosts": ["t.example.com"],
        }
    )
    assert p.strip_telemetry and not p.strip_forms and p.strip_feeds and not p.strip_wp_meta_links
    assert p.telemetry_extra_hosts == ["t.example.com"]


def test_policy_from_empty_config_is_all_on():
    p = Policy.from_config(None)
    assert p.strip_telemetry and p.strip_forms and p.strip_feeds and p.strip_wp_meta_links


def test_same_host_tracking_plugin_scripts_are_removed_by_path():
    """The characteristic self-hosted case: the analytics plugin serves its
    tracking JS from the site's own domain, so the host blocklist can't see
    it -- a path signature must."""
    html = (
        '<head>'
        '<script src="https://mysite.com/wp-content/plugins/'
        'google-analytics-for-wordpress/assets/js/frontend-gtag.min.js"></script>'
        '<script src="https://mysite.com/wp-content/themes/x/nav.js"></script>'
        '</head>'
    )
    out, stats = _apply(html)
    assert stats.telemetry_removed == 1
    assert "google-analytics-for-wordpress" not in out
    assert "themes/x/nav.js" in out


def test_matomo_self_hosted_tracker_is_removed_by_path():
    html = '<script src="https://mysite.com/matomo/matomo.js"></script>'
    _, stats = _apply(html)
    assert stats.telemetry_removed == 1
