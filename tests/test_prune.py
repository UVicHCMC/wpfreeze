"""prune.py: a rebuild removes what the previous build wrote and this one
didn't, and nothing else."""

from __future__ import annotations

import json
import os
from pathlib import Path

from wpfreeze.cli import SiteConfig, run_build
from wpfreeze.manifest import Manifest, Status
from wpfreeze.prune import LEDGER_NAME, load_ledger, prune_site, write_ledger


def _capture(output_dir: Path, pages: dict[str, str]) -> SiteConfig:
    """A capture whose records map URL -> output_path, one raw file each."""
    manifest = Manifest()
    (output_dir / "raw").mkdir(parents=True, exist_ok=True)
    for i, (url, output_path) in enumerate(pages.items()):
        record = manifest.get_or_create(url)
        record.status = Status.FETCHED.value
        record.http_status = 200
        record.output_path = output_path
        record.local_path = f"raw/{i}.html"
        record.content_type = "text/html"
        (output_dir / record.local_path).write_text(f"<html><body>page {i}</body></html>", encoding="utf-8")
    manifest.save(output_dir / "manifest.json")
    return SiteConfig(base_url="https://example.com/subsite/", output_dir=output_dir)


def _relocate(output_dir: Path, url: str, new_output_path: str) -> None:
    manifest = Manifest.load(output_dir / "manifest.json")
    manifest.get_or_create(url).output_path = new_output_path
    manifest.save(output_dir / "manifest.json")


def test_a_rebuild_removes_the_output_path_a_capture_no_longer_produces(tmp_path: Path, capsys):
    """The reseaufranco case: after c467eab, a subdirectory install's home
    page moved from site/subsite.html to site/index.html, and the old file
    stayed behind and shipped."""
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/subsite/": "/subsite.html",
                            "https://example.com/subsite/about/": "/subsite/about.html"})
    run_build(config, None, verify=False)
    assert (out / "site" / "subsite.html").exists()

    _relocate(out, "https://example.com/subsite/", "/index.html")
    capsys.readouterr()
    run_build(config, None, verify=False)

    assert (out / "site" / "index.html").exists()
    assert not (out / "site" / "subsite.html").exists()
    assert (out / "site" / "subsite" / "about.html").exists()
    assert "removed 1 file(s)" in capsys.readouterr().out


def test_a_directory_emptied_by_pruning_goes_too(tmp_path: Path):
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/a/b/": "/a/b/index.html"})
    run_build(config, None, verify=False)
    _relocate(out, "https://example.com/a/b/", "/index.html")
    run_build(config, None, verify=False)

    assert not (out / "site" / "a").exists()


def test_a_stale_file_edited_since_the_last_build_is_kept_and_reported(tmp_path: Path, capsys):
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/": "/old.html"})
    run_build(config, None, verify=False)
    (out / "site" / "old.html").write_text("someone's hand edit", encoding="utf-8")

    _relocate(out, "https://example.com/", "/index.html")
    capsys.readouterr()
    run_build(config, None, verify=False)

    assert (out / "site" / "old.html").read_text(encoding="utf-8") == "someone's hand edit"
    assert "kept 1 file(s)" in capsys.readouterr().out
    report = json.loads((out / "build-report.json").read_text(encoding="utf-8"))
    assert report["prune"]["kept_modified"] == ["old.html"]


def test_a_same_size_edit_is_still_an_edit(tmp_path: Path):
    """Size alone would miss an edit that happens to keep the byte count."""
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/": "/old.html"})
    run_build(config, None, verify=False)
    old = out / "site" / "old.html"
    st = old.stat()
    os.utime(old, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    _relocate(out, "https://example.com/", "/index.html")
    run_build(config, None, verify=False)

    assert old.exists()


def test_a_file_no_build_wrote_is_listed_but_never_deleted(tmp_path: Path, capsys):
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/": "/index.html"})
    run_build(config, None, verify=False)
    (out / "site" / "notes.txt").write_text("mine", encoding="utf-8")

    capsys.readouterr()
    run_build(config, None, verify=False)

    assert (out / "site" / "notes.txt").exists()
    assert "notes.txt" in capsys.readouterr().out
    report = json.loads((out / "build-report.json").read_text(encoding="utf-8"))
    assert report["prune"]["untracked"] == ["notes.txt"]


def test_without_a_ledger_nothing_is_deleted(tmp_path: Path, capsys):
    """A site built before the ledger existed: the orphan is reported, so
    it can be removed by hand, but not guessed at."""
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/subsite/": "/index.html"})
    (out / "site").mkdir()
    (out / "site" / "subsite.html").write_text("orphan from an older build", encoding="utf-8")

    run_build(config, None, verify=False)

    assert (out / "site" / "subsite.html").exists()
    printed = capsys.readouterr().out
    assert "subsite.html" in printed
    assert "no record of an earlier build here" in printed


def test_a_clean_rebuild_says_nothing_about_cleanup(tmp_path: Path, capsys):
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/": "/index.html"})
    run_build(config, None, verify=False)
    capsys.readouterr()
    run_build(config, None, verify=False)

    assert "Site directory cleanup" not in capsys.readouterr().out


def test_the_ledger_lives_beside_the_site_and_out_of_the_build_report(tmp_path: Path):
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/": "/index.html"})
    run_build(config, None, verify=False)

    assert (out / LEDGER_NAME).exists()
    assert not (out / "site" / LEDGER_NAME).exists()
    report = json.loads((out / "build-report.json").read_text(encoding="utf-8"))
    assert "written" not in report
    assert report["prune"]["removed"] == []


def test_builds_to_different_site_dirs_never_prune_each_other(tmp_path: Path):
    out = tmp_path / "out"
    config = _capture(out, {"https://example.com/": "/old.html"})
    other = tmp_path / "elsewhere"
    run_build(config, None, verify=False)
    run_build(config, other, verify=False)

    _relocate(out, "https://example.com/", "/index.html")
    run_build(config, other, verify=False)

    assert not (other / "old.html").exists()
    assert (out / "site" / "old.html").exists()  # untouched: its own ledger entry wasn't consulted


def test_ledger_paths_outside_the_site_are_ignored(tmp_path: Path):
    site = tmp_path / "out" / "site"
    site.mkdir(parents=True)
    outside = tmp_path / "out" / "precious.txt"
    outside.write_text("keep", encoding="utf-8")
    st = outside.stat()
    previous = {"files": {"../precious.txt": [st.st_size, st.st_mtime_ns],
                          str(outside): [st.st_size, st.st_mtime_ns]},
                "dirs": ["..", "/"]}

    prune_site(site, previous, written=set(), owned_dirs=set())

    assert outside.exists()
    assert site.exists()


def test_a_retired_search_bundle_is_removed_and_a_live_one_kept(tmp_path: Path):
    out = tmp_path / "out"
    site = out / "site"
    (site / "pagefind").mkdir(parents=True)
    (site / "pagefind" / "pagefind.js").write_text("x", encoding="utf-8")
    write_ledger(out, site, written=set(), owned_dirs={"pagefind"})

    kept = prune_site(site, load_ledger(out, site), written=set(), owned_dirs={"pagefind"})
    assert (site / "pagefind" / "pagefind.js").exists()
    assert kept.untracked == []

    retired = prune_site(site, load_ledger(out, site), written=set(), owned_dirs=set())
    assert not (site / "pagefind").exists()
    assert retired.removed_dirs == ["pagefind"]


def test_an_unreadable_ledger_means_delete_nothing(tmp_path: Path):
    out = tmp_path / "out"
    site = out / "site"
    site.mkdir(parents=True)
    (out / LEDGER_NAME).write_text("{not json", encoding="utf-8")

    assert load_ledger(out, site) is None
