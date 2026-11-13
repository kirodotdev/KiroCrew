"""The Strands mirror composes the session's MCP set, and none of it rides the wire.

The Strands CLI's Python-project ACP mode refuses a non-empty ``session/new`` /
``session/load`` ``mcpServers`` array, so the composition goes to the ``--mcp-config``
launch file instead (``acp/harness/strands.py``). These drive the REAL resolver over
a real agent spec and read what comes out: the array the launch file is written from,
and the wire composition, which must stay empty for this harness.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_STRANDS, ACP_BACKENDS_SESSION_MCP_ARRAY
from kiro_crew.providers.mirrors import MIRRORS, PROJECTIONS, ProjectionKind, mirror_for
from kiro_crew.providers.mirrors.base import Concern, Disposition
from kiro_crew.providers.mirrors.registry import PerToolDeny
from kiro_crew.providers.mirrors.strands import StrandsMirror

CONTROL_PLANE_SERVER = "kirocrew-core"
THIRD_PARTY_SERVER = "notes"


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """An agent spec on disk declaring Crew's control plane and one third-party server."""
    import kiro_crew.agent as agent_mod
    from kiro_crew.acp import session_mcp

    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    (d / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {
                    CONTROL_PLANE_SERVER: {"command": "/opt/kirocrew", "args": ["mcp-core"]},
                    THIRD_PARTY_SERVER: {"command": "/opt/notes", "args": ["serve"]},
                },
                "tools": ["@" + CONTROL_PLANE_SERVER, "@" + THIRD_PARTY_SERVER],
            }
        ),
        encoding="utf-8",
    )
    return d


def _client(tmp_path: Path) -> AcpClient:
    """A minimally constructed ``AcpClient``: only what the resolver reads."""
    client = object.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_STRANDS
    client._mcp_gateway_overlay = None  # shared gateway off: no broker stubs
    client._agent = "kirocrew"
    client._work_dir = tmp_path / "work"
    client._session_key = "dashboard_abc123"
    client._channel_id = "dashboard"
    client._stub_session_token = "9" * 64
    client._claude_settings_authored = False
    client._session_mcp_cache = None
    return client


def _names(out: list[dict]) -> list[str]:
    return [e["name"] for e in out]


def test_strands_is_declared_as_a_whole_server_mirror() -> None:
    assert MIRRORS[ACP_BACKEND_STRANDS] is StrandsMirror
    declared = PROJECTIONS[ACP_BACKEND_STRANDS]
    assert declared.kind is ProjectionKind.MIRROR
    assert declared.per_tool_deny is PerToolDeny.WHOLE_SERVER
    assert isinstance(mirror_for(ACP_BACKEND_STRANDS), StrandsMirror)


def test_every_concern_has_a_ruling_and_the_servers_are_delivered() -> None:
    rulings = StrandsMirror().rulings()
    assert set(rulings) == set(Concern)
    # The projected array is delivered whole, by the --mcp-config launch file rather
    # than the wire.
    assert rulings[Concern.MCP_SERVERS].disposition is Disposition.DELIVERED
    # The prompt rides the [AGENT SYSTEM PROMPT] block, as on every mirror; the _meta
    # agent definition would replace the project's own prompt and send it twice.
    assert rulings[Concern.PROMPT].disposition is Disposition.WITHHELD


def test_the_launch_composition_carries_the_spec_servers(agents_dir, tmp_path) -> None:
    assert ACP_BACKEND_STRANDS in ACP_BACKENDS_SESSION_MCP_ARRAY
    client = _client(tmp_path)
    assert _names(client._session_mcp_servers()) == [CONTROL_PLANE_SERVER, THIRD_PARTY_SERVER]


def test_a_switched_off_tool_withholds_its_whole_server(agents_dir, tmp_path) -> None:
    """No per-call deny path is verified here, so the narrowed server stays off."""
    (tmp_path / "settings-mcp.json").write_text(
        json.dumps({"mcpServers": {THIRD_PARTY_SERVER: {"disabledTools": ["delete"]}}}),
        encoding="utf-8",
    )
    client = _client(tmp_path)
    assert _names(client._resolve_session_mcp_servers()) == [CONTROL_PLANE_SERVER]
    assert THIRD_PARTY_SERVER in client._session_mcp_unhonoured


def test_pooled_stubs_never_reach_the_wire(agents_dir, tmp_path, monkeypatch) -> None:
    """The mirror places the stubs itself; the shared append stays empty for it."""
    client = _client(tmp_path)
    monkeypatch.setattr(
        AcpClient, "_pooled_broker_stubs", lambda self: [{"name": CONTROL_PLANE_SERVER}]
    )
    assert client._pooled_mcp_servers() == []


def test_no_strands_seam_is_spliced_onto_the_wire() -> None:
    """The session/new and session/load arrays carry nothing for this harness.

    The CLI refuses a non-empty array in project mode, so a strands splice in the
    wire composition would fail every session. The only splices are the declared
    per-harness seams, each gated on its own harness.
    """
    splice = re.compile(r"self\._(?P<h>[a-z]+)_session_mcp_servers\(\)")
    for fn in (
        acp_client.AcpClient._new_session_following_substitution,
        acp_client.AcpClient._initialize_session,
    ):
        hooks = {m.group("h") for m in splice.finditer(inspect.getsource(fn))}
        assert "strands" not in hooks
        assert "self._session_mcp_servers()" not in inspect.getsource(fn)


def test_the_report_and_ref_guard_read_the_launch_composition(agents_dir, tmp_path) -> None:
    """The wire array is empty for Strands, so judging refs against it would call
    every ``@server`` the launch file mounts unconfigured. The roster the session
    report and the ref guard read is the launch composition instead."""
    from kiro_crew.acp.client_start import _roster_of
    from kiro_crew.agent_sdk.mcp_refs import unresolved_server_refs

    spec = json.loads((agents_dir / "kirocrew.json").read_text(encoding="utf-8"))
    client = _client(tmp_path)
    # Before a Strands spawn sets it, the wire array is the roster, as on every harness.
    client._launch_mcp_servers = None
    assert _roster_of(client, []) == []
    assert unresolved_server_refs(spec, [], backend=ACP_BACKEND_STRANDS) == [
        "@" + CONTROL_PLANE_SERVER,
        "@" + THIRD_PARTY_SERVER,
    ]
    client._launch_mcp_servers = client._session_mcp_servers()
    roster = _roster_of(client, [])
    assert _names(roster) == [CONTROL_PLANE_SERVER, THIRD_PARTY_SERVER]
    assert unresolved_server_refs(spec, roster, backend=ACP_BACKEND_STRANDS) == []


def test_the_strands_spawn_records_its_launch_composition() -> None:
    """The adapter hands the pruned launch array to the client for that reading."""
    from kiro_crew.acp.harness import strands as strands_mod

    source = inspect.getsource(strands_mod.StrandsLaunch.resolve_spawn)
    assert "session._launch_mcp_servers = list(kept)" in source
