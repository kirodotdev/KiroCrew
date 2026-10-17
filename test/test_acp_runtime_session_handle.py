"""The ``AcpSessionHandle`` surface over a shared ``AcpRuntime``.

Send paths, handle teardown and termination, recycling (``_is_stale``), the
headline multi-session routing test, the handle API (prompt, approve and reject,
``set_mode``, ``set_model``), responses and errors, prompt-turn errors, and the
handle's command and state methods.
"""

import asyncio
import json
import os
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
    _next,
    _register,
    _replying_send,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import (
    _TERMINATE_TIMEOUT,
    AcpRuntime,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpSessionHandle,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    METHOD_COMMANDS_EXECUTE,
    METHOD_SESSION_TERMINATE,
    METHOD_SESSION_UPDATE,
    METHOD_SET_CONFIG_OPTION,
    JsonRpcMessage,
)
from kiro_crew.testing.wait import await_until

# ── Send paths ──


@pytest.mark.asyncio
async def test_send_request_registers_routing_and_increments_id():
    rt, _, proc = _make_runtime()
    _register(rt, "sA")
    rid = await rt.send_request("session/prompt", {"sessionId": "sA", "prompt": []})
    assert rt._routed_requests[rid] == "sA"
    # The next id advances.
    rid2 = await rt.send_request("session/prompt", {"sessionId": "sA"})
    assert rid2 != rid
    # Wire payload carries the id + method.
    sent = proc.stdin.write.call_args_list[0].args[0].decode()
    frame = json.loads(sent)
    assert frame["id"] == rid and frame["method"] == "session/prompt"
    proc.stdin.drain.assert_awaited()


@pytest.mark.asyncio
async def test_send_request_without_session_does_not_register_routing():
    rt, _, _ = _make_runtime()
    rid = await rt.send_request("initialize", {})  # no sessionId
    assert rid not in rt._routed_requests


@pytest.mark.asyncio
async def test_send_notification_has_no_id_and_no_routing():
    rt, _, proc = _make_runtime()
    _register(rt, "sA")
    before_id = rt._next_id
    await rt.send_notification("session/cancel", {"sessionId": "sA"})
    sent = proc.stdin.write.call_args_list[0].args[0].decode()
    frame = json.loads(sent)
    assert frame["method"] == "session/cancel"
    assert "id" not in frame  # notification: no id allocated
    assert rt._next_id == before_id  # id space untouched
    assert not rt._routed_requests  # nothing to leak


@pytest.mark.asyncio
async def test_send_request_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_request("session/prompt", {"sessionId": "sA"})


# ── AcpSessionHandle behaviour ──


@pytest.mark.asyncio
async def test_handle_destroy_terminates_and_unregisters_session():
    """destroy() must evict the session on kiro-cli via _kiro.dev/session/terminate
    (freeing its transcript/context in the shared multiplexed process) AND
    unregister the local queue. A local-only unregister would leak the session
    in kiro-cli's in-memory map for the process's whole lifetime — the
    background-runtime unbounded-RSS bug this fix closes."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.destroy()
    # kiro-cli was told to terminate exactly this session.
    rt._send_and_await.assert_awaited_once()
    assert rt._send_and_await.call_args.args[0] == METHOD_SESSION_TERMINATE
    assert rt._send_and_await.call_args.args[1] == {"sessionId": "sA"}
    # Local queue also unregistered.
    assert "sA" not in rt._session_queues


@pytest.mark.asyncio
async def test_terminate_session_sends_bounded_terminate_for_target_only():
    """terminate_session issues _kiro.dev/session/terminate for exactly the
    target sessionId with a bounded timeout (teardown can't stall on an
    unresponsive runtime), and unregisters ONLY that session — a co-tenant
    session on the shared runtime is untouched (unlike kill())."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA", "sB")
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    await rt.terminate_session("sA")
    rt._send_and_await.assert_awaited_once()
    assert rt._send_and_await.call_args.args[0] == METHOD_SESSION_TERMINATE
    assert rt._send_and_await.call_args.args[1] == {"sessionId": "sA"}
    assert rt._send_and_await.call_args.kwargs["timeout"] == _TERMINATE_TIMEOUT
    assert "sA" not in rt._session_queues
    assert "sB" in rt._session_queues  # co-tenant survives


@pytest.mark.asyncio
async def test_terminate_session_is_best_effort_when_send_fails():
    """If the terminate request fails (runtime slow/dead), teardown must NOT
    raise and MUST still unregister locally (incl. routed-request cleanup) so
    the reader stops routing to an abandoned queue."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._routed_requests[5] = "sA"
    rt._send_and_await = AsyncMock(side_effect=AcpRuntimeError("timed out"))  # type: ignore[method-assign]
    await rt.terminate_session("sA")  # must not raise
    assert "sA" not in rt._session_queues
    assert 5 not in rt._routed_requests


@pytest.mark.asyncio
async def test_terminate_session_skips_roundtrip_when_dead():
    """A dead runtime already freed the session's memory with the process, so
    terminate skips the doomed round-trip but still unregisters locally."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._dead = True
    rt._send_and_await = AsyncMock()  # type: ignore[method-assign]
    await rt.terminate_session("sA")
    rt._send_and_await.assert_not_awaited()
    assert "sA" not in rt._session_queues


@pytest.mark.asyncio
async def test_terminate_session_unregisters_even_on_cancellation():
    """If the terminate await is cancelled, the local unregister MUST still run.
    asyncio.CancelledError is a BaseException (not Exception in 3.9+), so it slips
    past the inner `except Exception`; the `finally` guarantees local cleanup so
    the reader loop stops routing to an abandoned queue. The cancellation itself
    still propagates (finally does not swallow it)."""
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._send_and_await = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await rt.terminate_session("sA")
    assert "sA" not in rt._session_queues


# ── _is_stale / has_active_sessions ──


@pytest.mark.asyncio
async def test_is_stale_none_when_fresh_and_small(monkeypatch):
    """A freshly-spawned runtime is not stale; the RSS probe is skipped
    entirely because it is younger than the age band."""
    rt, _, _ = _make_runtime()
    rt._spawn_monotonic = time.monotonic()  # just spawned
    rt._max_rss_mb = 500.0
    called = {"n": 0}

    def _boom(pid):
        called["n"] += 1
        return 999999.0  # would be "stale" if ever consulted

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _boom)
    assert await rt._is_stale() is None
    assert called["n"] == 0  # young runtime never probes RSS


@pytest.mark.asyncio
async def test_is_stale_none_when_old_but_small_rss(monkeypatch):
    """Past the age band but below the RSS threshold → not stale. Exercises the
    small-RSS branch with a concrete value (not the None lookup-failure path)."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0  # older than the probe band
    rt._max_rss_mb = 500.0
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: 10.0)
    assert await rt._is_stale() is None


@pytest.mark.asyncio
async def test_is_stale_age_when_past_max_age():
    """A runtime older than _max_age_secs is stale with reason 'age'."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 10.0
    rt._spawn_monotonic = time.monotonic() - 20.0
    assert await rt._is_stale() == "age"


@pytest.mark.asyncio
async def test_is_stale_rss_when_tree_over_threshold(monkeypatch):
    """Past the age band and RSS tree over threshold → stale with reason 'rss'."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0  # old enough to probe
    rt._max_rss_mb = 100.0
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: 250.0)
    assert await rt._is_stale() == "rss"


@pytest.mark.asyncio
async def test_the_declared_reclaim_scope_reaches_the_probe(monkeypatch):
    """A harness that bounds its RSS scope must have that bound actually applied.

    The ceiling and the scope are one decision: applied without its scope, a
    core-only ceiling is judged against a whole-subtree measurement, which for a
    host whose subtree is dominated by a per-session fleet reads as a leak on the
    first session and recycles a healthy process. Pinned on the ARGUMENT the probe
    receives, because a policy field that is stored and never passed is exactly the
    failure that looks correct in the policy object.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 1024.0
    rt._max_rss_depth = 1
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 250.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() is None
    assert seen == [1]


@pytest.mark.asyncio
async def test_an_unbounded_scope_is_the_default_and_is_passed_as_such(monkeypatch):
    """Every kiro-family host measures the whole subtree, and must keep doing so."""
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 100.0
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 250.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() == "rss"
    assert seen == [None]


def test_a_forking_sandbox_backend_adds_one_generation():
    """``self._pid`` is the launcher there, not the adapter the harness counts from.

    The probe is patched in the module that CALLS it, not in ``kiro_crew.sandbox``:
    the harness binds the name at import, so patching the definition site leaves the
    real backend probe in place and the assertion reads this host instead of the case.
    """
    from kiro_crew.acp.harness.codex import _sandbox_wrapper_generations

    with patch("kiro_crew.acp.harness.codex.detect_backend", return_value="namespace"):
        assert _sandbox_wrapper_generations("standard") == 1


@pytest.mark.parametrize("backend", ["sandbox-exec", "none"])
def test_an_execing_or_absent_backend_adds_none(backend):
    """Both leave the adapter AS ``self._pid``, so a declared depth is already right."""
    from kiro_crew.acp.harness.codex import _sandbox_wrapper_generations

    with patch("kiro_crew.acp.harness.codex.detect_backend", return_value=backend):
        assert _sandbox_wrapper_generations("standard") == 0


def test_a_failed_probe_answers_zero_and_can_only_under_count():
    """Fail-safe direction: an offset too small reaches the ceiling late, never early."""
    from kiro_crew.acp.harness.codex import _sandbox_wrapper_generations

    with patch("kiro_crew.acp.harness.codex.detect_backend", side_effect=OSError("boom")):
        assert _sandbox_wrapper_generations("standard") == 0


def test_the_spawn_path_does_not_branch_on_rss_depth():
    """H13: a host-specific RSS scope adds no conditional to the shared spawn."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(AcpRuntime._spawn_admitted)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        attributes = {
            child.attr for child in ast.walk(node.test) if isinstance(child, ast.Attribute)
        }
        assert attributes.isdisjoint({"rss_depth", "_max_rss_depth"})


@pytest.mark.asyncio
async def test_a_bounded_scope_is_offset_by_the_launcher_generation(monkeypatch):
    """The bug this pins: a launcher counted as the adapter hides the growing child.

    Under a forking backend the tree is launcher -> adapter -> app-server, so a
    harness declaring "the adapter and its direct children" needs depth 2 measured
    from ``self._pid``. Applied unoffset, the sum stops at the adapter -- which is
    the FLAT process -- and the ceiling never sees the child that actually grows, so
    the leak detector reads healthy forever.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 1024.0
    rt._max_rss_depth = 2
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 100.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() is None
    assert seen == [2]


@pytest.mark.asyncio
async def test_the_offset_does_not_touch_an_unbounded_scope(monkeypatch):
    """An extra generation at the top changes nothing when the whole subtree is summed.

    So the offset must stay out of the kiro-family answer entirely rather than being
    added and then ignored -- a None that arrives as an integer would silently bound
    a measurement nothing asked to bound.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 100.0
    rt._max_rss_depth = None
    seen: list[object] = []

    def _probe(pid, depth=None):
        seen.append(depth)
        return 250.0

    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", _probe)
    assert await rt._is_stale() == "rss"
    assert seen == [None]


@pytest.mark.asyncio
async def test_an_unreadable_bounded_measurement_does_not_recycle(monkeypatch):
    """None is "unknown, do not judge" -- the platform without a bounded walk.

    Answering with a subtree total there would apply a bounded host's ceiling to an
    unbounded measurement. Abstaining leaves the age ceiling governing, which is why
    a None must not read as a breach.
    """
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 6 * 3600
    rt._spawn_monotonic = time.monotonic() - 600.0
    rt._max_rss_mb = 1.0
    rt._max_rss_depth = 1
    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_tree_mb", lambda pid, depth=None: None)
    assert await rt._is_stale() is None


@pytest.mark.asyncio
async def test_is_stale_none_when_no_pid():
    rt, _, _ = _make_runtime()
    rt._pid = None
    assert await rt._is_stale() is None


def test_stale_by_age_cheap_check():
    rt, _, _ = _make_runtime()
    rt._max_age_secs = 10.0
    rt._spawn_monotonic = time.monotonic() - 20.0
    assert rt._stale_by_age() is True
    rt._spawn_monotonic = time.monotonic()
    assert rt._stale_by_age() is False
    rt._pid = None
    assert rt._stale_by_age() is False


def test_get_rss_mb_real_process():
    """_get_rss_mb parses a real process (this test process) and returns a
    positive MiB value; a nonexistent PID returns None. Skips where the
    platform can't introspect RSS (no /proc AND ps blocked, e.g. a locked-down
    macOS sandbox) — _get_rss_mb returns None there by design."""
    from kiro_crew.acp.runtime import _get_rss_mb

    rss = _get_rss_mb(os.getpid())
    if rss is None:
        pytest.skip("RSS introspection unavailable in this environment")
    assert rss > 0.0
    assert _get_rss_mb(2**31 - 1) is None  # nonexistent pid


def test_get_rss_tree_mb_real_process():
    """The real tree probe returns a positive sample for this process and None
    for a nonexistent PID.

    Do not compare this sample with a separate single-process RSS read: resident
    sets are live values and Windows may trim the working set between the two
    calls.  The deterministic root-plus-descendants arithmetic is covered with
    fixed values in ``test_platform_compat_coverage_metrics.py``.
    """
    from kiro_crew.acp.runtime import _get_rss_tree_mb

    tree = _get_rss_tree_mb(os.getpid())
    if tree is None:
        pytest.skip("RSS introspection unavailable in this environment")
    assert tree > 0.0
    assert _get_rss_tree_mb(2**31 - 1) is None


@pytest.mark.asyncio
async def test_has_active_sessions_false_when_empty():
    rt, _, _ = _make_runtime()
    assert rt.has_active_sessions() is False


@pytest.mark.asyncio
async def test_has_active_sessions_true_when_registered():
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    assert rt.has_active_sessions() is True


@pytest.mark.asyncio
async def test_handle_cancel_uses_notification():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    rt.send_notification = AsyncMock()  # type: ignore[method-assign]
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.cancel()
    rt.send_notification.assert_awaited_once()
    assert rt.send_notification.call_args.args[0] == "session/cancel"
    assert handle._cancelled is True


@pytest.mark.asyncio
async def test_concurrent_prompt_on_same_handle_rejected():
    """A second ``prompt()`` on a handle whose turn is in flight must refuse.

    The first turn is only "in flight" once ``_run_turn`` has passed its
    ``_turn_done`` guard, and that happens AFTER two awaits the driver task has
    to get through first (``_effective_prompt_timeout_async`` and the
    ``to_thread`` prompt build). A fixed ``sleep(0.05)`` was a guess at how long
    those take; on a loaded Windows runner the guess lost, the second prompt
    passed the guard too, and then waited on a completion this test never feeds
    -- with ``timeout=None`` resolving to the multi-hour dashboard ceiling. That
    is not a failure, it is a hang: pytest-timeout kills the xdist worker, and
    with ``--max-worker-restart=0`` the whole run aborts (observed in 2 of 5
    full runs). ``_await_routed`` waits on the observable fact instead -- the
    request exists in ``_routed_requests`` -- and the second prompt carries a
    bounded timeout so a missed rejection fails at this line, loudly.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        # First turn is in-flight (no completion fed) -- _turn_done stays clear.
        first = asyncio.ensure_future(handle.prompt("hello").__anext__())
        await _await_routed(rt, "sA")
        assert not handle._turn_done.is_set(), "first turn must be marked active"
        # A second prompt on the same handle must refuse rather than corrupt state.
        # The bound only matters if the guard is broken: then the wait fails by
        # name instead of blocking the worker.
        with pytest.raises(AcpRuntimeError):
            await _bounded(
                handle.prompt("again", timeout=1.0).__anext__(),
                what="the second prompt's refusal",
            )
        first.cancel()
        try:
            await first
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await _stop_reader(task)


# ── Headline: one runtime, many sessions, correct routing ──


@pytest.mark.asyncio
async def test_multiple_sessions_routed_independently():
    """Two concurrent prompt turns on ONE runtime each receive only their own
    session's text chunk and completion — proving sessionId demux isolates them.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    handle_a = AcpSessionHandle("sA", q["sA"], rt)
    handle_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    out_a: list = []
    out_b: list = []

    async def drive(handle, out):
        async for ev in handle.prompt("go", timeout=5.0):
            out.append(ev)

    da = asyncio.ensure_future(drive(handle_a, out_a))
    db = asyncio.ensure_future(drive(handle_b, out_b))
    try:
        # Let both turns issue their session/prompt requests and register routing.
        sid_to_req = await _await_routed(rt, "sA", "sB")
        assert set(sid_to_req) == {"sA", "sB"}, "both prompts must be in flight"

        # Interleave text chunks for the two sessions (out of order on purpose).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "Bravo"},
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "Alpha"},
                },
            },
        )
        # Complete each turn via its own prompt response (routed by id).
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})

        await await_until(asyncio.gather(da, db), what="every driver finishing its turn")

        text_a = "".join(e.text for e in out_a if e.kind == EVENT_TEXT_CHUNK)
        text_b = "".join(e.text for e in out_b if e.kind == EVENT_TEXT_CHUNK)
        assert text_a == "Alpha"
        assert text_b == "Bravo"
        # Cross-talk check: neither session saw the other's text.
        assert "Bravo" not in text_a
        assert "Alpha" not in text_b
        # Each turn ended with its own EVENT_COMPLETE.
        assert any(e.kind == EVENT_COMPLETE for e in out_a)
        assert any(e.kind == EVENT_COMPLETE for e in out_b)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


# ── AcpSessionHandle API method tests ──


@pytest.mark.asyncio
async def test_handle_session_id_property():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    assert handle.session_id == "sA"


@pytest.mark.asyncio
async def test_handle_is_turn_active():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Freshly created — no active turn
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_send_request_fails():
    """If send_request raises after _turn_done is cleared (e.g. AcpRuntimeDead on
    a broken pipe), the handle must NOT stay stuck as turn-active — otherwise
    every subsequent prompt() is permanently rejected with 'turn already active'."""
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(side_effect=AcpRuntimeDead("broken pipe"))

    gen = handle.prompt("hi", timeout=3.0)
    with pytest.raises(AcpRuntimeDead):
        await gen.__anext__()  # send_request fires on first iteration

    # Turn is not active, so the handle is reusable.
    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_cancelled():
    """Same guard, but for cancellation — which is NOT an ``Exception``.

    ``asyncio.CancelledError`` derives from ``BaseException``, so an
    ``except Exception`` guard lets it through and leaves ``_turn_done`` cleared
    forever: ``is_turn_active`` reports True permanently and every later
    ``prompt()`` on the handle is rejected as already active. A turn timing out
    or being cancelled is routine, so this must recover.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(side_effect=asyncio.CancelledError())

    gen = handle.prompt("hi", timeout=3.0)
    with pytest.raises(asyncio.CancelledError):
        await gen.__anext__()

    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_prompt_resets_turn_done_when_cancelled_while_building_blocks():
    """Cancellation at the prompt-ASSEMBLY await, not the send await.

    Image reads are offloaded with ``asyncio.to_thread``, which adds a second
    cancellation point inside the turn-state guard — and a longer-lived one,
    since it does file I/O. Cancelling there must not wedge the handle either.
    """
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    rt.send_request = AsyncMock(return_value=1)

    with patch(
        "kiro_crew.acp.session_handle.build_prompt_blocks",
        side_effect=asyncio.CancelledError(),
    ):
        gen = handle.prompt("hi", timeout=3.0)
        with pytest.raises(asyncio.CancelledError):
            await gen.__anext__()

    assert handle.is_turn_active is False


@pytest.mark.asyncio
async def test_handle_wait_turn_done_immediate():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Already done — returns True immediately
    result = await handle.wait_turn_done(timeout=0.1)
    assert result is True


@pytest.mark.asyncio
async def test_handle_wait_turn_done_timeout():
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._turn_done.clear()  # simulate active turn
    result = await handle.wait_turn_done(timeout=0.05)
    assert result is False


def _recorded_request(request_id):
    """The event the handle builds for a permission request it routes."""
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    return AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id=request_id, title="notes.txt")


@pytest.mark.asyncio
async def test_handle_approve_tool():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._permission_gate_events["req-7"] = _recorded_request("req-7")
    await handle.approve_tool("req-7", option_id="allow_always")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-7"
    assert sent["result"]["outcome"]["outcome"] == "selected"
    assert sent["result"]["outcome"]["optionId"] == "allow_always"


@pytest.mark.asyncio
async def test_handle_reject_tool():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.reject_tool("req-8")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-8"
    assert sent["result"]["outcome"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_handle_set_mode():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.set_mode("kirocrew-lite")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["method"] == "session/set_mode"
    assert sent["params"]["modeId"] == "kirocrew-lite"


@pytest.mark.asyncio
async def test_handle_set_model():
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    wire_send = rt.send_request

    async def _send_and_answer(method, params, **kwargs):
        req_id = await wire_send(method, params, **kwargs)
        q["sA"].put_nowait(JsonRpcMessage(id=req_id, result={}))
        return req_id

    rt.send_request = _send_and_answer  # type: ignore[method-assign]
    await handle.set_model("claude-sonnet-4")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["method"] == "session/set_model"
    assert sent["params"]["modelId"] == "claude-sonnet-4"
    assert handle.model == "claude-sonnet-4"


@pytest.mark.asyncio
async def test_handle_set_model_refusal_is_recorded_and_keeps_the_serving_model():
    """An adapter that refuses ``session/set_model`` must not read as a switch.

    Sent unawaited, the error reply was dropped by the pre-turn drain and the
    handle recorded the pick, so the dashboard showed a model that never served.
    The refusal is recorded in ``model_pin_refused`` like the config-option
    branch, so the session stays alive on its model and the caller reads it back.
    """
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.acp_backend = ACP_BACKEND_KIRO
    queue: asyncio.Queue = asyncio.Queue()
    rt.send_request = _replying_send(
        lambda _p: queue, error={"code": -32601, "message": "Method not found"}
    )
    h = AcpSessionHandle("sA", queue, rt)
    h._turn_done.set()
    h._model = "old-model"
    h._resolved_model_id = "old-model"

    await h.set_model("new-model")

    assert h.model_pin_refused == "new-model"
    assert h._model == "old-model"
    assert h._resolved_model_id == "old-model"


@pytest.mark.asyncio
async def test_handle_set_model_without_a_reply_records_the_switch(monkeypatch):
    """Silence is not a refusal: an adapter that never answers keeps the old,
    unawaited outcome, so only an explicit error reply stops the switch."""
    from kiro_crew.acp import session_handle as sh

    monkeypatch.setattr(sh, "_SET_MODEL_REPLY_TIMEOUT", 0.05)
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.acp_backend = ACP_BACKEND_KIRO
    rt.send_request = AsyncMock(return_value=1)
    h = AcpSessionHandle("sA", asyncio.Queue(), rt)
    h._turn_done.set()

    await h.set_model("new-model")

    assert h._model == "new-model"
    assert h._resolved_model_id == "new-model"


@pytest.mark.asyncio
async def test_refused_served_default_switch_does_not_fail_a_kiro_session_start():
    """``ensure_served_default`` runs right after session/new succeeded, so a
    refused switch leaves the session on its default instead of raising."""
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.acp_backend = ACP_BACKEND_KIRO
    queue: asyncio.Queue = asyncio.Queue()
    rt.send_request = _replying_send(
        lambda _p: queue, error={"code": -32603, "message": "model unavailable"}
    )
    h = AcpSessionHandle("sA", queue, rt)
    h._turn_done.set()
    h.store_session_config(
        {
            "models": {
                "currentModelId": "auto",
                "availableModels": [{"modelId": "gpt-5.6-sol"}],
            }
        }
    )

    await h.ensure_served_default()  # must not raise

    rt.send_request.assert_awaited_once()
    assert h.served_model == "auto"
    assert h.model == ""


# ── send_response / send_error ──


@pytest.mark.asyncio
async def test_send_response_writes_json():
    rt, _, proc = _make_runtime()
    await rt.send_response("req-42", {"ok": True})
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-42"
    assert sent["result"] == {"ok": True}
    assert "error" not in sent


@pytest.mark.asyncio
async def test_send_error_writes_json():
    rt, _, proc = _make_runtime()
    await rt.send_error("req-99", -32601, "Method not found")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == "req-99"
    assert sent["error"]["code"] == -32601
    assert sent["error"]["message"] == "Method not found"


@pytest.mark.asyncio
async def test_send_response_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_response("x", {})


@pytest.mark.asyncio
async def test_send_error_on_dead_runtime_raises():
    rt, _, _ = _make_runtime()
    rt._dead = True
    with pytest.raises(AcpRuntimeDead):
        await rt.send_error("x", -1, "err")


# ── unregister_session cleans routed_requests ──


@pytest.mark.asyncio
async def test_unregister_session_cleans_routed_requests():
    rt, _, _ = _make_runtime()
    _register(rt, "sA")
    rt._routed_requests[10] = "sA"
    rt._routed_requests[11] = "sA"
    rt._routed_requests[12] = "sB"  # different session
    rt.unregister_session("sA")
    assert "sA" not in rt._session_queues
    assert 10 not in rt._routed_requests
    assert 11 not in rt._routed_requests
    assert 12 in rt._routed_requests  # sB untouched


# ── is_alive ──


@pytest.mark.asyncio
async def test_is_alive_true():
    rt, _, proc = _make_runtime()
    proc.returncode = None
    assert rt.is_alive() is True


@pytest.mark.asyncio
async def test_is_alive_false_when_dead():
    rt, _, _ = _make_runtime()
    rt._dead = True
    assert rt.is_alive() is False


@pytest.mark.asyncio
async def test_is_alive_false_when_no_process():
    rt, _, _ = _make_runtime()
    rt._process = None
    assert rt.is_alive() is False


@pytest.mark.asyncio
async def test_approve_tool_echoes_recorded_option():
    """approve_tool echoes the advertised optionId recorded from the request."""
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    # Simulate build_permission_event having recorded claude-agent-acp ids.
    handle._permission_options[42] = {"once": "allow", "always": "allow_always"}
    handle._permission_gate_events[42] = _recorded_request(42)
    await handle.approve_tool(42)  # no explicit id → resolves the "once" variant
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["optionId"] == "allow"
    assert 42 not in handle._permission_options  # consumed on use


@pytest.mark.asyncio
async def test_reject_tool_prefers_recorded_reject_option():
    """reject_tool sends a clean 'selected' reject when one was advertised."""
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._permission_options[7] = {"once": "allow", "reject": "reject"}
    await handle.reject_tool(7)
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["outcome"] == "selected"
    assert sent["result"]["outcome"]["optionId"] == "reject"


# ── Error during prompt turn ──


@pytest.mark.asyncio
async def test_prompt_error_response_raises():
    """An error response for the prompt request raises AcpError."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(reader, {"id": req_id, "error": {"code": -1, "message": "throttled"}})
        with pytest.raises(AcpError):
            await await_until(driver, what="the driver finishing its turn")
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_prompt_transient_error_sets_transient_flag():
    """A transient backend 5xx error response (a mid-stream InternalServerError
    surfaced as JSON-RPC -32603) raises AcpError with transient=True, so the
    chat_runner / llm_helpers retry ladder fires instead of a bare error card."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": (
                        "Encountered an error in the response stream: "
                        "CodewhispererChatResponseStream(ServiceError(InternalServerError "
                        '{ message: "...please try again." }))'
                    ),
                },
            },
        )
        with pytest.raises(AcpError) as excinfo:
            await await_until(driver, what="the driver finishing its turn")
        assert excinfo.value.transient is True
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_prompt_auth_error_not_transient():
    """An auth error response raises AcpError with transient=False so it fails
    fast — a retry cannot fix an expired/denied credential."""
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=3.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        _feed(
            reader,
            {
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": "ExpiredTokenException: signature expired",
                },
            },
        )
        with pytest.raises(AcpError) as excinfo:
            await await_until(driver, what="the driver finishing its turn")
        assert excinfo.value.transient is False
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_wait_for_response_transient_error_sets_flag():
    """The non-streaming _wait_for_response path also classifies a transient
    backend 5xx (a -32603 InternalServerError) as transient=True, so
    request/response turns (session/new, set_mode, cancel, …) share the same
    retry eligibility. Covers the second kiro raise site."""
    from kiro_crew.acp.client import AcpError

    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    q["sA"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "id": 7,
                "error": {
                    "code": -32603,
                    "message": "Internal error",
                    "data": (
                        "Encountered an error in the response stream: "
                        "InternalServerError ... please try again."
                    ),
                },
            }
        )
    )
    with pytest.raises(AcpError) as excinfo:
        await handle._wait_for_response(7, timeout=3.0)
    assert excinfo.value.transient is True


# ── Runtime properties ──


@pytest.mark.asyncio
async def test_runtime_pid():
    rt, _, _ = _make_runtime()
    assert rt.pid == 4242


# ── Multi-session routing: stronger guarantees ──


@pytest.mark.asyncio
async def test_n_sessions_routed_independently():
    """Five concurrent prompt turns on ONE runtime each receive exactly their
    own session's text + completion — no cross-talk at higher fan-out.
    """
    n = 5
    sids = [f"s{i}" for i in range(n)]
    rt, reader, _ = _make_runtime()
    q = _register(rt, *sids)
    handles = {sid: AcpSessionHandle(sid, q[sid], rt) for sid in sids}
    task = await _start_reader(rt)

    out: dict[str, list] = {sid: [] for sid in sids}

    async def drive(sid):
        async for ev in handles[sid].prompt("go", timeout=5.0):
            out[sid].append(ev)

    drivers = [asyncio.ensure_future(drive(sid)) for sid in sids]
    try:
        sid_to_req = await _await_routed(rt, *sids)
        assert set(sid_to_req) == set(sids), "all prompts must be in flight"

        # Feed each session a uniquely-identifying text chunk, reverse order.
        for sid in reversed(sids):
            _feed(
                reader,
                {
                    "method": METHOD_SESSION_UPDATE,
                    "params": {
                        "sessionId": sid,
                        "update": {"sessionUpdate": "agent_message_chunk", "text": f"text-{sid}"},
                    },
                },
            )
        # Complete every turn (responses routed by id).
        for sid in sids:
            _feed(reader, {"id": sid_to_req[sid], "result": {"stopReason": "end_turn"}})

        await await_until(asyncio.gather(*drivers), what="every driver finishing its turn")

        for sid in sids:
            text = "".join(e.text for e in out[sid] if e.kind == EVENT_TEXT_CHUNK)
            assert text == f"text-{sid}", f"session {sid} got wrong/cross text: {text!r}"
            assert any(e.kind == EVENT_COMPLETE for e in out[sid])
    finally:
        for t in drivers:
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_one_session_errors_others_unaffected():
    """When one session's turn errors, the other concurrent session still
    completes normally — failures are isolated per session.
    """
    from kiro_crew.acp.client import AcpError

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sOk", "sErr")
    h_ok = AcpSessionHandle("sOk", q["sOk"], rt)
    h_err = AcpSessionHandle("sErr", q["sErr"], rt)
    task = await _start_reader(rt)

    ok_out: list = []
    err_exc: list = []

    async def drive_ok():
        async for ev in h_ok.prompt("go", timeout=5.0):
            ok_out.append(ev)

    async def drive_err():
        try:
            async for _ in h_err.prompt("go", timeout=5.0):
                pass
        except AcpError as exc:  # noqa: BLE001
            err_exc.append(exc)

    d_ok = asyncio.ensure_future(drive_ok())
    d_err = asyncio.ensure_future(drive_err())
    try:
        sid_to_req = await _await_routed(rt, "sOk", "sErr")
        # sErr gets an error response; sOk gets text + normal completion.
        _feed(reader, {"id": sid_to_req["sErr"], "error": {"code": -1, "message": "boom"}})
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sOk",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "fine"},
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sOk"], "result": {"stopReason": "end_turn"}})

        await await_until(asyncio.gather(d_ok, d_err), what="every driver finishing its turn")

        assert len(err_exc) == 1, "errored session should raise AcpError"
        ok_text = "".join(e.text for e in ok_out if e.kind == EVENT_TEXT_CHUNK)
        assert ok_text == "fine"
        assert any(e.kind == EVENT_COMPLETE for e in ok_out)
        # The errored session's turn is marked done (does not wedge the runtime).
        assert not h_err.is_turn_active
    finally:
        for t in (d_ok, d_err):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_interleaved_tool_calls_routed_per_session():
    """tool_call frames for two concurrent sessions are each delivered only to
    the originating session's stream.
    """
    from kiro_crew.acp.types import EVENT_TOOL_CALL

    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    h_a = AcpSessionHandle("sA", q["sA"], rt)
    h_b = AcpSessionHandle("sB", q["sB"], rt)
    task = await _start_reader(rt)

    out_a: list = []
    out_b: list = []

    async def drive(handle, out):
        async for ev in handle.prompt("go", timeout=5.0):
            out.append(ev)

    da = asyncio.ensure_future(drive(h_a, out_a))
    db = asyncio.ensure_future(drive(h_b, out_b))
    try:
        sid_to_req = await _await_routed(rt, "sA", "sB")
        # Interleave tool calls for each session.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "a1",
                        "title": "toolA",
                        "kind": "shell",
                    },
                },
            },
        )
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "b1",
                        "title": "toolB",
                        "kind": "fs",
                    },
                },
            },
        )
        _feed(reader, {"id": sid_to_req["sA"], "result": {"stopReason": "end_turn"}})
        _feed(reader, {"id": sid_to_req["sB"], "result": {"stopReason": "end_turn"}})
        await await_until(asyncio.gather(da, db), what="every driver finishing its turn")

        tc_a = [e for e in out_a if e.kind == EVENT_TOOL_CALL]
        tc_b = [e for e in out_b if e.kind == EVENT_TOOL_CALL]
        assert len(tc_a) == 1 and tc_a[0].tool_call_id == "a1" and tc_a[0].title == "toolA"
        assert len(tc_b) == 1 and tc_b[0].tool_call_id == "b1" and tc_b[0].title == "toolB"
        # No cross-talk: session A never saw session B's tool call and vice versa.
        assert all(e.tool_call_id != "b1" for e in out_a)
        assert all(e.tool_call_id != "a1" for e in out_b)
    finally:
        for t in (da, db):
            if not t.done():
                t.cancel()
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_destroyed_session_stops_receiving_frames():
    """After a session is destroyed, frames tagged with its id are dropped and
    do NOT leak into a sibling session that is still active.
    """
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA", "sB")
    # destroy() now round-trips _kiro.dev/session/terminate; no reader is running
    # yet here, so ack it instantly to avoid the bounded terminate timeout.
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    h_a = AcpSessionHandle("sA", q["sA"], rt)
    await h_a.destroy()  # sA terminated + unregistered
    task = await _start_reader(rt)
    try:
        # Frame for the destroyed session must be dropped (not broadcast to sB).
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sA",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "ghost"},
                },
            },
        )
        # A legitimate frame for sB still routes.
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sB",
                    "update": {"sessionUpdate": "agent_message_chunk", "text": "live"},
                },
            },
        )
        msg = await _next(q["sB"])
        assert msg.params["sessionId"] == "sB"
        # sB's queue must not contain the ghost frame.
        assert q["sB"].empty()
    finally:
        await _stop_reader(task)


# ── Tests for Phase 3 unification: AcpSessionHandle gap-fill methods ──


class TestAcpSessionHandleCommands:
    """Tests for send_command and set_config_option."""

    @pytest.mark.asyncio
    async def test_send_command_plain(self):
        """send_command with no args sends plain string command."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        # Mock send_request to capture what's sent and return a fake req_id
        sent_payloads = []
        req_counter = [100]

        async def capture_send(method, params, **_kw):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            # Put a fake response in the queue so _wait_for_response resolves
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {"text": "compacted"}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        result = await handle.send_command("/compact")
        assert result == "compacted"
        assert sent_payloads[0][0] == METHOD_COMMANDS_EXECUTE
        assert sent_payloads[0][1]["command"] == "/compact"
        assert sent_payloads[0][1]["sessionId"] == "s1"

    @pytest.mark.asyncio
    async def test_send_command_with_args(self):
        """send_command with args sends TuiCommand object form."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        sent_payloads = []
        req_counter = [200]

        async def capture_send(method, params, **_kw):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {"text": "ok"}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        result = await handle.send_command("/effort", args={"level": "high"})
        assert result == "ok"
        cmd = sent_payloads[0][1]["command"]
        assert isinstance(cmd, dict)
        assert cmd["command"] == "effort"
        assert cmd["args"] == {"level": "high"}

    @pytest.mark.asyncio
    async def test_set_config_option(self):
        """set_config_option sends correct JSON-RPC request."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        sent_payloads = []
        req_counter = [300]

        async def capture_send(method, params, **_kw):
            sent_payloads.append((method, params))
            req_id = req_counter[0]
            req_counter[0] += 1
            resp_msg = JsonRpcMessage.from_dict({"id": req_id, "result": {}})
            await q["s1"].put(resp_msg)
            return req_id

        rt.send_request = capture_send
        await handle.set_config_option("effort", "high")
        assert sent_payloads[0][0] == METHOD_SET_CONFIG_OPTION
        assert sent_payloads[0][1] == {
            "sessionId": "s1",
            "configId": "effort",
            "value": "high",
        }


class TestAcpSessionHandleState:
    """Tests for state tracking properties."""

    def test_initial_state(self):
        """New handle has empty state."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.model == ""
        assert handle.config_options == []
        assert handle.available_models == []

    def test_store_session_config(self):
        """store_session_config populates configOptions and available models."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        resp = {
            "sessionId": "s1",
            "configOptions": [
                {"id": "effort", "options": [{"value": "low"}, {"value": "high"}]},
            ],
            "models": {
                "availableModels": [
                    {"modelId": "opus-4", "name": "Claude Opus 4"},
                    {"modelId": "sonnet-4", "name": "Claude Sonnet 4"},
                ],
            },
        }
        handle.store_session_config(resp)
        assert len(handle.config_options) == 1
        assert handle.config_options[0]["id"] == "effort"
        assert len(handle.available_models) == 2
        assert handle.available_models[0]["modelId"] == "opus-4"

    def test_supports_config_option(self):
        """supports_config_option checks for matching id."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        # No options yet — returns True (lazy backend assumption)
        assert handle.supports_config_option("effort") is True

        handle._config_options = [{"id": "effort", "options": []}]
        assert handle.supports_config_option("effort") is True
        assert handle.supports_config_option("mode") is False

    def test_get_valid_effort_levels(self):
        """get_valid_effort_levels extracts from config options."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        handle._config_options = [
            {
                "id": "effort",
                "options": [
                    {"value": "low", "label": "Low"},
                    {"value": "medium", "label": "Medium"},
                    {"value": "high", "label": "High"},
                ],
            },
        ]
        assert handle.get_valid_effort_levels() == ["low", "medium", "high"]

    def test_set_model_updates_state(self):
        """set_model updates the _model field."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        # Directly set to test the state (set_model is async and would need send_request)
        handle._model = "opus-4"
        assert handle.model == "opus-4"

    def test_config_option_update_in_handle_update(self):
        """_handle_update processes config_option_update by updating state."""
        rt, _, _ = _make_runtime()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)

        msg = JsonRpcMessage.from_dict(
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {"id": "effort", "options": [{"value": "extreme"}]},
                        ],
                    },
                },
            }
        )
        events = handle._handle_update(msg)
        assert events == []  # No event emitted
        assert len(handle._config_options) == 1
        assert handle._config_options[0]["id"] == "effort"


class TestAcpSessionHandleResponsiveness:
    """Tests for is_responsive."""

    def test_responsive_when_alive_and_recent(self):
        """is_responsive returns True when runtime is alive with recent activity."""
        rt, _, _ = _make_runtime()
        rt._last_activity = time.monotonic()
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive() is True

    def test_not_responsive_when_stale(self):
        """is_responsive returns False when activity is old."""
        rt, _, _ = _make_runtime()
        rt._last_activity = time.monotonic() - 700  # 700s ago, threshold is 600
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive(stale_threshold=600.0) is False

    def test_not_responsive_when_dead(self):
        """is_responsive returns False when runtime is dead."""
        rt, _, _ = _make_runtime()
        rt._dead = True
        q = _register(rt, "s1")
        handle = AcpSessionHandle("s1", q["s1"], rt)
        assert handle.is_responsive() is False
