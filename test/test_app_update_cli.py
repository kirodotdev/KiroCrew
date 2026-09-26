"""``kirocrew app update <name>`` is the App Store's Sync, from a terminal.

The dashboard's Sync button is ``POST /api/apps/{name}/update`` inside the running
gateway: stop the backend, deregister the old manifest's resources, swap the files
with ``data/`` preserved, re-register, restart. Nothing but that process can do the
swap correctly -- ``update_app`` alone replaces files while the gateway keeps
serving the old manifest -- so the CLI verb delegates through the owner-only socket
the way ``enable``/``disable``/``uninstall`` do, and unlike them it has NO file-only
fallback: when no gateway answers it exits 3 with nothing changed.

Exit codes are the contract an agent or script switches on:

  0  updated (old -> new version and the re-registered resource counts printed)
  3  no running gateway answered (socket absent, refused, or the mint failed)
  4  the source's ``app.json`` names a different app
  5  not installed, or the app's lifecycle is not the gateway's
  1  any other refusal, and a timeout whose outcome is unknown
  2  is argparse's own usage error and is deliberately not reused

The mapping reads the gateway's machine-readable ``code``, never its prose. The
socket is faked at ``unix_socket_urlopen`` exactly as ``test_cli_commands_coverage``
does, so the request the CLI sends and the answer it renders are both on the table.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import app_lifecycle_client
from kiro_crew import cli_commands as cc


def _ns(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "app_action": "update",
        "name": "demo",
        "source": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _http_error(code: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    fp = io.BytesIO(json.dumps(body).encode())
    return urllib.error.HTTPError("http://localhost/x", code, "Boom", {}, fp)  # type: ignore[arg-type]


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


_UPDATED = {
    "ok": True,
    "name": "demo",
    "message": "updated demo v1.0.0 -> v1.1.0",
    "previousVersion": "1.0.0",
    "version": "1.1.0",
    "registration": {
        "agents": ["demo:helper"],
        "skills": ["demo-skill", "demo-other"],
        "crons": [],
        "mcp_servers": ["demo:api", "demo:files"],
        "errors": [],
    },
}


def _drive(
    answer: Any, ns: argparse.Namespace | None = None
) -> tuple[list[urllib.request.Request], int | None]:
    """Run the verb against a fake gateway; return the requests it sent and its exit code.

    *answer* is the action's response (a ``_FakeResponse`` or an exception to raise);
    the credential mint always succeeds. ``None`` as the exit code means the command
    returned normally, which is exit 0.
    """
    requests: list[urllib.request.Request] = []

    def _open(request: urllib.request.Request, *, timeout: int, socket_path: Path) -> Any:
        assert socket_path.name == "dashboard-8123.sock"
        requests.append(request)
        if request.full_url.endswith("/api/token/local?ttl=2m"):
            return _FakeResponse({"token": "dashboard-credential"})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    exit_code: int | None = None
    with (
        patch("kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)),
        patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
        patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
        patch("kiro_crew.cli_commands.register_app") as local_register,
        patch("kiro_crew.cli_commands.deregister_app") as local_deregister,
    ):
        try:
            cc._handle_app(ns or _ns())
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0
    # Whatever the gateway answered, the CLI process never touches registration
    # itself: that is the half that would leave the running gateway stale.
    local_register.assert_not_called()
    local_deregister.assert_not_called()
    return requests, exit_code


# ── argument parsing ──


class TestArgumentParsing:
    """The whole argv -> namespace -> handler hop, through the real parser."""

    def _parse(self, monkeypatch: pytest.MonkeyPatch, *argv: str) -> argparse.Namespace:
        from kiro_crew import cli

        seen: dict[str, argparse.Namespace] = {}
        monkeypatch.setattr("kiro_crew.cli_commands._handle_app", lambda args: seen.update(ns=args))
        monkeypatch.setattr(sys, "argv", ["kirocrew", "app", "update", *argv])
        cli.main()
        return seen["ns"]

    def test_bare_name_defaults_to_the_recorded_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ns = self._parse(monkeypatch, "demo")
        assert ns.app_action == "update"
        assert ns.name == "demo"
        assert ns.source is None

    def test_source_flag_is_carried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ns = self._parse(monkeypatch, "demo", "--source", "./checkout/demo")
        assert ns.source == "./checkout/demo"

    def test_there_is_no_registry_flag(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The registry entry an app came from IS the app's name, so a registry
        # re-clone is what the bare verb already does for a registry-installed app,
        # and a flag naming a different entry could only ever update the wrong app.
        # Argparse refuses it as a usage error (exit 2) before any handler runs.
        from kiro_crew import cli

        monkeypatch.setattr(
            "kiro_crew.cli_commands._handle_app",
            lambda args: pytest.fail("handler must not run on a refused parse"),
        )
        monkeypatch.setattr(sys, "argv", ["kirocrew", "app", "update", "demo", "--registry", "x"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert "unrecognized arguments: --registry" in capsys.readouterr().err

    def test_name_is_required(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kiro_crew import cli

        monkeypatch.setattr(sys, "argv", ["kirocrew", "app", "update"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert "name" in capsys.readouterr().err

    def test_usage_line_names_update(self, capsys: pytest.CaptureFixture[str]) -> None:
        cc._handle_app(argparse.Namespace(app_action=None))
        assert "update" in capsys.readouterr().out


# ── the happy path ──


class TestUpdateThroughTheGateway:
    def test_posts_the_update_action_and_renders_the_transition(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        requests, exit_code = _drive(_FakeResponse(_UPDATED))

        assert exit_code is None
        assert len(requests) == 2
        action = requests[1]
        assert action.get_method() == "POST"
        assert "/api/apps/demo/update?" in action.full_url
        # No override -> no body: the handler then reads the recorded source.
        assert action.data is None

        out = capsys.readouterr().out
        assert "✅ updated demo v1.0.0 -> v1.1.0" in out
        assert "   Version: 1.0.0 -> 1.1.0" in out
        assert "   Agents registered: 1" in out
        assert "   Skills registered: 2" in out
        assert "   MCP servers registered: 2" in out
        assert "   Crons registered: 0" in out
        assert "No running gateway was reached" not in out

    def test_source_override_travels_as_an_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gateway resolves a relative path against ITS cwd, so the CLI must
        # send the terminal's resolution, not the raw argument.
        monkeypatch.chdir(tmp_path)
        relative = Path("checkout") / "demo"
        requests, exit_code = _drive(_FakeResponse(_UPDATED), _ns(source=str(relative)))
        assert exit_code is None
        action = requests[1]
        assert action.get_header("Content-type") == "application/json"
        assert json.loads(action.data) == {"source": str((tmp_path / relative).resolve())}

    def test_an_unresolvable_source_is_a_refusal_not_a_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ``Path.resolve`` raises on a path it cannot walk -- a symlink loop on
        # POSIX raises RuntimeError under Python 3.12 -- but Windows resolves the
        # same loop without raising, so the trigger is injected rather than built
        # on disk: what is under test is the verb's promise of a defined exit and
        # that nothing is sent to the gateway for a path it could not resolve.
        with (
            patch(
                "kiro_crew.cli_commands.Path.resolve",
                side_effect=RuntimeError("Symlink loop from '/tmp/loop/app'"),
            ),
            patch("kiro_crew.app_lifecycle_client.toggle_app") as toggled,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(source="/tmp/loop/app"))
        assert exc.value.code == 1
        toggled.assert_not_called()
        err = capsys.readouterr().err
        assert "could not be resolved" in err and "Symlink loop" in err

    def test_a_disabled_app_reports_that_nothing_was_re_registered(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gateway registers nothing for an app the update left disabled (a
        # widened session-approval grant awaiting re-consent), so ``registration``
        # is absent; the counts must not silently read as "still registered".
        answer = {
            "ok": True,
            "name": "demo",
            "message": "updated demo v1.0.0 -> v2.0.0; disabled because ...",
            "notice": "session_approval_reconsent",
            "previousVersion": "1.0.0",
            "version": "2.0.0",
        }
        _, exit_code = _drive(_FakeResponse(answer))
        assert exit_code is None
        out = capsys.readouterr().out
        assert "   Version: 1.0.0 -> 2.0.0" in out
        assert "Resources not re-registered: the app is disabled" in out
        assert "Agents registered:" not in out and "MCP servers registered:" not in out

    def test_gateway_strings_are_confined_to_one_sanitized_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        answer = dict(_UPDATED, previousVersion="1.0\x1b]0;evil\x07.0", version="1.1.0\n✅ forged")
        _, exit_code = _drive(_FakeResponse(answer))
        assert exit_code is None
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\x07" not in out and "evil" not in out
        assert all(line.startswith(("✅ ", "   ")) for line in out.rstrip("\n").split("\n"))


# ── the four failure exits ──


class TestFailureExits:
    def test_not_installed_exits_5(self, capsys: pytest.CaptureFixture[str]) -> None:
        answer = _http_error(
            404, {"error": "app 'demo' not installed", "code": "app_not_installed"}
        )
        _, exit_code = _drive(answer)
        assert exit_code == cc.APP_UPDATE_EXIT_NOT_UPDATABLE == 5
        assert "gateway refused: app 'demo' not installed" in capsys.readouterr().err

    def test_self_managed_lifecycle_exits_5(self, capsys: pytest.CaptureFixture[str]) -> None:
        answer = _http_error(
            400,
            {
                "error": "app 'demo' has lifecycle='app' — cannot be updated via this endpoint",
                "code": "app_lifecycle_not_gateway",
            },
        )
        _, exit_code = _drive(answer)
        assert exit_code == 5
        assert "lifecycle='app'" in capsys.readouterr().err

    def test_no_local_secret_exits_3_without_a_file_only_fallback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value=""),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen") as opened,
            patch("kiro_crew.cli_commands.register_app") as local_register,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns())
        assert exc.value.code == cc.APP_UPDATE_EXIT_GATEWAY_UNREACHABLE == 3
        opened.assert_not_called()
        local_register.assert_not_called()
        err = capsys.readouterr().err
        assert "no running gateway was reached, so demo was not updated" in err
        assert "no file-only fallback" in err

    def test_missing_socket_exits_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(5476, False)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=urllib.error.URLError(FileNotFoundError("no socket")),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns())
        assert exc.value.code == 3
        assert "was not updated" in capsys.readouterr().err

    def test_a_socket_that_will_not_mint_exits_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A socket exists but the credential mint never gets an answer (here a
        # permission error on connect): no gateway received the action, so this is
        # "unreachable", not a refusal -- and still exit 3, not 1.
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=urllib.error.URLError(PermissionError("denied")),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns())
        assert exc.value.code == 3
        err = capsys.readouterr().err
        assert "no running gateway answered, so demo was not updated" in err
        assert "could not mint local dashboard credential" in err

    def test_source_naming_another_app_exits_4(self, capsys: pytest.CaptureFixture[str]) -> None:
        answer = _http_error(
            400,
            {
                "ok": False,
                "name": "demo",
                "error": "source manifest name 'other' does not match app 'demo'",
                "code": "app_source_name_mismatch",
            },
        )
        _, exit_code = _drive(answer, _ns(source="/tmp/other"))
        assert exit_code == cc.APP_UPDATE_EXIT_SOURCE_MISMATCH == 4
        assert "does not match app 'demo'" in capsys.readouterr().err

    def test_an_unclassified_refusal_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        answer = _http_error(
            409,
            {
                "error": "cannot update 'demo' while its timed-out startup hook is still running",
                "code": "startup_hook_still_running",
            },
        )
        _, exit_code = _drive(answer)
        assert exit_code == 1
        assert "startup hook" in capsys.readouterr().err

    def test_a_prose_only_refusal_from_an_older_gateway_exits_1(self) -> None:
        # No ``code`` -> unclassified. The prose is never parsed for an exit code.
        _, exit_code = _drive(_http_error(404, {"error": "app 'demo' not installed"}))
        assert exit_code == 1

    def test_an_ok_false_body_is_mapped_by_its_code_too(self) -> None:
        # A 200 whose body says ``ok: false`` is a refusal as well; the code inside
        # it drives the exit the same way an HTTP error's does.
        answer = _FakeResponse(
            {"ok": False, "name": "demo", "error": "mismatch", "code": "app_source_name_mismatch"}
        )
        _, exit_code = _drive(answer)
        assert exit_code == 4

    def test_a_timeout_is_an_unknown_outcome_not_a_refusal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, exit_code = _drive(TimeoutError("timed out"))
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "⏳ the gateway did not finish update for demo" in err
        assert "refused" not in err


# ── the lifecycle client's classification ──


class TestGatewayErrorCarriesTheCode:
    def test_http_error_body_code_is_kept(self) -> None:
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="s"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[
                    _FakeResponse({"token": "t"}),
                    _http_error(404, {"error": "gone", "code": "app_not_installed"}),
                ],
            ),
            pytest.raises(app_lifecycle_client.AppGatewayError) as exc,
        ):
            app_lifecycle_client.toggle_app("demo", "update")
        assert exc.value.code == "app_not_installed"
        assert str(exc.value) == "gone"
        assert not isinstance(exc.value, app_lifecycle_client.AppGatewayUnreachable)

    def test_a_body_without_a_code_yields_an_empty_code(self) -> None:
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="s"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[_FakeResponse({"token": "t"}), _http_error(500, {"error": "boom"})],
            ),
            pytest.raises(app_lifecycle_client.AppGatewayError) as exc,
        ):
            app_lifecycle_client.toggle_app("demo", "update")
        assert exc.value.code == ""

    def test_unreachable_is_raised_only_for_the_mint(self) -> None:
        # The same transport failure DURING the action is not "unreachable": the
        # gateway may have received and applied it, so it stays a plain error.
        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="s"),
            patch(
                "kiro_crew.app_lifecycle_client.unix_socket_urlopen",
                side_effect=[_FakeResponse({"token": "t"}), ConnectionResetError("reset")],
            ),
            pytest.raises(app_lifecycle_client.AppGatewayError) as exc,
        ):
            app_lifecycle_client.toggle_app("demo", "update")
        assert not isinstance(exc.value, app_lifecycle_client.AppGatewayUnreachable)
        assert exc.value.code == ""

    def test_enable_still_prints_nothing_new(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The update-only lines must not leak into the verbs that already have a
        # pinned output shape.
        app_lifecycle_client.print_result(
            "enable",
            "demo",
            {
                "message": "enabled demo",
                "previousVersion": "1.0.0",
                "version": "1.1.0",
                "registration": {"agents": ["a"], "skills": [], "mcp_servers": ["m"], "crons": []},
            },
        )
        out = capsys.readouterr().out
        assert "Version:" not in out
        assert "MCP servers registered" not in out
        assert "Agents registered: 1" in out
