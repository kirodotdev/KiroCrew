"""A pi member DM session holds the member session-control tools and its own panel.

pi has no mirror, so its array is composed by ``AcpClient._mirrorless_member_mounts``,
and pi-acp drops the wire array, so the mount reaches pi through the gate extension's
handoff file (``acp.harness.pi.pi_mcp_handoff_payload``). These drive the REAL
resolver over a real agent spec, then the real payload builder over its output. The
mounts themselves are pinned by the committed round trips in
``test/fixtures/acp_frames/pi/mcp-stdio-mount-live.jsonl`` and
``test/fixtures/acp_frames/pi/member-panel-mount-live.jsonl``.
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
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.members import MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER

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
    # The panel is its own decision (H6), held on the panel capture below.
    assert ACP_BACKEND_PI in ACP_BACKENDS_MEMBER_PANEL


def test_the_round_trip_the_rationale_names_is_committed() -> None:
    body = (FIXTURES / "mcp-stdio-mount-live.jsonl").read_text(encoding="utf-8")
    assert "tools/call" in body
    assert "mcp__probe_server__echo" in body


def _frames(name: str) -> list[dict]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _updates(frames: list[dict]) -> list[dict]:
    return [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update" and isinstance(f["params"].get("update"), dict)
    ]


def test_the_panel_is_mounted_gated_and_answered_on_a_live_capture() -> None:
    """A member thread on pi registers ``kirocrew-panel`` and its call reaches the server.

    The capture's array is the resolver's own output for a member key on pi; pi spells
    the panel tool ``mcp__kirocrew_panel__<tool>``, the call asks the host first through
    the gate extension's dialog, and the result is the panel server's own answer.
    """
    header, *frames = _frames("member-panel-mount-live.jsonl")
    meta = header["_meta"]
    assert meta["recorded"] == "live" and meta["backend"] == ACP_BACKEND_PI
    title = "mcp__kirocrew_panel__panel_publish"
    calls = [u for u in _updates(frames) if u.get("title") == title]
    assert calls and calls[0]["sessionUpdate"] == "tool_call"
    call_id = calls[0]["toolCallId"]
    asks = [f for f in frames if f.get("method") == "session/request_permission"]
    assert len(asks) == 1, "the panel call asks the host before it runs"
    envelope = json.loads(asks[0]["params"]["toolCall"]["rawInput"]["message"])
    assert envelope["tool"] == title and envelope["toolCallId"] == call_id
    assert envelope["nonce"] == meta["gate_envelope_nonce"]
    done = [
        u
        for u in _updates(frames)
        if u.get("toolCallId") == call_id and u.get("status") == "completed"
    ]
    assert done and done[-1]["rawOutput"]["details"]["server"] == MEMBER_PANEL_SERVER
    # The server's own answer: the identity the handoff carried reached the gateway,
    # which refused to vouch for a session it did not create (the capture's note).
    assert "identity_unattested" in json.dumps(done[-1]["content"])


def test_the_captured_array_is_what_the_pi_resolver_composes(agents_dir, tmp_path) -> None:
    """The two member mounts the capture handed off are the last two the resolver composes.

    The capture's array is the member mounts for an agent whose spec declared no
    extra servers; this test's ``agents_dir`` spec also declares ``kirocrew-core``,
    which the resolver carries ahead of the member mounts (the same server
    opencode/deepseek place from the same spec). So the member pair the capture
    names is the tail of the array, in the capture's order.
    """
    note = _frames("member-panel-mount-live.jsonl")[0]["_meta"]["note"]
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    names = _names(client._resolve_session_mcp_servers())
    assert names[-2:] == [MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER]
    for name in (MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER):
        assert name in note


def test_the_card_reads_member_threads_available() -> None:
    card = backend_cards.card_for(ACP_BACKEND_PI)
    line = next(x for x in card.capabilities if x.id == backend_cards.LINE_MEMBER_THREAD_TOOLS)
    assert line.available is True


def test_the_member_mounts_reach_the_handoff_with_their_identity(agents_dir, tmp_path) -> None:
    """Each element the resolver builds is an entry the extension registers, env intact.

    The ``agents_dir`` spec declares ``kirocrew-core`` too, so the array leads with
    it (the same server a mirrored harness carries from the same spec); the member
    dispatch and panel follow. Every control-plane entry -- the spec's own
    ``kirocrew-core`` and the two member mounts -- carries THIS session's identity,
    resolved live rather than read from the spec.
    """
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    out = client._resolve_session_mcp_servers()
    assert _names(out) == ["kirocrew-core", MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER]
    assert client.member_dispatch_mounted is True
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce, key = "a" * 32, bytes(range(32))
    payload, names = pi_mcp_handoff_payload(out, nonce, key)
    assert names == ("kirocrew-core", MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER)
    # The member's identity is in the file only under the seal.
    assert MEMBER_KEY.encode() not in payload
    body = json.loads(payload)
    plain = AESGCM(key).decrypt(
        base64.b64decode(body["iv"]), base64.b64decode(body["sealed"]), nonce.encode()
    )
    servers = json.loads(plain)["servers"]
    for name in names:
        assert servers[name]["env"]["KIROCREW_SESSION_KEY"] == MEMBER_KEY
        assert servers[name]["env"][STUB_SESSION_TOKEN_ENV] == client._stub_session_token
        assert servers[name]["exposure"] == "direct"


def test_an_ordinary_pi_session_carries_nothing(agents_dir, tmp_path) -> None:
    """A non-member pi session carries nothing: spec-server delivery is member-only.

    The spec servers reach pi only for a crewmate DM session (``member_context``);
    an ordinary pi session composes the empty array it always did, like every other
    mirror-less backend's ordinary session.
    """
    client = _client(tmp_path, "dashboard_abc123", backend=ACP_BACKEND_PI)
    assert client._resolve_session_mcp_servers() == []
    assert client.member_dispatch_mounted is False


def test_the_panel_switch_withholds_only_the_panel(agents_dir, tmp_path, monkeypatch) -> None:
    """``agent.crew_panel: false`` reaches pi: session control stays, the panel goes."""
    import kiro_crew.members as members_mod

    monkeypatch.setattr(members_mod, "crew_panel_enabled", lambda: False)
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    # The spec's kirocrew-core leads; session control stays, the panel goes.
    assert _names(client._resolve_session_mcp_servers()) == [
        "kirocrew-core",
        MEMBER_DISPATCH_SERVER,
    ]


def test_a_disabled_panel_server_is_not_mounted(agents_dir, tmp_path) -> None:
    (tmp_path / "settings-mcp.json").write_text(
        json.dumps({"mcpServers": {MEMBER_PANEL_SERVER: {"disabled": True}}}),
        encoding="utf-8",
    )
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    assert _names(client._resolve_session_mcp_servers()) == [
        "kirocrew-core",
        MEMBER_DISPATCH_SERVER,
    ]


def test_a_switched_off_panel_tool_withholds_the_whole_panel(agents_dir, tmp_path) -> None:
    """No mirror, so no per-tool deny channel: a narrowed panel is not mounted at all."""
    (tmp_path / "settings-mcp.json").write_text(
        json.dumps({"mcpServers": {MEMBER_PANEL_SERVER: {"disabledTools": ["dashboard_apply"]}}}),
        encoding="utf-8",
    )
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    assert _names(client._resolve_session_mcp_servers()) == [
        "kirocrew-core",
        MEMBER_DISPATCH_SERVER,
    ]


# --- Saved-spec verification: pi is a member (``test_pi_saved_spec.py``). The spec's
# OWN servers reach pi through the handoff too, the same set a mirrored harness gets.


def _spec_with_own_server(agents_dir) -> dict:
    spec = {
        "name": "kirocrew",
        "tools": ["*"],
        "mcpServers": {"spec-probe": {"command": "python3", "args": ["probe_server.py"]}},
    }
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    return spec


def test_a_spec_with_its_own_mcp_server_reaches_pi(agents_dir, tmp_path) -> None:
    """The spec's own server is present in what reaches pi, beside the member mounts.

    pi has no mirror, but ``_mirrorless_member_mounts`` emits the spec's translated
    servers for this BROKER_ONLY member host -- the same server a mirrored harness
    carries from the same spec (``test_pi_saved_spec.py`` covers the saved-member
    confirmation).
    """
    _spec_with_own_server(agents_dir)
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    names = _names(client._resolve_session_mcp_servers())
    assert "spec-probe" in names
    # tools ["*"] grants the control plane too; the member mounts ride at the tail.
    assert names == [
        "kirocrew-core",
        "kirocrew-cron",
        "spec-probe",
        MEMBER_DISPATCH_SERVER,
        MEMBER_PANEL_SERVER,
    ]


# --- Per-tool narrowing withholds the whole server on pi. pi has no per-call deny
# channel, and the mirror-less path fills no `_spec_denied_tools`, so a narrowed
# spec server handed to pi would make the switched-off tool reachable with nothing
# to refuse the call. The whole server is withheld, the control plane included.


def test_a_spec_server_with_a_switched_off_tool_is_withheld(agents_dir, tmp_path) -> None:
    """A spec server narrowed by `disabledTools` is not mounted at all on pi."""
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": ["@foo", "@kirocrew-core"],
                "mcpServers": {
                    "foo": {"command": "/bin/foo", "disabledTools": ["dangerous"]},
                    "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                },
            }
        ),
        encoding="utf-8",
    )
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    names = _names(client._resolve_session_mcp_servers())
    assert "foo" not in names
    # kirocrew-core is un-narrowed, so it still reaches pi.
    assert "kirocrew-core" in names


def test_kirocrew_core_with_a_switched_off_tool_is_withheld(agents_dir, tmp_path) -> None:
    """The control plane is NOT exempt: a narrowed kirocrew-core is withheld too.

    pi has no per-call deny, so unlike a mirrored backend it cannot refuse the
    switched-off tool while keeping the server; the whole server is withheld.
    """
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": ["@foo", "@kirocrew-core"],
                "mcpServers": {
                    "foo": {"command": "/bin/foo"},
                    "kirocrew-core": {
                        "command": "/opt/kirocrew",
                        "args": ["mcp-core"],
                        "disabledTools": ["learn_add"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    names = _names(client._resolve_session_mcp_servers())
    assert "kirocrew-core" not in names
    # foo is un-narrowed, so it still reaches pi.
    assert "foo" in names


def test_a_settings_file_tool_off_withholds_the_whole_server(agents_dir, tmp_path) -> None:
    """A tool switched off in the global settings file withholds the server on pi.

    The dashboard writes a per-tool switch-off into the global MCP settings file;
    on pi, with no per-call deny, the whole named server is withheld.
    """
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": ["@foo", "@kirocrew-core"],
                "mcpServers": {
                    "foo": {"command": "/bin/foo"},
                    "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "settings-mcp.json").write_text(
        json.dumps({"mcpServers": {"foo": {"disabledTools": ["dangerous"]}}}),
        encoding="utf-8",
    )
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    names = _names(client._resolve_session_mcp_servers())
    assert "foo" not in names
    assert "kirocrew-core" in names


def test_an_un_narrowed_spec_server_still_mounts(agents_dir, tmp_path) -> None:
    """A spec server with no switched-off tool reaches pi as before."""
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": ["@foo", "@kirocrew-core"],
                "mcpServers": {
                    "foo": {"command": "/bin/foo", "args": ["serve"]},
                    "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                },
            }
        ),
        encoding="utf-8",
    )
    client = _client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    names = _names(client._resolve_session_mcp_servers())
    assert "foo" in names
    assert "kirocrew-core" in names
