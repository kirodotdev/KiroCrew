"""A blocking ``spawn_sub_agents`` call's members are never injected into its parent.

A cron run's turn blocked in ``spawn_sub_agents`` must survive its first child
finishing: that child's ``[Subagent completion event]`` is the call's own result,
so injecting it into the parent session would interrupt the very turn waiting on
it. Every parent kind follows the same rule as the dashboard route. These tests
drive the real seams end to end: ``/api/spawn``'s reservation, the tool's closing
``mark-collected`` through the dashboard handler, then the gateway's completion
callback, for each parent kind that injects.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_handlers_messaging_coverage import _info as _run_info
from test_handlers_messaging_coverage import _mgr, _payload
from test_handlers_messaging_coverage import _Req as _CoverageReq
from test_handlers_messaging_coverage import _state

import kiro_crew.dashboard.handlers.messaging as handlers
import kiro_crew.subagent_inline_collection as sic
from kiro_crew.mcp_core import _call_tool
from kiro_crew.subagent_inline_collection import (
    CLAIM_TTL_SECS,
    COLLECTED_TTL_SECS,
    COLLECTION_GRACE_SECS,
    MAX_AGENT_ID_CHARS,
    MAX_CALL_ID_CHARS,
    MAX_COLLECTION_TTL_SECS,
    MAX_IDS_PER_PARENT,
    MAX_PARENT_KEY_CHARS,
    InlineCollections,
)

CRON_PARENT = "cron:job1:run1"
SLACK_PARENT = "C123:1234.567890"


def _hold(reg: Any, parent: str, aid: str, info: Any = None) -> bool:
    """``reg.hold`` with *info* as the live run its release reads back.

    The registry keeps only the id, so the run record a test hands in is what
    the bound manager's ``get`` answers for it, as ``_agents`` does live.
    """
    if info is not None and reg._manager is not None:
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


class _Req:
    """Minimal internal request double for the mark-collected handler."""

    def __init__(self, state: Any, body: Any) -> None:
        self.app: dict[str, Any] = {"state": state}
        self._body = body
        self.match_info: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.remote = "127.0.0.1"
        self._extra = {"app": "", "user": "U0OWNER0000"}

    def __contains__(self, key: str) -> bool:
        return key in self._extra

    def __getitem__(self, key: str) -> Any:
        return self._extra[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)

    async def json(self) -> Any:
        return self._body


def _gateway(registry: InlineCollections) -> tuple[Any, Any, Any]:
    """``(orchestrator, on_done, dashboard_state)`` with *registry* on the manager."""
    from test_slack_gateway import (
        _make_orchestrator,
        _mock_context_builder,
        _mock_dashboard_state,
        _mock_sessions,
    )

    orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
    orch.sessions = _mock_sessions()
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.ctx_builder = _mock_context_builder()
    orch.ctx_builder.hooks = MagicMock()
    orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    orch.dashboard_state = _mock_dashboard_state()
    orch.dashboard_state.get_slot = MagicMock(return_value=None)
    orch.slack = MagicMock()
    orch.slack.open_dm = AsyncMock(return_value="D_U1")
    orch.slack.post_message = AsyncMock()
    orch.slack.post_blocks = AsyncMock(return_value="ts")
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            mgr = MagicMock()
            mgr.inline_collections = registry
            mgr.start_reaper = MagicMock()
            mgr.running = []
            mgr.queued_count_for = MagicMock(return_value=0)
            mgr.queued_count_for_async = AsyncMock(return_value=0)
            mgr.has_pending_work_for = MagicMock(return_value=False)
            mgr.has_pending_work_for_async = AsyncMock(return_value=False)
            mgr.running_agents_for = MagicMock(return_value=[])
            # Every member id is one this gateway spawned; a run that reached
            # the route is in the manager's table, as ``_agents`` holds it.
            mgr._agents = {}
            mgr.get = MagicMock(side_effect=lambda aid: mgr._agents.get(aid, object()))
            mgr.notify_injection_failed = MagicMock()
            mgr.settle_queued_delivery = AsyncMock()
            mock_sm.return_value = mgr
            orch._init_subagents()
    route = mock_sm.call_args[1]["on_done"]

    async def on_done(info: Any) -> Any:
        # The terminal report hands the route a run the manager holds.
        mgr._agents.setdefault(info.id, info)
        return await route(info)

    # The registry delivers through the manager it is bound to: the gateway's
    # route, its sessions, and the terminal reports still running.
    mgr._on_done = route
    mgr._sessions = orch.sessions
    mgr._report_owners = {}
    registry.bind(mgr)
    return orch, on_done, mgr


def _info(agent_id: str, parent: str) -> Any:
    from kiro_crew.subagent import SubagentInfo

    # A real run record: the registry holds only a dataclass it can bound.
    info = SubagentInfo(id=agent_id, task="review a file", parent_session_key=parent)
    info.done = True
    info.result = f"{agent_id} result"
    info.elapsed = 1.0
    info.started = 0.0
    return info


async def _post(orch: Any, mgr: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Drive the real ``mark-collected`` handler the way the tool's ``_post`` reaches it."""
    state = orch.dashboard_state
    state.subagents = mgr
    resp = await handlers.api_spawn_mark_collected(_Req(state, body))
    raw = resp.body
    assert isinstance(raw, (bytes, bytearray))
    return json.loads(raw)


async def _mark(orch: Any, mgr: Any, body: dict[str, Any]) -> dict[str, Any]:
    """The tool's two steps for a written response: its claim, then its commit."""
    claimed = await _post(orch, mgr, {**body, "phase": "claim"})
    if claimed.get("status") != "ok":
        return claimed
    return await _post(orch, mgr, {**body, "phase": "commit"})


def _assert_parent_untouched(orch: Any, stream: AsyncMock) -> None:
    """Nothing prompted, cancelled or claimed the parent's session."""
    stream.assert_not_awaited()
    orch.sessions.get_or_create.assert_not_awaited()
    orch.sessions.cancel_current.assert_not_awaited()


async def _drain(orch: Any) -> None:
    """Wait (bounded) for every delivery the registry and the gateway started."""
    registry = orch.subagent_mgr.inline_collections
    for _ in range(5):
        await asyncio.sleep(0)
        pending = [*registry._tasks, *orch._background_tasks]
        if not pending:
            return
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10)


def _stream() -> Any:
    return patch(
        "kiro_crew.slack.gateway.stream_and_collect",
        new_callable=AsyncMock,
        return_value="llm response",
    )


class TestCronParentBlockedInSpawnSubAgents:
    @pytest.mark.asyncio
    async def test_first_child_finishing_does_not_touch_the_blocked_parent_turn(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        # /api/spawn reserved both members for the call, which now polls them.
        assert registry.reserve(CRON_PARENT, "a1", 600)
        assert registry.reserve(CRON_PARENT, "a2", 600)
        with _stream() as stream:
            # The first member finishes while the parent is still blocked.
            first = _info("a1", CRON_PARENT)
            await on_done(first)
            _assert_parent_untouched(orch, stream)
            # Held, so not delivered: its retention clock has not started.
            assert first._delivery_queued is True

            # The call returns both members inline and ends its collection.
            await _mark(
                orch,
                mgr,
                {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": CRON_PARENT},
            )
            # The second member's completion lands after the call returned it.
            await on_done(_info("a2", CRON_PARENT))
            await _drain(orch)
            _assert_parent_untouched(orch, stream)

        orch.dashboard_state.notify.assert_not_called()
        # The held member is settled when the call returned it, exactly once.
        settled = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [[d.agent_id for d in batch] for batch in settled] == [["a1"]]
        # Nothing is left behind that a later completion could trip over.
        assert not _hold(registry, CRON_PARENT, "a1", object())
        assert not registry.consume_collected(CRON_PARENT, "a1")
        assert not registry.consume_collected(CRON_PARENT, "a2")

    @pytest.mark.asyncio
    async def test_a_held_completion_resets_the_cron_parent_only_once_it_is_idle(
        self,
    ) -> None:
        """The held member was the parent's last sub-agent, so the idle reset the
        injecting branch would do still runs, but only as the skip-if-busy reset:
        the parent's own turn holds the session, and an unconditional reset
        would tear that turn down."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        orch.sessions.reset = AsyncMock(return_value=False)
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            _assert_parent_untouched(orch, stream)
        orch.sessions.reset.assert_awaited_once_with(CRON_PARENT, skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_a_held_memory_wait_expiry_settles_its_owed_report(self) -> None:
        """A member that expired waiting for memory has no folder, only the
        store's owed report. The hold parked it, so its report task did not
        clear that debt; the call returning it must, or the next start replays
        a failure the tool already returned inline."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        expiry = _info("a1", CRON_PARENT)
        expiry.error = "memory wait expired"
        expiry._report_owed = True
        with _stream() as stream:
            await on_done(expiry)
            await _mark(
                orch, mgr, {"ids": ["a1"], "released": ["a1"], "parent_session": CRON_PARENT}
            )
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [(d.agent_id, d.report_owed) for d in batch] == [("a1", True)]

    @pytest.mark.asyncio
    async def test_a_member_the_call_left_running_is_still_injected(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        # The wait expired with a1 still running: released, never collected.
        await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
        stream.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("on_disk", [False, True], ids=["transient", "transcript"])
    async def test_a_long_held_result_is_delivered_as_the_live_run_would_be(
        self, tmp_path: Any, on_disk: bool
    ) -> None:
        """Through the real route. The release reads the live run, with
        ``result.txt`` (whose path rides along) or without it (incognito,
        temporary), so the delivered text is the result whole."""
        transcript = tmp_path / "result.txt"
        if on_disk:
            transcript.write_text("the whole result", encoding="utf-8")
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        info.result = "head-" + "r" * 600_000 + "-tail"
        info.result_path = str(transcript)
        with _stream() as stream:
            await on_done(info)
            _assert_parent_untouched(orch, stream)
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
            stream.assert_awaited_once()
        # The announce the route built, handed to the context builder as the turn's text.
        (prompt,) = [c.args[0] for c in orch.ctx_builder.build_message.call_args_list]
        assert info.result in prompt
        assert registry._records == {}

    @pytest.mark.asyncio
    async def test_a_held_member_the_cancelled_call_never_returned_is_delivered(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        registry.reserve(CRON_PARENT, "a2", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            _assert_parent_untouched(orch, stream)
            # Cancelled before returning anything: a1 is delivered as an
            # ordinary completion, once the call has let go of the turn.
            await _mark(
                orch, mgr, {"ids": [], "released": ["a1", "a2"], "parent_session": CRON_PARENT}
            )
            await _drain(orch)
            stream.assert_awaited_once()
        settled = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [[d.agent_id for d in batch] for batch in settled] == [["a1"]]

    @pytest.mark.asyncio
    async def test_a_cancelled_calls_held_member_waits_for_the_parents_turn_to_end(
        self,
    ) -> None:
        """The cancel close lands while the parent's turn is still ending: the
        released result is delivered only once that turn has let go of the
        session, as a new turn after it, never as a prompt inside it."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        events: list[str] = []
        busy = iter([True, True, False])

        def _is_busy(key: str) -> bool:
            assert key == CRON_PARENT
            answer = next(busy, False)
            events.append(f"busy={answer}")
            return answer

        orch.sessions.is_busy = MagicMock(side_effect=_is_busy)
        registry.reserve(CRON_PARENT, "a1", 600)
        with (
            _stream() as stream,
            patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0),
        ):
            stream.side_effect = lambda *a, **k: events.append("prompt") or "ok"
            await on_done(_info("a1", CRON_PARENT))
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
        assert events == ["busy=True", "busy=True", "busy=False", "prompt"]

    @pytest.mark.asyncio
    async def test_a_released_result_whose_parent_was_retired_is_not_injected(self) -> None:
        """The parent was retired while the result was held: the registry's
        fence applies, so nothing recreates or reaches the retired conversation,
        and no delivered mark is written."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            registry.retire(CRON_PARENT)
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        mgr.settle_queued_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_parent_retired_during_the_idle_wait_gets_nothing(self) -> None:
        """The close lands while the parent's turn is still ending, and the
        parent is retired during that wait (``!agent default`` removes the
        session). The fence is read at the point of delivery, after the wait,
        so nothing is delivered, nothing is recreated, and an owed report is
        cleared rather than replayed into a later conversation."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        busy = iter([True, True, False])

        def _is_busy(_key: str) -> bool:
            answer = next(busy, False)
            if not answer:
                # The parent's turn ended because the session was removed.
                registry.retire(CRON_PARENT)
            return answer

        orch.sessions.is_busy = MagicMock(side_effect=_is_busy)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        info._report_owed = True
        with (
            _stream() as stream,
            patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0),
        ):
            await on_done(info)
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._admission.taskq_clear_owed_reports.assert_called_once_with(["a1"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", [SLACK_PARENT, CRON_PARENT])
    async def test_a_parent_retired_inside_the_route_gets_nothing(self, parent: str) -> None:
        """The fence passed and the released result is inside the parent's
        route when ``!agent default`` removes the parent, during the route's
        awaited ``session_store_for_turn``. Retirement cancels the delivery
        there, so routing never resumes into ``get_or_create`` or the
        injection, and an owed report is cleared."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(parent, "a1", 600)
        info = _info("a1", parent)
        info._report_owed = True
        entered = asyncio.Event()

        async def _store(*_a: Any, **_k: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()  # parked until retire cancels it

        with (
            _stream() as stream,
            patch("kiro_crew.slack.gateway.session_store_for_turn", side_effect=_store),
        ):
            await on_done(info)
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": parent})
            await asyncio.wait_for(entered.wait(), timeout=5)
            registry.retire(parent)
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._admission.taskq_clear_owed_reports.assert_called_once_with(["a1"])
        assert parent not in registry._records

    @pytest.mark.asyncio
    async def test_a_completion_landing_during_the_closes_settle_is_not_injected(
        self,
    ) -> None:
        """The close settles a held member, and while it awaits that settle the
        second member's completion callback runs. The id the call returned is
        already recorded by then, so the cron turn still blocked on the close
        is never prompted or cancelled."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        registry.reserve(CRON_PARENT, "a2", 600)
        landed: list[Any] = []

        async def _settle(_deliveries: Any) -> None:
            landed.append(await on_done(_info("a2", CRON_PARENT)))

        mgr.settle_queued_delivery.side_effect = _settle
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            await _mark(
                orch,
                mgr,
                {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": CRON_PARENT},
            )
            await _drain(orch)
            assert landed, "the settle hook did not run"
            _assert_parent_untouched(orch, stream)

    @pytest.mark.asyncio
    async def test_a_returned_member_whose_record_was_evicted_is_not_redelivered(
        self,
    ) -> None:
        """The held member's run record is gone before the close (a hold pins it
        against ``evict_completed_agents``, so another path removed it). The
        call still returned it, so it is never treated as an orphan and
        injected into the turn blocked on the close. With no run to settle, its
        delivered mark is left to restart recovery: a duplicate, never a loss."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        mgr.get.side_effect = lambda aid: None if aid == "a1" else object()
        registry.reserve(CRON_PARENT, "a1", 600)
        registry.reserve(CRON_PARENT, "a2", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            await _mark(
                orch,
                mgr,
                {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": CRON_PARENT},
            )
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        settled = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [[d.agent_id for d in batch] for batch in settled] == []

    @pytest.mark.asyncio
    async def test_the_orphan_waits_for_the_original_report_before_releasing_the_hold(
        self,
    ) -> None:
        """The hold is taken inside the run's own terminal report, which reads
        ``_delivery_queued`` once ``_on_done`` returns to decide whether to write
        the ``delivered`` mark. The orphan delivery waits for that report, and
        redelivers on a separate ticket, so the original record's flags are never
        touched and a failed reinjection is never tombstoned."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        release_report = asyncio.Event()
        seen: list[bool] = []

        async def _terminal_report() -> None:
            await on_done(info)
            # The original report is still finishing (e.g. the idle reset).
            await release_report.wait()
            seen.append(info._delivery_queued)

        report = asyncio.get_running_loop().create_task(_terminal_report())
        mgr._report_owners = {report: info}
        with _stream():
            await asyncio.sleep(0)
            assert info._delivery_queued is True
            orch.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no runtime"))
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            for _ in range(5):
                await asyncio.sleep(0)
            # The reinjection has not run: the report still owns the hold.
            orch.sessions.get_or_create.assert_not_awaited()
            release_report.set()
            await asyncio.wait_for(report, timeout=5)
            await _drain(orch)
        assert seen == [True]
        # The failure was recorded on the ticket; the original is untouched.
        assert info._delivery_queued is True
        assert info._report_undelivered is False
        mgr.settle_queued_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_released_result_the_route_failed_to_inject_stays_undelivered(
        self,
    ) -> None:
        """A cron injection that fails marks the run undelivered; the release
        path must leave its folder un-tombstoned for the restart replay."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        with _stream():
            await on_done(info)
            orch.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no runtime"))
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
        orch.sessions.get_or_create.assert_awaited()
        assert info._delivery_queued is True
        mgr.settle_queued_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_collection_whose_close_never_arrives_delivers_what_it_held(self) -> None:
        now = [0.0]
        registry = InlineCollections(clock=lambda: now[0])
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 60)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            _assert_parent_untouched(orch, stream)
            # The closing mark was lost; the collection outlives its bound.
            now[0] = 60 + COLLECTION_GRACE_SECS + 1
            _hold(registry, CRON_PARENT, "other", object())
            await _drain(orch)
            stream.assert_awaited_once()


class TestApiSpawnReservesBeforeTheRunExists:
    def _mgr(self, registry: InlineCollections, *, refuse: bool = False) -> Any:
        mgr = _mgr()
        mgr.inline_collections = registry
        mgr.reserve_inline_member = lambda parent, wait, call="": (
            "a1" if registry.reserve(parent, "a1", wait, call=call) else ""
        )
        held_when_spawned: list[bool] = []

        def _spawn(task: str, **kw: Any) -> Any:
            # Only probes the reservation: whatever the hold keeps is the
            # handler's to release, so a refused spawn exercises its release.
            held_when_spawned.append(
                _hold(
                    registry,
                    CRON_PARENT,
                    kw["_preassigned_id"],
                    _info(kw["_preassigned_id"], CRON_PARENT),
                )
            )
            if refuse:
                return _run_info(id=kw["_preassigned_id"], done=True, error="cwd not allowed")
            return _run_info(id=kw["_preassigned_id"])

        mgr.spawn.side_effect = _spawn
        mgr.held_when_spawned = held_when_spawned
        return mgr

    def _call(self, mgr: Any) -> Any:
        body = {
            "task": "x",
            "parent_session": CRON_PARENT,
            "inline_collect": True,
            "max_wait": 600,
        }
        with patch.object(
            handlers, "_spawn_request_memory_mode", AsyncMock(return_value="persistent")
        ):
            return asyncio.run(handlers.api_spawn(_CoverageReq(_state(subagents=mgr), body)))

    def test_the_minted_id_is_held_before_spawn_starts_it(self) -> None:
        registry = InlineCollections()
        mgr = self._mgr(registry)
        resp = self._call(mgr)
        assert _payload(resp)["id"] == "a1"
        assert mgr.spawn.call_args.kwargs["_preassigned_id"] == "a1"
        assert mgr.held_when_spawned == [True]

    def test_a_refused_spawn_releases_its_reservation(self) -> None:
        registry = InlineCollections()
        mgr = self._mgr(registry, refuse=True)
        assert self._call(mgr).status == 400
        assert not _hold(registry, CRON_PARENT, "a1", object())

    def test_a_spawn_that_raises_releases_its_reservation(self) -> None:
        registry = InlineCollections()
        mgr = self._mgr(registry)
        mgr.spawn.side_effect = RuntimeError("spawn failed")
        with pytest.raises(RuntimeError):
            self._call(mgr)
        assert CRON_PARENT not in registry._records

    def test_a_full_collection_refuses_before_spawning(self) -> None:
        registry = InlineCollections()
        for i in range(MAX_IDS_PER_PARENT):
            registry.reserve(CRON_PARENT, f"x{i}", 600)
        mgr = self._mgr(registry)
        resp = self._call(mgr)
        assert resp.status == 429
        assert _payload(resp)["code"] == "inline_collection_full"
        mgr.spawn.assert_not_called()


class TestOtherParentKinds:
    @pytest.mark.asyncio
    async def test_a_channel_thread_parent_is_held_the_same_way(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(SLACK_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", SLACK_PARENT))
            _assert_parent_untouched(orch, stream)

    @pytest.mark.asyncio
    async def test_a_dashboard_tab_parent_is_held_while_its_slot_is_idle(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "chat-1"
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        parent = "dashboard:chat-1"
        registry.reserve(parent, "a1", 600)
        with (
            patch("kiro_crew.slack.gateway.dashboard_slot_key", return_value="chat-1"),
            patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            await on_done(_info("a1", parent))
            await asyncio.sleep(0)
        # No turn was launched on the tab, and nothing was queued for one.
        assert slot.task is None
        slot.queue_append.assert_not_called()
        run_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_tabs_returned_ids_live_on_the_registry_like_any_parent(self) -> None:
        """One store for "the call returned this id": a tab's ids are consumed
        from the registry, and its synthesis turn is disarmed once none remain."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "chat-1"
        slot._queue = []
        slot._pending_synthesis = True
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        parent = "dashboard:chat-1"
        assert registry.reserve(parent, "a1", 600) and registry.reserve(parent, "a2", 600)
        with (
            patch(
                "kiro_crew.dashboard.handlers.messaging.dashboard_slot_key", return_value="chat-1"
            ),
            patch("kiro_crew.slack.gateway.dashboard_slot_key", return_value="chat-1"),
            patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            await _mark(
                orch, mgr, {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": parent}
            )
            assert registry.has_collected(parent)
            await on_done(_info("a1", parent))
            assert slot._pending_synthesis is True  # a2 is still to come
            await on_done(_info("a2", parent))
            await asyncio.sleep(0)
        assert slot._pending_synthesis is False
        assert not registry.has_collected(parent)
        slot.queue_append.assert_not_called()
        run_chat.assert_not_awaited()


class TestSpawnSubAgentsTool:
    def test_every_member_is_spawned_for_inline_collection_and_closed_with_the_call(
        self,
    ) -> None:
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
        ):
            mock_post.side_effect = [{"id": "a1"}, {"id": "a2"}, {}, {}]
            mock_get.return_value = {"done": True, "agent": "w", "result": "done"}
            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"}, {"prompt": "y"}]})
        spawns = [c.args[1] for c in mock_post.call_args_list if c.args[0] == "/api/spawn"]
        assert [(b["inline_collect"], b["max_wait"]) for b in spawns] == [(True, 7200.0)] * 2
        # One call id names every member and every step of this call.
        call = spawns[0]["inline_call"]
        assert re.fullmatch(r"[0-9a-f]{32}", call) and spawns[1]["inline_call"] == call
        claim, commit = mock_post.call_args_list[-2:]
        assert claim.args == (
            "/api/spawn/mark-collected",
            {
                "ids": ["a1", "a2"],
                "released": ["a1", "a2"],
                "parent_session": CRON_PARENT,
                "phase": "claim",
                "call": call,
            },
        )
        # A direct call has no dispatcher to report on its response: the result
        # is the caller's as soon as it returns, so the claim commits at once.
        assert commit.args == (
            "/api/spawn/mark-collected",
            {
                "ids": ["a1", "a2"],
                "released": ["a1", "a2"],
                "parent_session": CRON_PARENT,
                "phase": "commit",
                "call": call,
            },
        )

    @pytest.mark.parametrize(("delivered", "phase"), [(True, "commit"), (False, "drop")])
    def test_the_claim_settles_only_on_the_dispatchers_word(
        self, delivered: bool, phase: str
    ) -> None:
        """Dispatched, the call claims its results and returns; only the
        dispatcher's report on the response commits or releases the claim."""
        import threading

        from kiro_crew import mcp_shared

        reported = threading.Event()
        mcp_shared._arm_response_outcome(1)
        try:
            with (
                patch("kiro_crew.mcp_core._post") as mock_post,
                patch("kiro_crew.mcp_core._get") as mock_get,
                patch("kiro_crew.mcp_core.sel"),
                patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
            ):

                def _record(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
                    if body.get("phase") in ("commit", "drop"):
                        reported.set()
                    return {"id": "a1"} if path == "/api/spawn" else {}

                mock_post.side_effect = _record
                mock_get.return_value = {"done": True, "agent": "w", "result": "done"}
                _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
                phases = [c.args[1].get("phase") for c in mock_post.call_args_list[1:]]
                assert phases == ["claim"]  # nothing settles while the response is unwritten
                mcp_shared._settle_response_outcome(1, delivered)
                assert reported.wait(timeout=10)
                last = mock_post.call_args_list[-1].args[1]
        finally:
            mcp_shared._settle_response_outcome(1, False)
        assert last.pop("call") == mock_post.call_args_list[0].args[1]["inline_call"]
        assert last == {
            "ids": ["a1"],
            "parent_session": CRON_PARENT,
            "phase": phase,
            "released": ["a1"],
        }

    def test_a_cancelled_call_still_closes_its_collection(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.object(spawn_mod, "is_tool_cancelled", return_value=True),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
        ):
            mock_post.side_effect = [{"id": "a1"}, {}]
            mock_get.return_value = {"done": False}
            with pytest.raises(spawn_mod.ToolCancelled):
                spawn_mod.spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
        closing = mock_post.call_args_list[-1]
        assert closing.args[1].pop("call") == mock_post.call_args_list[0].args[1]["inline_call"]
        assert closing.args[1] == {
            "ids": [],
            "released": ["a1"],
            "parent_session": CRON_PARENT,
            "phase": "claim",
        }

    def test_a_call_cancelled_after_its_poll_returns_nothing(self) -> None:
        """Every member settled, then the turn was cancelled before the close
        (during the resume hold or the result reads). The results go to no
        turn, so the close names nothing returned and the held results are
        delivered as ordinary completions."""
        from kiro_crew.mcp_tools import spawn as spawn_mod

        checks = iter([False])
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.object(spawn_mod, "is_tool_cancelled", side_effect=lambda: next(checks, True)),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
        ):
            mock_post.side_effect = [{"id": "a1"}, {}]
            mock_get.return_value = {"done": True, "agent": "w", "result": "done"}
            with pytest.raises(spawn_mod.ToolCancelled):
                spawn_mod.spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
        closing = mock_post.call_args_list[-1]
        assert closing.args[1].pop("call") == mock_post.call_args_list[0].args[1]["inline_call"]
        assert closing.args[1] == {
            "ids": [],
            "released": ["a1"],
            "parent_session": CRON_PARENT,
            "phase": "claim",
        }

    def test_a_result_the_reply_truncation_cuts_is_not_claimed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Seven ordinary CJK results each under the summary threshold grow
        about sixfold under JSON escaping and pass the response cap, so the
        dispatcher cuts the tail. A member whose block is cut never reached the
        parent: it is released, never claimed."""
        ids = [f"a{i}" for i in range(7)]
        posts: list[dict[str, Any]] = []

        def _record(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            posts.append(body)
            return {"id": ids[len(posts) - 1]} if path == "/api/spawn" else {}

        with (
            patch("kiro_crew.mcp_core._post", side_effect=_record),
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
            caplog.at_level("WARNING", logger="kiro_crew.mcp_tools.spawn"),
        ):
            mock_get.return_value = {"done": True, "agent": "w", "result": "汉" * 3000}
            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"} for _ in ids]})
        claim = next(b for b in posts if b.get("phase") == "claim")
        assert claim["released"] == ids
        assert 0 < len(claim["ids"]) < len(ids)
        assert ids[-1] not in claim["ids"]
        (line,) = [r.getMessage() for r in caplog.records if "truncated" in r.getMessage()]
        assert f"{len(ids) - len(claim['ids'])} result(s)" in line

    def test_an_uncut_reply_claims_every_settled_member(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        assert spawn_mod._carried_in_reply(["x", "x"], {"a1": 0, "a2": 1}) == ["a1", "a2"]

    def test_the_cut_is_judged_by_position_at_the_exact_cap(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod
        from kiro_crew.validation import MAX_RESPONSE_LEN

        first = "a" * (MAX_RESPONSE_LEN - 2 - 10)
        # The second block ends exactly at the cap: kept whole. One more char cuts it.
        assert spawn_mod._carried_in_reply([first, "b" * 10, "c"], {"a1": 0, "a2": 1, "a3": 2}) == [
            "a1",
            "a2",
        ]
        assert spawn_mod._carried_in_reply([first, "b" * 11], {"a1": 0, "a2": 1}) == ["a1"]

    def test_a_failed_close_is_retried(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        pauses: list[float] = []
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch.object(spawn_mod, "_collection_retry_pause", pauses.append),
        ):
            mock_post.side_effect = [OSError("refused"), {}]
            spawn_mod._close_collection(CRON_PARENT, ["a1"], ["a1"])
        assert mock_post.call_count == 2
        assert pauses == [spawn_mod.COLLECTION_RETRY_PAUSES[0]]

    def test_a_close_answered_with_an_error_body_is_retried(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        pauses: list[float] = []
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch.object(spawn_mod, "_collection_retry_pause", pauses.append),
        ):
            mock_post.side_effect = [
                {"error": "HTTP 503"},
                {"error": "connection refused", "transport_error": True},
                {"status": "ok"},
            ]
            spawn_mod._close_collection(CRON_PARENT, ["a1"], ["a1"])
        assert mock_post.call_count == 3
        assert pauses == list(spawn_mod.COLLECTION_RETRY_PAUSES)


def _bound(registry: InlineCollections, *, busy: Any = False) -> Any:
    """A manager double the registry delivers through, recording what reached it."""
    mgr = MagicMock()
    mgr._on_done = AsyncMock()
    mgr._sessions.is_busy = MagicMock(return_value=busy)
    mgr._report_owners = {}
    mgr.settle_queued_delivery = AsyncMock()
    registry.bind(mgr)
    return mgr


async def _settled(registry: InlineCollections) -> None:
    while registry._tasks:
        await asyncio.wait_for(asyncio.gather(*registry._tasks), timeout=10)


class TestInlineCollections:
    @pytest.mark.asyncio
    async def test_held_then_collected_is_settled_by_the_registry(self) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve("p", "a1", 60)
        reg.reserve("p", "a2", 60)
        assert _hold(reg, "p", "a1", _info("a1", "p"))
        reg.finish("p", ["a1", "a2"], ["a1", "a2"])
        mgr.settle_queued_delivery.assert_not_awaited()  # claimed, not settled
        settles = reg.commit("p", ["a1", "a2"], True)
        assert await asyncio.gather(*settles) == ["delivered"]
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [d.agent_id for d in batch] == ["a1"]
        mgr._on_done.assert_not_awaited()
        assert not _hold(reg, "p", "a2", _info("a2", "p"))

    @pytest.mark.asyncio
    async def test_an_abandoned_collection_expires_and_delivers_what_it_held(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        mgr = _bound(reg)
        reg.reserve("p", "a1", 60)
        reg.reserve("p", "a2", 60)
        held = _info("a1", "p")
        assert _hold(reg, "p", "a1", held)
        now[0] = 60 + COLLECTION_GRACE_SECS + 1
        assert not _hold(reg, "p", "a2", _info("a2", "p"))
        await _settled(reg)
        (ticket,) = [c.args[0] for c in mgr._on_done.await_args_list]
        # Redelivered on a separate ticket: the original record is untouched.
        assert ticket is not held and ticket.id == "a1"
        assert held._delivery_queued is False

    def test_a_full_collection_refuses_a_new_member(self) -> None:
        reg = InlineCollections()
        assert all(reg.reserve("p", f"a{i}", 60) for i in range(MAX_IDS_PER_PARENT))
        assert not reg.reserve("p", "late", 60)
        # Re-reserving a member it already holds is not a new member.
        assert reg.reserve("p", "a0", 60)

    def test_a_returned_id_whose_completion_never_comes_is_forgotten(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        assert reg.reserve("p", "a1", 60) and reg.reserve("p", "a2", 60)
        reg.finish("p", ["a1", "a2"], ["a1", "a2"])
        reg.commit("p", ["a1", "a2"], True)
        assert reg.consume_collected("p", "a1")
        now[0] = COLLECTED_TTL_SECS + 1
        assert not reg.consume_collected("p", "a2")


class TestParentTeardownFencesHeldResults:
    @pytest.mark.asyncio
    async def test_a_held_member_whose_record_was_evicted_is_still_fenced(self) -> None:
        """Completed-run retention can evict a held member's record from
        ``_agents`` while its completion waits on the registry. The fence lives
        on the registry, keyed by parent, so the parent's teardown snapshot still
        drops it and its later release or expiry injects nothing."""
        from overload_fakes import mock_ctx, mock_sessions

        from kiro_crew.subagent import SubagentManager

        mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx())
        mgr._on_done = AsyncMock()
        reg = mgr.inline_collections
        reg.reserve(CRON_PARENT, "a1", 600)
        assert _hold(reg, CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        assert "a1" not in mgr._agents
        # Another parent's held member is left alone.
        reg.reserve(SLACK_PARENT, "b1", 600)
        _hold(reg, SLACK_PARENT, "b1", _info("b1", SLACK_PARENT))
        mgr.snapshot_teardown_children(CRON_PARENT)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        assert reg.commit(CRON_PARENT, ["a1"], True) == []
        await _settled(reg)
        mgr._on_done.assert_not_awaited()
        assert CRON_PARENT not in reg._records
        assert SLACK_PARENT in reg._records

    @pytest.mark.asyncio
    async def test_a_released_orphan_is_still_fenced_while_it_waits_to_be_delivered(
        self,
    ) -> None:
        """A cancelled call releases a held member whose run record was already
        evicted. Its delivery is waiting for the parent's turn to end when the
        parent is torn down: the fence is read at the point of delivery."""
        from overload_fakes import mock_ctx, mock_sessions

        from kiro_crew.subagent import SubagentManager

        sessions = mock_sessions()
        busy = [True]
        sessions.is_busy = MagicMock(side_effect=lambda _key: busy[0])
        mgr = SubagentManager(sessions=sessions, ctx_builder=mock_ctx())
        mgr._on_done = AsyncMock()
        reg = mgr.inline_collections
        reg.reserve(CRON_PARENT, "a1", 600)
        _hold(reg, CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        with patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0):
            reg.finish(CRON_PARENT, ["a1"], [])
            await asyncio.sleep(0)
            mgr.snapshot_teardown_children(CRON_PARENT)
            busy[0] = False
            await _settled(reg)
        mgr._on_done.assert_not_awaited()
        assert CRON_PARENT not in reg._records

    @pytest.mark.asyncio
    async def test_a_returned_result_is_settled_even_if_its_parent_is_retired_first(
        self,
    ) -> None:
        """The call's response carrying the held result was written, then the
        parent was torn down before the settle ran. The result reached the
        parent, so it is marked delivered; leaving it un-tombstoned would make
        restart recovery replay a result the parent already read."""
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        _hold(reg, CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        (settle,) = reg.commit(CRON_PARENT, ["a1"], True)
        reg.retire(CRON_PARENT)
        assert await settle == "delivered"
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [d.agent_id for d in batch] == ["a1"]
        mgr._on_done.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_late_completion_of_a_retired_member_is_owned_and_dropped(self) -> None:
        """Torn down while the member still ran: its completion is still owned
        (never injected into a replacement conversation under the same key), and
        an owed report is cleared."""
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.retire(CRON_PARENT)
        late = _info("a1", CRON_PARENT)
        late._report_owed = True
        assert _hold(reg, CRON_PARENT, "a1", late)
        mgr._admission.taskq_clear_owed_reports.assert_called_once_with(["a1"])
        assert CRON_PARENT not in reg._records


class TestReleasedOrphansAreTrackedUntilDelivered:
    @pytest.mark.asyncio
    async def test_the_registry_forgets_the_record_when_the_delivery_ends(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream():
            await on_done(_info("a1", CRON_PARENT))
            await _mark(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            assert registry._records[CRON_PARENT]["a1"].state == "delivering"
            await _drain(orch)
        assert CRON_PARENT not in registry._records


class TestAClaimSettlesOnlyOnceTheResponseIsWritten:
    """A claim settles only after the dispatcher wrote the response."""

    @pytest.mark.asyncio
    async def test_a_dropped_response_releases_its_held_results_for_ordinary_delivery(
        self,
    ) -> None:
        """spawn.py:1720: cancelled while the close was in flight. The claim is
        released, so the result is delivered as an ordinary completion and its
        delivered mark is written only by the route that took it."""
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        held = _info("a1", CRON_PARENT)
        _hold(reg, CRON_PARENT, "a1", held)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        mgr.settle_queued_delivery.assert_not_awaited()
        assert reg.commit(CRON_PARENT, ["a1"], False) == []
        await _settled(reg)
        (ticket,) = [c.args[0] for c in mgr._on_done.await_args_list]
        assert ticket.id == "a1" and ticket is not held
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [d.agent_id for d in batch] == ["a1"]

    @pytest.mark.asyncio
    async def test_a_dropped_response_whose_completion_is_late_injects_it_normally(self) -> None:
        reg = InlineCollections()
        _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        reg.commit(CRON_PARENT, ["a1"], False)
        assert CRON_PARENT not in reg._records
        late = _info("a1", CRON_PARENT)
        assert not _hold(reg, CRON_PARENT, "a1", late)
        assert not reg.consume_collected(CRON_PARENT, "a1")

    @pytest.mark.asyncio
    async def test_a_completion_landing_between_claim_and_commit_is_held_then_settled(
        self,
    ) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        assert reg.has_collected(CRON_PARENT)
        assert _hold(reg, CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        (settle,) = reg.commit(CRON_PARENT, ["a1"], True)
        assert await settle == "delivered"
        mgr._on_done.assert_not_awaited()
        assert CRON_PARENT not in reg._records

    @pytest.mark.asyncio
    async def test_a_claim_nobody_confirms_expires_into_ordinary_delivery(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        _hold(reg, CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        now[0] = CLAIM_TTL_SECS - 1
        reg._expire(CRON_PARENT)
        assert reg._records[CRON_PARENT]["a1"].state == "claimed"
        now[0] = CLAIM_TTL_SECS + 1
        reg._expire(CRON_PARENT)
        await _settled(reg)
        mgr._on_done.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_parent_retired_before_the_commit_settles_nothing(self) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        _hold(reg, CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        reg.retire(CRON_PARENT)
        assert reg.commit(CRON_PARENT, ["a1"], True) == []
        await _settled(reg)
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._on_done.assert_not_awaited()

    def test_a_late_completion_of_a_claim_retired_before_its_commit_is_fenced(self) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        reg.retire(CRON_PARENT)
        assert reg.commit(CRON_PARENT, ["a1"], True) == []
        # Not consumed as a returned result (no live turn read the response),
        # and not let go either: the registry still owns the late completion
        # and its fence drops it.
        assert not reg.consume_collected(CRON_PARENT, "a1")
        assert _hold(reg, CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        assert CRON_PARENT not in reg._records
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._on_done.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_handler_claims_then_commits(self) -> None:
        """Through the real handler: the claim alone settles nothing; the commit does."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        settle = mgr.settle_queued_delivery
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            body = {"ids": ["a1"], "released": ["a1"], "parent_session": CRON_PARENT}
            assert (await _post(orch, mgr, {**body, "phase": "claim"}))["status"] == "ok"
            settle.assert_not_awaited()
            commit = {"ids": ["a1"], "parent_session": CRON_PARENT, "phase": "commit"}
            await _post(orch, mgr, commit)
            await _drain(orch)
        settle.assert_awaited_once()
        _assert_parent_untouched(orch, stream)

    @pytest.mark.asyncio
    async def test_the_handler_refuses_an_unknown_phase_and_a_non_object_body(self) -> None:
        registry = InlineCollections()
        orch, _on_done, mgr = _gateway(registry)
        bad = {"ids": ["a1"], "parent_session": CRON_PARENT, "phase": "settle"}
        assert (await _post(orch, mgr, bad))["code"] == "invalid_phase"
        state = orch.dashboard_state
        resp = await handlers.api_spawn_mark_collected(_Req(state, ["a1"]))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_a_body_with_no_phase_is_refused_and_marks_nothing(self) -> None:
        """No ``phase`` -> 400 invalid_phase; the member stays owned by its own
        collection and, once that ends, is delivered by the base path."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream():
            await on_done(_info("a1", CRON_PARENT))
            body = {"ids": ["a1"], "released": ["a1"], "parent_session": CRON_PARENT}
            assert (await _post(orch, mgr, body))["code"] == "invalid_phase"
            rec = registry._records[CRON_PARENT]["a1"]
            # Not marked: the refused body claimed and settled nothing.
            assert rec.state == "held"
            assert not registry.consume_collected(CRON_PARENT, "a1")
            mgr.settle_queued_delivery.assert_not_awaited()
            # The call's own phased claim ends the collection: ordinary delivery.
            claim = {**body, "ids": [], "phase": "claim"}
            assert (await _post(orch, mgr, claim))["status"] == "ok"
            assert rec.state == "delivering"
            await _drain(orch)
        assert CRON_PARENT not in registry._records


class TestRetainedRecordsHoldTheirCapacity:
    """Every retained record holds its place at admission."""

    def test_retained_returned_records_count_at_admission(self) -> None:
        reg = InlineCollections()
        ids = [f"r{i}" for i in range(MAX_IDS_PER_PARENT)]
        assert all(reg.reserve(CRON_PARENT, aid, 60) for aid in ids)
        reg.finish(CRON_PARENT, ids, ids)
        reg.commit(CRON_PARENT, ids, True)
        assert not reg.reserve(CRON_PARENT, "late", 60)

    def test_an_admitted_member_keeps_its_place_when_its_reservation_is_claimed(self) -> None:
        reg = InlineCollections()
        ids = [f"a{i}" for i in range(MAX_IDS_PER_PARENT)]
        assert all(reg.reserve(CRON_PARENT, aid, 60) for aid in ids)
        reg.finish(CRON_PARENT, ids, ids)
        reg.commit(CRON_PARENT, ids, True)
        assert all(reg.consume_collected(CRON_PARENT, aid) for aid in ids)


#: A cron parent key exactly at the bound, and one character past it.
AT_BOUND_PARENT = "cron:" + "j" * (MAX_PARENT_KEY_CHARS - len("cron:"))
OVER_BOUND_PARENT = AT_BOUND_PARENT + "j"
AT_BOUND_ID = "a" * MAX_AGENT_ID_CHARS
OVER_BOUND_ID = AT_BOUND_ID + "a"
AT_BOUND_CALL = "c" * MAX_CALL_ID_CHARS
OVER_BOUND_CALL = AT_BOUND_CALL + "c"


@pytest.fixture
def refusals(caplog: pytest.LogCaptureFixture) -> Any:
    """The identity refusals the registry logged so far, one WARNING each."""
    caplog.set_level("WARNING", logger="kiro_crew.subagent_inline_collection")

    def _read() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "kiro_crew.subagent_inline_collection"
            and r.levelname == "WARNING"
            and "inline collection refused a" in r.getMessage()
        ]

    return _read


class TestEveryRetainedIdentityIsBoundedByLength:
    """The record cap bounds how many records a parent keeps; these bound each one.

    A record keeps three caller-supplied strings, its parent key, its agent id
    and its call id. Each is refused whole past its bound, before anything is
    retained, and the refusal is logged once as a WARNING. Nothing else a
    record keeps comes from the caller.
    """

    def test_the_parent_bound_admits_the_longest_real_session_keys(self) -> None:
        from kiro_crew.dashboard.chat_threads import thread_session_key
        from kiro_crew.dashboard.state import _normalize_slot_key

        slot = _normalize_slot_key("n" * 4000)  # folded to the longest slot key
        mid = "m-" + "0" * 16
        assert len(thread_session_key(slot, mid)) <= MAX_PARENT_KEY_CHARS
        assert len(f"dashboard:{slot}") <= MAX_PARENT_KEY_CHARS

    @pytest.mark.parametrize("field", ["parent_session", "agent_id", "call_id"])
    def test_reserve_refuses_an_oversized_identity_and_logs_it(
        self, field: str, refusals: Any
    ) -> None:
        reg = InlineCollections()
        parent, aid, call = {
            "parent_session": (OVER_BOUND_PARENT, "a1", ""),
            "agent_id": (CRON_PARENT, OVER_BOUND_ID, ""),
            "call_id": (CRON_PARENT, "a1", OVER_BOUND_CALL),
        }[field]
        assert not reg.reserve(parent, aid, 60, call=call)
        assert reg._records == {}
        assert len(refusals()) == 1

    def test_reserve_admits_identities_exactly_at_their_bounds(self, refusals: Any) -> None:
        reg = InlineCollections()
        assert reg.reserve(AT_BOUND_PARENT, AT_BOUND_ID, 60, call=AT_BOUND_CALL)
        assert reg._records[AT_BOUND_PARENT][AT_BOUND_ID].call == AT_BOUND_CALL
        assert refusals() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["claim", "commit", "drop"])
    @pytest.mark.parametrize(
        ("field", "parent", "ids", "released"),
        [
            ("parent_session", OVER_BOUND_PARENT, ["a1"], ["a1"]),
            ("agent_id", CRON_PARENT, [OVER_BOUND_ID], []),
            ("agent_id", CRON_PARENT, ["a1"], [OVER_BOUND_ID]),
            ("call_id", CRON_PARENT, ["a1"], ["a1"]),
        ],
        ids=["parent", "id-in-ids", "id-in-released", "call"],
    )
    async def test_mark_collected_answers_400_and_retains_nothing(
        self,
        phase: str,
        field: str,
        parent: str,
        ids: list[str],
        released: list[str],
        refusals: Any,
    ) -> None:
        registry = InlineCollections()
        orch, _on_done, mgr = _gateway(registry)
        body: dict[str, Any] = {"ids": ids, "released": released, "parent_session": parent}
        if field == "call_id":
            body["call"] = OVER_BOUND_CALL
        body["phase"] = phase
        state = orch.dashboard_state
        state.subagents = mgr
        resp = await handlers.api_spawn_mark_collected(_Req(state, body))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == f"{field}_too_long"
        assert registry._records == {}
        assert len(refusals()) == 1

    @pytest.mark.asyncio
    async def test_mark_collected_records_identities_exactly_at_their_bounds(
        self, refusals: Any
    ) -> None:
        registry = InlineCollections()
        orch, _on_done, mgr = _gateway(registry)
        assert registry.reserve(AT_BOUND_PARENT, AT_BOUND_ID, 60, call=AT_BOUND_CALL)
        body = {
            "ids": [AT_BOUND_ID],
            "parent_session": AT_BOUND_PARENT,
            "call": AT_BOUND_CALL,
            "phase": "claim",
        }
        assert (await _post(orch, mgr, body))["status"] == "ok"
        assert registry._records[AT_BOUND_PARENT][AT_BOUND_ID].state == "claimed"
        assert refusals() == []

    def _spawn(self, registry: InlineCollections, parent: str, call: Any = "") -> tuple[Any, Any]:
        mgr = _mgr()
        mgr.inline_collections = registry
        mgr.reserve_inline_member = lambda p, wait, call="": (
            "a1" if registry.reserve(p, "a1", wait, call=call) else ""
        )
        mgr.spawn.side_effect = lambda task, **kw: _run_info(id=kw["_preassigned_id"])
        body = {
            "task": "x",
            "parent_session": parent,
            "inline_collect": True,
            "inline_call": call,
            "max_wait": 600,
        }
        with patch.object(
            handlers, "_spawn_request_memory_mode", AsyncMock(return_value="persistent")
        ):
            resp = asyncio.run(handlers.api_spawn(_CoverageReq(_state(subagents=mgr), body)))
        return resp, mgr

    def test_api_spawn_refuses_an_oversized_parent_before_reserving(self, refusals: Any) -> None:
        registry = InlineCollections()
        resp, mgr = self._spawn(registry, OVER_BOUND_PARENT)
        assert resp.status == 400
        assert _payload(resp)["code"] == "parent_session_too_long"
        mgr.spawn.assert_not_called()
        assert registry._records == {}
        assert len(refusals()) == 1

    def test_api_spawn_reserves_a_parent_exactly_at_the_bound(self, refusals: Any) -> None:
        registry = InlineCollections()
        resp, mgr = self._spawn(registry, AT_BOUND_PARENT)
        assert _payload(resp)["id"] == "a1"
        assert "a1" in registry._records[AT_BOUND_PARENT]
        assert refusals() == []

    def test_api_spawn_refuses_an_oversized_call_id_before_reserving(self, refusals: Any) -> None:
        registry = InlineCollections()
        resp, mgr = self._spawn(registry, CRON_PARENT, OVER_BOUND_CALL)
        assert resp.status == 400
        assert _payload(resp)["code"] == "call_id_too_long"
        mgr.spawn.assert_not_called()
        assert registry._records == {}
        assert len(refusals()) == 1

    def test_api_spawn_reserves_a_call_id_exactly_at_the_bound(self, refusals: Any) -> None:
        registry = InlineCollections()
        resp, _mgr = self._spawn(registry, CRON_PARENT, AT_BOUND_CALL)
        assert _payload(resp)["id"] == "a1"
        assert registry._records[CRON_PARENT]["a1"].call == AT_BOUND_CALL
        assert refusals() == []

    def test_api_spawn_refuses_a_call_id_that_is_not_a_string(self) -> None:
        registry = InlineCollections()
        resp, mgr = self._spawn(registry, CRON_PARENT, ["not", "a", "string"])
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_inline_call"
        mgr.spawn.assert_not_called()
        assert registry._records == {}

    @pytest.mark.asyncio
    async def test_mark_collected_refuses_a_call_that_is_not_a_string(self) -> None:
        registry = InlineCollections()
        orch, _on_done, mgr = _gateway(registry)
        state = orch.dashboard_state
        state.subagents = mgr
        body = {"ids": ["a1"], "parent_session": CRON_PARENT, "phase": "claim", "call": 7}
        resp = await handlers.api_spawn_mark_collected(_Req(state, body))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_call"
        assert registry._records == {}


def _done_run(aid: str) -> Any:
    from kiro_crew.subagent import SubagentInfo

    info = SubagentInfo(id=aid, task="t", parent_session_key=CRON_PARENT)
    info.done = True
    info._delivery_queued = True
    return info


class _TimerLoop:
    """Records ``call_later`` so each registry timer fires at its own due time."""

    def __init__(self, now: list[float]) -> None:
        self.now = now
        self.timers: list[tuple[float, int, Any, tuple[Any, ...]]] = []

    def call_later(self, delay: float, fn: Any, *args: Any) -> None:
        self.timers.append((self.now[0] + delay, len(self.timers), fn, args))

    def is_closed(self) -> bool:
        return False

    def run_next(self) -> bool:
        if not self.timers:
            return False
        self.timers.sort()
        due, _n, fn, args = self.timers.pop(0)
        self.now[0] = max(self.now[0], due)
        fn(*args)
        return True

    def run_all(self) -> None:
        while self.run_next():
            pass


class TestEveryRecordHasItsOwnExpiry:
    """A ``returned`` marker is expired by its own timer, like every other phase."""

    def test_a_returned_marker_on_a_key_nothing_touches_again_expires(self) -> None:
        now = [1000.0]
        reg = InlineCollections(clock=lambda: now[0])
        loop = _TimerLoop(now)
        with patch.object(sic.asyncio, "get_running_loop", return_value=loop):
            assert reg.reserve(CRON_PARENT, "a1", 60, call="c1")
            reg.finish(CRON_PARENT, ["a1"], ["a1"], call="c1")
            reg.commit(CRON_PARENT, ["a1"], True, released=["a1"], call="c1")
            assert reg._records[CRON_PARENT]["a1"].state == "returned"
            # The run's completion is fenced and never reaches consume_collected.
            reg.retire(CRON_PARENT)
            loop.run_all()
        assert CRON_PARENT not in reg._records

    def test_the_marker_lives_the_returned_ttl(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        loop = _TimerLoop(now)
        with patch.object(sic.asyncio, "get_running_loop", return_value=loop):
            assert reg.reserve(CRON_PARENT, "a1", 60)
            reg.finish(CRON_PARENT, ["a1"], ["a1"])
            reg.commit(CRON_PARENT, ["a1"], True)
            # The parent's one timer fires and re-arms until the marker goes.
            while CRON_PARENT in reg._records:
                assert loop.run_next(), "no timer left while the marker is pending"
        assert now[0] == COLLECTED_TTL_SECS + 1.0


class TestOneExpiryTimerPerParent:
    """The registry keeps one expiry timer per parent, never one per reserve."""

    @staticmethod
    def _live_timers(loop: asyncio.AbstractEventLoop) -> int:
        return sum(1 for h in loop._scheduled if not h.cancelled())  # type: ignore[attr-defined]

    def test_reserve_and_discard_cycles_reuse_one_timer(self) -> None:
        async def run() -> tuple[int, int]:
            loop = asyncio.get_running_loop()
            reg = InlineCollections()
            before_live = self._live_timers(loop)
            before_all = len(loop._scheduled)  # type: ignore[attr-defined]
            for i in range(3 * MAX_IDS_PER_PARENT):
                aid = f"{i:016x}"
                assert reg.reserve(CRON_PARENT, aid, 7200.0, call="c" * 32)
                reg.discard(CRON_PARENT, aid)  # the refused-spawn path
            assert reg._records == {}
            return (
                self._live_timers(loop) - before_live,
                len(loop._scheduled) - before_all,  # type: ignore[attr-defined]
            )

        live, scheduled = asyncio.run(run())
        # One timer, and no pile of cancelled ones either.
        assert (live, scheduled) == (1, 1)

    def test_a_parent_with_a_timer_pending_schedules_no_second_one(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        loop = _TimerLoop(now)
        with patch.object(sic.asyncio, "get_running_loop", return_value=loop):
            assert reg.reserve(CRON_PARENT, "a1", 7200, call="c1")
            assert reg.reserve(CRON_PARENT, "a2", 60, call="c2")
            reg.finish(CRON_PARENT, ["a2"], ["a2"], call="c2")  # a claim's deadline
        # One timer, never further away than the shortest deadline given.
        assert [due for due, *_ in loop.timers] == [sic._EXPIRY_SWEEP_SECS + 1.0]

    def test_every_deadline_is_at_least_one_sweep_away(self) -> None:
        """The reason a pending timer never needs moving earlier."""
        for ttl in (CLAIM_TTL_SECS, COLLECTED_TTL_SECS, COLLECTION_GRACE_SECS):
            assert ttl >= sic._EXPIRY_SWEEP_SECS

    def test_a_long_collection_is_expired_on_time_through_re_arms(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        loop = _TimerLoop(now)
        with patch.object(sic.asyncio, "get_running_loop", return_value=loop):
            assert reg.reserve(CRON_PARENT, "a1", 7200, call="c1")
            while CRON_PARENT in reg._records:
                assert loop.run_next(), "no timer left while the reservation is pending"
        assert now[0] == 7200 + COLLECTION_GRACE_SECS + 1.0


class TestACollectionIsDueFromItsCallsLatestReserve:
    """A member reserved early stays held while its call is still spawning."""

    def test_first_member_stays_held_while_its_call_still_collects(self) -> None:
        now = [0.0]
        parent, call = CRON_PARENT, "c" * 32

        async def run() -> str:
            # On a running loop, as in the gateway, so an expiry really releases.
            reg = InlineCollections(clock=lambda: now[0])
            assert reg.reserve(parent, "a" * 16, 60.0, call=call)
            assert reg.hold(parent, "a" * 16)
            # Spawning the rest of the batch took longer than the grace.
            now[0] = COLLECTION_GRACE_SECS + 100.0
            assert reg.reserve(parent, "b" * 16, 60.0, call=call)
            now[0] += 60.0 - 1.0  # still inside the call's poll
            reg._expire(parent)
            await asyncio.sleep(0)
            rec = reg._records.get(parent, {}).get("a" * 16)
            return rec.state if rec else "gone"

        assert asyncio.run(run()) == sic.HELD

    def test_another_calls_reserve_extends_nothing(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        assert reg.reserve(CRON_PARENT, "a1", 60.0, call="c1")
        now[0] = 60 + COLLECTION_GRACE_SECS - 1
        assert reg.reserve(CRON_PARENT, "b1", 60.0, call="c2")
        now[0] += 2  # past c1's own deadline
        reg._expire(CRON_PARENT)
        assert "a1" not in reg._records[CRON_PARENT]
        assert reg._records[CRON_PARENT]["b1"].state == sic.COLLECTING

    def test_reserve_and_discard_cycles_cannot_hold_a_member_past_its_absolute_cap(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        assert reg.reserve(CRON_PARENT, "a" * 16, 7200.0, call="c" * 32)
        assert reg.hold(CRON_PARENT, "a" * 16)
        for i in range(4):
            now[0] += 7000.0
            aid = f"{i:016x}"
            assert reg.reserve(CRON_PARENT, aid, 7200.0, call="c" * 32)
            reg.discard(CRON_PARENT, aid)  # the refused-spawn path
            rec = reg._records[CRON_PARENT].get("a" * 16)
            # No loop, so an expired held record is logged and kept: read its deadline.
            assert rec is not None and rec.deadline <= MAX_COLLECTION_TTL_SECS

    def test_a_repeated_reserve_of_one_member_never_moves_it_past_its_cap(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        for _ in range(3):
            assert reg.reserve(CRON_PARENT, "a" * 16, 7200.0, call="c" * 32)
            now[0] += 3000.0
        assert reg._records[CRON_PARENT]["a" * 16].deadline == MAX_COLLECTION_TTL_SECS

    def test_a_reserve_refused_at_the_cap_extends_nothing(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        for i in range(MAX_IDS_PER_PARENT):
            assert reg.reserve(CRON_PARENT, f"{i:016x}", 60.0, call="c" * 32)
        first = reg._records[CRON_PARENT][f"{0:016x}"].deadline
        now[0] += 300.0
        assert not reg.reserve(CRON_PARENT, "f" * 16, 60.0, call="c" * 32)  # 429
        assert reg._records[CRON_PARENT][f"{0:016x}"].deadline == first


class TestATimerOnAClosedLoopIsReplaced:
    """A parent's timer pending on a loop that has closed never fires, so it is replaced."""

    def test_a_second_loop_gets_its_own_timer_once_the_first_closed(self) -> None:
        reg = InlineCollections()

        async def reserve(aid: str) -> int:
            assert reg.reserve(CRON_PARENT, aid, 60.0, call="c" * 32)
            loop = asyncio.get_running_loop()
            return sum(1 for h in loop._scheduled if not h.cancelled())  # type: ignore[attr-defined]

        assert asyncio.run(reserve("a" * 16)) == 1
        # The first loop is closed now and its timer never fires, so the new
        # loop must get one of its own.
        assert asyncio.run(reserve("b" * 16)) == 1


class TestArmingDoesNotDependOnImportOrder:
    """A backend whose first frame is ``tools/call spawn_sub_agents`` arms the call."""

    def test_the_dispatcher_module_alone_arms_spawn_sub_agents(
        self, tmp_path: pathlib.Path
    ) -> None:
        import os
        import subprocess
        import sys

        # Only the dispatcher's own module is imported before the arm: neither
        # the server assembly (mcp_core) nor the spawn handler. Arming that
        # moved behind either import leaves this call unarmed.
        probe = (
            "import sys\n"
            "from kiro_crew import mcp_shared\n"
            "assert 'kiro_crew.mcp_core' not in sys.modules\n"
            "assert 'kiro_crew.mcp_tools.spawn' not in sys.modules\n"
            "mcp_shared._arm_for_dispatch('1', 'spawn_sub_agents')\n"
            "import kiro_crew.mcp_tools.spawn\n"
            "print(mcp_shared.on_response_outcome(lambda delivered: None))\n"
        )
        src = str(pathlib.Path(sic.__file__).resolve().parents[1])
        env = dict(
            os.environ, PYTHONPATH=src, HOME=str(tmp_path), KIROCREW_HOME=str(tmp_path / "kc")
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
            cwd=str(tmp_path),
        )
        assert out.returncode == 0, out.stderr[-2000:]
        # None: the call was armed, so the hook will be called.
        assert out.stdout.strip().splitlines()[-1] == "None", out.stdout


class TestEndingACallEndsEveryReservationItMade:
    """A reservation belongs to its call, so the call's end ends it, even when
    the ``/api/spawn`` reply that named its id never reached the call."""

    def _held(self, reg: InlineCollections, *pairs: tuple[str, str]) -> Any:
        mgr = _bound(reg)
        runs = {aid: _done_run(aid) for aid, _ in pairs}
        for info in runs.values():
            info._delivery_queued = True
        mgr.get = lambda aid: runs.get(aid)
        for aid, call in pairs:
            assert reg.reserve(CRON_PARENT, aid, 7200, call=call)
            assert reg.hold(CRON_PARENT, aid)
        return mgr

    @pytest.mark.asyncio
    async def test_the_claim_releases_a_member_whose_id_the_call_never_learned(self) -> None:
        reg = InlineCollections()
        mgr = self._held(reg, ("a1", "c1"), ("a2", "c1"))
        # The call only learned a1: a2's /api/spawn reply was lost.
        reg.finish(CRON_PARENT, ["a1"], ["a1"], call="c1")
        await _settled(reg)
        assert "a2" not in reg._records.get(CRON_PARENT, {})
        assert [c.args[0].id for c in mgr._on_done.await_args_list] == ["a2"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delivered", [True, False])
    async def test_a_commit_or_drop_without_the_claim_ends_them_too(self, delivered: bool) -> None:
        reg = InlineCollections()
        mgr = self._held(reg, ("a1", "c1"), ("a2", "c1"))
        await asyncio.gather(
            *reg.commit(CRON_PARENT, ["a1"], delivered, released=["a1"], call="c1")
        )
        await _settled(reg)
        assert CRON_PARENT not in reg._records
        assert "a2" in [c.args[0].id for c in mgr._on_done.await_args_list]

    @pytest.mark.asyncio
    async def test_a_claim_that_expires_ends_its_calls_other_reservations(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        mgr = self._held(reg, ("a1", "c1"), ("a2", "c1"))
        reg.finish(CRON_PARENT, ["a1"], ["a1"], call=None)  # names no call: a2 stays
        assert reg._records[CRON_PARENT]["a2"].state == "held"
        now[0] += CLAIM_TTL_SECS + 1  # the commit never came
        reg._expire(CRON_PARENT)
        await _settled(reg)
        assert CRON_PARENT not in reg._records
        assert sorted(c.args[0].id for c in mgr._on_done.await_args_list) == ["a1", "a2"]

    @pytest.mark.asyncio
    async def test_another_calls_reservations_are_untouched(self) -> None:
        reg = InlineCollections()
        mgr = self._held(reg, ("a1", "c1"), ("b1", "c2"))
        reg.finish(CRON_PARENT, ["a1"], ["a1"], call="c1")
        await _settled(reg)
        assert reg._records[CRON_PARENT]["b1"].state == "held"
        mgr._on_done.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_endpoint_carries_the_call_into_the_registry(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        runs = {aid: _done_run(aid) for aid in ("a1", "a2")}
        mgr.get = lambda aid: runs.get(aid)
        for aid in ("a1", "a2"):
            assert registry.reserve(CRON_PARENT, aid, 7200, call="c1")
        body = {
            "ids": [],
            "parent_session": CRON_PARENT,
            "phase": "claim",
            "call": "c1",
        }
        assert (await _post(orch, mgr, body))["status"] == "ok"
        assert CRON_PARENT not in registry._records

    def test_a_tool_whose_every_spawn_failed_still_ends_its_call(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core.sel"),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
        ):
            # The gateway reserved and started the member; its reply was lost.
            mock_post.side_effect = [{"error": "timed out", "transport_error": True}, {}]
            out = spawn_mod.spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
        assert out.startswith("Error spawning sub-agents")
        spawn_body = mock_post.call_args_list[0].args[1]
        close = mock_post.call_args_list[-1].args
        assert close == (
            "/api/spawn/mark-collected",
            {
                "ids": [],
                "parent_session": CRON_PARENT,
                "phase": "claim",
                "call": spawn_body["inline_call"],
            },
        )


class TestTheTemporaryBusyParentWaitStaysPinned:
    """``_await_parent_idle`` is the registry's one busy-parent wait, owned by
    the completion route's general busy-parent fence once that lands. A second
    caller would make it a second fence."""

    def test_it_has_exactly_one_call_site(self) -> None:
        import ast
        import pathlib

        src = pathlib.Path(sic.__file__).resolve().parents[1]
        sites: list[str] = []
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "_await_parent_idle" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_await_parent_idle"
                ):
                    sites.append(path.relative_to(src).as_posix())
        assert sites == ["kiro_crew/subagent_inline_collection.py"]

    def test_it_is_marked_temporary_and_the_spec_names_its_owner(self) -> None:
        import pathlib

        doc = InlineCollections._await_parent_idle.__doc__ or ""
        assert "TEMPORARY" in doc and "docs/system-specs/modules/subagent.md" in doc
        spec = (
            pathlib.Path(sic.__file__)
            .resolve()
            .parents[2]
            .joinpath("docs", "system-specs", "modules", "subagent.md")
        )
        owner = "busy-parent fence on the completion route, " + "#" + "17898"
        assert owner in spec.read_text(encoding="utf-8")
