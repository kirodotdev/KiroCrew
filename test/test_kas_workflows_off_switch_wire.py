"""With Kiro Workflows off, an ordinary Kiro Agent chat's frames are main's, byte for byte.

``session/new`` carries no workflow key, nor does the ``session/load`` of a session Crew
never drove with workflows on. Only the load of a session Crew last started or loaded
with the surface on says ``workflows.enabled:false``: KAS resolves
``enabled ?? persisted ?? false`` there, so only that load can carry a persisted surface.

Drives the real ``create_session`` / ``load_session`` against the fake subprocess of
``test_acp_harness_wire_parity``, whose golden (``fixtures/acp_harness_wire/kas.json``,
unchanged by this PR) is main's frame.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_acp_harness_wire_parity import (  # noqa: F401 - the fixture is used by name
    _AGENT,
    _CWD,
    _KAS_AGENTS,
    _MCP_ROSTER,
    _MODES,
    _RESUME_SID,
    _SESSION_FILE,
    _SESSION_TOKEN,
    GOLDEN_DIR,
    _fast_no_report_ceiling,
    _runtime,
)

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
    JsonRpcMessage,
)
from kiro_crew.kas_workflows import chat as kas_chat


@pytest.fixture(autouse=True)
def _no_workflow_sessions_known(_floor_monkeypatch):
    # Which KAS sessions were driven with workflows on is process state; each test
    # starts with none known and leaves none behind.
    _floor_monkeypatch.setattr(kas_chat, "_WORKFLOW_SESSIONS", {}, raising=False)


async def _session_new(backend: str, monkeypatch: pytest.MonkeyPatch) -> dict:
    return (await _frames(backend, monkeypatch))[0]


async def _frames(backend: str, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """A switched-off dashboard chat's ``session/new``, then a ``session/load``."""
    from pathlib import PurePosixPath

    from kiro_crew.acp.harness import SessionExtras

    rt = _runtime(backend)
    sent: list[dict] = []

    async def _fake_send(method, params, timeout=None):
        sent.append({"method": method, "params": params})
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "pinned-new-sid", "modes": _MODES}
        if method == METHOD_SESSION_LOAD:
            return {"modes": _MODES}
        if method == METHOD_SET_MODE and backend == ACP_BACKEND_KAS:
            # KAS reports its servers ready after the mode switch, as in the wire gate.
            sid = params["sessionId"]
            names = [entry["name"] for entry in _MCP_ROSTER]
            origin = {"kiro": {"resource": {"source": {"origin": "client"}}}}
            status = [{"name": n, "status": "connected", "_meta": origin} for n in names]
            tags = [{"source": "mcp", "tag": f"@{n}/tool"} for n in names]
            for method_name, payload in (
                ("_kiro/mcp/status", {"servers": status}),
                ("_kiro/tools/didChange", {"tags": tags}),
            ):
                rt._session_queues[sid].put_nowait(
                    JsonRpcMessage(method=method_name, params={"sessionId": sid, **payload})
                )
        return {}

    async def _fake_work_dir(cwd=None):
        return PurePosixPath(_CWD)

    async def _fake_kas_agents(
        agent, *, member_dispatch=False, crew_panel=False, session_key="", session_cwd=None
    ):
        return SessionExtras(custom_agents=_KAS_AGENTS if backend == ACP_BACKEND_KAS else None)

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_session_work_dir", _fake_work_dir)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_kas_agents)
    monkeypatch.setattr(runtime_mod, "mint_stub_session_token", lambda: "pinned-token")
    monkeypatch.setattr(runtime_mod, "publish_session_token", lambda token, key: None)
    monkeypatch.setattr(kas_chat, "chat_workflows_enabled", lambda: False)
    monkeypatch.setattr(
        runtime_mod, "pooled_session_servers", lambda overlay, agent, **_kw: _MCP_ROSTER
    )
    # The golden's own token, so the frames compare whole.
    monkeypatch.setattr(runtime_mod, "mint_stub_session_token", lambda: _SESSION_TOKEN)

    handle = await rt.create_session(
        cwd=_CWD, agent=_AGENT, mcp_servers=_MCP_ROSTER, session_key="dashboard:chat-7"
    )
    await handle.destroy()
    loaded = await rt.load_session(
        session_file=_SESSION_FILE,
        resume_sid=_RESUME_SID,
        cwd=_CWD,
        agent=_AGENT,
        session_key="dashboard:chat-7",
    )
    await loaded.destroy()
    (new,) = [r["params"] for r in sent if r["method"] == METHOD_SESSION_NEW]
    (load,) = [r["params"] for r in sent if r["method"] == METHOD_SESSION_LOAD]
    return new, load


def _settings(params: dict):
    return ((params.get("_meta") or {}).get("kiro") or {}).get("settings")


def _workflow_key(params: dict):
    return (_settings(params) or {}).get("workflows")


@pytest.mark.asyncio
async def test_a_kas_chat_with_the_switch_off_starts_with_no_workflow_key(monkeypatch):
    params = await _session_new(ACP_BACKEND_KAS, monkeypatch)
    assert _workflow_key(params) is None
    assert "workflows" not in json.dumps(params)


@pytest.mark.asyncio
async def test_a_never_workflows_session_loads_with_no_workflow_key(monkeypatch):
    _new, load = await _frames(ACP_BACKEND_KAS, monkeypatch)
    assert "workflows" not in json.dumps(load)


@pytest.mark.asyncio
async def test_a_switched_off_kas_chats_frames_are_mains_byte_for_byte(monkeypatch):
    # The golden is main's capture (this PR leaves it unchanged); a switched-off
    # dashboard chat's session/new and session/load must serialize to the same bytes.
    golden = json.loads(Path(GOLDEN_DIR, "kas.json").read_text(encoding="utf-8"))
    mains = {r["method"]: r["params"] for r in golden["requests"]}
    new, load = await _frames(ACP_BACKEND_KAS, monkeypatch)
    for method, params in ((METHOD_SESSION_NEW, new), (METHOD_SESSION_LOAD, load)):
        assert json.dumps(params, sort_keys=True) == json.dumps(mains[method], sort_keys=True)


@pytest.mark.asyncio
async def test_only_the_load_of_a_workflows_session_says_workflows_off(monkeypatch):
    kas_chat.note_workflow_session(_RESUME_SID, on=True)  # Crew last drove it with them on
    new, load = await _frames(ACP_BACKEND_KAS, monkeypatch)
    assert _workflow_key(new) is None, "a new session has nothing persisted to clear"
    assert _workflow_key(load) == {"enabled": False}
    assert not kas_chat.was_workflow_session(_RESUME_SID), "cleared once, then main's frame"
    _new, again = await _frames(ACP_BACKEND_KAS, monkeypatch)
    assert "workflows" not in json.dumps(again)


@pytest.mark.parametrize("backend, asked", [(ACP_BACKEND_KIRO, False), (ACP_BACKEND_KAS, True)])
@pytest.mark.asyncio
async def test_only_a_kas_start_asks_whether_the_chat_wants_workflows(monkeypatch, backend, asked):
    # harness-parity H13: a kiro-cli start awaits nothing it did not await before.
    from kiro_crew.acp import runtime_session_create

    calls: list[str] = []

    async def _wants(session_key: str) -> bool:
        calls.append(session_key)
        return False

    monkeypatch.setattr(runtime_session_create, "kas_chat_wants_workflows", _wants)
    await _session_new(backend, monkeypatch)
    assert calls == (["dashboard:chat-7"] if asked else [])


@pytest.mark.asyncio
async def test_a_kiro_cli_chat_with_the_switch_off_is_sent_no_kas_setting(monkeypatch):
    params = await _session_new(ACP_BACKEND_KIRO, monkeypatch)
    assert _settings(params) is None
    assert "workflows" not in str(params.get("_meta"))


# ── The switch changes nothing until the dashboard is wired ──
#
# The real gate (config AND the router's turn starter) on the real create_session and
# load_session: with no dashboard wiring no turn starter is installed, so a switched-on KAS
# chat is sent workflows.enabled:false and no workflow tools, exactly as switched off.

_TOOLED_AGENTS = [{"id": _AGENT, "name": _AGENT, "prompt": "pinned", "tools": ["fs_read"]}]


async def _new_and_load(
    monkeypatch: pytest.MonkeyPatch, *, starter: bool, sink: bool = True
) -> tuple[dict, dict]:
    from pathlib import PurePosixPath
    from types import SimpleNamespace

    from kiro_crew.acp.harness import SessionExtras
    from kiro_crew.acp.types import METHOD_SESSION_LOAD
    from kiro_crew.config import live
    from kiro_crew.kas_workflows import router as router_mod

    monkeypatch.setattr(
        live,
        "snapshot",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(acp_backend=ACP_BACKEND_KAS, kas_workflows=True)
        ),
    )
    monkeypatch.setattr(router_mod, "_TURN_STARTER", object() if starter else None)
    rt = _runtime(ACP_BACKEND_KAS)
    sent: list[dict] = []

    async def _fake_send(method, params, timeout=None):
        sent.append({"method": method, "params": params})
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "pinned-new-sid", "modes": _MODES}
        if method == METHOD_SESSION_LOAD:
            return {"modes": _MODES}
        if method == METHOD_SET_MODE:
            sid = params["sessionId"]
            names = [entry["name"] for entry in _MCP_ROSTER]
            origin = {"kiro": {"resource": {"source": {"origin": "client"}}}}
            status = [{"name": n, "status": "connected", "_meta": origin} for n in names]
            tags = [{"source": "mcp", "tag": f"@{n}/tool"} for n in names]
            for method_name, payload in (
                ("_kiro/mcp/status", {"servers": status}),
                ("_kiro/tools/didChange", {"tags": tags}),
            ):
                rt._session_queues[sid].put_nowait(
                    JsonRpcMessage(method=method_name, params={"sessionId": sid, **payload})
                )
        return {}

    async def _fake_work_dir(cwd=None):
        return PurePosixPath(_CWD)

    async def _fake_kas_agents(
        agent, *, member_dispatch=False, crew_panel=False, session_key="", session_cwd=None
    ):
        return SessionExtras(custom_agents=[dict(a) for a in _TOOLED_AGENTS])

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_session_work_dir", _fake_work_dir)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_kas_agents)
    monkeypatch.setattr(runtime_mod, "mint_stub_session_token", lambda: "pinned-token")
    monkeypatch.setattr(runtime_mod, "publish_session_token", lambda token, key: None)
    monkeypatch.setattr(
        runtime_mod, "pooled_session_servers", lambda overlay, agent, **_kw: _MCP_ROSTER
    )
    assert rt._workflow_sink is not None, "a KAS runtime carries the router's sink"
    if not sink:
        rt._workflow_sink = None  # as when the router failed to build

    handle = await rt.create_session(
        cwd=_CWD, agent=_AGENT, mcp_servers=_MCP_ROSTER, session_key="dashboard:chat-7"
    )
    await handle.destroy()
    loaded = await rt.load_session(
        session_file="/tmp/pinned-session.json",
        resume_sid="pinned-resume-sid",
        cwd=_CWD,
        agent=_AGENT,
        session_key="dashboard:chat-7",
    )
    await loaded.destroy()
    (new,) = [r["params"] for r in sent if r["method"] == METHOD_SESSION_NEW]
    (load,) = [r["params"] for r in sent if r["method"] == METHOD_SESSION_LOAD]
    return new, load


def _tools(params: dict) -> list[str]:
    agents = ((params.get("_meta") or {}).get("kiro") or {}).get("customAgents") or []
    return [t for a in agents if a.get("id") == _AGENT for t in a.get("tools") or []]


@pytest.mark.asyncio
async def test_a_switched_on_chat_with_no_dashboard_wiring_is_sent_no_workflow_key(monkeypatch):
    for params in await _new_and_load(monkeypatch, starter=False):
        assert _workflow_key(params) is None
        assert "run_workflow" not in _tools(params)


@pytest.mark.asyncio
async def test_a_switched_on_load_with_no_sink_keeps_the_record_for_the_off_load(monkeypatch):
    # No sink (the router failed to build): the switched-on load sends no key, so it
    # must not clear the record the later switched-off load needs.
    kas_chat.note_workflow_session("pinned-resume-sid", on=True)
    _new, load = await _new_and_load(monkeypatch, starter=True, sink=False)
    assert _workflow_key(load) is None
    assert kas_chat.was_workflow_session("pinned-resume-sid")


@pytest.mark.asyncio
async def test_a_session_driven_with_workflows_then_switched_off_is_loaded_with_them_off(
    monkeypatch,
):
    await _new_and_load(monkeypatch, starter=True)  # started and loaded with them on
    assert kas_chat.was_workflow_session("pinned-new-sid")
    assert kas_chat.was_workflow_session("pinned-resume-sid")
    new, load = await _new_and_load(monkeypatch, starter=False)
    assert _workflow_key(new) is None
    assert _workflow_key(load) == {"enabled": False}
    _new, again = await _new_and_load(monkeypatch, starter=False)
    assert _workflow_key(again) is None, "said once; the session no longer has the surface"


@pytest.mark.asyncio
async def test_a_switched_on_chat_with_the_dashboard_wired_is_sent_workflows_on(monkeypatch):
    for params in await _new_and_load(monkeypatch, starter=True):
        assert _settings(params)["workflows"]["enabled"] is True
        assert "run_workflow" in _tools(params)


@pytest.mark.asyncio
async def test_the_workflow_setting_rides_beside_the_memory_setting(monkeypatch):
    # Both write ``_meta.kiro.settings``; neither may replace the other's key.
    from kiro_crew.acp import kas_memory

    async def _disabled(_backend, _mode):
        return kas_memory.MODE_DISABLED

    monkeypatch.setattr(kas_memory, "runtime_memory_mode", _disabled)
    for params in await _new_and_load(monkeypatch, starter=True):
        settings = _settings(params)
        assert settings["workflows"]["enabled"] is True
        assert settings["memory"] == kas_memory.memory_settings(kas_memory.MODE_DISABLED)["memory"]
        assert "run_workflow" in _tools(params)


@pytest.mark.asyncio
async def test_a_kiro_cli_resume_reads_no_workflow_record(monkeypatch):
    # harness-parity H13: the kiro load path does no KAS workflow bookkeeping.
    from kiro_crew.acp import runtime_session_load

    asked: list[str] = []
    monkeypatch.setattr(
        runtime_session_load, "kas_was_workflow_session", lambda sid: asked.append(sid) or False
    )
    monkeypatch.setattr(
        runtime_session_load,
        "note_kas_workflow_session",
        lambda sid, *, on: asked.append(sid),
    )
    _new, load = await _frames(ACP_BACKEND_KIRO, monkeypatch)
    assert asked == []
    assert "workflows" not in json.dumps(load)


@pytest.mark.asyncio
async def test_the_off_load_moves_the_record_only_as_its_frame_said(monkeypatch):
    # The decision is the frame's: a record written while the load awaited KAS is
    # not cleared by a load that sent no key.
    from kiro_crew.acp import runtime_session_load

    real_note = kas_chat.note_workflow_session
    seen = {"reads": 0}

    def _was(sid: str) -> bool:
        seen["reads"] += 1
        return kas_chat.was_workflow_session(sid)

    monkeypatch.setattr(runtime_session_load, "kas_was_workflow_session", _was)
    moved: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        runtime_session_load,
        "note_kas_workflow_session",
        lambda sid, *, on: (moved.append((sid, on)), real_note(sid, on=on)),
    )
    _new, load = await _frames(ACP_BACKEND_KAS, monkeypatch)
    assert "workflows" not in json.dumps(load)
    assert moved == [] and seen["reads"] == 1, "read once, with the frame; nothing moved"
