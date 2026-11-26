from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest
import yaml

from wpfreeze.cli import ConfigError, SiteConfig, load_config, run_build
from wpfreeze.customize import CustomizeError, Site, resolve_local_href, run_customize_script
from wpfreeze.manifest import Manifest, Status

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HEADER = (
    '<div id="top-header"><div id="et-info"><a href="mailto:info@example.com">info@example.com</a></div>'
    '<ul id="et-secondary-nav"><li class="menu-item"><a href="{up}contact.html">Contact Us</a></li></ul></div>'
)


def _page(up: str, body: str) -> str:
    return f"<html><head><title>t</title></head><body>{HEADER.format(up=up)}{body}</body></html>"


def _site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    (site / "news").mkdir(parents=True)
    (site / "index.html").write_text(
        _page("", '<div class="row social"><h2>Follow Us</h2><a href="https://twitter.com/x">t</a></div>'
                  '<p>Read <a href="contact.html#form">our contact page</a> today.</p>'),
        encoding="utf-8",
    )
    (site / "contact.html").write_text(_page("", "<p>Write to us.</p>"), encoding="utf-8")
    (site / "news" / "post.html").write_text(
        _page("../", '<p>Follow <a href="https://twitter.com/x">@x</a> and <a href="https://example.org/">friends</a>.</p>'),
        encoding="utf-8",
    )
    (site / "thanks.html").write_text("<html><body><p>Thanks!</p></body></html>", encoding="utf-8")
    (site / ".htaccess").write_text(
        "# wpfreeze generated redirect map\n\nRewriteEngine On\nRewriteCond %{REQUEST_FILENAME} !-f\n"
        "RewriteRule ^(.*)/$ /$1.html [L]\n\nRedirect 301 /?p=885 /contact.html\nRedirect 301 /?p=2 /index.html\n",
        encoding="utf-8",
    )
    return site


def _read(site: Path, rel: str) -> str:
    return (site / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_resolve_local_href_mirrors_verify_site():
    assert resolve_local_href("news/post.html", "../contact.html") == "contact.html"
    assert resolve_local_href("news/post.html", "../contact.html?x=1#f") == "contact.html"
    assert resolve_local_href("index.html", "/contact.html") == "contact.html"
    assert resolve_local_href("index.html", "caf%C3%A9.html") == "café.html"
    assert resolve_local_href("index.html", "https://example.com/contact.html") is None
    assert resolve_local_href("index.html", "//example.com/contact.html") is None
    assert resolve_local_href("index.html", "mailto:a@b") is None
    assert resolve_local_href("index.html", "#top") is None
    assert resolve_local_href("index.html", "../outside.html") is None


def test_remove_everywhere(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.remove("#et-info")
    stats = site.commit()
    for rel in ("index.html", "contact.html", "news/post.html"):
        assert "info@example.com" not in _read(site_dir, rel)
    assert stats.pages_changed == ["contact.html", "index.html", "news/post.html"]
    assert site.actions[0].matches == 3
    assert site.actions[0].samples == ["info@example.com"]


def test_remove_zero_matches_is_an_error(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="matched nothing"):
        site.remove("#no-such-thing")


def test_remove_named_pages_requires_a_match_on_each(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="news/post.html"):
        site.remove(".social", pages=["index.html", "news/post.html"])


def test_remove_named_page_that_does_not_exist(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="does not exist"):
        site.remove(".social", pages=["nope.html"])


def test_remove_glob_pages(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.remove("a[href^='https://twitter.com']", pages="news/*")
    site.commit()
    assert "twitter.com" not in _read(site_dir, "news/post.html")
    assert "twitter.com" in _read(site_dir, "index.html")


def test_remove_contains_selector_and_sample(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.remove('div.row:-soup-contains("Follow Us")', pages=["index.html"])
    site.commit()
    assert "Follow Us" not in _read(site_dir, "index.html")
    assert site.actions[0].samples == ["Follow Us t"]


def test_nested_matches_count_once(tmp_path: Path):
    site = Site(_site(tmp_path))
    site.remove("div", pages=["contact.html"])
    assert site.actions[0].matches == 1  # #top-header; #et-info inside it goes with it


def test_unwrap_links_to_keeps_text(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.unwrap_links_to(r"https?://(www\.)?twitter\.com/", pages=["news/post.html"])
    site.commit()
    text = _read(site_dir, "news/post.html")
    assert "twitter.com" not in text
    assert "Follow @x and" in text
    assert 'href="https://example.org/"' in text


def test_unwrap_links_to_zero_matches(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="no link matching"):
        site.unwrap_links_to("facebook", pages=["news/post.html"])


def test_replace_exactly_once(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.replace("thanks.html", "<p>Thanks!</p>", "<p>Thank you.</p>")
    site.commit()
    assert "Thank you." in _read(site_dir, "thanks.html")


def test_replace_zero_or_many_is_an_error(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="0 times.*copy it from site/thanks.html"):
        site.replace("thanks.html", "Thanks?", "x")
    with pytest.raises(CustomizeError, match="4 times"):
        site.replace("index.html", "<a ", "<b ")


def test_replace_sees_earlier_tree_edits(tmp_path: Path):
    """Script order holds across the text/tree boundary: a replace after a
    remove on the same page sees the post-remove markup."""
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.remove(".social", pages=["index.html"])
    with pytest.raises(CustomizeError, match="0 times"):
        site.replace("index.html", "<h2>Follow Us</h2>", "")
    site.replace("index.html", "today.", "soon.")
    site.remove("#et-info", pages=["index.html"])
    site.commit()
    text = _read(site_dir, "index.html")
    assert "soon." in text and "info@example.com" not in text and "Follow Us" not in text


def test_delete_page_removes_menu_items_and_unwraps_body_links(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.delete_page("contact.html")
    stats = site.commit()
    assert not (site_dir / "contact.html").exists()
    assert stats.pages_deleted == ["contact.html"]
    index = _read(site_dir, "index.html")
    assert "Contact Us" not in index  # the menu <li> went
    assert "<li" not in index
    assert "Read our contact page today." in index  # body link unwrapped, text kept
    assert "contact.html" not in index
    assert "contact.html" not in _read(site_dir, "news/post.html")
    assert site.actions[0].matches == 3
    htaccess = _read(site_dir, ".htaccess")
    assert "/contact.html" not in htaccess
    assert "Redirect 301 /?p=2 /index.html" in htaccess


def test_delete_page_with_no_inbound_links_is_fine(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.delete_page("thanks.html")
    site.commit()
    assert not (site_dir / "thanks.html").exists()
    assert site.actions[0].matches == 0


def test_delete_page_missing_is_an_error(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="does not exist"):
        site.delete_page("gone.html")


def test_delete_page_twice_says_why(tmp_path: Path):
    site = Site(_site(tmp_path))
    site.delete_page("thanks.html")
    with pytest.raises(CustomizeError, match="deleted earlier"):
        site.delete_page("thanks.html")


def test_delete_page_redirect_to(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.delete_page("contact.html", redirect_to="index.html")
    site.commit()
    lines = _read(site_dir, ".htaccess").splitlines()
    assert "Redirect 301 /?p=885 /index.html" in lines
    rule = r"RewriteRule ^contact(/|\.html)?$ /index.html [R=301,L]"
    assert rule in lines
    assert lines.index(rule) < lines.index("RewriteRule ^(.*)/$ /$1.html [L]")


def test_delete_page_redirect_to_must_exist(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="redirect_to"):
        site.delete_page("contact.html", redirect_to="nowhere.html")


def test_nothing_written_without_commit(tmp_path: Path):
    site_dir = _site(tmp_path)
    before = {p: p.read_bytes() for p in site_dir.rglob("*") if p.is_file()}
    site = Site(site_dir)
    site.remove("#et-info")
    site.delete_page("contact.html")
    assert {p: p.read_bytes() for p in site_dir.rglob("*") if p.is_file()} == before


def test_run_script_failure_changes_nothing(tmp_path: Path):
    site_dir = _site(tmp_path)
    before = {p: p.read_bytes() for p in site_dir.rglob("*") if p.is_file()}
    script = tmp_path / "customize.py"
    script.write_text("def customize(site):\n    site.remove('#et-info')\n    site.remove('#missing')\n")
    with pytest.raises(CustomizeError):
        run_customize_script(script, site_dir)
    assert {p: p.read_bytes() for p in site_dir.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "__pycache__").exists()


def test_run_script_requires_customize_function(tmp_path: Path):
    script = tmp_path / "customize.py"
    script.write_text("x = 1\n")
    with pytest.raises(CustomizeError, match="customize\\(site\\)"):
        run_customize_script(script, _site(tmp_path))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _yaml(tmp_path: Path, **extra) -> Path:
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump({"base_url": "https://example.com", "output_dir": str(tmp_path / "o"), **extra}))
    return path


def test_config_customize_tristate(tmp_path: Path):
    assert load_config(_yaml(tmp_path)).customize is None
    assert load_config(_yaml(tmp_path, customize=True)).customize is True
    assert load_config(_yaml(tmp_path, customize=False)).customize is False
    with pytest.raises(ConfigError, match="unquoted"):
        load_config(_yaml(tmp_path, customize="false"))


# ---------------------------------------------------------------------------
# Build integration
# ---------------------------------------------------------------------------


def _capture(output_dir: Path, **config_kwargs) -> SiteConfig:
    """Two pages: index links to contact, contact has an unresolved link
    (so the deleted page's findings can be seen to drop out)."""
    manifest = Manifest()
    pages = {
        "https://example.com/": ("/index.html", '<p><a href="https://example.com/contact/">Contact</a></p>'),
        "https://example.com/contact/": ("/contact.html", '<p><a href="https://example.com/missing/">gone</a></p>'),
    }
    (output_dir / "raw").mkdir(parents=True)
    for i, (url, (out, body)) in enumerate(pages.items()):
        record = manifest.get_or_create(url)
        record.status = Status.FETCHED.value
        record.http_status = 200
        record.output_path = out
        record.local_path = f"raw/p{i}.html"
        record.content_type = "text/html"
        (output_dir / "raw" / f"p{i}.html").write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    manifest.save(output_dir / "manifest.json")
    return SiteConfig(base_url="https://example.com/", output_dir=output_dir, name="ex", **config_kwargs)


def _script(output_dir: Path, body: str = "    site.delete_page('contact.html')\n") -> None:
    (output_dir / "customize.py").write_text("def customize(site):\n" + body, encoding="utf-8")


def _report(output_dir: Path) -> dict:
    return json.loads((output_dir / "build-report.json").read_text())


def _tty(monkeypatch, answer: str | None):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    asked: list[str] = []

    def fake_input(prompt: str) -> str:
        asked.append(prompt)
        return answer or ""

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def test_build_without_script_is_unchanged(tmp_path: Path):
    out = tmp_path / "out"
    config = _capture(out)
    assert run_build(config, None, verify=True) == 1  # the unresolved link on contact
    assert _report(out)["customize"] is None


def test_build_customize_true_runs_before_verify(tmp_path: Path, capsys):
    out = tmp_path / "out"
    config = _capture(out, customize=True)
    _script(out)
    exit_code = run_build(config, None, verify=True)
    report = _report(out)
    assert not (out / "site" / "contact.html").exists()
    # The deleted page's unresolved link no longer counts, and the link to
    # it was removed before verification ran, so nothing is broken.
    assert report["unresolved"] == 0 and report["unresolved_samples"] == []
    assert report["verification"]["broken"] == 0
    assert exit_code == 0
    assert report["customize"]["ran"] is True
    assert report["customize"]["pages_deleted"] == ["contact.html"]
    assert len(report["customize"]["sha256"]) == 64
    assert "Customized (customize.py)" in capsys.readouterr().out
    assert "Customized after build" in (out / "cleanup-todo.md").read_text()


def test_build_customize_true_without_script_fails(tmp_path: Path, capsys):
    out = tmp_path / "out"
    assert run_build(_capture(out, customize=True), None, verify=False) == 2
    assert "does not exist" in capsys.readouterr().out


def test_build_customize_false_never_runs_or_asks(tmp_path: Path, monkeypatch):
    out = tmp_path / "out"
    _capture(out)
    _script(out)
    asked = _tty(monkeypatch, "y")
    run_build(_capture_config(out, customize=False), None, verify=False)
    assert asked == []
    assert (out / "site" / "contact.html").exists()
    assert _report(out)["customize"]["reason"] == "disabled"


def _capture_config(out: Path, **kwargs) -> SiteConfig:
    return SiteConfig(base_url="https://example.com/", output_dir=out, name="ex", **kwargs)


def test_build_asks_and_runs_on_yes(tmp_path: Path, monkeypatch):
    out = tmp_path / "out"
    config = _capture(out)
    _script(out)
    asked = _tty(monkeypatch, "y")
    run_build(config, None, verify=False)
    assert len(asked) == 1 and "customize.py" in asked[0]
    assert not (out / "site" / "contact.html").exists()


def test_build_asks_and_skips_on_no(tmp_path: Path, monkeypatch, capsys):
    out = tmp_path / "out"
    config = _capture(out)
    _script(out)
    _tty(monkeypatch, "")
    run_build(config, None, verify=False)
    assert (out / "site" / "contact.html").exists()
    assert _report(out)["customize"]["reason"] == "declined"
    assert "customize: false" in capsys.readouterr().out


def test_build_unattended_skips_loudly(tmp_path: Path, capsys):
    out = tmp_path / "out"
    config = _capture(out)
    _script(out)
    run_build(config, None, verify=False)  # pytest's stdin is not a tty
    assert (out / "site" / "contact.html").exists()
    assert "was NOT run" in capsys.readouterr().out
    assert _report(out)["customize"]["reason"] == "unattended"
    assert "Customization script NOT run" in (out / "cleanup-todo.md").read_text()


def test_build_interactive_false_never_asks_even_on_a_tty(tmp_path: Path, monkeypatch):
    out = tmp_path / "out"
    config = _capture(out)
    _script(out)
    asked = _tty(monkeypatch, "y")
    run_build(config, None, verify=False, interactive=False)
    assert asked == []
    assert _report(out)["customize"]["reason"] == "unattended"


def test_build_script_error_exits_2_and_leaves_site_as_built(tmp_path: Path, capsys):
    out = tmp_path / "out"
    config = _capture(out, customize=True)
    _script(out, "    site.delete_page('contact.html')\n    site.remove('#nope')\n")
    assert run_build(config, None, verify=False) == 2
    assert (out / "site" / "contact.html").exists()
    assert "matched nothing" in capsys.readouterr().out


def test_build_script_exception_shows_traceback(tmp_path: Path, capsys):
    out = tmp_path / "out"
    config = _capture(out, customize=True)
    _script(out, "    raise RuntimeError('boom')\n")
    assert run_build(config, None, verify=False) == 2
    err = capsys.readouterr().err
    assert "customize.py" in err and "boom" in err


def test_ledger_across_builds_with_and_without_the_hook(tmp_path: Path):
    """Deleted by the hook: not pruned-as-stale, not untracked; declining
    the hook on a later build brings the page back."""
    out = tmp_path / "out"
    config = _capture(out, customize=True)
    _script(out)
    run_build(config, None, verify=False)
    run_build(config, None, verify=False)
    prune = _report(out)["prune"]
    assert "contact.html" not in prune["untracked"] and "contact.html" not in prune["kept_modified"]
    assert not (out / "site" / "contact.html").exists()

    run_build(dataclasses.replace(config, customize=False), None, verify=False)
    assert (out / "site" / "contact.html").exists()
    assert _report(out)["prune"]["untracked"] == []


def test_add_css_appends_to_head_of_every_page(tmp_path: Path):
    site_dir = _site(tmp_path)
    site = Site(site_dir)
    site.add_css("#et-top-navigation { padding-left: 386px !important; }")
    stats = site.commit()
    for rel in ("index.html", "contact.html", "news/post.html", "thanks.html"):
        head = _read(site_dir, rel).split("</head>")[0]
        assert "<style data-wpfreeze-customize=\"\">" in head
        assert "padding-left: 386px" in head
    assert len(stats.pages_changed) == 4


def test_add_css_lands_after_existing_styles(tmp_path: Path):
    site_dir = _site(tmp_path)
    (site_dir / "styled.html").write_text(
        "<html><head><style>a{color:red}</style><link rel=stylesheet href=x.css></head><body></body></html>",
        encoding="utf-8",
    )
    site = Site(site_dir)
    site.add_css("a{color:blue}", pages=["styled.html"])
    site.commit()
    text = _read(site_dir, "styled.html")
    assert text.index("color:blue") > text.index("x.css") > text.index("color:red")


def test_add_css_rejects_empty_or_style_breaking_css(tmp_path: Path):
    site = Site(_site(tmp_path))
    with pytest.raises(CustomizeError, match="empty"):
        site.add_css("  ")
    with pytest.raises(CustomizeError, match="</style"):
        site.add_css("a{}</style><script>")
