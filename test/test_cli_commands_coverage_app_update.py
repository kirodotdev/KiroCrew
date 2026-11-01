"""``kirocrew app update``: the one app verb with no file-only path.

Same shape as ``test_cli_commands_coverage.py``, whose docstring lists the topic
split: the handler is called directly with an ``argparse.Namespace``, the gateway
socket is faked through ``app_lifecycle_client.unix_socket_urlopen``, and nothing
touches the data home. What is pinned here: the request the gateway receives (the
app's update route, an absolute source, or no body at all), how its answer is
rendered, and that with no gateway reachable the command refuses and changes
nothing instead of editing files the way ``enable`` and ``disable`` do.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cli_commands_coverage_helpers import _FakeResponse, _http_error, _ns

from kiro_crew import cli_commands as cc
from kiro_crew import cli_commands_app
from kiro_crew.cli_parser_agent import register_app_parser

_PORT = 8123


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    register_app_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["app", *argv])


def _answering(
    answer: dict[str, Any], requests: list[urllib.request.Request]
) -> Callable[..., _FakeResponse]:
    """A socket that mints the credential, then answers the action with *answer*."""

    def _open(
        request: urllib.request.Request, *, timeout: int, socket_path: Path, verify_peer: object
    ) -> _FakeResponse:
        assert socket_path.name == f"dashboard-{_PORT}.sock"
        requests.append(request)
        if request.full_url.endswith("/api/token/local?ttl=2m"):
            return _FakeResponse({"token": "dashboard-credential"})
        return _FakeResponse(answer)

    return _open


@contextlib.contextmanager
def _gateway(side_effect: object, *, secret: str = "local-secret") -> Iterator[MagicMock]:
    """Resolve a port and secret, and hand the socket open to *side_effect*."""
    with (
        patch("kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(_PORT, True)),
        patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value=secret),
        patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=side_effect) as m,
    ):
        yield m


class TestParser:
    def test_source_is_optional(self) -> None:
        args = _parse("update", "demo")
        assert (args.app_action, args.name, args.source) == ("update", "demo", None)

    def test_source_is_a_flag(self) -> None:
        assert _parse("update", "demo", "--source", "/apps/demo").source == "/apps/demo"


class TestUpdateThroughGateway:
    def test_sends_the_absolute_source_to_the_update_route(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "demo-app"
        source.mkdir()
        monkeypatch.chdir(tmp_path)
        requests: list[urllib.request.Request] = []
        answer = {
            "ok": True,
            "name": "demo",
            "message": "updated demo v1.0.0 -> v1.1.0",
            "registration": {"agents": ["demo-agent"], "skills": [], "crons": [], "errors": []},
        }

        with _gateway(_answering(answer, requests)):
            # A relative path: the gateway's working directory is not this shell's.
            cc._handle_app(_ns(app_action="update", name="demo", source="demo-app"))

        assert len(requests) == 2
        action = requests[1]
        assert action.get_method() == "POST"
        assert "/api/apps/demo/update?" in action.full_url
        assert json.loads(action.data) == {"source": str(source.resolve())}
        assert action.get_header("Content-type") == "application/json"
        captured = capsys.readouterr()
        assert "✅ updated demo v1.0.0 -> v1.1.0" in captured.out
        assert "Agents registered: 1" in captured.out
        assert "Skills registered: 0" in captured.out
        assert "kirocrew app enable" not in captured.out
        assert "… applying update for demo" in captured.err

    def test_without_source_sends_no_body_so_the_gateway_uses_the_recorded_one(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        requests: list[urllib.request.Request] = []

        with _gateway(_answering({"ok": True, "message": "updated demo v1 -> v2"}, requests)):
            cc._handle_app(_ns(app_action="update", name="demo", source=None))

        action = requests[1]
        assert "/api/apps/demo/update?" in action.full_url
        assert action.data is None
        assert action.get_header("Content-type") is None
        assert "✅ updated demo v1 -> v2" in capsys.readouterr().out

    def test_an_update_parked_for_consent_gets_no_enable_hint(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gateway leaves an app off when the new version newly asks to control
        # the owner's chats, and the CLI cannot give that consent, so no enable line.
        answer = {
            "ok": True,
            "name": "demo",
            "message": (
                "updated demo v1 -> v2; disabled because this version newly requests "
                "session approval control -- review it on the app page and enable again"
            ),
            "notice": "session_approval_reconsent",
        }

        with _gateway(_answering(answer, [])):
            cc._handle_app(_ns(app_action="update", name="demo", source=None))

        out = capsys.readouterr().out
        assert "disabled because this version newly requests session approval" in out
        assert "kirocrew app enable" not in out
        assert "registered" not in out

    def test_registration_errors_are_warnings_not_a_failure(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        answer = {
            "ok": True,
            "message": "updated demo v1 -> v2",
            "registration": {"agents": [], "skills": [], "crons": [], "errors": ["cron x skipped"]},
        }

        with _gateway(_answering(answer, [])):
            cc._handle_app(_ns(app_action="update", name="demo", source=None))

        captured = capsys.readouterr()
        assert "✅ updated demo v1 -> v2" in captured.out
        assert "⚠️  cron x skipped" in captured.err

    def test_a_refusal_exits_1_and_names_sync(self, capsys: pytest.CaptureFixture[str]) -> None:
        refusal = {
            "ok": False,
            "name": "demo",
            "error": "source manifest name 'other' does not match app 'demo'",
        }
        error = _http_error(400, json.dumps(refusal).encode())

        with (
            _gateway([_FakeResponse({"token": "dashboard-credential"}), error]),
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="update", name="demo", source="/apps/other"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "gateway refused: source manifest name 'other' does not match app 'demo'" in err
        assert "Use Sync on the app's page in the dashboard instead" in err


class TestUpdateHasNoFileOnlyPath:
    def test_no_socket_refuses_and_changes_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            _gateway(urllib.error.URLError(FileNotFoundError("no socket"))) as urlopen,
            pytest.raises(SystemExit) as exc,
        ):
            cc._handle_app(_ns(app_action="update", name="demo", source="/apps/demo"))

        assert exc.value.code == 1
        assert urlopen.call_args.kwargs["socket_path"].name == f"dashboard-{_PORT}.sock"
        err = capsys.readouterr().err
        assert "no running gateway answered, so demo was not updated" in err
        assert "Sync" in err

    def test_missing_local_secret_refuses_without_a_request(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _gateway(None, secret="") as urlopen, pytest.raises(SystemExit) as exc:
            cc._handle_app(_ns(app_action="update", name="demo", source=None))

        assert exc.value.code == 1
        urlopen.assert_not_called()
        assert "no running gateway answered" in capsys.readouterr().err

    def test_an_empty_source_is_refused_before_any_request(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _gateway(None) as urlopen, pytest.raises(SystemExit) as exc:
            cc._handle_app(_ns(app_action="update", name="demo", source="   "))

        assert exc.value.code == 1
        urlopen.assert_not_called()
        assert "--source is empty" in capsys.readouterr().err

    def test_the_handler_has_no_file_level_update_to_fall_back_on(self) -> None:
        # Structural half of the rule: the module cannot swap an app's files
        # itself, so there is no path that could leave a running gateway stale.
        assert "update_app" not in vars(cli_commands_app)
