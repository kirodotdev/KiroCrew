"""A Slack-started turn shows on its dashboard tab while it runs.

A conversation started in a Slack thread has a dashboard tab bound to the same
session, but the turn runs on the Slack path: ``handle_message_transport`` drives a
``TurnDriver`` whose events reached only the Slack renderer. The tab got the
user's row at receipt and the reply at the end, and said "Thinking..." for
everything in between.

These pin the live view: a tool step reaches the tab as a transcript row and a
``tool_call`` frame (the frame that draws the sidebar's current-step line) while
the turn is still running; streamed text arrives as ``chat_chunk`` frames; the
final refresh lands the reply without a second copy of the step; Slack sees the
same calls with or without a tab; and a broken live view never costs the turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import itertools
import os
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STOP_REASON_END_TURN,
)
from kiro_crew.dashboard import channel_slots
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import transport_dispatch
from kiro_crew.testing.wait import async_wait_until

golden = importlib.import_module("test_slack_golden_transcript")

MSG_TS = "1700000000.000100"
SESSION_KEY = canonical_key(MSG_TS)
TCID = "tooluse_live_1"


class _PausingProvider(golden.ScriptedProvider):
    """Scripted provider that runs a probe after the tool call, mid-turn.

    The probe waits (bounded) for the step to reach the tab, then records what
    the tab held at that moment -- before the tool result, the closing text and
    the reply row exist.
    """

    def __init__(self, events: list[Any], probe: Any) -> None:
        super().__init__(events)
        self._probe = probe

    async def stream(self, message: str):
        self.stream_calls += 1
        events = self._events if self.stream_calls == 1 else []
        for ev in events:
            yield ev
            if ev.kind == EVENT_TOOL_CALL:
                await self._probe()


def _script() -> list[Any]:
    return [
        golden.make_event(EVENT_TEXT_CHUNK, text="Let me check the files. "),
        golden.make_event(
            EVENT_TOOL_CALL,
            tool_call_id=TCID,
            title="Running: ls -la src",
            tool_kind="execute",
            tool_purpose="List the source files",
            tool_input='{"command": "ls -la src"}',
            is_shell=True,
        ),
        golden.make_event(
            EVENT_TOOL_RESULT,
            tool_call_id=TCID,
            tool_output="main.py\nutil.py",
            tool_final=True,
            tool_status="completed",
        ),
        golden.make_event(EVENT_TEXT_CHUNK, text="There are two files."),
        golden.make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]


@pytest.fixture
def harness(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    state = _make_state(tmp_path)
    state.sessions.channel_key_for_stem = lambda stem: ""
    frames: list[tuple[str, dict]] = []
    notes: list[dict] = []
    state.broadcast_ws = lambda kind, data: frames.append((kind, dict(data)))
    state._broadcast = lambda note: notes.append(note)
    state.push_slots_update = lambda *a, **k: None

    async def refresh(session_key: str) -> None:
        # The production refresh, minus its config read: no dashboard, no pass.
        if transport_dispatch.get_dashboard_state() is None:
            return
        await channel_slots.reconcile_channel_slots(state, 0)

    monkeypatch.setattr(transport_dispatch, "_refresh_dashboard_tab", refresh)

    class H:
        pass

    h = H()
    h.state = state
    h.frames = frames
    h.notes = notes
    h.log = state.conversation_log
    return h


def _surface_tab(h: Any) -> Any:
    """The tab of an earlier exchange in this thread, as the reconciler built it."""
    h.log.append(SESSION_KEY, "user", "earlier ask")
    h.log.append(SESSION_KEY, "assistant", "earlier answer")
    slot = channel_slots.surface_channel_session(
        h.state,
        {"key": channel_slots.channel_slot_name(SESSION_KEY), "title": "thread", "modified": 0},
        {},
        h.log.read_messages(SESSION_KEY),
        session_key=SESSION_KEY,
    )
    assert slot is not None and slot.linked_session_key == SESSION_KEY
    return slot


def _run(h: Any, provider: Any, slack: Any, *, with_dashboard: bool = True) -> None:
    transport_dispatch.get_dashboard_state = (  # type: ignore[attr-defined]
        (lambda: h.state) if with_dashboard else (lambda: None)
    )
    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=golden.FakeSessions(provider),
            channel="C1",
            text="what files are there?",
            thread_ts=None,
            msg_ts=MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=h.log,
        )
    )


def _slack_lanes(rec: Any) -> tuple[list[Any], list[Any]]:
    """A recorded Slack transcript as two ordered lanes: reactions, and the rest.

    The status reactions are swapped by fire-and-forget tasks and a debounce timer
    (``StatusReactionController``), so where they fall between the stream calls is
    the event loop's scheduling, not the turn's. Two runs of one turn make the same
    calls in the same order within each lane.
    """
    reactions = [c for c in rec.transcript if c[0] in ("add_reaction", "remove_reaction")]
    rest = [c for c in rec.transcript if c[0] not in ("add_reaction", "remove_reaction")]
    return reactions, rest


def _tool_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("role") == "tool"]


def _chat_message_roles(notes: list[dict]) -> list[str]:
    out = []
    for note in notes:
        data = note.get("data", note) if isinstance(note, dict) else {}
        if isinstance(data, dict) and data.get("role"):
            out.append(data["role"])
    return out


@pytest.fixture(autouse=True)
def _restore_dashboard_getter() -> Any:
    original = transport_dispatch.get_dashboard_state
    yield
    transport_dispatch.get_dashboard_state = original


def test_the_tab_shows_the_tool_step_and_current_step_while_the_turn_runs(harness: Any) -> None:
    h = harness
    slot = _surface_tab(h)
    seen: dict[str, Any] = {}

    async def probe() -> None:
        await async_wait_until(lambda: _tool_rows(slot.messages), timeout=5)
        seen["window"] = [dict(r) for r in slot.messages]
        seen["frames"] = list(h.frames)
        seen["roles"] = _chat_message_roles(h.notes)
        seen["disk_assistant"] = [
            r for r in h.log.read_messages(SESSION_KEY) if r["content"] == "There are two files."
        ]

    _run(h, _PausingProvider(_script(), probe), golden.RecordingSlackClient())

    # Mid-turn: the step is a row in the tab's window, broadcast as a chat
    # message, before the reply exists anywhere.
    assert not seen["disk_assistant"], "probe ran after the reply; it proves nothing"
    tools = _tool_rows(seen["window"])
    assert [r["content"] for r in tools] == ["🔧 Running: ls -la src"]
    assert tools[0]["cls"] == "msg msg-tool"
    assert tools[0]["meta"]["tool_call_id"] == TCID
    assert tools[0]["meta"]["purpose"] == "List the source files"
    assert "tool" in seen["roles"]
    # The current-step line: the same tool_call frame a dashboard turn sends.
    calls = [d for k, d in seen["frames"] if k == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["slot"] == slot.key
    assert calls[0]["purpose"] == "List the source files"
    assert calls[0]["tool_call_id"] == TCID
    assert calls[0]["is_shell"] is True
    # The text before the call streamed instead of "Thinking...".
    chunks = [d for k, d in seen["frames"] if k == "chat_chunk"]
    assert chunks and all(d["slot"] == slot.key for d in chunks)
    assert "".join(d["content"] for d in chunks).startswith("Let me check")


def test_the_final_refresh_lands_the_reply_without_a_second_step(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    slot = _surface_tab(h)

    # A real turn outlasts the transcript's mtime tick; an instant scripted one
    # can write the user's row and the reply inside one tick, which reads to the
    # reconcile pass as "nothing newer" whatever this change does.
    # So every transcript write is given its own mtime, one second after the
    # last, the way writes a real turn's length apart land in distinct ticks.
    real_append = h.log.append
    stamps = itertools.count(int(time.time()) - 600)

    def stamped_append(key: str, *a: Any, **k: Any) -> Any:
        out = real_append(key, *a, **k)
        stamp = next(stamps)
        os.utime(h.log._path(key), (stamp, stamp))
        return out

    monkeypatch.setattr(h.log, "append", stamped_append)

    async def no_pause() -> None:
        return None

    _run(h, _PausingProvider(_script(), no_pause), golden.RecordingSlackClient())

    turn = [(r["role"], r["content"]) for r in slot.messages[2:]]
    assert turn == [
        ("user", "what files are there?"),
        ("tool", "🔧 Running: ls -la src"),
        ("assistant", "Let me check the files. There are two files."),
    ]
    disk = [(r["role"], r["content"]) for r in h.log.read_messages(SESSION_KEY)[2:]]
    assert disk == turn
    # The step was broadcast once, by the refresh that first pulled it in.
    assert _chat_message_roles(h.notes).count("tool") == 1
    results = [d for k, d in h.frames if k == "tool_result"]
    assert [(d["tool_call_id"], d["final"], d["output"]) for d in results] == [
        (TCID, True, "main.py\nutil.py")
    ]
    # Every streamed chunk advanced the slot's own counter.
    seqs = [d["seq"] for k, d in h.frames if k == "chat_chunk"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[-1] == slot._chunk_seq


def test_the_next_sessions_provenance_block_cites_no_tool_step(harness: Any) -> None:
    """A step row carries the turn's provenance like the user's and the reply's.
    The few rows a new session cites as "## Recent Session Context" are what was
    said, so a turn's steps never push its question out of them."""
    h = harness
    _surface_tab(h)

    _run(h, golden.ScriptedProvider(_script()), golden.RecordingSlackClient())

    steps = _tool_rows(h.log.read_messages(SESSION_KEY))
    assert steps and steps[0].get("source_thread"), "the step must carry provenance"
    cited = [p["snippet"] for p in h.log.recent_with_provenance(SESSION_KEY)]
    assert cited == ["what files are there?", "Let me check the files. There are two files."]
    assert not [c for c in cited if c.startswith("🔧")], cited


def test_a_step_pull_leaves_the_full_pass_its_watermark(harness: Any) -> None:
    """The one-session pull must not advance the mtime watermark: a reply that
    lands in the same mtime tick as the step would read as "nothing newer" to the
    pass that follows it, and the tab would never get the reply."""
    h = harness
    slot = _surface_tab(h)
    slot._channel_window_mtime = 123.0
    h.log.append(SESSION_KEY, "tool", "🔧 Running: ls", cls="msg msg-tool")

    appended = asyncio.run(channel_slots.refresh_linked_channel_slot(h.state, SESSION_KEY))

    assert appended == 1
    assert slot.messages[-1]["content"] == "🔧 Running: ls"
    assert slot._channel_window_mtime == 123.0


def test_slack_sees_the_same_calls_with_or_without_a_tab(harness: Any) -> None:
    h = harness
    without = golden.RecordingSlackClient()
    _run(h, golden.ScriptedProvider(_script()), without, with_dashboard=False)

    _surface_tab(h)
    with_tab = golden.RecordingSlackClient()
    _run(h, golden.ScriptedProvider(_script()), with_tab)

    assert _slack_lanes(with_tab) == _slack_lanes(without)
    assert any(k == "tool_call" for k, _ in h.frames), "the live view never ran"


def test_no_tab_means_no_step_is_written(harness: Any) -> None:
    """Without an open tab the turn writes what it always wrote: no tool rows."""
    h = harness

    _run(h, golden.ScriptedProvider(_script()), golden.RecordingSlackClient())

    assert _tool_rows(h.log.read_messages(SESSION_KEY)) == []
    assert not [k for k, _ in h.frames if k in ("tool_call", "chat_chunk")]


def test_a_failing_live_view_never_breaks_the_slack_turn(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.channel_live import ChannelLiveSink

    h = harness
    baseline = golden.RecordingSlackClient()
    _run(h, golden.ScriptedProvider(_script()), baseline, with_dashboard=False)
    baseline_rows = [(r["role"], r["content"]) for r in h.log.read_messages(SESSION_KEY)]

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("dashboard exploded")

    monkeypatch.setattr(ChannelLiveSink, "on_text", boom)
    monkeypatch.setattr(ChannelLiveSink, "on_tool_step", boom)
    monkeypatch.setattr(ChannelLiveSink, "on_tool_result", boom)
    _surface_tab(h)
    broken = golden.RecordingSlackClient()
    _run(h, golden.ScriptedProvider(_script()), broken)

    assert _slack_lanes(broken) == _slack_lanes(baseline)
    rows = [(r["role"], r["content"]) for r in h.log.read_messages(SESSION_KEY)]
    # The second run's turn landed whole, after the tab's seed rows.
    assert rows[-2:] == baseline_rows[-2:]
    assert _tool_rows(h.log.read_messages(SESSION_KEY)) == []


def test_a_driver_event_tap_that_raises_does_not_change_the_turn() -> None:
    from kiro_crew.messaging.driver import TurnDriver
    from kiro_crew.messaging.renderer import Renderer

    class _Collect(Renderer):
        def __init__(self) -> None:
            super().__init__(None)  # type: ignore[arg-type]
            self.text = ""

        async def on_text_chunk(self, text: str) -> None:
            self.text += text

        async def on_thinking(self, text: str) -> None: ...

        async def on_tool_call(self, *a: Any, **k: Any) -> None: ...

        async def on_prompt_choice(self, *a: Any, **k: Any) -> None: ...

        async def on_compaction(self, pct: float) -> None: ...

        async def on_done(self, stop_reason: str = "") -> None: ...

    def tap(_event: Any) -> None:
        raise RuntimeError("observer broke")

    renderer = _Collect()
    driver = TurnDriver(golden.ScriptedProvider(_script()), renderer, event_tap=tap)
    out = asyncio.run(driver.run("hi"))

    assert out == "Let me check the files. There are two files."
    assert renderer.text == out


def test_no_live_view_when_the_users_row_did_not_land(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed receipt write means the reply path writes the question AND the
    answer after the turn; steps recorded during it would sit above the question."""
    h = harness
    _surface_tab(h)
    real_append = h.log.append
    calls = {"n": 0}

    def flaky_append(key: str, role: str, *a: Any, **k: Any) -> Any:
        if role == "user" and calls["n"] == 0:
            calls["n"] += 1
            raise TimeoutError("transcript lock busy")
        return real_append(key, role, *a, **k)

    monkeypatch.setattr(h.log, "append", flaky_append)

    _run(h, golden.ScriptedProvider(_script()), golden.RecordingSlackClient())

    rows = [(r["role"], r["content"]) for r in h.log.read_messages(SESSION_KEY)[2:]]
    assert rows == [
        ("user", "what files are there?"),
        ("assistant", "Let me check the files. There are two files."),
    ]
    assert not [k for k, _ in h.frames if k == "tool_call"]


def test_a_restricted_thread_gets_no_frames_or_rows(harness: Any) -> None:
    from kiro_crew.dashboard.channel_live import ChannelLiveSink
    from kiro_crew.messaging.renderer import TOOL_CALL, OutputEvent

    h = harness
    _surface_tab(h)
    restricted = {"on": False}
    sink = ChannelLiveSink(h.state, SESSION_KEY, h.log, allowed=lambda: not restricted["on"])
    restricted["on"] = True  # an ``!incognito`` landing mid-turn

    async def go() -> None:
        sink.on_text("secret plan")
        sink.on_tool_step(None, OutputEvent(kind=TOOL_CALL, tool_call_id="t", title="Running: ls"))
        await sink.settle()

    asyncio.run(go())

    assert h.frames == []
    assert _tool_rows(h.log.read_messages(SESSION_KEY)) == []


def test_a_tab_running_its_own_turn_keeps_its_chunk_counter(harness: Any) -> None:
    """A dashboard turn sent from the tab (even one still waiting for the Slack
    turn's session lease) copied the slot's chunk counter at its start. The live
    view must not advance it under that turn, or the turn's first chunks reuse
    seqs the tab has already seen and are dropped."""
    from kiro_crew.dashboard.channel_live import ChannelLiveSink
    from kiro_crew.messaging.renderer import TOOL_CALL, OutputEvent

    h = harness
    slot = _surface_tab(h)
    # A turn task that has not finished: the tab's own turn owns the slot.
    slot.task = SimpleNamespace(done=lambda: False)
    assert slot.turn_running
    seq = slot._chunk_seq
    sink = ChannelLiveSink(h.state, SESSION_KEY, h.log)

    async def go() -> None:
        sink.on_text("live text")
        sink.on_tool_step(None, OutputEvent(kind=TOOL_CALL, tool_call_id="t", title="Running: ls"))
        await sink.settle()

    asyncio.run(go())

    assert slot._chunk_seq == seq
    assert h.frames == []
    assert _tool_rows(h.log.read_messages(SESSION_KEY)) == []


def test_a_tab_bound_to_another_session_is_not_this_turns_tab(harness: Any) -> None:
    h = harness
    slot = _surface_tab(h)
    slot.linked_session_key = "slack:1111111111.000001"

    assert channel_slots.linked_channel_slot(h.state, SESSION_KEY) is None


def test_a_step_still_queued_at_the_reply_is_dropped_never_written_below_it(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow transcript lock must not land a step under the reply: settle waits
    (bounded), then abandons what is left; a write already holding the lock
    finishes first, because the reply is written under the same lock."""
    import threading

    from kiro_crew.dashboard.channel_live import ChannelLiveSink
    from kiro_crew.messaging.renderer import TOOL_CALL, OutputEvent

    h = harness
    _surface_tab(h)
    real_append = h.log.append
    entered = threading.Event()
    release = threading.Event()

    def slow_append(key: str, role: str, content: str, *a: Any, **k: Any) -> Any:
        if content == "🔧 Running: first":
            entered.set()
            release.wait(5)
        return real_append(key, role, content, *a, **k)

    monkeypatch.setattr(h.log, "append", slow_append)
    sink = ChannelLiveSink(h.state, SESSION_KEY, h.log)

    reply_waiting = threading.Event()

    def write_reply() -> None:
        # Set just before the reply asks for the lock the first step holds.
        reply_waiting.set()
        with h.log.atomic_appends(SESSION_KEY):
            real_append(SESSION_KEY, "assistant", "the reply")

    async def go() -> None:
        sink.on_tool_step(
            None, OutputEvent(kind=TOOL_CALL, tool_call_id="a", title="Running: first")
        )
        sink.on_tool_step(
            None, OutputEvent(kind=TOOL_CALL, tool_call_id="b", title="Running: second")
        )
        try:
            # Bounded, and released on every exit: a write that failed before
            # it reached the lock times this out rather than hanging the run.
            await async_wait_until(entered.is_set, timeout=5)
            await sink.settle(timeout=0.1)
            reply = asyncio.ensure_future(asyncio.to_thread(write_reply))
            await async_wait_until(lambda: reply_waiting.is_set() or reply.done(), timeout=5)
        finally:
            release.set()
        await reply  # re-raises a failed reply write

    asyncio.run(go())

    tail = [(r["role"], r["content"]) for r in h.log.read_messages(SESSION_KEY)[2:]]
    assert tail == [("tool", "🔧 Running: first"), ("assistant", "the reply")]


def test_a_step_waiting_on_the_lock_when_abandoned_writes_nothing(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abandon is re-checked UNDER the transcript lock: a step write that was
    already waiting for the lock (another writer held it) when the reply's settle
    gave up must not land once it finally gets the lock."""
    import threading

    from kiro_crew.dashboard.channel_live import ChannelLiveSink
    from kiro_crew.messaging.renderer import TOOL_CALL, OutputEvent

    h = harness
    _surface_tab(h)
    held = threading.Event()
    release = threading.Event()

    # Counts every lock entry and exit, so the test waits on the step's write
    # asking for the lock (the second entry) and on that write being over (the
    # second exit), not on a fixed sleep.
    real_atomic = h.log.atomic_appends
    entries: list[str] = []
    exits: list[str] = []

    @contextlib.contextmanager
    def counted_atomic(key: str) -> Any:
        entries.append(key)
        try:
            with real_atomic(key):
                yield
        finally:
            exits.append(key)

    monkeypatch.setattr(h.log, "atomic_appends", counted_atomic)

    def other_writer() -> None:
        with h.log.atomic_appends(SESSION_KEY):
            held.set()
            release.wait(5)

    sink = ChannelLiveSink(h.state, SESSION_KEY, h.log)

    async def go() -> None:
        holder = asyncio.ensure_future(asyncio.to_thread(other_writer))
        try:
            # Bounded, and released on every exit; a writer that failed is
            # re-raised by the await below instead of being waited on.
            await async_wait_until(lambda: held.is_set() or holder.done(), timeout=5)
            if holder.done():
                holder.result()
            sink.on_tool_step(
                None, OutputEvent(kind=TOOL_CALL, tool_call_id="a", title="Running: ls")
            )
            # The step's thread is now asking for the lock the other writer holds.
            await async_wait_until(lambda: len(entries) >= 2, timeout=5)
            await sink.settle(timeout=0.1)
        finally:
            release.set()
        await holder
        # The step's write has had the lock and given it back.
        await async_wait_until(lambda: len(exits) >= 2, timeout=5)

    asyncio.run(go())

    assert _tool_rows(h.log.read_messages(SESSION_KEY)) == []


def test_the_live_view_settles_before_the_channel_renderer_sees_done() -> None:
    """A channel renderer may write the reply from its DONE (Slack's options
    footer persists the reply in ``on_done``), so the tee settles the live view's
    step writes BEFORE it hands DONE to the channel, and only then to the mirror."""
    from kiro_crew.messaging.dashboard_live import DashboardLiveRenderer, TeeRenderer
    from kiro_crew.messaging.renderer import DONE, TEXT_CHUNK, OutputEvent, SilentRenderer

    order: list[str] = []

    class _Primary(SilentRenderer):
        async def dispatch(self, event: OutputEvent) -> None:
            order.append(f"primary:{event.kind}")

    class _Sink:
        def on_text(self, text: str) -> None:
            order.append("mirror:text")

        def on_tool_step(self, raw: Any, event: OutputEvent) -> None:
            order.append("mirror:step")

        def on_tool_result(self, raw: Any) -> None:
            return None

        async def settle(self) -> None:
            order.append("settle")

        def abandon(self) -> None:
            order.append("abandon")

    tee = TeeRenderer(_Primary(), DashboardLiveRenderer(_Sink()))

    async def go() -> None:
        await tee.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi"))
        await tee.dispatch(OutputEvent(kind=DONE))

    asyncio.run(go())

    assert order[: order.index("settle")] == [f"primary:{TEXT_CHUNK}", "mirror:text"]
    assert order[order.index("settle") + 1] == f"primary:{DONE}"
