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


# --- config ---------------------------------------------------------------


def test_policy_defaults_strip_everything():
    p = Policy()
    assert p.strip_telemetry and p.strip_forms and p.strip_feeds


def test_policy_from_config_reads_yaml_shaped_dict():
    p = Policy.from_config(
        {
            "strip_telemetry": True,
            "strip_forms": False,
            "strip_feeds": True,
            "telemetry_extra_hosts": ["t.example.com"],
        }
    )
    assert p.strip_telemetry and not p.strip_forms and p.strip_feeds
    assert p.telemetry_extra_hosts == ["t.example.com"]


def test_policy_from_empty_config_is_all_on():
    p = Policy.from_config(None)
    assert p.strip_telemetry and p.strip_forms and p.strip_feeds


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
