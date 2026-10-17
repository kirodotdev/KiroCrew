"""Crew's MCP servers reach a pi session through the gate extension's handoff file.

pi-acp stores the ACP ``session/new`` array and never hands it to pi, so the launch
writes the same composition to a per-session file the gate extension reads and
registers with pi's built-in MCP support. These cases pin the host half: what goes
in the file, where it lives, when it is (re)written and removed, and the version
floor. The extension half runs under Node in ``test_pi_gate_extension_mcp.py``; the live round
trip is ``test/fixtures/acp_frames/pi/mcp-stdio-mount-live.jsonl``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_PI,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.platform_compat import IS_WINDOWS

NONCE = "0123456789abcdef" * 2
KEY = bytes(range(32))

STDIO = {
    "name": "kirocrew-core",
    "command": "/opt/bin/python",
    "args": ["-m", "kiro_crew.mcp_gateway.stub", 7],
    "env": [{"name": "KIROCREW_STUB", "value": "1"}],
    "type": "stdio",
}


def _open(payload: bytes, nonce: str = NONCE, key: bytes = KEY) -> dict:
    """The extension's half, in Python: check the envelope, open the seal."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    body = json.loads(payload)
    assert body["kiro-crew-mcp"] == 1 and body["nonce"] == nonce
    plain = AESGCM(key).decrypt(
        base64.b64decode(body["iv"]), base64.b64decode(body["sealed"]), nonce.encode()
    )
    return json.loads(plain)


def _servers(elements, nonce=NONCE) -> dict:
    payload, _names = pi_mod.pi_mcp_handoff_payload(elements, nonce, KEY)
    return _open(payload, nonce)["servers"]


class TestTheHandoffPayload:
    def test_a_stdio_element_becomes_a_direct_pi_entry(self) -> None:
        assert _servers([STDIO]) == {
            "kirocrew-core": {
                "command": "/opt/bin/python",
                "args": ["-m", "kiro_crew.mcp_gateway.stub"],
                "env": {"KIROCREW_STUB": "1"},
                "exposure": "direct",
            }
        }

    def test_an_http_element_keeps_its_url_and_headers(self) -> None:
        element = {
            "name": "docs",
            "type": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": [{"name": "X-Team", "value": "a"}],
        }
        assert _servers([element]) == {
            "docs": {
                "url": "https://mcp.example.com/mcp",
                "headers": {"X-Team": "a"},
                "exposure": "direct",
            }
        }

    def test_an_sse_element_is_left_out(self) -> None:
        """pi rejects the legacy SSE transport."""
        element = {"name": "old", "type": "sse", "url": "https://x/sse", "headers": []}
        assert _servers([element]) == {}

    @pytest.mark.parametrize("value", ["${HOME}/token", "!cat /etc/passwd"])
    def test_a_value_pi_would_expand_or_run_withholds_the_server(self, value) -> None:
        """pi reads ``${NAME}`` and a leading ``!`` in env values; no other harness does."""
        element = {**STDIO, "env": [{"name": "TOKEN", "value": value}]}
        assert _servers([element]) == {}

    def test_a_name_pi_refuses_is_left_out(self) -> None:
        assert _servers([{**STDIO, "name": "has space"}]) == {}

    def test_the_first_element_of_a_name_wins(self) -> None:
        second = {**STDIO, "command": "/opt/bin/other"}
        assert _servers([STDIO, second])["kirocrew-core"]["command"] == "/opt/bin/python"

    def test_the_file_is_bound_to_the_nonce(self) -> None:
        payload, _ = pi_mod.pi_mcp_handoff_payload([STDIO], "a" * 32, KEY)
        assert json.loads(payload)["nonce"] == "a" * 32
        from cryptography.exceptions import InvalidTag

        # The nonce is the seal's associated data: a file relabelled for another
        # session does not open.
        body = json.loads(payload)
        body["nonce"] = NONCE
        with pytest.raises(InvalidTag):
            _open(json.dumps(body).encode(), NONCE)

    def test_the_servers_are_not_readable_without_the_key(self) -> None:
        """The file sits in a leaf every pi and DeepSeek child can read."""
        payload, _ = pi_mod.pi_mcp_handoff_payload([STDIO], NONCE, KEY)
        assert b"KIROCREW_STUB" not in payload
        assert b"/opt/bin/python" not in payload
        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            _open(payload, NONCE, bytes(32))

    def test_each_spawn_gets_its_own_key(self) -> None:
        assert pi_mod.new_pi_mcp_handoff_key() != pi_mod.new_pi_mcp_handoff_key()
        assert len(pi_mod.new_pi_mcp_handoff_key()) == 32


class TestTheHandoffFile:
    def test_it_is_written_owner_only_in_the_gate_artifact_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(tmp_path))
        payload, _ = pi_mod.pi_mcp_handoff_payload([STDIO], NONCE, KEY)
        path = pi_mod.publish_pi_mcp_handoff(NONCE, payload)
        assert os.path.dirname(path) == str(tmp_path)
        assert os.path.basename(path) == f"kirocrew_pi_gate_{os.getpid()}_mcp_{NONCE}.json"
        with open(path, "rb") as fh:
            assert fh.read() == payload
        if not IS_WINDOWS:
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o400
        pi_mod.discard_pi_mcp_handoff(path)
        assert not os.path.exists(path)
        # Discarding a file the extension already removed is the same outcome.
        pi_mod.discard_pi_mcp_handoff(path)

    def test_the_sweep_reclaims_its_family(self) -> None:
        from kiro_crew import sandbox

        assert ".json" in sandbox._PI_GATE_DIR_ARTIFACTS[sandbox._PI_GATE_ARTIFACT_PREFIX]


class TestTheClientLifecycle:
    def _client(self, tmp_path, monkeypatch, calls):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        client._pi_gate_nonce = NONCE
        path = str(tmp_path / f"kirocrew_pi_gate_1_mcp_{NONCE}.json")
        client._pi_mcp_handoff = (path, b"{}")
        monkeypatch.setattr(
            pi_mod,
            "publish_pi_mcp_handoff",
            lambda nonce, payload: calls.append(("publish", nonce, payload)),
        )
        monkeypatch.setattr(
            pi_mod, "discard_pi_mcp_handoff", lambda p: calls.append(("discard", p))
        )
        return client, path

    def test_it_is_republished_before_a_request_that_starts_pi(self, tmp_path, monkeypatch):
        calls: list = []
        client, _path = self._client(tmp_path, monkeypatch, calls)
        asyncio.run(client._republish_pi_mcp_handoff())
        assert calls == [("publish", NONCE, b"{}")]

    def test_it_is_removed_once_and_forgotten(self, tmp_path, monkeypatch):
        calls: list = []
        client, path = self._client(tmp_path, monkeypatch, calls)
        asyncio.run(client._discard_pi_mcp_handoff())
        asyncio.run(client._discard_pi_mcp_handoff())
        assert calls == [("discard", path)]
        assert client._pi_mcp_handoff == ("", b"")

    def test_a_reset_removes_a_file_no_session_consumed(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        leftover = tmp_path / "handoff.json"
        leftover.write_text("{}", encoding="utf-8")
        client._pi_mcp_handoff = (str(leftover), b"{}")
        client._reset_state()
        assert not leftover.exists()
        assert client._pi_mcp_handoff == ("", b"")

    def test_the_session_array_composes_for_pi(self, tmp_path):
        assert ACP_BACKEND_PI in ACP_BACKENDS_SESSION_MCP_ARRAY
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        client._session_mcp_cache = [STDIO]
        assert client._pi_session_mcp_servers() == [STDIO]


class TestThePiToolTitle:
    """pi spells a server's tools ``mcp__<server with - as _>__<tool>``."""

    def test_a_handed_off_servers_tool_is_put_back_in_its_own_spelling(self) -> None:
        from kiro_crew.acp._dispatch import pi_mcp_title

        servers = ["kirocrew", "kirocrew-core"]
        assert pi_mcp_title("mcp__kirocrew_core__send_message", servers) == (
            "mcp__kirocrew-core__send_message"
        )
        assert pi_mcp_title("mcp__kirocrew__x", servers) == "mcp__kirocrew__x"
        assert pi_mcp_title("mcp__other__x", servers) == "mcp__other__x"
        assert pi_mcp_title("read", servers) == "read"

    def test_the_client_names_every_handed_off_server(self, tmp_path) -> None:
        """The pooled stubs are not in the placed array on pi; the handoff names them."""
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        client._pi_mcp_server_names = ("kirocrew-core",)
        assert "kirocrew-core" in client._harness_mcp_server_names()
        client._reset_state()
        assert client._pi_mcp_server_names == ()

    def test_a_governance_rule_on_the_server_binds_to_the_pi_call(self) -> None:
        """The permission event's title is the server's spelling, so ``@kirocrew-core`` binds."""
        from kiro_crew.acp._dispatch import GATE_ENVELOPE_MARKER, build_permission_event
        from kiro_crew.acp.types import JsonRpcMessage

        message = json.dumps(
            {
                GATE_ENVELOPE_MARKER: 1,
                "nonce": NONCE,
                "toolCallId": "call_1",
                "tool": "mcp__kirocrew_core__send_message",
                "kind": "other",
                "input": {"text": "hi"},
            }
        )
        frame = JsonRpcMessage(
            id=0,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {
                    "toolCallId": "pi-ui-1",
                    "title": "mcp__kirocrew_core__send_message",
                    "kind": "other",
                    "status": "pending",
                    "rawInput": {
                        "method": "confirm",
                        "title": "mcp__kirocrew_core__send_message",
                        "message": message,
                    },
                },
                "options": [
                    {"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                    {"optionId": "no", "name": "No", "kind": "reject_once"},
                ],
            },
        )
        event, _ = build_permission_event(
            frame,
            gate_envelope_nonce=NONCE,
            harness_backend=ACP_BACKEND_PI,
            harness_mcp_servers=("kirocrew-core",),
        )
        assert event is not None
        assert event.title == "mcp__kirocrew-core__send_message"
        from kiro_crew.platform.governance import mcp_title_to_ref

        assert mcp_title_to_ref(event.title).startswith("@kirocrew-core/")


def test_the_floor_is_the_release_with_built_in_mcp() -> None:
    assert acp_client.PI_MIN_VERSION == (0, 99, 2)
