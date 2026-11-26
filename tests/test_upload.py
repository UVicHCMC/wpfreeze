from __future__ import annotations

import stat
import shutil
import subprocess
from pathlib import Path

import pytest

from wpfreeze.upload import write_upload_script


def test_writes_even_with_neither_remote_configured(tmp_path: Path):
    # --local needs neither remote, so there's always something useful
    # for the script to do -- unlike the old single-remote behavior, this
    # is never a reason to refuse to write it.
    path = write_upload_script(tmp_path, None, None)
    assert path == tmp_path / "upload.sh"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert 'REMOTE=""' in content
    assert 'PROD_REMOTE=""' in content


def test_remote_writes_an_executable_script(tmp_path: Path):
    path = write_upload_script(tmp_path, "user@example.com:/var/www/html")

    assert path == tmp_path / "upload.sh"
    content = path.read_text(encoding="utf-8")
    assert 'REMOTE="user@example.com:/var/www/html"' in content
    assert "rsync -av --delete" in content
    assert path.stat().st_mode & stat.S_IXUSR


def test_prod_remote_is_written_separately_from_staging_remote(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@staging:/path", "user@prod:/path").read_text(encoding="utf-8")
    assert 'REMOTE="user@staging:/path"' in content
    assert 'PROD_REMOTE="user@prod:/path"' in content


def test_default_site_rel_is_site(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@host:/path").read_text(encoding="utf-8")
    assert 'SITE_REL="site"' in content


def test_custom_site_rel_is_used_verbatim(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@host:/path", site_rel="built/output").read_text(encoding="utf-8")
    assert 'SITE_REL="built/output"' in content


def test_script_lists_the_three_human_facing_reports(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@host:/path").read_text(encoding="utf-8")
    assert "cleanup-todo.html report.html broken-external-links.html" in content


def test_script_has_three_modes_and_a_usage_message(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@host:/path").read_text(encoding="utf-8")
    assert "--local" in content
    assert "--prod" in content
    assert "Usage:" in content


def test_prod_mode_asks_for_confirmation(tmp_path: Path):
    content = write_upload_script(tmp_path, None, "user@prod:/path").read_text(encoding="utf-8")
    assert "Continue? [y/N]" in content
    assert "Aborted." in content


def test_local_mode_never_touches_network(tmp_path: Path):
    content = write_upload_script(tmp_path, None, None).read_text(encoding="utf-8")
    # The --local branch, up to the next "if" starting the --prod branch,
    # should contain no rsync call.
    local_branch = content.split('if [ "$MODE" = "local" ]')[1].split('if [ "$MODE" = "prod" ]')[0]
    assert "rsync" not in local_branch
    assert "preview" in local_branch


def test_only_prod_mode_omits_reports(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@staging:/path", "user@prod:/path").read_text(encoding="utf-8")
    prod_branch = content.split('if [ "$MODE" = "prod" ]')[1].split("# staging")[0]
    assert "copy_reports" not in prod_branch


# --- tool cruft never ships ----------------------------------------------------


def _site_with_cruft(output_dir: Path) -> None:
    site = output_dir / "site"
    (site / "news" / ".claude" / ".cc-writes").mkdir(parents=True)
    (site / ".claude" / ".cc-writes").mkdir(parents=True)
    (site / ".claude" / "note").write_text("x", encoding="utf-8")
    (site / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    (site / "news" / "post.html").write_text("<p>post</p>", encoding="utf-8")


def _run(output_dir: Path, *args: str, stdin: str = "") -> None:
    subprocess.run(["bash", "upload.sh", *args], cwd=output_dir, input=stdin, text=True, check=True,
                   capture_output=True)


def _no_cruft(root: Path) -> None:
    assert (root / "index.html").exists() and (root / "news" / "post.html").exists()
    assert not list(root.rglob(".claude"))


def test_local_mode_drops_claude_dirs_but_leaves_site_alone(tmp_path: Path):
    _site_with_cruft(tmp_path)
    write_upload_script(tmp_path, None, None)
    _run(tmp_path, "--local")
    _no_cruft(tmp_path / "preview")
    assert (tmp_path / "site" / ".claude" / "note").exists()  # the copy is cleaned, not the source


@pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")
def test_staging_and_prod_drop_claude_dirs(tmp_path: Path):
    out = tmp_path / "out"
    _site_with_cruft(out)
    staging, prod = tmp_path / "staging", tmp_path / "prod"
    staging.mkdir()
    prod.mkdir()
    (prod / ".claude").mkdir()  # shipped by an earlier upload: --delete removes it
    write_upload_script(out, str(staging), str(prod))
    _run(out)
    _no_cruft(staging)
    _run(out, "--prod", stdin="y\n")
    _no_cruft(prod)
