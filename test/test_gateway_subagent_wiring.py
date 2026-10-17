"""The sub-agent completion consumer, pinned through ``on_done`` as it is wired.

``_init_subagents`` builds the ``SubagentManager`` and hands it ``_subagent_done``
as ``on_done``: the one consumer every terminal path of a sub-agent reports
through. It resolves the parent's tab (loading a restored tab's window first),
broadcasts the member's "done" frame on that tab, composes the completion, folds
a wave member into its digest, and routes the result to the tab, the parent's
channel session, its cron session, or the notification bell.

These tests drive that consumer through the real ``on_done`` with every
collaborator replaced, and pin what decides the route before any of it runs:
which slot the completion lands in, which slot name its frames carry, and the
records that must route nothing. :class:`_Done` is the harness the sibling
``test_gateway_subagent_*.py`` and ``test_gateway_runtime_charge.py`` files
share.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import runtime_death
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.handlers import messaging as messaging_handlers
from kiro_crew.slack import gateway as gw_mod
from kiro_crew.subagent import SubagentInfo

#: Every wait in these tests is on a condition; this is only the lost-run guard.
_LOST_RUN_SECS = 30.0


class _Slot:
    """A dashboard tab with exactly the state the completion consumer reads."""

    def __init__(self, key: str = "main") -> None:
        self.key = key
        self.window_pending = False
        self.task: Any = None
        self.running = False
        self._pending_synthesis = False
        self._subagents_inline_collected: set[str] = set()
        self._subagent_deliveries_inflight = 0
        self._synthesis_completion_turns = 0
        self.queued: list[tuple[str, str, Any]] = []
        self.owed: list[tuple[str, list[Any]]] = []

    def queue_append(self, text: str, *, kind: str, meta: Any) -> None:
        self.queued.append((text, kind, meta))

    def note_pending_subagent_delivery(self, content: str, deliveries: list[Any]) -> None:
        self.owed.append((content, list(deliveries)))


class _Tasks(set):  # type: ignore[type-arg]
    """``dashboard_state._background_tasks``, remembering every task added to it."""

    def __init__(self) -> None:
        super().__init__()
        self.added: list[Any] = []

    def add(self, task: Any) -> None:
        self.added.append(task)
        super().add(task)


class _Done:
    """One gateway whose ``on_done`` is captured, with its effects recorded.

    The parent ``dashboard:main`` has the tab ``main``; every other key has none
    unless a test says so. ``subagent_event_slot`` is replaced by ``evt:<key>`` so
    a frame's slot shows which of the two names it was given.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[Any] = []
        self.chats: list[tuple[Any, str, dict[str, Any]]] = []
        #: For each turn, whether the tab recorded that very turn as its task.
        self.turn_is_slot_task: list[bool] = []
        self.chat_error: BaseException | None = None
        self.stream_results: list[Any] = ["parent reply"]
        self.streamed_clients: list[Any] = []
        self.bound_slot: Any = None
        #: Answers for the parent's bound tab, used up first, one per lookup.
        self.bound_slots: list[Any] = []
        self.tabs: dict[str, str] = {"dashboard:main": "main"}

        self.client = MagicMock(name="client")
        self.clients: list[Any] = [self.client]
        sessions = MagicMock(name="sessions")
        sessions.get_or_create = AsyncMock(side_effect=self._get_or_create)
        sessions.reset = AsyncMock(side_effect=lambda key: self.calls.append(("reset", key)))
        sessions.cancel_current = AsyncMock(
            side_effect=lambda key, **_kw: self.calls.append(("cancel", key))
        )
        sessions.release = MagicMock(side_effect=lambda key: self.calls.append(("release", key)))
        sessions.get_thread = MagicMock(return_value=None)
        sessions.get_channel = MagicMock(return_value=None)
        self.sessions = sessions

        state = MagicMock(name="dashboard_state")
        state._background_tasks = _Tasks()
        state.get_slot = MagicMock(return_value=None)
        self.state = state

        mgr = MagicMock(name="subagent_mgr")
        mgr.running = []
        mgr.running_agents_for = MagicMock(return_value=[])
        mgr.has_in_memory_pending_work_for = MagicMock(return_value=False)
        mgr.batch_reports_in_flight = MagicMock(return_value=False)
        mgr.wave_has_live_nested_spawns = MagicMock(return_value=False)
        mgr.published_queued_depths = MagicMock(return_value={})
        mgr.running_count = 0
        self.mgr = mgr
        self.manager_kwargs: dict[str, Any] = {}
        #: ``(kwargs, gate)`` for each ``_interactive_approval`` the wiring builds.
        self.approvals: list[tuple[dict[str, Any], AsyncMock]] = []

        def _approval(_orch: Any, source: str, **kwargs: Any) -> AsyncMock:
            gate = AsyncMock(name=f"{source} approval", return_value=True)
            self.approvals.append((kwargs, gate))
            return gate

        def _manager(**kwargs: Any) -> MagicMock:
            self.manager_kwargs = kwargs
            return mgr

        monkeypatch.setattr(gw_mod, "SubagentManager", _manager)
        monkeypatch.setattr(gw_mod.GatewayOrchestrator, "_interactive_approval", _approval)
        monkeypatch.setattr(gw_mod, "subagent_event_slot", lambda key: f"evt:{key}")
        monkeypatch.setattr(gw_mod, "dashboard_slot_key", lambda key: self.tabs.get(key, ""))
        monkeypatch.setattr(messaging_handlers, "_slot_for_parent", self._slot_for_parent)
        monkeypatch.setattr(gw_mod, "load_window_off_loop", AsyncMock(side_effect=self._load))
        monkeypatch.setattr(gw_mod, "_run_chat", self._run_chat)
        monkeypatch.setattr(gw_mod, "bounded_chat_turn", self._bounded)
        monkeypatch.setattr(gw_mod, "_arm_queued_delivery_settlement", MagicMock())
        monkeypatch.setattr(gw_mod, "_subagent_batch_pending", AsyncMock(return_value=True))
        monkeypatch.setattr(gw_mod, "_subagent_queued_count", AsyncMock(return_value=0))
        monkeypatch.setattr(gw_mod, "session_store_for_turn", AsyncMock(return_value="STORE"))
        monkeypatch.setattr(gw_mod, "run_in_embed_pool", AsyncMock(side_effect=self._build_message))
        monkeypatch.setattr(gw_mod, "stream_and_collect", self._stream)
        monkeypatch.setattr(gw_mod, "redact_credentials", self._redact_credentials)
        monkeypatch.setattr(gw_mod, "redact_exfiltration_urls", self._redact_urls)
        monkeypatch.setattr(runtime_death, "caused_by_this_session", lambda _client: True)

        orch = gw_mod.GatewayOrchestrator(
            KiroCrewConfig(),
            no_crons=True,
            no_open=True,
            credentials={gw_mod.CRED_OWNER_ID: "U_OWNER"},
        )
        orch.sessions = sessions
        orch.ctx_builder = MagicMock(name="ctx_builder")
        orch.dashboard_state = state
        orch.conv_log = None
        orch.slack = None
        orch.cron_svc = MagicMock(name="cron_svc")
        orch._deliver_cron_response = AsyncMock()
        orch._cron_job_is_silent = MagicMock(return_value=False)
        self.orch = orch
        orch._init_subagents()
        self.on_done = self.manager_kwargs["on_done"]

    # ── collaborators ────────────────────────────────────────────────────────

    async def _get_or_create(self, key: str, **_kw: Any) -> tuple[Any, bool, bool]:
        self.calls.append(("acquire", key))
        client = self.clients.pop(0) if len(self.clients) > 1 else self.clients[0]
        return client, True, False

    def _slot_for_parent(self, _state: Any, _key: str) -> Any:
        return self.bound_slots.pop(0) if self.bound_slots else self.bound_slot

    async def _load(self, slot: Any) -> None:
        self.calls.append(("load", slot.key))

    async def _run_chat(self, _state: Any, slot: Any, text: str, **kwargs: Any) -> None:
        self.chats.append((slot, text, kwargs))
        self.turn_is_slot_task.append(slot.task is asyncio.current_task())
        if self.chat_error is not None:
            raise self.chat_error

    async def _bounded(self, coro: Any, **_kw: Any) -> Any:
        return await coro

    async def _build_message(self, fn: Any, *args: Any, **kwargs: Any) -> tuple[str, None]:
        self.calls.append(("build", args, kwargs))
        return f"built:{args[0]}", None

    async def _stream(self, client: Any, msg: str, **_kw: Any) -> Any:
        self.streamed_clients.append(client)
        self.calls.append(("stream", msg))
        result = (
            self.stream_results.pop(0) if len(self.stream_results) > 1 else (self.stream_results[0])
        )
        if isinstance(result, BaseException):
            raise result
        return result

    @staticmethod
    def _redact_credentials(text: str) -> tuple[str, int]:
        return text.replace("SECRET", "[R]"), 0

    @staticmethod
    def _redact_urls(text: str) -> tuple[str, int]:
        return text.replace("EXFIL", "[U]"), 0

    # ── driving ──────────────────────────────────────────────────────────────

    def tab(self, key: str = "main") -> _Slot:
        """A tab the folded slot name resolves to."""
        slot = _Slot(key)
        self.state.get_slot = MagicMock(side_effect=lambda name: slot if name == slot.key else None)
        return slot

    async def done(self, info: SubagentInfo) -> None:
        await asyncio.wait_for(self.on_done(info), _LOST_RUN_SECS)

    async def drain(self) -> None:
        """Run every turn the consumer dispatched to completion."""
        pending = [t for t in self.state._background_tasks if not t.done()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), _LOST_RUN_SECS)

    def frames(self, kind: str) -> list[dict[str, Any]]:
        return [c.args[1] for c in self.state.broadcast_ws.call_args_list if c.args[0] == kind]

    def notices(self) -> list[tuple[Any, ...]]:
        return [(c.args, c.kwargs) for c in self.state.notify.call_args_list]


def make_info(
    agent_id: str = "a1",
    *,
    parent: str = "dashboard:main",
    task: str = "do the thing",
    result: str = "the result",
    error: str = "",
    batch_id: str = "",
    batch_total: int = 0,
) -> SubagentInfo:
    info = SubagentInfo(
        id=agent_id,
        task=task,
        parent_session_key=parent,
        batch_id=batch_id,
        batch_total=batch_total,
    )
    info.done = True
    info.result = result
    info.error = error
    info.elapsed = 12.0
    info.credits = 0.25
    info.tool_count = 1
    return info


def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    """The ``done`` fixture's body, for the sibling files' own ``done`` fixtures."""
    runtime_death._reset_for_tests()
    yield _Done(monkeypatch)
    runtime_death._reset_for_tests()


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


async def _until(predicate: Callable[[], bool], what: str) -> None:
    """Yield to the loop until *predicate* holds; fail naming *what* if it never does."""
    for _ in range(10_000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"never reached: {what}")


class _RecordingAsyncio:
    """The facade's ``asyncio`` with ``sleep`` and ``wait_for`` recorded.

    ``timeouts`` is how many ``wait_for`` calls end in ``TimeoutError`` before
    one runs its awaitable.
    """

    def __init__(self, calls: list[Any]) -> None:
        self._calls = calls
        self.timeouts = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, *_args: Any) -> None:
        self._calls.append(("sleep", delay))

    async def wait_for(self, awaitable: Any, timeout: float | None) -> Any:
        self._calls.append(("wait_for", timeout))
        if self.timeouts:
            self.timeouts -= 1
            awaitable.close()
            raise asyncio.TimeoutError
        return await awaitable


def recording_clock(done: _Done, monkeypatch: pytest.MonkeyPatch) -> _RecordingAsyncio:
    """Install a :class:`_RecordingAsyncio` as the facade's ``asyncio`` for *done*."""
    recording = _RecordingAsyncio(done.calls)
    monkeypatch.setattr(gw_mod, "asyncio", recording)
    return recording


# ── the parent's tab ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_done_frame_names_the_tab_the_completion_resolved(done: _Done) -> None:
    done.tab("main")

    await done.done(make_info())
    await done.drain()

    statuses = done.frames("subagent_status")
    assert [(f["id"], f["event"], f["slot"]) for f in statuses] == [("a1", "done", "main")]


@pytest.mark.asyncio
async def test_a_parent_with_no_tab_names_its_event_slot(done: _Done) -> None:
    await done.done(make_info(parent="subagent:p1"))

    statuses = done.frames("subagent_status")
    assert [(f["event"], f["slot"]) for f in statuses] == [("done", "evt:subagent:p1")]


@pytest.mark.asyncio
async def test_the_tab_bound_to_the_parent_wins_over_the_folded_name(done: _Done) -> None:
    folded = done.tab("main")
    bound = _Slot("channel-born")
    done.bound_slot = bound

    await done.done(make_info())
    await done.drain()

    assert [slot for slot, _text, _kw in done.chats] == [bound]
    assert folded.task is None
    assert done.frames("subagent_status")[0]["slot"] == "channel-born"


@pytest.mark.asyncio
async def test_a_restored_tab_loads_its_window_before_the_completion_lands(
    done: _Done,
) -> None:
    restored = _Slot("main")
    restored.window_pending = True
    loaded = _Slot("main")
    done.bound_slots = [restored, loaded]

    await done.done(make_info())
    await done.drain()

    assert ("load", "main") in done.calls
    assert [slot for slot, _text, _kw in done.chats] == [loaded]
    assert restored.task is None


# ── records that route nothing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_flush_only_record_with_no_wave_routes_nothing(done: _Done) -> None:
    slot = done.tab("main")
    info = make_info(result="")
    info._digest_flush_only = True

    await done.done(info)
    await done.drain()

    assert done.frames("subagent_status") == []
    assert done.chats == [] and slot.queued == [] and slot.task is None
    assert done.notices() == []
    assert not any(c[0] == "acquire" for c in done.calls)


@pytest.mark.asyncio
async def test_the_manager_gets_the_consumer_and_holds_queue_dispatch(done: _Done) -> None:
    assert asyncio.iscoroutinefunction(done.on_done)
    assert done.manager_kwargs["defer_queue_dispatch"] is True
    done.sessions.set_child_teardown_handler.assert_called_once_with(done.mgr)
    done.mgr.start_reaper.assert_called_once_with()


# ── the manager's other callbacks ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_yolo_is_read_from_the_safety_override_at_call_time(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = MagicMock(is_active=MagicMock(return_value=True))
    monkeypatch.setattr(gw_mod, "safety_override", lambda: active)

    assert done.manager_kwargs["is_yolo"]() is True
    active.is_active.assert_called_once_with()


@pytest.mark.asyncio
async def test_a_spawn_request_resolves_the_slot_its_parent_answers_to(done: _Done) -> None:
    (resolver_kwargs, _tool_gate), (spawn_kwargs, _spawn_gate) = done.approvals
    resolve = resolver_kwargs["slot_resolver"]
    assert spawn_kwargs == {"slot_resolver": resolve, "raise_when_unreachable": True}
    done.mgr.get = MagicMock(
        side_effect=lambda agent_id: make_info(agent_id) if agent_id == "a9" else None
    )

    assert resolve("spawn:a9") == "evt:dashboard:main"
    assert resolve("spawn:gone") == ""
    assert [c.args for c in done.mgr.get.call_args_list] == [("a9",), ("gone",)]
    assert done.manager_kwargs["on_tool_approval"] is _tool_gate


@pytest.mark.asyncio
async def test_a_spawn_approval_asks_the_channel_seam_before_the_gate(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers: list[Any] = [False, None]
    asked: list[tuple[str, str, str]] = []

    async def _seam(request_id: str, description: str, parent: str) -> Any:
        asked.append((request_id, description, parent))
        return answers.pop(0)

    monkeypatch.setattr(gw_mod, "deliver_spawn_approval", _seam)
    approve = done.manager_kwargs["on_spawn_approval"]
    _kwargs, spawn_gate = done.approvals[1]

    assert await approve("spawn:a1", "run it", "telegram:9") is False
    spawn_gate.assert_not_awaited()
    assert await approve("spawn:a2", "run that", "dashboard:main") is True
    assert asked == [
        ("spawn:a1", "run it", "telegram:9"),
        ("spawn:a2", "run that", "dashboard:main"),
    ]
    (event, parent), _kw = spawn_gate.await_args
    assert (event.kind, event.request_id, event.title, parent) == (
        "permission_request",
        "spawn:a2",
        "run that",
        "dashboard:main",
    )


@pytest.mark.asyncio
async def test_an_event_without_a_dashboard_is_dropped(done: _Done) -> None:
    done.orch.dashboard_state = None

    await done.manager_kwargs["on_event"]("subagent_spawn", make_info(), {})

    done.state.broadcast_ws.assert_not_called()


@pytest.mark.asyncio
async def test_an_event_frame_carries_the_wave_and_the_tab_queued_depth(done: _Done) -> None:
    on_event = done.manager_kwargs["on_event"]

    await on_event("subagent_spawn", make_info(batch_id="w1", batch_total=2), {"x": 1})
    await on_event("subagent_queued", make_info(), {"queued": "unknown"})

    assert done.frames("subagent_spawn") == [
        {"id": "a1", "slot": "evt:dashboard:main", "batch_id": "w1", "x": 1}
    ]
    assert done.frames("subagent_queued") == [
        {"id": "a1", "slot": "evt:dashboard:main", "queued": "unknown"}
    ]


@pytest.mark.asyncio
async def test_a_failed_injection_notice_lands_on_the_loaded_tab_and_queues_its_failure(
    done: _Done,
) -> None:
    restored = _Slot("evt:dashboard:main")
    restored.window_pending = True
    loaded = MagicMock(name="loaded tab", _pending_subagent_failures=[])
    tabs = iter([restored, loaded])
    done.state.get_slot = MagicMock(side_effect=lambda _name: next(tabs))
    info = make_info()

    await done.manager_kwargs["on_event"](
        "subagent_injection_failed",
        info,
        {"error": "boom SECRET", "failure_msg": "report EXFIL"},
    )

    assert ("load", "evt:dashboard:main") in done.calls
    loaded.append.assert_called_once()
    assert "Error: boom [R]" in loaded.append.call_args.args[1]
    assert loaded._pending_subagent_failures == ["report [U]"]
    (content, deliveries), _kw = loaded.note_pending_subagent_delivery.call_args
    assert content == "report [U]" and [d.agent_id for d in deliveries] == ["a1"]
    done.state.push_slots_update.assert_called_once_with()
    assert len(done.frames("subagent_injection_failed")) == 1


@pytest.mark.asyncio
async def test_a_stalled_child_kicks_the_parents_parked_queue(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    drain = AsyncMock()
    monkeypatch.setattr(gw_mod, "drain_idle_parent_queue_for_stall", drain)

    await done.manager_kwargs["on_event"]("subagent_stalled", make_info(), {"stalled": True})
    await done.drain()

    drain.assert_awaited_once_with(done.state, "dashboard:main")
    assert len(done.frames("subagent_stalled")) == 1


@pytest.mark.asyncio
async def test_an_orphan_notice_without_a_tab_falls_through_to_the_owner_dm(done: _Done) -> None:
    notify = done.manager_kwargs["on_orphan_notify"]

    assert await notify("subagent:p1", "orphaned") is False
    assert await notify("dashboard:main", "orphaned") is False


@pytest.mark.asyncio
async def test_an_orphan_notice_loads_a_restored_tab_first(done: _Done) -> None:
    restored = _Slot("main")
    restored.window_pending = True
    loaded = MagicMock(name="loaded tab", _pending_subagent_failures=[])
    tabs = iter([restored, loaded])
    done.state.get_slot = MagicMock(side_effect=lambda _name: next(tabs))

    assert await done.manager_kwargs["on_orphan_notify"]("dashboard:main", "lost SECRET") is True

    assert ("load", "main") in done.calls
    assert loaded._pending_subagent_failures == ["lost [R]"]


@pytest.mark.asyncio
async def test_an_orphan_dm_that_reaches_no_surface_reports_it(done: _Done) -> None:
    done.state.notify = MagicMock(side_effect=RuntimeError("bell down"))

    assert await done.manager_kwargs["on_orphan_dm"]("orphaned SECRET") is False
    done.state.notify.assert_called_once_with(
        "subagent", "Sub-agent orphaned by restart", "orphaned [R]"
    )
