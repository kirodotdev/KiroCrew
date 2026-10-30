"""A pi member's saved spec is confirmed against the spec its session consumed.

pi has no mirror, so the consumed spec is the ``session_mcp_projection`` parse that
``AcpClient._mirrorless_member_mounts`` also builds the member mounts and the zero-tool
answer from. These drive the REAL resolver over a real spec on disk and the REAL
confirmation over what it recorded. The live round trip is
``test/fixtures/acp_frames/pi/saved-spec-projection-live.jsonl``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_member_mount_deepseek as deepseek_mount

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import agent_state
from kiro_crew.acp import client_mcp_array, client_start, session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_MEMBER_TOOL_LIMITS,
)
from kiro_crew.agent_capabilities import _digest, member_backend_gap
from kiro_crew.platform.governance_profiles import governance_answer_generation
from kiro_crew.session_capabilities import LoadedCapabilities, runtime_view

FIXTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "pi" / "saved-spec-projection-live.jsonl"
)

agents_dir = deepseek_mount.agents_dir
MEMBER_KEY = deepseek_mount.MEMBER_KEY

SAVED = {"name": "kirocrew", "description": "a saved member", "tools": ["*"]}


def _write(agents_dir: Path, spec: dict) -> dict:
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    return spec


def _pi_member(tmp_path: Path) -> AcpClient:
    client = deepseek_mount._client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_PI)
    client.member_context = True
    client._session_id = "pi-session-1"
    client._confirmed_capability_session = None
    client._loaded_capability_template = ""
    client._capability_projection_gaps = ()
    return client


def _saved_as(monkeypatch, spec: dict) -> None:
    """Stamp the saved intent the way a save does: the digest of the spec as saved."""
    intent = {"materialized": _digest(spec)}
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _template: intent)


def test_pi_clears_the_start_gate_but_not_the_tool_limits_one() -> None:
    assert ACP_BACKEND_PI in ACP_BACKENDS_MEMBER_CAPABILITIES
    assert member_backend_gap(ACP_BACKEND_PI, ()) is None
    # disabledTools is still kiro-cli's alone: such a spec is refused before spawn.
    assert ACP_BACKEND_PI not in ACP_BACKENDS_MEMBER_TOOL_LIMITS
    assert member_backend_gap(ACP_BACKEND_PI, ("docs",)) == "tool_limits_unsupported"


def test_the_consumed_spec_is_the_parse_the_mounts_came_from(
    agents_dir, tmp_path, monkeypatch
) -> None:
    """One parse: the recorded spec IS the projection's, and no second read happens."""
    _write(agents_dir, SAVED)
    real = session_mcp.session_mcp_projection
    seen = []

    def counting(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(session_mcp, "session_mcp_projection", counting)
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    assert len(seen) == 1
    assert client._session_agent_spec is seen[0].agent_spec
    assert client._spec_zero_tools is seen[0].zero_tools


def test_a_saved_spec_is_confirmed_on_pi(agents_dir, tmp_path, monkeypatch) -> None:
    spec = _write(agents_dir, SAVED)
    _saved_as(monkeypatch, spec)
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    asyncio.run(client.confirm_member_projection())
    assert client._loaded_capability_template == "kirocrew"
    assert client.capability_projection_gaps == ()
    assert client._confirmed_capability_session == "pi-session-1"


def test_a_spec_edited_after_its_save_is_refused_on_pi(agents_dir, tmp_path, monkeypatch) -> None:
    _saved_as(monkeypatch, _write(agents_dir, SAVED))
    _write(agents_dir, dict(SAVED, description="edited after it was saved"))
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        asyncio.run(client.confirm_member_projection())
    assert client._loaded_capability_template == ""


def test_an_unreadable_spec_on_a_re_resolve_drops_the_earlier_parse(
    agents_dir, tmp_path, monkeypatch
) -> None:
    """A failed re-read must not leave the previous generation standing as consumed."""
    _saved_as(monkeypatch, _write(agents_dir, SAVED))
    client = _pi_member(tmp_path)
    client._resolve_session_mcp_servers()
    assert client._session_agent_spec is not None

    def unreadable(*_a, **_k):
        raise OSError("spec unreadable")

    monkeypatch.setattr(session_mcp, "session_mcp_projection", unreadable)
    assert client._resolve_session_mcp_servers() == []
    assert client._session_agent_spec is None
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        asyncio.run(client.confirm_member_projection())


def test_kiro_records_no_consumed_spec(agents_dir, tmp_path) -> None:
    """kiro-cli reads the saved file itself through ``--agent``; this branch is not its."""
    _write(agents_dir, SAVED)
    client = deepseek_mount._client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_KIRO)
    marker = {"untouched": True}
    client._session_agent_spec = marker
    assert client._mirrorless_member_mounts(()) == []
    assert client._session_agent_spec is marker


# --- The pooled stubs a member session on pi receives pass the spec's ``tools``.


def _stub(name: str) -> dict:
    return {"name": name, "command": "/opt/stub", "args": [name]}


def _stub_names(client: AcpClient, *, member: bool) -> list[str]:
    client.member_context = member
    client._pooled_broker_stubs = lambda: [_stub("docs"), _stub("search")]
    client._session_mcp_cache = client._resolve_session_mcp_servers()
    return [s["name"] for s in client._pooled_mcp_servers()]


@pytest.mark.parametrize(
    ("tools", "expected"),
    [(["@docs"], ["docs"]), (["*"], ["docs", "search"]), ([], [])],
)
def test_a_member_on_pi_mounts_only_the_stubs_its_spec_references(
    agents_dir, tmp_path, tools, expected
) -> None:
    _write(agents_dir, dict(SAVED, tools=tools))
    client = _pi_member(tmp_path)
    assert _stub_names(client, member=True) == expected


def test_a_switched_off_server_is_not_mounted_as_a_stub_on_a_member(agents_dir, tmp_path) -> None:
    _write(agents_dir, dict(SAVED, mcpServers={"docs": {"command": "x", "disabled": True}}))
    client = _pi_member(tmp_path)
    assert _stub_names(client, member=True) == ["search"]


def test_a_member_spec_with_no_tools_key_mounts_no_stub(agents_dir, tmp_path) -> None:
    """A missing ``tools`` is an empty allowlist, as on a mirror: nothing referenced."""
    _write(agents_dir, {"name": "kirocrew", "description": "no tools key"})
    assert _stub_names(_pi_member(tmp_path), member=True) == []


def test_an_unreadable_member_spec_mounts_no_stub(agents_dir, tmp_path, monkeypatch) -> None:
    """Fail closed: no spec read means no reference, so no pooled stub reaches pi."""

    def unreadable(*_a, **_k):
        raise OSError("spec unreadable")

    monkeypatch.setattr(session_mcp, "session_mcp_projection", unreadable)
    assert _stub_names(_pi_member(tmp_path), member=True) == []


def test_a_missing_member_spec_mounts_no_stub(agents_dir, tmp_path) -> None:
    """No spec file: nothing referenced, so no stub (its confirmation refuses too)."""
    (agents_dir / "kirocrew.json").unlink()
    assert _stub_names(_pi_member(tmp_path), member=True) == []


def test_every_session_composition_reads_pi_s_array_before_its_pooled_stubs() -> None:
    """The allowlist the stubs read is recorded by the pi array's resolve.

    ``_pooled_mcp_servers`` reads only that field (H13), so each ``session/new`` /
    ``session/load`` splice must evaluate the pi array -- which warms the cache
    through ``_mirrorless_member_mounts`` -- before the pooled stubs.
    """
    source = (Path(client_start.__file__)).read_text(encoding="utf-8")
    pooled = [m.start() for m in re.finditer(r"self\._pooled_mcp_servers\b", source)]
    assert len(pooled) == 3
    for at in pooled:
        assert "self._pi_session_mcp_servers()" in source[max(0, at - 400) : at], at


def test_kiro_stub_composition_makes_no_projection_lookup(tmp_path, monkeypatch) -> None:
    """kiro-cli's pooled stubs gain no branch and no registry lookup (H13)."""
    client = deepseek_mount._client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_KIRO)
    client.member_context = True
    client._pooled_broker_stubs = lambda: [_stub("docs")]
    client._session_mcp_cache = None

    def no_lookup(*_a, **_k):
        raise AssertionError("projection_for reached from kiro's stub composition")

    monkeypatch.setattr(client_mcp_array, "projection_for", no_lookup)
    assert [s["name"] for s in client._pooled_mcp_servers()] == ["docs"]


def test_a_re_resolve_takes_the_edited_tools_list(agents_dir, tmp_path) -> None:
    _write(agents_dir, dict(SAVED, tools=["@docs"]))
    client = _pi_member(tmp_path)
    assert _stub_names(client, member=True) == ["docs"]
    _write(agents_dir, dict(SAVED, tools=["@search"]))
    assert _stub_names(client, member=True) == ["search"]


def test_a_member_stub_with_a_globally_switched_off_tool_is_withheld_whole(
    agents_dir, tmp_path
) -> None:
    """pi has no per-tool deny, so a narrowed pooled server is not mounted at all."""
    _write(agents_dir, dict(SAVED, tools=["*"]))
    (tmp_path / "settings-mcp.json").write_text(
        json.dumps({"mcpServers": {"docs": {"disabledTools": ["delete_page"]}}}),
        encoding="utf-8",
    )
    assert _stub_names(_pi_member(tmp_path), member=True) == ["search"]


def test_a_non_member_pi_session_keeps_every_stub(agents_dir, tmp_path) -> None:
    _write(agents_dir, dict(SAVED, tools=["@docs"]))
    client = deepseek_mount._client(tmp_path, "dashboard:plain", backend=ACP_BACKEND_PI)
    assert _stub_names(client, member=False) == ["docs", "search"]


def test_kiro_stubs_are_never_filtered(agents_dir, tmp_path) -> None:
    """kiro-cli applies ``tools`` itself; its stub append gains no filter (H13)."""
    _write(agents_dir, dict(SAVED, tools=["@docs"]))
    client = deepseek_mount._client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_KIRO)
    client.member_context = True
    client._pooled_broker_stubs = lambda: [_stub("docs"), _stub("search")]
    client._session_mcp_cache = None
    assert [s["name"] for s in client._pooled_mcp_servers()] == ["docs", "search"]
    assert getattr(client, "_member_stub_allowlist", None) is None


def test_the_allowlist_is_dropped_with_the_session(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_PI)
    client._member_stub_allowlist = session_mcp._tools_allowlist({"tools": ["@docs"]})
    client._reset_state()
    assert client._member_stub_allowlist is None


def _view(provider, mcp_servers: tuple[str, ...]) -> dict:
    stamp = LoadedCapabilities(
        "A",
        "kirocrew",
        "rev-1",
        "proc-1",
        "pi-session-1",
        governance_answer_generation(),
        mcp_servers,
    )
    session = SimpleNamespace(
        capability_member="A",
        loaded_capabilities=stamp,
        provider=provider,
        semaphore=asyncio.Lock(),
    )
    state = SimpleNamespace(sessions={"dashboard:A": session}, capability_failures={})
    return runtime_view(state, "A", "rev-1")


def _confirmed_pi_provider() -> SimpleNamespace:
    # What a confirmed pi session reports: pi sends no MCP registration frames, so its
    # report is the empty one an AcpClient starts with.
    return SimpleNamespace(
        is_process_alive=lambda: True,
        process_instance="proc-1",
        session_id="pi-session-1",
        loaded_capability_template="kirocrew",
        capability_projection_gaps=(),
        mcp_session_report=McpSessionReport,
    )


def test_a_declared_server_that_did_not_reach_pi_keeps_the_pane_unverified() -> None:
    """The ``BROKER_ONLY`` gap: confirming the spec does not claim its servers run."""
    view = _view(_confirmed_pi_provider(), ("spec-probe",))
    assert view["status"] == "unverified"
    assert view["sessions"][0]["error_code"] == "capability_mcp_unreported"


def test_a_confirmed_spec_with_no_servers_reads_applied_on_pi() -> None:
    assert _view(_confirmed_pi_provider(), ())["status"] == "applied"


def _frames() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def test_the_live_capture_confirms_a_saved_spec_and_runs_its_server() -> None:
    header, *frames = _frames()
    meta = header["_meta"]
    assert meta["recorded"] == "live" and meta["backend"] == ACP_BACKEND_PI
    assert meta["agent_version"] == "0.0.34 (pi 1.3.0)"
    for fact in ("consumed_spec_matches was true", "capability_runtime_unverified"):
        assert fact in meta["note"]
    updates = [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update" and isinstance(f["params"].get("update"), dict)
    ]
    call = next(u for u in updates if u.get("title") == "mcp__probe_server__echo")
    asks = [f for f in frames if f.get("method") == "session/request_permission"]
    assert len(asks) == 1
    envelope = json.loads(asks[0]["params"]["toolCall"]["rawInput"]["message"])
    assert envelope["nonce"] == meta["gate_envelope_nonce"]
    assert envelope["toolCallId"] == call["toolCallId"]
    done = [
        u
        for u in updates
        if u.get("toolCallId") == call["toolCallId"] and u.get("status") == "completed"
    ]
    assert done and done[-1]["rawOutput"]["details"]["server"] == "probe-server"
    assert "ECHO:ping" in json.dumps(done[-1]["content"])
