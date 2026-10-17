"""``AcpSessionHandle.drain_init`` and the first parity items with ``AcpClient``.

Steer notifications, the interrupted-tool completion, an unresponsive cancel, the
MCP-init drain at session start and the report it records, and the context-window
backfill from metadata.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _bounded,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    METHOD_SESSION_NEW,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)
from kiro_crew.testing.wait import await_until, default_timeout

# ── Drift-parity fixes (AcpRuntime ↔ AcpClient): #1-#4 + #5b ──


@pytest.mark.asyncio
async def test_steer_notifications_yield_steer_events():
    """steering_* session/update frames classify as "steer" and yield the
    EVENT_STEER_* events. Without a steer branch in classify_notification the
    shared demux path never surfaces mid-turn steer."""
    from kiro_crew.acp.types import (
        EVENT_STEER_CLEARED,
        EVENT_STEER_CONSUMED,
        EVENT_STEER_QUEUED,
    )

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=3.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_queued", "content": "please focus on X"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_consumed", "content": "focus on X"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "steering_cleared"},
                },
            },
        )
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")

        kinds = [e.kind for e in events]
        assert EVENT_STEER_QUEUED in kinds
        assert EVENT_STEER_CONSUMED in kinds
        assert EVENT_STEER_CLEARED in kinds
        queued = next(e for e in events if e.kind == EVENT_STEER_QUEUED)
        assert queued.text == "please focus on X"
        consumed = next(e for e in events if e.kind == EVENT_STEER_CONSUMED)
        assert consumed.text == "focus on X"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_tool_interrupted_marker_synthesizes_complete(monkeypatch):
    """#2: kiro-cli's security-filter marker (text-only, no `complete` response)
    must synthesize EVENT_COMPLETE so the turn does not hang until the 2h prompt
    timeout, and must emit the SEL audit. No prompt response is fed here — the
    turn MUST still terminate."""
    import kiro_crew.acp.session_handle as sh

    sel_mock = MagicMock()
    monkeypatch.setattr(sh, "sel", lambda: sel_mock)
    marker = "Tool uses were interrupted, waiting for the next user prompt"

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        # flake-ok: pre-existing 50 ms barrier for the prompt driver to send its request, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        # Only the marker text chunk — NO {"id": req_id, "result": ...} response.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": marker},
                    },
                },
            },
        )
        # Must finish WITHOUT the turn response (the synthesized complete ends it).
        await await_until(driver, what="the driver finishing its turn")

        assert events[-1].kind == EVENT_COMPLETE
        assert handle._turn_done.is_set()
        sel_mock.log_tool_invocation.assert_called_once()
        _kwargs = sel_mock.log_tool_invocation.call_args.kwargs
        assert _kwargs["outcome"] == "denied"
        assert _kwargs["tool_name"] == "kiro_cli_security_filter"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unresponsive_cancel_unblocks_without_killing_runtime():
    """#3: after cancel(), if kiro-cli never acks (no cancelled stopReason) within
    the grace budget, the dispatch loop synthesizes a terminal EVENT_COMPLETE so
    the caller unblocks — WITHOUT killing the shared runtime (send_notification is
    the only runtime call; no kill)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.kill = MagicMock()  # type: ignore[method-assign]  # must NOT be called
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        events: list = []

        async def drive():
            async for ev in handle.prompt("hi", timeout=5.0):
                events.append(ev)

        driver = asyncio.ensure_future(drive())
        # flake-ok: pre-existing 50 ms barrier for the prompt driver to send its request, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        await handle.cancel()
        # Backdate the cancel so the grace window has already elapsed.
        handle._cancel_ts = time.monotonic() - (handle._cancel_grace_secs + 1)
        # Wake the loop so it re-checks the cancel guard at the top of the while.
        _feed(reader, {"method": "_kiro.dev/metadata", "params": {"sessionId": "sA"}})
        await await_until(driver, what="the driver finishing its turn")

        assert events[-1].kind == EVENT_COMPLETE
        assert events[-1].stop_reason == "error: cancel unacked"
        assert handle._turn_done.is_set()
        rt.kill.assert_not_called()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_drain_init_consumes_init_frames_and_captures_config():
    """#1: drain_init() pulls MCP-init/config frames off the session queue after
    set_mode so they don't race into the first prompt, and captures
    config_option_update into cached configOptions."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    cfg = [{"id": "effort", "options": ["low", "high"]}]
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "config_option_update", "configOptions": cfg},
                },
            }
        )
    )
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/mcp/server_initialized",
                "params": {"sessionId": "sA", "serverName": "builder-mcp"},
            }
        )
    )
    await handle.drain_init(duration=0.5, idle_exit=0.05)
    assert q["sA"].empty()  # frames drained, not left for the first prompt
    assert handle._config_options == cfg


@pytest.mark.asyncio
async def test_drain_init_repoisons_on_dead_runtime():
    """#1: a None sentinel (runtime died during init) is re-queued so the next
    consumer still sees the death, and drain stops."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(None)
    await handle.drain_init(duration=0.5, idle_exit=0.05)
    assert q["sA"].get_nowait() is None  # sentinel preserved


def _mcp_initialized_frame(session_id: str, server: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_initialized",
            "params": {"sessionId": session_id, "serverName": server},
        }
    )


def _metadata_frame(session_id: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/metadata",
            "params": {"sessionId": session_id, "contextUsagePercentage": 1.0},
        }
    )


def _mcp_failure_frame(session_id: str, server: str, error: str) -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "method": "_kiro.dev/mcp/server_init_failure",
            "params": {"sessionId": session_id, "serverName": server, "error": error},
        }
    )


@pytest.mark.asyncio
async def test_drain_init_records_the_session_mcp_report():
    """Registration frames the drain consumes become this session's report.

    Parity with AcpClient._drain_notifications: without this the frames are
    consumed and the session has no way to say which servers started.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "kirocrew-core"))
    q["sA"].put_nowait(_mcp_failure_frame("sA", "slack-mcp", "spawn ENOENT"))
    q["sA"].put_nowait(_metadata_frame("sA"))

    await handle.drain_init(duration=0.5, idle_exit=0.05)

    payload = handle.mcp_session_report().payload()
    assert payload is not None
    assert payload["ready"] == ["kirocrew-core"]
    assert payload["failed"] == ["slack-mcp"]
    assert payload["failures"] == {"slack-mcp": "spawn ENOENT"}


@pytest.mark.asyncio
async def test_drain_init_skips_a_stale_backlog_report():
    """A pre-switch backlog frame is drained but NOT credited to this report.

    ``stale_report_frames`` names how many already-queued frames are the PRE-switch
    agent's roster. Recording one would show a server the current agent does not
    have as mounted here; leaving it out only understates, which the report
    renders as "no report" rather than "not mounted".
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    await handle.drain_init(
        duration=0.2, idle_exit=0.01, no_report_ceiling=0.05, stale_report_frames=1
    )

    assert q["sA"].empty()  # still drained, as the arming docstring promises
    assert handle.mcp_session_report().payload() is None


@pytest.mark.asyncio
async def test_drain_init_refuses_a_sessionless_frame_on_a_lone_runtime():
    """A frame naming no session is not this session's, even when unmarked.

    The runtime sets ``fanout_no_owner`` only once MORE THAN ONE queue is
    registered — right for the idle-stall clock, which may treat a lone session
    as the sole owner of whatever arrives, but not for a view that publishes
    server names as "what THIS session mounted". A co-tenant that emits a
    sessionless frame before registering its own queue would otherwise have its
    server attributed here.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    frame = _mcp_initialized_frame("sA", "a-co-tenants-server")
    frame.params.pop("sessionId", None)
    assert frame.fanout_no_owner is False, "the lone-queue case leaves it clear"
    q["sA"].put_nowait(frame)

    await handle.drain_init(duration=0.2, idle_exit=0.01, no_report_ceiling=0.05)

    assert handle.mcp_session_report().payload() is None


@pytest.mark.asyncio
async def test_drain_init_credits_the_active_agent_when_the_queue_never_empties():
    """A refilled queue must not extend the stale backlog past its own depth.

    The original defect: exhaustion was detected by the queue going EMPTY, so an
    active agent that refilled it before the backlog drained kept the flag set
    for the rest of the drain and had every one of its reports skipped — a
    session stuck at "no report" for as long as its servers kept talking.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    async def _refill_before_the_backlog_drains() -> None:
        # Lands while the drain is awaiting its first get(), so the queue is
        # already non-empty again when the second iteration begins.
        await asyncio.sleep(0)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    feeder = asyncio.create_task(_refill_before_the_backlog_drains())
    try:
        await handle.drain_init(
            duration=0.3, idle_exit=0.05, no_report_ceiling=0.5, stale_report_frames=1
        )
    finally:
        await feeder

    payload = handle.mcp_session_report().payload()
    assert payload is not None, "the active agent's report was swallowed as stale"
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_drain_init_credits_a_report_that_landed_before_set_mode_answered():
    """A count measured at drain time would swallow the active agent's own report.

    The original defect: the pre-switch depth was read inside ``drain_init``,
    i.e. AFTER set_mode returned. kiro-cli can emit the switched-to agent's
    registrations before it answers, so those frames were already queued by then
    and got counted as pre-switch — consumed without being recorded, leaving a
    false "no report" panel. The caller measures before it sends instead, so a
    frame that arrives during the request is credited.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    # What the caller reads BEFORE sending set_mode: one staged frame.
    staged = handle.queued_frame_count()
    assert staged == 1
    # The switched-to agent registers while set_mode is still in flight.
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    await handle.drain_init(
        duration=0.2, idle_exit=0.01, no_report_ceiling=0.05, stale_report_frames=staged
    )

    payload = handle.mcp_session_report().payload()
    assert payload is not None, "the active agent's report was swallowed as pre-switch"
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_drain_init_records_a_post_backlog_report_after_a_switch():
    """After the stale backlog is exhausted, the active agent's own report counts."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "previous-agent-server"))

    async def _after_switch() -> None:
        # flake-ok: pre-existing 50 ms delay before the feeder queues the post-switch report, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the feeder's own delay, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "current-agent-server"))

    feeder = asyncio.create_task(_after_switch())
    try:
        await handle.drain_init(
            duration=0.3, idle_exit=0.05, no_report_ceiling=0.5, stale_report_frames=1
        )
    finally:
        await feeder

    payload = handle.mcp_session_report().payload()
    assert payload is not None
    assert payload["ready"] == ["current-agent-server"]


@pytest.mark.asyncio
async def test_create_session_records_the_wire_roster(monkeypatch):
    """The report carries the roster session/new actually sent.

    A different fact from the agent spec on disk, which is what the dashboard's
    other MCP views read.
    """
    rt, _, _ = _make_runtime()

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-roster"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    servers = [{"name": "kirocrew-core", "command": "x"}, {"name": "kirocrew-cron"}]

    handle = await rt.create_session(cwd="/w", agent="kirocrew", mcp_servers=servers)

    assert handle.mcp_session_report().configured == ("kirocrew-core", "kirocrew-cron")


@pytest.mark.asyncio
async def test_drain_init_waits_past_idle_window_for_first_mcp_report(monkeypatch):
    """The idle shortcut is not eligible before the first MCP
    registration frame. A server that stays silent past the idle window and
    THEN reports is still observed — non-MCP frames (metadata) that arrive
    immediately after set_mode must not arm the shortcut either."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 5.0, raising=False)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # A non-MCP frame is already queued (kiro-cli emits metadata right after
    # set_mode); it must be consumed without arming the idle exit.
    q["sA"].put_nowait(_metadata_frame("sA"))

    async def _late_report() -> None:
        # Many idle windows of silence before the server finally reports.
        # flake-ok: pre-existing 150 ms delay that outlasts several idle windows before the late report, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the feeder's own delay, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.15)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "slow-npx"))

    feeder = asyncio.create_task(_late_report())
    try:
        await handle.drain_init(duration=0.2, idle_exit=0.01)
    finally:
        await feeder
    # The late report was drained rather than left to race into the first turn.
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_no_reports_returns_at_ceiling():
    """A drain that never sees an MCP report returns at the no-report
    ceiling instead of hanging (bounded even when servers are dead or absent)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_metadata_frame("sA"))  # non-MCP traffic doesn't extend it
    # The outer bound is the hang guard (half the test's timeout): it only
    # names a genuine unbounded wait, never a slow shard.
    await _bounded(
        handle.drain_init(duration=0.05, idle_exit=0.01, no_report_ceiling=0.2),
        what="the init drain returning at its no-report ceiling",
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_idle_exit_stays_prompt_after_first_report():
    """Once a report has been seen, a subsequent idle gap still exits
    promptly — the warm path must not degrade into full-ceiling waits. The
    ceilings sit far past the wait's own lost-run bound, so completing inside
    it proves the idle shortcut (not a ceiling) ended the drain: with the
    shortcut broken the drain holds to its ceiling and the wait fails by name."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "fast-server"))
    far = 10 * default_timeout()
    await _bounded(
        handle.drain_init(duration=far, idle_exit=0.02, no_report_ceiling=far),
        what="the init drain exiting on its idle gap",
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_drain_init_zero_ceiling_keeps_idle_exit_active_from_start():
    """no_report_ceiling=0.0 (MCP-free runtime opt-out) keeps idle exit
    active before any report, so an empty
    queue exits after one idle window instead of holding for a first report."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(_metadata_frame("sA"))
    # duration sits far past the wait's lost-run bound: completing inside it
    # proves the idle shortcut ended the drain despite zero reports, and a
    # broken shortcut fails the wait by name instead of ending at the deadline.
    await _bounded(
        handle.drain_init(duration=10 * default_timeout(), idle_exit=0.02, no_report_ceiling=0.0),
        what="the init drain exiting on its idle gap",
    )
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_mcp_free_runtime_skips_no_report_ceiling(monkeypatch):
    """A runtime constructed with expect_mcp_reports=False passes the
    zero ceiling to drain_init, so its sessions never hold for a report."""
    rt = AcpRuntime(work_dir="/tmp", expect_mcp_reports=False)
    rt._initialized = True
    proc = MagicMock()
    proc.returncode = None
    # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the fake process, moved verbatim; tracked for a de-flake pass
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-lite"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    seen: dict = {}
    orig = AcpSessionHandle.drain_init

    async def _spy(self, *args, **kwargs):
        seen.update(kwargs)
        await orig(self, *args, **kwargs)

    with patch.object(AcpSessionHandle, "drain_init", _spy):
        await rt.create_session(cwd="/w", agent="kirocrew-lite", mcp_servers=[])
    assert seen.get("no_report_ceiling") == 0.0


@pytest.mark.asyncio
async def test_drain_init_ignores_pre_switch_reports_still_waits_for_new_agent(monkeypatch):
    """On a shared runtime, session/new initializes the
    PARENT mode's servers; their staged registration frames must not arm the
    idle shortcut for a session that was then mode-SWITCHED — the switched-to
    agent's own slow server, reporting after set_mode, must still be observed."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 5.0, raising=False)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Staged during session/new: the pre-switch agent's roster.
    q["sA"].put_nowait(_mcp_initialized_frame("sA", "parent-mode-server"))
    q["sA"].put_nowait(_metadata_frame("sA"))

    async def _late_report() -> None:
        # The switched-to agent's server reports well past the idle window.
        # flake-ok: pre-existing 150 ms delay that outlasts several idle windows before the late report, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the feeder's own delay, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.15)
        q["sA"].put_nowait(_mcp_initialized_frame("sA", "subagent-slow-npx"))

    feeder = asyncio.create_task(_late_report())
    try:
        await handle.drain_init(duration=0.2, idle_exit=0.01, stale_report_frames=1)
    finally:
        await feeder
    # Without the stale-backlog gate, the staged parent report arms the idle
    # exit and the drain returns before the late report — leaving it queued.
    assert q["sA"].empty()


@pytest.mark.asyncio
async def test_reader_retains_mcp_registration_frames_during_init():
    """server_initialized / init_failure emitted before the session/new
    response are staged (like OAuth) and handed to the new session's queue, so
    drain_init() sees warm servers' reports and arms its idle shortcut."""
    rt, reader, _ = _make_runtime()
    task = asyncio.create_task(rt._reader_loop())
    try:

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                # Frames arrive while session/new is in flight — before the
                # queue can be registered under the not-yet-known session id.
                _feed(
                    reader,
                    {
                        "method": "_kiro.dev/mcp/server_initialized",
                        "params": {"sessionId": "sid-warm", "serverName": "core"},
                    },
                )
                _feed(
                    reader,
                    {
                        "method": "_kiro.dev/mcp/server_init_failure",
                        "params": {"sessionId": "sid-warm", "serverName": "broken"},
                    },
                )
                # flake-ok: pre-existing 50 ms barrier for the reader to route the fed frames, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)  # let the reader route them
                return {"sessionId": "sid-warm"}
            return {}

        with patch.object(rt, "_send_and_await", _fake_send):
            with patch.object(AcpSessionHandle, "drain_init", AsyncMock()) as mock_drain:
                handle = await rt.create_session(cwd="/w", agent="kirocrew", mcp_servers=[])
        assert handle.session_id == "sid-warm"
        mock_drain.assert_awaited_once()
        # Both registration frames were transferred into the session queue
        # (this is what drain_init would consume to arm its idle shortcut).
        methods = []
        while not handle._queue.empty():
            frame = handle._queue.get_nowait()
            assert frame is not None
            methods.append(frame.method)
        assert methods == [
            "_kiro.dev/mcp/server_initialized",
            "_kiro.dev/mcp/server_init_failure",
        ]
        # Staging area was emptied by the transfer.
        assert not rt._pending_init_notifications
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_backfill_context_window_from_pct(monkeypatch):
    """#5b: pct-only metadata (kiro 2.10+) backfills window/used tokens from the
    model registry; no-op once a real usage_update set the window."""
    import kiro_crew.acp.session_handle as sh

    # The backfill only fires for a KNOWN window (has_known_window) and resolves
    # via the central model_window authority, so mock both for the fake model.
    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 200000)
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._model = "some-model"
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 25},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 25.0
    assert handle.last_prompt_stats.context_window_tokens == 200000
    assert handle.last_prompt_stats.context_used_tokens == 50000

    # A prior real usage_update wins — metadata must override neither the
    # window NOR the token-derived pct (else the headline % desyncs from the
    # "used / total" token text shown in the dashboard popover).
    handle2 = AcpSessionHandle("sA", q["sA"], rt)
    handle2._model = "some-model"
    handle2.last_prompt_stats.context_pct = 40.8
    handle2.last_prompt_stats.context_used_tokens = 408000
    handle2.last_prompt_stats.context_window_tokens = 999
    handle2.last_prompt_stats.context_tokens_from_usage = True
    handle2._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 80},
            }
        )
    )
    assert handle2.last_prompt_stats.context_window_tokens == 999
    assert handle2.last_prompt_stats.context_pct == 40.8
    assert handle2.last_prompt_stats.context_used_tokens == 408000


def test_session_handle_usage_update_sets_flag_and_metadata_cannot_clobber():
    """SessionHandle parity with AcpClient: a real usage_update through
    _handle_update sets context_tokens_from_usage, and a later metadata
    contextUsagePercentage must not clobber the token-derived pct (the
    408K/1000K-vs-73% desync on the shared-runtime path)."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._handle_update(
        JsonRpcMessage.from_dict(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "usage_update",
                        "used": 408000,
                        "size": 1000000,
                    }
                },
            }
        )
    )
    assert handle.last_prompt_stats.context_tokens_from_usage is True
    assert handle.last_prompt_stats.context_pct == 40.8
    assert handle.last_prompt_stats.context_used_tokens == 408000
    assert handle.last_prompt_stats.context_window_tokens == 1000000

    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 73},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 40.8  # NOT clobbered to 73
    assert handle.last_prompt_stats.context_used_tokens == 408000


def test_backfill_context_window_clamps_malformed_pct(monkeypatch):
    """A degenerate metadata percentage (huge finite / inf / NaN) must not
    overflow round() and abort the turn on the shared-runtime path; derived
    used stays in [0, window]."""
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh.model_registry, "has_known_window", lambda mid: True)
    monkeypatch.setattr(sh.model_registry, "model_window", lambda mid, **kw: 200000)
    for bad in (1e308, float("inf"), float("nan")):
        rt, _, _ = _make_runtime()
        q = _register(rt, "sA")
        handle = AcpSessionHandle("sA", q["sA"], rt)
        handle._model = "some-model"
        # Must not raise OverflowError/ValueError.
        handle._track_metadata(
            JsonRpcMessage.from_dict(
                {
                    "method": "_kiro.dev/metadata",
                    "params": {"contextUsagePercentage": bad},
                }
            )
        )
        used = handle.last_prompt_stats.context_used_tokens
        assert 0 <= used <= 200000
        # context_pct is sanitized at the source, never left non-finite.
        pct = handle.last_prompt_stats.context_pct
        assert 0.0 <= pct <= 100.0


def test_backfill_context_window_no_model_is_safe(monkeypatch):
    """#5b: no _model set → records pct only, no crash, no token backfill."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._track_metadata(
        JsonRpcMessage.from_dict(
            {
                "method": "_kiro.dev/metadata",
                "params": {"contextUsagePercentage": 30},
            }
        )
    )
    assert handle.last_prompt_stats.context_pct == 30.0
    assert handle.last_prompt_stats.context_window_tokens == 0
