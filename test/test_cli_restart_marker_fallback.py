from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def test_authenticated_shutdown_request_uses_fixed_loopback_endpoint() -> None:
    from kiro_crew import cli_server

    assert hasattr(cli_server, "_request_gateway_shutdown")
    _request_gateway_shutdown = cli_server._request_gateway_shutdown

    response = MagicMock(status=200)
    response.read.return_value = json.dumps({"ok": True, "shutting_down": True}).encode()
    context = MagicMock()
    context.__enter__.return_value = response

    with (
        patch("kiro_crew.cli_server.run_marker.read_secret", return_value="local-secret"),
        patch("kiro_crew.cli_server.loopback_urlopen", return_value=context) as mock_open,
    ):
        assert _request_gateway_shutdown(7777) is True

    request = mock_open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:7777/api/shutdown"
    assert request.method == "POST"
    assert request.get_header("X-local-secret") == "local-secret"


def test_authenticated_shutdown_request_fails_closed_without_secret() -> None:
    from kiro_crew import cli_server

    assert hasattr(cli_server, "_request_gateway_shutdown")
    _request_gateway_shutdown = cli_server._request_gateway_shutdown

    with (
        patch("kiro_crew.cli_server.run_marker.read_secret", return_value=""),
        patch("kiro_crew.cli_server.loopback_urlopen") as mock_open,
    ):
        assert _request_gateway_shutdown(7777) is False
    mock_open.assert_not_called()


def test_authenticated_shutdown_request_rejects_non_object_response() -> None:
    from kiro_crew import cli_server

    assert hasattr(cli_server, "_request_gateway_shutdown")
    _request_gateway_shutdown = cli_server._request_gateway_shutdown

    response = MagicMock(status=200)
    response.read.return_value = b"[]"
    context = MagicMock()
    context.__enter__.return_value = response

    with (
        patch("kiro_crew.cli_server.run_marker.read_secret", return_value="local-secret"),
        patch("kiro_crew.cli_server.loopback_urlopen", return_value=context),
    ):
        assert _request_gateway_shutdown(7777) is False


def test_stop_uses_authenticated_shutdown_when_listener_lookup_is_blind(capsys) -> None:
    from kiro_crew import cli_server

    assert hasattr(cli_server, "_report_authenticated_shutdown")
    _stop = cli_server._stop

    mock_sel = MagicMock()
    with (
        patch("kiro_crew.cli_server.service_controller.stop_service", return_value=False),
        patch(
            "kiro_crew.cli_server.platform_compat.find_listening_pids",
            return_value=[],
        ),
        patch(
            "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
            return_value=True,
        ),
        patch("kiro_crew.cli_server._request_gateway_shutdown", return_value=True),
        patch("kiro_crew.cli_server.sel", return_value=mock_sel),
    ):
        _stop(7777)

    assert "graceful shutdown" in capsys.readouterr().out
    audit = mock_sel.log_api_access.call_args.kwargs
    assert audit["outcome"] == "allowed"
    assert "via=api" in audit["resources"]


def test_authenticated_shutdown_request_rejects_oversized_response() -> None:
    from kiro_crew import cli_server

    response = MagicMock(status=200)
    response.read.return_value = b"x" * (cli_server._SHUTDOWN_RESPONSE_MAX_BYTES + 1)
    context = MagicMock()
    context.__enter__.return_value = response

    with (
        patch("kiro_crew.cli_server.run_marker.read_secret", return_value="local-secret"),
        patch("kiro_crew.cli_server.loopback_urlopen", return_value=context),
    ):
        assert cli_server._request_gateway_shutdown(7777) is False

    response.read.assert_called_once_with(cli_server._SHUTDOWN_RESPONSE_MAX_BYTES + 1)


def test_authenticated_shutdown_request_rejects_recursive_json() -> None:
    from kiro_crew import cli_server

    response = MagicMock(status=200)
    response.read.return_value = b"[" * 1500 + b"]" * 1500
    context = MagicMock()
    context.__enter__.return_value = response

    with (
        patch("kiro_crew.cli_server.run_marker.read_secret", return_value="local-secret"),
        patch("kiro_crew.cli_server.loopback_urlopen", return_value=context),
    ):
        assert cli_server._request_gateway_shutdown(7777) is False


@pytest.mark.parametrize("marker_pid", [None, 1234])
def test_restart_refuses_spawn_after_blind_authenticated_shutdown(
    capsys, marker_pid: int | None
) -> None:
    from kiro_crew import cli_server

    mock_sel = MagicMock()
    with (
        patch("kiro_crew.cli_server.resolve_client_port", return_value=7777),
        patch(
            "kiro_crew.cli_server.service_controller.restart_service",
            return_value=False,
        ),
        patch(
            "kiro_crew.cli_server.service_controller.is_service_active",
            return_value=False,
        ),
        patch("kiro_crew.cli_server.run_marker.read_pid", return_value=marker_pid),
        patch(
            "kiro_crew.cli_server.platform_compat.find_listening_pids",
            return_value=[],
        ),
        patch(
            "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
            return_value=True,
        ),
        patch("kiro_crew.cli_server._report_authenticated_shutdown", return_value=True),
        patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        patch("kiro_crew.cli_server.sel", return_value=mock_sel),
    ):
        with pytest.raises(SystemExit) as exc:
            cli_server._restart(None)

    assert exc.value.code == 1
    mock_spawn.assert_not_called()
    assert "listener PID lookup is unavailable" in capsys.readouterr().out
    audit = mock_sel.log_api_access.call_args.kwargs
    assert audit["outcome"] == "denied"
    assert "reason=shutdown_ack_listener_lookup_blind" in audit["resources"]
