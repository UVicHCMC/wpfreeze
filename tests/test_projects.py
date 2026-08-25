from __future__ import annotations

import pytest

from wpfreeze.projects import normalize_name, validate_name


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
