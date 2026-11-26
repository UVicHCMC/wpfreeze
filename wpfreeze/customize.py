"""Post-build customization: a site owner's "done, except for these little
adjustments" list, applied to every build.

Edits like "remove the header email", "delete the contact page" or "reword
this one sentence" are one owner's editorial choices, not heuristics that
hold for other sites, so they don't belong in policy.py. Hand-editing
`site/` doesn't work either: the next `build` overwrites it, and the link
check, search index and reports would describe a site that isn't the one
shipped.

So a project may keep a script at `<output_dir>/customize.py` (beside
`upload.sh`; `build` owns only `site/`, so it survives rebuilds) defining

    def customize(site):
        site.remove("#top-header")
        site.delete_page("contact.html")
        ...

`build` runs it straight after the pages are written and before
verification, Pagefind, pruning and the reports, so all of those describe
the customized site. Whether it runs is decided in cli.py
(`_maybe_run_customize`): `customize: true` in the site YAML runs it,
`customize: false` never does, and with the key absent the build asks.

Every helper is all-or-nothing. Edits are held in memory and written only
by `Site.commit()`, after the script returns without raising, so a script
that fails halfway leaves `site/` exactly as `build_site` wrote it.

A helper that matches nothing is an error, not a no-op: the capture is
frozen, so a rebuild is deterministic, and a selector that stops matching
means the script is wrong -- silently carrying on would ship the content
the owner asked to have removed. Every action also records the text of
what it matched, so a selector that matches the *wrong* element is visible
in the build output instead of discovered by the owner.
"""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from posixpath import normpath
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup, NavigableString, Tag

from wpfreeze.outputs import href_quote
from wpfreeze.urlnorm import safe_urlsplit

logger = logging.getLogger(__name__)

SCRIPT_NAME = "customize.py"

# How much of each matched element's text an action keeps as evidence of
# what it hit, and how many such samples. Enough to tell "Follow Us on
# Social Medias" from "Past Wrongs, Future Choices", not a transcript.
_SAMPLE_CHARS = 80
_SAMPLES_PER_ACTION = 5

_HTML_SUFFIXES = (".html", ".htm")


class CustomizeError(Exception):
    """The script, or one of its helper calls, can't be carried out as
    written. Always an authoring problem the owner of the script has to fix;
    the build stops before anything is written."""


@dataclass
class CustomizeAction:
    helper: str
    args: str
    matches: int = 0
    pages_changed: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class CustomizeStats:
    """What the hook did this build, for build-report.json and the
    cleanup checklist. `ran` False with a `reason` is as informative as a
    run: "a script exists and was skipped" must never read like "there is
    no script"."""

    ran: bool = False
    # None when no script exists. Otherwise one of: "ran", "declined"
    # (asked, answered no), "unattended" (couldn't ask), "disabled"
    # (`customize: false`).
    reason: str | None = None
    script: str | None = None
    sha256: str | None = None
    actions: list[CustomizeAction] = field(default_factory=list)
    pages_changed: list[str] = field(default_factory=list)
    pages_deleted: list[str] = field(default_factory=list)


def _sample(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= _SAMPLE_CHARS else text[: _SAMPLE_CHARS - 1] + "…"


def _normalize_page(path: str) -> str:
    """A page argument as a site-relative POSIX path: "/contact.html",
    "contact.html" and "./contact.html" are all the same page."""
    cleaned = normpath(path.strip().lstrip("/"))
    if cleaned.startswith("..") or cleaned in ("", "."):
        raise CustomizeError(f"{path!r} is not a page inside the site")
    return cleaned


def resolve_local_href(page: str, href: str) -> str | None:
    """The site-relative path `href` on `page` points at, or None if it
    isn't a local file reference (external, mailto:, a bare fragment,
    unparseable, or escaping the site root). Mirrors verify_site's
    resolution -- query and fragment ignored, percent-encoding decoded --
    and additionally treats a root-absolute "/x.html" as site-relative."""
    value = href.strip()
    if not value or value.startswith("//"):
        return None
    parts = safe_urlsplit(value)
    if parts is None or parts.scheme:
        return None
    path = unquote(parts.path)
    if not path:
        return None
    if path.startswith("/"):
        resolved = normpath(path.lstrip("/"))
    else:
        base = Path(page).parent.as_posix()
        resolved = normpath(path if base == "." else f"{base}/{path}")
    if resolved.startswith(".."):
        return None
    return resolved


class _Page:
    """One page's working copy, held as text or as a parsed tree and
    converted only when the next action needs the other form -- so a
    `replace` after a `remove` on the same page sees the post-`remove`
    markup, in script order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._text: str | None = path.read_text(encoding="utf-8")
        self._soup: BeautifulSoup | None = None
        self.dirty = False

    def soup(self) -> BeautifulSoup:
        if self._soup is None:
            # html5lib: the parser build_site itself writes with, so a
            # round trip changes nothing but the edits made here.
            self._soup = BeautifulSoup(self._text, "html5lib")
            self._text = None
        return self._soup

    def text(self) -> str:
        if self._text is None:
            self._text = str(self._soup)
            self._soup = None
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self._soup = None
        self.dirty = True

    def raw_contains(self, needle: str) -> bool:
        """Cheap prefilter on whichever form is current."""
        return needle in (self._text if self._text is not None else str(self._soup))


class Site:
    """The handle a customize.py script receives. All paths are relative to
    the built site's root, as they appear under `site/`."""

    def __init__(self, site_dir: Path) -> None:
        self.site_dir = site_dir
        self._all_pages = sorted(
            p.relative_to(site_dir).as_posix()
            for p in site_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in _HTML_SUFFIXES
        )
        self._pages: dict[str, _Page] = {}
        self._deleted: list[str] = []
        self._htaccess_drops: set[str] = set()
        self._htaccess_redirects: dict[str, str] = {}
        self.actions: list[CustomizeAction] = []

    # -- page selection -------------------------------------------------

    def _live(self) -> list[str]:
        deleted = set(self._deleted)
        return [p for p in self._all_pages if p not in deleted]

    def _page(self, rel: str) -> _Page:
        page = self._pages.get(rel)
        if page is None:
            page = self._pages[rel] = _Page(self.site_dir / rel)
        return page

    def _select(self, pages: list[str] | str | None) -> tuple[list[str], bool]:
        """(pages to act on, whether they were named explicitly). A named
        page that doesn't exist, or a pattern that matches nothing, is an
        error: the script is pointing at something that isn't there."""
        if pages is None:
            return self._live(), False
        if isinstance(pages, str):
            pages = [pages]
        live = self._live()
        live_set = set(live)
        chosen: list[str] = []
        for spec in pages:
            if any(ch in spec for ch in "*?["):
                hits = [p for p in live if fnmatch.fnmatchcase(p, spec.lstrip("/"))]
                if not hits:
                    raise CustomizeError(f"pages={spec!r} matches no page in the built site")
                chosen.extend(h for h in hits if h not in chosen)
            else:
                rel = _normalize_page(spec)
                if rel not in live_set:
                    gone = " (deleted earlier in this script)" if rel in self._deleted else ""
                    raise CustomizeError(f"page {rel!r} does not exist in the built site{gone}")
                if rel not in chosen:
                    chosen.append(rel)
        return chosen, True

    def _record(self, action: CustomizeAction, page: str, matched_text: str | None = None) -> None:
        if page not in action.pages_changed:
            action.pages_changed.append(page)
        if matched_text is not None and len(action.samples) < _SAMPLES_PER_ACTION:
            sample = _sample(matched_text)
            if sample not in action.samples:
                action.samples.append(sample)

    def _require_matches(self, action: CustomizeAction, pages: list[str], explicit: bool, what: str) -> None:
        if explicit:
            missed = [p for p in pages if p not in action.pages_changed]
            if missed:
                raise CustomizeError(f"{action.helper}({action.args}): {what} on {', '.join(missed)}")
        elif not action.matches:
            raise CustomizeError(f"{action.helper}({action.args}): {what} on any page")

    # -- helpers --------------------------------------------------------

    def remove(self, selector: str, pages: list[str] | str | None = None) -> None:
        """Remove every element matching CSS `selector` (soupsieve syntax,
        including `:-soup-contains("text")`). With `pages` omitted, every
        page; it is an error if nothing matches anywhere. With `pages`
        named, it is an error if any named page has no match."""
        targets, explicit = self._select(pages)
        action = CustomizeAction("remove", repr(selector) + (f", pages={pages!r}" if pages is not None else ""))
        for rel in targets:
            soup = self._page(rel).soup()
            try:
                found = soup.select(selector)
            except Exception as exc:  # soupsieve raises its own SelectorSyntaxError
                raise CustomizeError(f"remove({selector!r}): invalid selector: {exc}") from exc
            # An element nested inside another match goes with its
            # ancestor; count only outermost matches.
            found_ids = {id(el) for el in found}
            outermost = [el for el in found if not any(id(a) in found_ids for a in el.parents)]
            for el in outermost:
                self._record(action, rel, el.get_text(" "))
                el.decompose()
            if outermost:
                action.matches += len(outermost)
                self._page(rel).dirty = True
        self._require_matches(action, targets, explicit, f"selector {selector!r} matched nothing")
        self.actions.append(action)

    def unwrap_links_to(self, url_regex: str, pages: list[str] | str | None = None) -> None:
        """Replace every `<a>` whose href matches `url_regex` (re.search)
        with its own contents: the text stays, the link goes. Same
        zero-match rules as `remove`."""
        try:
            pattern = re.compile(url_regex)
        except re.error as exc:
            raise CustomizeError(f"unwrap_links_to({url_regex!r}): invalid regex: {exc}") from exc
        targets, explicit = self._select(pages)
        action = CustomizeAction(
            "unwrap_links_to", repr(url_regex) + (f", pages={pages!r}" if pages is not None else "")
        )
        for rel in targets:
            soup = self._page(rel).soup()
            hits = [a for a in soup.find_all("a", href=True) if pattern.search(a["href"])]
            for a in hits:
                self._record(action, rel, f"{a.get_text(' ')} <{a['href']}>")
                a.unwrap()
            if hits:
                action.matches += len(hits)
                self._page(rel).dirty = True
        self._require_matches(action, targets, explicit, f"no link matching {url_regex!r}")
        self.actions.append(action)

    def replace(self, page: str, old: str, new: str) -> None:
        """Replace `old` with `new` in one page's HTML. `old` is markup,
        not visible text -- copy it from the built file under `site/` --
        and must occur exactly once."""
        (rel,), _ = self._select([page])
        working = self._page(rel)
        text = working.text()
        count = text.count(old)
        action = CustomizeAction("replace", f"{rel!r}, {_sample(old)!r}")
        if count != 1:
            hint = (
                " -- `old` is the page's HTML, not its visible text; copy it from "
                f"site/{rel}"
                if count == 0
                else "; make `old` longer so it identifies one place"
            )
            raise CustomizeError(f"replace on {rel}: `old` occurs {count} times, expected exactly 1{hint}")
        working.set_text(text.replace(old, new, 1))
        action.matches = 1
        self._record(action, rel, BeautifulSoup(old, "html.parser").get_text(" "))
        self.actions.append(action)

    def delete_page(self, page: str, redirect_to: str | None = None) -> None:
        """Delete a page from the built site and every link to it.

        A link that is the sole content of a list item (a menu entry) takes
        its `<li>` with it; any other link is unwrapped, keeping its text.
        Only `<a href>` is handled -- verification, which runs after this,
        reports any other reference (an iframe, a form action) as broken.

        In `site/.htaccess`, redirects to the page are dropped, so its old
        URLs 404 -- or, with `redirect_to` (another page, e.g.
        "index.html"), they and the page's own URL 301 there instead.

        Errors only if the page doesn't exist; a page nothing links to is
        a normal case (a form's thank-you page, say), not a mistake.
        """
        (rel,), _ = self._select([page])
        if redirect_to is not None:
            target = _normalize_page(redirect_to)
            if target == rel or target not in set(self._live()):
                raise CustomizeError(f"delete_page({rel!r}): redirect_to {redirect_to!r} is not a page in the built site")
        action = CustomizeAction(
            "delete_page", repr(rel) + (f", redirect_to={redirect_to!r}" if redirect_to is not None else "")
        )
        self._deleted.append(rel)
        self._pages.pop(rel, None)

        name = rel.rsplit("/", 1)[-1]
        needles = {name, quote(name)}
        for other in self._live():
            working = self._page(other)
            if not any(working.raw_contains(n) for n in needles):
                continue
            soup = working.soup()
            for a in soup.find_all("a", href=True):
                if resolve_local_href(other, a["href"]) != rel:
                    continue
                li = a.parent
                if isinstance(li, Tag) and li.name == "li" and _sole_content(li, a):
                    self._record(action, other, f"menu item: {a.get_text(' ')}")
                    li.decompose()
                else:
                    self._record(action, other, f"link: {a.get_text(' ')}")
                    a.unwrap()
                action.matches += 1
                working.dirty = True

        if redirect_to is not None:
            self._htaccess_redirects[rel] = _normalize_page(redirect_to)
        else:
            self._htaccess_drops.add(rel)
        self.actions.append(action)

    # -- writing --------------------------------------------------------

    def commit(self) -> CustomizeStats:
        """Write every change to disk. Called once, after the script has
        returned without raising."""
        stats = CustomizeStats()
        for rel, working in self._pages.items():
            if working.dirty:
                working.path.write_text(working.text(), encoding="utf-8")
                stats.pages_changed.append(rel)
        for rel in self._deleted:
            (self.site_dir / rel).unlink()
            stats.pages_deleted.append(rel)
        if self._htaccess_drops or self._htaccess_redirects:
            self._edit_htaccess()
        stats.pages_changed.sort()
        stats.actions = self.actions
        return stats

    def _edit_htaccess(self) -> None:
        path = self.site_dir / ".htaccess"
        if not path.exists():
            return  # no redirect map shipped with this capture: nothing to keep in step
        lines = path.read_text(encoding="utf-8").splitlines()
        drop = {"/" + href_quote(p) for p in self._htaccess_drops}
        retarget = {"/" + href_quote(p): "/" + href_quote(t) for p, t in self._htaccess_redirects.items()}
        out: list[str] = []
        for line in lines:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "Redirect":
                if parts[3] in drop:
                    continue
                if parts[3] in retarget:
                    line = f"{parts[0]} {parts[1]} {parts[2]} {retarget[parts[3]]}"
            out.append(line)
        if retarget:
            # The page's own URL (/contact/, /contact.html) never appears in
            # an explicit Redirect line -- the mechanical ^(.*)/$ rule maps
            # it to the now-missing file -- so it needs a rule of its own,
            # ahead of that one.
            rules = [
                f"RewriteRule ^{re.escape(href_quote(p[: -len(Path(p).suffix)]))}(/|{re.escape(Path(p).suffix)})?$ "
                f"/{href_quote(t)} [R=301,L]"
                for p, t in self._htaccess_redirects.items()
            ]
            try:
                at = out.index("RewriteEngine On") + 1
            except ValueError:
                out = ["RewriteEngine On", *out]
                at = 1
            out[at:at] = ["# customize.py: deleted pages redirected", *rules]
        path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _sole_content(li: Tag, a: Tag) -> bool:
    for child in li.children:
        if child is a:
            continue
        if isinstance(child, NavigableString) and not child.strip():
            continue
        return False
    return True


def script_path(output_dir: Path) -> Path:
    return output_dir / SCRIPT_NAME


def run_customize_script(script: Path, site_dir: Path) -> CustomizeStats:
    """Run `script`'s customize(site) against `site_dir` and commit its
    edits. Raises CustomizeError for anything the script's author must fix;
    any other exception from inside the script propagates with its own
    traceback (filename and line point into customize.py)."""
    source = script.read_text(encoding="utf-8")
    namespace: dict = {"__name__": "wpfreeze_customize", "__file__": str(script)}
    # exec(compile(...)) rather than importlib: same traceback, but no
    # __pycache__ left beside site/ and no sys.modules entry to go stale.
    exec(compile(source, str(script), "exec"), namespace)  # noqa: S102 -- the owner's own script, opted into
    func = namespace.get("customize")
    if not callable(func):
        raise CustomizeError(f"{script} does not define a customize(site) function")
    site = Site(site_dir)
    func(site)
    stats = site.commit()
    stats.ran = True
    stats.reason = "ran"
    stats.script = str(script)
    stats.sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return stats


def format_customize_summary(stats: CustomizeStats) -> str:
    if not stats.ran:
        return ""
    lines = [
        f"Customized ({SCRIPT_NAME}): {len(stats.actions)} action(s), "
        f"{len(stats.pages_changed)} page(s) changed, {len(stats.pages_deleted)} deleted"
    ]
    for action in stats.actions:
        lines.append(
            f"  {action.helper}({action.args}): {action.matches} match(es) on {len(action.pages_changed)} page(s)"
        )
        for sample in action.samples:
            lines.append(f"      {sample!r}")
    return "\n".join(lines)
