"""``kirocrew app install``/``update`` name the gateway that decided, and its version.

The CLI forwards these to the running gateway, which can be older than the CLI
until it restarts, so its answer says which version decided and, when that
differs from the CLI's, says to restart the gateway on a refusal.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
from cli_commands_coverage_helpers import _FakeResponse, _http_error, _ns

from kiro_crew import __version__ as cli_version
from kiro_crew import cli_commands as cc

_REFUSAL = {
    "error": "App requires Kiro Crew >= 99.0.0, but this Kiro Crew is version 0.0.1.",
    "code": "kirocrew_version_too_old",
}


def _gateway(action_answer: object, version_answer: object):
    """A fake socket opener: mint, then the action, then ``/api/version``."""
    urls: list[str] = []

    def _open(
        sent: urllib.request.Request, *, timeout: int, socket_path: Path, verify_peer: object
    ) -> _FakeResponse:
        urls.append(sent.full_url)
        if "/api/token/local" in sent.full_url:
            return _FakeResponse({"token": "dashboard-credential"})
        answer = version_answer if "/api/version?" in sent.full_url else action_answer
        if isinstance(answer, BaseException):
            raise answer
        return _FakeResponse(answer)

    return _open, urls


def _install(opener: object) -> None:
    with (
        patch("kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)),
        patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
        patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=opener),
        patch("kiro_crew.cli_commands.install_app") as local_install,
    ):
        cc._handle_app(_ns(app_action="install", source="/pkg"))
    local_install.assert_not_called()


def test_an_older_gateways_refusal_names_both_versions_and_the_restart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    opener, urls = _gateway(_http_error(400, json.dumps(_REFUSAL).encode()), {"version": "0.0.1"})
    with pytest.raises(SystemExit) as exc:
        _install(opener)

    assert exc.value.code == 1
    # Read while the two-minute credential is fresh: right after the mint, before
    # an action that can take five minutes.
    assert "/api/version?token=dashboard-credential" in urls[1]
    assert "/api/apps/install?" in urls[2]
    err = capsys.readouterr().err
    assert "gateway refused: App requires Kiro Crew >= 99.0.0" in err
    assert (
        f"Decided by the running gateway, Kiro Crew 0.0.1; this kirocrew CLI is {cli_version}."
        in err
    )
    assert "restart the gateway so it runs the same version: kirocrew restart" in err


def test_a_same_version_install_names_the_gateway_without_a_restart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    opener, _ = _gateway(
        {"ok": True, "name": "demo", "message": "Installed demo"}, {"version": cli_version}
    )
    _install(opener)

    captured = capsys.readouterr()
    assert f"   Decided by the running gateway, Kiro Crew {cli_version}.\n" in captured.out
    assert "kirocrew restart" not in captured.out + captured.err


def test_a_gateway_that_does_not_name_its_version_keeps_the_plain_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    opener, _ = _gateway(_http_error(400, json.dumps(_REFUSAL).encode()), _http_error(404))
    with pytest.raises(SystemExit):
        _install(opener)

    err = capsys.readouterr().err
    assert "gateway refused: App requires Kiro Crew >= 99.0.0" in err
    assert "Decided by" not in err


def test_enable_does_not_read_the_gateway_version() -> None:
    opener, urls = _gateway({"ok": True, "message": "enabled demo"}, {"version": "0.0.1"})
    with (
        patch("kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)),
        patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="local-secret"),
        patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=opener),
        patch("kiro_crew.cli_commands.enable_app"),
    ):
        cc._handle_app(_ns(app_action="enable", name="demo"))

    assert not any("/api/version" in url for url in urls)
