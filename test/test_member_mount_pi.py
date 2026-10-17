"""A pi member DM session holds the member session-control tools.

pi has no mirror, so its array is composed by ``AcpClient._mirrorless_member_mounts``,
and pi-acp drops the wire array, so the mount reaches pi through the gate extension's
handoff file (``acp.harness.pi.pi_mcp_handoff_payload``). These drive the REAL
resolver over a real agent spec, then the real payload builder over its output. The
mount itself is pinned by the committed round trip in
``test/fixtures/acp_frames/pi/mcp-stdio-mount-live.jsonl``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import test_member_mount_deepseek as deepseek_mount

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import acp_tool_gate
from kiro_crew.acp.harness.pi import pi_mcp_handoff_payload
from kiro_crew.acp.types import (
    ACP_BACKEND_PI,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
)
from kiro_crew.agent_sdk import backend_cards
from kiro_crew.members import MEMBER_DISPATCH_SERVER

FIXTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "pi"

# The deepseek module's spec-on-disk fixture and minimal client: the resolver under
# test is the same mirror-less branch.
agents_dir = deepseek_mount.agents_dir
MEMBER_KEY = deepseek_mount.MEMBER_KEY
_client = deepseek_mount._client
_names = deepseek_mount._names


def test_pi_holds_both_things_membership_needs() -> None:
    """The mount it rides, and the gate that makes the mount safe, for THIS harness."""
    assert ACP_BACKEND_PI in ACP_BACKENDS_MEMBER_DISPATCH
    assert ACP_BACKEND_PI in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert acp_tool_gate.is_enforced(ACP_BACKEND_PI) is True
    # The panel is its own decision (H6), and no panel mount has been driven on pi.
    assert ACP_BACKEND_PI not in ACP_BACKENDS_MEMBER_PANEL


def test_the_round_trip_the_rationale_names_is_committed() -> None:
    body = (FIXTURES / "mcp-stdio-mount-live.jsonl").read_text(encoding="utf-8")
    assert "tools/call" in body
    assert "mcp__probe_server__echo" in body


def test_the_card_reads_member_threads_available() -> None:
    card = backend_cards.card_for(ACP_BACKEND_PI)
    line = next(x for x in card.capabilities if x.id == backend_cards.LINE_MEMBER_THREAD_TOOLS)
    assert line.available is True


def test_the_member_mount_reaches_the_handoff_with_its_identity(agents_dir, tmp_path) -> None:
    """The element the resolver builds is the entry the extension registers, env intact."""
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    out = client._resolve_session_mcp_servers()
    assert _names(out) == [MEMBER_DISPATCH_SERVER]
    assert client.member_dispatch_mounted is True
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce, key = "a" * 32, bytes(range(32))
    payload, names = pi_mcp_handoff_payload(out, nonce, key)
    assert names == (MEMBER_DISPATCH_SERVER,)
    # The member's identity is in the file only under the seal.
    assert MEMBER_KEY.encode() not in payload
    body = json.loads(payload)
    plain = AESGCM(key).decrypt(
        base64.b64decode(body["iv"]), base64.b64decode(body["sealed"]), nonce.encode()
    )
    entry = json.loads(plain)["servers"][MEMBER_DISPATCH_SERVER]
    assert entry["env"]["KIROCREW_SESSION_KEY"] == MEMBER_KEY
    assert entry["exposure"] == "direct"


def test_an_ordinary_pi_session_carries_nothing(agents_dir, tmp_path) -> None:
    client = _client(tmp_path, "dashboard_abc123", backend=ACP_BACKEND_PI)
    assert client._resolve_session_mcp_servers() == []
    assert client.member_dispatch_mounted is False
