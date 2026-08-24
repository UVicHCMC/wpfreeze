from __future__ import annotations

import stat
from pathlib import Path

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
