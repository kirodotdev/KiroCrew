"""The one status reaction a Slack turn wears on the message it answers.

No 🦞 "done" reaction anywhere on Slack: the reply is the done signal. Where
Slack shows the bot's native thread status (``assistant.threads.setStatus``
accepted), the message gets no reaction at all while the turn runs; elsewhere it
gets a single 👀 ack, removed when the reply lands. A failed turn still gets the
error reaction on either surface.

Both Slack turn paths are pinned: the native handler (``handle_message``) and the
transport renderer (``SlackRenderer``, the default path while
``messaging.use_transport`` is on).
"""

from __future__ import annotations

import asyncio

import pytest
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import _build_phase_emojis, handle_message, phase_emojis
from kiro_crew.slack.renderer import SlackRenderer

_ACK = "eyes"
_ERROR = "scream"


class _StatusSlack(MockSlackClient):
    """A mock whose ``set_thread_status`` answers like Slack would on this surface."""

    def __init__(self, native_status: bool) -> None:
        super().__init__()
        self._native_status = native_status

    async def set_thread_status(self, channel, thread_ts, status):
        await super().set_thread_status(channel, thread_ts, status)
        return self._native_status


class _RaisingProvider(FakeProvider):
    async def stream(self, message, timeout=120.0):
        raise RuntimeError("backend blew up")
        yield  # pragma: no cover - makes this an async generator


def _added(slack: MockSlackClient, ts: str) -> list[str]:
    return [a[1]["emoji"] for a in slack.actions if a[0] == "react" and a[1]["ts"] == ts]


def _left_on(slack: MockSlackClient, ts: str) -> set[str]:
    """The reactions still on message *ts* once every add/remove is replayed."""
    on: set[str] = set()
    for name, kw in slack.actions:
        if kw.get("ts") != ts:
            continue
        if name == "react":
            on.add(kw["emoji"])
        elif name == "unreact":
            on.discard(kw["emoji"])
    return on


async def _drain() -> None:
    # The controller fires its Slack calls as tasks; let them all land.
    for _ in range(20):
        await asyncio.sleep(0)


# (channel, thread_ts): a DM, a channel thread reply, a top-level channel message.
_SURFACES = [
    pytest.param("D1", None, id="dm"),
    pytest.param("C1", "500.000", id="channel-thread"),
    pytest.param("C1", None, id="channel"),
]


class TestNativeHandler:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel,thread_ts", _SURFACES)
    async def test_no_lobster_and_ack_removed_when_the_reply_lands(self, channel, thread_ts):
        slack = _StatusSlack(native_status=False)
        await handle_message(slack, FakeSessionManager(), channel, "hi", thread_ts, "msg1", "U1")
        await _drain()
        assert "lobster" not in _added(slack, "msg1")
        assert _added(slack, "msg1") == [_ACK], "one ack, no per-phase trail"
        assert _left_on(slack, "msg1") == set(), "the reply is the done signal"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel,thread_ts", _SURFACES)
    async def test_no_reaction_where_native_status_shows(self, channel, thread_ts):
        slack = _StatusSlack(native_status=True)
        provider = FakeProvider(
            [
                LLMEvent(kind="thinking", text="hmm"),
                LLMEvent(kind="tool_call", title="Running: Bash", tool_kind="bash"),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        await handle_message(
            slack, FakeSessionManager(provider), channel, "hi", thread_ts, "msg1", "U1"
        )
        await _drain()
        assert _added(slack, "msg1") == []
        assert any(
            a[0] == "set_thread_status" and a[1]["status"] for a in slack.actions
        ), "the native status is the progress signal"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("native_status", [False, True])
    async def test_a_failed_turn_keeps_the_error_reaction(self, native_status):
        slack = _StatusSlack(native_status=native_status)
        await handle_message(
            slack, FakeSessionManager(_RaisingProvider()), "C1", "hi", None, "msg1", "U1"
        )
        await _drain()
        assert _left_on(slack, "msg1") == {_ERROR}


class TestTransportRenderer:
    @pytest.mark.asyncio
    async def test_no_lobster_and_ack_removed_on_done(self):
        slack = _StatusSlack(native_status=False)
        r = SlackRenderer(slack, "C1", "500.000", react_ts="msg1")
        await r.on_turn_start()
        await r.on_thinking("hmm")
        await r.on_tool_call("t1", "Running: Bash", tool_kind="bash")
        await r.on_text_chunk("answer")
        await r.on_done()
        await r.close()
        await _drain()
        assert _added(slack, "msg1") == [_ACK]
        assert _left_on(slack, "msg1") == set()

    @pytest.mark.asyncio
    async def test_no_reaction_where_native_status_shows(self):
        slack = _StatusSlack(native_status=True)
        r = SlackRenderer(slack, "D1", "500.000", react_ts="msg1")
        await r.on_turn_start()
        await r.on_tool_call("t1", "Running: WebFetch", tool_kind="webfetch")
        await r.on_text_chunk("answer")
        await r.on_done()
        await _drain()
        assert _added(slack, "msg1") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("native_status", [False, True])
    async def test_a_turn_that_never_finished_keeps_the_error_reaction(self, native_status):
        slack = _StatusSlack(native_status=native_status)
        r = SlackRenderer(slack, "C1", "500.000", react_ts="msg1")
        await r.on_turn_start()
        await r.close()  # the dispatcher's finally after the driver raised
        await _drain()
        assert _left_on(slack, "msg1") == {_ERROR}


class _RefusingPostSlack(_StatusSlack):
    """Every plain message post fails, so the answer never reaches the reader."""

    async def post_message(self, channel, text, thread_ts=None, **kw):
        raise RuntimeError("slack is down")

    async def update_message(self, channel, ts, text="", blocks=None, **kw):
        raise RuntimeError("slack is down")


class TestAReplyThatFailsToPost:
    """The ack comes down when the turn ends; a reply that then fails to post still
    owes the user the error reaction."""

    @pytest.mark.asyncio
    async def test_native_handler(self):
        slack = _RefusingPostSlack(native_status=False)
        await handle_message(slack, FakeSessionManager(), "C1", "hi", None, "msg1", "U1")
        await _drain()
        assert _left_on(slack, "msg1") == {_ERROR}

    @pytest.mark.asyncio
    async def test_transport_renderer(self):
        slack = _RefusingPostSlack(native_status=False)
        r = SlackRenderer(slack, "C1", None, react_ts="msg1")
        await r.on_turn_start()
        await r.on_text_chunk("answer")
        try:
            await r.on_done()
        except Exception:
            pass  # the dispatcher's finally runs close() either way
        assert not r.turn_finalized, "the answer never reached the reader"
        await r.close()
        await _drain()
        assert _left_on(slack, "msg1") == {_ERROR}


class TestController:
    @pytest.mark.asyncio
    async def test_a_reply_faster_than_the_ack_still_takes_the_ack_down(self):
        """The reply-time remove must not overtake a slow ack add."""
        from kiro_crew.slack.handler import StatusReactionController
        from kiro_crew.testing.wait import async_wait_until, until_parked

        gate = asyncio.Event()

        class _SlowAdd(MockSlackClient):
            async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
                await gate.wait()
                await super().add_reaction(channel, ts, emoji)

        slack = _SlowAdd()
        ctrl = StatusReactionController(slack, "C1", "msg1")
        ctrl.start(native_status=False)
        ctrl.finalize()
        # The ack add is in flight and parked when the reply-time remove is asked for.
        await until_parked(gate)
        gate.set()
        await async_wait_until(
            lambda: _added(slack, "msg1") == [_ACK] and _left_on(slack, "msg1") == set(),
            describe=lambda: (_added(slack, "msg1"), _left_on(slack, "msg1")),
        )

    @pytest.mark.asyncio
    async def test_disabled_adds_nothing_even_on_error(self):
        from kiro_crew.slack.handler import StatusReactionController

        slack = MockSlackClient()
        ctrl = StatusReactionController(slack, "C1", "msg1", enabled=False)
        ctrl.start(native_status=False)
        ctrl.finalize(error=True)
        await _drain()
        assert _added(slack, "msg1") == []


class TestPhaseTable:
    def test_the_table_carries_only_the_ack_and_the_error(self):
        assert set(phase_emojis()) == {"queued", "error"}
        assert "lobster" not in phase_emojis().values()

    def test_retired_trail_keys_load_without_an_unknown_warning(self):
        table, unknown = _build_phase_emojis(
            {"done": None, "thinking": "x", "coding": "y", "browsing": "z", "tool": "w"}
        )
        assert unknown == []
        assert set(table) == {"queued", "error"}

    def test_a_typo_is_still_reported(self):
        _, unknown = _build_phase_emojis({"qeued": "x"})
        assert unknown == ["qeued"]


class TestBangCommands:
    @pytest.mark.asyncio
    async def test_title_replies_instead_of_reacting(self, monkeypatch):
        import kiro_crew.slack.handler as h

        monkeypatch.setattr(h, "_owner_id", "U1")
        slack = MockSlackClient()
        await h._handle_slash_command(
            "!title Release notes",
            slack,
            FakeSessionManager(),
            "C1",
            "t1",
            "msg1",
            "t1",
            "U1",
            None,
        )
        await _drain()
        assert _added(slack, "msg1") == []
        posts = [a[1].get("text") or "" for a in slack.actions if a[0] == "post"]
        assert any("Release notes" in t for t in posts), posts
