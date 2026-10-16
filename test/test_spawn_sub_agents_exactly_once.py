"""A result ``spawn_sub_agents`` returned inline reaches its parent exactly once.

Each test is one ordering an independent review found where a result the call
returned could be lost or delivered a second time:

* a member the reply's truncation cut is never committed;
* a torn response frame is never reported as written;
* a written commit settles its members, so the result is not redelivered when
  the collection later expires;
* a response that was never written commits nothing, and a later word on the
  same call changes nothing;
* a parent retired before its commit keeps a late completion fenced;
* a run's hung terminal report cannot keep a released result forever.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import mcp_shared
from kiro_crew.mcp_core import _call_tool
from kiro_crew.subagent_inline_collection import COLLECTION_GRACE_SECS, InlineCollections
from kiro_crew.validation import build_tool_response

PARENT = "cron:job1:run1"


def _hold(reg: Any, parent: str, aid: str, info: Any = None) -> bool:
    """``reg.hold`` with *info* as the live run its release reads back.

    The registry keeps only the id, so the run record a test hands in is what
    the bound manager's ``get`` answers for it, as ``_agents`` does live.
    """
    if info is not None:
        _live_runs(reg)[aid] = info
    return reg.hold(parent, aid)


def _live_runs(reg: Any) -> dict:
    mgr = reg._manager
    runs = getattr(mgr, "_test_live_runs", None)
    if not isinstance(runs, dict):
        runs = {}
        previous = getattr(mgr, "get", None)

        def get(aid: str) -> Any:
            if aid in runs:
                return runs[aid]
            return previous(aid) if callable(previous) else None

        mgr._test_live_runs = runs
        mgr.get = get
    return runs


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _registry() -> tuple[InlineCollections, _Clock, Any]:
    clock = _Clock()
    reg = InlineCollections(clock=clock)
    mgr = MagicMock()
    mgr._on_done = AsyncMock()
    mgr._sessions = MagicMock()
    mgr._sessions.is_busy = MagicMock(return_value=False)
    mgr._report_owners = {}
    mgr.settle_queued_delivery = AsyncMock()
    reg.bind(mgr)
    return reg, clock, mgr


def _info(aid: str) -> Any:
    from kiro_crew.subagent import SubagentInfo

    # A real run record: the registry holds only a dataclass it can bound.
    info = SubagentInfo(id=aid, task="t", parent_session_key=PARENT)
    info.done = True
    info.elapsed = 1.0
    info._delivery_queued = True
    return info


async def _drain(reg: InlineCollections) -> None:
    while reg._tasks:
        await asyncio.wait_for(asyncio.gather(*reg._tasks), timeout=30)


def _settled_ids(mgr: Any) -> list[str]:
    return [d.agent_id for call in mgr.settle_queued_delivery.await_args_list for d in call.args[0]]


def test_a_member_cut_by_response_truncation_is_not_committed() -> None:
    n = 7
    cjk = "漢" * 3000  # at the completion keep threshold, so not summarised
    spawned = iter(f"a{i}" for i in range(1, n + 1))
    posts: list[tuple[str, dict[str, Any]]] = []

    def _post(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        posts.append((path, body))
        return {"id": next(spawned)} if path == "/api/spawn" else {}

    def _get(path: str, **_kw: Any) -> dict[str, Any]:
        aid = path.rsplit("/", 1)[1]
        return {"done": True, "agent": aid, "result": f"{aid}:{cjk}"}

    with (
        patch("kiro_crew.mcp_core._post", side_effect=_post),
        patch("kiro_crew.mcp_core._get", side_effect=_get),
        patch("kiro_crew.mcp_core.sel"),
        patch.dict("os.environ", {"KIROCREW_SESSION_KEY": PARENT}),
    ):
        text = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"}] * n})
    sent = build_tool_response(text)["content"][0]["text"]  # what the dispatcher writes
    committed = next(b for p, b in reversed(posts) if p == "/api/spawn/mark-collected")["ids"]
    missing = [aid for aid in committed if f'"agent": "{aid}"' not in sent]
    assert not missing, f"committed but cut from the written response: {missing}"
    assert len(committed) < n  # the cut member is released, not committed


def test_a_torn_frame_is_not_reported_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    def _torn(fd: int, payload: bytes) -> int:
        exc = BrokenPipeError(32, "pipe closed")
        exc.bytes_written = 10  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(mcp_shared, "_stdout_fd", 99)
    monkeypatch.setattr(mcp_shared, "_write_all", _torn)
    outcome = mcp_shared.respond(1, {"content": [{"type": "text", "text": "x"}]})
    assert outcome is False


@pytest.mark.asyncio
async def test_a_written_commit_settles_and_is_not_redelivered_at_expiry() -> None:
    reg, clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 7200, call="c1")
    assert _hold(reg, PARENT, "a1", _info("a1"))
    # The response was written; the commit arrives.
    reg.commit(PARENT, ["a1"], released=["a1"])
    assert PARENT not in reg._records or reg._records[PARENT]["a1"].state != "held"
    clock.now += 7200 + COLLECTION_GRACE_SECS + 1
    reg._expire(PARENT)
    await _drain(reg)
    mgr._on_done.assert_not_awaited()
    assert _settled_ids(mgr) == ["a1"]


@pytest.mark.asyncio
async def test_a_written_commit_before_the_completion_consumes_it() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    reg.commit(PARENT, ["a1"], released=["a1"])  # written before a1 completed
    assert not _hold(reg, PARENT, "a1", _info("a1"))
    assert reg.consume_collected(PARENT, "a1")
    await _drain(reg)
    mgr._on_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_commit_naming_nothing_delivers_every_held_result() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    assert reg.reserve(PARENT, "a2", 60, call="c1")
    assert _hold(reg, PARENT, "a1", _info("a1"))
    assert _hold(reg, PARENT, "a2", _info("a2"))
    reg.commit(PARENT, [], released=["a1", "a2"])  # the response was never written
    await _drain(reg)
    # Both go out by the ordinary route; neither is settled as returned.
    assert sorted(c.args[0].id for c in mgr._on_done.await_args_list) == ["a1", "a2"]


def _collected_call(collected: list[str]) -> Any:
    """A ``spawn_sub_agents`` call whose collect step carried *collected*."""
    from kiro_crew.mcp_tools import spawn as spawn_mod

    step = spawn_mod._SubAgentsStep(
        sa_ids=list(collected),
        sa_deferred=set(),
        sa_errors=[],
        parent_session=PARENT,
        max_wait=60.0,
        inline_result=lambda _aid, raw: raw,
    )
    step._collected = list(collected)
    return step


def test_an_unwritten_answer_commits_nothing_and_a_later_answer_changes_nothing() -> None:
    step = _collected_call(["a1"])
    with patch("kiro_crew.mcp_core._post", return_value={}) as post:
        step.on_answered(False)  # the write tore, or the loop ended unanswered
        step.on_answered(True)  # a second word on the same call is ignored
        step.cancel()
    assert [(c.args[0], c.args[1]["ids"]) for c in post.call_args_list] == [
        ("/api/spawn/mark-collected", [])
    ]


def test_a_direct_drive_commits() -> None:
    """A direct call (no dispatcher) holds the result already: it commits."""
    from kiro_crew.mcp_shared import drive_deferred

    step = _collected_call(["a1"])
    step._step = lambda: "reply"  # type: ignore[method-assign]
    with patch("kiro_crew.mcp_core._post", return_value={}) as post:
        assert drive_deferred(step) == "reply"
    assert [(c.args[0], c.args[1]["ids"]) for c in post.call_args_list] == [
        ("/api/spawn/mark-collected", ["a1"])
    ]


def test_retire_then_commit_leaves_the_late_completion_fenced() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    reg.retire(PARENT)  # torn down before the commit, completion not yet seen
    reg.commit(PARENT, ["a1"])
    assert _hold(reg, PARENT, "a1", _info("a1"))  # owned, and dropped by the fence
    mgr._on_done.assert_not_called()
    mgr.settle_queued_delivery.assert_not_called()


@pytest.mark.asyncio
async def test_a_hung_terminal_report_does_not_keep_a_released_result() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    info = _info("a1")
    assert _hold(reg, PARENT, "a1", info)
    never = asyncio.Event()
    report = asyncio.get_running_loop().create_task(never.wait())
    mgr._report_owners = {report: info}
    try:
        with (
            patch("kiro_crew.subagent.INJECTION_TIMEOUT", 0),
            patch("kiro_crew.subagent_inline_collection.TERMINAL_REPORT_GRACE_SECS", 0.01),
        ):
            reg.commit(PARENT, [], released=["a1"])  # not returned: released for ordinary delivery
            await _drain(reg)
        mgr._on_done.assert_awaited_once()
        assert PARENT not in reg._records
        assert not report.done()  # the hung report itself is never cancelled
    finally:
        report.cancel()


@pytest.mark.parametrize("answer", ["written", "unanswered"])
def test_the_outcome_report_carries_the_calls_caller_identity(answer: str) -> None:
    """A pooled backend's only credential is the per-call caller block, so the
    commit the hook sends must carry it."""
    import threading

    from kiro_crew import mcp_core
    from kiro_crew.mcp_caller import CallerContext, set_current_caller

    caller = CallerContext(session_key=PARENT, from_gateway=True, session_token="forwarded-token")
    sent: list[tuple[str, dict[str, str]]] = []
    reported = threading.Event()

    def _post(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        sent.append((path, mcp_core._session_token_header()))
        reported.set()
        return {}

    step = _collected_call(["a1"])

    with patch("kiro_crew.mcp_core._post", side_effect=_post):
        # The driver installs the call's caller for the hook and clears it after.
        set_current_caller(caller)
        try:
            step.on_answered(answer == "written")
        finally:
            set_current_caller(None)
        assert reported.is_set()  # sent before the hook returned
    from kiro_crew.session_token_sig import session_token_header

    expected = session_token_header("forwarded-token")
    assert expected, "the header helper produced nothing to compare against"
    assert sent == [("/api/spawn/mark-collected", expected)]


def _real_info(aid: str, result: str, result_path: str) -> Any:
    from kiro_crew.subagent import SubagentInfo

    info = SubagentInfo(id=aid, task="t", parent_session_key=PARENT)
    info.done = True
    info.result = result
    info.result_path = result_path
    return info


@pytest.mark.asyncio
async def test_a_released_member_reads_its_full_result_from_the_run() -> None:
    """``completion_keep_chars=0`` leaves the run's result uncapped. The registry
    keeps no text of it: a release reads the whole result back from the run, and
    while held the run is pinned against completed-run eviction."""
    import gc
    import weakref

    from kiro_crew.context_management import evict_completed_agents

    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    text = "h" * 1_000 + "x" * 5_000_000 + "THE-END"
    info = _real_info("a1", text, "")
    info._delivery_queued = True  # what the gateway sets beside the hold
    assert _hold(reg, PARENT, "a1", info)
    # The run lives in the manager's table; a wave of later completions would
    # evict it by count unless the hold pins it.
    agents = {"a1": info}
    for n in range(60):
        other = _real_info(f"o{n}", "", "")
        other.started = info.started + 1 + n
        agents[other.id] = other
    info.started = 0.0  # the oldest, so the first a count eviction would take
    evict_completed_agents(agents, max_retained=5, pinned=reg.pins)
    assert "a1" in agents, "a held run was evicted while held"
    rec = reg._records[PARENT]["a1"]
    assert not any(
        isinstance(v, str) and v not in ("a1", PARENT, rec.state, rec.call)
        for v in vars(rec).values()
    ), "the registry kept text of the held result"
    reg.commit(PARENT, [], released=["a1"])  # not returned: released for ordinary delivery
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.result == text and ticket.result_truncated is False
    assert ticket is not info and info._delivery_queued is True  # the run's flags untouched
    assert not reg.pins(info), "a released run stays pinned"
    _live_runs(reg).clear()
    run = weakref.ref(info)
    del info, ticket, agents
    mgr._on_done.reset_mock()
    gc.collect()
    assert run() is None, "the registry kept the run object alive"


@pytest.mark.asyncio
async def test_a_failed_member_is_released_with_the_runs_own_error() -> None:
    """Error and status come from the live run at release, never from a copy,
    so a failure that landed after the hold is announced as the failure."""
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    info = _real_info("a1", "partial output", "")
    assert _hold(reg, PARENT, "a1", info)
    info.error = "e" * 100_000  # the live run's error, whole
    reg.commit(PARENT, [], released=["a1"])
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.error == "e" * 100_000 and ticket.outcome != "completed"
    assert ticket.result == "partial output"


@pytest.mark.asyncio
@pytest.mark.parametrize("on_disk", [True, False])
async def test_a_run_gone_at_release_is_an_explicit_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch, on_disk: bool
) -> None:
    """The held run's record left the manager (a restart-free teardown path).
    The release is an explicit error naming what happened, never an empty
    success, and points at the result file when it is still readable."""
    import kiro_crew.subagent_persistence as persistence

    run_dir = tmp_path / "a1"
    run_dir.mkdir()
    if on_disk:
        (run_dir / "result.txt").write_text("the whole result", encoding="utf-8")
    monkeypatch.setattr(persistence, "agent_dir_for_display", lambda aid: tmp_path / aid)
    reg, _clock, mgr = _registry()
    mgr.get = MagicMock(return_value=None)
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    assert _hold(reg, PARENT, "a1")
    reg.commit(PARENT, [], released=["a1"])
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.id == "a1" and ticket.parent_session_key == PARENT
    assert ticket.error.startswith("[run record gone before its held result was released")
    assert ticket.outcome != "completed"
    assert ticket.result == ""
    if on_disk:
        assert str(run_dir / "result.txt") in ticket.error
        assert ticket.result_path == str(run_dir / "result.txt")
    else:
        assert ticket.error.endswith("result not retained]") and ticket.result_path == ""
    assert _settled_ids(mgr) == [], "a gone run was marked delivered"


@pytest.mark.asyncio
async def test_a_returned_member_whose_run_is_gone_is_not_marked_delivered() -> None:
    """The written response carried it, but there is no run to settle: the
    delivered mark is left to restart recovery (a duplicate, never a loss)."""
    reg, _clock, mgr = _registry()
    mgr.get = MagicMock(return_value=None)
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    assert _hold(reg, PARENT, "a1")
    tasks = reg.commit(PARENT, ["a1"], released=["a1"])
    assert [await t for t in tasks] == ["undelivered"]
    assert _settled_ids(mgr) == []
    mgr._on_done.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_path", ["", "/no/transcript/was/written/result.txt"])
async def test_a_held_result_with_no_transcript_is_released_whole(result_path: str) -> None:
    """Incognito and temporary runs write no ``result.txt``, so the run's text is
    the only copy: an unwritten reply releases all of it, not a preview."""
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    text = "".join(f"line {n}\n" for n in range(2_000))  # about 17,000 chars
    assert _hold(reg, PARENT, "a1", _real_info("a1", text, result_path))
    reg.commit(PARENT, [], released=["a1"])
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.result == text and ticket.result_truncated is False


@pytest.mark.asyncio
async def test_a_released_result_still_waits_for_its_own_terminal_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record keeps only the id, so the run's report is found by it: the
    bounded wait runs (and says so when it gives up) instead of being skipped."""
    import logging

    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    info = _info("a1")
    assert _hold(reg, PARENT, "a1", info)
    never = asyncio.Event()
    report = asyncio.get_running_loop().create_task(never.wait())
    mgr._report_owners = {report: info}
    try:
        with (
            caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_inline_collection"),
            patch("kiro_crew.subagent.INJECTION_TIMEOUT", 0),
            patch("kiro_crew.subagent_inline_collection.TERMINAL_REPORT_GRACE_SECS", 0.01),
        ):
            reg.commit(PARENT, [], released=["a1"])
            await _drain(reg)
        assert any("terminal report is still running" in r.message for r in caplog.records)
        mgr._on_done.assert_awaited_once()
    finally:
        report.cancel()


def _pinned(reg: InlineCollections) -> int:
    """How many runs the registry pins, asked through ``pins`` itself."""
    return sum(
        1
        for recs in reg._records.values()
        for rec in recs.values()
        if reg.pins(_real_info(rec.aid, "", "") if rec.parent == PARENT else None)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["release", "written", "retire", "expiry", "discard", "shutdown"],
)
async def test_every_terminal_path_releases_the_pin(path: str) -> None:
    """A held run is pinned against completed-run eviction only while its record
    is on the registry. Every way a record leaves (a commit that releases it, a
    written commit, the parent's teardown, expiry, a refused spawn, a delivery
    cancelled at shutdown) takes the pin with it, so ``evict_completed_agents``
    can evict the run again. The pins are records, so the registry's
    ``MAX_IDS_PER_PARENT`` cap bounds them."""
    from kiro_crew.context_management import evict_completed_agents

    reg, clock, _mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    info = _real_info("a1", "done", "")
    info._delivery_queued = True
    assert _hold(reg, PARENT, "a1", info)
    assert _pinned(reg) == 1 and reg.pins(info)
    if path == "release":
        reg.commit(PARENT, [], released=["a1"])
        await _drain(reg)
    elif path == "written":
        await asyncio.gather(*reg.commit(PARENT, ["a1"]))
    elif path == "retire":
        reg.retire(PARENT)
    elif path == "expiry":
        clock.now += 60 + 300 + 1
        reg._expire(PARENT)
        await _drain(reg)
    elif path == "discard":
        reg.discard(PARENT, "a1")
        await _drain(reg)
        assert reg.pins(info), "a refused spawn's held run is still owned by its call"
        reg.commit(PARENT, [], call="c1")  # the call's end releases what it held
        await _drain(reg)
    else:
        hang = asyncio.Event()
        reg._manager._sessions.is_busy = MagicMock(return_value=True)
        with patch.object(reg, "_await_parent_idle", side_effect=lambda _p: hang.wait()):
            reg.commit(PARENT, [], released=["a1"])
            await asyncio.sleep(0)
            for task in list(reg._tasks):
                task.cancel()  # the gateway's shutdown cancels the delivery
            await asyncio.gather(*reg._tasks, return_exceptions=True)
    assert _pinned(reg) == 0 and not reg.pins(info), f"{path} left the run pinned"
    agents = {"a1": info, **{f"o{n}": _real_info(f"o{n}", "", "") for n in range(3)}}
    info.started = 0.0
    evict_completed_agents(agents, max_retained=3, pinned=reg.pins)
    assert "a1" not in agents, f"{path}: retention still cannot evict the run"


@pytest.mark.asyncio
async def test_a_refused_spawn_whose_run_already_finished_delivers_the_held_result() -> None:
    """A refused spawn never loses or early-delivers a result its run already
    produced, for cron and channel parents as for tabs. A nested child can
    finish with output and an error while its spawn awaits the store, so its
    completion is HELD before ``api_spawn`` sees ``info.done and info.error``
    and discards the reservation. The parent's turn is still blocked in the
    call, and a cron or channel parent never looks busy to the idle poll, so
    the discard must deliver NOTHING yet: the held record stays until the
    call's own end releases it as an ordinary completion. Delivering at
    discard lands it in the blocked turn; removing the record loses it for
    good."""
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    info = _real_info("a1", "partial output", "")
    info.error = "child failed"
    info._delivery_queued = True
    assert _hold(reg, PARENT, "a1", info)
    reg.discard(PARENT, "a1")  # the refused-spawn path in api_spawn
    await _drain(reg)
    mgr._on_done.assert_not_awaited()  # never into the turn still blocked in the call
    assert reg._records[PARENT]["a1"].state == "held"
    reg.commit(PARENT, [], call="c1")  # the call ends without naming it
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.result == "partial output" and ticket.error == "child failed"
    assert not reg._records.get(PARENT)


@pytest.mark.asyncio
async def test_a_refused_spawn_with_nothing_held_delivers_nothing() -> None:
    """The ordinary refusal: nothing ran, so the record is forgotten."""
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    reg.discard(PARENT, "a1")
    await _drain(reg)
    mgr._on_done.assert_not_awaited()
    assert not reg._records.get(PARENT)


@pytest.mark.asyncio
async def test_a_member_is_pinned_from_its_reservation_not_its_hold() -> None:
    """A member can finish, and be followed by many newer completions, before
    its terminal report reaches ``hold``. The pin is taken at ``reserve``, so
    count eviction in that window cannot take the run, and the release returns
    the FULL result, never the "run record gone" error. Pinning only at hold
    turns this red."""
    from kiro_crew.context_management import evict_completed_agents

    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    text = "r" * 200_000 + "THE-END"
    info = _real_info("a1", text, "")  # completed, not yet held
    info.started = 0.0  # the oldest, so the first a count eviction would take
    agents = {"a1": info}
    for n in range(60):
        other = _real_info(f"o{n}", "", "")
        other.started = 1.0 + n
        agents[other.id] = other
    evict_completed_agents(agents, max_retained=50, pinned=reg.pins)
    assert "a1" in agents, "a reserved, completed run was evicted before its hold"
    _live_runs(reg)["a1"] = agents["a1"]
    info._delivery_queued = True
    assert reg.hold(PARENT, "a1")
    reg.commit(PARENT, [], released=["a1"])  # not returned: released for ordinary delivery
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.result == text and not ticket.error
    assert "run record gone" not in (ticket.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["release", "written", "retire", "expiry", "discard", "consumed"],
)
async def test_every_reserve_time_path_releases_the_pin(path: str) -> None:
    """The pin ``reserve`` takes ends with collection ownership on every path a
    record can take before it is ever held: a commit without it, a written
    commit (only the ``returned`` marker is left), the parent's teardown,
    expiry, a refused spawn, and a consumed completion."""
    from kiro_crew.context_management import evict_completed_agents

    reg, clock, _mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60, call="c1")
    info = _real_info("a1", "done", "")
    assert _pinned(reg) == 1 and reg.pins(info)
    if path == "release":
        reg.commit(PARENT, [], released=["a1"])
    elif path == "written":
        await asyncio.gather(*reg.commit(PARENT, ["a1"]))
    elif path == "retire":
        reg.retire(PARENT)
    elif path == "expiry":
        clock.now += 60 + 300 + 1
        reg._expire(PARENT)
    elif path == "discard":
        reg.discard(PARENT, "a1")
    else:
        await asyncio.gather(*reg.commit(PARENT, ["a1"]))
        assert reg.consume_collected(PARENT, "a1")
    await _drain(reg)
    assert _pinned(reg) == 0 and not reg.pins(info), f"{path} left the run pinned"
    agents = {"a1": info, **{f"o{n}": _real_info(f"o{n}", "", "") for n in range(3)}}
    info.started = 0.0
    evict_completed_agents(agents, max_retained=3, pinned=reg.pins)
    assert "a1" not in agents, f"{path}: retention still cannot evict the run"


def test_a_reservation_refused_at_the_cap_takes_no_pin() -> None:
    """The pins are the registry's records, so ``MAX_IDS_PER_PARENT`` bounds
    them: a reservation past the cap is refused and pins nothing."""
    from kiro_crew.subagent_inline_collection import MAX_IDS_PER_PARENT

    reg, _clock, _mgr = _registry()
    for n in range(MAX_IDS_PER_PARENT):
        assert reg.reserve(PARENT, f"m{n}", 60, call="c1")
    assert not reg.reserve(PARENT, "over", 60, call="c1")
    assert not reg.pins(_real_info("over", "", ""))
    assert _pinned(reg) == MAX_IDS_PER_PARENT
