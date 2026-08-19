from __future__ import annotations

import stat
from pathlib import Path

from wpfreeze.upload import write_upload_script


def test_no_remote_writes_nothing(tmp_path: Path):
    assert write_upload_script(tmp_path, None) is None
    assert not (tmp_path / "upload.sh").exists()


def test_remote_writes_an_executable_script(tmp_path: Path):
    path = write_upload_script(tmp_path, "user@example.com:/var/www/html")

    assert path == tmp_path / "upload.sh"
    content = path.read_text(encoding="utf-8")
    assert 'REMOTE="user@example.com:/var/www/html"' in content
    assert "rsync -av --delete" in content
    assert path.stat().st_mode & stat.S_IXUSR


def test_default_site_rel_is_site(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@host:/path").read_text(encoding="utf-8")
    assert "cp -a site/. " in content


def test_custom_site_rel_is_used_verbatim(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@host:/path", site_rel="built/output").read_text(
        encoding="utf-8"
    )
    assert "cp -a built/output/. " in content


def test_script_also_copies_the_html_todo_and_report(tmp_path: Path):
    content = write_upload_script(tmp_path, "user@host:/path").read_text(encoding="utf-8")
    assert "cp cleanup-todo.html report.html" in content
