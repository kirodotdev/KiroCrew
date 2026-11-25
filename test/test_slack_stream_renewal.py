"""A Slack stream is renewed before Slack's 10-minute limit closes it.

Slack ends a stream 10 minutes after ``chat.startStream`` and marks every task
card still running as an error, which reads as a failed step on a turn that is
still going. Both streaming paths (the transport renderer and the native
handler's ``_AnswerStream``) move to a fresh stream at the first append after
``STREAM_RENEW_AFTER_S``: the running step is closed on the old message with a
"continues below" title and opened again on the new one.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from redaction_structured_cases import github_classic
from slack_handler_helpers import FakeProvider, FakeSessionManager, hide_slack_timing_footer

from conftest import MockSlackClient
from kiro_crew.acp.types import EVENT_TOOL_CALL
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler as _slack_handler  # wires stream globals
from kiro_crew.slack import renderer as _renderer_mod
from kiro_crew.slack import renderer_stream_renewal as _renewal_mod
from kiro_crew.slack.handler import handle_message
from kiro_crew.slack.handler_runtime.stream import _AnswerStream
from kiro_crew.slack.renderer import SlackRenderer
from kiro_crew.slack.stream_window import (
    SLACK_STREAM_LIFETIME_S,
    STREAM_RENEW_AFTER_S,
    stream_due_for_renewal,
)
from kiro_crew.testing.wait import await_until


class _RecSlack:
    """Records every stream call with the message it targets."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._n = 0

    async def start_stream(self, channel, thread_ts, **kw):
        self._n += 1
        ts = f"ts-{self._n}"
        self.calls.append(("start_stream", {"ts": ts, "initial_text": kw.get("initial_text")}))
        return ts

    async def append_stream(self, channel, ts, text):
        self.calls.append(("append_stream", {"ts": ts, "text": text}))
        return True

    async def stop_stream(self, channel, ts, final_text=None, blocks=None):
        self.calls.append(("stop_stream", {"ts": ts}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.calls.append(
            ("append_task", {"ts": ts, "id": task_id, "title": title, "status": status})
        )
        return True

    async def set_thread_status(self, channel, thread_ts, status):
        return True

    async def post_message(self, channel, text, thread_ts=None, **kw):
        return "posted"


def _named(rec: _RecSlack, name: str) -> list[dict]:
    return [kw for m, kw in rec.calls if m == name]


def test_renewal_comes_before_slacks_limit_with_room_for_one_card_refresh():
    assert STREAM_RENEW_AFTER_S + 30 < SLACK_STREAM_LIFETIME_S
    assert not stream_due_for_renewal(0.0, 10_000.0)
    assert not stream_due_for_renewal(1000.0, 1000.0 + STREAM_RENEW_AFTER_S - 1)
    assert stream_due_for_renewal(1000.0, 1000.0 + STREAM_RENEW_AFTER_S)


class TestRendererRenewal:
    def _renderer(self):
        rec = _RecSlack()
        return rec, SlackRenderer(rec, "C1", "t1", reactions_enabled=False)

    def test_close_cancels_inflight_renewal(self, clock):
        rec = _GatedSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)

        async def scenario():
            renderer._stream_ts = "ts-0"
            renderer._use_slack_stream = True
            renderer._active_task_id = "tool_1"
            renderer._task_state["tool_1"] = ("Bash", "in_progress")
            renderer._stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            refresh = asyncio.ensure_future(renderer._append_task("tool_1", "Bash", "in_progress"))
            await await_until(rec.starting, what="the renewal to start")
            renewal = renderer._renewal
            await renderer.close()
            rec.release.set()
            await await_until(asyncio.gather(refresh, return_exceptions=True), what="refresh exit")
            assert renewal is not None and renewal.cancelled()
            await renderer.close()
            assert not _named(rec, "start_stream"), rec.calls
            assert not [c for c in _named(rec, "append_task") if c["status"] == "in_progress"]

        asyncio.run(scenario())

    def test_old_stream_moves_the_running_step_to_a_new_message(self, clock):
        rec, renderer = self._renderer()

        async def scenario():
            await renderer._ensure_stream()
            renderer._active_task_id = "tool_1"
            renderer._active_task_title = "Bash"
            await renderer._append_task("tool_1", "Bash", "in_progress")
            renderer._stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            await renderer._append_task("tool_1", "Bash  ⏱ 9m 0s", "in_progress")

        asyncio.run(scenario())
        starts = _named(rec, "start_stream")
        assert [s["ts"] for s in starts] == ["ts-1", "ts-2"], rec.calls
        assert "continued" in (starts[1]["initial_text"] or ""), starts
        assert _named(rec, "stop_stream") == [{"ts": "ts-1"}], rec.calls
        cards = _named(rec, "append_task")
        closed = [c for c in cards if c["ts"] == "ts-1" and c["status"] == "complete"]
        assert closed and closed[0]["title"].endswith("continues below"), cards
        # The refresh itself lands on the new message, after the step reopens there.
        assert cards[-1] == {
            "ts": "ts-2",
            "id": "tool_1",
            "title": "Bash  ⏱ 9m 0s",
            "status": "in_progress",
        }, cards
        assert renderer._stream_ts == "ts-2"

    @pytest.mark.parametrize("status", ["complete", "error"])
    def test_renewal_does_not_carry_a_finished_step(self, clock, status):
        rec, renderer = self._renderer()

        async def scenario():
            await renderer._ensure_stream()
            renderer._active_task_id = "tool_1"
            renderer._task_state["tool_1"] = ("Bash", status)
            renderer._narration_pending = ("tool_1", "Bash", "ts-1")
            renderer._stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            await renderer._renew_stream_if_due()

        asyncio.run(scenario())
        assert renderer._stream_ts == "ts-2", rec.calls
        assert not _named(rec, "append_task"), rec.calls
        assert renderer._narration_pending == ("tool_1", "Bash", "ts-1")

    def test_new_step_on_old_stream_is_drawn_once_with_a_display_safe_title(self, clock):
        rec = _RecSlack()
        renderer = SlackRenderer(
            rec, "C1", "t1", reactions_enabled=False, show_thinking=False, now=clock
        )
        secret = github_classic(seed=43)
        raw = f"{secret[:4]}**{secret[4:]}**"
        purpose = f"Use {raw}"
        safe_title = _renderer_mod._display_safe(purpose)
        assert _renderer_mod._redact_all(purpose) == purpose
        assert safe_title != purpose and "[REDACTED: credential]" in safe_title

        async def scenario():
            try:
                await renderer._ensure_stream()
                clock.now += STREAM_RENEW_AFTER_S + 1
                await renderer.on_tool_call("a", "Running: Bash", tool_purpose=purpose)
            finally:
                await renderer.close()

        asyncio.run(scenario())
        cards = _named(rec, "append_task")
        assert all(raw not in c["title"] and secret not in c["title"] for c in cards), cards
        # This step never appeared on the old message, so it has nothing to close.
        assert not [c for c in cards if c["ts"] == "ts-1"], cards
        assert cards == [
            {"ts": "ts-2", "id": "tool_1", "title": safe_title, "status": "in_progress"}
        ], cards
        assert _named(rec, "stop_stream") == [{"ts": "ts-1"}], rec.calls

    def test_new_steps_held_narration_follows_the_renewal_before_it_is_drawn(self, clock):
        rec = _DetailsSlack()
        renderer = SlackRenderer(
            rec, "C1", "t1", reactions_enabled=False, show_thinking=False, now=clock
        )

        async def scenario():
            try:
                await renderer._ensure_stream()
                await renderer.on_text_chunk("Let me check the ")
                renderer.tool_text_held = True
                clock.now += STREAM_RENEW_AFTER_S + 1
                await renderer.on_tool_call("a", "Running: ls")
                renderer.tool_text_held = False
                await renderer.on_text_continuation("backlog.")
            finally:
                await renderer.close()

        asyncio.run(scenario())
        assert renderer._stream_ts == "ts-2", rec.calls
        assert not [c for c in _named(rec, "append_task") if c["ts"] == "ts-1"], rec.calls
        details = [d for ts, task_id, d in rec.details if ts == "ts-2" and task_id == "tool_1"]
        assert any("Let me check the backlog." in d for d in details), rec.details

    def test_young_stream_is_left_alone(self, clock):
        rec, renderer = self._renderer()

        async def scenario():
            await renderer._ensure_stream()
            renderer._stream_started_at = clock.now - STREAM_RENEW_AFTER_S + 5
            await renderer._append_task("tool_1", "Bash", "in_progress")
            await renderer._append_stream("text")

        asyncio.run(scenario())
        assert len(_named(rec, "start_stream")) == 1, rec.calls
        assert not _named(rec, "stop_stream"), rec.calls

    def test_old_stream_is_renewed_before_text_is_appended(self, clock):
        rec, renderer = self._renderer()

        async def scenario():
            await renderer._ensure_stream()
            renderer._stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            assert await renderer._append_stream("still going")

        asyncio.run(scenario())
        assert _named(rec, "append_stream") == [{"ts": "ts-2", "text": "still going"}], rec.calls
        # No step was running, so none is closed or reopened.
        assert not _named(rec, "append_task"), rec.calls
        # Renewed once: the new stream is young.
        assert len(_named(rec, "start_stream")) == 2, rec.calls

    def test_a_renewed_step_starts_its_details_clean_on_the_new_message(self, clock):
        rec = _DetailsSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)

        async def scenario():
            await renderer._ensure_stream()
            renderer._active_task_id = "tool_1"
            renderer._active_task_title = "Bash"
            await renderer._append_task("tool_1", "Bash", "in_progress", details="first")
            renderer._stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            await renderer._append_task("tool_1", "Bash", "in_progress", details="second")

        asyncio.run(scenario())
        on_new = [d for ts, _, d in rec.details if ts == "ts-2" and d]
        assert on_new == ["second"], rec.details

    def test_a_steps_held_narration_and_outcome_land_on_the_renewed_message(self, clock):
        # The narration's end is held past the tool call; the stream is renewed
        # while the tool runs, and the tool fails, so its outcome waits for the
        # narration. Both still finish the step, on the new message.
        rec = _DetailsSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)

        async def scenario():
            await renderer.on_text_chunk("Let me check the ")
            renderer.tool_text_held = True
            await renderer.on_tool_call("a", "Running: ls")
            renderer.tool_text_held = False
            clock.now += STREAM_RENEW_AFTER_S
            await renderer._append_task("tool_1", "ls  ⏱ 9m 0s", "in_progress")
            await renderer.on_tool_result("a", "failed")
            await renderer.on_tool_call("b", "Running: pwd")
            await renderer.on_text_continuation("backlog.")

        asyncio.run(scenario())
        assert renderer._stream_ts == "ts-2", rec.calls
        on_new = [d for ts, task_id, d in rec.details if ts == "ts-2" and task_id == "tool_1"]
        assert any("Let me check the backlog." in d and "failed" in d for d in on_new), rec.details
        assert not renderer._deferred_outcomes


class _DetailsSlack(_RecSlack):
    """Also records the details each card was sent with."""

    def __init__(self) -> None:
        super().__init__()
        self.details: list[tuple[str, str, str]] = []

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.details.append((ts, task_id, details))
        return await super().append_task(channel, ts, task_id, title, status, details, output)


class TestNativeRenewal:
    def _stream(self, rec):
        stream = _AnswerStream(
            rec,
            "C1",
            "t1",
            team_id="",
            user_id="",
            channel_activation=None,
            show_thinking=False,
        )
        stream.stream_ts = "ts-0"
        stream.use_slack_stream = True
        return stream

    def test_abandon_cancels_timer_and_inflight_renewal(self, clock):
        rec = _GatedSlack()
        stream = self._stream(rec)
        stream.active_task_id = "tool_1"
        stream.active_task_title = "Bash"

        async def scenario():
            stream.stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            refresh = asyncio.ensure_future(stream.append_task("tool_1", "Bash", "in_progress"))
            stream.tool_timer_task = refresh
            await await_until(rec.starting, what="the renewal to start")
            renewal = stream.renewal
            await stream.abandon()
            rec.release.set()
            await await_until(asyncio.gather(refresh, return_exceptions=True), what="refresh exit")
            assert refresh.cancelled() and stream.tool_timer_task is None
            assert renewal is not None and renewal.cancelled()
            await stream.abandon()
            stream.renewal = None
            await stream.renew_if_due()
            assert not _named(rec, "start_stream"), rec.calls
            assert not [c for c in _named(rec, "append_task") if c["status"] == "in_progress"]

        asyncio.run(scenario())

    def test_old_stream_moves_the_running_step_to_a_new_message(self, clock):
        rec = _RecSlack()
        stream = self._stream(rec)
        stream.active_task_id = "tool_1"
        stream.active_task_title = "Bash"

        async def scenario():
            stream.stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            await stream.append_task("tool_1", "Bash  ⏱ 9m 0s", "in_progress")

        asyncio.run(scenario())

        assert _named(rec, "stop_stream") == [{"ts": "ts-0"}], rec.calls
        assert [s["ts"] for s in _named(rec, "start_stream")] == ["ts-1"], rec.calls
        cards = _named(rec, "append_task")
        assert cards[0]["ts"] == "ts-0" and cards[0]["status"] == "complete", cards
        assert cards[0]["title"] == "Bash · continues below", cards
        assert cards[1] == {"ts": "ts-1", "id": "tool_1", "title": "Bash", "status": "in_progress"}
        assert cards[-1]["ts"] == "ts-1" and cards[-1]["title"] == "Bash  ⏱ 9m 0s", cards
        assert stream.stream_ts == "ts-1"
        assert stream.rotated_ts == ["ts-0"]

    def test_young_stream_is_left_alone(self, clock):
        rec = _RecSlack()
        stream = self._stream(rec)

        async def scenario():
            stream.stream_started_at = clock.now - STREAM_RENEW_AFTER_S + 5
            await stream.append_task("tool_1", "Bash", "in_progress")

        asyncio.run(scenario())

        assert not _named(rec, "start_stream"), rec.calls
        assert not _named(rec, "stop_stream"), rec.calls

    @pytest.mark.parametrize("demoted", [False, True])
    def test_redaction_attempts_every_rotated_message_even_when_one_edit_fails(
        self, demoted, monkeypatch, caplog
    ):
        slack = MockSlackClient()
        stream = self._stream(slack)
        stream.rotated_ts = ["old-1", "old-2"]
        stream.streamed_at_finish = True
        stream.use_slack_stream = not demoted
        stream.had_redaction = True
        attempts = []
        update = slack.update_message

        async def fail_first_edit(channel, ts, text="", blocks=None):
            attempts.append(ts)
            if ts == "old-1":
                raise RuntimeError("edit refused")
            await update(channel, ts, text, blocks)

        monkeypatch.setattr(slack, "update_message", fail_first_edit)
        asyncio.run(stream.seal("[REDACTED: credential]", redacted=False))

        assert attempts == ["ts-0", "old-1", "old-2"]
        assert "Failed to redact rotated message old-1" in caplog.text
        assert stream.rotated_ts == ["old-1", "old-2"]
        assert [p["text"] for n, p in slack.actions if n == "update"] == [
            "[REDACTED: credential]",
            _slack_handler.CONTINUES_BELOW.strip(),
        ]

    @pytest.mark.parametrize("demoted", [False, True])
    @pytest.mark.parametrize("failure", ["primary", "overflow", "none"])
    def test_redaction_preserves_rotated_text_until_every_part_lands(
        self, demoted, failure, clock, monkeypatch
    ):
        slack = _TimedSlack(clock)
        stream = self._stream(slack)
        stream.rotated_ts = ["old-1", "old-2"]
        stream.streamed_at_finish = True
        stream.use_slack_stream = not demoted
        before = {"old-1": "First answer part", "old-2": "Second part", "ts-0": "Last part"}
        visible = dict(before)
        calls = []
        text = "safe answer " * (_slack_handler.SLACK_MSG_LIMIT // 12 + 20)
        parts = _slack_handler.split_message(text)
        assert len(parts) == 2

        async def update(channel, ts, text="", blocks=None):
            calls.append(("update", ts))
            if failure == "primary" and ts == "ts-0":
                raise RuntimeError("primary refused")
            visible[ts] = text

        async def post(channel, text, thread_ts=None, **kw):
            calls.append(("post", "overflow"))
            if failure == "overflow":
                raise RuntimeError("overflow refused")
            visible["overflow"] = text
            return "overflow"

        monkeypatch.setattr(slack, "update_message", update)
        monkeypatch.setattr(slack, "post_message", post)
        if demoted and failure == "primary":
            with pytest.raises(RuntimeError, match="primary refused"):
                asyncio.run(stream.seal(text, redacted=True))
        else:
            asyncio.run(stream.seal(text, redacted=True))

        if failure != "none":
            assert not any(ts in stream.rotated_ts for _, ts in calls)
            assert all(visible[ts] == before[ts] for ts in stream.rotated_ts)
            if failure == "primary":
                assert all(visible[ts] == value for ts, value in before.items())
        else:
            assert calls == [
                ("update", "ts-0"),
                ("post", "overflow"),
                ("update", "old-1"),
                ("update", "old-2"),
            ]
            assert visible["ts-0"] == parts[0]
            assert visible["overflow"] == parts[1]
            assert all(
                visible[ts] == _slack_handler.CONTINUES_BELOW.strip() for ts in stream.rotated_ts
            )

    def test_final_copy_owns_the_stream(self, clock):
        rec = _RecSlack()
        stream = self._stream(rec)
        stream.finishing = True

        async def scenario():
            stream.stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            await stream.renew_if_due()

        asyncio.run(scenario())

        assert not rec.calls

    def test_completion_crossing_renewal_age_does_not_reopen_previous_step(self, clock):
        class CompletionSlack(_RecSlack):
            async def append_task(self, channel, ts, task_id, title, status, **kw):
                result = await super().append_task(channel, ts, task_id, title, status, **kw)
                if task_id == "tool_1" and status == "complete":
                    clock.now += 2
                return result

        rec = CompletionSlack()
        stream = self._stream(rec)
        stream.active_task_id = "tool_1"
        stream.active_task_title = "Bash"
        stream.task_counter = 1

        async def scenario():
            stream.stream_started_at = clock.now - STREAM_RENEW_AFTER_S + 1
            try:
                await stream.on_tool_call(
                    LLMEvent(
                        kind=EVENT_TOOL_CALL, title="Running: ls", tool_name="ls", tool_call_id="b"
                    )
                )
            finally:
                stream.cancel_tool_timer()

        asyncio.run(scenario())
        assert stream.stream_ts == "ts-1", rec.calls
        cards = _named(rec, "append_task")
        assert [(c["ts"], c["id"], c["status"]) for c in cards] == [
            ("ts-0", "tool_1", "complete"),
            ("ts-1", "tool_2", "in_progress"),
        ], cards

    def test_a_step_that_starts_on_an_old_stream_is_drawn_only_on_the_new_message(self, clock):
        rec = _RecSlack()
        stream = self._stream(rec)

        async def scenario():
            stream.stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            await stream.on_tool_call(
                LLMEvent(
                    kind=EVENT_TOOL_CALL, title="Running: ls", tool_name="ls", tool_call_id="a"
                )
            )
            stream.cancel_tool_timer()

        asyncio.run(scenario())

        assert stream.stream_ts == "ts-1"
        # The old message never showed the step, so it gets no "continues below" card.
        assert _named(rec, "append_task") == [
            {"ts": "ts-1", "id": "tool_1", "title": "ls", "status": "in_progress"}
        ], rec.calls

    def test_a_caller_during_a_renewal_waits_for_it_even_when_the_starter_is_cancelled(self, clock):
        # The 30 s refresh usually starts the renewal and is cancelled when its tool
        # ends, which is also when the tool's closing card comes in.
        rec = _GatedSlack()
        stream = self._stream(rec)
        stream.active_task_id = "tool_1"
        stream.active_task_title = "Bash"

        async def scenario():
            stream.stream_started_at = clock.now - STREAM_RENEW_AFTER_S
            refresh = asyncio.ensure_future(stream.append_task("tool_1", "Bash", "in_progress"))
            await await_until(rec.starting, what="the renewal to start")
            closing = asyncio.ensure_future(stream.append_task("tool_1", "Bash", "complete"))
            await asyncio.sleep(0)
            refresh.cancel()
            rec.release.set()
            await await_until(closing, what="the closing card")
            assert refresh.cancelled()

        asyncio.run(scenario())

        # Renewed once: the old message is stopped and closed once, one new one opens.
        assert _named(rec, "stop_stream") == [{"ts": "ts-0"}], rec.calls
        assert [s["ts"] for s in _named(rec, "start_stream")] == ["ts-1"], rec.calls
        cards = _named(rec, "append_task")
        assert [c for c in cards if c["ts"] == "ts-0"] == [
            {"ts": "ts-0", "id": "tool_1", "title": "Bash · continues below", "status": "complete"}
        ], cards
        assert cards[-1] == {"ts": "ts-1", "id": "tool_1", "title": "Bash", "status": "complete"}
        assert stream.stream_ts == "ts-1"


class _GatedSlack(_RecSlack):
    """Holds ``start_stream`` until released, so a renewal can be caught under way."""

    def __init__(self) -> None:
        super().__init__()
        self.starting = asyncio.Event()
        self.release = asyncio.Event()

    async def start_stream(self, channel, thread_ts, **kw):
        self.starting.set()
        await self.release.wait()
        return await super().start_stream(channel, thread_ts, **kw)


# ── The whole answer survives a renewal or a timed-out stream ─────────────────
#
# Slack is modelled as it behaves: an append to a stream 600 s or more after its
# ``chat.startStream`` is refused. A float in a scripted turn moves the clock on by
# that many seconds. Each case asserts the text streamed across every message,
# joined in order, is the answer exactly once.

_RENEW = STREAM_RENEW_AFTER_S + 1  # past the renewal point, inside Slack's limit
_EXPIRE = SLACK_STREAM_LIFETIME_S + 1  # past Slack's limit: the stream is already gone


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    # The native path reads its names off the handler facade (``_facade_compose``).
    for module in (_slack_handler, _renderer_mod, _renewal_mod):
        monkeypatch.setattr(module, "stream_clock", c)
    return c


class _TimedSlack(MockSlackClient):
    """Refuses an append to a stream Slack would already have ended."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self._stream_enabled = True
        self._clock = clock
        self._opened: dict[str, float] = {}

    def _expired(self, ts: str) -> bool:
        return self._clock.now - self._opened.get(ts, self._clock.now) >= SLACK_STREAM_LIFETIME_S

    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None):
        ts = await super().start_stream(channel, thread_ts, initial_text, team_id, user_id)
        if ts:
            self._opened[ts] = self._clock.now
        return ts

    async def append_stream(self, channel, ts, text):
        if self._expired(ts):
            return False
        return await super().append_stream(channel, ts, text)

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        if self._expired(ts):
            return False
        return await super().append_task(channel, ts, task_id, title, status, details, output)

    def streamed(self) -> str:
        return "".join(p["text"] for name, p in self.actions if name == "append_stream")

    def started(self) -> list[str]:
        return [p["ts"] for name, p in self.actions if name == "start_stream"]

    def rewrites(self) -> list[str]:
        """Text sent outside the stream: a post or an edit."""
        return [p.get("text") or "" for name, p in self.actions if name in ("post", "update")]


class _ClockedProvider(FakeProvider):
    """A ``FakeProvider`` whose script may also move the clock on."""

    def __init__(self, script, clock: _Clock) -> None:
        super().__init__([])
        self._script = script
        self._clock = clock

    async def stream(self, message, timeout=120.0):
        for item in self._script:
            if isinstance(item, float):
                self._clock.now += item
                continue
            yield item
        yield LLMEvent(kind="complete")


def _say(text: str) -> LLMEvent:
    return LLMEvent(kind="text_chunk", text=text)


_WAIT = LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="w1")
_LONG = ["First, the plan. ", "Second, the work. ", "Third, the result."]

#: (script, the answer text the reader must end up with, stream messages opened).
#: At the end of a turn the final copy owns the stream, so a stream past the
#: renewal point but inside Slack's limit takes the last text as it is, and one
#: past the limit is rotated by the refused append.
_NATIVE_CASES = {
    "renewed-mid-answer": ([_say(_LONG[0]), _RENEW, _say(_LONG[1]), _say(_LONG[2])], _LONG, 2),
    "renewed-at-the-end": ([*map(_say, _LONG), _RENEW], _LONG, 1),
    "expired-mid-answer": ([_say(_LONG[0]), _EXPIRE, _say(_LONG[1]), _say(_LONG[2])], _LONG, 2),
    "expired-at-the-end": ([*map(_say, _LONG), _EXPIRE], _LONG, 2),
    "very-short-answer": ([_say("Hi!"), _RENEW], ["Hi!"], 1),
    "renewed-before-a-wait": (
        [_say("Waiting now."), _RENEW, _WAIT, _say(" Back again.")],
        ["Waiting now.", "Back again."],
        3,
    ),
    "renewed-after-a-wait": (
        [_say("Waiting now."), _WAIT, _say(" Back"), _RENEW, _say(" again.")],
        ["Waiting now.", "Back again."],
        3,
    ),
}


@pytest.mark.parametrize("case", list(_NATIVE_CASES))
def test_native_answer_text_is_kept_whole_across_a_renewal(case, clock, monkeypatch, tmp_path):
    hide_slack_timing_footer(monkeypatch, tmp_path)
    # Every chunk goes out as it comes, so a renewal can fall between two of them.
    monkeypatch.setattr(_slack_handler, "_EDIT_INTERVAL", 0.0)
    script, parts, messages = _NATIVE_CASES[case]
    slack = _TimedSlack(clock)
    sessions = FakeSessionManager(_ClockedProvider(script, clock))

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert len(slack.started()) == messages, slack.actions
    streamed = slack.streamed()
    # Nothing lost: every part is there, in order. Nothing doubled: each part once,
    # and no post or edit restates the answer the stream already shows.
    assert streamed.split() == " ".join(parts).split(), slack.actions
    for part in parts:
        assert streamed.count(part.strip()) == 1, slack.actions
        assert not any(part.strip() in text for text in slack.rewrites()), slack.actions


@pytest.mark.parametrize("renewal", [True, False], ids=["renewal", "refused-append"])
def test_final_redaction_rewrites_rotated_messages(renewal, clock, monkeypatch, tmp_path):
    hide_slack_timing_footer(monkeypatch, tmp_path)
    monkeypatch.setattr(_slack_handler, "_EDIT_INTERVAL", 0.0)
    secret = github_classic(seed=42)
    # The literal scan cannot join the emphasis; the final display scan can.
    # Both halves reach the first message before the stream is rotated away.
    disguised = secret[:12] + "**" + secret[12:] + "**"
    first = _say("The access key is " + disguised + ". ")
    script = [first, _RENEW if renewal else 0.0, _say("Done. All set.")]
    slack = _TimedSlack(clock)
    if not renewal:
        append = slack.append_stream

        async def refuse_second_append(channel, ts, text):
            if ts == slack.started()[0] and any(n == "append_stream" for n, _ in slack.actions):
                return False
            return await append(channel, ts, text)

        monkeypatch.setattr(slack, "append_stream", refuse_second_append)
    sessions = cast(
        _slack_handler.SessionManager, FakeSessionManager(_ClockedProvider(script, clock))
    )

    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert len(slack.started()) == 2, slack.actions
    old_ts, last_ts = slack.started()
    old_appends = "".join(
        p["text"] for name, p in slack.actions if name == "append_stream" and p["ts"] == old_ts
    )
    assert secret[4:12] in old_appends and secret[12:28] in old_appends, slack.actions
    old_updates = [p["text"] for name, p in slack.actions if name == "update" and p["ts"] == old_ts]
    assert old_updates == [_slack_handler.CONTINUES_BELOW.strip()], slack.actions
    # stopStream seals; only an update replaces already-appended text.
    visible = {}
    for name, payload in slack.actions:
        ts, text = payload.get("ts"), payload.get("text") or ""
        if name in ("start_stream", "post", "update"):
            visible[ts] = text
        elif name == "append_stream":
            visible[ts] += text
    assert (
        visible[last_ts] == "The access key is [REDACTED: credential]. Done. All set."
    ), slack.actions
    for text in [*slack.rewrites(), *visible.values()]:
        assert secret[4:12] not in text and secret[12:28] not in text, slack.actions
    assert sum("The access key is" in text for text in visible.values()) == 1


_RENDERER_CASES = {
    "renewed-before-the-answer": ([_RENEW, "The whole answer."], "The whole answer."),
    "expired-before-the-answer": ([_EXPIRE, "The whole answer."], "The whole answer."),
    "very-short-answer": ([_RENEW, "Hi!"], "Hi!"),
}


@pytest.mark.parametrize("case", list(_RENDERER_CASES))
def test_renderer_answer_text_is_kept_whole_across_a_renewal(case, clock):
    script, answer = _RENDERER_CASES[case]
    slack = _TimedSlack(clock)
    renderer = SlackRenderer(slack, "C1", "t1", reactions_enabled=False)

    async def scenario():
        await renderer._ensure_stream()
        await renderer._append_task("tool_1", "Bash", "in_progress")
        for item in script:
            if isinstance(item, float):
                clock.now += item
            else:
                await renderer.on_text_chunk(item)
        await renderer.on_done()

    asyncio.run(scenario())
    assert len(slack.started()) == 2, slack.actions
    assert slack.streamed() == answer, slack.actions
    assert not any(answer in text for text in slack.rewrites()), slack.actions


def test_cancelled_native_turn_cancels_its_tool_timer(clock, monkeypatch, tmp_path):
    hide_slack_timing_footer(monkeypatch, tmp_path)
    timers = []
    start_timer = _AnswerStream.start_tool_timer

    def record_timer(self):
        start_timer(self)
        timers.append(self.tool_timer_task)

    class CancelledProvider(FakeProvider):
        async def stream(self, message, timeout=120.0):
            yield LLMEvent(kind=EVENT_TOOL_CALL, title="Bash", tool_call_id="tool_1")
            raise asyncio.CancelledError

    monkeypatch.setattr(_AnswerStream, "start_tool_timer", record_timer)
    slack = _TimedSlack(clock)
    sessions = FakeSessionManager(CancelledProvider([]))

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1")
        await asyncio.sleep(0)
        assert timers and all(timer.cancelled() for timer in timers)

    asyncio.run(scenario())
