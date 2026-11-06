"""With Kiro Workflows off, only a KAS chat's ``session/new`` says ``workflows.enabled:false``.

KAS keeps a workflow surface it was once given unless a later request turns it off,
so a KAS dashboard chat whose switch is off says so on every start. No other harness
reads ``_meta.kiro.settings``, and none is sent the key.

Drives the real ``create_session`` against the fake subprocess of
``test_acp_harness_wire_parity``.
"""

from __future__ import annotations

import pytest
from test_acp_harness_wire_parity import (  # noqa: F401 - the fixture is used by name
    _AGENT,
    _CWD,
    _KAS_AGENTS,
    _MCP_ROSTER,
    _MODES,
    _fast_no_report_ceiling,
    _runtime,
)

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
    JsonRpcMessage,
)
from kiro_crew.kas_workflows import chat as kas_chat


async def _session_new(backend: str, monkeypatch: pytest.MonkeyPatch) -> dict:
    from pathlib import PurePosixPath

    from kiro_crew.acp.harness import SessionExtras

    rt = _runtime(backend)
    sent: list[dict] = []

    async def _fake_send(method, params, timeout=None):
        sent.append({"method": method, "params": params})
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "pinned-new-sid", "modes": _MODES}
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

    handle = await rt.create_session(
        cwd=_CWD, agent=_AGENT, mcp_servers=_MCP_ROSTER, session_key="dashboard:chat-7"
    )
    await handle.destroy()
    (new,) = [r for r in sent if r["method"] == METHOD_SESSION_NEW]
    return new["params"]


def _settings(params: dict):
    return ((params.get("_meta") or {}).get("kiro") or {}).get("settings")


@pytest.mark.asyncio
async def test_a_kas_chat_with_the_switch_off_says_workflows_off(monkeypatch):
    params = await _session_new(ACP_BACKEND_KAS, monkeypatch)
    assert _settings(params)["workflows"]["enabled"] is False


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
