"""Native Windows refuses a ``command`` cron when it is created, not only when it fires.

``cron_script._resolve_command_shell`` returns ``None`` on Windows by design, so
every command cron fails at its first wake there. Each create door (MCP
``cron_add``, ``kirocrew cron add --command``, app manifest crons and the app
cron SDK) runs the same refusal up front, with the fire step's own text, and
stores nothing. POSIX hosts are unchanged, and the import and fire-time re-vet
(``_vet_shell_command``) keep accepting a clean command on Windows.
"""

from __future__ import annotations

import argparse
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app_bridges_helpers import app_env_fixture  # noqa: F401

from kiro_crew import platform_compat
from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronService
from kiro_crew.cron_script import _no_command_shell_message


@pytest.fixture()
def on_windows(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)


@pytest.fixture()
def on_posix(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)


def _windows_text() -> str:
    with patch.object(platform_compat, "IS_WINDOWS", True):
        return _no_command_shell_message().removeprefix("❌ ")


def _mcp_add(tmp_path, args: dict):
    from kiro_crew.mcp_cron import _call_tool_inner

    with (
        patch("kiro_crew.mcp_cron.config_dir", return_value=tmp_path),
        patch("kiro_crew.mcp_cron.CronService") as svc_cls,
        patch("kiro_crew.mcp_cron._resolve_session_key", return_value=""),
    ):
        svc_cls.return_value.add_job.return_value = MagicMock(id="new1", name="cmd-job")
        result = _call_tool_inner("cron_add", args)
    return result, svc_cls.return_value.add_job


class TestMcpCronAdd:
    def test_windows_refuses_command_and_stores_nothing(
        self, tmp_path, named_cron_caller, on_windows
    ):
        result, add_job = _mcp_add(
            tmp_path, {"name": "cmd-job", "command": "echo hi", "every": 3600}
        )
        assert result == "Error: " + _windows_text()
        add_job.assert_not_called()

    def test_windows_deny_list_vet_still_answers_first(
        self, tmp_path, named_cron_caller, on_windows
    ):
        result, add_job = _mcp_add(
            tmp_path, {"name": "cmd-job", "command": "cat ~/.aws/credentials", "every": 3600}
        )
        assert result.startswith("Error:")
        assert "POSIX shell" not in result
        add_job.assert_not_called()

    def test_posix_still_stores_command(self, tmp_path, named_cron_caller, on_posix):
        result, add_job = _mcp_add(
            tmp_path, {"name": "cmd-job", "command": "echo hi", "every": 3600}
        )
        assert not result.startswith("Error:")
        assert add_job.call_args.kwargs["command"] == "echo hi"

    def test_windows_message_cron_is_unaffected(self, tmp_path, named_cron_caller, on_windows):
        result, add_job = _mcp_add(tmp_path, {"name": "msg-job", "message": "hi", "every": 3600})
        assert not result.startswith("Error:")
        add_job.assert_called_once()


def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        cron_action="add",
        name="job",
        message="",
        every=600,
        cron_expr=None,
        at=None,
        timezone="",
        channel=None,
        agent="",
        script="",
        shell_command="echo hi",
        timeout=None,
        timeout_secs=None,
        model="",
        persistent_session=True,
        minimal_context=False,
        hide_in_chat=False,
        silent=False,
        folder="",
        approval_mode="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (config_dir() / "crons").mkdir(parents=True)
    return root


class TestCliCronAdd:
    def test_windows_refuses_command_exit_1_nothing_stored(self, home, capsys, on_windows):
        with patch("kiro_crew.cli_commands.sel"), pytest.raises(SystemExit) as exc:
            _cron(_ns())
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert err.strip() == "Error: " + _windows_text()
        assert CronService(base_dir=config_dir()).list_jobs(include_disabled=True) == []

    def test_posix_still_stores_command(self, home, capsys, on_posix):
        with patch("kiro_crew.cli_commands.sel"):
            _cron(_ns())
        (job,) = CronService(base_dir=config_dir()).list_jobs(include_disabled=True)
        assert job.command == "echo hi"


class TestAppDoors:
    def test_cron_sdk_windows_raises_and_stores_nothing(self, tmp_path, on_windows):
        from kiro_crew.apps.cron_sdk import CronSDK

        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="cron command rejected: Error: No POSIX shell"):
            CronSDK("some-app", svc).add_job(
                name="collect", message="", command="echo hi", cron_expr="* * * * *"
            )
        assert svc.list_jobs(include_disabled=True) == []

    def test_manifest_bridge_windows_skips_command_cron(
        self, tmp_path, app_env, on_windows, caplog
    ):
        import json

        from kiro_crew.apps.bridges import _app_crons_path, register_app_crons_with_service

        path = _app_crons_path("test-app")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([{"name": "test-app/collect", "every": 60, "command": "echo hi"}])
        )
        sdk = MagicMock()
        sdk.list_jobs.return_value = []
        sdk.add_job_if_absent_async = AsyncMock()
        with patch("kiro_crew.apps.bridges.CronSDK", return_value=sdk), caplog.at_level("WARNING"):
            result = asyncio.run(register_app_crons_with_service("test-app", MagicMock()))
        assert result == []
        sdk.add_job_if_absent_async.assert_not_called()
        assert "command rejected" in caplog.text


def test_shell_vet_still_accepts_clean_command_on_windows(on_windows):
    """Import and the fire-time re-vet call this; they must keep the job."""
    from kiro_crew.mcp_cron import _vet_shell_command

    assert _vet_shell_command("echo hi") is None
