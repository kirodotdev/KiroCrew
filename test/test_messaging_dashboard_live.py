"""``messaging.dashboard_live``: the live view's fencing and the tee's delegation.

The end-to-end behaviour (a Slack turn shown live on its dashboard tab) is in
``test_slack_dashboard_live.py``; this pins the two renderers' own contracts with
fakes: the live view never raises and switches itself off on its first failure,
remembers a bounded number of announced calls, and the tee hands every per-kind
handler to the channel renderer unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from kiro_crew.agent_sdk.drivers.acp_vocab import EVENT_TOOL_CALL, EVENT_TOOL_RESULT
from kiro_crew.messaging import dashboard_live
from kiro_crew.messaging.dashboard_live import DashboardLiveRenderer, TeeRenderer
from kiro_crew.messaging.renderer import TEXT_CHUNK, TOOL_CALL, OutputEvent, SilentRenderer


class _Sink:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[tuple[str, Any]] = []

    def _record(self, name: str, value: Any = None) -> None:
        self.calls.append((name, value))
        if name in self.fail:
            raise RuntimeError(name)

    def on_text(self, text: str) -> None:
        self._record("text", text)

    def on_tool_step(self, raw: Any, event: OutputEvent) -> None:
        self._record("step", raw)

    def on_tool_result(self, raw: Any) -> None:
        self._record("result", raw)

    async def settle(self) -> None:
        self._record("settle")

    def abandon(self) -> None:
        self._record("abandon")


def _call(tcid: str) -> Any:
    return SimpleNamespace(kind=EVENT_TOOL_CALL, tool_call_id=tcid)


def test_a_tool_step_gets_the_raw_call_it_was_announced_with() -> None:
    sink = _Sink()
    live = DashboardLiveRenderer(sink)
    raw = _call("a")
    live.observe(raw)
    asyncio.run(live.dispatch(OutputEvent(kind=TOOL_CALL, tool_call_id="a")))
    asyncio.run(live.dispatch(OutputEvent(kind=TOOL_CALL, tool_call_id="b")))
    assert sink.calls == [("step", raw), ("step", None)]


def test_a_result_goes_to_the_sink_and_other_kinds_are_ignored() -> None:
    sink = _Sink()
    live = DashboardLiveRenderer(sink)
    result = SimpleNamespace(kind=EVENT_TOOL_RESULT, tool_call_id="a")
    live.observe(result)
    live.observe(SimpleNamespace(kind="something_else"))
    asyncio.run(live.dispatch(OutputEvent(kind=TEXT_CHUNK, text="")))
    assert sink.calls == [("result", result)]


def test_announced_calls_are_bounded_oldest_first(monkeypatch: Any) -> None:
    monkeypatch.setattr(dashboard_live, "_MAX_PENDING_CALLS", 2)
    sink = _Sink()
    live = DashboardLiveRenderer(sink)
    first, second, third = _call("1"), _call("2"), _call("3")
    for raw in (first, second, third):
        live.observe(raw)
    for tcid in ("1", "2", "3"):
        asyncio.run(live.dispatch(OutputEvent(kind=TOOL_CALL, tool_call_id=tcid)))
    assert sink.calls == [("step", None), ("step", second), ("step", third)]


def test_the_first_failure_switches_the_live_view_off(caplog: Any) -> None:
    sink = _Sink(fail={"result"})
    live = DashboardLiveRenderer(sink)
    live.observe(SimpleNamespace(kind=EVENT_TOOL_RESULT, tool_call_id="a"))
    assert live.disabled
    # Nothing more reaches the sink, and the warning is logged once.
    live.observe(SimpleNamespace(kind=EVENT_TOOL_RESULT, tool_call_id="b"))
    asyncio.run(live.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi")))
    assert [name for name, _ in sink.calls] == ["result"]
    assert sum("dashboard live view" in r.getMessage() for r in caplog.records) == 1


def test_a_failing_dispatch_switches_the_live_view_off() -> None:
    sink = _Sink(fail={"text"})
    live = DashboardLiveRenderer(sink)
    asyncio.run(live.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi")))
    assert live.disabled


def test_a_failing_settle_abandons_and_never_raises() -> None:
    sink = _Sink(fail={"settle"})
    live = DashboardLiveRenderer(sink)
    asyncio.run(live.settle())
    assert live.disabled
    assert [name for name, _ in sink.calls] == ["settle", "abandon"]


def test_a_failing_abandon_never_raises() -> None:
    sink = _Sink(fail={"abandon"})
    live = DashboardLiveRenderer(sink)
    live.abandon()
    assert live.disabled


def test_the_live_view_renders_nothing_through_its_handlers() -> None:
    live = DashboardLiveRenderer(_Sink())

    async def go() -> list[Any]:
        return [
            await live.on_text_chunk("x"),
            await live.on_thinking("x"),
            await live.on_tool_call("a", "t"),
            await live.on_prompt_choice([], 1),
            await live.on_compaction(50.0),
            await live.on_done(),
        ]

    assert asyncio.run(go()) == [None] * 6


def test_the_tee_hands_every_handler_to_the_channel_renderer() -> None:
    seen: list[tuple[str, tuple[Any, ...]]] = []

    class _Primary(SilentRenderer):
        async def on_turn_start(self) -> None:
            seen.append(("turn_start", ()))

        async def on_text_chunk(self, text: str) -> None:
            seen.append(("text", (text,)))

        async def on_thinking(self, text: str) -> None:
            seen.append(("thinking", (text,)))

        async def on_tool_call(self, *args: Any) -> None:
            seen.append(("tool_call", args))

        async def on_prompt_choice(self, *args: Any) -> None:
            seen.append(("prompt_choice", args))

        async def on_compaction(self, pct: float) -> None:
            seen.append(("compaction", (pct,)))

        async def on_done(self, stop_reason: str = "") -> None:
            seen.append(("done", (stop_reason,)))

        async def on_steer_consumed(self, summary: str = "") -> None:
            seen.append(("steer", (summary,)))

    sink = _Sink()
    tee = TeeRenderer(_Primary(channel_type="slack"), DashboardLiveRenderer(sink))
    assert tee.channel_type == "slack"

    async def go() -> None:
        await tee.on_turn_start()
        await tee.on_text_chunk("hi")
        await tee.on_thinking("hmm")
        await tee.on_tool_call("a", "Run", "execute", "Why")
        await tee.on_prompt_choice([{"id": 1}], 7, "t", "p", "i")
        await tee.on_compaction(80.0)
        await tee.on_done("end_turn")
        await tee.on_steer_consumed("s")

    asyncio.run(go())
    assert seen == [
        ("turn_start", ()),
        ("text", ("hi",)),
        ("thinking", ("hmm",)),
        ("tool_call", ("a", "Run", "execute", "Why")),
        ("prompt_choice", ([{"id": 1}], 7, "t", "p", "i")),
        ("compaction", (80.0,)),
        ("done", ("end_turn",)),
        ("steer", ("s",)),
    ]
    # The handlers are the channel's alone: the live view saw none of them.
    assert sink.calls == []
