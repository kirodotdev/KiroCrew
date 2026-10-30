"""pi honours an agent spec's ``"tools": []`` as a total ban.

pi has no mirror, so the answer is read beside its member mounts from the one spec
parse (``AcpClient._mirrorless_member_mounts``), and the refusal is the shared one,
``AcpClient._deny_zero_tools``: Crew's gate extension turns every pi tool call into a
``session/request_permission``, and on a zero-tool spec the client answers each with
the advertised reject option before any consumer sees it.

The permission frames below are REPLAYED from
``test/fixtures/acp_frames/pi/zero-tools-refused-live.jsonl``, recorded while a real
pi-acp 0.0.34 over pi 1.3.0 ran under ``AcpClient`` on a zero-tool spec.
"""

from __future__ import annotations

import copy
import json
import types
from pathlib import Path

import pytest
import test_member_mount_deepseek as deepseek_mount

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HONOR_ZERO_TOOL_BAN
from kiro_crew.agent_sdk.capabilities import capabilities_for

FIXTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "pi" / "zero-tools-refused-live.jsonl"

agents_dir = deepseek_mount.agents_dir
_client = deepseek_mount._client


def _write_spec(agents_dir: Path, name: str, body: dict) -> None:
    (agents_dir / f"{name}.json").write_text(json.dumps({"name": name, **body}), encoding="utf-8")


def _capture() -> tuple[dict, list[dict]]:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["_meta"], [json.loads(x) for x in lines[1:]]


def _permission_frames() -> list[dict]:
    return [f for f in _capture()[1] if f.get("method") == "session/request_permission"]


def _envelope(frame: dict) -> dict:
    return json.loads(frame["params"]["toolCall"]["rawInput"]["message"])


def _with_envelope(frame: dict, **changes) -> dict:
    """The same live dialog frame with its gate envelope edited (still nonce-bound)."""
    out = copy.deepcopy(frame)
    raw = out["params"]["toolCall"]["rawInput"]
    env = json.loads(raw["message"])
    env.update(changes)
    raw["message"] = json.dumps(env)
    return out


def _msg(frame: dict) -> JsonRpcMessage:
    return JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])


def _reject_option(frame: dict) -> str:
    return next(o["optionId"] for o in frame["params"]["options"] if o["kind"].startswith("reject"))


@pytest.fixture
def quiet_sel(monkeypatch):
    audited: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            audited.append(kw)

    monkeypatch.setattr(acp_client, "sel_module", types.SimpleNamespace(sel=lambda: _Sel()))
    return audited


def _pi_client(*, zero_tools: bool) -> tuple[AcpClient, list]:
    meta, _frames = _capture()
    client = AcpClient(acp_backend=ACP_BACKEND_PI)
    client._pi_gate_nonce = meta["gate_envelope_nonce"]
    client._spec_zero_tools = zero_tools
    sent: list = []

    async def _send(request_id, payload):
        sent.append((request_id, payload))

    client._send_response = _send  # type: ignore[method-assign]
    return client, sent


# ── The answer: read from the spec parse beside the member mounts ──


class TestTheSpecParse:
    def test_pi_is_a_mirror_less_array_member(self):
        from kiro_crew.providers.mirrors import MIRRORS

        assert ACP_BACKEND_PI not in MIRRORS
        assert ACP_BACKEND_PI in ACP_BACKENDS_SESSION_MCP_ARRAY
        assert ACP_BACKEND_PI in ACP_BACKENDS_HONOR_ZERO_TOOL_BAN
        assert capabilities_for(ACP_BACKEND_PI).honors_zero_tool_ban is True

    @pytest.mark.parametrize("session_key", ["dashboard_abc123", deepseek_mount.MEMBER_KEY])
    def test_a_zero_tool_spec_arms_the_refusal(self, agents_dir, tmp_path, session_key):
        _write_spec(agents_dir, "pi-zero", {"tools": [], "mcpServers": {}})
        client = _client(tmp_path, session_key, backend=ACP_BACKEND_PI)
        client._agent = "pi-zero"
        client._spec_denied_tools = frozenset()
        client._resolve_session_mcp_servers()
        assert client.spec_zero_tools is True
        assert client._enforces_zero_tool_ban is True
        assert client._judges_permission_requests is True

    @pytest.mark.parametrize(
        "body",
        [
            {"mcpServers": {}},  # no tools key: no restriction stated
            {"tools": ["@kirocrew-core"], "mcpServers": {}},
            {"tools": "none", "mcpServers": {}},  # not a list: not a ban
        ],
    )
    def test_an_ordinary_spec_leaves_it_off(self, agents_dir, tmp_path, body):
        _write_spec(agents_dir, "pi-normal", body)
        client = _client(tmp_path, "dashboard_abc123", backend=ACP_BACKEND_PI)
        client._agent = "pi-normal"
        assert client._resolve_session_mcp_servers() == []
        assert client.spec_zero_tools is False
        assert client._enforces_zero_tool_ban is False

    def test_an_unreadable_spec_leaves_it_off(self, agents_dir, tmp_path, monkeypatch):
        from kiro_crew.acp import session_mcp

        def _boom(*_a, **_k):
            raise OSError("unreadable")

        monkeypatch.setattr(session_mcp, "session_mcp_projection", _boom)
        client = _client(tmp_path, "dashboard_abc123", backend=ACP_BACKEND_PI)
        client._spec_zero_tools = True  # an earlier resolution on the same client
        assert client._resolve_session_mcp_servers() == []
        assert client.spec_zero_tools is False

    def test_a_respawn_on_an_edited_spec_reads_it_again(self, agents_dir, tmp_path):
        _write_spec(agents_dir, "pi-zero", {"tools": [], "mcpServers": {}})
        client = _client(tmp_path, "dashboard_abc123", backend=ACP_BACKEND_PI)
        client._agent = "pi-zero"
        client._resolve_session_mcp_servers()
        assert client.spec_zero_tools is True
        _write_spec(agents_dir, "pi-zero", {"mcpServers": {}})
        client._resolve_session_mcp_servers()
        assert client.spec_zero_tools is False

    def test_kiro_never_records_it(self, agents_dir, tmp_path):
        """kiro-cli reads its spec through --agent and refuses natively: untouched."""
        _write_spec(agents_dir, "pi-zero", {"tools": [], "mcpServers": {}})
        client = _client(tmp_path, "dashboard_abc123", backend=ACP_BACKEND_KIRO)
        client._agent = "pi-zero"
        client._spec_zero_tools = False
        client._mirrorless_member_mounts(frozenset())
        assert client._spec_zero_tools is False


# ── The refusal: every live pi permission frame, and its nearby inputs ──


class TestTheReplayedRefusal:
    def test_the_capture_holds_write_bash_and_mcp(self):
        tools = [_envelope(f)["tool"] for f in _permission_frames()]
        assert tools == ["write", "bash", "mcp__probe_server__echo"]
        body = FIXTURE.read_text(encoding="utf-8")
        # The tool pi does not have never reached a permission frame: pi failed it.
        assert "Tool frobnicate not found" in body
        assert "Denied by the Kiro Crew tool gate" in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", [0, 1, 2], ids=["write", "bash", "mcp"])
    async def test_each_live_call_is_refused_without_asking(self, quiet_sel, index):
        frame = _permission_frames()[index]
        client, sent = _pi_client(zero_tools=True)
        event = client._build_permission_event(_msg(frame))
        assert event is not None and event.title == _envelope(frame)["tool"]
        # The predicate both answering sites run first: True means the consumer
        # (the user's approval card) is never handed this request.
        assert await client._deny_zero_tools(event) is True
        assert sent == [
            (frame["id"], {"outcome": {"outcome": "selected", "optionId": _reject_option(frame)}})
        ]
        assert quiet_sel[-1]["outcome"] == "denied"
        assert quiet_sel[-1]["metadata"] == {"reason": "spec_zero_tools", "backend": "pi"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", [0, 1, 2], ids=["write", "bash", "mcp"])
    async def test_the_auto_approve_site_refuses_too(self, quiet_sel, index):
        """A session with no consumer (the knowledge pool) must not approve it."""
        frame = _permission_frames()[index]
        client, sent = _pi_client(zero_tools=True)
        await client._handle_permission(_msg(frame))
        assert len(sent) == 1
        assert sent[0][1]["outcome"]["optionId"] == _reject_option(frame)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "changes",
        [
            {"tool": "frobnicate", "kind": "other", "input": {"target": "/work/proj/x"}},
            {"tool": "mcp__someone_elses_server__drop", "kind": "other", "input": {}},
            {"tool": "edit", "kind": "edit", "input": {"path": "/work/proj/a"}},
            {"tool": "read", "kind": "read", "input": {"path": "/work/proj/a"}},
            {"truncated": True},
        ],
        ids=["unknown-tool", "unknown-mcp-server", "edit", "read", "truncated-args"],
    )
    async def test_nearby_inputs_are_refused(self, quiet_sel, changes):
        frame = _with_envelope(_permission_frames()[1], **changes)
        client, sent = _pi_client(zero_tools=True)
        event = client._build_permission_event(_msg(frame))
        assert event is not None
        assert await client._deny_zero_tools(event) is True
        assert sent and sent[0][1]["outcome"]["optionId"] == _reject_option(frame)

    @pytest.mark.asyncio
    async def test_a_frame_with_a_foreign_nonce_is_refused_as_the_dialog_it_is(self, quiet_sel):
        """Not an envelope this session issued -- still a permission request, still refused."""
        frame = _with_envelope(_permission_frames()[1], nonce="0" * 32)
        client, sent = _pi_client(zero_tools=True)
        event = client._build_permission_event(_msg(frame))
        assert event is not None
        assert await client._deny_zero_tools(event) is True
        assert sent


class TestAnOrdinarySpecIsUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", [0, 1, 2], ids=["write", "bash", "mcp"])
    async def test_the_same_frames_reach_the_consumer(self, index):
        frame = _permission_frames()[index]
        client, sent = _pi_client(zero_tools=False)
        event = client._build_permission_event(_msg(frame))
        assert event is not None
        assert await client._deny_zero_tools(event) is False
        assert sent == []

    def test_judging_is_not_switched_on(self):
        client, _sent = _pi_client(zero_tools=False)
        assert client._judges_permission_requests is False
