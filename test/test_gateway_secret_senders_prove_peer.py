"""Every CLI sender of the gateway's local/internal secret proves its peer first.

The value ``kirocrew`` reads from the local secret file is the gateway's
``X-Local-Secret`` and also its ``X-Internal-Secret``. These tests pin that the
spawn and artifact senders hand their request to the
shared connection-bound proof (``cli_server_peer._owner_verified_secret_urlopen``)
instead of a plain loopback opener, that a refusal from that proof is reported
and sends nothing, and that the app-lifecycle client checks the connected socket
peer before writing either of its two requests.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import kiro_crew.cli_commands as cc
from kiro_crew import app_lifecycle_client
from kiro_crew.cli_server_peer import _GatewayPeerRefused
from kiro_crew.config.loader import KiroCrewConfig

_PORT = 5476


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


class _ProvenSender:
    """Stand-in for the connection-bound proof: records each send, then answers."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[urllib.request.Request, int, str]] = []

    def __call__(
        self, req: urllib.request.Request, port: int, timeout: float, *, operation: str
    ) -> Any:
        self.sent.append((req, port, operation))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Resp(answer)


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


@pytest.fixture
def secret() -> Any:
    with patch("kiro_crew.cli_commands._internal_secret", return_value="s"):
        yield


class TestSpawnSendsThroughThePeerProof:
    def test_list_goes_through_the_proof(self, secret: None, capsys: Any) -> None:
        sender = _ProvenSender([{"agents": [], "queued": []}])
        with patch("kiro_crew.cli_server._owner_verified_secret_urlopen", sender):
            cc._spawn(_ns(spawn_action="list", port=_PORT))
        [(req, port, operation)] = sender.sent
        assert port == _PORT
        assert operation == "cli_spawn_list"
        assert req.get_header("X-internal-secret") == "s"
        assert "No subagents." in capsys.readouterr().out

    def test_run_and_its_poll_go_through_the_proof(self, secret: None, capsys: Any) -> None:
        sender = _ProvenSender([{"id": "ag1", "task": "t"}, {"done": True, "result": "finished"}])
        with (
            patch("kiro_crew.cli_server._owner_verified_secret_urlopen", sender),
            # The module's own clock binding, so no other code sees the stub.
            patch("kiro_crew.cli_commands._time", SimpleNamespace(sleep=lambda _s: None)),
        ):
            cc._spawn(_ns(spawn_action="run", port=_PORT, task="t", fire_and_forget=False))
        assert [op for _req, _port, op in sender.sent] == ["cli_spawn_run", "cli_spawn_poll"]
        assert all(req.get_header("X-internal-secret") == "s" for req, _p, _o in sender.sent)
        assert "finished" in capsys.readouterr().out

    @pytest.mark.parametrize("action", ["list", "run"])
    def test_a_refused_peer_is_reported_and_exits_1(
        self, secret: None, capsys: Any, action: str
    ) -> None:
        sender = _ProvenSender([_GatewayPeerRefused("peer not proven")])
        with (
            patch("kiro_crew.cli_server._owner_verified_secret_urlopen", sender),
            pytest.raises(SystemExit) as exc,
        ):
            cc._spawn(_ns(spawn_action=action, port=_PORT, task="t", fire_and_forget=True))
        assert exc.value.code == 1
        assert "gateway ownership not verified: peer not proven" in capsys.readouterr().out
        assert len(sender.sent) == 1


class TestArtifactSendsThroughThePeerProof:
    def _run(self, sender: _ProvenSender) -> None:
        with (
            patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
            patch("kiro_crew.cli_commands.parse_dashboard_url", return_value=("localhost", _PORT)),
            patch("kiro_crew.cli_commands._internal_secret", return_value="s"),
            patch("kiro_crew.cli_server._owner_verified_secret_urlopen", sender),
        ):
            cc._artifact(_ns(artifact_action="list", tag=None, kind=None, q=None))

    def test_list_goes_through_the_proof(self, capsys: Any) -> None:
        sender = _ProvenSender([{"artifacts": []}])
        self._run(sender)
        [(req, port, operation)] = sender.sent
        assert (port, operation) == (_PORT, "cli_artifact")
        assert req.get_header("X-internal-secret") == "s"
        assert "No artifacts." in capsys.readouterr().out

    def test_a_refused_peer_is_reported(self, capsys: Any) -> None:
        sender = _ProvenSender([_GatewayPeerRefused("peer not proven")])
        with pytest.raises(SystemExit):
            self._run(sender)
        assert "gateway ownership not verified" in capsys.readouterr().err


class TestAppLifecycleChecksTheSocketPeer:
    def _run(self, sent: list[urllib.request.Request], *, peer_is_gateway: bool) -> None:
        def _open(req: Any, *, timeout: float, socket_path: Any, verify_peer: Any = None) -> _Resp:
            assert verify_peer is not None, "the socket send must check its peer"
            verify_peer(object())
            sent.append(req)
            if "/api/token/local" in req.full_url:
                return _Resp({"token": "tok"})
            return _Resp({"ok": True})

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex",
                return_value=(_PORT, True),
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="s"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
            patch(
                "kiro_crew.cli_server._connected_unix_peer_is_recorded_gateway",
                return_value=peer_is_gateway,
            ),
        ):
            app_lifecycle_client.toggle_app("demo", "enable")

    @pytest.mark.parametrize(("peer_is_gateway", "outcome"), [(True, "allowed"), (False, "denied")])
    def test_each_peer_decision_is_audited(self, peer_is_gateway: bool, outcome: str) -> None:
        sent: list[urllib.request.Request] = []
        with patch("kiro_crew.cli_server.sel") as sel:
            try:
                self._run(sent, peer_is_gateway=peer_is_gateway)
            except app_lifecycle_client.AppGatewayError:
                pass
        outcomes = [c.kwargs["outcome"] for c in sel.return_value.log_api_access.call_args_list]
        assert outcome in outcomes
        calls = sel.return_value.log_api_access.call_args_list
        assert all(c.kwargs["operation"] == "app_lifecycle" for c in calls)

    def test_the_recorded_gateway_receives_both_requests(self) -> None:
        sent: list[urllib.request.Request] = []
        self._run(sent, peer_is_gateway=True)
        assert len(sent) == 2

    def test_another_socket_peer_receives_nothing(self) -> None:
        sent: list[urllib.request.Request] = []
        with pytest.raises(app_lifecycle_client.AppGatewayError, match="recorded gateway") as exc:
            self._run(sent, peer_is_gateway=False)
        assert not isinstance(exc.value, OSError)
        assert sent == []
