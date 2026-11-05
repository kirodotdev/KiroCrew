"""``kirocrew app disable`` / ``uninstall``: the name is checked before any cleanup.

Both verbs clean up an app's crons, skills, agents and MCP servers by joining its
name onto a data-home path. ``disable_app`` and ``uninstall_app`` refuse a name
that is not one directory name, but they run AFTER that cleanup, so the refusal
came too late. These tests pin the order (name first, cleanup after) and that a
failed deregistration is reported with a non-zero exit instead of a success line.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from cli_commands_coverage_helpers import _ns

from kiro_crew import cli_commands as cc

_BAD_NAMES = ["..", "../other", "a/b", "a\\b", ".", "", "C:", "c:demo"]


def _registration(errors: list[str] | None = None) -> Any:
    return SimpleNamespace(agents=[], skills=[], crons=[], mcp_servers=[], errors=errors or [])


def _ok(name: str = "demo") -> Any:
    return SimpleNamespace(ok=True, name=name, message="done", error="")


@pytest.fixture
def lifecycle_mocks() -> Any:
    """Every collaborator of the two verbs, with no gateway answering."""
    with (
        patch("kiro_crew.cli_commands._run_app_action_through_gateway", return_value=False) as gw,
        patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler") as cleanup,
        patch("kiro_crew.cli_commands.deregister_app", return_value=_registration()) as deregister,
        patch("kiro_crew.cli_commands.trust_grant_removal_blocked", return_value="") as grant,
        patch("kiro_crew.cli_commands.disable_app", return_value=_ok()) as disable,
        patch("kiro_crew.cli_commands.uninstall_app", return_value=_ok()) as uninstall,
        patch("kiro_crew.cli_commands._app_declares_backend", return_value=False),
        patch("kiro_crew.cli_commands.recorded_backend_port", return_value=None),
        patch(
            "kiro_crew.cli_commands.discard_app_session_pointers",
            return_value=SimpleNamespace(dropped=0, deferred=False, error=""),
        ) as pointers,
        patch("kiro_crew.cli_commands._print_pointer_cleanup"),
    ):
        yield SimpleNamespace(
            gateway=gw,
            cleanup=cleanup,
            deregister=deregister,
            grant=grant,
            disable=disable,
            uninstall=uninstall,
            pointers=pointers,
        )


@pytest.mark.parametrize("action", ["disable", "uninstall"])
@pytest.mark.parametrize("name", _BAD_NAMES)
def test_bad_name_is_refused_before_any_cleanup(
    action: str, name: str, lifecycle_mocks: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cc._handle_app(_ns(app_action=action, name=name, purge_data=False))
    assert exc.value.code == 1
    assert "unsafe app name" in capsys.readouterr().err
    for step in ("gateway", "cleanup", "deregister", "grant", "disable", "uninstall"):
        getattr(lifecycle_mocks, step).assert_not_called()


def test_dotdot_disable_leaves_data_home_links_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real files: skill cleanup for ``..`` would walk the data home itself."""
    home = tmp_path / "kirocrew-home"
    (home / "skills").mkdir(parents=True)
    target = tmp_path / "linked"
    target.mkdir()
    link = home / "keep-me"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("this host cannot create directory symlinks")
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    # Skill cleanup runs for real; the other deregistration steps and cron
    # cleanup reach outside the data home, so they are stubbed.
    with (
        patch("kiro_crew.cli_commands._run_app_action_through_gateway", return_value=False),
        patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
        patch("kiro_crew.apps.bridges._deregister_agents", return_value=0),
        patch("kiro_crew.apps.bridges._deregister_crons"),
        patch("kiro_crew.apps.bridges._deregister_mcp_servers", return_value=0),
        pytest.raises(SystemExit),
    ):
        cc._handle_app(_ns(app_action="disable", name=".."))

    assert link.is_symlink(), "the data home's own link was removed"


def test_disable_reports_a_failed_deregistration(
    lifecycle_mocks: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    lifecycle_mocks.deregister.return_value = _registration(["skill deregistration failed: EACCES"])
    with pytest.raises(SystemExit) as exc:
        cc._handle_app(_ns(app_action="disable", name="demo"))
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "skill deregistration failed: EACCES" in captured.err
    assert "marked disabled" in captured.err
    assert "✅" not in captured.out
    lifecycle_mocks.disable.assert_called_once_with("demo")


def test_uninstall_still_removes_files_when_deregistration_fails(
    lifecycle_mocks: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no ``--force``, so a failing deregistration must not strand the app."""
    lifecycle_mocks.deregister.return_value = _registration(["agent deregistration failed: EIO"])
    with pytest.raises(SystemExit) as exc:
        cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=False))
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "agent deregistration failed: EIO" in captured.err
    assert "demo was uninstalled, but some of its resources could not be removed" in captured.err
    lifecycle_mocks.uninstall.assert_called_once_with("demo", keep_data=True)
    lifecycle_mocks.pointers.assert_called_once_with("demo")


@pytest.mark.parametrize("action", ["disable", "uninstall"])
def test_good_name_still_runs_every_step(
    action: str, lifecycle_mocks: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    cc._handle_app(_ns(app_action=action, name="demo", purge_data=False))
    lifecycle_mocks.cleanup.assert_called_once_with("demo")
    lifecycle_mocks.deregister.assert_called_once_with("demo")
    assert "✅" in capsys.readouterr().out
