"""Slack's native Stop (``agent_session_stopped``) and user renames.

The handlers run against a session-manager double that records ``stop_turn``,
the real router helpers (busy hold, queue owner tags, flat-DM key), and the
real Slack client over a recording ``_web`` -- so "nothing is posted after
Stop" is checked on the wire, not on a mock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_slack_agent_session_status import _caps, make_client  # noqa: F401 - autouse fixture

from kiro_crew.config.loader import KiroCrewConfig, MessagingConfig, SlackConfig
from kiro_crew.session_lifecycle import STOP_DECLINED_COMPACTING_TEXT
from kiro_crew.slack import events
from kiro_crew.slack.agent_sessions import fence, session_events
from kiro_crew.slack.agent_sessions.status import SESSION_STATUS_PROCESSING
from kiro_crew.slack.events_inbound import SeenCache
from kiro_crew.testing.wait import await_until


class FakeSessions:
    def __init__(
        self,
        *,
        live: set[str] | None = None,
        owners: dict[str, str] | None = None,
        compacting: set[str] | None = None,
        outcome: str = "soft",
    ) -> None:
        self.live = live or set()
        self.owners = owners or {}
        self.compacting = compacting or set()
        self.outcome = outcome
        self.stops: list[tuple[str, dict[str, Any]]] = []
        self.on_stop: Any = None
        #: The session queue the stop hold detaches at a press and puts back.
        self.queue: list[tuple[Any, ...]] = []
        self.restored: list[tuple[Any, ...]] = []
        self.cleared: list[tuple[Any, ...]] = []

    def detach_queue(self, key: str) -> tuple[Any, ...]:
        out, self.queue = tuple(self.queue), []
        return out

    def restore_queue(self, key: str, entries: tuple[Any, ...]) -> None:
        self.restored.extend(entries)
        self.queue = list(entries) + self.queue

    def clear_queue(self, key: str, only: tuple[Any, ...] = ()) -> None:
        self.cleared.extend(only)

    @staticmethod
    def _fold(key: str) -> str:
        return key if ":" in key or "-" in key else f"slack:{key}"

    def has_session(self, key: str) -> bool:
        return self._fold(key) in self.live

    def get_session_for_thread(self, ts: str) -> str | None:
        return self.owners.get(ts)

    def is_compacting(self, key: str) -> bool:
        return self._fold(key) in self.compacting

    def dequeue(self, key: str) -> Any:
        return self.queue.pop(0) if self.queue else None

    async def stop_turn(self, key: str, **kwargs: Any) -> str:
        self.stops.append((key, kwargs))
        if self.on_stop is not None:
            await self.on_stop()
        if self._fold(key) in self.compacting and not kwargs.get("force"):
            return "compacting"
        return self.outcome if self.has_session(key) else "idle"


class Orch:
    def __init__(
        self,
        sessions: FakeSessions,
        *,
        flat: bool = False,
        live: bool = True,
    ) -> None:
        self._cfg = KiroCrewConfig(
            slack=SlackConfig(dm_single_session=flat, agent_sessions=live),
            messaging=MessagingConfig(use_transport=True),
        )
        self.sessions = sessions
        self.slack, self.web = make_client(live=live)
        self._session_tasks: dict[str, Any] = {}
        self._pending_queue: dict[str, Any] = {}
        self._handler_tasks: set[Any] = set()
        self.conv_log = None


class SelRecorder:
    def __init__(self) -> None:
        self.access: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.release_failures: list[str] = []
        self.expect_release_failure = False

    def log_api_access(self, **kw: Any) -> None:
        self.access.append(kw)

    def log_tool_invocation(self, **kw: Any) -> None:
        self.tools.append(kw)


@pytest.fixture(autouse=True)
def _env(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    recorder = SelRecorder()
    _floor_monkeypatch.setattr(session_events, "sel", lambda: recorder)
    _floor_monkeypatch.setattr(events, "is_owner", lambda uid: uid == "UOWNER")
    _floor_monkeypatch.setattr(events, "is_allowed_user", lambda uid: uid in {"UOWNER", "U1"})
    _floor_monkeypatch.setattr(session_events, "_SEEN_EVENTS", SeenCache(512))
    session_events._NEWEST_TITLE_TS.clear()
    # handle_stopped never raises, so a stop hold whose release raised would pass
    # quietly; every test fails on one unless it says it expects it.
    real_warning = session_events.logger.warning
    recorder.release_failures = []

    def warning(msg: str, *a: Any, **kw: Any) -> None:
        if "stop hold release failed" in msg:
            recorder.release_failures.append(msg)
        real_warning(msg, *a, **kw)

    _floor_monkeypatch.setattr(session_events.logger, "warning", warning)
    yield recorder
    assert not recorder.release_failures or recorder.expect_release_failure


def stop_event(**kw: Any) -> dict[str, Any]:
    base = {
        "type": "agent_session_stopped",
        "channel": "C1",
        "thread_ts": "1700.1",
        "user": "U1",
        "event_ts": "1800.1",
        "streaming_message_ts": [],
    }
    base.update(kw)
    return base


async def _working(orch: Orch, channel: str = "C1", thread: str = "1700.1") -> None:
    await orch.slack.set_session_status(channel, thread, SESSION_STATUS_PROCESSING)
    orch.web.calls.clear()


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_during_streaming_stops_the_turn_and_posts_nothing_after(
        self, _env: SelRecorder
    ) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        orch.web.responses["chat.startStream"] = {"ok": True, "ts": "1700.5"}
        started, release = asyncio.Event(), asyncio.Event()

        async def turn() -> None:
            reset = fence.enter_turn("C1", "1700.1")
            try:
                await orch.slack.start_stream("C1", "1700.1")  # the reply's stream, 1700.5
                await orch.slack.post_message("C1", "partial…", "1700.1")
                started.set()
                await release.wait()
                # What a cancelled turn's tail would do: seal, footer, clear.
                await orch.slack.stop_stream("C1", "1700.5", "partial answer")
                await orch.slack.post_blocks("C1", [], "⏱ 12s", "1700.1")
                await orch.slack.set_thread_status("C1", "1700.1", "")
            finally:
                fence.exit_turn(reset)

        task = asyncio.ensure_future(turn())
        try:
            await await_until(started, what="the turn's first post")
            # The cancel lands while stop_turn waits for the ack; the turn's tail
            # then runs to its end (not cancelled here, so the fence is what keeps
            # it quiet).
            sessions.on_stop = _async(release.set)
            outcome = await session_events.handle_stopped(
                orch, stop_event(streaming_message_ts=["1700.5"])
            )
            await await_until(task, what="the stopped turn's tail")
        finally:
            release.set()
            if not task.done():
                task.cancel()
        assert outcome == "soft"
        assert [k for k, _ in sessions.stops] == ["1700.1"]
        assert sessions.stops[0][1].get("preserve_queue") is True
        # The streamed partial stays; after Stop only the seal and the status move.
        assert orch.web.calls == [
            ("chat.startStream", orch.web.calls[0][1]),
            ("chat.postMessage", orch.web.calls[1][1]),
            ("chat.stopStream", {"channel": "C1", "ts": "1700.5"}),
            (
                "agents.sessions.setStatus",
                {"channel_id": "C1", "status": "active", "thread_ts": "1700.1"},
            ),
        ]
        assert any(a["outcome"] == "allowed" for a in _env.access)
        assert _env.tools[-1]["outcome"] == "soft"

    @pytest.mark.asyncio
    async def test_the_running_task_is_cancelled_and_untracked(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        task = asyncio.ensure_future(asyncio.sleep(60))
        orch._session_tasks["1700.1"] = task
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        await await_until(
            asyncio.gather(task, return_exceptions=True), what="the cancelled router task"
        )
        assert task.cancelled() and "1700.1" not in orch._session_tasks

    @pytest.mark.asyncio
    async def test_stop_while_an_approval_waits(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await orch.slack.set_thread_status("C1", "1700.1", "Waiting for approval…")
        orch.web.calls.clear()
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert orch.web.methods() == ["agents.sessions.setStatus"]
        assert orch.web.calls[0][1]["status"] == "active"

    @pytest.mark.asyncio
    async def test_stop_on_an_idle_thread_of_ours_only_settles_the_status(self) -> None:
        sessions = FakeSessions()  # nothing running any more
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event()) == "idle"
        assert [p["status"] for _, p in orch.web.calls] == ["active"]

    @pytest.mark.asyncio
    async def test_a_redelivered_stop_is_dropped(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert await session_events.handle_stopped(orch, stop_event()) == "duplicate"
        assert len(sessions.stops) == 1

    @pytest.mark.asyncio
    async def test_a_second_press_is_idempotent(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        await session_events.handle_stopped(orch, stop_event())
        sessions.live.clear()
        assert await session_events.handle_stopped(orch, stop_event(event_ts="1800.2")) == "idle"
        assert [p["status"] for _, p in orch.web.calls] == ["active", "active"]

    @pytest.mark.asyncio
    async def test_a_thread_the_bot_has_no_session_in_is_left_alone(
        self, _env: SelRecorder
    ) -> None:
        sessions = FakeSessions()
        orch = Orch(sessions)
        assert await session_events.handle_stopped(orch, stop_event()) == "not_ours"
        assert sessions.stops == [] and orch.web.calls == []  # no setStatus: it would create one
        assert _env.access[-1]["outcome"] == "no_session"

    @pytest.mark.asyncio
    async def test_a_stop_from_someone_not_allowed_stops_nothing(self, _env: SelRecorder) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event(user="UOTHER")) == "denied"
        assert sessions.stops == [] and orch.web.calls == []
        assert _env.access[-1] == {
            "caller": "UOTHER",
            "operation": "slack.agent_session_stopped",
            "outcome": "denied",
            "source": "slack",
            "resources": "C1:1700.1",
            "error": "unauthorized sender",
        }

    @pytest.mark.asyncio
    async def test_a_dm_thread_stops_like_a_channel_thread(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch, channel="D1")
        assert await session_events.handle_stopped(orch, stop_event(channel="D1")) == "soft"
        assert [k for k, _ in sessions.stops] == ["1700.1"]

    @pytest.mark.asyncio
    async def test_a_single_session_dm_stops_the_flat_key_and_mutes_the_whole_dm(self) -> None:
        sessions = FakeSessions(live={"slack:D1"})
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.9")
        root_turn = fence.enter_turn("D1", "1700.2")  # a turn replying in another thread
        try:
            outcome = await session_events.handle_stopped(
                orch, stop_event(channel="D1", thread_ts="1700.9")
            )
            assert await orch.slack.post_message("D1", "late", "1700.2") == ""
        finally:
            fence.exit_turn(root_turn)
        assert outcome == "soft"
        assert [k for k, _ in sessions.stops] == ["slack:D1"]

    @pytest.mark.asyncio
    async def test_a_single_session_dm_stop_settles_the_thread_whose_turn_it_ended(
        self,
    ) -> None:
        """Pressed in thread A after A finished, while a queued turn runs in thread B:
        the Stop ends B's turn, so B leaves ``processing`` too."""
        sessions = FakeSessions(live={"slack:D1"})
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.9")
        await orch.slack.set_session_status("D1", "1700.2", SESSION_STATUS_PROCESSING)
        orch.web.calls.clear()
        turn_b = fence.enter_turn("D1", "1700.2")
        try:
            outcome = await session_events.handle_stopped(
                orch, stop_event(channel="D1", thread_ts="1700.9")
            )
        finally:
            fence.exit_turn(turn_b)
        assert outcome == "soft"
        settled = {p.get("thread_ts") for _, p in orch.web.calls if p.get("status") == "active"}
        assert settled == {"1700.9", "1700.2"}

    @pytest.mark.asyncio
    async def test_a_single_session_dm_stop_sets_no_status_in_a_thread_never_shown_working(
        self,
    ) -> None:
        """Thread B's turn has not set ``processing`` yet (still before its prompt, or
        a command reply): the Stop must not create a session there with ``active``."""
        sessions = FakeSessions(live={"slack:D1"})
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.9")
        turn_b = fence.enter_turn("D1", "1700.2")
        try:
            await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.9"))
        finally:
            fence.exit_turn(turn_b)
        assert not [p for _, p in orch.web.calls if p.get("thread_ts") == "1700.2"]

    @pytest.mark.asyncio
    async def test_a_dashboard_linked_thread_stops_its_owner_without_flat_dms(self) -> None:
        """The turn runs in the dashboard session that owns the thread."""
        sessions = FakeSessions(live={"chat-7-abc"}, owners={"1700.1": "chat-7-abc"})
        orch = Orch(sessions)
        await _working(orch)
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert [k for k, _ in sessions.stops] == ["chat-7-abc"]

    @pytest.mark.asyncio
    async def test_a_self_derived_owner_is_not_a_binding(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"}, owners={"1700.1": "slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        await session_events.handle_stopped(orch, stop_event())
        assert [k for k, _ in sessions.stops] == ["1700.1"]

    @pytest.mark.asyncio
    async def test_stop_during_compaction_is_declined_then_forced(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"}, compacting={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        turn = fence.enter_turn("C1", "1700.1")
        try:
            assert await session_events.handle_stopped(orch, stop_event()) == "compacting"
            # Declined before any side effect: no stop_turn, the turn still talks.
            assert sessions.stops == []
            assert await orch.slack.post_message("C1", "still working", "1700.1") == "9.9"
        finally:
            fence.exit_turn(turn)
        assert (
            "chat.postEphemeral",
            {
                "channel": "C1",
                "user": "U1",
                "text": STOP_DECLINED_COMPACTING_TEXT,
                "thread_ts": "1700.1",
            },
        ) in orch.web.calls
        assert "agents.sessions.setStatus" not in orch.web.methods()  # still working
        outcome = await session_events.handle_stopped(orch, stop_event(event_ts="1800.2"))
        assert sessions.stops[-1][1].get("force") is True
        assert outcome == "soft"

    @pytest.mark.asyncio
    async def test_a_declined_stop_with_no_warning_delivered_arms_nothing(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"}, compacting={"slack:1700.1"})
        orch = Orch(sessions)
        orch.web.chat_postEphemeral = _raise  # type: ignore[method-assign]
        await session_events.handle_stopped(orch, stop_event())
        await session_events.handle_stopped(orch, stop_event(event_ts="1800.2"))
        assert sessions.stops == []  # the second press is a first press again

    @pytest.mark.asyncio
    async def test_a_stop_before_the_prompt_opens_does_not_hide_the_turn(self) -> None:
        """Nothing to cancel yet (no session, no router task): the turn stays audible."""
        sessions = FakeSessions()
        orch = Orch(sessions)
        await _working(orch)
        turn = fence.enter_turn("C1", "1700.1")
        try:
            assert await session_events.handle_stopped(orch, stop_event()) == "idle"
            assert await orch.slack.post_message("C1", "the answer", "1700.1") == "9.9"
        finally:
            fence.exit_turn(turn)
        # Still working, so the loader stays: no setStatus active.
        assert "agents.sessions.setStatus" not in orch.web.methods()

    @pytest.mark.asyncio
    async def test_a_second_stop_that_stops_nothing_keeps_the_first_stops_fence(self) -> None:
        """A turn no router task tracks, already stopped by one Stop: a second press
        answers ``idle`` with nothing cancelled, and must not make it audible again."""
        sessions = FakeSessions()
        orch = Orch(sessions)
        await _working(orch)
        turn = fence.enter_turn("C1", "1700.1")
        try:
            first = fence.mute_turns("C1", "1700.1")  # the first, successful Stop
            assert first
            fence.release(first, ended=True)
            assert await session_events.handle_stopped(orch, stop_event()) == "idle"
            assert await orch.slack.post_message("C1", "the stopped draft", "1700.1") == ""
        finally:
            fence.exit_turn(turn)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("b_first", [False, True])
    async def test_an_overlapping_stop_keeps_the_loader_of_a_turn_that_runs_on(
        self, b_first: bool
    ) -> None:
        """Two Stops on a turn no router task tracks both end nothing: whichever
        decides first, the still-working turn keeps its loader and stays audible."""
        sessions = FakeSessions()
        orch = Orch(sessions)
        await _working(orch)
        gate_a, gate_b = asyncio.Event(), asyncio.Event()
        pressed = [asyncio.Event(), asyncio.Event()]
        presses = 0

        async def on_stop() -> None:
            nonlocal presses
            presses += 1
            pressed[presses - 1].set()
            await (gate_a if presses == 1 else gate_b).wait()

        sessions.on_stop = on_stop
        turn = fence.enter_turn("C1", "1700.1")
        try:
            stop_a = asyncio.ensure_future(session_events.handle_stopped(orch, stop_event()))
            await await_until(pressed[0], what="Stop A in flight")
            stop_b = asyncio.ensure_future(
                session_events.handle_stopped(orch, stop_event(event_ts="1800.2"))
            )
            await await_until(pressed[1], what="Stop B in flight")
            order = (
                [(gate_b, stop_b), (gate_a, stop_a)]
                if b_first
                else [(gate_a, stop_a), (gate_b, stop_b)]
            )
            for gate, stop in order:
                gate.set()
                assert await await_until(stop, what="the released Stop to decide") == "idle"
            assert await orch.slack.post_message("C1", "the answer", "1700.1") == "9.9"
        finally:
            fence.exit_turn(turn)
        assert "agents.sessions.setStatus" not in orch.web.methods()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ender_first", [False, True])
    async def test_a_stop_that_ended_the_turn_wins_over_one_that_ended_nothing(
        self, ender_first: bool
    ) -> None:
        """Stop A ends the turn (soft); overlapping Stop B ends nothing (idle). In
        either decision order the stopped turn stays silent and the session settles."""
        sessions = FakeSessions()
        orch = Orch(sessions)
        await _working(orch)
        gates = [asyncio.Event(), asyncio.Event()]
        pressed = [asyncio.Event(), asyncio.Event()]
        presses = 0

        async def on_stop() -> None:
            nonlocal presses
            presses += 1
            mine = presses - 1
            pressed[mine].set()
            await gates[mine].wait()
            # What stop_turn answers is read right after: A ended a turn, B found none.
            sessions.live = {"slack:1700.1"} if mine == 0 else set()
            sessions.outcome = "soft"

        sessions.on_stop = on_stop
        turn = fence.enter_turn("C1", "1700.1")
        try:
            stop_a = asyncio.ensure_future(session_events.handle_stopped(orch, stop_event()))
            await await_until(pressed[0], what="Stop A in flight")
            stop_b = asyncio.ensure_future(
                session_events.handle_stopped(orch, stop_event(event_ts="1800.2"))
            )
            await await_until(pressed[1], what="Stop B in flight")
            order = [(0, stop_a), (1, stop_b)] if ender_first else [(1, stop_b), (0, stop_a)]
            outcomes = {}
            for i, stop in order:
                gates[i].set()
                outcomes[i] = await await_until(stop, what=f"Stop {'AB'[i]} to decide")
            assert outcomes == {0: "soft", 1: "idle"}
            assert await orch.slack.post_message("C1", "the stopped draft", "1700.1") == ""
        finally:
            fence.exit_turn(turn)
        assert "active" in [p.get("status") for _, p in orch.web.calls]

    @pytest.mark.asyncio
    async def test_a_stop_before_the_prompt_opens_cancels_the_router_task(self) -> None:
        sessions = FakeSessions()
        orch = Orch(sessions)
        await _working(orch)
        task = asyncio.ensure_future(asyncio.sleep(60))
        orch._session_tasks["1700.1"] = task
        turn = fence.enter_turn("C1", "1700.1")
        try:
            assert await session_events.handle_stopped(orch, stop_event()) == "idle"
            assert await orch.slack.post_message("C1", "late", "1700.1") == ""
        finally:
            fence.exit_turn(turn)
        await await_until(
            asyncio.gather(task, return_exceptions=True), what="the cancelled router task"
        )
        assert task.cancelled()
        assert [p["status"] for _, p in orch.web.calls if "status" in p] == ["active"]

    @pytest.mark.asyncio
    async def test_a_failing_stop_still_ends_the_task_and_settles(self, _env: SelRecorder) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)

        async def boom() -> None:
            raise RuntimeError("reset failed")

        sessions.on_stop = boom
        task = asyncio.ensure_future(asyncio.sleep(60))
        orch._session_tasks["1700.1"] = task
        assert await session_events.handle_stopped(orch, stop_event()) == "error"
        await await_until(
            asyncio.gather(task, return_exceptions=True), what="the cancelled router task"
        )
        assert task.cancelled() and "1700.1" not in orch._session_tasks
        assert [p["status"] for _, p in orch.web.calls] == ["active"]
        assert _env.tools[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_a_failing_stop_with_nothing_to_cancel_leaves_the_turn_audible(self) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)

        async def boom() -> None:
            raise RuntimeError("reset failed")

        sessions.on_stop = boom
        turn = fence.enter_turn("C1", "1700.1")
        try:
            assert await session_events.handle_stopped(orch, stop_event()) == "error"
            assert await orch.slack.post_message("C1", "still here", "1700.1") == "9.9"
        finally:
            fence.exit_turn(turn)


class TestFeatureGate:
    @pytest.mark.asyncio
    async def test_with_agent_sessions_off_session_events_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off is identical to main, which never acted on these events."""
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions, live=False)
        orch.conv_log = TitleLog()
        _gate(monkeypatch, allowed=True)
        session_events.dispatch(orch, {"team_id": "T1"}, stop_event())
        session_events.dispatch(
            orch,
            {"team_id": "T1"},
            {
                "type": "agent_session_title_changed",
                "channel": "C1",
                "thread_ts": "1700.1",
                "title": "x",
                "user": "U1",
            },
        )
        assert orch._handler_tasks == set() and sessions.stops == []

    @pytest.mark.asyncio
    async def test_a_stop_on_a_session_left_working_still_runs_after_the_switch_goes_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off mid-turn: the Stop on a session this process set working is still
        handled and settles it, while every other event stays ignored."""
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        _gate(monkeypatch, allowed=True)
        await orch.slack.set_thread_status("C1", "1700.1", "is working on your request")
        orch._cfg.slack.agent_sessions = False
        orch.slack.agent_sessions._gate = lambda: False
        session_events.dispatch(orch, {"team_id": "T1"}, stop_event())
        assert len(orch._handler_tasks) == 1
        await await_until(asyncio.gather(*orch._handler_tasks), what="the dispatched Stop")
        assert orch.web.calls[-1][1]["status"] == "active"
        # A Stop on a thread with nothing owed is still ignored while off.
        session_events.dispatch(orch, {"team_id": "T1"}, stop_event(thread_ts="1800.1"))
        assert orch._handler_tasks == set()

    @pytest.mark.asyncio
    async def test_on_but_unavailable_session_events_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack import features

        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        _gate(monkeypatch, allowed=True)
        features.latch_unavailable(features.AGENT_SESSIONS, "feature_disabled")
        session_events.dispatch(orch, {"team_id": "T1"}, stop_event())
        assert orch._handler_tasks == set() and sessions.stops == []

    @pytest.mark.asyncio
    async def test_a_whole_channel_stop_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Stop with no ``thread_ts`` belongs to no feature this gateway runs."""
        from kiro_crew.slack import features

        _gate(monkeypatch, allowed=True)
        features.record_granted_scopes({"assistant:write"})
        on = Orch(FakeSessions())
        session_events.dispatch(on, {"team_id": "T1"}, stop_event(thread_ts=None))
        assert on._handler_tasks == set()

    def test_the_native_turn_is_fenced_through_its_delivery_tail(self) -> None:
        """``_run_turn`` opens the fence; ``handle_message`` closes it after delivery."""
        import inspect

        from kiro_crew.slack import handler
        from kiro_crew.slack.handler_runtime import turn

        run_src = inspect.getsource(turn._run_turn)
        enter = run_src.index("turn._turn_scope = enter_turn(channel, reply_ts)")
        assert enter < run_src.index(
            "await slack.set_thread_status(channel, reply_ts, _STATUS_WORKING)"
        )
        assert "exit_turn(" not in run_src
        src = inspect.getsource(handler.handle_message)
        assert src.count("turn.close_turn_scope()") == 1
        assert "exit_turn(" not in src
        # The function's last statement: a finally after the answer, its tail and
        # the cancel arm, so every exit (a cancel included) closes the fence.
        assert src.rstrip().endswith(
            "    finally:\n"
            "        # Native Stop's write fence, opened by _run_turn, closes after the answer.\n"
            "        turn.close_turn_scope()"
        )
        body = src[src.index("    try:\n") : src.index("    finally:\n")]
        assert "await _deliver_answer(turn)" in body
        assert "except asyncio.CancelledError:" in body
        assert "exit_turn(scope)" in inspect.getsource(turn._SlackTurn.close_turn_scope)


class TestDispatchGate:
    @pytest.mark.asyncio
    async def test_no_workspace_id_is_refused(
        self, _env: SelRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        session_events.dispatch(orch, {}, stop_event())
        assert orch._handler_tasks == set()
        assert _env.access[-1]["error"] == "missing_team_id"

    @pytest.mark.asyncio
    async def test_a_disallowed_workspace_is_refused(
        self, _env: SelRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        _gate(monkeypatch, allowed=False)
        session_events.dispatch(orch, {"team_id": "TBAD"}, stop_event())
        assert orch._handler_tasks == set()
        assert _env.access[-1]["error"] == "enterprise_origin_mismatch"

    @pytest.mark.asyncio
    async def test_an_allowed_workspace_reaches_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        _gate(monkeypatch, allowed=True)
        session_events.dispatch(orch, {"team_id": "T1"}, stop_event())
        await await_until(asyncio.gather(*orch._handler_tasks), what="the dispatched Stop")
        assert [k for k, _ in sessions.stops] == ["1700.1"]

    @pytest.mark.asyncio
    async def test_the_socket_listener_routes_session_events_here(self) -> None:
        import inspect

        src = inspect.getsource(events.init_socket_mode)
        assert "event_type in AGENT_SESSION_EVENTS" in src
        assert "dispatch_agent_session_event(orch, req.payload or {}, event)" in src


class TestTitleChanged:
    def _event(self, **kw: Any) -> dict[str, Any]:
        base = {
            "type": "agent_session_title_changed",
            "channel": "C1",
            "thread_ts": "1700.1",
            "title": "Bora Bora trip prep",
            "previous_title": "Scuba",
            "user": "U1",
        }
        base.update(kw)
        return base

    @pytest.mark.asyncio
    async def test_a_rename_titles_the_dashboard_session(self, _env: SelRecorder) -> None:
        from kiro_crew.messaging import auto_title

        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "renamed"
        assert orch.conv_log.titles == [("slack:1700.1", "Bora Bora trip prep")]
        assert auto_title._titled.get("slack:1700.1") == auto_title.TITLE_KIND_MANUAL
        assert _env.access[-1]["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_a_closed_tab_rename_is_stored_as_a_user_title(self) -> None:
        # A tab reopened on the transcript must restore it as final, never as an
        # auto title its background refresh may replace.
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "renamed"
        assert orch.conv_log.calls[-1][1] == {
            "title": "Bora Bora trip prep",
            "title_origin": "user",
        }

    @pytest.mark.asyncio
    async def test_a_channels_policy_denying_slack_blocks_the_rename(
        self, monkeypatch: pytest.MonkeyPatch, _env: SelRecorder
    ) -> None:
        async def denied(channel_type: str) -> bool:
            return False

        monkeypatch.setattr(session_events, "channel_inbound_permitted", denied)
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "denied"
        assert orch.conv_log.titles == []
        assert _env.access[-1]["error"] == "channels policy denies slack"

    @pytest.mark.asyncio
    async def test_a_policy_refusing_this_slack_channel_blocks_the_rename(
        self, monkeypatch: pytest.MonkeyPatch, _env: SelRecorder
    ) -> None:
        """The policy's Slack channel list applies to a rename as to a message."""
        asked: list[tuple[str, dict]] = []

        def refuse(channel: str, **kw: Any) -> str:
            asked.append((channel, kw))
            return f"slack channel {channel!r} refused by the security policy"

        monkeypatch.setattr(session_events, "channel_refusal", refuse)
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "denied"
        assert orch.conv_log.titles == []
        assert asked[0] == ("C1", {"caller": "U1", "audit": False})
        assert all(not kw.get("audit", True) for _c, kw in asked)  # the rename audits once
        # One audit row, the rename's own, naming the refusal.
        assert _env.access[-1]["operation"] == "slack.agent_session_title_changed"
        assert _env.access[-1]["error"] == "slack channel 'C1' refused by the security policy"

    @pytest.mark.asyncio
    async def test_a_failed_title_write_leaves_auto_titling_on(self) -> None:
        from kiro_crew.messaging import auto_title

        class Broken:
            def set_title(self, key: str, title: str) -> None:
                raise OSError("disk full")

        orch = Orch(FakeSessions(live={"slack:1700.9"}))
        orch.conv_log = Broken()
        event = self._event(thread_ts="1700.9")
        assert await session_events.handle_title_changed(orch, event) == "failed"
        assert auto_title._titled.get("slack:1700.9") is None

    @pytest.mark.asyncio
    async def test_a_write_the_log_skipped_reports_failed_and_marks_nothing(self) -> None:
        """A legacy message-first transcript skips the metadata write silently."""
        from kiro_crew.messaging import auto_title

        log = TitleLog(writes=False)
        orch = Orch(FakeSessions(live={"slack:1700.8"}))
        orch.conv_log = log
        event = self._event(thread_ts="1700.8")
        assert await session_events.handle_title_changed(orch, event) == "failed"
        fields = {"title": "Bora Bora trip prep", "title_origin": "user"}
        assert log.calls == [("slack:1700.8", fields, True, True)]
        assert auto_title._titled.get("slack:1700.8") is None

    @pytest.mark.asyncio
    async def test_a_durably_incognito_thread_records_nothing_after_a_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The in-memory trackers are empty; the durable record must be read first."""
        from kiro_crew.messaging import privacy_mode

        restored: list[str] = []

        async def hydrate_inbound(sessions: Any, key: str) -> None:
            restored.append(key)
            privacy_mode.mark_incognito(key)

        monkeypatch.setattr(privacy_mode, "hydrate_inbound", hydrate_inbound)
        orch = Orch(FakeSessions(live={"slack:1700.7"}))
        orch.conv_log = TitleLog()
        event = self._event(thread_ts="1700.7")
        try:
            assert await session_events.handle_title_changed(orch, event) == "restricted"
        finally:
            privacy_mode.reset()
        assert restored == ["slack:1700.7"] and orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_thread_claimed_during_the_read_is_not_renamed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import privacy_mode

        sessions = FakeSessions(live={"slack:1700.6"})

        async def hydrate_inbound(s: Any, key: str) -> None:
            sessions.owners["1700.6"] = "chat-9-new"  # a send-to-Slack lands meanwhile

        monkeypatch.setattr(privacy_mode, "hydrate_inbound", hydrate_inbound)
        orch = Orch(sessions)
        orch.conv_log = TitleLog()
        assert (
            await session_events.handle_title_changed(orch, self._event(thread_ts="1700.6"))
            == "ignored"
        )
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_live_dashboard_slot_is_renamed_through_the_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The slot is authoritative: a transcript-only write would be saved over."""
        from kiro_crew.dashboard import channel_slots

        renamed: list[tuple[Any, str, str]] = []

        async def rename_live(state: Any, key: str, title: str) -> bool:
            renamed.append((state, key, title))
            return True

        monkeypatch.setattr(channel_slots, "rename_channel_title_live", rename_live)
        sessions = FakeSessions(live={"dashboard:abc"}, owners={"1700.1": "dashboard:abc"})
        orch = Orch(sessions)
        orch.dashboard_state = state = object()
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "renamed"
        assert renamed == [(state, "dashboard:abc", "Bora Bora trip prep")]
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_thread_the_bot_never_answered_is_not_titled(self) -> None:
        orch = Orch(FakeSessions())
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "ignored"
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_one_thread_of_a_single_session_dm_does_not_rename_the_dm(self) -> None:
        orch = Orch(FakeSessions(live={"slack:D1"}), flat=True)
        orch.conv_log = TitleLog()
        assert (
            await session_events.handle_title_changed(orch, self._event(channel="D1")) == "ignored"
        )
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_linked_thread_titles_its_owner(self) -> None:
        orch = Orch(FakeSessions(owners={"1700.1": "chat-7-abc"}))
        orch.conv_log = TitleLog()
        await session_events.handle_title_changed(orch, self._event())
        assert orch.conv_log.titles == [("chat-7-abc", "Bora Bora trip prep")]

    @pytest.mark.asyncio
    async def test_a_rename_by_someone_not_allowed_is_not_mirrored(self, _env: SelRecorder) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event(user="UX")) == "denied"
        assert orch.conv_log.titles == []
        assert _env.access[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_our_own_rename_coming_back_is_ignored(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        await orch.slack.set_thread_title("C1", "slack:1700.1", "Bora Bora trip prep")
        assert await session_events.handle_title_changed(orch, self._event()) == "echo"
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_person_renaming_back_to_our_title_is_mirrored(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        await orch.slack.set_thread_title("C1", "slack:1700.1", "A")
        assert await session_events.handle_title_changed(orch, self._event(title="A")) == "echo"
        assert await session_events.handle_title_changed(orch, self._event(title="B")) == "renamed"
        assert await session_events.handle_title_changed(orch, self._event(title="A")) == "renamed"
        assert [t for _, t in orch.conv_log.titles] == ["B", "A"]

    @pytest.mark.asyncio
    async def test_a_person_renaming_back_before_our_echo_arrives_is_mirrored(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        await orch.slack.set_thread_title("C1", "slack:1700.1", "A")
        # Slack never echoed A: the person's B clears it, so their A counts.
        assert await session_events.handle_title_changed(orch, self._event(title="B")) == "renamed"
        assert await session_events.handle_title_changed(orch, self._event(title="A")) == "renamed"
        assert [t for _, t in orch.conv_log.titles] == ["B", "A"]

    @staticmethod
    def _stall_first_policy_read(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
        """The first rename's policy read waits on the returned gate; later ones don't."""
        gate = asyncio.Event()
        calls: list[str] = []

        async def permitted(channel_type: str) -> bool:
            calls.append(channel_type)
            if len(calls) == 1:
                await gate.wait()
            return True

        monkeypatch.setattr(session_events, "channel_inbound_permitted", permitted)
        return gate

    @pytest.mark.asyncio
    async def test_two_racing_renames_leave_the_newest_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = self._stall_first_policy_read(monkeypatch)
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        older = asyncio.create_task(
            session_events.handle_title_changed(orch, self._event(title="Old", event_ts="1.1"))
        )
        newer = asyncio.create_task(
            session_events.handle_title_changed(orch, self._event(title="New", event_ts="1.2"))
        )
        for _ in range(10):
            await asyncio.sleep(0)
        # The newer rename waits its turn instead of finishing past the stalled one.
        assert orch.conv_log.titles == []
        gate.set()
        both = asyncio.gather(older, newer)
        assert await await_until(both, what="both renames") == ["renamed", "renamed"]
        assert [t for _, t in orch.conv_log.titles] == ["Old", "New"]
        assert session_events._TITLE_LOCKS == {}

    @pytest.mark.asyncio
    async def test_a_stalled_rename_does_not_hold_up_another_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = self._stall_first_policy_read(monkeypatch)
        orch = Orch(FakeSessions(live={"slack:1700.1", "slack:1700.2"}))
        orch.conv_log = TitleLog()
        stalled = asyncio.create_task(
            session_events.handle_title_changed(orch, self._event(title="One"))
        )
        await asyncio.sleep(0)
        other = self._event(thread_ts="1700.2", title="Two")
        assert await session_events.handle_title_changed(orch, other) == "renamed"
        gate.set()
        assert await await_until(stalled, what="the stalled rename") == "renamed"
        assert session_events._TITLE_LOCKS == {}

    @pytest.mark.asyncio
    async def test_a_retried_older_rename_does_not_overwrite_a_newer_one(self) -> None:
        # Rename A went unacknowledged, B was applied, then Slack retried A.
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        newer = self._event(title="New", event_ts="1700.2")
        older = self._event(title="Old", event_ts="1700.1")
        assert await session_events.handle_title_changed(orch, newer) == "renamed"
        assert await session_events.handle_title_changed(orch, older) == "stale"
        assert [t for _, t in orch.conv_log.titles] == ["New"]
        # Another thread keeps its own order.
        other = self._event(thread_ts="1700.5", title="Elsewhere", event_ts="1700.1")
        orch.sessions.live.add("slack:1700.5")
        assert await session_events.handle_title_changed(orch, other) == "renamed"

    @pytest.mark.asyncio
    async def test_a_retry_older_than_our_echo_is_stale(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        await orch.slack.set_thread_title("C1", "slack:1700.1", "A")
        echo = self._event(title="A", event_ts="1700.5")
        assert await session_events.handle_title_changed(orch, echo) == "echo"
        older = self._event(title="Old", event_ts="1700.2")
        assert await session_events.handle_title_changed(orch, older) == "stale"
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_non_finite_event_ts_orders_nothing(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        odd = self._event(title="Odd", event_ts="inf")
        assert await session_events.handle_title_changed(orch, odd) == "renamed"
        later = self._event(title="Later", event_ts="1700.3")
        assert await session_events.handle_title_changed(orch, later) == "renamed"

    @pytest.mark.asyncio
    async def test_a_denied_newer_rename_does_not_block_an_older_retry(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        denied = self._event(user="UX", title="Theirs", event_ts="1700.2")
        allowed = self._event(title="Mine", event_ts="1700.1")
        assert await session_events.handle_title_changed(orch, denied) == "denied"
        assert await session_events.handle_title_changed(orch, allowed) == "renamed"

    @pytest.mark.asyncio
    async def test_a_redelivered_echo_is_still_ours(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        await orch.slack.set_thread_title("C1", "slack:1700.1", "A")
        event = self._event(title="A", event_ts="1700.9")
        assert await session_events.handle_title_changed(orch, event) == "echo"
        assert await session_events.handle_title_changed(orch, dict(event)) == "duplicate"
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_two_of_our_renames_echo_in_order(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        await orch.slack.set_thread_title("C1", "slack:1700.1", "A")
        await orch.slack.set_thread_title("C1", "slack:1700.1", "C")
        assert await session_events.handle_title_changed(orch, self._event(title="A")) == "echo"
        assert await session_events.handle_title_changed(orch, self._event(title="C")) == "echo"
        assert orch.conv_log.titles == []

    @pytest.mark.asyncio
    async def test_a_credential_in_the_title_is_redacted(self) -> None:
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        await session_events.handle_title_changed(orch, self._event(title=f"keys {secret}"))
        assert secret not in orch.conv_log.titles[0][1]

    @pytest.mark.asyncio
    async def test_a_restricted_session_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.messaging import privacy_mode

        monkeypatch.setattr(privacy_mode, "is_restricted", lambda key: True)
        orch = Orch(FakeSessions(live={"slack:1700.1"}))
        orch.conv_log = TitleLog()
        assert await session_events.handle_title_changed(orch, self._event()) == "restricted"
        assert orch.conv_log.titles == []


class TitleLog:
    """The conversation log's title write: ``update_metadata_if`` as production uses it."""

    def __init__(self, *, writes: bool = True) -> None:
        self.titles: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict, bool, bool]] = []
        self._writes = writes

    def update_metadata_if(
        self,
        key: str,
        fields: dict,
        guard: Any,
        *,
        require_existing: bool = False,
        require_write: bool = False,
    ) -> bool:
        self.calls.append((key, dict(fields), require_existing, require_write))
        if not (self._writes and guard({})):
            return False
        self.titles.append((key, fields["title"]))
        return True


def _gate(monkeypatch: pytest.MonkeyPatch, *, allowed: bool) -> None:
    class Gate:
        def check_message_origin(self, team_id: str) -> bool:
            return allowed

    class Ctx:
        slack_gate = Gate()

    monkeypatch.setattr(events, "current_context", lambda: Ctx())


def _async(fn: Any) -> Any:
    async def _run() -> None:
        fn()
        await asyncio.sleep(0)

    return _run


async def _raise(**kwargs: Any) -> Any:
    raise RuntimeError("ephemeral failed")


def _pending(text: str, sender: str, channel: str = "C1") -> tuple[str, str, dict[str, Any]]:
    from kiro_crew.slack.events_queue import _queue_tags

    return (
        f"ts-{text}",
        text,
        dict(channel=channel, sender_id=sender, **_queue_tags(sender, channel)),
    )


def _record_dispatch(
    monkeypatch: pytest.MonkeyPatch, orch: Orch, on_start: Any = None
) -> list[str]:
    """Stand in for the router's dispatch of a queued message; returns the texts."""

    started: list[str] = []

    async def dispatch(_orch: Any, _key: str, _ts: str, text: str, kwargs: Any) -> None:
        started.append(text)
        if on_start is not None:
            await on_start()

    monkeypatch.setattr(events, "_dispatch_queued", dispatch)
    return started


async def _drain_tasks(orch: Orch) -> None:
    """Run the release's dispatches and the drains each one chains, to quiet."""
    from kiro_crew.slack import events_queue

    for _ in range(10):
        pending = [t for t in (*orch._handler_tasks, *events_queue._bg_tasks) if not t.done()]
        if not pending:
            return
        await await_until(
            asyncio.gather(*pending, return_exceptions=True),
            what="the release's dispatches and drains",
        )


class TestStopKeepsOthersQueue:
    @pytest.mark.asyncio
    async def test_native_stop_drops_the_pressers_pre_session_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F1: a cold start's stash is the presser's queue too; the release must not run it."""
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        started = _record_dispatch(monkeypatch, orch)
        orch._pending_queue["1700.1"] = [_pending("mine", "U1"), _pending("theirs", "UOWNER")]
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        await _drain_tasks(orch)
        assert started == ["theirs"]
        assert "1700.1" not in orch._pending_queue

    @pytest.mark.asyncio
    async def test_a_declined_stop_keeps_the_pressers_pre_session_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = FakeSessions(live={"slack:1700.1"}, compacting={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        started = _record_dispatch(monkeypatch, orch)
        # Forced past the pre-check so the stop itself is the one that declines.
        monkeypatch.setattr(session_events, "compaction_in_flight", lambda s, k: False)
        orch._pending_queue["1700.1"] = [_pending("mine", "U1")]
        assert await session_events.handle_stopped(orch, stop_event()) == "compacting"
        await _drain_tasks(orch)
        assert started == ["mine"]

    @pytest.mark.asyncio
    async def test_the_next_senders_turn_keeps_its_processing_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F2: the stopped turn's settle lands before the kept entry starts, never after."""
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)

        async def next_turn() -> None:
            await orch.slack.set_session_status("C1", "1700.1", SESSION_STATUS_PROCESSING)

        started = _record_dispatch(monkeypatch, orch, on_start=next_turn)
        orch._pending_queue["1700.1"] = [_pending("theirs", "UOWNER")]
        real_call = orch.web.api_call

        async def slow_seal(method: str, **kw: Any) -> Any:
            # Sealing the stopped stream is a Slack round trip: a kept entry the
            # release already dispatched runs during it.
            dispatched = orch._session_tasks.get("1700.1")
            if method == "chat.stopStream" and dispatched is not None:
                await await_until(dispatched, what="the next sender's turn")
            return await real_call(method, **kw)

        monkeypatch.setattr(orch.web, "api_call", slow_seal)
        assert (
            await session_events.handle_stopped(orch, stop_event(streaming_message_ts=["1700.5"]))
            == "soft"
        )
        await _drain_tasks(orch)
        assert started == ["theirs"]
        statuses = [p["status"] for m, p in orch.web.calls if m == "agents.sessions.setStatus"]
        assert statuses[-1] == SESSION_STATUS_PROCESSING


class TestDashboardLinkedDmThread:
    @pytest.mark.asyncio
    async def test_a_rename_reaches_the_linked_session_with_single_session_dms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F3: a DM thread a dashboard send moved to its own session is not the flat DM."""
        from kiro_crew.dashboard import channel_slots

        renamed: list[tuple[Any, str, str]] = []

        async def rename_live(state: Any, key: str, title: str) -> bool:
            renamed.append((state, key, title))
            return True

        monkeypatch.setattr(channel_slots, "rename_channel_title_live", rename_live)
        sessions = FakeSessions(
            live={"slack:D1", "dashboard:abc"}, owners={"1700.1": "dashboard:abc"}
        )
        orch = Orch(sessions, flat=True)
        orch.dashboard_state = state = object()
        orch.conv_log = TitleLog()
        event = TestTitleChanged()._event(channel="D1")
        assert await session_events.handle_title_changed(orch, event) == "renamed"
        assert renamed == [(state, "dashboard:abc", "Bora Bora trip prep")]

    @pytest.mark.asyncio
    async def test_its_stop_silences_only_that_thread(self) -> None:
        sessions = FakeSessions(
            live={"slack:D1", "dashboard:abc"}, owners={"1700.9": "dashboard:abc"}
        )
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.9")
        other = fence.enter_turn("D1", "1700.2")  # the flat DM's own turn, another thread
        try:
            outcome = await session_events.handle_stopped(
                orch, stop_event(channel="D1", thread_ts="1700.9")
            )
            assert await orch.slack.post_message("D1", "still mine", "1700.2") != ""
        finally:
            fence.exit_turn(other)
        assert outcome == "soft"
        assert [k for k, _ in sessions.stops] == ["dashboard:abc"]


class TestStopParityWithBangStop:
    """Fresh-context review of the GPT fixes: each failed on the first fix."""

    @pytest.mark.asyncio
    async def test_a_linked_dm_thread_stop_leaves_the_flat_dms_own_turn_running(self) -> None:
        """Pins ``!stop``'s keying: the linked owner is stopped through ``stop_turn``,
        and a flat-key router task started for another thread is not touched."""
        sessions = FakeSessions(
            live={"slack:D1", "dashboard:abc"}, owners={"1700.9": "dashboard:abc"}
        )
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.9")
        flat_turn = asyncio.ensure_future(asyncio.Event().wait())  # the DM's own turn
        orch._session_tasks["slack:D1"] = flat_turn
        try:
            outcome = await session_events.handle_stopped(
                orch, stop_event(channel="D1", thread_ts="1700.9")
            )
            alive = not flat_turn.done() and orch._session_tasks.get("slack:D1") is flat_turn
        finally:
            flat_turn.cancel()
        assert outcome == "soft"
        assert alive

    @pytest.mark.asyncio
    async def test_a_linked_dm_thread_stop_keeps_the_pressers_flat_dm_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = FakeSessions(live={"dashboard:abc"}, owners={"1700.9": "dashboard:abc"})
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.9")
        started = _record_dispatch(monkeypatch, orch)
        orch._pending_queue["slack:D1"] = [_pending("dm root msg", "U1", channel="D1")]
        assert (
            await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.9"))
            == "soft"
        )
        await _drain_tasks(orch)
        # The Stop addressed the linked thread only: the presser's message at the
        # DM's root is not dropped, and with nothing else running under the flat
        # key the release hands it to the router like any other kept entry.
        assert started == ["dm root msg"]
        assert not orch._pending_queue.get("slack:D1")

    @pytest.mark.asyncio
    async def test_an_overlapping_bang_stop_does_not_bring_back_the_pressers_messages(
        self,
    ) -> None:
        from kiro_crew.slack import events_queue

        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        mine = _pending("mine, queued before my Stop", "U1")
        sessions.queue = [mine]

        async def bang_stop_by_another_member() -> None:
            # The hold operations of events.py's ``!stop``: detach at its press,
            # went through, release while this Stop is still in flight.
            hold = events_queue._stops_in_flight[orch]["1700.1"]
            hold.count += 1
            held_at = len(hold.held)
            hold.held.append((list(sessions.detach_queue("1700.1")), []))
            other = events_queue._entry_owner("UOWNER", "C1")
            hold.dropping[other] = max(hold.dropping.get(other, -1), held_at)
            hold.count -= 1

        sessions.on_stop = bang_stop_by_another_member
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        assert mine not in sessions.restored and mine in sessions.cleared

    @pytest.mark.asyncio
    async def test_another_members_message_survives_and_a_later_one_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = FakeSessions(live={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        started = _record_dispatch(monkeypatch, orch)
        sessions.queue = [_pending("mine", "U1"), _pending("theirs", "UOWNER")]

        async def later_message() -> None:
            sessions.queue.append(_pending("mine, after the press", "U1"))

        sessions.on_stop = later_message
        assert await session_events.handle_stopped(orch, stop_event()) == "soft"
        await _drain_tasks(orch)
        assert started == ["theirs", "mine, after the press"]

    @pytest.mark.asyncio
    async def test_a_failing_release_on_a_declined_stop_leaves_the_compacting_turn(
        self, monkeypatch: pytest.MonkeyPatch, _env: SelRecorder
    ) -> None:
        _env.expect_release_failure = True
        sessions = FakeSessions(live={"slack:1700.1"}, compacting={"slack:1700.1"})
        orch = Orch(sessions)
        await _working(orch)
        monkeypatch.setattr(session_events, "compaction_in_flight", lambda s, k: False)
        turn = asyncio.ensure_future(asyncio.Event().wait())
        orch._session_tasks["1700.1"] = turn

        async def boom(*_a: Any) -> None:
            raise RuntimeError("drain failed")

        monkeypatch.setattr(events, "_drain_slack_queue", boom)
        try:
            outcome = await session_events.handle_stopped(orch, stop_event())
            # A requested cancel shows at once, before the task has run it.
            alive = orch._session_tasks.get("1700.1") is turn and not turn.cancelling()
        finally:
            turn.cancel()
        assert outcome == "compacting"
        assert alive
        assert _env.release_failures

    @pytest.mark.asyncio
    async def test_a_forced_reset_drops_nobodys_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Like ``!stop``'s forced repeat: aimed at the compaction, not the queue."""
        sessions = FakeSessions(live={"slack:1700.1"}, compacting={"slack:1700.1"}, outcome="hard")
        orch = Orch(sessions)
        await _working(orch)
        started = _record_dispatch(monkeypatch, orch)
        monkeypatch.setattr(session_events, "consume_stop_declined", lambda k, u: True)
        orch._pending_queue["1700.1"] = [_pending("mine", "U1")]
        assert await session_events.handle_stopped(orch, stop_event()) == "hard"
        await _drain_tasks(orch)
        assert started == ["mine"]

    @pytest.mark.asyncio
    async def test_an_escalated_reset_drops_what_was_queued_for_everybody(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``!stop``'s rule: a stop that escalated to a reset takes the queue with it."""
        sessions = FakeSessions(live={"slack:1700.1"}, outcome="hard")
        orch = Orch(sessions)
        await _working(orch)
        started = _record_dispatch(monkeypatch, orch)
        theirs = _pending("theirs", "UOWNER")
        sessions.queue = [theirs]
        orch._pending_queue["1700.1"] = [_pending("theirs-pending", "UOWNER")]
        assert await session_events.handle_stopped(orch, stop_event()) == "hard"
        await _drain_tasks(orch)
        assert started == []
        assert theirs in sessions.cleared


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["soft", "hard"])
async def test_a_stop_drops_the_pressers_entry_an_earlier_stop_detached(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Another member's ``!stop``, pressed first and still in flight, detached U1's
    message. U1's native Stop drops it whether it ends soft or escalates to a reset:
    the stronger outcome never keeps more of the presser's work."""
    from kiro_crew.slack import events_queue

    sessions = FakeSessions(live={"slack:1700.1"}, outcome=outcome)
    orch = Orch(sessions)
    await _working(orch)
    started = _record_dispatch(monkeypatch, orch)
    mine = _pending("U1's, queued before U1's Stop", "U1")
    hold = events_queue._stops_in_flight.setdefault(orch, {}).setdefault(
        "1700.1", events_queue._StopHold()
    )
    hold.count += 1
    hold.held.append(([mine], []))
    hold.dropping[events_queue._entry_owner("UOWNER", "C1")] = 0
    assert await session_events.handle_stopped(orch, stop_event()) == outcome
    # The !stop releases last.
    hold.count -= 1
    events_queue._stops_in_flight[orch].pop("1700.1", None)
    await events_queue._settle_stop_hold(orch, "1700.1", hold)
    await events_queue._drain_slack_queue(orch, "1700.1")
    await _drain_tasks(orch)
    assert mine not in sessions.restored
    assert started == []


class TestLinkedThreadOfAFlatDm:
    """A dashboard-linked thread of a single-session DM: the router runs and queues
    it under the DM's flat key, so its Stop holds that key, scoped to the thread."""

    @staticmethod
    def _orch() -> tuple[FakeSessions, Orch]:
        sessions = FakeSessions(
            live={"slack:D1", "dashboard:abc"}, owners={"1700.9": "dashboard:abc"}
        )
        return sessions, Orch(sessions, flat=True)

    @staticmethod
    def _in(thread: str | None, text: str, user: str) -> Any:
        entry = _pending(text, user, channel="D1")
        entry[2]["thread_ts"] = thread
        return entry

    @pytest.mark.asyncio
    async def test_its_stop_holds_the_flat_key_and_cancels_its_own_router_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack import events_queue

        sessions, orch = self._orch()
        await _working(orch, channel="D1", thread="1700.9")
        started = _record_dispatch(monkeypatch, orch)
        linked_turn = asyncio.ensure_future(asyncio.Event().wait())
        events_queue._track_router_task(orch, "slack:D1", linked_turn, "1700.9")
        mine = self._in("1700.9", "mine, in the linked thread", "U1")
        orch._pending_queue["slack:D1"] = [mine]
        busy: list[bool] = []

        async def on_stop() -> None:
            busy.append(events_queue._key_busy(orch, "slack:D1"))

        sessions.on_stop = on_stop
        try:
            outcome = await session_events.handle_stopped(
                orch, stop_event(channel="D1", thread_ts="1700.9")
            )
            await asyncio.sleep(0)
            cancelled = linked_turn.cancelled()
        finally:
            linked_turn.cancel()
        await _drain_tasks(orch)
        assert outcome == "soft"
        assert busy == [True]
        assert cancelled
        assert started == []
        assert not orch._pending_queue.get("slack:D1")

    @pytest.mark.asyncio
    async def test_its_stop_keeps_the_dms_other_threads_and_other_senders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions, orch = self._orch()
        await _working(orch, channel="D1", thread="1700.9")
        started = _record_dispatch(monkeypatch, orch)
        orch._pending_queue["slack:D1"] = [
            self._in("1700.9", "mine, linked thread", "U1"),
            self._in("1700.2", "mine, another thread", "U1"),
            self._in("1700.9", "someone else's, linked thread", "U2"),
        ]
        assert (
            await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.9"))
            == "soft"
        )
        await _drain_tasks(orch)
        remaining = [e[1] for e in orch._pending_queue.get("slack:D1", [])]
        assert sorted(started + remaining) == [
            "mine, another thread",
            "someone else's, linked thread",
        ]

    @pytest.mark.asyncio
    async def test_a_reset_drops_only_the_linked_threads_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions, orch = self._orch()
        sessions.outcome = "hard"
        await _working(orch, channel="D1", thread="1700.9")
        started = _record_dispatch(monkeypatch, orch)
        orch._pending_queue["slack:D1"] = [
            self._in("1700.9", "someone else's, linked thread", "U2"),
            self._in("1700.2", "someone else's, another thread", "U2"),
        ]
        assert (
            await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.9"))
            == "hard"
        )
        await _drain_tasks(orch)
        remaining = [e[1] for e in orch._pending_queue.get("slack:D1", [])]
        assert started + remaining == ["someone else's, another thread"]

    @pytest.mark.asyncio
    async def test_its_stop_spares_a_router_task_started_for_another_thread(self) -> None:
        sessions, orch = self._orch()
        await _working(orch, channel="D1", thread="1700.9")
        from kiro_crew.slack import events_queue

        other_turn = asyncio.ensure_future(asyncio.Event().wait())
        events_queue._track_router_task(orch, "slack:D1", other_turn, "1700.2")
        try:
            await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.9"))
            await asyncio.sleep(0)
            alive = not other_turn.done() and orch._session_tasks.get("slack:D1") is other_turn
        finally:
            other_turn.cancel()
        assert alive

    @pytest.mark.asyncio
    async def test_its_stop_scopes_the_session_queue_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions, orch = self._orch()
        await _working(orch, channel="D1", thread="1700.9")
        # The release drains what it restored: stand in for that dispatch, and run it
        # to the end here, so no real one outlives the test on this fake orchestrator.
        started = _record_dispatch(monkeypatch, orch)
        mine_here = self._in("1700.9", "mine, linked thread", "U1")
        mine_there = self._in("1700.2", "mine, another thread", "U1")
        sessions.queue = [mine_here, mine_there]
        await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.9"))
        await _drain_tasks(orch)
        assert mine_here in sessions.cleared and mine_here not in sessions.restored
        assert mine_there in sessions.restored and mine_there not in sessions.cleared
        assert mine_here[1] not in started

    @pytest.mark.asyncio
    async def test_the_dms_own_stop_leaves_its_linked_threads_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse direction: a Stop in an unlinked DM thread is the flat DM's,
        and a linked thread is another session it does not address."""
        from kiro_crew.slack import events_queue

        sessions, orch = self._orch()
        await _working(orch, channel="D1", thread="1700.2")
        started = _record_dispatch(monkeypatch, orch)
        linked_turn = asyncio.ensure_future(asyncio.Event().wait())
        events_queue._track_router_task(orch, "slack:D1", linked_turn, "1700.9")
        orch._pending_queue["slack:D1"] = [
            self._in("1700.9", "mine, linked thread", "U1"),
            self._in("1700.2", "mine, this thread", "U1"),
        ]
        linked_scope = fence.enter_turn("D1", "1700.9")
        try:
            assert (
                await session_events.handle_stopped(
                    orch, stop_event(channel="D1", thread_ts="1700.2")
                )
                == "soft"
            )
            await asyncio.sleep(0)
            cancelled = linked_turn.cancelled()
            assert await orch.slack.post_message("D1", "still the linked turn", "1700.9") != ""
        finally:
            fence.exit_turn(linked_scope)
            linked_turn.cancel()
        await _drain_tasks(orch)
        assert not cancelled
        assert sorted(started + [e[1] for e in orch._pending_queue.get("slack:D1", [])]) == [
            "mine, linked thread"
        ]

    @pytest.mark.asyncio
    async def test_the_dms_own_reset_keeps_linked_threads_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions, orch = self._orch()
        sessions.outcome = "hard"
        await _working(orch, channel="D1", thread="1700.2")
        started = _record_dispatch(monkeypatch, orch)
        orch._pending_queue["slack:D1"] = [
            self._in("1700.9", "U2 linked", "U2"),
            self._in("1700.2", "U2 flat", "U2"),
            self._in("1700.9", "U1 linked", "U1"),
        ]
        assert (
            await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.2"))
            == "hard"
        )
        await _drain_tasks(orch)
        remaining = [e[1] for e in orch._pending_queue.get("slack:D1", [])]
        assert sorted(started + remaining) == ["U1 linked", "U2 linked"]

    @pytest.mark.asyncio
    async def test_the_dms_own_stop_keeps_linked_entries_in_the_session_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions, orch = self._orch()
        await _working(orch, channel="D1", thread="1700.2")
        started = _record_dispatch(monkeypatch, orch)
        linked = self._in("1700.9", "mine, linked thread", "U1")
        here = self._in("1700.2", "mine, this thread", "U1")
        sessions.queue = [linked, here]
        await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.2"))
        await _drain_tasks(orch)
        assert here in sessions.cleared and here not in sessions.restored
        assert linked in sessions.restored and linked not in sessions.cleared
        assert here[1] not in started

    @pytest.mark.asyncio
    async def test_a_binding_made_mid_stop_does_not_change_what_the_press_meant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions = FakeSessions(live={"slack:D1", "dashboard:abc"})
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.2")
        started = _record_dispatch(monkeypatch, orch)
        orch._pending_queue["slack:D1"] = [self._in("1700.3", "mine, the DM's at press", "U1")]

        async def link_it() -> None:
            sessions.owners["1700.3"] = "dashboard:abc"

        sessions.on_stop = link_it
        await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.2"))
        await _drain_tasks(orch)
        assert started + [e[1] for e in orch._pending_queue.get("slack:D1", [])] == []

    @pytest.mark.asyncio
    async def test_an_overlapping_dm_stop_judges_an_earlier_holds_entries_at_its_press(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A linked-thread Stop keeps the presser's message from an unlinked thread;
        the DM's own Stop, pressed while that hold is open, addresses it -- even if
        the thread is linked before the last hold releases."""
        sessions, orch = self._orch()
        await _working(orch, channel="D1", thread="1700.9")
        await _working(orch, channel="D1", thread="1700.2")
        started = _record_dispatch(monkeypatch, orch)
        orch._pending_queue["slack:D1"] = [self._in("1700.3", "mine, an unlinked thread", "U1")]
        presses = 0

        async def on_stop() -> None:
            nonlocal presses
            presses += 1
            if presses == 1:
                # The linked-thread Stop is in flight: the DM's own Stop overlaps it.
                await session_events.handle_stopped(
                    orch, stop_event(channel="D1", thread_ts="1700.2")
                )
            else:
                sessions.owners["1700.3"] = "dashboard:abc"

        sessions.on_stop = on_stop
        await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.9"))
        await _drain_tasks(orch)
        assert presses == 2
        assert started + [e[1] for e in orch._pending_queue.get("slack:D1", [])] == []

    @pytest.mark.asyncio
    async def test_a_thread_bound_to_the_dms_own_session_is_the_dm(self) -> None:
        from kiro_crew.slack import events_queue

        sessions = FakeSessions(live={"slack:D1"}, owners={"1700.5": "slack:D1"})
        orch = Orch(sessions, flat=True)
        await _working(orch, channel="D1", thread="1700.5")
        own_turn = asyncio.ensure_future(asyncio.Event().wait())
        events_queue._track_router_task(orch, "slack:D1", own_turn, "1700.5")
        scope = fence.enter_turn("D1", "1700.5")
        try:
            keys = session_events.resolve_session(orch, "D1", "1700.5")
            assert keys is not None and keys[2] is True
            await session_events.handle_stopped(orch, stop_event(channel="D1", thread_ts="1700.5"))
            await asyncio.sleep(0)
            cancelled = own_turn.cancelled()
            muted = fence.turn_muted("D1", "1700.5")
        finally:
            fence.exit_turn(scope)
            own_turn.cancel()
        assert cancelled
        assert muted


def test_every_router_task_registration_records_its_thread() -> None:
    """A router task registered without its thread cannot be told apart from another
    thread's, so a linked-thread Stop could not cancel it."""
    import inspect

    from kiro_crew.slack import events as events_facade
    from kiro_crew.slack import events_queue

    for module in (inspect.getmodule(events_facade._route_message), events_queue):
        source = inspect.getsource(module)
        body = source.replace(inspect.getsource(events_queue._track_router_task), "")
        assert "._session_tasks[session_key] = " not in body, module.__name__


@pytest.mark.asyncio
async def test_the_queue_drain_records_the_entrys_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack import events_queue

    sessions = FakeSessions(live={"slack:D1"})
    orch = Orch(sessions, flat=True)
    gate = asyncio.Event()

    async def dispatch(*_a: Any) -> None:
        await gate.wait()

    monkeypatch.setattr(events, "_dispatch_queued", dispatch)
    entry = _pending("queued in a thread", "U1", channel="D1")
    entry[2]["thread_ts"] = "1700.9"
    orch._pending_queue["slack:D1"] = [entry]
    await events_queue._drain_slack_queue(orch, "slack:D1")
    task = orch._session_tasks["slack:D1"]
    try:
        assert events_queue.router_task_thread(task) == "1700.9"
    finally:
        gate.set()
        await _drain_tasks(orch)


@pytest.mark.asyncio
async def test_a_raising_detach_does_not_leave_the_key_busy(_env: Any) -> None:
    """A detach that raises still releases the hold, or every later message queues."""
    from kiro_crew.slack import events_queue

    _env.expect_release_failure = True
    sessions = FakeSessions(live={"slack:1700.1"})

    def boom(_key: str) -> Any:
        raise RuntimeError("detach failed")

    sessions.detach_queue = boom  # type: ignore[method-assign]
    orch = Orch(sessions)
    await _working(orch)
    await session_events.handle_stopped(orch, stop_event())
    assert not events_queue._key_busy(orch, "1700.1")
