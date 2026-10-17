"""Evidence tests for four workflow lifecycle invariants.

All run against a STUB ``agent_fn`` (never real kiro-cli) and hold no host state,
per ``docs/system-specs/common/testing-conventions.md``.

Invariants covered:
  (1) events.py / runner.py — each event's ``ts`` is stamped from a HOST clock
      (real UTC), separate from the fixed, script-visible ``ctx.now``.
  (2) registry.py + store.py — a restart loads a stored ``running`` run as
      ``failed`` (it cannot resume in a new process) and persists that corrected
      state back durably, once, retaining payloads / provenance.
  (3) runner.py — the caller-cancellation branch drains the script's cancellation
      (cooperative ``finally`` / checkpoints, including an async cleanup that then
      raises) BEFORE the terminal event, so no side effect lands after
      ``run_cancelled`` and no task exception is left unhandled.
  (4) runner.py / registry.py — the run-global agent-slot cap lets independent
      fast calls settle while one call is held, and a FINISHED run's settled
      per-call outputs are exposed additively without changing
      ``partial_results`` semantics.

Spec: ``docs/system-specs/modules/workflows.md``.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime

import pytest

from kiro_crew.workflows.registry import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_FINISHED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    RunHandle,
    RunRegistry,
)
from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.store import WorkflowRunStore

# A fixed run-start stamp that is deliberately NOT an ISO timestamp and that a
# real host wall clock can never produce, so "the event ts is this sentinel" is a
# clean proxy for "the ts came from ctx.now rather than a host clock".
NOW_SENTINEL = "SCRIPT-NOW-SENTINEL"


async def _echo(prompt: str, opts: dict):
    """Stub agent: echoes the prompt (no real LLM / kiro-cli)."""
    return f"echo:{prompt}"


def _runner(**kw) -> WorkflowRunner:
    kw.setdefault("agent_fn", _echo)
    kw.setdefault("audit", lambda *a, **k: None)
    return WorkflowRunner(**kw)


GOOD_SCRIPT = (
    'META = {"name": "demo"}\n'
    "async def workflow(ctx):\n"
    "    ctx.phase('Work')\n"
    "    ctx.log('starting')\n"
    "    r = await ctx.agent('hello')\n"
    "    return {'said': r}\n"
)


# --------------------------------------------------------------------------- #
# (1) HOST per-event clock, separate from the fixed script ctx.now
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_event_ts_uses_host_clock_not_ctx_now() -> None:
    """Each event's ``ts`` is a real host wall-clock stamp, so no event carries
    the fixed script-visible sentinel and every ``ts`` parses as ISO-8601."""
    res = await _runner().run(GOOD_SCRIPT, run_id="wf_clk", now=NOW_SENTINEL)
    assert res.ok, res.error
    assert res.events, "expected a non-empty event stream"
    assert all(ev.ts != NOW_SENTINEL for ev in res.events)
    for ev in res.events:
        datetime.fromisoformat(ev.ts)  # raises if not a valid ISO-8601 stamp


@pytest.mark.asyncio
async def test_ctx_now_stays_fixed_and_visible_to_script() -> None:
    """The script-visible clock stays the fixed run-start stamp: ``ctx.now`` is
    that single value and the script gains no new time capability."""
    script = 'META = {"name": "clock"}\n' "async def workflow(ctx):\n" "    return ctx.now\n"
    res = await _runner().run(script, run_id="wf_now", now=NOW_SENTINEL)
    assert res.ok, res.error
    assert res.result == NOW_SENTINEL


@pytest.mark.asyncio
async def test_host_event_clock_is_deterministic_when_patched(monkeypatch) -> None:
    """Pin the host clock without adding a test-only runner constructor option."""
    counter = itertools.count()
    monkeypatch.setattr("kiro_crew.workflows.runner.host_now_iso", lambda: f"evt-{next(counter)}")
    runner = _runner()
    res = await runner.run(GOOD_SCRIPT, run_id="wf_ec", now=NOW_SENTINEL)
    assert res.ok, res.error
    stamps = [ev.ts for ev in res.events]
    assert stamps == [f"evt-{i}" for i in range(len(res.events))]
    assert all(s != NOW_SENTINEL for s in stamps)


# --------------------------------------------------------------------------- #
# (2) Restart writeback through a real WorkflowRunStore on disk
# --------------------------------------------------------------------------- #


def _seed_running(base_dir, run_id: str = "wf_r1", **overrides) -> None:
    """Persist one run to a real store the way a prior process would have."""
    handle = RunHandle(
        run_id=run_id,
        name="interrupted",
        status=STATUS_RUNNING,
        source="META = {}\nasync def workflow(ctx):\n    return 1\n",
        source_is_original=True,
        args={"k": "v"},
        agent_results={0: "payload-0", 1: "payload-1"},
        agent_errors={2: "TimeoutError: slow"},
    )
    for key, value in overrides.items():
        setattr(handle, key, value)
    WorkflowRunStore(base_dir=base_dir).save(run_id, handle.to_store_json())


def _spy_saves(store: WorkflowRunStore) -> list[str]:
    """Record every run_id ``store.save`` is called with, delegating to the real write."""
    calls: list[str] = []
    original = store.save

    def _save(run_id: str, payload: dict) -> None:
        calls.append(run_id)
        original(run_id, payload)

    store.save = _save  # type: ignore[method-assign]
    return calls


def _disk_record(base_dir, run_id: str) -> dict:
    """Read one run's durable JSON back through a fresh store."""
    return {r["run_id"]: r for r in WorkflowRunStore(base_dir=base_dir).load_all()}[run_id]


def test_store_roundtrip_demotes_running_and_retains_evidence(tmp_path) -> None:
    """A fresh registry loads a stored ``running`` run as ``failed`` with a clear
    interrupted reason, and the corrected status/error plus every payload,
    provenance flag and arg survive on disk (not only in memory)."""
    _seed_running(tmp_path)

    reg = RunRegistry(store=WorkflowRunStore(base_dir=tmp_path))
    assert reg.load_persisted() == 1

    handle = reg.get("wf_r1")
    assert handle is not None and handle.status == STATUS_FAILED
    assert "interrupted" in (handle.error or "")
    assert handle.agent_results == {0: "payload-0", 1: "payload-1"}
    assert handle.agent_errors == {2: "TimeoutError: slow"}
    assert handle.source_is_original is True
    assert handle.args == {"k": "v"}

    disk = _disk_record(tmp_path, "wf_r1")
    assert disk["status"] == STATUS_FAILED
    assert "interrupted" in disk["error"]
    assert disk["agent_results"] == {"0": "payload-0", "1": "payload-1"}
    assert disk["agent_errors"] == {"2": "TimeoutError: slow"}
    assert disk["source_is_original"] is True
    assert disk["args"] == {"k": "v"}
    assert disk["source"] == "META = {}\nasync def workflow(ctx):\n    return 1\n"


def test_store_writeback_is_idempotent_across_restarts(tmp_path) -> None:
    """The demotion writeback fires once: the first restart corrects the record,
    a second restart reads the already-failed record and writes nothing."""
    _seed_running(tmp_path)

    first = WorkflowRunStore(base_dir=tmp_path)
    first_saves = _spy_saves(first)
    RunRegistry(store=first).load_persisted()
    assert first_saves == ["wf_r1"]

    second = WorkflowRunStore(base_dir=tmp_path)
    second_saves = _spy_saves(second)
    RunRegistry(store=second).load_persisted()
    assert second_saves == []
    assert _disk_record(tmp_path, "wf_r1")["status"] == STATUS_FAILED


def test_store_load_does_not_rewrite_already_terminal_runs(tmp_path) -> None:
    """The writeback is narrow: a stored terminal run is loaded without a re-write."""
    _seed_running(tmp_path, "wf_done", status=STATUS_FINISHED)
    store = WorkflowRunStore(base_dir=tmp_path)
    saves = _spy_saves(store)
    reg = RunRegistry(store=store)
    assert reg.load_persisted() == 1
    assert saves == []
    assert reg.get("wf_done").status == STATUS_FINISHED


def test_demoted_host_run_remains_reopenable_after_writeback(tmp_path) -> None:
    """A host (TaskRunner) run demoted after interruption keeps its identity: the
    writeback does not break host reopen, which reconciles it back to running."""
    _seed_running(tmp_path, "wf_host", driver="taskrunner", task_id="t1")
    reg = RunRegistry(store=WorkflowRunStore(base_dir=tmp_path))
    reg.load_persisted()
    assert reg.get("wf_host").status == STATUS_FAILED
    assert reg.reopen_host_run("wf_host", task_id="t1") is True
    assert reg.get("wf_host").status == STATUS_RUNNING


# --------------------------------------------------------------------------- #
# (3) Caller cancellation drains cleanup BEFORE the terminal event
# --------------------------------------------------------------------------- #


def _assert_terminal_after_cleanup(res) -> None:
    """The stream ends in exactly one ``run_cancelled``, and the script's cleanup
    log lands before that single terminal event."""
    events = list(res.events)
    types = [e.type for e in events]
    assert types[-1] == "run_cancelled"
    assert types.count("run_cancelled") == 1
    cleanup_idx = next(
        i
        for i, e in enumerate(events)
        if e.type == "log" and e.data.get("message") == "cleanup ran"
    )
    cancelled_idx = next(i for i, e in enumerate(events) if e.type == "run_cancelled")
    assert cleanup_idx < cancelled_idx


@pytest.mark.asyncio
async def test_cancel_drains_sync_finally_before_terminal() -> None:
    """Cancelling a run mid agent-call drains the script's ``finally`` first, so
    its cleanup log precedes the single terminal event and the already-settled
    first call is still checkpointed."""
    started = asyncio.Event()
    release = asyncio.Event()  # never set: the second call blocks until cancelled

    async def agent_fn(prompt: str, opts: dict):
        if prompt == "second":
            started.set()
            await release.wait()
            return "unreached"
        return f"ok:{prompt}"

    script = (
        'META = {"name": "cancel-drain"}\n'
        "async def workflow(ctx):\n"
        "    await ctx.agent('first')\n"
        "    try:\n"
        "        await ctx.agent('second')\n"
        "    finally:\n"
        "        ctx.log('cleanup ran')\n"
        "    return 'done'\n"
    )
    runner = _runner(agent_fn=agent_fn, timeout_secs=3600)
    task = asyncio.ensure_future(runner.run(script, run_id="wf_cd", now=NOW_SENTINEL))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    res = await asyncio.wait_for(task, 5)  # run() swallows cancel, returns a result

    _assert_terminal_after_cleanup(res)
    assert res.agent_results.get(0) == "ok:first"


@pytest.mark.asyncio
async def test_cancel_while_agent_waiting_for_slot_drains_cleanly() -> None:
    """With concurrency=1, cancellation arrives while a second fan-out call is
    still queued for the only slot; the run still ends ``run_cancelled`` (once,
    last) with the cleanup log drained ahead of it."""
    started = asyncio.Event()
    release = asyncio.Event()  # never set

    async def agent_fn(prompt: str, opts: dict):
        started.set()
        await release.wait()
        return "unreached"

    script = (
        'META = {"name": "slot-cancel"}\n'
        "async def workflow(ctx):\n"
        "    try:\n"
        "        await ctx.parallel([lambda: ctx.agent('h1'), lambda: ctx.agent('h2')])\n"
        "    finally:\n"
        "        ctx.log('cleanup ran')\n"
        "    return 'done'\n"
    )
    runner = _runner(agent_fn=agent_fn, timeout_secs=3600, concurrency=1)
    task = asyncio.ensure_future(runner.run(script, run_id="wf_slot", now=NOW_SENTINEL))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    res = await asyncio.wait_for(task, 5)

    _assert_terminal_after_cleanup(res)


@pytest.mark.asyncio
async def test_cancel_drains_async_cleanup_that_raises_without_unhandled_error() -> None:
    """When cancellation triggers a ``finally`` that awaits an async cleanup and
    then raises, the cleanup still settles before the single terminal event and
    the raised exception is consumed by the runner, not left unhandled."""
    started = asyncio.Event()
    release = asyncio.Event()  # never set: the first call blocks until cancelled

    async def agent_fn(prompt: str, opts: dict):
        if prompt == "block":
            started.set()
            await release.wait()
            return "unreached"
        return f"ok:{prompt}"

    script = (
        'META = {"name": "async-cleanup-raises"}\n'
        "async def workflow(ctx):\n"
        "    try:\n"
        "        await ctx.agent('block')\n"
        "    finally:\n"
        "        await ctx.agent('cleanup')\n"
        "        ctx.log('cleanup ran')\n"
        "        raise RuntimeError('cleanup boom')\n"
        "    return 'done'\n"
    )
    unhandled: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: unhandled.append(ctx))

    runner = _runner(agent_fn=agent_fn, timeout_secs=3600)
    task = asyncio.ensure_future(runner.run(script, run_id="wf_acr", now=NOW_SENTINEL))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    res = await asyncio.wait_for(task, 5)

    _assert_terminal_after_cleanup(res)
    # The async cleanup call ran to completion during the drain (call_index 1)...
    assert res.agent_results.get(1) == "ok:cleanup"
    # ...and the exception it raised surfaced to no one but the runner.
    await asyncio.sleep(0)
    assert unhandled == []


# --------------------------------------------------------------------------- #
# (4a) Agent-return matrix: the engine pins the payload, not its business meaning
# --------------------------------------------------------------------------- #

_ONE_CALL = (
    'META = {"name": "one"}\n' "async def workflow(ctx):\n" '    return await ctx.agent("go")\n'
)
_ONE_CALL_SCHEMA = (
    'META = {"name": "one-schema"}\n'
    "async def workflow(ctx):\n"
    '    return await ctx.agent("go", schema={"type": "object"})\n'
)


@pytest.mark.parametrize(
    "returns, use_schema, expected_result, expected_error",
    [
        (None, False, None, "agent returned no result"),
        ("…still working…", False, "…still working…", None),
        ("could not save: permission denied", False, "could not save: permission denied", None),
        ("not json at all", True, None, "no schema-valid result after bounded re-asks"),
        ({"kind": "rca"}, True, {"kind": "rca"}, None),
    ],
)
@pytest.mark.asyncio
async def test_agent_return_matrix_pins_payloads_and_errors(
    returns, use_schema, expected_result, expected_error
) -> None:
    """The engine records whatever a call returns as that call's payload and keys
    a per-call error only on a null / no-schema-valid outcome. Neither a
    schema-valid dict nor arbitrary text (progress chatter, a save-refusal
    sentence) is treated as proof the agent performed the business task: the
    payload is pinned, its meaning is not verified."""

    async def agent_fn(prompt: str, opts: dict):
        return returns

    script = _ONE_CALL_SCHEMA if use_schema else _ONE_CALL
    res = await _runner(agent_fn=agent_fn).run(script, run_id="wf_matrix", now=NOW_SENTINEL)
    assert res.ok, res.error
    assert res.result == expected_result
    assert res.agent_results.get(0) == expected_result
    if expected_error is None:
        assert 0 not in res.agent_errors
    else:
        assert res.agent_errors.get(0) == expected_error


# --------------------------------------------------------------------------- #
# (4b) One held call must not starve independent fast calls at concurrency=2
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrency_two_one_held_call_does_not_block_later_fast_calls() -> None:
    """At concurrency=2 one held agent call occupies a single slot; the remaining
    slot must let a fanout of independent fast calls all settle before the held
    call is released. Release is driven by a test Event, never a sleep."""
    started_slow = asyncio.Event()
    release_slow = asyncio.Event()  # held until every fast call has settled
    fast_total = 5
    fast_settled = 0
    all_fast = asyncio.Event()

    async def agent_fn(prompt: str, opts: dict):
        nonlocal fast_settled
        if prompt == "slow":
            started_slow.set()
            await release_slow.wait()
            return "slow-done"
        fast_settled += 1
        if fast_settled == fast_total:
            all_fast.set()
        return f"fast:{prompt}"

    script = (
        'META = {"name": "held-slot"}\n'
        "async def workflow(ctx):\n"
        "    thunks = [\n"
        "        lambda: ctx.agent('slow'),\n"
        "        lambda: ctx.agent('f0'),\n"
        "        lambda: ctx.agent('f1'),\n"
        "        lambda: ctx.agent('f2'),\n"
        "        lambda: ctx.agent('f3'),\n"
        "        lambda: ctx.agent('f4'),\n"
        "    ]\n"
        "    return await ctx.parallel(thunks)\n"
    )
    runner = _runner(agent_fn=agent_fn, concurrency=2, timeout_secs=3600)
    task = asyncio.ensure_future(runner.run(script, run_id="wf_hs", now=NOW_SENTINEL))

    await asyncio.wait_for(started_slow.wait(), 5)
    await asyncio.wait_for(all_fast.wait(), 5)  # fast calls settle while slow is held
    assert not release_slow.is_set()
    assert not task.done()  # the run cannot finish while the held call blocks

    release_slow.set()
    res = await asyncio.wait_for(task, 5)
    assert res.ok, res.error
    assert res.result[0] == "slow-done"
    assert res.result[1:] == ["fast:f0", "fast:f1", "fast:f2", "fast:f3", "fast:f4"]


# --------------------------------------------------------------------------- #
# (4c) A FINISHED run exposes its settled per-call outputs additively
# --------------------------------------------------------------------------- #


def test_finished_snapshot_exposes_agent_results_in_full_detail() -> None:
    """A FINISHED run reports its settled per-call outputs additively: a count in
    the compact view, the payloads only in the FULL detail view, alongside the
    aggregate ``result`` and without a ``partial_results`` key."""
    handle = RunHandle(run_id="wf_f", name="f", status=STATUS_FINISHED)
    handle.result = {"synthesis": "done"}
    handle.agent_results = {0: "echo:a", 1: "echo:b"}

    compact = handle.snapshot(include_events=False)
    assert compact["agent_result_count"] == 2
    assert "agent_results" not in compact

    full = handle.snapshot(include_events=True)
    assert full["agent_results"] == {"0": "echo:a", "1": "echo:b"}
    assert full["result"] == {"synthesis": "done"}
    assert "partial_results" not in full


def test_finished_none_result_still_exposes_per_call_outputs() -> None:
    """A run that finished and legitimately returned None still surfaces the
    per-call outputs it produced."""
    handle = RunHandle(run_id="wf_fn", name="fn", status=STATUS_FINISHED)
    handle.result = None
    handle.agent_results = {0: "echo:only"}

    assert handle.snapshot(include_events=False)["agent_result_count"] == 1
    full = handle.snapshot(include_events=True)
    assert full["agent_results"] == {"0": "echo:only"}
    assert full["result"] is None


def test_failed_run_keeps_partial_results_and_no_agent_results_key() -> None:
    """A failed / cancelled run reports its work as ``partial_results``, never as
    ``agent_results``, so a run never double-reports the same outputs."""
    for terminal in (STATUS_FAILED, STATUS_CANCELLED):
        handle = RunHandle(run_id=f"wf_{terminal}", name="x", status=terminal)
        handle.result = None
        handle.agent_results = {0: "echo:partial"}

        full = handle.snapshot(include_events=True)
        assert full["partial_results"] == {"0": "echo:partial"}
        assert "agent_results" not in full
        compact = handle.snapshot(include_events=False)
        assert compact["partial_result_count"] == 1
        assert "agent_result_count" not in compact


def test_active_run_snapshot_omits_result_counts() -> None:
    """An active (running / paused) run is still accumulating, so the compact
    snapshot omits both count keys and the full snapshot omits both payload keys."""
    for active in (STATUS_RUNNING, STATUS_PAUSED):
        handle = RunHandle(run_id=f"wf_{active}", name="x", status=active)
        handle.agent_results = {0: "echo:inflight"}

        compact = handle.snapshot(include_events=False)
        assert "agent_result_count" not in compact
        assert "partial_result_count" not in compact
        full = handle.snapshot(include_events=True)
        assert "agent_results" not in full
        assert "partial_results" not in full


@pytest.mark.parametrize("aggregate", [None, {"summary": "complete"}])
def test_mcp_result_exposes_redacted_finished_agent_outputs(monkeypatch, aggregate) -> None:
    import json

    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import workflows

    token = "AKIA" + "L" * 16
    handle = RunHandle(run_id="wf_mcp", name="example", status=STATUS_FINISHED)
    handle.result = aggregate
    handle.agent_results = {0: {"answer": "kept", "credential": token}, 1: None}
    handle.agent_errors = {1: "agent returned no result"}
    # This unit test owns the caller and transport; never resolve host identity.
    caller = "wf-worker:wf_mcp:result-reader"
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: caller)
    reads = []

    def get(path, *, session_key):
        reads.append((path, session_key))
        return handle.snapshot(include_events=True)

    monkeypatch.setattr(mcp_core, "_get", get)
    monkeypatch.setattr(workflows, "_wf_return", lambda tool, text, **kw: text)

    raw = workflows.workflow_result("workflow_result", {"run_id": "wf_mcp"})
    assert reads == [("/api/workflows/runs/wf_mcp", caller)]
    payload = json.loads(raw)
    assert payload["agent_results"]["0"]["answer"] == "kept"
    assert payload["agent_results"]["1"] is None
    assert token not in raw
    assert payload["agent_errors"] == {"1": "agent returned no result"}
    assert payload["result"] == aggregate


def test_completion_summary_distinguishes_calls_from_verified_artifacts() -> None:
    from kiro_crew.dashboard.workflow_inject import _summarize

    handle = RunHandle(run_id="wf_summary", name="example", status=STATUS_FINISHED)
    handle.agent_results = {0: "write was refused", 1: None}
    handle.agent_errors = {1: "agent returned no result"}
    text = _summarize(handle.snapshot(include_events=False))
    assert "agent_results" in text
    assert "2 agent call" in text
    assert "1 agent call(s) failed" in text
    assert "not verified" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("phase", ["script", "pre_terminal", "on_complete"])
@pytest.mark.parametrize("cleanup_raises", [False, True])
async def test_repeated_registry_cancel_preserves_ordinary_cleanup(timeout, phase, cleanup_raises):
    started, cleanup_started = asyncio.Event(), asyncio.Event()
    release, settled = asyncio.Event(), asyncio.Event()
    cleanup_cancelled = []
    completed = []

    async def cleanup():
        cleanup_started.set()
        try:
            await release.wait()  # ordinary cleanup: does NOT suppress cancellation
            settled.set()
            if cleanup_raises:
                raise RuntimeError("synthetic cleanup error")
        except asyncio.CancelledError:
            cleanup_cancelled.append(True)
            raise

    async def agent_fn(prompt, opts):
        if prompt == "work":
            started.set()
            await asyncio.Event().wait()
        if phase == "script":
            await cleanup()
        return "cleaned"

    script = (
        'META = {"name": "ordinary-finally"}\n'
        "async def workflow(ctx):\n"
        "    try:\n"
        "        await ctx.agent('work')\n"
        "    finally:\n"
        "        await ctx.agent('cleanup')\n"
        "        ctx.log('cleanup finished')\n"
    )
    registry = RunRegistry()
    registry.set_on_done(lambda rid, snapshot: completed.append(snapshot))
    runner = _runner(
        agent_fn=agent_fn,
        timeout_secs=0.01 if timeout else 3600,
        pre_terminal=cleanup if phase == "pre_terminal" else None,
        on_complete=cleanup if phase == "on_complete" else None,
    )
    rid = await runner.run_background(
        script, registry=registry, run_id="wf_repeat", now=NOW_SENTINEL
    )
    handle = registry.get(rid)
    try:
        await asyncio.wait_for(started.wait(), 5)
        if not timeout:
            assert await registry.cancel(rid)
        await asyncio.wait_for(cleanup_started.wait(), 5)
        for _ in range(2):
            assert await registry.cancel(rid) is (phase != "on_complete")
            if phase == "on_complete":
                handle.task.cancel()  # shutdown cancellation still drains owned cleanup
            await asyncio.sleep(0)
        assert not cleanup_cancelled
        assert not settled.is_set()
        assert not handle.task.done()
        assert handle.status == STATUS_RUNNING and completed == []
        if phase != "on_complete":
            assert not any(
                e.type.startswith("run_") and e.type != "run_started" for e in handle.events
            )
        release.set()
        await asyncio.wait_for(handle.task, 5)
        assert settled.is_set() and not cleanup_cancelled
        assert len(completed) == 1
        terminal = "run_failed" if timeout else "run_cancelled"
        assert handle.events[-1].type == terminal
        assert sum(e.type == terminal for e in handle.events) == 1
        assert handle.status == (STATUS_FAILED if timeout else STATUS_CANCELLED)
        assert handle.error == ("timeout" if timeout else "cancelled")
        assert any(
            e.type == "log" and e.data.get("message") == "cleanup finished" for e in handle.events
        )
    finally:
        release.set()
        if not handle.task.done():
            handle.task.cancel()
        await asyncio.wait_for(asyncio.gather(handle.task, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pre_terminal", "on_complete"])
@pytest.mark.parametrize("cleanup_raises", [False, True])
async def test_success_cancellation_respects_terminal_boundary(phase, cleanup_raises):
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, completed = [], []

    async def cleanup():
        calls.append("entered")
        entered.set()
        await release.wait()
        settled.set()
        if cleanup_raises:
            raise RuntimeError("synthetic cleanup failure")

    registry = RunRegistry()
    registry.set_on_done(lambda rid, snap: completed.append(snap))
    runner = _runner(
        pre_terminal=cleanup if phase == "pre_terminal" else None,
        on_complete=cleanup if phase == "on_complete" else None,
    )
    rid = await runner.run_background(
        GOOD_SCRIPT, registry=registry, run_id="wf_success_cancel", now=NOW_SENTINEL
    )
    handle = registry.get(rid)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(2):
            accepted = await registry.cancel(rid)
            assert accepted is (phase == "pre_terminal")
            if phase == "on_complete":
                handle.task.cancel()  # outer shutdown still cannot abandon final cleanup
            await asyncio.sleep(0)
        assert not handle.task.done() and not settled.is_set()
        assert calls == ["entered"] and completed == []
        release.set()
        await asyncio.wait_for(handle.task, 5)
        assert settled.is_set() and calls == ["entered"]
        assert len(completed) == 1
        terminal = "run_cancelled" if phase == "pre_terminal" else "run_finished"
        assert handle.events[-1].type == terminal
        assert (
            sum(e.type in {"run_cancelled", "run_finished", "run_failed"} for e in handle.events)
            == 1
        )
        assert handle.status == (STATUS_CANCELLED if phase == "pre_terminal" else STATUS_FINISHED)
        assert handle.error == ("cancelled" if phase == "pre_terminal" else None)
        assert handle.agent_results == {0: "echo:hello"}
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(handle.task, return_exceptions=True), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_save_failure_is_visible_without_replacing_execution_result(
    tmp_path, monkeypatch, asynchronous
):
    from kiro_crew.dashboard.workflow_inject import _summarize

    store = WorkflowRunStore(base_dir=tmp_path / "workflows")
    registry = RunRegistry(store=store)
    handle = RunHandle(run_id="wf_storage_error", name="storage error")
    registry.register(handle)
    path = store.runs_dir / "wf_storage_error.json"
    saved = path.read_bytes()
    save = store.save
    notifications = []
    registry.set_on_done(lambda _rid, snapshot: notifications.append(snapshot))

    def fail_save(*_args):
        raise OSError("PRIVATE_SAVE_FAILURE_SENTINEL")

    monkeypatch.setattr(store, "save", fail_save)
    if asynchronous:
        await registry.mark_terminal_async(handle.run_id, STATUS_FINISHED, result={"kept": True})
    else:
        registry.mark_terminal(handle.run_id, STATUS_FINISHED, result={"kept": True})
    for snapshot in (handle.snapshot(), notifications[0]):
        assert snapshot["status"] == STATUS_FINISHED
        assert snapshot["result"] == {"kept": True}
        assert "checkpoint could not be saved" in snapshot["error"]
        assert "PRIVATE_SAVE_FAILURE_SENTINEL" not in snapshot["error"]
        assert str(tmp_path) not in snapshot["error"]
        assert "checkpoint could not be saved" in _summarize(snapshot)
    assert handle.error is None  # Execution did not fail; durability did.
    assert path.read_bytes() == saved
    monkeypatch.setattr(store, "save", save)
    if asynchronous:
        await registry.persist_async(handle.run_id)
    else:
        registry.persist(handle.run_id)
    assert handle.snapshot()["error"] is None
    assert RunRegistry(store=store).load_persisted() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["directory", "serialization", "write", "replace"])
async def test_real_store_failures_reach_registry_durability_reporting(
    tmp_path, monkeypatch, caplog, failure
):
    import logging
    from pathlib import Path

    from kiro_crew.workflows import store as store_module

    caplog.set_level(logging.DEBUG, logger=store_module.__name__)

    store = WorkflowRunStore(base_dir=tmp_path / "workflows")
    registry = RunRegistry(store=store)
    handle = RunHandle(run_id="wf_store_error", name="storage")
    registry.register(handle)
    path = store.runs_dir / "wf_store_error.json"
    saved = path.read_bytes()
    sentinel = "PRIVATE_FILESYSTEM_FAILURE_SENTINEL"
    with monkeypatch.context() as patch:
        if failure == "serialization":
            handle.args = {("invalid", "json-key"): "secret"}
        elif failure == "directory":
            original = Path.mkdir

            def mkdir(candidate, *args, **kwargs):
                if candidate == store.runs_dir:
                    raise OSError(sentinel)
                return original(candidate, *args, **kwargs)

            patch.setattr(Path, "mkdir", mkdir)
        elif failure == "write":
            original = Path.write_text

            def write(candidate, *args, **kwargs):
                if candidate == path.with_suffix(".json.tmp"):
                    raise OSError(sentinel)
                return original(candidate, *args, **kwargs)

            patch.setattr(Path, "write_text", write)
        else:
            original = store_module.os.replace

            def replace(source, destination, *args, **kwargs):
                if Path(destination) == path:
                    raise OSError(sentinel)
                return original(source, destination, *args, **kwargs)

            patch.setattr(store_module.os, "replace", replace)
        await registry.mark_terminal_async(handle.run_id, STATUS_FINISHED, result={"kept": True})
        snapshot = handle.snapshot()
        assert "checkpoint could not be saved" in (snapshot["error"] or "")
        assert sentinel not in snapshot["error"]
        assert snapshot["result"] == {"kept": True}
        assert path.read_bytes() == saved
        assert not path.with_suffix(".json.tmp").exists()
        assert sentinel not in caplog.text
        assert str(tmp_path) not in caplog.text
        assert all(
            record.exc_info is None
            for record in caplog.records
            if record.name == store_module.__name__
        )
    handle.args = {}
    await registry.persist_async(handle.run_id)
    assert handle.snapshot()["error"] is None
    assert path.read_bytes() != saved
