"""Unit tests for the AcpRuntime single-reader demux (Phase 1 multiplexing).

These exercise the routing logic that lets ONE kiro-cli acp process host
multiple concurrent sessions: the single _reader_loop owns stdout and routes
each frame to the right destination —

  - JSON-RPC response whose id is in _pending_requests  → resolve that Future
  - JSON-RPC response whose id is in _routed_requests   → that session's queue
  - notification carrying params.sessionId              → that session's queue
  - request (method + id) with no sessionId             → answered ONCE at
                                                           connection level (-32601)
  - notification with no sessionId                       → broadcast to all
  - empty read (process exit)                            → _mark_dead: fail all
                                                           futures + poison queues

The headline test
(`test_acp_runtime_session_handle.py::test_multiple_sessions_routed_independently`)
proves the end-to-end claim: two AcpSessionHandle turns run concurrently on one
runtime and each receives only its own session's text + completion.

The reader is driven with a REAL asyncio.StreamReader fed crafted JSON-RPC
lines; the subprocess and stdin are mocked (no kiro-cli is launched).

This module keeps the routing tests and KAS session readiness; the rest of the
runtime is pinned in the sibling ``test_acp_runtime_<topic>.py`` modules, which
share the harness in ``acp_runtime_helpers.py``.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _feed,
    _make_runtime,
    _next,
    _permission_msg,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.runtime import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODE,
)
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.testing.wait import await_until, default_timeout

# ── Harness ──


@pytest.fixture
def kas_readiness_wire(monkeypatch, tmp_path):
    """Real demux and session startup; only the subprocess and clock are fake.

    The fake stdout is a ``_RunningLoopStreamReader`` (see ``_make_runtime``): a
    StreamReader that binds its loop lazily, the first time a read suspends, so it
    picks up the loop pytest-asyncio runs the test on rather than the synchronous
    fixture-setup loop. Any consumer of ``rt._process.stdout`` gets a correctly-
    bound reader, not only tests that start it through ``_start_reader``.
    """
    from types import SimpleNamespace

    import kiro_crew.acp.runtime as runtime_mod
    import kiro_crew.acp.session_handle as sh

    rt, reader, proc = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._can_load_session = True
    rt._work_dir = tmp_path
    clock = [0.0]
    monkeypatch.setattr(sh, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(rt, "_session_start_budget", AsyncMock(return_value=30.0))
    monkeypatch.setattr(
        rt,
        "_kas_custom_agents",
        AsyncMock(
            return_value=SessionExtras(
                custom_agents=[
                    {
                        "id": "worker",
                        "tools": ["@kirocrew-core", "@kirocrew-dashboard"],
                        "mcpServers": {"kirocrew-core": {}, "external": {}},
                    },
                    {"id": "inactive", "mcpServers": {"kirocrew-work": {}}},
                ]
            )
        ),
    )
    sent = asyncio.Queue()
    reads = asyncio.Queue()
    proc.stdin.write.side_effect = lambda raw: sent.put_nowait(json.loads(raw))
    original_init = AcpSessionHandle.__init__

    def observe_queue(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        get = self._queue.get

        async def observed_get():
            reads.put_nowait(None)
            return await get()

        monkeypatch.setattr(self._queue, "get", observed_get)

    monkeypatch.setattr(AcpSessionHandle, "__init__", observe_queue)

    async def take(queue):
        return await _next(queue, what="the next frame on the wire")

    def status(
        state="connecting", sid="ready-session", *, origin="client", extra_servers=(), **extra
    ):
        # ``origin=None`` reproduces the captured kiro-cli 2.18.0 wire: no
        # ``_meta`` on ANY entry, not a foreign origin on one of them.
        meta = (
            {}
            if origin is None
            else {"_meta": {"kiro": {"resource": {"source": {"origin": origin}}}}}
        )
        _feed(
            reader,
            {
                "method": "_kiro/mcp/status",
                "params": {
                    "sessionId": sid,
                    "servers": [
                        {"name": "kirocrew-core", "status": state, **meta, **extra},
                        {"name": "kirocrew-dashboard", "status": "connected", **meta},
                        {"name": "external", "status": "failed", "errorMessage": "irrelevant"},
                        *({"name": name, "status": "connected", **meta} for name in extra_servers),
                    ],
                },
            },
        )

    def tags(*names, sid="ready-session"):
        _feed(
            reader,
            {
                "method": "_kiro/tools/didChange",
                "params": {
                    "sessionId": sid,
                    "tags": [{"source": "mcp", "tag": f"@{name}/some_tool"} for name in names],
                },
            },
        )

    async def handshake(
        resume,
        *,
        switch=True,
        pre_ready=False,
        pre_frames=(),
        injected=None,
        session_key="",
        agent_name="worker",
    ):
        kwargs = {"cwd": tmp_path, "agent": agent_name, "session_key": session_key}
        servers = injected if injected is not None else [{"name": "kirocrew-dashboard"}]
        if resume:
            # Load gets the session injection through the existing overlay seam.
            # ``**_kw``: the real signature takes the session's checkout as
            # ``work_dir``, and a double that refuses it makes ``load_session``
            # raise before it ever reaches the wire, so every assertion below
            # fails as a handshake timeout instead of naming the double.
            monkeypatch.setattr(
                runtime_mod,
                "pooled_session_servers",
                lambda *_, **_kw: servers,
            )
            start = rt.load_session("", "ready-session", **kwargs)
        else:
            start = rt.create_session(mcp_servers=servers, **kwargs)
        task = asyncio.create_task(start)
        request = await take(sent)
        assert request["method"] == (METHOD_SESSION_LOAD if resume else METHOD_SESSION_NEW)
        # The projection reaches the wire with the ACTIVE agent's hoistable
        # managed declarations carried in the session-level array instead of the
        # block (``hoist_servers_for_session``); everything else is byte-identical.
        projection = rt._kas_custom_agents.return_value.custom_agents
        sent_agents = request["params"]["_meta"]["kiro"]["customAgents"]
        assert len(sent_agents) == len(projection)
        wire_names = [entry["name"] for entry in request["params"]["mcpServers"]]
        assert len(wire_names) == len(set(wire_names)), "a name must appear once on the wire"
        for sent_agent, projected in zip(sent_agents, projection):
            hoisted = {
                name
                for name, entry in (projected.get("mcpServers") or {}).items()
                if projected.get("id") == agent_name
                and name in KIROCREW_BIN_MCP_SERVERS
                and isinstance(entry.get("command"), str)
                and entry.get("command")
                and name not in {e["name"] for e in servers}
            }
            expected = dict(projected)
            kept = {
                k: v for k, v in (projected.get("mcpServers") or {}).items() if k not in hoisted
            }
            if kept:
                expected["mcpServers"] = kept
            else:
                expected.pop("mcpServers", None)
            assert sent_agent == expected
            for name in hoisted:
                assert name in wire_names
                element = next(e for e in request["params"]["mcpServers"] if e["name"] == name)
                assert element["type"] == "stdio"
                # The resume path owns its array, so its session token rides on
                # the hoisted element; create_session is handed an explicit array
                # here, which mints none.
                tokens = [pair for pair in element["env"] if pair["name"] == STUB_SESSION_TOKEN_ENV]
                assert len(tokens) == (1 if resume else 0) and all(p["value"] for p in tokens)
                declared_env = [
                    {"name": k, "value": str(v)}
                    for k, v in (projected["mcpServers"][name].get("env") or {}).items()
                ]
                if not tokens:
                    assert element["command"] == projected["mcpServers"][name]["command"]
                    assert element["env"] == declared_env
                    continue
                # A tokened element launches the managed invocation, never the
                # spec's (KAS spawns it; gatewayd's own-binary check never runs).
                from kiro_crew.agent import _managed_mcp_env, managed_mcp_spec_entry

                managed = managed_mcp_spec_entry(name, include_opt_in=True)
                assert element["command"] == managed["command"]
                assert element["args"] == [str(a) for a in managed.get("args", [])]
                assert [p for p in element["env"] if p not in tokens] == [
                    p
                    for p in declared_env
                    if p["name"] in ("KIROCREW_PORT", "KIROCREW_SESSION_KEY")
                ] + [{"name": k, "value": v} for k, v in _managed_mcp_env().items()]
        if pre_ready:
            status("connected")
            tags("kirocrew-core", "kirocrew-dashboard")
        for frame in pre_frames:
            _feed(reader, frame)
        _feed(
            reader,
            {
                "id": request["id"],
                "result": {
                    "sessionId": "ready-session",
                    "modes": {
                        "currentModeId": "before" if switch else agent_name,
                        "availableModes": [{"id": agent_name}],
                    },
                },
            },
        )
        mode = await take(sent)
        assert mode["method"] == METHOD_SET_MODE
        _feed(reader, {"id": mode["id"], "result": {}})
        return task

    return SimpleNamespace(
        runtime=rt,
        reader=reader,
        sent=sent,
        reads=reads,
        take=take,
        clock=clock,
        status=status,
        tags=tags,
        handshake=handshake,
    )


@pytest.mark.asyncio
async def test_kas_readiness_wire_stdout_reads_on_the_running_loop(kas_readiness_wire):
    """A consumer that reads ``rt._process.stdout`` directly must not hit the
    cross-loop error the fixture once needed ``_start_reader`` to paper over.

    A plain ``StreamReader`` pins its loop at construction, so one built on a
    synchronous fixture-setup loop would create its ``readuntil`` waiter on that
    loop and, from a test running on a different loop, raise ``RuntimeError: got
    Future attached to a different loop``. The fixture's reader binds its loop
    lazily on the first suspending read, so it picks up the running loop for any
    consumer -- including this one, which drives stdout directly without going
    through ``_start_reader``. The read must suspend (partial data, no delimiter
    yet) to create that waiter; a line already complete in the buffer returns
    without one and would not exercise the bug.
    """
    wire = kas_readiness_wire
    stdout = wire.runtime._process.stdout
    assert isinstance(stdout, asyncio.StreamReader)
    # Feed a partial line so readuntil must suspend and create its waiter on the
    # reader's bound loop; complete the line from a task on the running loop. The
    # read is bounded by ``default_timeout()`` (half the test's budget, >= the
    # wait-discipline floor) so a wrong-loop regression fails this test by name
    # instead of hanging into the runner's worker-killing timeout (rule D8).
    stdout.feed_data(b'{"ok": tr')

    async def _complete() -> None:
        await asyncio.sleep(0)
        stdout.feed_data(b"ue}\n")

    completer = asyncio.ensure_future(_complete())
    try:
        line = await asyncio.wait_for(stdout.readuntil(b"\n"), timeout=default_timeout())
    finally:
        completer.cancel()
        await asyncio.gather(completer, return_exceptions=True)
    assert json.loads(line) == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("revoked", [False, True], ids=["valid", "revoked"])
async def test_derived_worker_identity_keeps_freshness_and_readiness(
    kas_readiness_wire, monkeypatch, tmp_path, resume, revoked
):
    from kiro_crew import agent, agent_state
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.config import paths as paths_mod

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents_dir)
    # Redirect through the override: ``acp.skill_projection`` is first imported
    # inside this test and binds ``kiro_agents_dir`` by name, so the redirect must
    # live in a value that function reads on every call.
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: agents_dir)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path / "derived-state")
    default = agents_dir / "kirocrew.json"
    spec = {
        "name": "kirocrew",
        "prompt": "Complete the assigned work.",
        "tools": ["@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "unused"}},
    }
    default.write_text(json.dumps(spec), encoding="utf-8")
    await asyncio.to_thread(agent._install_worker_agent)
    key = "subagent:derived-worker"
    extras = await harness_for(ACP_BACKEND_KAS).session_extras(
        "kirocrew-worker", work_dir=tmp_path, session_key=key
    )
    assert extras.derived_spec_snapshot is not None
    for server in ("kirocrew-core", "kirocrew-work"):
        assert extras.custom_agents[0]["mcpServers"][server]["env"]["KIROCREW_SESSION_KEY"] == key
    wire = kas_readiness_wire
    monkeypatch.setattr(wire.runtime, "_kas_custom_agents", AsyncMock(return_value=extras))
    terminate = AsyncMock()
    monkeypatch.setattr(wire.runtime, "terminate_session", terminate)
    if revoked:
        # The host will receive the old payload. Ready MCP reports must not
        # authorize it after the owner's source grants have changed.
        spec["mcpServers"] = {}
        default.write_text(json.dumps(spec), encoding="utf-8")

    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume,
            pre_ready=revoked,
            injected=[],
            session_key=key,
            agent_name="kirocrew-worker",
        )
        wire.runtime._kas_custom_agents.assert_awaited_once_with(
            "kirocrew-worker", member_dispatch=False, crew_panel=False, session_key=key
        )
        if revoked:
            wire.status("connected", extra_servers=("kirocrew-work",))
            wire.tags("kirocrew-core", "kirocrew-work")
            with pytest.raises(AcpRuntimeError, match="changed during worker load"):
                await await_until(start, what="the session start")
            terminate.assert_awaited_once_with("ready-session")
        else:
            await wire.take(wire.reads)
            wire.clock[0] = 7.0
            wire.status()
            await wire.take(wire.reads)
            assert not start.done()
            wire.status("connected", extra_servers=("kirocrew-work",))
            await wire.take(wire.reads)
            assert not start.done(), "a fresh template still needs actual tool exposure"
            wire.tags("kirocrew-core", "kirocrew-work")
            await await_until(start, what="the session start")
            terminate.assert_not_awaited()
        assert wire.sent.empty(), "startup must not issue a prompt"
    finally:
        if start is not None:
            if not start.done():
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_session_cwd_is_bound_through_real_create_and_load(
    kas_readiness_wire, monkeypatch, tmp_path, resume
):
    """The directory a session opens against must survive to the PUBLIC provider cwd.

    This drives the REAL ``create_session``/``load_session`` wire path (not a
    hand-set ``handle._bound_cwd``), so it pins the wiring the unit tests cannot:
    ``runtime.py`` records ``bound_cwd=str(session_work_dir)`` on create and
    ``bound_cwd=str(load_params["cwd"])`` on load. Reverting either line makes
    the handle's ``_bound_cwd`` fall back to ``""`` and both assertions below
    fail. The runtime's ``_work_dir`` is repointed at a sibling directory while
    the session opens against ``tmp_path`` (a different directory), which is the
    shared-runtime case bolichen97's review is about: on a shared runtime,
    answering reuse validation with the runtime's own directory would evict a
    live session that bound elsewhere.

    It also pins the SessionMap/resume-caller flow end to end: the inner
    ``AcpSessionProvider.cwd`` must report the bound dir, AND the ``AcpProvider``
    wrapper that every caller actually holds must forward it rather than
    returning ``self._client._work_dir``.
    """
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    wire = kas_readiness_wire
    # The fixture starts the runtime in tmp_path and the session also opens
    # against tmp_path. Repoint the runtime's _work_dir at a DIFFERENT directory
    # so the two genuinely diverge -- the shared-runtime case: a runtime started
    # in A hosting a session that bound to B. If cwd leaked the runtime's dir,
    # the assertions below would read A, not the session's B (tmp_path).
    runtime_work_dir = tmp_path / "runtime-elsewhere"
    runtime_work_dir.mkdir()
    wire.runtime._work_dir = runtime_work_dir
    assert str(wire.runtime._work_dir) != str(tmp_path)

    # ``_bound_cwd`` is recorded synchronously when ``create_session`` /
    # ``load_session`` constructs the handle, well before the session reaches
    # readiness. Capture the handle at construction rather than driving the full
    # readiness dance -- the wiring under test is the construction argument.
    captured: list[AcpSessionHandle] = []
    constructed = AcpSessionHandle.__init__

    def capture_init(self, *args, **kwargs):
        constructed(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(AcpSessionHandle, "__init__", capture_init)

    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume, pre_ready=True)
        # Let the handshake get as far as constructing the handle (session/new or
        # session/load has returned; the handle is built right after).
        for _ in range(20):
            if captured:
                break
            await asyncio.sleep(0)
        assert captured, "the real create/load path never constructed a handle"
        handle = captured[-1]

        # The wiring recorded the session's own directory, not the runtime's.
        assert handle._bound_cwd == str(tmp_path)

        # The inner provider reads the bound dir off the handle.
        inner = AcpSessionProvider(handle, wire.runtime)
        assert inner.cwd == str(tmp_path)

        # The wrapper every SessionMap/resume caller holds forwards the SAME
        # bound dir, not the shared runtime's _work_dir.
        wrapper = AcpProvider.__new__(AcpProvider)
        wrapper._client = inner
        assert wrapper.cwd == str(tmp_path)
    finally:
        if start is not None:
            if not start.done():
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("pre_ready", [False, True], ids=["cold", "stale-mode"])
async def test_kas_readiness_delays_prompt_until_active_managed_tools(
    kas_readiness_wire, monkeypatch, resume, pre_ready
):
    """The first prompt cannot race core when startup takes more than six seconds."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 6.0)
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume, pre_ready=pre_ready, session_key="subagent:readiness-worker"
        )
        wire.runtime._kas_custom_agents.assert_awaited_once_with(
            "worker",
            member_dispatch=False,
            crew_panel=False,
            session_key="subagent:readiness-worker",
        )
        # Two pre-mode snapshots must be consumed without satisfying this activation.
        for _ in range(3 if pre_ready else 1):
            await wire.take(wire.reads)
        wire.clock[0] = 7.0
        wire.status()
        await wire.take(wire.reads)
        assert not start.done()
        assert wire.sent.empty()

        wire.status("connected", sid="other-session")
        wire.tags("kirocrew-core", "kirocrew-dashboard", sid="other-session")
        wire.status("connected", sid=None)
        wire.tags("kirocrew-core", "kirocrew-dashboard", sid=None)
        wire.status("connected", origin="global")
        wire.tags("kirocrew-dashboard")
        for _ in range(3):
            await wire.take(wire.reads)
        await wire.take(wire.reads)
        assert not start.done()

        wire.status("connected")
        await wire.take(wire.reads)
        assert not start.done(), "Connected transport alone does not establish tool exposure"
        wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert wire.sent.empty()

        async def collect():
            return [event async for event in handle.prompt("ready")]

        turn = asyncio.create_task(collect())
        try:
            request = await wire.take(wire.sent)
            assert request["method"] == "session/prompt"
            _feed(wire.reader, {"id": request["id"], "result": {"stopReason": "end_turn"}})
            await await_until(turn, what="the turn finishing")
            assert wire.sent.empty()
        finally:
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize(
    "state",
    [
        "failed",
        "disabled",
        "authorization",
        "timeout",
        "unreported",
        "foreign-only",
        "missing-catalog",
        "legacy-provenance",
    ],
)
async def test_kas_readiness_refuses_failure_or_missing_report(
    kas_readiness_wire, monkeypatch, resume, state
):
    import kiro_crew.acp.runtime_session_load as load_mod
    from kiro_crew.acp.session_handle import AcpRequestTimeout

    monkeypatch.setattr(load_mod, "_declared_governed", lambda: False)
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume)
        await wire.take(wire.reads)
        if state in ("timeout", "missing-catalog"):
            wire.clock[0] = 31.0
            wire.status("connected" if state == "missing-catalog" else "connecting")
        elif state == "unreported":
            # No report at all and registry mode off: silence is not proof of a
            # governed drop, so the start still fails.
            wire.clock[0] = 31.0
            _feed(
                wire.reader, {"method": "session/update", "params": {"sessionId": "ready-session"}}
            )
        elif state == "foreign-only":
            # Only a same-named server that is not this session's own: it stays
            # pending and must never pass for a server the host did not start.
            wire.clock[0] = 31.0
            wire.status("connected", origin="global")
        elif state == "authorization":
            wire.status(
                "connecting", failedAuthorization=True, errorMessage="authorization required"
            )
        elif state == "legacy-provenance":
            # Captured kiro-cli 2.18.0: connected with a catalog, tag to follow,
            # no origin anywhere. ``kirocrew-core`` reached the backend only via
            # the agent block (the fixture injects just ``kirocrew-dashboard``),
            # so it is refused before the timeout, naming the limit.
            wire.status("connected", origin=None, tools=[{"name": "ping", "disabled": False}])
            wire.tags("kirocrew-core", "kirocrew-dashboard")
        else:
            wire.status(state, errorMessage="managed test failure")
        expected = (
            AcpRequestTimeout
            if state in ("timeout", "unreported", "foreign-only", "missing-catalog")
            else AcpRuntimeError
        )
        if not resume:
            deletion = await wire.take(wire.sent)
            assert deletion["method"] == "_kiro/session/delete"
            assert deletion["params"] == {"sessionId": "ready-session"}
            _feed(wire.reader, {"id": deletion["id"], "result": {}})
        with pytest.raises(expected, match="kirocrew-core") as raised:
            await await_until(start, what="the session start")
        if state == "legacy-provenance":
            assert "connected without provenance" in str(raised.value)
            assert "reports no MCP server origin" in str(raised.value)
        assert wire.sent.empty(), "A failed startup must not prompt or delete a retained session"
        assert "ready-session" not in wire.runtime._session_queues
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_kas_readiness_under_registry_mode_starts_without_waiting(
    kas_readiness_wire, monkeypatch, resume
):
    """Under registry mode a governed MCP profile drops Crew's servers unreported.

    Waiting for them spends the whole budget and then fails, and the retry meets
    the same filter, so the chat never starts. The session starts after a short
    settle instead (7s on the fake clock, under the 30s budget), each managed
    server named as not started, and nothing is torn down.
    """
    import kiro_crew.acp.runtime_session_load as load_mod

    monkeypatch.setattr(load_mod, "_declared_governed", lambda: True)
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume)
        await wire.take(wire.reads)
        wire.clock[0] = 7.0
        _feed(wire.reader, {"method": "session/update", "params": {"sessionId": "ready-session"}})
        handle = await await_until(start, what="the session start")
        assert wire.sent.empty(), "Starting degraded must not delete the session"
        assert "ready-session" in wire.runtime._session_queues
        report = handle.mcp_session_report()
        for name in ("kirocrew-core", "kirocrew-dashboard"):
            assert name in report._failed
            assert "not started by the host" in report._failures[name]
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_kas_readiness_accepts_provenance_less_wire_for_injected_servers(
    kas_readiness_wire, resume
):
    """Captured kiro-cli 2.18.0 (``2.18.0-newload-global+session.json``): a
    session-level injection connects as the session's own server on new and
    load with no ``_meta`` anywhere. Injected names are therefore trusted on a
    provenance-less snapshot; connection plus tag exposure is still required.
    """
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume, injected=[{"name": "kirocrew-core"}, {"name": "kirocrew-dashboard"}]
        )
        await wire.take(wire.reads)
        wire.status("connecting", origin=None)
        await wire.take(wire.reads)
        assert not start.done()
        wire.status("connected", origin=None, tools=[{"name": "ping", "disabled": False}])
        await wire.take(wire.reads)
        assert not start.done(), "Connected transport alone does not establish tool exposure"
        wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert set(handle.mcp_session_report().payload()["ready"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert wire.sent.empty(), "startup must not issue a prompt"
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("catalog", [True, False], ids=["catalog", "no-catalog"])
async def test_kas_default_managed_core_is_hoisted_and_ready_on_provenance_less_wire(
    kas_readiness_wire, monkeypatch, resume, catalog
):
    """The ordinary install: ``kirocrew-core`` declared only by the agent spec,
    nothing stubbed. The runtime carries the projected declaration in the
    session-level array (``2.18.0-payload-probe.json``: that payload connects
    Crew's own server past colliding global and workspace entries on new and
    load), so the provenance-less wire reads it as injected and startup completes.
    Exposure comes from the connected entry's own catalog when it carries one
    (2.18.0 through 2.22.0 all do), and only otherwise from a tag frame.
    """
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    wire = kas_readiness_wire
    projected = to_client_custom_agent(
        "worker",
        {
            "tools": ["@kirocrew-core"],
            "allowedTools": [],
            "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp"]}},
        },
        "Test worker",
        session_key="subagent:default-worker",
    )
    monkeypatch.setattr(
        wire.runtime,
        "_kas_custom_agents",
        AsyncMock(return_value=SessionExtras(custom_agents=[projected])),
    )
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume, session_key="subagent:default-worker")
        sent = wire.runtime._kas_custom_agents.call_args
        assert sent.kwargs["session_key"] == "subagent:default-worker"
        await wire.take(wire.reads)
        if catalog:
            wire.status("connected", origin=None, tools=[{"name": "ping", "disabled": False}])
        else:
            wire.status("connected", origin=None)
            await wire.take(wire.reads)
            assert not start.done(), "exposure is still required for an injected server"
            wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert set(handle.mcp_session_report().payload()["ready"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert set(handle.mcp_session_report().payload()["configured"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert wire.sent.empty(), "startup must not issue a prompt"
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_kas_readiness_accepts_pre_response_reports_for_unchanged_mode(
    kas_readiness_wire, resume
):
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(resume, switch=False, pre_ready=True)
        handle = await await_until(start, what="the session start")
        report = handle.mcp_session_report().payload()
        assert set(report["ready"]) == {"kirocrew-core", "kirocrew-dashboard"}
        assert wire.sent.empty()
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize(
    "tools,excluded,catalog_state",
    [
        (["read"], [], "enabled"),
        (["*"], ["@kirocrew-core"], "enabled"),
        (["@kirocrew-core/memory_recall"], ["@kirocrew-core/memory_recall"], "enabled"),
        (["@kirocrew-core/memory_recall"], ["@kirocrew-core/memory_recall"], "empty"),
        (
            ["@kirocrew-core"],
            ["@kirocrew-core/memory_recall", "@kirocrew-core/learn_add"],
            "enabled",
        ),
        (["@kirocrew-core/memory_recall"], [], "disabled"),
        (["*"], [], "disabled"),
        (["@kirocrew-core"], ["@kirocrew-core/learn_add"], "enabled"),
    ],
    ids=[
        "no-server-grant",
        "excluded-server",
        "excluded-selected-tool",
        "excluded-selected-tool-empty-catalog",
        "excluded-all-tools",
        "disabled-selected-tool",
        "disabled-all-tools",
        "unapproved-recall-exposed-by-catalog",
    ],
)
async def test_kas_readiness_respects_projected_tool_restrictions(
    kas_readiness_wire, monkeypatch, resume, tools, excluded, catalog_state
):
    """A declared server with intentionally hidden tools must still connect.

    The last case is the one with an exposed tool: its connected catalog is the
    exposure evidence, so no tag frame is needed (none arrives on 2.22.0).
    """
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    wire = kas_readiness_wire
    projected = to_client_custom_agent(
        "worker",
        {
            "tools": tools,
            "excludedTools": excluded,
            "allowedTools": [],
            "mcpServers": {"kirocrew-core": {"command": "unused-test-mcp"}},
        },
        "Test worker",
        member_dispatch=True,
    )
    original = json.loads(json.dumps(projected))
    monkeypatch.setattr(
        wire.runtime,
        "_kas_custom_agents",
        AsyncMock(return_value=SessionExtras(custom_agents=[projected])),
    )
    reader_task = await _start_reader(wire.runtime)
    start = None
    catalog = [
        {"name": name, "disabled": catalog_state == "disabled"}
        for name in ("memory_recall", "learn_add")
        if catalog_state != "empty"
    ]
    try:
        start = await wire.handshake(resume)
        await wire.take(wire.reads)
        wire.status("connecting", tools=[])
        wire.tags("kirocrew-dashboard")
        for _ in range(2):
            await wire.take(wire.reads)
        assert not start.done(), "A restricted tool policy does not waive connection readiness"
        wire.status("connected", tools=catalog)
        handle = await await_until(start, what="the session start")
        assert set(handle.mcp_session_report().payload()["ready"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
        }
        assert projected == original, "Readiness must not change the projected grants"
        assert wire.sent.empty()
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("managed", [False, True], ids=["external-only", "managed-and-external"])
async def test_kas_readiness_preserves_external_init_side_effects(
    kas_readiness_wire, monkeypatch, caplog, resume, managed
):
    """OAuth/config/failure information survives without gating managed readiness."""
    wire = kas_readiness_wire
    oauth = {
        "method": METHOD_MCP_OAUTH_REQUEST,
        "params": {
            "sessionId": "ready-session",
            "serverName": "external-auth",
            "oauthUrl": "https://example.com/authorize",
        },
    }
    if not managed:
        monkeypatch.setattr(
            wire.runtime,
            "_kas_custom_agents",
            AsyncMock(
                return_value=SessionExtras(
                    custom_agents=[{"id": "worker", "mcpServers": {"external-auth": {}}}]
                )
            ),
        )
    reader_task = await _start_reader(wire.runtime)
    start = None
    caplog.set_level("INFO", logger="kiro_crew.acp.session_handle")
    try:
        start = await wire.handshake(resume, pre_frames=[oauth], injected=None if managed else [])
        # The pre-mode OAuth frame is still captured; it cannot arm readiness.
        for _ in range(2):
            await wire.take(wire.reads)
        cfg = [{"id": "effort", "options": ["low", "high"]}]
        _feed(wire.reader, oauth)  # duplicated notifications stay deduplicated
        _feed(
            wire.reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "ready-session",
                    "update": {"sessionUpdate": "config_option_update", "configOptions": cfg},
                },
            },
        )
        _feed(
            wire.reader,
            {
                "method": "_kiro.dev/mcp/server_init_failure",
                "params": {
                    "sessionId": "ready-session",
                    "serverName": "external-failed",
                    "error": "external initialization failed",
                },
            },
        )
        if managed:
            for _ in range(3):
                await wire.take(wire.reads)
            assert not start.done()
            wire.status("connected")
            wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        assert handle._config_options == cfg
        assert handle.pop_pending_oauth_requests() == [
            {"serverName": "external-auth", "oauthUrl": "https://example.com/authorize"}
        ]
        assert handle.pop_pending_oauth_requests() == []
        assert "external-failed" in handle.mcp_session_report().payload()["failed"]
        assert "MCP server init failure on ready-session: external-failed" in caplog.text
        assert wire.sent.empty()
    finally:
        if start is not None and not start.done():
            start.cancel()
        if start is not None:
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_managed_readiness_keeps_external_wire_roster(kas_readiness_wire, resume):
    wire = kas_readiness_wire
    reader_task = await _start_reader(wire.runtime)
    start = None
    try:
        start = await wire.handshake(
            resume,
            injected=[
                {"name": "kirocrew-core"},
                {"name": "kirocrew-dashboard"},
                {"name": "external"},
            ],
        )
        await wire.take(wire.reads)
        wire.status("connected")
        wire.tags("kirocrew-core", "kirocrew-dashboard")
        handle = await await_until(start, what="the session start")
        report = handle.mcp_session_report().payload()
        assert set(report["configured"]) == {"kirocrew-core", "kirocrew-dashboard", "external"}
        assert "external" in report["failed"]
        assert set(report["ready"]) == {"kirocrew-core", "kirocrew-dashboard"}
    finally:
        if start is not None:
            if not start.done():
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        await _stop_reader(reader_task)


# ── The _await_routed helper itself ──


@pytest.mark.asyncio
async def test_await_routed_tolerates_a_driver_that_has_not_run_yet():
    """The helper must not depend on the driver having been scheduled.

    This is the exact condition that made the old
    ``await asyncio.sleep(0.05); req_id = rt._next_id - 1`` idiom flake on loaded
    Windows runners: the sleep expires, but the driver task has not yet reached
    ``send_request``, so ``_next_id`` has not advanced and the computed id
    belongs to no request. The test then feeds a response nothing is waiting for
    and fails later as an opaque ``TimeoutError``.

    Here the driver is deliberately never given a chance to run before the read,
    which is the worst case of that race.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        # No yield: the driver has definitely not sent anything yet, so the old
        # arithmetic would compute an id for a request that does not exist.
        assert rt._routed_requests == {}
        stale_id = rt._next_id - 1

        routed = await _await_routed(rt, "sA")
        assert routed["sA"] != stale_id, "the old idiom would have used a wrong id"
        assert rt._routed_requests[routed["sA"]] == "sA"

        _feed(reader, {"id": routed["sA"], "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_await_routed_reports_which_sessions_were_missing_on_timeout():
    """A timeout must name the sessions it waited for, not just time out.

    The old idiom failed indirectly, in an unrelated ``wait_for``; this keeps the
    diagnosis at the line that actually waited.
    """
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    with pytest.raises(AssertionError) as exc:
        await _await_routed(rt, "sA", timeout=0.05)
    assert "sA" in str(exc.value)
    assert "currently routed" in str(exc.value)


# ── Notification routing by sessionId ──


@pytest.mark.asyncio
async def test_notification_routed_to_named_session():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA", "x": 1}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        # The other session's queue must NOT have received it.
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_notification_for_unknown_session_is_dropped_not_broadcast():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # sessionId present but not registered → routed-by-id path misses; it
        # has a sessionId so it is NOT broadcast either.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
        await asyncio.sleep(0.05)
        assert q["sA"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_null_session_notification_broadcasts_to_all():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "some/global", "params": {}})  # no sessionId
        a = await _next(q["sA"])
        b = await _next(q["sB"])
        assert a.method == "some/global"
        assert b.method == "some/global"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_ownerless_request_answered_once_not_broadcast():
    """A server→client REQUEST with no sessionId gets exactly ONE -32601 reply.

    The runtime answers it once at connection level and never enqueues it. The
    broadcast branch is wrong here: every registered session's dispatch loop
    would classify it as server_request_unknown and each reply -32601 on the
    shared stdin — one request id, N responses.
    """
    rt, reader, proc = _make_runtime()
    q = _register(rt, "sA", "sB")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 4864, "method": "unknown/ownerless", "params": {}})
        # The answer task runs off the reader loop; give it ticks to complete.
        for _ in range(20):
            await asyncio.sleep(0)
        replies = [json.loads(call.args[0].decode()) for call in proc.stdin.write.call_args_list]
        errors = [r for r in replies if r.get("id") == 4864 and "error" in r]
        assert len(errors) == 1, f"expected exactly one reply, got {replies}"
        assert errors[0]["error"]["code"] == -32601
        # Not enqueued to ANY session — no dispatch loop ever sees it.
        assert q["sA"].empty()
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_permission_answer_waits_for_shared_answer_capacity_then_answers():
    """A temporary full cap delays, rather than drops, the next auto-answer.

    Drives the unroutable-permission answerer, the one caller of the shared
    admission wait. (It was written against KAS's credential callback, which was
    the second caller until kiro-cli's ACP relay took ownership of auth.)
    """
    rt, _reader, _ = _make_runtime()
    rt._max_answer_tasks = 1
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_capacity_check = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    capacity_checks = 0

    async def blocked_answer(msg, session_id, *, reason="x") -> None:
        del session_id, reason
        if msg.id == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
            await release_second.wait()

    wait_for_capacity = rt._wait_for_answer_capacity

    async def observed_capacity(*args, **kwargs) -> bool:
        nonlocal capacity_checks
        capacity_checks += 1
        if capacity_checks == 2:
            second_capacity_check.set()
        return await wait_for_capacity(*args, **kwargs)

    rt._answer_unroutable_permission = blocked_answer  # type: ignore[method-assign]
    rt._wait_for_answer_capacity = observed_capacity  # type: ignore[method-assign]
    try:
        await rt._spawn_answer_task(_permission_msg(1), "sA")
        await await_until(first_started, what="the first answer task starting")
        assert len(rt._answer_tasks) == 1

        second = asyncio.ensure_future(rt._spawn_answer_task(_permission_msg(2), "sA"))
        await await_until(second_capacity_check, what="the second answer task's capacity check")
        assert not second_started.is_set()

        release_first.set()
        await await_until(second_started, what="the second answer task starting")
        await second
        assert len(rt._answer_tasks) == 1
        assert sum(rt._dropped_frames.values()) == 0

        retained = next(iter(rt._answer_tasks))
        discarded = asyncio.Event()
        retained.add_done_callback(lambda _task: discarded.set())
        release_second.set()
        await await_until(discarded, what="the retained answer task being discarded")
        assert rt._answer_tasks == set()
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(*rt._answer_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_ownerless_response_with_null_result_is_not_answered():
    """An id-carrying frame with NO method is a response, not a request.

    A response whose result is null slips past the result/error routing check;
    it must not be mistaken for an ownerless request and answered -32601 —
    that would inject a spurious error reply for an id the backend owns.
    """
    rt, reader, proc = _make_runtime()
    _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 77, "result": None})  # response shape, no method
        for _ in range(20):
            await asyncio.sleep(0)
        replies = [json.loads(call.args[0].decode()) for call in proc.stdin.write.call_args_list]
        assert not [r for r in replies if r.get("id") == 77]
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_answer_cap_timeout_marks_runtime_dead_without_growth():
    """A wedged shared cap fails the runtime instead of losing a request."""
    rt, _reader, _ = _make_runtime()
    rt._max_answer_tasks = 1
    rt._answer_cap_wait_secs = 0.0
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    marked_dead = asyncio.Event()
    dead_reasons: list[str] = []

    async def blocked_answer(msg, session_id, *, reason="x") -> None:
        del session_id, reason
        if msg.id == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    def mark_dead(reason: str, **_kw: object) -> None:
        dead_reasons.append(reason)
        rt._dead = True
        marked_dead.set()

    rt._answer_unroutable_permission = blocked_answer  # type: ignore[method-assign]
    rt._mark_dead = mark_dead  # type: ignore[method-assign]
    try:
        await rt._spawn_answer_task(_permission_msg(1), "sA")
        await await_until(first_started, what="the first answer task starting")

        await rt._spawn_answer_task(_permission_msg(2), "sA")
        await await_until(marked_dead, what="the runtime being marked dead")

        assert not second_started.is_set()
        assert len(rt._answer_tasks) == 1
        assert sum(rt._dropped_frames.values()) == 0
        assert dead_reasons and "permission" in dead_reasons[0]
    finally:
        release_first.set()
        await asyncio.gather(*rt._answer_tasks, return_exceptions=True)


# ── Response routing by id ──


@pytest.mark.asyncio
async def test_awaited_response_resolves_pending_future():
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 7, "result": {"sessionId": "new1"}})
        result = await await_until(fut, what="the pending request's future")
        assert result == {"sessionId": "new1"}
        assert 7 not in rt._pending_requests
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_missing_agent_spec_error_reaches_caller_actionable(tmp_path):
    """A missing agent spec must not reach the caller as a raw -32603 dict.

    kiro-cli answers ``session/set_mode`` for an agent it cannot resolve with a
    bare "Internal error" whose data is ``Mode '<name>' not found``. Routed raw,
    the caller — and the dashboard chat bubble behind it — got the JSON-RPC dict
    verbatim: an internal ACP concept, no mention of the missing file, and no
    remedy, on a condition that fails every subsequent turn too.

    This pins the formatting AT THE CALL SITE rather than only unit-testing the
    helper: the awaited-request branch of the reader is the single path every
    handshake error (initialize / session/new / session/set_mode) takes, so a
    regression that unwires the helper is invisible to a helper-only test.
    """
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        with patch("kiro_crew.acp.runtime.kiro_agents_dir", return_value=tmp_path):
            _feed(
                reader,
                {
                    "id": 7,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": "Mode 'kirocrew' not found",
                    },
                },
            )
            with pytest.raises(AcpRuntimeError) as excinfo:
                await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)

    text = str(excinfo.value)
    assert "'kirocrew.json'" in text  # the file that is missing
    assert str(tmp_path) in text  # where it was looked for
    assert "kirocrew setup --agent-only`" in text  # the repair
    assert "--clean" not in text  # which would drop the operator's own config
    assert "-32603" not in text  # no raw protocol frame


@pytest.mark.asyncio
async def test_non_numeric_response_id_dropped_without_killing_demux():
    """The id in a response frame is agent-controlled. int("req-1") raised
    ValueError, which the reader's catch-all turned into _mark_dead — poisoning
    EVERY multiplexed session over one unmatched frame. The frame must be
    dropped and the reader must keep routing."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        # String / list / overflow ids: none int() coercible. json parses
        # 1e9999 to float("inf"), which raises OverflowError (not ValueError).
        _feed(reader, {"id": "req-1", "result": {"ok": True}})
        _feed(reader, {"id": [1], "error": {"code": -1}})
        reader.feed_data(b'{"id": 1e9999, "result": {}}\n')
        # The reader must still be alive: a valid response and a routed
        # notification must both be delivered after the bad frames.
        _feed(reader, {"id": 7, "result": {"sessionId": "new1"}})
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        result = await await_until(fut, what="the pending request's future")
        assert result == {"sessionId": "new1"}
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_numeric_string_response_id_still_coerced():
    """A digit-string id ("7") keeps working — it was int()-coerced before and
    must keep matching the pending int key after the guard."""
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[7] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": "7", "result": {"ok": 1}})
        result = await await_until(fut, what="the pending request's future")
        assert result == {"ok": 1}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_error_response_sets_exception_on_future():
    rt, reader, _ = _make_runtime()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rt._pending_requests[9] = fut
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 9, "error": {"code": -1, "message": "boom"}})
        with pytest.raises(AcpRuntimeError):
            await await_until(fut, what="the pending request's future")
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_routed_response_goes_to_session_queue():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._routed_requests[11] = "sA"
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 11, "result": {"stopReason": "end_turn"}})
        msg = await _next(q["sA"])
        assert msg.id == 11
        assert 11 not in rt._routed_requests
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unmatched_response_is_ignored():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"id": 999, "result": {}})  # no pending/routed entry
        await asyncio.sleep(0.05)
        assert q["sA"].empty()
        assert not rt._dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_json_line_is_skipped():
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(b"not json at all\n")
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"  # loop survived the bad line
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_object_json_line_does_not_crash_reader():
    """A valid-JSON but non-object line (bare scalar / array) must be skipped,
    not fed to JsonRpcMessage.from_dict (which would raise AttributeError and
    tear down EVERY multiplexed session on the shared runtime)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        for bad in (b"123\n", b'"a string"\n', b"[1, 2, 3]\n", b"true\n", b"null\n"):
            reader.feed_data(bad)
        # A well-formed frame after the bad lines must still route → reader alive.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
        assert not rt._dead  # reader never marked the runtime dead
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        # About 60 KB: under the reader's 64 KiB buffer, so the line reaches the
        # parser instead of being dropped as oversize.
        pytest.param(b"[" * 30_000 + b"]" * 30_000 + b"\n", id="nested-past-decoder-ceiling"),
        pytest.param(b"1" * 5_000 + b"\n", id="integer-past-digit-limit"),
        pytest.param(b"\x80 stray bytes\n", id="invalid-utf8"),
        pytest.param(b"\xff\xfe stray bytes\n", id="utf16-bom"),
    ],
)
async def test_line_json_loads_rejects_without_decode_error_does_not_crash_reader(bad):
    """A line that ``json.loads`` rejects with something other than
    ``json.JSONDecodeError`` is skipped like a non-JSON line.

    Nesting past the decoder's ceiling raises ``RecursionError``, an integer
    past the int-string digit limit a plain ``ValueError``, and undecodable
    bytes ``UnicodeDecodeError``. None of them may reach the loop's crash arm,
    which marks the runtime dead and ends every multiplexed session on it.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(bad)
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert not rt._dead, "one stray stdout line marked the shared runtime dead"
        assert msg.params["sessionId"] == "sA"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_frame_with_an_invalid_utf8_byte_in_a_string_is_still_routed():
    """An invalid byte inside a string value costs that character, not the frame:
    the reader decodes with replacement, as ``AcpClient._read_message`` does."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        reader.feed_data(
            b'{"method":"session/update","params":{"sessionId":"sA","note":"a\xffb"}}\n'
        )
        msg = await _next(q["sA"])
        assert not rt._dead, "one invalid byte marked the shared runtime dead"
        assert msg.params == {"sessionId": "sA", "note": "a\ufffdb"}
    finally:
        await _stop_reader(task)
