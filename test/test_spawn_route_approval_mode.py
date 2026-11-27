"""``POST /api/spawn`` keeps ``approval_mode: "auto"`` only where the caller's session has it.

The internal secret admits any local process, so it grants no approval mode by
itself. Each test is a real HTTP request to a started API server; the subagent
manager is a double, so what the route hands it is the observable answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_TOKENS = {
    "tok-chat": "dash:chat-normal",
    "tok-trusted": "dash:chat-trusted",
    "tok-cron-auto": "cron:aaaa1111",
    "tok-cron-ask": "cron:bbbb2222",
    "tok-child": "subagent:child1",
}
_POLICIES = {"dash:chat-trusted": "auto"}
_JOBS = {
    "aaaa1111": SimpleNamespace(id="aaaa1111", approval_mode="auto"),
    "bbbb2222": SimpleNamespace(id="bbbb2222", approval_mode=""),
}


def _manager() -> MagicMock:
    mgr = MagicMock()
    mgr._global_approval_mode = "auto"
    mgr._sessions.get_approval_policy = lambda key: _POLICIES.get(key, "")
    child = SimpleNamespace(
        id="child1",
        done=False,
        approval_mode="auto",
        conversation_key="",
        _session_sharing=False,
        started=0.0,
    )
    runs = {"child1": child}
    mgr.get = lambda agent_id: runs.get(agent_id)
    mgr._agents = runs
    mgr.spawn.return_value = SimpleNamespace(
        id="sa1", done=False, error="", queued_reason="", status="running"
    )
    return mgr


async def _start(tmp_path, monkeypatch, mgr):
    import kiro_crew.config.loader as _loader
    import kiro_crew.dashboard.server as _srv
    import kiro_crew.dashboard.state as _st
    import kiro_crew.execution_context as _exec
    import kiro_crew.member_memory_auth as _auth

    monkeypatch.setattr(_srv, "data_home", lambda: tmp_path)
    monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)
    # The session token check is the transport's own; these tokens stand for
    # tokens it verified. No transcript exists for the test sessions.
    monkeypatch.setattr(_auth, "verify_session_token", lambda token: _TOKENS.get(token, ""))
    default = _exec.ExecutionContext(None, _exec.MemoryStoreRef("default"), "template", "kirocrew")
    monkeypatch.setattr(_exec, "read_session_execution", lambda *_a, **_kw: default)
    import kiro_crew.dashboard.handlers.messaging as _msg

    monkeypatch.setattr(_msg, "_spawn_request_memory_mode", AsyncMock(return_value="persistent"))
    from kiro_crew.dashboard.server import start_api_server

    runner, _state = await start_api_server(
        sessions=MagicMock(count=0),
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]),
            list_jobs_async=AsyncMock(return_value=[]),
            status=MagicMock(return_value={}),
            get_job=lambda job_id: _JOBS.get(job_id),
            _jobs=list(_JOBS.values()),
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        port=0,
        subagents=mgr,
    )
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    secret = (tmp_path / ".local_secret").read_text(encoding="utf-8").strip()
    return runner, base, secret


async def _post(tmp_path, monkeypatch, body: dict, token: str = "") -> tuple[int, MagicMock]:
    import aiohttp

    mgr = _manager()
    runner, base, secret = await _start(tmp_path, monkeypatch, mgr)
    headers = {"X-Internal-Secret": secret}
    if token:
        headers["X-Session-Key"] = _TOKENS[token]
        headers["X-Session-Token"] = token
        body = {**body, "parent_session": _TOKENS[token]}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/api/spawn", json=body, headers=headers) as resp:
                mgr.response_text = await resp.text()
                return resp.status, mgr
    finally:
        await runner.cleanup()


def _spawned_mode(mgr: MagicMock) -> object:
    assert mgr.spawn.call_count == 1
    return mgr.spawn.call_args.kwargs["approval_mode"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "asked", "expected"),
    [
        # No session: the secret alone keeps no "auto" (the manager still applies
        # the global agent.approval_mode to a run with no parent).
        ("", "auto", None),
        ("", "", None),
        ("", None, None),
        # A chat that asks before each tool keeps asking.
        ("tok-chat", "auto", None),
        ("tok-chat", "", None),
        # A trusted chat keeps the "auto" it already has.
        ("tok-trusted", "auto", "auto"),
        # A scheduled job's spawn_run forwards the job's own mode.
        ("tok-cron-auto", "auto", "auto"),
        ("tok-cron-ask", "auto", None),
        # A sub-agent that runs auto passes it on to its own children.
        ("tok-child", "auto", "auto"),
    ],
)
async def test_spawn_keeps_auto_only_where_the_session_has_it(
    tmp_path, monkeypatch, token, asked, expected
):
    body: dict = {"task": "noop"}
    if asked is not None:
        body["approval_mode"] = asked
    status, mgr = await _post(tmp_path, monkeypatch, body, token)
    assert status == 200, mgr.response_text
    assert _spawned_mode(mgr) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["yolo", "trust-all", "AUTO", "trust", 1, True])
async def test_spawn_refuses_an_unknown_approval_mode(tmp_path, monkeypatch, asked):
    status, mgr = await _post(tmp_path, monkeypatch, {"task": "noop", "approval_mode": asked})
    assert status == 400
    assert not mgr.spawn.called
