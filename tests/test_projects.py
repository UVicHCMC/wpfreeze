from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wpfreeze.projects import (
    AmbiguousProject,
    ProjectNotFound,
    list_projects,
    normalize_name,
    resolve_project,
    validate_name,
)


def test_validate_name_accepts_ordinary_name():
    assert validate_name("landscapes") == "landscapes"


def test_validate_name_accepts_digits_dash_underscore_dot():
    assert validate_name("site-2.name_v2") == "site-2.name_v2"


def test_validate_name_rejects_empty_string():
    with pytest.raises(ValueError):
        validate_name("")


def test_validate_name_rejects_leading_dot_or_slash():
    with pytest.raises(ValueError):
        validate_name("../evil")
    with pytest.raises(ValueError):
        validate_name("/etc/passwd")


def test_validate_name_rejects_embedded_double_dot():
    with pytest.raises(ValueError):
        validate_name("a..b")


def test_normalize_name_lowercases_and_trims():
    assert normalize_name("  Landscapes  ") == "landscapes"


# ---------------------------------------------------------------------------
# list_projects / resolve_project
# ---------------------------------------------------------------------------


def _write_config(path: Path, name: str | None, base_url: str = "https://example.com/", output_dir: str = "out") -> Path:
    data = {"base_url": base_url, "output_dir": output_dir}
    if name is not None:
        data["name"] = name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_list_projects_lists_every_config(tmp_path: Path):
    _write_config(tmp_path / "landscapes.yaml", "landscapes")
    _write_config(tmp_path / "other.yaml", None)  # falls back to stem "other"

    projects = list_projects(tmp_path)

    assert sorted(p.name for p in projects) == ["landscapes", "other"]


def test_resolve_project_by_explicit_name(tmp_path: Path):
    path = _write_config(tmp_path / "site1.yaml", "landscapes")

    project = resolve_project("landscapes", tmp_path)

    assert project.path == path
    assert project.name == "landscapes"


def test_resolve_project_by_filename_stem(tmp_path: Path):
    path = _write_config(tmp_path / "site-a.yaml", None)

    project = resolve_project("site-a", tmp_path)

    assert project.path == path


def test_resolve_project_by_path(tmp_path: Path):
    path = _write_config(tmp_path / "landscapes.yaml", "landscapes")

    project = resolve_project(str(path), tmp_path)

    assert project.path == path
    assert project.name == "landscapes"


def test_resolve_project_is_case_insensitive(tmp_path: Path):
    path = _write_config(tmp_path / "landscapes.yaml", "Landscapes")

    project = resolve_project("LANDSCAPES", tmp_path)

    assert project.path == path


def test_resolve_project_not_found_lists_known_names(tmp_path: Path):
    _write_config(tmp_path / "landscapes.yaml", "landscapes")
    _write_config(tmp_path / "other.yaml", None)

    with pytest.raises(ProjectNotFound) as excinfo:
        resolve_project("nope", tmp_path)

    assert "nope" in str(excinfo.value)
    assert "landscapes" in str(excinfo.value)
    assert "other" in str(excinfo.value)


def test_resolve_project_ambiguous_on_duplicate_explicit_name(tmp_path: Path):
    _write_config(tmp_path / "a.yaml", "dup")
    _write_config(tmp_path / "b.yaml", "dup")

    with pytest.raises(AmbiguousProject):
        resolve_project("dup", tmp_path)


def test_resolve_project_ambiguous_when_name_collides_with_another_configs_stem(tmp_path: Path):
    # a.yaml's explicit name equals b.yaml's own filename stem -- the same
    # token "b" plausibly means either one.
    _write_config(tmp_path / "a.yaml", "b")
    _write_config(tmp_path / "b.yaml", None)

    with pytest.raises(AmbiguousProject):
        resolve_project("b", tmp_path)
