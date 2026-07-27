"""Mechanical markup repairs applied to every built page.

The static site is meant to *replace* the original -- someone drops their
WordPress host and repoints the domain at this -- so markup the browser
already throws away has no reason to survive the move. This module removes
that class of defect: things that are inert (no rendering effect
whatsoever, because browsers ignore them today) and mechanically
identifiable (no guessing at what the author meant).

The line that keeps this safe is inertness, not restraint. A defect whose
brokenness is *load-bearing* -- where the original site renders the way it
does precisely because the markup is wrong -- must be left alone, because
"fixing" it would make the replacement look different from the thing it
replaces. Three examples, all deliberately not handled here:

- A stray <style> in <body> is invalid but universally supported, and
  moving it to <head> changes its position in document order, which is how
  CSS resolves equal-specificity rules. Divi's deferred-critical-CSS puts
  it late on purpose. Relocating it can change what the page looks like.
- WordPress core emits `button:not(".components-button"):hover` in its
  inline global styles. The quoted string is invalid per the Selectors
  spec, so browsers drop the whole rule -- on the live site too. Repairing
  the selector would make the rule apply and the archive would render
  differently from the original.
- An <img> with no alt is a real accessibility defect, but injecting
  alt="" asserts the image is decorative, which we cannot know. A wrong
  accessibility claim is worse than an honest gap, and it silences the
  report that would have prompted a human to write a real description.
  Those stay in the VNU report and the cleanup checklist.

Everything removed is counted and surfaced in the build summary, on the
same principle as policy.py: nothing is stripped silently.
"""
from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString

# Boolean attributes per the HTML spec: present means true, and the only
# conforming values are the empty string or the attribute's own name. A
# value like "false" is therefore *already* true in every browser -- so
# normalizing to "" preserves current behaviour exactly. `hidden` is
# deliberately absent: it takes a meaningful "until-found" value and is no
# longer purely boolean.
_BOOLEAN_ATTRS: frozenset[str] = frozenset(
    {
        "allowfullscreen", "async", "autofocus", "autoplay", "checked", "controls",
        "default", "defer", "disabled", "formnovalidate", "ismap", "itemscope",
        "loop", "multiple", "muted", "nomodule", "novalidate", "open", "playsinline",
        "readonly", "required", "reversed", "selected",
    }
)

# `border` is obsolete on these. Deliberately not <table>, where
# border="1" is still conforming.
_OBSOLETE_BORDER_TAGS: frozenset[str] = frozenset({"a", "img", "object"})

# C0 controls the HTML spec forbids in text, minus the ones it allows
# (tab, LF, FF, CR). Seen in real captures as U+001E/U+001F embedded in
# post content -- invisible, unremovable through the CMS UI, and rejected
# by every validator.
_FORBIDDEN_CONTROLS = frozenset(
    [chr(c) for c in range(0x00, 0x09)]
    + [chr(0x0B)]
    + [chr(c) for c in range(0x0E, 0x20)]
    + [chr(0x7F)]
)


@dataclass
class NormalizeStats:
    word_artifacts_unwrapped: int = 0
    obsolete_attrs_removed: int = 0
    control_chars_stripped: int = 0
    invalid_dimensions_removed: int = 0
    boolean_attrs_normalized: int = 0

    @property
    def total(self) -> int:
        return (
            self.word_artifacts_unwrapped
            + self.obsolete_attrs_removed
            + self.control_chars_stripped
            + self.invalid_dimensions_removed
            + self.boolean_attrs_normalized
        )


def _unwrap_word_artifacts(soup: BeautifulSoup, stats: NormalizeStats) -> None:
    """`<o:p>` is stamped by Microsoft Word's clipboard when content is
    pasted into the editor. It is not an HTML element, renders as nothing,
    and its children are ordinary content -- so unwrap rather than remove.
    """
    for tag in soup.find_all(lambda t: t.name and t.name.startswith("o:")):
        tag.unwrap()
        stats.word_artifacts_unwrapped += 1


def _strip_obsolete_attrs(soup: BeautifulSoup, stats: NormalizeStats) -> None:
    for tag in soup.find_all(list(_OBSOLETE_BORDER_TAGS)):
        if "border" in tag.attrs:
            del tag["border"]
            stats.obsolete_attrs_removed += 1


# Script and stylesheet bodies are skipped. A forbidden control character
# in prose is inert garbage; inside a JS string literal it is *data*, and
# removing it would change what the code does. bs4 only tags these as
# Script/Stylesheet under some parsers (not html5lib), so this checks the
# parent element instead, which holds for every parser.
_OPAQUE_PARENTS: frozenset[str] = frozenset({"script", "style"})


def _strip_control_chars(soup: BeautifulSoup, stats: NormalizeStats) -> None:
    for node in list(soup.find_all(string=True)):
        parent = node.parent
        if parent is not None and parent.name in _OPAQUE_PARENTS:
            continue
        text = str(node)
        cleaned = "".join(ch for ch in text if ch not in _FORBIDDEN_CONTROLS)
        if cleaned != text:
            stats.control_chars_stripped += len(text) - len(cleaned)
            node.replace_with(NavigableString(cleaned))


def _strip_invalid_dimensions(soup: BeautifulSoup, stats: NormalizeStats) -> None:
    """width/height on <img> must be a valid non-negative integer. Values
    like "auto" are a CSS idea leaking into an HTML attribute; browsers
    ignore them, so removing them cannot change layout.
    """
    for tag in soup.find_all("img"):
        for attr in ("width", "height"):
            value = tag.get(attr)
            if value is None:
                continue
            if not str(value).strip().isdigit():
                del tag[attr]
                stats.invalid_dimensions_removed += 1


def _normalize_boolean_attrs(soup: BeautifulSoup, stats: NormalizeStats) -> None:
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower() not in _BOOLEAN_ATTRS:
                continue
            value = tag.attrs[attr]
            if isinstance(value, list):  # multi-valued per bs4; never boolean
                continue
            if value in ("", attr.lower()):
                continue
            tag[attr] = ""
            stats.boolean_attrs_normalized += 1


def apply_normalizations(soup: BeautifulSoup, stats: NormalizeStats) -> None:
    """Apply every mechanical repair to a parsed document, in place."""
    _unwrap_word_artifacts(soup, stats)
    _strip_obsolete_attrs(soup, stats)
    _strip_control_chars(soup, stats)
    _strip_invalid_dimensions(soup, stats)
    _normalize_boolean_attrs(soup, stats)
