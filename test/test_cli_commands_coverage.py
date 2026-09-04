"""Coverage tests for :mod:`kiro_crew.cli_commands` subcommand dispatch.

Each handler in ``cli_commands`` is a plain ``argparse.Namespace`` consumer, so
these tests call the handlers DIRECTLY (never through a subprocess) and assert
on the routed side effects: which collaborator was called, what was printed, and
which exit code was raised. Everything that would touch the network, the real
data home, or a live gateway is mocked -- the module's HTTP paths are exercised
by patching ``kiro_crew.cli_commands.loopback_urlopen``, and every filesystem write lands in
``tmp_path``.

Follows the style already established by ``test_cli.py`` (``argparse.Namespace``
+ ``patch("kiro_crew.cli_commands.<name>")``) and ``test_workspace_crud_cli.py``
(config fixtures written into ``tmp_path``).
"""

from __future__ import annotations

import argparse
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import sel as sel_mod
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
from kiro_crew.cron import CronSchedule
from kiro_crew.eval.scenario import AssertionType
from kiro_crew.security import BUILTIN_DENIED_RULES
from kiro_crew.vector_memory import LessonWriteOutcome, LessonWriteResult

# ── helpers ──


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _http_error(
    code: int, body: bytes | None = None, reason: str = "Boom"
) -> urllib.error.HTTPError:
    """Build an ``HTTPError`` whose ``.read()`` yields *body*."""
    fp = io.BytesIO(body if body is not None else b"")
    return urllib.error.HTTPError("http://localhost/x", code, reason, {}, fp)  # type: ignore[arg-type]


class _FakeResponse:
    """Minimal context-manager stand-in for ``urlopen``'s return value."""

    def __init__(self, payload: Any) -> None:
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _result(ok: bool, *, message: str = "done", error: str = "nope", name: str = "demo") -> Any:
    return SimpleNamespace(ok=ok, message=message, error=error, name=name)


def _registration(**kw: Any) -> Any:
    base: dict[str, list[str]] = {"agents": [], "skills": [], "crons": [], "errors": []}
    base.update(kw)
    return SimpleNamespace(**base)


def _cfg_with(
    *,
    agents: dict[str, KiroCrewAgentConfig] | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    default_agent: str = "default",
    default_workspace: str = "default",
) -> KiroCrewConfig:
    """An in-memory config whose ``save()`` is a no-op recorder."""
    cfg = KiroCrewConfig()
    cfg.agents = agents if agents is not None else {}
    cfg.workspaces = workspaces if workspaces is not None else {}
    cfg.default_agent = default_agent
    cfg.default_workspace = default_workspace
    cfg.save = MagicMock()  # type: ignore[method-assign]
    return cfg


# ── _internal_secret / _format_schedule ──


class TestSmallHelpers:
    def test_internal_secret_reads_file(self, tmp_path: Path) -> None:
        (tmp_path / ".local_secret").write_text("  s3cr3t\n", encoding="utf-8")
        with patch("kiro_crew.cli_commands.read_local_secret", return_value="s3cr3t") as read:
            assert cc._internal_secret(6123) == "s3cr3t"
        read.assert_called_once_with(6123)

    def test_internal_secret_missing_file_is_empty(self, tmp_path: Path) -> None:
        """A missing secret must yield "" so the server answers 403, not a crash."""
        with patch("kiro_crew.cli_commands.read_local_secret", return_value=""):
            assert cc._internal_secret(6123) == ""

    def test_format_schedule_non_schedule_falls_back_to_str(self) -> None:
        assert cc._format_schedule("weekly-ish") == "weekly-ish"

    def test_format_schedule_at_job_shows_full_date(self) -> None:
        sched = CronSchedule(kind="at", at_ts=1700000000.0)
        out = cc._format_schedule(sched)
        assert out.startswith("at ") and len(out) == len("at 2023-11-14 14:13")

    def test_format_schedule_delegates_for_every(self) -> None:
        sched = CronSchedule(kind="every", every_secs=300)
        with patch("kiro_crew.cli_commands.format_schedule", return_value="every 5m") as fmt:
            assert cc._format_schedule(sched) == "every 5m"
        fmt.assert_called_once_with(sched)


class TestWorkspaceDirGuard:
    """``_ws_dir_resolves_inside_home`` must fail CLOSED, never raise."""

    def test_relative_name_inside_home_is_accepted(self, tmp_path: Path) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert cc._ws_dir_resolves_inside_home("workspace-demo") is True

    def test_home_root_itself_is_refused(self, tmp_path: Path) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert cc._ws_dir_resolves_inside_home(".") is False

    def test_escaping_path_is_refused(self, tmp_path: Path) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert cc._ws_dir_resolves_inside_home("../elsewhere") is False

    def test_unknown_user_tilde_fails_closed(self, tmp_path: Path) -> None:
        """``expanduser`` raises RuntimeError here -- it must not escape."""
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert cc._ws_dir_resolves_inside_home("~nosuchuser1234/x") is False

    def test_sensitive_target_is_refused(self, tmp_path: Path) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.is_sensitive_path", return_value=True),
        ):
            assert cc._ws_dir_resolves_inside_home("profiles") is False

    def test_error_message_names_boundary_and_value(self, tmp_path: Path) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            msg = cc._ws_dir_error("/etc")
            assert "data home" in msg and "/etc" in msg and "relative directory name" in msg


# ── _spawn / _spawn_run ──


class TestSpawnCli:
    def test_list_prints_agents_with_status_glyphs(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = {
            "agents": [{"id": "a1", "task": "do x", "done": True}, {"id": "a2", "task": "y"}]
        }
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
            patch("kiro_crew.cli_commands.loopback_urlopen", return_value=_FakeResponse(payload)),
        ):
            cc._spawn(_ns(spawn_action="list", port=1234))
        out = capsys.readouterr().out
        assert "a1" in out and "a2" in out and "✅" in out and "⏳" in out

    def test_list_empty_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch(
                "kiro_crew.cli_commands.loopback_urlopen",
                return_value=_FakeResponse({"agents": []}),
            ),
        ):
            cc._spawn(_ns(spawn_action="list", port=1234))
        assert "No subagents." in capsys.readouterr().out

    def test_list_http_error_with_json_body_prints_server_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = _http_error(400, json.dumps({"error": "bad spawn"}).encode())
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch("kiro_crew.cli_commands.loopback_urlopen", side_effect=err),
            pytest.raises(SystemExit) as exc,
        ):
            cc._spawn(_ns(spawn_action="list", port=1234))
        assert exc.value.code == 1
        assert "bad spawn" in capsys.readouterr().out

    def test_list_http_error_with_opaque_body_prints_status(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch(
                "kiro_crew.cli_commands.loopback_urlopen", side_effect=_http_error(503, b"<html>")
            ),
            pytest.raises(SystemExit),
        ):
            cc._spawn(_ns(spawn_action="list", port=1234))
        assert "503" in capsys.readouterr().out

    def test_list_unreachable_gateway_reports_port(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch(
                "kiro_crew.cli_commands.loopback_urlopen",
                side_effect=urllib.error.URLError("refused"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cc._spawn(_ns(spawn_action="list", port=4321))
        assert exc.value.code == 1
        assert "4321" in capsys.readouterr().out

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        cc._spawn(_ns(spawn_action="bogus", port=1))
        assert "Usage: kirocrew spawn" in capsys.readouterr().out

    def test_run_fire_and_forget_prints_id_and_returns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch(
                "kiro_crew.cli_commands.loopback_urlopen",
                return_value=_FakeResponse({"id": "ag1", "task": "t"}),
            ) as uo,
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=True))
        assert "Spawned subagent ag1" in capsys.readouterr().out
        assert uo.call_count == 1

    def test_run_blocking_polls_until_done_then_prints_result(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        responses = [
            _FakeResponse({"id": "ag2", "task": "t"}),
            _FakeResponse({"done": False}),
            _FakeResponse({"done": True, "result": "final answer"}),
        ]
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch.object(cc._time, "sleep") as slept,
            patch("kiro_crew.cli_commands.loopback_urlopen", side_effect=responses),
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=False))
        assert "final answer" in capsys.readouterr().out
        assert slept.call_count == 2

    def test_run_blocking_surfaces_agent_error_as_exit_1(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        responses = [
            _FakeResponse({"id": "ag3", "task": "t"}),
            _FakeResponse({"done": True, "error": "agent blew up"}),
        ]
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch.object(cc._time, "sleep"),
            patch("kiro_crew.cli_commands.loopback_urlopen", side_effect=responses),
            pytest.raises(SystemExit) as exc,
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=False))
        assert exc.value.code == 1
        assert "agent blew up" in capsys.readouterr().err

    def test_run_lost_connection_during_poll_exits_1(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch.object(cc._time, "sleep"),
            patch(
                "kiro_crew.cli_commands.loopback_urlopen",
                side_effect=[_FakeResponse({"id": "ag4", "task": "t"}), OSError("gone")],
            ),
            pytest.raises(SystemExit),
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=False))
        assert "lost connection" in capsys.readouterr().err

    def test_run_create_http_error_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = _http_error(422, json.dumps({"error": "task too long"}).encode())
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch("kiro_crew.cli_commands.loopback_urlopen", side_effect=err),
            pytest.raises(SystemExit),
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=True))
        assert "task too long" in capsys.readouterr().out

    def test_run_unreachable_gateway_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch("kiro_crew.cli_commands.loopback_urlopen", side_effect=OSError("no route")),
            pytest.raises(SystemExit),
        ):
            cc._spawn(_ns(spawn_action="run", port=9, task="t", fire_and_forget=True))
        assert "gateway not running" in capsys.readouterr().out


# ── app subcommands ──


class TestAppCli:
    def test_install_success_reports_registrations(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands.install_app", return_value=_result(True, name="demo")),
            patch(
                "kiro_crew.cli_commands.register_app",
                return_value=_registration(
                    agents=["a"], skills=["s"], crons=["c"], errors=["partial"]
                ),
            ),
        ):
            cc._handle_app(_ns(app_action="install", source="/pkg"))
        out = capsys.readouterr().out
        assert "Agents: a" in out and "Skills: s" in out and "Crons:  c" in out
        assert "partial" in out and "kirocrew app enable demo" in out

    def test_install_failure_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.cli_commands.install_app",
                return_value=_result(False, error="not an app"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="install", source="/pkg"))
        assert exc.value.code == 1
        assert "not an app" in capsys.readouterr().err

    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.list_apps", return_value=[]):
            cc._handle_app(_ns(app_action="list"))
        assert "No apps installed." in capsys.readouterr().out

    def test_list_renders_enabled_and_disabled_rows(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        apps = [
            {"name": "one", "version": "1.0", "enabled": True, "displayName": "One"},
            {"name": "two", "enabled": False},
        ]
        with patch("kiro_crew.cli_commands.list_apps", return_value=apps):
            cc._handle_app(_ns(app_action="list"))
        out = capsys.readouterr().out
        assert "one" in out and "enabled" in out and "two" in out and "disabled" in out

    def test_enable_success_counts_registrations(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("kiro_crew.cli_commands.enable_app", return_value=_result(True, message="on")),
            patch(
                "kiro_crew.cli_commands.register_app",
                return_value=_registration(agents=["a", "b"], skills=["s"]),
            ),
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))
        out = capsys.readouterr().out
        assert "Agents registered: 2" in out and "Skills registered: 1" in out

    def test_enable_failure_exits_1(self) -> None:
        with (
            patch("kiro_crew.cli_commands.enable_app", return_value=_result(False)),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))
        assert exc.value.code == 1

    def test_disable_cleans_crons_then_deregisters(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler") as cleanup,
            patch("kiro_crew.cli_commands.deregister_app") as dereg,
            patch("kiro_crew.cli_commands.disable_app", return_value=_result(True, message="off")),
        ):
            cc._handle_app(_ns(app_action="disable", name="demo"))
        cleanup.assert_called_once_with("demo")
        dereg.assert_called_once_with("demo")
        assert "off" in capsys.readouterr().out

    def test_disable_flips_the_flag_before_deregistering(self) -> None:
        """Order is a security control, not cosmetics (#5726 review).

        A running gateway is a DIFFERENT process: it watches this app's backend and
        re-registers its MCP servers and agents on a health recovery, gated on the
        enabled flag it reads from installed.json. Deregistering first leaves a window
        where that flag still says enabled and the resources are already gone — a
        recovery landing there puts them back for the app being disabled.
        """
        order: list[str] = []
        with (
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch(
                "kiro_crew.cli_commands.deregister_app",
                side_effect=lambda n: order.append("deregister"),
            ),
            patch(
                "kiro_crew.cli_commands.disable_app",
                side_effect=lambda n: order.append("disable") or _result(True, message="off"),
            ),
        ):
            cc._handle_app(_ns(app_action="disable", name="demo"))
        assert order == ["disable", "deregister"], order

    def test_disable_failure_exits_1(self) -> None:
        with (
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.deregister_app"),
            patch("kiro_crew.cli_commands.disable_app", return_value=_result(False)),
            pytest.raises(SystemExit),
        ):
            cc._handle_app(_ns(app_action="disable", name="demo"))

    @pytest.mark.parametrize(
        ("purge", "expect_keep_data"),
        [(False, True), (True, False)],
    )
    def test_uninstall_maps_purge_flag_to_keep_data(
        self, purge: bool, expect_keep_data: bool
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.deregister_app"),
            patch("kiro_crew.cli_commands.uninstall_app", return_value=_result(True)) as uninstall,
        ):
            cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=purge))
        uninstall.assert_called_once_with("demo", keep_data=expect_keep_data)

    def test_uninstall_failure_exits_1(self) -> None:
        with (
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.deregister_app"),
            patch("kiro_crew.cli_commands.uninstall_app", return_value=_result(False)),
            pytest.raises(SystemExit),
        ):
            cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=False))

    def test_dev_on_prints_live_reload_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.apps.dev_mode.set_dev_mode", return_value={"ok": True}):
            cc._handle_app(_ns(app_action="dev", name="demo", off=False))
        out = capsys.readouterr().out
        assert "dev mode" in out and "--off" in out

    def test_dev_off_restores_caching(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.apps.dev_mode.set_dev_mode", return_value={"ok": True}) as setter:
            cc._handle_app(_ns(app_action="dev", name="demo", off=True))
        setter.assert_called_once_with("demo", False)
        assert "dev mode off" in capsys.readouterr().out

    def test_dev_error_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("kiro_crew.apps.dev_mode.set_dev_mode", return_value={"error": "no such app"}),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="dev", name="demo", off=False))
        assert exc.value.code == 1
        assert "no such app" in capsys.readouterr().err

    def test_info_prints_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.get_app", return_value={"name": "demo", "v": 1}):
            cc._handle_app(_ns(app_action="info", name="demo"))
        assert json.loads(capsys.readouterr().out) == {"name": "demo", "v": 1}

    def test_info_missing_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("kiro_crew.cli_commands.get_app", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="info", name="ghost"))
        assert exc.value.code == 1
        assert "not installed" in capsys.readouterr().err

    def test_init_scaffolds_and_prints_next_steps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "my-app"
        with patch("kiro_crew.cli_commands.scaffold_app", return_value=target) as scaffold:
            cc._handle_app(
                _ns(
                    app_action="init",
                    name="my-app",
                    dir=str(tmp_path),
                    backend=True,
                    ui=True,
                    cron=True,
                )
            )
        scaffold.assert_called_once_with(
            tmp_path.resolve(),
            "my-app",
            include_backend=True,
            include_ui=True,
            include_cron=True,
        )
        out = capsys.readouterr().out
        assert "Scaffolded app" in out and "npm install" in out and "app install" in out

    def test_init_without_ui_skips_npm_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("kiro_crew.cli_commands.scaffold_app", return_value=tmp_path / "a"):
            cc._handle_app(
                _ns(app_action="init", name="a", dir=str(tmp_path), backend=False, ui=False)
            )
        assert "npm install" not in capsys.readouterr().out

    def test_mcp_action_delegates_to_server_runner(self) -> None:
        with patch("kiro_crew.cli_commands._run_app_mcp_server") as runner:
            cc._handle_app(_ns(app_action="mcp", name="demo"))
        runner.assert_called_once_with("demo")

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        cc._handle_app(_ns(app_action=None))
        assert "Usage: kirocrew app" in capsys.readouterr().out


class TestRunAppMcpServer:
    def test_missing_module_exits_1_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """stdout is the JSON-RPC channel -- diagnostics must go to stderr."""
        target = "kiro_crew.apps.builtins.my_app.mcp_server"
        with (
            patch(
                "importlib.import_module",
                side_effect=ModuleNotFoundError(f"No module named {target!r}", name=target),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cc._run_app_mcp_server("my-app")
        captured = capsys.readouterr()
        assert exc.value.code == 1
        assert captured.out == ""
        assert "my-app" in captured.err

    def test_missing_parent_package_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A ModuleNotFoundError naming a PARENT package of the target is the
        target being unimportable -- same clean refusal."""
        parent = "kiro_crew.apps.builtins.my_app"
        with (
            patch(
                "importlib.import_module",
                side_effect=ModuleNotFoundError(f"No module named {parent!r}", name=parent),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cc._run_app_mcp_server("my-app")
        assert exc.value.code == 1
        assert "my-app" in capsys.readouterr().err

    def test_missing_dependency_inside_module_propagates(self) -> None:
        """A dependency missing INSIDE mcp_server.py is a real defect: it must
        keep its traceback, not exit with a misleading 'has no MCP server'."""
        with (
            patch(
                "importlib.import_module",
                side_effect=ModuleNotFoundError(
                    "No module named 'some_missing_dep'", name="some_missing_dep"
                ),
            ),
            pytest.raises(ModuleNotFoundError, match="some_missing_dep"),
        ):
            cc._run_app_mcp_server("my-app")

    def test_nameless_import_error_propagates(self) -> None:
        """An ImportError that names no module cannot be attributed to the
        target -- it must propagate."""
        with (
            patch("importlib.import_module", side_effect=ModuleNotFoundError("boom")),
            pytest.raises(ModuleNotFoundError, match="boom"),
        ):
            cc._run_app_mcp_server("my-app")

    def test_module_without_runner_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("importlib.import_module", return_value=SimpleNamespace()),
            pytest.raises(SystemExit),
        ):
            cc._run_app_mcp_server("my-app")
        assert "run_mcp_server" in capsys.readouterr().err

    def test_runner_is_invoked(self) -> None:
        runner = MagicMock()
        with patch(
            "importlib.import_module", return_value=SimpleNamespace(run_mcp_server=runner)
        ) as imp:
            cc._run_app_mcp_server("my-app")
        runner.assert_called_once_with()
        # Hyphens in app names map to underscores in the module path.
        assert imp.call_args[0][0].endswith("my_app.mcp_server")


class TestCleanupAppCrons:
    async def _zero(self, *_a: object, **_kw: object) -> int:
        return 0

    def test_reports_removed_count_and_audits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def _two(*_a: object, **_kw: object) -> int:
            return 2

        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.CronService"),
            patch("kiro_crew.cli_commands.deregister_app_crons_from_service", new=_two),
            patch("kiro_crew.cli_commands.sel") as sel,
        ):
            assert cc._cleanup_app_crons_from_scheduler("demo") == 2
        assert "removed 2 cron job(s)" in capsys.readouterr().out
        assert sel.return_value.log_api_access.call_args.kwargs["outcome"] == "completed"

    def test_zero_removed_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.CronService"),
            patch("kiro_crew.cli_commands.deregister_app_crons_from_service", new=self._zero),
            patch("kiro_crew.cli_commands.sel"),
        ):
            assert cc._cleanup_app_crons_from_scheduler("demo") == 0
        assert capsys.readouterr().out == ""

    def test_failure_is_audited_then_reraised(self, tmp_path: Path) -> None:
        async def _boom(*_a: object, **_kw: object) -> int:
            raise RuntimeError("scheduler down")

        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.CronService"),
            patch("kiro_crew.cli_commands.deregister_app_crons_from_service", new=_boom),
            patch("kiro_crew.cli_commands.sel") as sel,
            pytest.raises(RuntimeError, match="scheduler down"),
        ):
            cc._cleanup_app_crons_from_scheduler("demo")
        assert sel.return_value.log_api_access.call_args.kwargs["outcome"] == "failed"


# ── agent subcommands ──


class TestAgentCli:
    def test_list_marks_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(
            agents={
                "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
                "other": KiroCrewAgentConfig(kiro_agent="alt"),
            }
        )
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            cc._handle_agent(_ns(agent_action="list"))
        out = capsys.readouterr().out
        assert "default *" in out and "other" in out and "alt" in out

    def test_create_persists_new_agent(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(agents={})
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            cc._handle_agent(
                _ns(
                    agent_action="create",
                    name="new",
                    kiro_agent="ka",
                    workspace="ws",
                    memory_store="ms",
                )
            )
        assert cfg.agents["new"].kiro_agent == "ka"
        assert cfg.agents["new"].workspace == "ws"
        cfg.save.assert_called_once()  # type: ignore[attr-defined]
        assert "Created agent: new" in capsys.readouterr().out

    def test_create_duplicate_exits_1_without_saving(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _cfg_with(agents={"dup": KiroCrewAgentConfig()})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_agent(
                _ns(
                    agent_action="create",
                    name="dup",
                    kiro_agent="",
                    workspace="",
                    memory_store="",
                )
            )
        assert exc.value.code == 1
        cfg.save.assert_not_called()  # type: ignore[attr-defined]
        assert "already exists" in capsys.readouterr().err

    def test_update_applies_only_provided_fields(self) -> None:
        cfg = _cfg_with(
            agents={"a": KiroCrewAgentConfig(kiro_agent="old", workspace="ws0", memory_store="m0")}
        )
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            cc._handle_agent(
                _ns(
                    agent_action="update",
                    name="a",
                    kiro_agent="new",
                    workspace=None,
                    memory_store=None,
                )
            )
        assert cfg.agents["a"].kiro_agent == "new"
        assert cfg.agents["a"].workspace == "ws0"
        assert cfg.agents["a"].memory_store == "m0"

    def test_update_all_fields(self) -> None:
        cfg = _cfg_with(agents={"a": KiroCrewAgentConfig()})
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            cc._handle_agent(
                _ns(
                    agent_action="update",
                    name="a",
                    kiro_agent="k",
                    workspace="w",
                    memory_store="m",
                )
            )
        agent = cfg.agents["a"]
        assert (agent.kiro_agent, agent.workspace, agent.memory_store) == ("k", "w", "m")

    def test_update_missing_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(agents={})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_agent(
                _ns(
                    agent_action="update",
                    name="ghost",
                    kiro_agent=None,
                    workspace=None,
                    memory_store=None,
                )
            )
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_delete_removes_non_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(
            agents={"default": KiroCrewAgentConfig(), "spare": KiroCrewAgentConfig()},
        )
        with patch.object(KiroCrewConfig, "load", return_value=cfg):
            cc._handle_agent(_ns(agent_action="delete", name="spare"))
        assert "spare" not in cfg.agents
        assert "Deleted agent: spare" in capsys.readouterr().out

    def test_delete_default_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = _cfg_with(agents={"default": KiroCrewAgentConfig()})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_agent(_ns(agent_action="delete", name="default"))
        assert exc.value.code == 1
        assert "default" in cfg.agents
        assert "cannot delete default agent" in capsys.readouterr().err

    def test_delete_missing_exits_1(self) -> None:
        cfg = _cfg_with(agents={})
        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            pytest.raises(SystemExit),
        ):
            cc._handle_agent(_ns(agent_action="delete", name="ghost"))

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.object(KiroCrewConfig, "load", return_value=_cfg_with()):
            cc._handle_agent(_ns(agent_action=None))
        assert "Usage: kirocrew agent" in capsys.readouterr().out


# ── workspace create --copy-from ──


class TestWorkspaceCopyFrom:
    def _base(self) -> KiroCrewConfig:
        return _cfg_with(
            workspaces={
                "default": WorkspaceConfig(dir="workspace"),
                "src": WorkspaceConfig(dir="workspace-src"),
            }
        )

    def test_copy_from_unknown_source_exits_1(self, tmp_path: Path) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.sel"),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="new", dir=None, copy_from="ghost")
            )
        assert exc.value.code == 1

    def test_copy_from_copies_tree_and_registers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "workspace-src"
        (src / "memory").mkdir(parents=True)
        (src / "memory" / "notes.md").write_text("hi", encoding="utf-8")
        cfg = self._base()
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.cli_commands.sel"),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="copy1", dir=None, copy_from="src")
            )
        assert (tmp_path / "workspace-copy1" / "memory" / "notes.md").read_text() == "hi"
        assert cfg.workspaces["copy1"].dir == "workspace-copy1"
        assert "Created workspace: copy1" in capsys.readouterr().out

    def test_copy_from_skips_sensitive_entries(self, tmp_path: Path) -> None:
        src = tmp_path / "workspace-src"
        src.mkdir()
        (src / "ok.txt").write_text("keep", encoding="utf-8")
        (src / "secret.env").write_text("nope", encoding="utf-8")

        def _sensitive(p: str) -> bool:
            return p.endswith("secret.env")

        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.is_sensitive_path", side_effect=_sensitive),
            patch("kiro_crew.cli_commands.sel"),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="copy2", dir=None, copy_from="src")
            )
        dst = tmp_path / "workspace-copy2"
        assert (dst / "ok.txt").exists()
        assert not (dst / "secret.env").exists()

    def test_copy_from_escaping_dir_is_refused(self, tmp_path: Path) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.sel") as sel,
            pytest.raises(SystemExit),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="bad", dir="/etc", copy_from="src")
            )
        assert sel.return_value.log_api_access.call_args.kwargs["outcome"] == "denied"

    def test_copy_from_dir_collision_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=self._base()),
            patch("kiro_crew.cli_commands.sel"),
            pytest.raises(SystemExit),
        ):
            cc._handle_workspace(
                _ns(
                    workspace_action="create",
                    name="clash",
                    dir="workspace-src",
                    copy_from="src",
                )
            )
        assert "already used by another workspace" in capsys.readouterr().err

    def test_copy_from_missing_source_dir_still_registers(self, tmp_path: Path) -> None:
        """A source workspace with no directory on disk is a config-only copy."""
        cfg = self._base()
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch("kiro_crew.cli_commands.sel"),
        ):
            cc._handle_workspace(
                _ns(workspace_action="create", name="copy3", dir=None, copy_from="src")
            )
        assert "copy3" in cfg.workspaces
        assert not (tmp_path / "workspace-copy3").exists()


# ── security subcommands ──


class TestSecurityCli:
    def test_deny_list_prints_builtins_and_user_patterns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"hooks": {"auto_deny_tools": ["my-custom-pattern"]}}), encoding="utf-8"
        )
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            cc._security(_ns(sec_action="deny-list"))
        out = capsys.readouterr().out
        assert "Built-in deny patterns" in out
        assert "my-custom-pattern" in out

    def test_deny_list_without_config_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            cc._security(_ns(sec_action="deny-list"))
        out = capsys.readouterr().out
        assert "Built-in deny patterns" in out
        assert "User-configured" not in out

    def test_audit_clean_reports_history_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Both stores clean: the history line is printed and the memory verdict is
        deliberately suppressed (the ``elif not findings: pass`` branch), so a fully
        clean audit says nothing about vector memory."""
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.scan_history", return_value=[]),
            patch("kiro_crew.cli_commands.scan_memory", return_value=[]),
        ):
            cc._security(_ns(sec_action="audit"))
        out = capsys.readouterr().out
        assert "No suspicious tool usage" in out
        assert "vector memory" not in out

    def test_audit_reports_history_and_memory_findings(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        history = [{"file": "s.jsonl", "warning": "exfil", "snippet": "curl evil"}]
        memory = [{"type": "semantic", "key": "k", "warning": "odd", "value": "v"}]
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.scan_history", return_value=history),
            patch("kiro_crew.cli_commands.scan_memory", return_value=memory),
        ):
            cc._security(_ns(sec_action="audit"))
        out = capsys.readouterr().out
        assert "1 suspicious entries found" in out and "s.jsonl" in out
        assert "1 suspicious memory entries" in out and "[semantic] k" in out

    def test_audit_history_findings_with_clean_memory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        history = [{"file": "s.jsonl", "warning": "w", "snippet": "x"}]
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.scan_history", return_value=history),
            patch("kiro_crew.cli_commands.scan_memory", return_value=[]),
        ):
            cc._security(_ns(sec_action="audit"))
        assert "No suspicious content in vector memory." in capsys.readouterr().out

    def test_events_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = []
            cc._security(_ns(sec_action="events", limit=5))
        assert "No security events recorded." in capsys.readouterr().out

    def test_events_passes_the_time_window_through(self) -> None:
        """``-n`` alone cannot express "the last two hours" (issue #4843)."""
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = []
            cc._security(
                _ns(sec_action="events", limit=5, since="2026-08-21T00:00:00Z", until="2h")
            )
        kwargs = sel.return_value.recent.call_args.kwargs
        assert kwargs["since"] == datetime(2026, 8, 21, tzinfo=timezone.utc)
        assert kwargs["until"] is not None and kwargs["until"].tzinfo is not None

    def test_events_rejects_an_unreadable_time(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A typo must not read as "no events in that window"."""
        with patch("kiro_crew.cli_commands.sel") as sel:
            with pytest.raises(SystemExit) as exc:
                cc._security(_ns(sec_action="events", limit=5, since="yesterdayish"))
        assert exc.value.code == 2
        assert "cannot read" in capsys.readouterr().out
        sel.return_value.recent.assert_not_called()

    def test_events_rejects_an_inverted_window(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.sel") as sel:
            with pytest.raises(SystemExit) as exc:
                cc._security(
                    _ns(
                        sec_action="events",
                        limit=5,
                        since="2026-08-21T10:00:00Z",
                        until="2026-08-21T09:00:00Z",
                    )
                )
        assert exc.value.code == 2
        assert "--since must be earlier than --until" in capsys.readouterr().out
        sel.return_value.recent.assert_not_called()

    def test_events_names_the_window_when_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = []
            cc._security(_ns(sec_action="events", limit=5, since="2026-08-21T00:00:00Z"))
        out = capsys.readouterr().out
        assert "No security events recorded in [2026-08-21T00:00:00+00:00, now)." in out

    def test_events_renders_error_and_downstream(self, capsys: pytest.CaptureFixture[str]) -> None:
        events = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "event_type": "api_access",
                "operation": "cron.add",
                "outcome": "allowed",
                "source": "cli",
                "caller_identity": "me",
                "error": "boom",
                "downstream_service": "slack",
            }
        ]
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.recent.return_value = events
            cc._security(_ns(sec_action="events", limit=20))
        out = capsys.readouterr().out
        assert "cron.add → allowed" in out and "error: boom" in out and "slack" in out

    @pytest.mark.parametrize(
        ("total", "valid", "verifiable", "expected"),
        [
            (0, 0, True, "No security events to verify."),
            (3, 3, True, "HMAC chain intact"),
            (3, 1, True, "HMAC chain COMPROMISED"),
            (
                5,
                5,
                False,
                "Audit history UNVERIFIABLE: segment directory refused to pin",
            ),
            (0, 0, False, "Audit history UNVERIFIABLE"),
            (
                5,
                3,
                False,
                "the live log shows tampered entries: 3/5 entries valid",
            ),
        ],
    )
    def test_verify_reports_chain_state(
        self,
        total: int,
        valid: int,
        verifiable: bool,
        expected: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        outcome = sel_mod.SelVerification(
            total=total,
            valid=valid,
            history_verifiable=verifiable,
            reason="" if verifiable else "segment directory refused to pin (planted link?)",
        )
        with patch("kiro_crew.cli_commands.sel") as sel:
            sel.return_value.verify_integrity.return_value = outcome
            cc._security(_ns(sec_action="verify"))
        assert expected in capsys.readouterr().out

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        cc._security(_ns(sec_action="nope"))
        assert "Usage: kirocrew security" in capsys.readouterr().out


# ── policy subcommands ──


def _fake_ceiling() -> Any:
    return SimpleNamespace(
        version=2,
        signature_summary=lambda: "unsigned",
        boot=SimpleNamespace(require_sandbox=True, allow_terminal=False, fail_closed=True),
        controls={"capabilities.telemetry": "off"},
    )


class TestParseTimeSelector:
    """``--since``/``--until`` input handling for ``security events``."""

    def test_empty_means_no_bound(self) -> None:
        assert cc.parse_time_selector("") is None
        assert cc.parse_time_selector("   ") is None

    @pytest.mark.parametrize(
        ("text", "secs"),
        [("45s", 45), ("30m", 1800), ("2h", 7200), ("7d", 604800), ("1w", 604800 * 1)],
    )
    def test_relative_age_counts_back_from_now(self, text: str, secs: int) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        assert cc.parse_time_selector(text, now=now) == now - timedelta(seconds=secs)

    def test_relative_age_is_case_insensitive(self) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        assert cc.parse_time_selector("2H", now=now) == cc.parse_time_selector("2h", now=now)

    def test_iso_instant_with_z_suffix(self) -> None:
        # fromisoformat only accepts "Z" from 3.11; the package supports 3.10.
        assert cc.parse_time_selector("2026-08-21T04:00:00Z") == datetime(
            2026, 8, 21, 4, tzinfo=timezone.utc
        )

    def test_bare_date_is_read_as_utc(self) -> None:
        """The audit log is written in UTC.

        Reading a bare date as local time would silently shift the window by the
        host's offset, so a window that looks right returns the wrong records.
        """
        assert cc.parse_time_selector("2026-08-21") == datetime(2026, 8, 21, tzinfo=timezone.utc)

    def test_offset_is_normalized_to_utc(self) -> None:
        assert cc.parse_time_selector("2026-08-21T10:00:00+02:00") == datetime(
            2026, 8, 21, 8, tzinfo=timezone.utc
        )

    def test_unreadable_input_raises_with_the_accepted_forms(self) -> None:
        with pytest.raises(ValueError, match="relative age"):
            cc.parse_time_selector("last tuesday")

    def test_an_absurd_but_well_formed_age_raises_instead_of_overflowing(self) -> None:
        """``timedelta`` overflows before the subtraction.

        Uncaught, that is an OverflowError traceback and exit 1 from a bad flag
        value -- the CLI must still refuse it as input (exit 2) with guidance.
        """
        with pytest.raises(ValueError, match="too far in the past"):
            cc.parse_time_selector("999999999999999999w")


class TestPolicyCli:
    def test_show_without_policy(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action="show"))
        assert "No enterprise security policy is active" in capsys.readouterr().out

    def test_show_prints_provenance_boot_and_scopes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=_fake_ceiling()),
        ):
            cc._policy(_ns(policy_action="show"))
        out = capsys.readouterr().out
        assert "Security policy v2" in out
        assert "provenance: unsigned" in out
        assert "require_sandbox=True" in out
        assert "capabilities.telemetry: off" in out

    def test_show_without_policy_includes_denied_command_summary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression for #3454: an agent's only prior discovery mechanism for
        the built-in denied-command rules was to attempt one and be refused.
        `policy show` must surface them even on a standalone (non-enterprise)
        install, which is the common case the early-return branch serves.

        The totals are DERIVED from the catalog rather than spelled out: the
        printer itself derives them, so a literal here would only duplicate
        the explicit count assertion in ``test_denied_commands_security.py``
        and rot on every rule that gets added (it did -- the docstring said
        139 while the catalog held 140)."""
        by_category: dict[str, int] = {}
        for rule in BUILTIN_DENIED_RULES:
            by_category[rule.category] = by_category.get(rule.category, 0) + 1
        biggest, biggest_n = max(by_category.items(), key=lambda kv: kv[1])
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action="show"))
        out = capsys.readouterr().out
        assert (
            f"commands.denied: {len(BUILTIN_DENIED_RULES)} rules "
            f"in {len(by_category)} categories"
        ) in out
        assert f"{biggest}({biggest_n})" in out
        # Counts only by default -- rule ids are the --ids opt-in.
        assert "aws-destructive-ec2-terminate-instances" not in out

    def test_show_with_policy_also_includes_denied_command_summary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=_fake_ceiling()),
        ):
            cc._policy(_ns(policy_action="show"))
        assert "commands.denied:" in capsys.readouterr().out

    def test_show_ids_lists_rule_ids_per_category(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Named --ids, not --verbose: the top-level parser already defines
        # --verbose/-v as an int `count` (log level); a same-named store_true
        # on this subparser would collide via argparse's parent/subparser
        # default-override gotcha.
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action="show", ids=True))
        out = capsys.readouterr().out
        assert "aws-destructive-ec2-terminate-instances" in out
        assert "reverse-shell-nc" in out

    def test_show_with_no_governed_scopes(self, capsys: pytest.CaptureFixture[str]) -> None:
        ceiling = _fake_ceiling()
        ceiling.controls = {}
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=ceiling),
        ):
            cc._policy(_ns(policy_action="show"))
        assert "(no governed scopes)" in capsys.readouterr().out

    def test_validate_without_policy_or_profiles_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch(
                "kiro_crew.platform.governance_profiles._profiles_dir",
                return_value=tmp_path / "absent",
            ),
        ):
            cc._policy(_ns(policy_action="validate"))
        out = capsys.readouterr().out
        assert "nothing to validate" in out and "(no profiles directory)" in out and "valid" in out

    def test_validate_flags_invalid_profile(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pdir = tmp_path / "profiles"
        pdir.mkdir()
        (pdir / "good.json").write_text("{}", encoding="utf-8")
        (pdir / "bad.json").write_text("{}", encoding="utf-8")

        def _get(stem: str) -> Any:
            return SimpleNamespace(name="_deny_all" if stem == "bad" else stem)

        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=_fake_ceiling()),
            ),
            patch("kiro_crew.platform.governance_profiles._profiles_dir", return_value=pdir),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", side_effect=_get),
        ):
            cc._policy(_ns(policy_action="validate"))
        out = capsys.readouterr().out
        assert "Policy: v2 OK" in out
        assert "bad.json: INVALID→deny-all" in out
        assert "some profiles failed validation" in out

    def test_explain_unknown_scope_lists_catalog(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch(
                "kiro_crew.platform.governance.SCOPE_CATALOG",
                {"capabilities.telemetry": object()},
            ),
        ):
            cc._policy(
                _ns(
                    policy_action="explain",
                    scope="nope.scope",
                    item="x",
                    session_key="s",
                    agent=None,
                    app=None,
                )
            )
        out = capsys.readouterr().out
        assert "Unknown scope" in out and "capabilities.telemetry" in out

    def test_explain_known_scope_prints_verdicts(self, capsys: pytest.CaptureFixture[str]) -> None:
        decision = SimpleNamespace(
            permitted=False, rule="deny", layer="policy", reason="pinned off"
        )
        gate = SimpleNamespace(permitted=True, reason="title allowed")
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=_fake_ceiling()),
            ),
            patch(
                "kiro_crew.platform.governance.SCOPE_CATALOG",
                {"capabilities.telemetry": object()},
            ),
            patch(
                "kiro_crew.platform.governance_profiles.resolve_active_scope",
                return_value=SimpleNamespace(name="prof"),
            ),
            patch("kiro_crew.platform.governance.resolve", return_value=decision),
            patch("kiro_crew.platform.governance.gate_decision", return_value=gate),
        ):
            cc._policy(
                _ns(
                    policy_action="explain",
                    scope="capabilities.telemetry",
                    item="send",
                    session_key="sess-1",
                    agent="a",
                    app=None,
                )
            )
        out = capsys.readouterr().out
        assert "DENIED: capabilities.telemetry" in out
        assert "active profile : prof" in out
        assert "deny / policy" in out
        assert "gate verdict   : ALLOWED" in out

    def test_explain_without_active_profile(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance.SCOPE_CATALOG", {"s": object()}),
            patch(
                "kiro_crew.platform.governance_profiles.resolve_active_scope",
                return_value=None,
            ),
            patch(
                "kiro_crew.platform.governance.resolve",
                return_value=SimpleNamespace(
                    permitted=True, rule="allow", layer=None, reason="default"
                ),
            ),
            patch(
                "kiro_crew.platform.governance.gate_decision",
                return_value=SimpleNamespace(permitted=True, reason="ok"),
            ),
        ):
            cc._policy(
                _ns(
                    policy_action="explain",
                    scope="s",
                    item="i",
                    session_key="k",
                    agent=None,
                    app=None,
                )
            )
        out = capsys.readouterr().out
        assert "ALLOWED: s" in out and "(none — policy only)" in out and "allow / —" in out

    def test_profile_missing(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", return_value=None),
        ):
            cc._policy(_ns(policy_action="profile", name="ghost"))
        assert "No profile named 'ghost'" in capsys.readouterr().out

    def test_profile_prints_bind_and_scopes(self, capsys: pytest.CaptureFixture[str]) -> None:
        prof = SimpleNamespace(
            name="team",
            bind=SimpleNamespace(type="agent", id="kirocrew"),
            extends="base",
            controls={"capabilities.telemetry": "off"},
        )
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", return_value=prof),
        ):
            cc._policy(_ns(policy_action="profile", name="team"))
        out = capsys.readouterr().out
        assert "bind=agent:kirocrew" in out and "extends=base" in out
        assert "capabilities.telemetry: off" in out

    def test_profile_unbound_with_no_controls(self, capsys: pytest.CaptureFixture[str]) -> None:
        prof = SimpleNamespace(name="empty", bind=None, extends=None, controls={})
        with (
            patch(
                "kiro_crew.platform.context.current_context",
                return_value=SimpleNamespace(governance=None),
            ),
            patch("kiro_crew.platform.governance_profiles.get_store_profile", return_value=prof),
        ):
            cc._policy(_ns(policy_action="profile", name="empty"))
        out = capsys.readouterr().out
        assert "(unbound)" in out and "no governed scopes" in out

    def test_unknown_action_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=SimpleNamespace(governance=None),
        ):
            cc._policy(_ns(policy_action=None))
        assert "Usage: kirocrew policy" in capsys.readouterr().out


# ── learn subcommands ──


class _LearnHarness:
    """Patches ``_learn``'s two stores and exposes the mocks."""

    def __init__(self) -> None:
        self.vs = MagicMock()
        self.jsonl = MagicMock()
        self._patches = [
            patch("kiro_crew.cli_commands.VectorMemoryStore", return_value=self.vs),
            patch("kiro_crew.cli_commands.LessonStore", return_value=self.jsonl),
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        ]

    def __enter__(self) -> _LearnHarness:
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestLearnCli:
    def test_add_prefers_vector_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.INSERTED)
            cc._learn(_ns(learn_action="add", rule="do x", category="tool", negative="not y"))
        h.vs.write_lesson.assert_called_once_with("do x", "tool", "not y")
        h.jsonl.save.assert_not_called()
        h.vs.close.assert_called_once()
        assert "Saved: do x (not y) [tool]" in capsys.readouterr().out

    def test_add_does_not_write_jsonl_when_the_store_declines(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """This replaces a test that PINNED the defect (issue #2325).

        It asserted the JSONL fallback fires whenever the vector store returns a
        falsy value -- which is most often "the lesson is already stored exactly as
        submitted". So the old contract was: write a duplicate record for a lesson
        that was fine, and print "Saved:" when nothing needed saving. The declining
        outcomes are now distinguished, and none of them routes to the other store.
        """
        with _LearnHarness() as h:
            h.vs.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.UNCHANGED)
            cc._learn(_ns(learn_action="add", rule="do x", category="knowledge", negative=None))
        h.jsonl.save_or_enrich.assert_not_called()
        h.jsonl.save.assert_not_called()
        out = capsys.readouterr().out
        assert "Already stored, nothing written: do x" in out
        # The store keeps the stored category on a re-submit, so echoing the submitted
        # one would show a value it may not hold.
        assert "[knowledge]" not in out

    def test_add_refusal_exits_non_zero_without_writing_jsonl(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refusal stored nothing anywhere, so the command must fail loudly.

        Routing it to the JSONL store was the sharper half of the defect: that store
        validates no content, so a value the vector store rejected landed there and
        the context builder reads it whenever the vector store holds no lessons.
        """
        with _LearnHarness() as h:
            h.vs.write_lesson.return_value = LessonWriteResult(
                LessonWriteOutcome.REFUSED, "injection_blocked"
            )
            with pytest.raises(SystemExit) as exc:
                cc._learn(_ns(learn_action="add", rule="do x", category="knowledge", negative=None))
        assert exc.value.code == 1
        h.jsonl.save_or_enrich.assert_not_called()
        assert "NOT saved" in capsys.readouterr().err

    def test_list_from_vector_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = [{"value_json": json.dumps({"rule": "r1"})}]
            cc._learn(_ns(learn_action="list"))
        assert "[knowledge]" in capsys.readouterr().out

    def test_list_falls_back_to_jsonl(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.load_all.return_value = [
                SimpleNamespace(category="tool", rule="r", negative="n")
            ]
            cc._learn(_ns(learn_action="list"))
        assert "[tool] r — n" in capsys.readouterr().out

    def test_list_jsonl_fallback_normalizes_blank_category(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The JSONL fallback branch applies the same display policy as the
        vector-store branch and the dashboard: a blank/legacy category renders
        the store's own "knowledge" default, never a bare []."""
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.load_all.return_value = [
                SimpleNamespace(category="", rule="legacy row", negative=None)
            ]
            cc._learn(_ns(learn_action="list"))
        out = capsys.readouterr().out
        assert "[knowledge] legacy row" in out
        assert "[]" not in out

    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.load_all.return_value = []
            cc._learn(_ns(learn_action="list"))
        assert "No lessons." in capsys.readouterr().out

    def test_remove_via_vector_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = [{"value_json": "{}"}]
            h.vs.delete_lesson.return_value = True
            cc._learn(_ns(learn_action="remove", query="q"))
        assert "Removed lessons matching: q" in capsys.readouterr().out

    def test_remove_via_jsonl_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.remove.return_value = True
            cc._learn(_ns(learn_action="remove", query="q"))
        assert "Removed lessons matching: q" in capsys.readouterr().out

    def test_remove_no_match(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _LearnHarness() as h:
            h.vs.get_lessons.return_value = []
            h.jsonl.remove.return_value = False
            cc._learn(_ns(learn_action="remove", query="q"))
        assert "No lessons match: q" in capsys.readouterr().out

    def test_unknown_action_prints_usage_and_closes_store(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _LearnHarness() as h:
            cc._learn(_ns(learn_action=None))
        h.vs.close.assert_called_once()
        assert "Usage: kirocrew learn" in capsys.readouterr().out


# ── memory subcommands ──


class _MemHarness:
    def __init__(self) -> None:
        self.store = MagicMock()
        self._patches = [
            patch("kiro_crew.cli_commands.VectorMemoryStore", return_value=self.store),
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        ]

    def __enter__(self) -> _MemHarness:
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestMemoryCli:
    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = []
            cc._memory_cmd(_ns(mem_action="list"))
        assert "No semantic memory entries." in capsys.readouterr().out

    def test_list_renders_json_and_raw_values(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = [
                {"key": "a", "value_json": '{"x": 1}', "confidence": 0.9, "source": "cli"},
                {"key": "b", "value_json": "not-json", "confidence": 0.1, "source": "cli"},
            ]
            cc._memory_cmd(_ns(mem_action="list"))
        out = capsys.readouterr().out
        assert "a: {'x': 1}" in out and "b: not-json" in out

    def test_search_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.search_episodic.return_value = []
            cc._memory_cmd(_ns(mem_action="search", query="q"))
        assert "No episodic memories found." in capsys.readouterr().out

    def test_search_handles_str_and_list_tags(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.search_episodic.return_value = [
                {"text": "first", "importance": 0.5, "tags": '["t1"]'},
                {"text": "second", "importance": 0.2, "tags": ["t2"]},
                {"text": "third"},
            ]
            cc._memory_cmd(_ns(mem_action="search", query="q"))
        out = capsys.readouterr().out
        assert "tags: t1" in out and "tags: t2" in out and "third" in out

    def test_stats(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.memory_stats.return_value = {
                "semantic_active": 3,
                "semantic_deleted": 1,
                "episodic_active": 7,
                "episodic_deleted": 2,
                "faiss_index_size": 10,
                "events_count": 4,
                "embedded_count": 7,
                "faiss_available": True,
            }
            cc._memory_cmd(_ns(mem_action="stats"))
        out = capsys.readouterr().out
        assert "Semantic: 3 active, 1 deleted" in out
        assert "Embedded: 7/7" in out
        assert "FAISS accelerator: 10 vectors indexed" in out

    def test_stats_without_faiss_reports_fallback_not_zero_vectors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No accelerator must not read as "nothing indexed" when embeddings exist."""
        with _MemHarness() as h:
            h.store.memory_stats.return_value = {
                "semantic_active": 3,
                "semantic_deleted": 1,
                "episodic_active": 228,
                "episodic_deleted": 2,
                "faiss_index_size": 0,
                "events_count": 4,
                "embedded_count": 228,
                "faiss_available": False,
            }
            cc._memory_cmd(_ns(mem_action="stats"))
        out = capsys.readouterr().out
        assert "Embedded: 228/228" in out
        assert "FAISS accelerator: not installed — stdlib cosine fallback (exact)" in out
        assert "0 vectors" not in out

    def test_audit_with_and_without_findings(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness(), patch("kiro_crew.cli_commands.scan_memory", return_value=[]):
            cc._memory_cmd(_ns(mem_action="audit"))
        assert "No suspicious content in memory." in capsys.readouterr().out

        findings = [{"type": "semantic", "key": "k", "warning": "w", "value": "v"}]
        with _MemHarness(), patch("kiro_crew.cli_commands.scan_memory", return_value=findings):
            cc._memory_cmd(_ns(mem_action="audit"))
        assert "1 suspicious entries" in capsys.readouterr().out

    def test_export_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = [{"key": "a"}]
            h.store.get_episodic_list.return_value = []
            h.store.get_events.return_value = []
            cc._memory_cmd(_ns(mem_action="export", output=None))
        assert json.loads(capsys.readouterr().out)["semantic"] == [{"key": "a"}]

    def test_export_to_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out_file = tmp_path / "dump.json"
        with _MemHarness() as h:
            h.store.get_all_semantic.return_value = []
            h.store.get_episodic_list.return_value = []
            h.store.get_events.return_value = []
            cc._memory_cmd(_ns(mem_action="export", output=str(out_file)))
        assert json.loads(out_file.read_text())["episodic"] == []
        assert "Exported to" in capsys.readouterr().out

    def test_migrate_prints_counts(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness() as h:
            h.store.migrate_from_markdown.return_value = {
                "semantic": 1,
                "episodic": 2,
                "skipped": 3,
            }
            cc._memory_cmd(_ns(mem_action="migrate"))
        out = capsys.readouterr().out
        assert "Migration complete" in out and "Skipped:  3" in out

    def test_import_requires_a_file_argument(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness():
            cc._memory_cmd(_ns(mem_action="import", file=None))
        assert "Usage: kirocrew memory import" in capsys.readouterr().out

    def test_import_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        with _MemHarness():
            cc._memory_cmd(_ns(mem_action="import", file=str(tmp_path / "absent.json")))
        assert "File not found" in capsys.readouterr().out

    def test_import_reads_through_safe_read_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "in.json"
        src.write_text(json.dumps({"semantic": []}), encoding="utf-8")
        with _MemHarness() as h:
            h.store.import_memory.return_value = {"semantic": 5, "episodic": 0, "skipped": 1}
            cc._memory_cmd(_ns(mem_action="import", file=str(src)))
        h.store.import_memory.assert_called_once_with({"semantic": []})
        assert "Import complete" in capsys.readouterr().out

    def test_unknown_action_prints_usage_and_closes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _MemHarness() as h:
            cc._memory_cmd(_ns(mem_action="bogus"))
        h.store.close.assert_called_once()
        assert "Usage: kirocrew memory" in capsys.readouterr().out


# ── artifact subcommands ──


class _ArtifactHarness:
    """Patches config/dashboard resolution so ``_artifact`` talks to a fake HTTP layer."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self._patches: list[Any] = []
        self.urlopen: MagicMock = MagicMock()

    def __enter__(self) -> _ArtifactHarness:
        self._patches = [
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", 5476)),
            patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
            patch("kiro_crew.cli_commands.loopback_urlopen", side_effect=self._responses),
        ]
        started = [p.start() for p in self._patches]
        self.urlopen = started[-1]
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestArtifactCli:
    def test_list_renders_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = {
            "artifacts": [
                {"slug": "s1", "version": 3, "kind": "widget", "tags": ["a"], "name": "One"},
                {"slug": "s2", "version": 1, "kind": "markdown", "name": "Two"},
            ]
        }
        with _ArtifactHarness([_FakeResponse(payload)]):
            cc._artifact(_ns(artifact_action="list", tag="ops", kind=None, q="One"))
        out = capsys.readouterr().out
        assert "s1" in out and "[a]" in out and "s2" in out

    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"artifacts": []})]):
            cc._artifact(_ns(artifact_action="list", tag=None, kind=None, q=None))
        assert "No artifacts." in capsys.readouterr().out

    def test_list_server_error_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = _http_error(500, json.dumps({"error": "db down"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit) as exc:
            cc._artifact(_ns(artifact_action="list", tag=None, kind=None, q=None))
        assert exc.value.code == 1
        assert "db down" in capsys.readouterr().err

    def test_list_transport_error_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            _ArtifactHarness([urllib.error.URLError("refused")]),
            pytest.raises(SystemExit),
        ):
            cc._artifact(_ns(artifact_action="list", tag=None, kind=None, q=None))
        assert "Error:" in capsys.readouterr().err

    def test_show_prints_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"content": "<p>hi</p>"})]):
            cc._artifact(_ns(artifact_action="show", slug="s1", version=None, meta=False))
        assert "<p>hi</p>" in capsys.readouterr().out

    def test_show_meta_strips_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"content": "x", "slug": "s1", "version": 2})]):
            cc._artifact(_ns(artifact_action="show", slug="s1", version=2, meta=True))
        data = json.loads(capsys.readouterr().out)
        assert "content" not in data and data["slug"] == "s1"

    def test_show_error_exits_1(self) -> None:
        err = _http_error(404, json.dumps({"error": "no such artifact"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(_ns(artifact_action="show", slug="ghost", version=None, meta=False))

    def test_save_with_inline_content_and_tags(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "new", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="New",
                    content="body",
                    content_file=None,
                    tags="a, b ,",
                    kind="widget",
                    description="d",
                )
            )
        assert "Saved: slug=new version=1" in capsys.readouterr().out

    def test_save_warns_when_the_slug_was_suffixed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `save` has no --slug, so a colliding name silently lands a NEW
        # artifact at a suffixed slug while the canonical one keeps its old
        # content. The warning has to name both the taken slug and the verb
        # that versions in place.
        with _ArtifactHarness(
            [
                _FakeResponse(
                    {
                        "slug": "run-summary-2",
                        "version": 1,
                        "slug_collided_with": "run-summary",
                    }
                )
            ]
        ):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Run Summary",
                    content="corrected",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=run-summary-2 version=1" in captured.out
        assert "run-summary" in captured.err
        assert "kirocrew artifact update run-summary" in captured.err

    def test_save_is_silent_when_the_slug_was_free(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "fresh", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Fresh",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=fresh version=1" in captured.out
        assert captured.err == ""

    def test_save_forwards_an_explicit_slug(self) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "chosen", "version": 1})]) as h:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Chosen",
                    slug="chosen-by-hand",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        body = json.loads(h.urlopen.call_args[0][0].data.decode())
        assert body["slug"] == "chosen-by-hand"

    def test_save_forwards_an_empty_explicit_slug(self) -> None:
        # "" is a slug the caller named, not a request to derive one. Filtering it
        # out would silently route an explicit save into the derive-and-suffix
        # branch — the exact asymmetry this flag exists to close.
        with _ArtifactHarness([_FakeResponse({"slug": "x", "version": 1})]) as h:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Empty Slug",
                    slug="",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        body = json.loads(h.urlopen.call_args[0][0].data.decode())
        assert "slug" in body
        assert body["slug"] == ""

    def test_save_with_an_empty_slug_surfaces_the_refusal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Forwarding "" is only worth anything if the refusal reaches the caller.
        # The store rejects it via _validate_slug and the handler answers 400, so
        # it arrives on the CLI's existing error path — no new error surface.
        err = _http_error(
            400,
            json.dumps({"error": "invalid slug '': must match ^[a-z0-9]..."}).encode(),
        )
        with _ArtifactHarness([err]), pytest.raises(SystemExit) as exc:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Empty Slug",
                    slug="",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "invalid slug" in captured.err
        assert captured.out == ""

    def test_save_omits_the_slug_key_when_none_was_given(self) -> None:
        # An ABSENT flag must send no key at all. Forwarding it as "" would hand
        # the store a slug it refuses, so every plain `save` would start failing;
        # only an explicitly-passed value may reach the request body.
        with _ArtifactHarness([_FakeResponse({"slug": "derived", "version": 1})]) as h:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Derived",
                    slug=None,
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        body = json.loads(h.urlopen.call_args[0][0].data.decode())
        # Positive control: this is the save request body, so the absence below
        # is a fact about the field and not about a request that never went out.
        assert body["name"] == "Derived"
        assert "slug" not in body

    def test_save_with_a_taken_slug_reports_the_conflict(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The store refuses an explicit slug rather than suffixing it, so the
        # handler answers 409. It has to reach the caller as the named condition
        # and a non-zero exit, never a traceback.
        err = _http_error(409, json.dumps({"error": "artifact already exists: taken"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit) as exc:
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Taken",
                    slug="taken",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "artifact already exists: taken" in captured.err
        assert captured.out == ""

    def test_save_with_an_explicit_slug_emits_no_suffix_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The warning advises `artifact update`, which is wrong guidance for a
        # caller who named the slug: they get a 409, never a rename. The CLI must
        # therefore key it on the server's field alone and not on the flag being
        # set. The server half — an explicit slug reporting no collision — is
        # pinned in test_artifacts_handlers.py.
        with _ArtifactHarness([_FakeResponse({"slug": "chosen-by-hand", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Run Summary",
                    slug="chosen-by-hand",
                    content="body",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=chosen-by-hand version=1" in captured.out
        assert captured.err == ""

    def test_save_relays_the_theme_contrast_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gateway computes the verdict (same relay pattern as
        # slug_collided_with); the CLI must surface it — this direct-POST
        # path is the one the motivating incident traveled, which the
        # MCP-layer hint alone never covered.
        with _ArtifactHarness(
            [_FakeResponse({"slug": "perf", "version": 1, "theme_contrast_warning": True})]
        ):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="Perf",
                    content='<div style="color:#111">x</div>',
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        captured = capsys.readouterr()
        assert "Saved: slug=perf version=1" in captured.out
        assert "theme variables" in captured.err

    def test_update_relays_the_theme_contrast_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _ArtifactHarness(
            [_FakeResponse({"slug": "perf", "version": 2, "theme_contrast_warning": True})]
        ):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="perf",
                    content='<body style="background:#fffbe6">x</body>',
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )
        captured = capsys.readouterr()
        assert "Updated: slug=perf version=2" in captured.out
        assert "theme variables" in captured.err

    def test_save_reads_content_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "body.html"
        f.write_text("from-file", encoding="utf-8")
        with _ArtifactHarness([_FakeResponse({"slug": "s", "version": 1})]):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="N",
                    content=None,
                    content_file=str(f),
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert "Saved:" in capsys.readouterr().out

    def test_save_refuses_sensitive_content_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "creds"
        f.write_text("secret", encoding="utf-8")
        with (
            _ArtifactHarness([]),
            patch("kiro_crew.cli_commands.is_sensitive_path", return_value=True),
            pytest.raises(SystemExit) as exc,
        ):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="N",
                    content=None,
                    content_file=str(f),
                    tags=None,
                    kind=None,
                    description=None,
                )
            )
        assert exc.value.code == 1
        assert "sensitive path" in capsys.readouterr().err

    def test_save_error_exits_1(self) -> None:
        err = _http_error(400, json.dumps({"error": "bad name"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(
                _ns(
                    artifact_action="save",
                    name="N",
                    content="c",
                    content_file=None,
                    tags=None,
                    kind=None,
                    description=None,
                )
            )

    def test_update_metadata_only_does_not_touch_content(self) -> None:
        with (
            _ArtifactHarness([_FakeResponse({"slug": "s", "version": 4})]) as h,
            patch("sys.stdin.isatty", return_value=True),
        ):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content=None,
                    content_file=None,
                    name="Renamed",
                    description="d",
                    tags="x",
                )
            )
        req = h.urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert "content" not in body
        assert body == {"name": "Renamed", "description": "d", "tags": ["x"]}

    def test_update_with_content_sends_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"slug": "s", "version": 5})]):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content="new body",
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )
        assert "Updated: slug=s version=5" in capsys.readouterr().out

    def test_update_with_nothing_to_change_exits_1(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            _ArtifactHarness([]),
            patch("sys.stdin.isatty", return_value=True),
            pytest.raises(SystemExit) as exc,
        ):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content=None,
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )
        assert exc.value.code == 1
        assert "provide content/--name" in capsys.readouterr().err

    def test_update_error_exits_1(self) -> None:
        err = _http_error(409, json.dumps({"error": "conflict"}).encode())
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(
                _ns(
                    artifact_action="update",
                    slug="s",
                    content="c",
                    content_file=None,
                    name=None,
                    description=None,
                    tags=None,
                )
            )

    def test_delete_reports_slug(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse(b"")]):
            cc._artifact(_ns(artifact_action="delete", slug="gone"))
        assert "Deleted: gone" in capsys.readouterr().out

    def test_delete_error_exits_1(self) -> None:
        err = _http_error(404, b"not json")
        with _ArtifactHarness([err]), pytest.raises(SystemExit):
            cc._artifact(_ns(artifact_action="delete", slug="gone"))

    def test_versions_lists_numbers(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"versions": [1, 2, 3]})]):
            cc._artifact(_ns(artifact_action="versions", slug="s"))
        assert "v1, v2, v3" in capsys.readouterr().out

    def test_versions_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([_FakeResponse({"versions": []})]):
            cc._artifact(_ns(artifact_action="versions", slug="s"))
        assert "No versions for s." in capsys.readouterr().out

    def test_versions_error_exits_1(self) -> None:
        with _ArtifactHarness([_http_error(500, b"")]), pytest.raises(SystemExit):
            cc._artifact(_ns(artifact_action="versions", slug="s"))

    def test_unknown_action_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _ArtifactHarness([]), pytest.raises(SystemExit) as exc:
            cc._artifact(_ns(artifact_action="bogus"))
        assert exc.value.code == 2
        assert "Usage: kirocrew artifact" in capsys.readouterr().err


class TestPodDispatch:
    def test_pod_delegates_to_verb_layer(self) -> None:
        args = _ns(pod_action="ls")
        with patch("kiro_crew.pod.cli.dispatch") as dispatch:
            cc._pod(args)
        dispatch.assert_called_once_with(args)


# ── telemetry subcommand ──


class TestTelemetryCli:
    def test_status_is_read_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.format_status.return_value = "beacon: OFF"
            cc._telemetry(_ns(telemetry_action="status"))
        assert "beacon: OFF" in capsys.readouterr().out

    def test_missing_action_defaults_to_status(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.format_status.return_value = "default-status"
            cc._telemetry(_ns(telemetry_action=None))
        assert "default-status" in capsys.readouterr().out

    def test_unknown_action_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            pytest.raises(SystemExit) as exc,
        ):
            cc._telemetry(_ns(telemetry_action="frobnicate"))
        assert exc.value.code == 1
        assert "Unknown telemetry action" in capsys.readouterr().err

    def test_disable_writes_config_and_acks_privacy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"agent": {"model": "auto"}}), encoding="utf-8")
        effective = KiroCrewConfig()
        effective.telemetry.beacon_enabled = False
        with (
            patch.object(KiroCrewConfig, "load", return_value=effective),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.DISABLE_ENV = "KIROCREW_NO_BEACON"
            beacon.INSTALL_ID_FILE = "install_id"
            cc._telemetry(_ns(telemetry_action="disable"))
        data = json.loads(path.read_text())
        assert data["telemetry"]["beacon_enabled"] is False
        assert data["dashboard"]["privacy_acked"] is True
        # Unrelated sections survive the rewrite.
        assert data["agent"] == {"model": "auto"}
        assert "DISABLED" in capsys.readouterr().out

    def test_enable_creates_config_when_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "config.json"
        effective = KiroCrewConfig()
        effective.telemetry.beacon_enabled = True
        with (
            patch.object(KiroCrewConfig, "load", return_value=effective),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.is_governance_pinned_off.return_value = False
            cc._telemetry(_ns(telemetry_action="enable"))
        assert json.loads(path.read_text())["telemetry"]["beacon_enabled"] is True

    def test_enable_refused_when_governance_pins_off(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
            pytest.raises(SystemExit) as exc,
        ):
            beacon.is_governance_pinned_off.return_value = True
            cc._telemetry(_ns(telemetry_action="enable"))
        assert exc.value.code == 1
        assert not path.exists(), "must not write a setting that would have no effect"
        assert "pinned OFF" in capsys.readouterr().err

    def test_unreadable_config_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("{not json", encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            pytest.raises(SystemExit) as exc,
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert exc.value.code == 1
        assert "Could not read" in capsys.readouterr().err

    def test_non_object_config_is_refused_not_overwritten(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            pytest.raises(SystemExit),
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert path.read_text() == "[1, 2, 3]", "a toggle must never be a data-loss path"
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_non_object_section_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        original = json.dumps({"telemetry": "yes-please"})
        path.write_text(original, encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            pytest.raises(SystemExit),
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert path.read_text() == original
        assert 'non-object "telemetry" value' in capsys.readouterr().err

    def test_overlay_shadowing_the_write_is_reported_as_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A false promise on a privacy control is worse than an error."""
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        still_on = KiroCrewConfig()
        still_on.telemetry.beacon_enabled = True
        with (
            patch.object(KiroCrewConfig, "load", return_value=still_on),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
            pytest.raises(SystemExit) as exc,
        ):
            beacon.DISABLE_ENV = "KIROCREW_NO_BEACON"
            cc._telemetry(_ns(telemetry_action="disable"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "still ENABLED" in err and "config.local.json" in err

    def test_write_failure_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon"),
            patch("kiro_crew.config.loader.atomic_write", side_effect=OSError("disk full")),
            pytest.raises(SystemExit) as exc,
        ):
            cc._telemetry(_ns(telemetry_action="disable"))
        assert exc.value.code == 1
        assert "Could not write" in capsys.readouterr().err

    def test_effective_check_failure_does_not_mask_the_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A broken diagnostic read must not turn a successful write into an error."""
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        with (
            patch.object(
                KiroCrewConfig,
                "load",
                side_effect=[KiroCrewConfig(), RuntimeError("bad config")],
            ),
            patch("kiro_crew.cli_commands.config_path", return_value=path),
            patch("kiro_crew.cli_commands.beacon") as beacon,
        ):
            beacon.INSTALL_ID_FILE = "install_id"
            cc._telemetry(_ns(telemetry_action="disable"))
        assert "DISABLED" in capsys.readouterr().out


# ── eval runner (`kirocrew eval`) ──


def _scenario(name: str = "smoke", *, turns: int = 2, judge_criteria: str = "") -> Any:
    sessions = [SimpleNamespace(turns=[object()] * turns)]
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        judge_criteria=judge_criteria,
        sessions=sessions,
    )


def _scenario_result(passed: bool = True, *, turn: Any | None = None) -> Any:
    turns = [turn] if turn is not None else []
    return SimpleNamespace(
        passed=passed,
        sessions=[SimpleNamespace(turns=turns)],
        summary=lambda: {"passed": passed},
    )


class _EvalHarness:
    """Stubs every collaborator ``_run_eval`` touches, so no provider is built,
    no model is called, and results land under the (chdir'd) tmp dir."""

    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.runner = MagicMock()
        self.runner.run_scenarios = AsyncMock(return_value=results)
        self.judge = MagicMock()
        self.judge.pass_threshold = 3
        self.judge.start = AsyncMock()
        self.judge.shutdown = AsyncMock()
        self.judge.judge_turn = AsyncMock(
            return_value=SimpleNamespace(score=5, reason="looks right")
        )
        self._patches: list[Any] = []

    def __enter__(self) -> _EvalHarness:
        self._patches = [
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.build_provider_factory", return_value=MagicMock()),
            patch("kiro_crew.cli_commands.EvalRunner", return_value=self.runner),
            patch("kiro_crew.cli_commands.LLMJudge", return_value=self.judge),
            patch("kiro_crew.cli_commands.format_results", return_value="## Report"),
            patch("kiro_crew.cli_commands.score_by_dimension", return_value={}),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestRunEval:
    """``_run_eval`` writes its report into ``Path.cwd()``, so every test chdirs
    into ``tmp_path`` first -- nothing is written outside the temp dir."""

    @pytest.mark.asyncio
    async def test_default_runs_smoke_test_and_writes_both_reports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with (
            _EvalHarness([_scenario_result(True)]),
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()) as loader,
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=False))
        assert loader.call_args[0][0].name == "smoke_test.json"
        results_dir = tmp_path / "eval_results"
        assert len(list(results_dir.iterdir())) == 2
        report = next(p for p in results_dir.iterdir() if p.suffix == ".md")
        assert report.read_text() == "## Report\n"
        dump = next(p for p in results_dir.iterdir() if p.suffix == ".json")
        payload = json.loads(dump.read_text())
        assert payload["overall_passed"] == 1 and payload["overall_total"] == 1
        out = capsys.readouterr().out
        assert "Running: smoke (2 turns)" in out and "Overall: 1/1 scenarios passed" in out

    @pytest.mark.asyncio
    async def test_all_scenarios_loads_the_whole_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        scenarios = [_scenario("a"), _scenario("b")]
        with (
            _EvalHarness([_scenario_result(True), _scenario_result(False)]) as h,
            patch("kiro_crew.cli_commands.load_scenarios", return_value=scenarios) as loader,
        ):
            await cc._run_eval(_ns(all_scenarios=True, scenarios=None, judge=False))
        assert loader.call_args[0][0].name == "scenarios"
        h.runner.run_scenarios.assert_awaited_once_with(scenarios)

    @pytest.mark.asyncio
    async def test_named_scenario_is_resolved_by_extension(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with (
            _EvalHarness([_scenario_result(True)]),
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario("named")) as ld,
        ):
            await cc._run_eval(
                _ns(all_scenarios=False, scenarios=["memory_recall_basic"], judge=False)
            )
        assert ld.call_args[0][0].name == "memory_recall_basic.json"

    @pytest.mark.asyncio
    async def test_unknown_scenario_lists_available_and_returns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with _EvalHarness([]) as h:
            await cc._run_eval(_ns(all_scenarios=False, scenarios=["does-not-exist"], judge=False))
        out = capsys.readouterr().out
        assert "scenario 'does-not-exist' not found" in out
        assert "smoke_test" in out
        h.runner.run_scenarios.assert_not_awaited()
        assert not (tmp_path / "eval_results").exists()

    @pytest.mark.asyncio
    async def test_dimension_summary_marks_pass_and_fail_rates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        dims = {
            "memory": {"rate": 0.8, "passed": 4, "total": 5},
            "tools": {"rate": 0.5, "passed": 1, "total": 2},
        }
        with (
            _EvalHarness([_scenario_result(True)]),
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()),
            patch("kiro_crew.cli_commands.score_by_dimension", return_value=dims),
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=False))
        out = capsys.readouterr().out
        assert "✅ memory: 4/5 (80%)" in out
        assert "❌ tools: 1/2 (50%)" in out

    @pytest.mark.asyncio
    async def test_judge_scores_judge_assertions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assertion = SimpleNamespace(type=AssertionType.JUDGE, value="was it helpful")
        turn = SimpleNamespace(
            assertion_results=[(assertion, False)],
            user_message="hi",
            agent_response="hello",
        )
        with (
            _EvalHarness([_scenario_result(True, turn=turn)]) as h,
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()),
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=True))
        h.judge.start.assert_awaited_once()
        h.judge.shutdown.assert_awaited_once()
        # score 5 >= pass_threshold 3, so the assertion flips to passing.
        assert turn.assertion_results[0][1] is True
        assert "Judge: 5/5" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_judge_failure_marks_assertion_failed_without_aborting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assertion = SimpleNamespace(type=AssertionType.JUDGE, value=None)
        turn = SimpleNamespace(
            assertion_results=[(assertion, True)],
            user_message="hi",
            agent_response="hello",
        )
        with (
            _EvalHarness([_scenario_result(True, turn=turn)]) as h,
            patch(
                "kiro_crew.cli_commands.load_scenario",
                return_value=_scenario(judge_criteria="be terse"),
            ),
        ):
            h.judge.judge_turn.side_effect = RuntimeError("judge offline")
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=True))
        assert turn.assertion_results[0][1] is False
        assert "Judge failed for turn: judge offline" in capsys.readouterr().out
        h.judge.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_judge_assertions_are_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assertion = SimpleNamespace(type=AssertionType.CONTAINS, value="x")
        turn = SimpleNamespace(
            assertion_results=[(assertion, False)],
            user_message="hi",
            agent_response="hello",
        )
        with (
            _EvalHarness([_scenario_result(True, turn=turn)]) as h,
            patch("kiro_crew.cli_commands.load_scenario", return_value=_scenario()),
        ):
            await cc._run_eval(_ns(all_scenarios=False, scenarios=None, judge=True))
        h.judge.judge_turn.assert_not_awaited()
        assert turn.assertion_results[0][1] is False
