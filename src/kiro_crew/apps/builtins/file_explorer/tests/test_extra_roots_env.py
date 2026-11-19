"""``FE_EXTRA_ROOTS``: operator-declared extra browsing roots.

The default allow-list is the user profile plus the temp dir. A project on
another drive is unreachable unless the operator names it, and a drive or
filesystem root must never be accepted, so one typo cannot expose a volume.
"""

import os
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.file_explorer import server


@pytest.fixture
def home_and_tmp(tmp_path):
    home = tmp_path / "profile"
    tmp = tmp_path / "temp"
    for d in (home, tmp):
        d.mkdir()
    return home, tmp


def test_unset_env_adds_no_roots(monkeypatch):
    monkeypatch.delenv("FE_EXTRA_ROOTS", raising=False)

    assert server._extra_roots_from_env() == []


def test_existing_directory_is_accepted(monkeypatch, tmp_path):
    extra = tmp_path / "projects"
    extra.mkdir()
    monkeypatch.setenv("FE_EXTRA_ROOTS", str(extra))

    assert server._extra_roots_from_env() == [extra.resolve()]


def test_several_entries_use_pathsep(monkeypatch, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("FE_EXTRA_ROOTS", os.pathsep.join([str(a), str(b)]))

    assert server._extra_roots_from_env() == [a.resolve(), b.resolve()]


def test_volume_root_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("FE_EXTRA_ROOTS", Path(tmp_path.anchor).as_posix())

    assert server._extra_roots_from_env() == []


def test_missing_directory_and_plain_file_are_rejected(monkeypatch, tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    monkeypatch.setenv(
        "FE_EXTRA_ROOTS", os.pathsep.join([str(tmp_path / "nope"), str(a_file), ""])
    )

    assert server._extra_roots_from_env() == []


def test_extras_follow_home_and_tmp(monkeypatch, home_and_tmp, tmp_path):
    home, tmp = home_and_tmp
    extra = tmp_path / "projects"
    extra.mkdir()
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", False)

    roots = server._compute_allowed_roots(home, tmp, [extra])

    assert roots == [home, tmp, extra]


def test_default_call_is_unchanged(monkeypatch, home_and_tmp):
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", False)

    assert server._compute_allowed_roots(home, tmp) == [home, tmp]


def test_path_inside_extra_root_allowed_sibling_denied(monkeypatch, tmp_path):
    extra = tmp_path / "projects"
    extra.mkdir()
    inside = extra / "a.txt"
    inside.write_text("x")
    outside = tmp_path / "other.txt"
    outside.write_text("x")
    monkeypatch.setattr(server, "ALLOWED_ROOTS", [extra.resolve()])
    # Isolate the allow-list check from the host's sensitive-path rules.
    monkeypatch.setattr(server, "_is_sensitive", lambda p: False)

    assert server._safe_path(str(inside)) == inside.resolve()
    with pytest.raises(server.PathError):
        server._safe_path(str(outside))
