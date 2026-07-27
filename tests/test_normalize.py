from __future__ import annotations

from bs4 import BeautifulSoup

from wpfreeze.normalize import NormalizeStats, apply_normalizations


def _run(html: str) -> tuple[BeautifulSoup, NormalizeStats]:
    soup = BeautifulSoup(html, "html5lib")
    stats = NormalizeStats()
    apply_normalizations(soup, stats)
    return soup, stats


# --- the five repairs -------------------------------------------------------


def test_word_paste_artifacts_are_unwrapped_not_deleted():
    """<o:p> is Word clipboard residue, not an element -- but its children
    are ordinary content, so it must be unwrapped rather than removed."""
    soup, stats = _run("<p>before<o:p>keep me</o:p>after</p>")
    assert "keep me" in soup.get_text()
    assert soup.find("o:p") is None
    assert stats.word_artifacts_unwrapped == 1


def test_obsolete_border_attribute_is_removed():
    soup, stats = _run('<a href="/x" border="0">link</a><img src="/i.png" border="0">')
    assert "border" not in soup.find("a").attrs
    assert "border" not in soup.find("img").attrs
    assert stats.obsolete_attrs_removed == 2


def test_border_on_table_is_left_alone():
    """border="1" is still conforming on <table>; only the elements where
    it is obsolete are touched."""
    soup, stats = _run('<table border="1"><tr><td>c</td></tr></table>')
    assert soup.find("table")["border"] == "1"
    assert stats.obsolete_attrs_removed == 0


def test_forbidden_control_characters_are_stripped_from_text():
    soup, stats = _run("<p>beforeafterend</p>")
    text = soup.find("p").get_text()
    assert "" not in text and "" not in text
    assert text == "beforeafterend"
    assert stats.control_chars_stripped == 2


def test_permitted_whitespace_controls_survive():
    """Tab, newline and carriage return are legal in HTML text and carry
    meaning inside <pre>; only the forbidden C0 set goes."""
    soup, stats = _run("<pre>a\tb\nc</pre>")
    assert soup.find("pre").get_text() == "a\tb\nc"
    assert stats.control_chars_stripped == 0


def test_invalid_image_dimensions_are_removed():
    soup, stats = _run('<img src="/a.png" width="auto" height="120">')
    img = soup.find("img")
    assert "width" not in img.attrs
    assert img["height"] == "120"  # a valid dimension is untouched
    assert stats.invalid_dimensions_removed == 1


def test_boolean_attribute_with_an_explicit_value_is_normalized():
    """A boolean attribute is true whenever present, whatever its value --
    so allowfullscreen="false" is already true in every browser and
    normalizing to "" preserves behaviour exactly rather than changing it.
    """
    soup, stats = _run('<iframe src="/v" allowfullscreen="false"></iframe>')
    assert soup.find("iframe")["allowfullscreen"] == ""
    assert stats.boolean_attrs_normalized == 1


def test_already_conforming_boolean_attributes_are_left_alone():
    soup, stats = _run('<input required=""><input disabled="disabled">')
    assert stats.boolean_attrs_normalized == 0


def test_hidden_is_not_treated_as_boolean():
    """`hidden` takes a meaningful "until-found" value and is no longer
    purely boolean; normalizing it would change behaviour."""
    soup, stats = _run('<div hidden="until-found">x</div>')
    assert soup.find("div")["hidden"] == "until-found"
    assert stats.boolean_attrs_normalized == 0


# --- the boundary: load-bearing defects stay ---------------------------------


def test_body_level_style_is_not_relocated():
    """Invalid but universally supported, and moving it changes its
    position in document order -- which is how CSS resolves
    equal-specificity rules. Repairing it could change how the page looks,
    which is the one thing a replacement site must not do."""
    soup, _ = _run("<body><p>x</p><style>.a{color:red}</style></body>")
    body_styles = soup.body.find_all("style")
    assert len(body_styles) == 1


def test_images_without_alt_are_left_for_a_human():
    """Injecting alt="" asserts the image is decorative, which we cannot
    know. A wrong accessibility claim is worse than an honest gap, and it
    would silence the report that prompts someone to write a real
    description."""
    soup, _ = _run('<img src="/a.png">')
    assert "alt" not in soup.find("img").attrs


def test_invalid_css_selectors_are_not_repaired():
    """WordPress core emits button:not(".components-button"). The quoted
    string is invalid, so browsers drop the rule -- on the live site too.
    Fixing it would make the rule apply and the archive would render
    differently from the original."""
    css = 'button:not(".components-button"):hover{color:red}'
    soup, _ = _run(f"<style>{css}</style>")
    assert css in str(soup.find("style").string)


def test_control_characters_inside_script_are_left_alone():
    """Inert garbage in prose; *data* inside a JS string literal. Removing
    it there would change what the code does."""
    soup, stats = _run("<script>var sep = \"a\x1fb\";</script>")
    assert "\x1f" in str(soup.find("script").string)
    assert stats.control_chars_stripped == 0


def test_stats_total_sums_every_category():
    soup, stats = _run(
        '<p>ab<o:p>w</o:p></p><img src="/i.png" width="auto" border="0">'
        '<iframe allowfullscreen="yes"></iframe>'
    )
    assert stats.total == 5
