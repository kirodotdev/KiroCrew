"""``kirocrew policy validate`` reports a profiles directory it cannot list.

The profile store records a directory it cannot enumerate (a file where the
directory should be, or EACCES) as one unrecoverable entry, because honouring it
as "empty" would silently drop every profile's narrowing. ``policy validate``
printed "(no profiles directory)" and "valid" for it. Under a policy ceiling the
boot gate refuses that entry, so validate now fails with it; a standalone host
runs policy-only on it, so validate warns and still exits 0, as on main.
"""

from __future__ import annotations

import argparse
import os
import types

import pytest

from kiro_crew import platform_compat
from kiro_crew.cli_commands_security import _policy
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

_POSIX_DIR_MODES = pytest.mark.skipif(
    not platform_compat.IS_POSIX or os.geteuid() == 0,
    reason="needs POSIX directory modes, which root ignores",
)


@pytest.fixture(autouse=True)
def _standalone_host(_floor_monkeypatch):
    # On the floor stack, not the test's own monkeypatch, so a test's undo() cannot drop it.
    _floor_monkeypatch.setattr(
        ctx_mod, "current_context", lambda: types.SimpleNamespace(governance=None)
    )
    yield
    gp.reset_store()


def _governed(monkeypatch) -> None:
    ceiling = types.SimpleNamespace(version=2, controls={}, fallback_profile=None)
    monkeypatch.setattr(
        ctx_mod, "current_context", lambda: types.SimpleNamespace(governance=ceiling)
    )


def _validate(capsys) -> tuple[object, str]:
    code: object = 0
    try:
        _policy(argparse.Namespace(policy_action="validate"))
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr().out


def _file_at_profiles_path(monkeypatch, tmp_path) -> None:
    not_a_dir = tmp_path / "profiles"
    not_a_dir.write_text("{}")
    monkeypatch.setattr(gp, "_PROFILES_DIR", not_a_dir)
    gp.reset_store()


def test_a_file_at_the_profiles_path_fails_validation_under_a_ceiling(
    monkeypatch, tmp_path, capsys
):
    _governed(monkeypatch)
    _file_at_profiles_path(monkeypatch, tmp_path)

    code, out = _validate(capsys)
    assert code == 1, out
    assert "INVALID" in out and "✅ valid" not in out


def test_a_file_at_the_profiles_path_only_warns_on_a_standalone_host(monkeypatch, tmp_path, capsys):
    _file_at_profiles_path(monkeypatch, tmp_path)

    code, out = _validate(capsys)
    assert code == 0, out
    assert "cannot be listed" in out and "✅ valid" in out
    assert "(no profiles directory)" not in out


@_POSIX_DIR_MODES
def test_an_unlistable_profiles_directory_fails_validation_under_a_ceiling(
    monkeypatch, tmp_path, capsys
):
    _governed(monkeypatch)
    unlistable = tmp_path / "profiles"
    unlistable.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", unlistable)
    gp.reset_store()
    unlistable.chmod(0o300)
    try:
        code, out = _validate(capsys)
    finally:
        unlistable.chmod(0o700)
    assert code == 1, out
    assert "INVALID" in out and "✅ valid" not in out


def test_a_missing_profiles_directory_is_still_valid(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(gp, "_PROFILES_DIR", tmp_path / "absent")
    gp.reset_store()

    code, out = _validate(capsys)
    assert code == 0, out
    assert "(no profiles directory)" in out and "✅ valid" in out
