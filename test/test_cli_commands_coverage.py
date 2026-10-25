"""Coverage tests for :mod:`kiro_crew.cli_commands` subcommand dispatch.

Each handler in ``cli_commands`` is a plain ``argparse.Namespace`` consumer, so
these tests call the handlers DIRECTLY (never through a subprocess) and assert
on the routed side effects: which collaborator was called, what was printed, and
which exit code was raised. Everything that would touch the network, the real
data home, or a live gateway is mocked -- the module's HTTP paths are exercised
by patching ``kiro_crew.cli_commands._gateway_send``, and every filesystem write lands in
``tmp_path``.

Follows the style already established by ``test_cli.py`` (``argparse.Namespace``
+ ``patch("kiro_crew.cli_commands.<name>")``) and ``test_workspace_crud_cli.py``
(config fixtures written into ``tmp_path``).

The handlers are split by topic across sibling files: agent, workspace, security
and policy in ``test_cli_commands_coverage_agent_security.py``; learn, memory,
artifact, pod, telemetry and eval in ``test_cli_commands_coverage_memory_eval.py``.
The fakes more than one of them uses live in ``cli_commands_coverage_helpers.py``.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from cli_commands_coverage_helpers import _FakeResponse, _http_error, _ns

from kiro_crew import cli_commands as cc
from kiro_crew.cron import CronSchedule


class _IncompleteResponse(_FakeResponse):
    """Response whose body terminates before the declared payload completes."""

    def read(self) -> bytes:
        raise http.client.IncompleteRead(self._raw)


def _result(ok: bool, *, message: str = "done", error: str = "nope", name: str = "demo") -> Any:
    return SimpleNamespace(ok=ok, message=message, error=error, name=name)


def _registration(**kw: Any) -> Any:
    base: dict[str, list[str]] = {"agents": [], "skills": [], "crons": [], "errors": []}
    base.update(kw)
    return SimpleNamespace(**base)


# ── _internal_secret / _format_schedule ──


class TestSmallHelpers:
    def test_internal_secret_reads_file(self, tmp_path: Path) -> None:
        (tmp_path / ".local_secret").write_text("  s3cr3t\n", encoding="utf-8")
        with patch("kiro_crew.cli_commands.read_local_secret", return_value="s3cr3t") as read:
            assert cc._internal_secret(6123) == "s3cr3t"
        read.assert_called_once_with(6123, dial_host="127.0.0.1")

    def test_internal_secret_missing_file_is_empty(self, tmp_path: Path) -> None:
        """A missing secret must yield "" so the server answers 403, not a crash."""
        with patch("kiro_crew.cli_commands.read_local_secret", return_value=""):
            assert cc._internal_secret(6123) == ""

    def test_format_schedule_non_schedule_falls_back_to_str(self) -> None:
        assert cc._format_schedule("weekly-ish") == "weekly-ish"

    def test_format_schedule_at_job_shows_full_date(self) -> None:
        # A one-shot renders in the CONFIGURED timezone (the zone the instant
        # was resolved in), with its zone label, not the host's local time.
        sched = CronSchedule(kind="at", at_ts=1700000000.0)
        with patch(
            "kiro_crew.cli_commands.get_local_tz",
            return_value=("UTC", ZoneInfo("UTC")),
        ):
            out = cc._format_schedule(sched)
        assert out == "at 2023-11-14 22:13 UTC"

    def test_format_schedule_delegates_for_every(self) -> None:
        sched = CronSchedule(kind="every", every_secs=300)
        with patch("kiro_crew.cli_commands.format_schedule", return_value="every 5m") as fmt:
            assert cc._format_schedule(sched) == "every 5m"
        fmt.assert_called_once_with(sched)


class TestWorkspaceDirGuard:
    """``_ws_dir_resolves_inside_home`` must fail CLOSED, never raise.

    An accepted dir comes back as the ONE resolved path the guard judged (the
    caller materializes that object, never a second resolution); a refusal is None.
    """

    def test_relative_name_inside_home_is_accepted(self, tmp_path: Path) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert (
                cc._ws_dir_resolves_inside_home("workspace-demo")
                == (tmp_path / "workspace-demo").resolve()
            )

    def test_home_root_itself_is_refused(self, tmp_path: Path) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert cc._ws_dir_resolves_inside_home(".") is None

    def test_escaping_path_is_refused(self, tmp_path: Path) -> None:
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert cc._ws_dir_resolves_inside_home("../elsewhere") is None

    def test_unknown_user_tilde_fails_closed(self, tmp_path: Path) -> None:
        """``expanduser`` raises RuntimeError here -- it must not escape."""
        with patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path):
            assert cc._ws_dir_resolves_inside_home("~nosuchuser1234/x") is None

    def test_sensitive_target_is_refused(self, tmp_path: Path) -> None:
        with (
            patch("kiro_crew.cli_commands.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_commands.is_sensitive_path", return_value=True),
        ):
            assert cc._ws_dir_resolves_inside_home("profiles") is None

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
            patch("kiro_crew.cli_commands._gateway_send", return_value=_FakeResponse(payload)),
        ):
            cc._spawn(_ns(spawn_action="list", port=1234))
        out = capsys.readouterr().out
        assert "a1" in out and "a2" in out and "✅" in out and "⏳" in out

    def test_list_empty_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch(
                "kiro_crew.cli_commands._gateway_send",
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
            patch("kiro_crew.cli_commands._gateway_send", side_effect=err),
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
            patch("kiro_crew.cli_commands._gateway_send", side_effect=_http_error(503, b"<html>")),
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
                "kiro_crew.cli_commands._gateway_send",
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
                "kiro_crew.cli_commands._gateway_send",
                return_value=_FakeResponse({"id": "ag1", "task": "t"}),
            ) as uo,
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=True))
        assert "Spawned subagent ag1" in capsys.readouterr().out
        assert uo.call_count == 1

    def test_run_fire_and_forget_says_queued_when_the_gate_deferred_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A deferred row is accepted under its id but not running; the CLI
        relays the gateway's ``queued`` answer and reason instead of a start."""
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch(
                "kiro_crew.cli_commands._gateway_send",
                return_value=_FakeResponse(
                    {
                        "id": "ag1",
                        "task": "t",
                        "status": "queued",
                        "reason": "low_memory",
                        "reason_detail": "low memory: 3.2 GB available, need 4 GB",
                    }
                ),
            ),
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=True))
        out = capsys.readouterr().out
        assert "Queued subagent ag1" in out
        assert "low memory: 3.2 GB available, need 4 GB" in out
        assert "Spawned" not in out

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
            patch("kiro_crew.cli_commands._gateway_send", side_effect=responses),
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
            patch("kiro_crew.cli_commands._gateway_send", side_effect=responses),
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
                "kiro_crew.cli_commands._gateway_send",
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
            patch("kiro_crew.cli_commands._gateway_send", side_effect=err),
            pytest.raises(SystemExit),
        ):
            cc._spawn(_ns(spawn_action="run", port=1, task="t", fire_and_forget=True))
        assert "task too long" in capsys.readouterr().out

    def test_run_unreachable_gateway_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("kiro_crew.cli_commands._internal_secret", return_value=""),
            patch("kiro_crew.cli_commands._gateway_send", side_effect=OSError("no route")),
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
        assert "✅ Recorded demo as enabled." in out
        assert "No running gateway was reached" in out
        assert "Agents registered: 2" in out and "Skills registered: 1" in out

    def test_owner_socket_forwards_enable_without_local_edits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        requests: list[urllib.request.Request] = []

        def _open(
            request: urllib.request.Request, *, timeout: int, socket_path: Path, verify_peer: object
        ) -> _FakeResponse:
            assert socket_path.name == "dashboard-8123.sock"
            requests.append(request)
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                assert request.get_header("X-local-secret") == "port-scoped-secret"
                return _FakeResponse({"token": "dashboard-credential"})
            return _FakeResponse(
                {
                    "ok": True,
                    "message": "enabled demo",
                    "registration": {
                        "agents": ["a"],
                        "skills": ["s", "t"],
                        "errors": ["agent registration deferred"],
                    },
                    "warnings": ["backend health check pending"],
                    "backend": {"port": 9100, "healthy": False},
                }
            )

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch(
                "kiro_crew.app_lifecycle_client.read_local_secret",
                return_value="port-scoped-secret",
            ) as credential,
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
            patch("kiro_crew.cli_commands.enable_app") as local_enable,
            patch("kiro_crew.cli_commands.register_app") as local_register,
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))

        assert len(requests) == 2
        assert requests[1].get_method() == "POST"
        assert "/api/apps/demo/enable?" in requests[1].full_url
        local_enable.assert_not_called()
        local_register.assert_not_called()
        credential.assert_called_once_with(8123)
        captured = capsys.readouterr()
        out = captured.out
        assert "✅ enabled demo" in out
        assert "No running gateway was reached" not in out
        assert "Agents registered: 1" in out
        assert "Skills registered: 2" in out
        assert "Backend: port 9100 (starting)" in out
        assert "⚠️  backend health check pending" in captured.err
        assert "⚠️  agent registration deferred" in captured.err

    def test_gateway_output_sanitizes_terminal_control_sequences(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import app_lifecycle_client

        app_lifecycle_client.print_result(
            "enable",
            "demo",
            {
                "message": "enabled \x1b]0;evil\x07demo\x1b[2J!\x07",
                "warnings": ["warning \x1b]0;evil\x07still\x1b[2J readable\x07"],
                "registration": {
                    "agents": [],
                    "skills": [],
                    "errors": ["registration \x1b]0;evil\x07safe\x1b[2J\x07"],
                },
            },
        )

        captured = capsys.readouterr()
        assert captured.out.startswith("✅ enabled demo!")
        assert "⚠️  warning still readable" in captured.err
        assert "⚠️  registration safe" in captured.err
        assert "\x1b" not in captured.out + captured.err
        assert "\x07" not in captured.out + captured.err
        assert "evil" not in captured.out + captured.err

    def test_gateway_output_caps_each_gateway_string(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import app_lifecycle_client

        app_lifecycle_client.print_result("disable", "demo", {"message": "x" * 3000})

        line = capsys.readouterr().out.removeprefix("✅ ").rstrip("\n")
        assert len(line) == 2000
        assert line.endswith("…")

    def test_gateway_output_cannot_forge_an_unprefixed_result_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import app_lifecycle_client

        app_lifecycle_client.print_result(
            "enable",
            "demo",
            {
                "message": "enabled demo\n✅ also enabled evil",
                "warnings": ["hook failed:\r\n✅ enabled evil\n   Agents registered: 9"],
                "registration": {"agents": [], "skills": [], "errors": ["bad\nline"]},
            },
        )

        captured = capsys.readouterr()
        out_lines = captured.out.rstrip("\n").split("\n")
        err_lines = captured.err.rstrip("\n").split("\n")
        assert out_lines[0] == "✅ enabled demo\\x0a✅ also enabled evil"
        assert all(line.startswith(("✅ ", "⚠️  ", "   ")) for line in out_lines + err_lines)
        assert err_lines == [
            "⚠️  hook failed:\\x0a✅ enabled evil\\x0a   Agents registered: 9",
            "⚠️  bad\\x0aline",
        ]
        assert out_lines[1:] == ["   Agents registered: 0", "   Skills registered: 0"]

    def test_gateway_error_message_is_confined_to_one_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import app_lifecycle_client

        error = app_lifecycle_client.AppGatewayError("denied\n✅ enabled demo")

        assert str(error) == "denied\\x0a✅ enabled demo"

    def test_gateway_action_timeout_is_a_gateway_error_without_file_edit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        timeouts: list[int] = []

        def _open(
            request: urllib.request.Request, *, timeout: int, socket_path: Path, verify_peer: object
        ) -> _FakeResponse:
            assert socket_path.name == "dashboard-8123.sock"
            timeouts.append(timeout)
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _FakeResponse({"token": "dashboard-credential"})
            raise TimeoutError("timed out")

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
            patch("kiro_crew.cli_commands.enable_app") as local_enable,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))

        assert exc.value.code == 1
        assert timeouts == [5, 300]
        err = capsys.readouterr().err
        assert "… applying enable for demo through the running gateway" in err
        assert "⏳ the gateway did not finish enable for demo within 300 s" in err
        assert "may still be applying it" in err
        assert "applies it twice" in err
        assert "refused" not in err
        local_enable.assert_not_called()

    def test_gateway_route_error_sanitizes_terminal_controls(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = _http_error(
            409,
            json.dumps({"error": "busy\x1b]0;evil\x07 now\x1b[2J\x07"}).encode(),
        )
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[_FakeResponse({"token": "dashboard-credential"}), error],
            ),
            pytest.raises(SystemExit),
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))

        err = capsys.readouterr().err
        assert "gateway refused: busy now" in err
        assert "\x1b" not in err
        assert "\x07" not in err
        assert "evil" not in err

    def test_disable_forwards_to_running_gateway_without_local_edits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        requests: list[urllib.request.Request] = []

        def _open(
            request: urllib.request.Request, *, timeout: int, socket_path: Path, verify_peer: object
        ) -> _FakeResponse:
            assert socket_path.name == "dashboard-8123.sock"
            requests.append(request)
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _FakeResponse({"token": "dashboard-credential"})
            return _FakeResponse(
                {
                    "ok": True,
                    "message": "disabled demo",
                    "warnings": ["backend still listening on port 9100"],
                }
            )

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
            patch("kiro_crew.cli_commands.disable_app") as local_disable,
            patch("kiro_crew.cli_commands.deregister_app") as local_deregister,
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler") as local_cleanup,
        ):
            cc._handle_app(_ns(app_action="disable", name="demo"))

        assert len(requests) == 2
        assert requests[1].get_method() == "POST"
        assert "/api/apps/demo/disable?" in requests[1].full_url
        local_disable.assert_not_called()
        local_deregister.assert_not_called()
        local_cleanup.assert_not_called()
        assert "⚠️  backend still listening on port 9100" in capsys.readouterr().err

    def test_default_port_evidence_attempts_socket_then_reports_file_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(5476, False)
            ),
            patch(
                "kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"
            ) as credential,
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=urllib.error.URLError(FileNotFoundError("no socket")),
            ) as urlopen,
            patch("kiro_crew.cli_commands.enable_app", return_value=_result(True)) as local_enable,
            patch(
                "kiro_crew.cli_commands.register_app", return_value=_registration()
            ) as local_register,
            patch("kiro_crew.cli_commands._register_app_crons_to_scheduler"),
            patch("kiro_crew.cli_commands._warn_hooks_need_restart"),
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))

        credential.assert_called_once_with(5476)
        assert urlopen.call_args.kwargs["socket_path"].name == "dashboard-5476.sock"
        local_enable.assert_called_once_with("demo")
        local_register.assert_called_once_with("demo")
        out = capsys.readouterr().out
        assert "✅ Recorded demo as enabled." in out
        assert "No running gateway was reached" in out
        assert "next gateway start" in out
        assert "apply it live from the dashboard" in out

    def test_missing_local_secret_keeps_enable_file_only_behavior(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value=""),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen") as urlopen,
            patch("kiro_crew.cli_commands.enable_app", return_value=_result(True)) as local_enable,
            patch(
                "kiro_crew.cli_commands.register_app", return_value=_registration()
            ) as local_register,
            patch("kiro_crew.cli_commands._register_app_crons_to_scheduler"),
            patch("kiro_crew.cli_commands._warn_hooks_need_restart"),
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))

        urlopen.assert_not_called()
        local_enable.assert_called_once_with("demo")
        local_register.assert_called_once_with("demo")
        assert "✅ Recorded demo as enabled. No running gateway was reached" in (
            capsys.readouterr().out
        )

    def test_gateway_route_error_exits_without_file_edit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = _http_error(409, json.dumps({"error": "app is busy"}).encode())
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[_FakeResponse({"token": "dashboard-credential"}), error],
            ),
            patch("kiro_crew.cli_commands.enable_app") as local_enable,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))

        assert exc.value.code == 1
        assert "gateway refused: app is busy" in capsys.readouterr().err
        local_enable.assert_not_called()

    def test_gateway_mint_error_exits_with_dashboard_hint(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = _http_error(403, json.dumps({"error": "local credential denied"}).encode())
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=error),
            patch("kiro_crew.cli_commands.disable_app") as local_disable,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="disable", name="demo"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "gateway refused: local credential denied" in err
        assert "Toggle the app in the dashboard instead" in err
        local_disable.assert_not_called()

    def test_install_forwards_to_running_gateway_without_local_edits(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "demo-app"
        source.mkdir()
        monkeypatch.chdir(tmp_path)
        requests: list[urllib.request.Request] = []

        def _open(
            request: urllib.request.Request, *, timeout: int, socket_path: Path, verify_peer: object
        ) -> _FakeResponse:
            assert socket_path.name == "dashboard-8123.sock"
            requests.append(request)
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _FakeResponse({"token": "dashboard-credential"})
            return _FakeResponse(
                {
                    "ok": True,
                    "name": "demo",
                    "message": "Installed demo v1.0.0",
                    "registration": {
                        "agents": ["demo-agent"],
                        "skills": ["demo-skill"],
                        "crons": [],
                        "errors": ["skill registration deferred"],
                    },
                }
            )

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
            patch("kiro_crew.cli_commands.install_app") as local_install,
            patch("kiro_crew.cli_commands.register_app") as local_register,
        ):
            # A relative path: the gateway's working directory is not this shell's.
            cc._handle_app(_ns(app_action="install", source="demo-app"))

        assert len(requests) == 2
        action = requests[1]
        assert action.get_method() == "POST"
        assert "/api/apps/install?" in action.full_url
        assert json.loads(action.data) == {"source": str(source.resolve())}
        assert action.get_header("Content-type") == "application/json"
        local_install.assert_not_called()
        local_register.assert_not_called()
        captured = capsys.readouterr()
        assert "✅ Installed demo v1.0.0" in captured.out
        assert "Agents: demo-agent" in captured.out
        assert "Skills: demo-skill" in captured.out
        assert "Crons:" not in captured.out
        assert "Run: kirocrew app enable demo" in captured.out
        assert "session approval" not in captured.out
        assert "… applying install for" in captured.err
        assert "⚠️  skill registration deferred" in captured.err

    def test_forwarded_install_pending_consent_names_the_enable_step(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import app_lifecycle_client

        app_lifecycle_client.print_result(
            "install",
            "demo",
            {
                "ok": True,
                "name": "demo",
                "message": "Installed demo v1.0.0",
                "notice": "session_approval_reconsent",
                "registration": {"agents": [], "skills": [], "crons": [], "errors": []},
            },
        )

        out = capsys.readouterr().out
        assert "✅ Installed demo v1.0.0" in out
        assert "stays off until enabled" in out
        assert "Run: kirocrew app enable demo" in out

    def test_install_without_reachable_gateway_keeps_file_only_path(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(5476, False)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=urllib.error.URLError(FileNotFoundError("no socket")),
            ) as urlopen,
            patch(
                "kiro_crew.cli_commands.install_app", return_value=_result(True, name="demo")
            ) as local_install,
            patch(
                "kiro_crew.cli_commands.register_app", return_value=_registration(agents=["a"])
            ) as local_register,
        ):
            cc._handle_app(_ns(app_action="install", source="/pkg"))

        assert urlopen.call_args.kwargs["socket_path"].name == "dashboard-5476.sock"
        local_install.assert_called_once_with("/pkg")
        local_register.assert_called_once_with("demo")
        out = capsys.readouterr().out
        assert "Agents: a" in out
        assert "kirocrew app enable demo" in out

    def test_install_missing_local_secret_keeps_file_only_path(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value=""),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen") as urlopen,
            patch(
                "kiro_crew.cli_commands.install_app", return_value=_result(True, name="demo")
            ) as local_install,
            patch("kiro_crew.cli_commands.register_app", return_value=_registration()),
        ):
            cc._handle_app(_ns(app_action="install", source="/pkg"))

        urlopen.assert_not_called()
        local_install.assert_called_once_with("/pkg")
        out = capsys.readouterr().out
        assert out.startswith("✅ done\n")
        assert "Run: kirocrew app enable demo" in out

    def test_install_gateway_route_error_exits_without_file_edit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = _http_error(
            400, json.dumps({"ok": False, "name": "", "error": "no app.json"}).encode()
        )
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[_FakeResponse({"token": "dashboard-credential"}), error],
            ),
            patch("kiro_crew.cli_commands.install_app") as local_install,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="install", source="/pkg"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "gateway refused: no app.json" in err
        assert "Nothing was installed; the gateway reads the app directory" in err
        local_install.assert_not_called()

    def test_install_gateway_mint_error_exits_without_file_edit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = _http_error(403, json.dumps({"error": "local credential denied"}).encode())
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=error),
            patch("kiro_crew.cli_commands.install_app") as local_install,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="install", source="/pkg"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "gateway refused: local credential denied" in err
        assert "Nothing was installed; the gateway reads the app directory" in err
        local_install.assert_not_called()

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (http.client.BadStatusLine(""), "gateway"),
            (http.client.RemoteDisconnected(""), "gateway"),
            (http.client.LineTooLong("status line"), "gateway"),
            (http.client.IncompleteRead(b""), "gateway"),
            (ConnectionResetError(), "gateway"),
            (TimeoutError(), "gateway"),
            (socket.timeout(), "gateway"),
            (urllib.error.URLError(ConnectionResetError()), "gateway"),
            (ValueError("bad json"), "gateway"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), "gateway"),
            (RuntimeError("peer garbage"), "gateway"),
            (urllib.error.HTTPError("http://localhost", 500, "broken", {}, None), "gateway"),
            (FileNotFoundError("missing"), "file-only"),
            (ConnectionRefusedError("stale"), "file-only"),
            (urllib.error.URLError(FileNotFoundError("missing")), "file-only"),
            (urllib.error.URLError(ConnectionRefusedError("stale")), "file-only"),
            (OSError("AF_UNIX sockets are not available on this platform"), "file-only"),
            (KeyboardInterrupt(), "interrupt"),
        ],
        ids=[
            "bad-status-line",
            "remote-disconnected",
            "line-too-long",
            "incomplete-read",
            "connection-reset",
            "timeout",
            "socket-timeout",
            "wrapped-reset",
            "bad-json",
            "bad-unicode",
            "runtime-error",
            "http-error",
            "missing-direct",
            "refused-direct",
            "missing-wrapped",
            "refused-wrapped",
            "af-unix-unavailable",
            "keyboard-interrupt",
        ],
    )
    def test_mint_exception_surface_is_classified_by_outcome(
        self, failure: BaseException, expected: str
    ) -> None:
        from kiro_crew import app_lifecycle_client

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, False)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=failure),
            patch("kiro_crew.cli_commands.enable_app") as local_enable,
        ):
            if expected == "file-only":
                assert app_lifecycle_client.toggle_app("demo", "enable") is None
            elif expected == "interrupt":
                with pytest.raises(KeyboardInterrupt):
                    app_lifecycle_client.toggle_app("demo", "enable")
            else:
                with pytest.raises(app_lifecycle_client.AppGatewayError):
                    app_lifecycle_client.toggle_app("demo", "enable")
        local_enable.assert_not_called()

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (http.client.BadStatusLine(""), "gateway"),
            (http.client.RemoteDisconnected(""), "gateway"),
            (http.client.LineTooLong("status line"), "gateway"),
            (http.client.IncompleteRead(b""), "gateway"),
            (ConnectionResetError(), "gateway"),
            (TimeoutError(), "gateway"),
            (socket.timeout(), "gateway"),
            (urllib.error.URLError(ConnectionResetError()), "gateway"),
            (ValueError("bad json"), "gateway"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), "gateway"),
            (RuntimeError("peer garbage"), "gateway"),
            (urllib.error.HTTPError("http://localhost", 500, "broken", {}, None), "gateway"),
            (FileNotFoundError("missing"), "gateway"),
            (ConnectionRefusedError("stale"), "gateway"),
            (urllib.error.URLError(FileNotFoundError("missing")), "gateway"),
            (urllib.error.URLError(ConnectionRefusedError("stale")), "gateway"),
            (OSError("AF_UNIX sockets are not available on this platform"), "gateway"),
            (KeyboardInterrupt(), "interrupt"),
        ],
        ids=[
            "bad-status-line",
            "remote-disconnected",
            "line-too-long",
            "incomplete-read",
            "connection-reset",
            "timeout",
            "socket-timeout",
            "wrapped-reset",
            "bad-json",
            "bad-unicode",
            "runtime-error",
            "http-error",
            "missing-direct",
            "refused-direct",
            "missing-wrapped",
            "refused-wrapped",
            "af-unix-unavailable",
            "keyboard-interrupt",
        ],
    )
    def test_action_exception_surface_is_classified_by_outcome(
        self, failure: BaseException, expected: str
    ) -> None:
        from kiro_crew import app_lifecycle_client

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[_FakeResponse({"token": "dashboard-credential"}), failure],
            ),
            patch("kiro_crew.cli_commands.enable_app") as local_enable,
        ):
            if expected == "interrupt":
                with pytest.raises(KeyboardInterrupt):
                    app_lifecycle_client.toggle_app("demo", "enable")
            else:
                with pytest.raises(app_lifecycle_client.AppGatewayError):
                    app_lifecycle_client.toggle_app("demo", "enable")
        local_enable.assert_not_called()

    def test_post_mint_socket_refusal_is_gateway_error_without_file_edit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        failure = urllib.error.URLError(ConnectionRefusedError("gateway restarted"))
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[_FakeResponse({"token": "dashboard-credential"}), failure],
            ),
            patch("kiro_crew.cli_commands.enable_app") as local_enable,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="enable", name="demo"))

        assert exc.value.code == 1
        assert (
            "gateway refused: gateway stopped answering before the action completed; "
            "check the dashboard" in capsys.readouterr().err
        )
        local_enable.assert_not_called()

    @pytest.mark.parametrize("stage", ["mint", "action"])
    @pytest.mark.parametrize(
        "failure",
        [
            OSError("connection reset"),
            urllib.error.URLError(OSError("broken transport")),
            TimeoutError("timed out"),
            urllib.error.URLError(TimeoutError("connect timed out")),
        ],
        ids=["direct", "wrapped", "timeout", "wrapped-timeout"],
    )
    def test_other_transport_failures_are_gateway_errors(
        self, stage: str, failure: OSError
    ) -> None:
        from kiro_crew import app_lifecycle_client

        side_effect: object
        expected_type: type[Exception] = app_lifecycle_client.AppGatewayError
        if stage == "mint":
            side_effect = failure
            expected = "could not mint local dashboard credential"
        else:
            side_effect = [_FakeResponse({"token": "dashboard-credential"}), failure]
            reason = failure.reason if isinstance(failure, urllib.error.URLError) else failure
            if isinstance(reason, TimeoutError):
                # Our deadline passing means the outcome is unknown, never a refusal.
                expected_type = app_lifecycle_client.AppGatewayTimeout
                expected = "did not finish enable for demo within 300 s"
            else:
                expected = (
                    "gateway stopped answering before the action completed; check the dashboard"
                )
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=side_effect),
            pytest.raises(expected_type, match=expected) as raised,
        ):
            app_lifecycle_client.toggle_app("demo", "enable")
        if expected_type is app_lifecycle_client.AppGatewayError:
            assert not isinstance(raised.value, app_lifecycle_client.AppGatewayTimeout)

    @pytest.mark.parametrize("stage", ["mint", "action"])
    @pytest.mark.parametrize(
        "bad_response",
        [_FakeResponse(b"{"), _IncompleteResponse(b'{"partial"')],
        ids=["malformed", "truncated"],
    )
    def test_malformed_or_truncated_gateway_responses_are_gateway_errors(
        self, stage: str, bad_response: _FakeResponse
    ) -> None:
        from kiro_crew import app_lifecycle_client

        side_effect = (
            [bad_response]
            if stage == "mint"
            else [_FakeResponse({"token": "dashboard-credential"}), bad_response]
        )
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=side_effect),
            pytest.raises(
                app_lifecycle_client.AppGatewayError,
                match="gateway returned a malformed response",
            ),
        ):
            app_lifecycle_client.toggle_app("demo", "enable")

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
        out = capsys.readouterr().out
        assert "✅ Recorded demo as disabled." in out
        assert "No running gateway was reached" in out

    def test_disable_flips_the_flag_before_deregistering(self) -> None:
        """Order is a security control, not cosmetics.

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
        self, purge: bool, expect_keep_data: bool, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.deregister_app"),
            patch("kiro_crew.cli_commands.uninstall_app", return_value=_result(True)) as uninstall,
        ):
            cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=purge))
        uninstall.assert_called_once_with("demo", keep_data=expect_keep_data)
        assert capsys.readouterr().out.startswith("✅ done\n")

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
        setter.assert_called_once_with("demo", False, confirm_out_of_install_root=False)
        assert "dev mode off" in capsys.readouterr().out

    def test_dev_confirmation_refusal_reaches_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The refusal's own text (which names the CLI flag) is printed as-is."""
        refusal = {
            "error": "needs confirmation: run `kirocrew app dev demo --confirm-out-of-install-root`",
            "code": "dev_mode_out_of_install_confirmation_required",
        }
        with (
            patch("kiro_crew.apps.dev_mode.set_dev_mode", return_value=refusal) as setter,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(
                _ns(app_action="dev", name="demo", off=False, confirm_out_of_install_root=False)
            )
        assert exc.value.code == 1
        assert "--confirm-out-of-install-root" in capsys.readouterr().err
        setter.assert_called_once_with("demo", True, confirm_out_of_install_root=False)

    def test_dev_confirm_flag_is_forwarded(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("kiro_crew.apps.dev_mode.set_dev_mode", return_value={"ok": True}) as setter:
            cc._handle_app(
                _ns(app_action="dev", name="demo", off=False, confirm_out_of_install_root=True)
            )
        setter.assert_called_once_with("demo", True, confirm_out_of_install_root=True)
        captured = capsys.readouterr()
        assert "✅ demo is now in dev mode" in captured.out
        assert captured.err == ""

    def test_dev_confirm_flag_with_off_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The flag only applies to enabling — an ignored flag must say so,
        not exit 0 looking like a confirmed grant."""
        with patch("kiro_crew.apps.dev_mode.set_dev_mode", return_value={"ok": True}) as setter:
            cc._handle_app(
                _ns(app_action="dev", name="demo", off=True, confirm_out_of_install_root=True)
            )
        err = capsys.readouterr().err
        assert "no effect with --off" in err
        setter.assert_called_once_with("demo", False, confirm_out_of_install_root=True)

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

    def test_mcp_action_delegates_to_server_runner(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("kiro_crew.cli_commands._run_app_mcp_server") as runner:
            cc._handle_app(_ns(app_action="mcp", name="demo"))
        runner.assert_called_once_with("demo")
        # stdout is the MCP stdio channel: the dispatcher must add nothing to it.
        assert capsys.readouterr().out == ""

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


class TestDevConfirmFlagNoAbbreviation:
    """The dev subparser must reject flag abbreviations.

    The builtin agent deny rule for `--confirm-out-of-install-root` matches
    the flag's LITERAL text, but argparse's default `allow_abbrev=True` would
    accept `--confirm` (or `--c`) as the same flag — an agent shell could then
    supply the operator attestation in a spelling the rule never sees. The
    `dev` subparser is built with `allow_abbrev=False`, so only the exact
    flag parses and the substring rule stays sufficient.
    """

    def test_abbreviated_confirm_flag_is_rejected(self, capsys) -> None:
        import sys
        from unittest.mock import patch

        for abbrev in ("--confirm", "--confirm-out-of-install-roo", "--c"):
            argv = ["kirocrew", "app", "dev", "demo", abbrev]
            with (
                patch.object(sys, "argv", argv),
                patch("kiro_crew.cli_commands._handle_app") as handler,
            ):
                from kiro_crew.cli import main

                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 2, f"{abbrev} did not exit 2"
                handler.assert_not_called()
            err = capsys.readouterr().err
            assert "unrecognized arguments" in err, f"{abbrev}: {err!r}"

    def test_literal_confirm_flag_still_parses(self) -> None:
        import sys
        from unittest.mock import patch

        argv = ["kirocrew", "app", "dev", "demo", "--confirm-out-of-install-root"]
        with (
            patch.object(sys, "argv", argv),
            patch("kiro_crew.cli_commands._handle_app") as handler,
        ):
            from kiro_crew.cli import main

            main()
            ns = handler.call_args[0][0]
            assert ns.confirm_out_of_install_root is True
            assert ns.off is False
