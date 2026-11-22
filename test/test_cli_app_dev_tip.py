"""``kirocrew app dev <name>`` names a link a non-elevated Windows user can make.

A directory symlink on Windows needs Developer Mode or an elevated shell, so the
Windows tip names a directory junction (``mklink /J``). The POSIX tip is unchanged.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import kiro_crew.apps.manager as manager
from kiro_crew.cli_commands_app import _handle_app


@pytest.fixture
def dev_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.apps.dev_mode as dev_mode

    monkeypatch.setattr(dev_mode, "set_dev_mode", lambda name, enabled, **kw: {"dev": enabled})


def _run_dev() -> None:
    _handle_app(
        types.SimpleNamespace(
            app_action="dev", name="my-app", off=False, confirm_out_of_install_root=False
        )
    )


@pytest.mark.usefixtures("dev_enabled")
def test_windows_tip_names_a_junction(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(manager, "apps_dir", lambda: tmp_path / "apps")
    monkeypatch.setattr(sys, "platform", "win32")
    _run_dev()
    out = capsys.readouterr().out
    ui_path = Path(tmp_path / "apps" / "my-app" / "ui")
    backup = f'ren "{ui_path}" ui.bak'
    link = f'mklink /J "{ui_path}" "<source>\\ui"'
    assert backup in out
    assert link in out
    assert out.index(backup) < out.index(link)
    assert "rmdir" not in out
    assert "kirocrew app dev my-app --confirm-out-of-install-root" in out
    assert "no admin needed" in out
    assert "local absolute path" in out
    assert "Developer Mode or an elevated shell" in out
    assert "kept as ui.bak" in out
    assert "Tip: symlink the installed ui/" not in out


@pytest.mark.usefixtures("dev_enabled")
def test_posix_tip_is_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    _run_dev()
    out = capsys.readouterr().out
    assert "Tip: symlink the installed ui/ to your source tree for zero-copy edits." in out
    assert "mklink" not in out
