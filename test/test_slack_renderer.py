"""Tests for SlackRenderer + SlackApprovalDecider (v1b-3).

Drives the real TurnDriver with a scripted provider into a SlackRenderer
backed by a recording Slack client, and asserts the abstract output events
map to the expected SlackClientOps calls -- including prompt_choice ->
Block Kit approve/deny buttons and the interactive approval await/resolve.
"""

from __future__ import annotations

import asyncio

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
)
from kiro_crew.messaging import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.slack.renderer import (
    _STATUS_WORKING,
    TOOL_APPROVE_ACTION_PREFIX,
    TOOL_DENY_ACTION_PREFIX,
    TOOL_TRUST_ACTION_PREFIX,
    SlackApprovalDecider,
    SlackRenderer,
    build_approval_blocks,
)


class _RecSlack:
    """Minimal recording Slack client (only methods SlackRenderer calls)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._n = 0

    def _ts(self):
        self._n += 1
        return f"ts-{self._n}"

    async def start_stream(self, channel, thread_ts, **kw):
        self.calls.append(("start_stream", {"channel": channel, "thread_ts": thread_ts}))
        return self._ts()

    async def append_stream(self, channel, ts, text):
        self.calls.append(("append_stream", {"text": text}))
        return True

    async def update_message(self, channel, ts, text="", blocks=None):
        self.calls.append(("update_message", {"text": text}))

    async def stop_stream(self, channel, ts, final_text=None):
        self.calls.append(("stop_stream", {"final_text": final_text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.calls.append(("append_task", {"title": title, "status": status}))
        return True

    async def set_thread_status(self, channel, thread_ts, status):
        self.calls.append(("set_thread_status", {"status": status}))

    async def post_blocks(self, channel, blocks, text, thread_ts=None, **kw):
        self.calls.append(("post_blocks", {"blocks": blocks}))
        return self._ts()

    async def post_message(self, channel, text, thread_ts=None, **kw):
        self.calls.append(("post_message", {"text": text}))
        return self._ts()


class _Provider:
    def __init__(self, events):
        self._events = events
        self.approved: list = []
        self.rejected: list = []

    async def stream(self, message):
        for ev in self._events:
            yield ev

    async def approve_tool(self, request_id, *, always=False):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


class TestRendererClose:
    """close() must guarantee the 30s tool-elapsed timer is cancelled even
    when the turn ends via an exception (TurnDriver.run raises -> on_done never
    fires), so the updater task can't survive against a dead stream."""

    def test_close_cancels_leaked_tool_timer(self):
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            renderer._start_tool_timer()  # simulates an in-flight tool
            task = renderer._tool_timer_task
            assert task is not None and not task.done()
            # Simulate the dispatcher finally after driver.run() raised.
            await renderer.close()
            assert renderer._tool_timer_task is None
            await asyncio.sleep(0)  # let the cancellation propagate
            assert task.cancelled() or task.done()

        asyncio.run(scenario())

    def test_close_is_idempotent_and_safe_after_done(self):
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_done()          # success path marks _finalized
            await renderer.close()            # must be a no-op, not re-finalize
            await renderer.close()            # idempotent
            assert renderer._finalized is True

        asyncio.run(scenario())


class TestOptionsControlIsStamped:
    """The canonical Slack path must stamp the controls it posts.

    ``messaging.use_transport`` defaults to True, so this renderer -- not the
    native handler -- posts most real OPTIONS controls. A control that goes out
    unstamped is one whose clicks can never be judged: the click-time check
    abstains and honours it however far the conversation has since moved.
    """

    @staticmethod
    def _options_block(rec):
        for name, kw in rec.calls:
            if name != "post_blocks":
                continue
            for block in kw["blocks"]:
                if block.get("type") == "actions":
                    return block
        return None

    def test_the_footer_control_carries_the_dispatcher_s_stamp(self):
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            renderer._accumulated = "pick one\n[OPTIONS: alpha | beta]"

            seen: list[str] = []

            async def _stamp(final_text: str) -> str:
                seen.append(final_text)
                return "stamp-abc"

            renderer.stamp_options = _stamp
            await renderer.on_done()

            actions = self._options_block(rec)
            assert actions is not None, "the options control must still be posted"
            assert actions.get("block_id") == "stamp-abc", (
                "the control must carry the stamp, or its clicks cannot be judged"
            )
            assert seen, "the stamp must be taken BEFORE the control is posted"

        asyncio.run(scenario())

    def test_what_gets_persisted_keeps_the_options_trailer(self):
        """The stamp replaces the dispatcher's persist, so it must write the same text.

        A replayed turn re-renders its question as a control by finding the
        ``[OPTIONS: ...]`` trailer in the persisted text. Handing the stamp the
        stripped copy that goes to Slack would drop that trailer from history, and
        every replayed control would come back as literal prose instead.
        """

        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            renderer._accumulated = "pick one\n[OPTIONS: alpha | beta]"

            persisted: list[str] = []

            async def _stamp(final_text: str) -> str:
                persisted.append(final_text)
                return "stamp-abc"

            renderer.stamp_options = _stamp
            await renderer.on_done()

            assert persisted, "the stamp must have been invoked"
            assert "[OPTIONS: alpha | beta]" in persisted[0], (
                "the persisted text must keep the trailer, or replay loses the control"
            )

        asyncio.run(scenario())

    def test_a_failing_stamp_costs_the_user_nothing(self):
        """Best-effort: an unstamped control is honoured on click, as before.

        Losing the footer entirely because bookkeeping failed would be a worse
        outcome than losing the ability to refuse a stale click.
        """

        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            renderer._accumulated = "pick one\n[OPTIONS: alpha | beta]"

            async def _boom(final_text: str) -> str:
                raise RuntimeError("transcript unavailable")

            renderer.stamp_options = _boom
            await renderer.on_done()

            actions = self._options_block(rec)
            assert actions is not None, "the control must post even if stamping fails"
            assert "block_id" not in actions

        asyncio.run(scenario())


class TestBuildApprovalBlocks:
    def test_action_ids_encode_request_id(self):
        blocks = build_approval_blocks("grep", "rq9")
        actions = [b for b in blocks if b["type"] == "actions"][0]["elements"]
        ids = {e["action_id"] for e in actions}
        assert f"{TOOL_APPROVE_ACTION_PREFIX}rq9" in ids
        assert f"{TOOL_TRUST_ACTION_PREFIX}rq9" in ids
        assert f"{TOOL_DENY_ACTION_PREFIX}rq9" in ids


class TestSlackRendererMapping:
    def test_text_turn_maps_to_stream(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Hello "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="world"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))
        methods = [m for m, _ in rec.calls]
        assert methods == [
            "set_thread_status", "start_stream", "append_stream", "append_stream",
            "stop_stream", "set_thread_status", "post_blocks",
        ]
        # stop_stream carries the clean final text
        stop = [kw for m, kw in rec.calls if m == "stop_stream"][0]
        assert stop["final_text"] == "Hello world"

    def test_shared_driver_strips_split_steering_marker(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Before [STEERING steer-7e6a4a0d"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="94314d2db: internal ack] after"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))
        visible = "".join(
            str(kw.get("text") or kw.get("final_text") or "")
            for method, kw in rec.calls
            if method in {"append_stream", "stop_stream"}
        )
        assert "Before" in visible and "after" in visible
        assert "STEERING" not in visible and "7e6a4a0d" not in visible
        assert "internal ack" not in visible

    def test_on_turn_start_is_idempotent(self):
        # The dispatcher fires on_turn_start early (before session acquisition)
        # so the ack reaches the user immediately; the driver's later call must
        # no-op rather than post a second working-status update.
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])

        async def _drive():
            await renderer.on_turn_start()  # early ack, before session spin-up
            await TurnDriver(provider, renderer, approval_mode="auto").run("hi")

        asyncio.run(_drive())
        # Exactly one working-status ack at the start (the driver's call no-ops),
        # plus the clearing set_thread_status at on_done.
        working = [
            kw for m, kw in rec.calls
            if m == "set_thread_status" and kw.get("status") == _STATUS_WORKING
        ]
        assert len(working) == 1

    def test_bracket_hold_filters_options_from_stream(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Pick one "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="[OPTIONS: A | B]"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))
        streamed = "".join(kw["text"] for m, kw in rec.calls if m == "append_stream")
        # The [OPTIONS:...] markup must never hit the live stream...
        assert "OPTIONS" not in streamed and "[" not in streamed
        # ...but the surrounding prose still streams.
        assert "Pick one" in streamed
        # ...and the final message is clean (extract_options strips the tag).
        stop = [kw for m, kw in rec.calls if m == "stop_stream"][0]
        assert "OPTIONS" not in (stop["final_text"] or "")
        assert "Pick one" in (stop["final_text"] or "")

    def test_tool_turn_maps_to_tasks(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="x", title="grep", tool_final=False),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="x", tool_output="ok", tool_final=True),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))
        statuses = [kw.get("status") for m, kw in rec.calls if m == "append_task"]
        # Unified on_tool_call: start task1; tool2 completes task1 + starts task2;
        # done completes task2.
        assert statuses == ["in_progress", "complete", "in_progress", "complete"]

    def test_prompt_choice_posts_approval_blocks(self):
        rec = _RecSlack()
        decider = SlackApprovalDecider()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, decider=decider)
        provider = _Provider([
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", options=[{"id": "grep", "label": "grep"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        driver = TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE, decider=decider)

        async def scenario():
            task = asyncio.create_task(driver.run("hi"))
            for _ in range(1000):
                if decider._futures:
                    break
                await asyncio.sleep(0)
            decider.resolve("rq1", True)
            await task

        asyncio.run(scenario())
        posted = [kw for m, kw in rec.calls if m == "post_blocks"]
        # Approval blocks are posted only when a decider can act on them.
        all_ids = {
            e["action_id"]
            for kw in posted for b in kw["blocks"] if b["type"] == "actions"
            for e in b["elements"] if "action_id" in e
        }
        assert f"{TOOL_APPROVE_ACTION_PREFIX}rq1" in all_ids
        assert provider.approved == ["rq1"]

    def test_prompt_choice_suppressed_without_decider(self):
        # Deny-by-default (no decider): no dead approve/deny buttons are posted.
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", options=[{"id": "grep", "label": "grep"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE).run("hi"))
        posted = [kw for m, kw in rec.calls if m == "post_blocks"]
        all_ids = {
            e["action_id"]
            for kw in posted for b in kw["blocks"] if b["type"] == "actions"
            for e in b["elements"] if "action_id" in e
        }
        assert f"{TOOL_APPROVE_ACTION_PREFIX}rq1" not in all_ids
        # No decider to resolve -> deny by default.
        assert provider.rejected == ["rq1"]


class TestApprovalDecider:
    def test_await_then_resolve_approves(self):
        rec = _RecSlack()
        decider = SlackApprovalDecider()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, decider=decider)
        provider = _Provider([
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", options=[{"id": "grep"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        driver = TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE, decider=decider)

        async def scenario():
            task = asyncio.create_task(driver.run("hi"))
            for _ in range(1000):
                if decider._futures:
                    break
                await asyncio.sleep(0)
            assert decider.resolve("rq1", True) is True
            await task

        asyncio.run(scenario())
        assert provider.approved == ["rq1"]
        assert provider.rejected == []

    def test_resolve_unknown_returns_false(self):
        decider = SlackApprovalDecider()
        assert decider.resolve("nope", True) is False

    def test_session_for_maps_rid_to_session(self):
        # While a prompt is awaiting, session_for() returns the decider's
        # session_key so the interaction handler can grant per-session Trust.
        rec = _RecSlack()
        decider = SlackApprovalDecider(session_key="thread-42")
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, decider=decider)
        provider = _Provider([
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rqS", options=[{"id": "grep"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        driver = TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE, decider=decider)

        async def scenario():
            task = asyncio.create_task(driver.run("hi"))
            for _ in range(1000):
                if decider._futures:
                    break
                await asyncio.sleep(0)
            assert SlackApprovalDecider.session_for("thread-42:rqS") == "thread-42"
            assert SlackApprovalDecider.resolve_global("thread-42:rqS", True) is True
            await task

        asyncio.run(scenario())
        # Cleared after the turn.
        assert SlackApprovalDecider.session_for("thread-42:rqS") == ""

    def test_resolve_global_approves_via_registry(self):
        # The Slack interaction handler resolves clicks through the
        # process-global registry (it holds no direct decider reference).
        rec = _RecSlack()
        decider = SlackApprovalDecider()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, decider=decider)
        provider = _Provider([
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rqG", options=[{"id": "grep"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        driver = TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE, decider=decider)

        async def scenario():
            task = asyncio.create_task(driver.run("hi"))
            for _ in range(1000):
                if decider._futures:
                    break
                await asyncio.sleep(0)
            # Resolve WITHOUT a direct decider reference — as interactions.py does.
            assert SlackApprovalDecider.resolve_global("rqG", True) is True
            await task

        asyncio.run(scenario())
        assert provider.approved == ["rqG"]
        assert provider.rejected == []
        # Registry is cleaned up after the turn.
        assert "rqG" not in SlackApprovalDecider._REGISTRY

    def test_resolve_global_denies_via_registry(self):
        # A Slack "deny" button click resolves the global registry with
        # approved=False -> the tool is rejected (not approved).
        rec = _RecSlack()
        decider = SlackApprovalDecider()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, decider=decider)
        provider = _Provider([
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rqD", options=[{"id": "grep"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        driver = TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE, decider=decider)

        async def scenario():
            task = asyncio.create_task(driver.run("hi"))
            for _ in range(1000):
                if decider._futures:
                    break
                await asyncio.sleep(0)
            assert SlackApprovalDecider.resolve_global("rqD", False) is True
            await task

        asyncio.run(scenario())
        assert provider.rejected == ["rqD"]
        assert provider.approved == []
        assert "rqD" not in SlackApprovalDecider._REGISTRY

    def test_timeout_denies(self, monkeypatch):
        # If the user never clicks, the decider denies by default after the
        # approval window (patched tiny for the test).
        monkeypatch.setattr("kiro_crew.slack.renderer._APPROVAL_TIMEOUT", 0.01)
        rec = _RecSlack()
        decider = SlackApprovalDecider()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, decider=decider)
        provider = _Provider([
            AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rqT", options=[{"id": "grep"}]),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode=APPROVAL_INTERACTIVE, decider=decider).run("hi"))
        assert provider.rejected == ["rqT"]
        assert provider.approved == []
        assert "rqT" not in SlackApprovalDecider._REGISTRY

    def test_concurrent_sessions_same_rid_do_not_collide(self):
        # Regression: kiro-cli request ids restart at 1 per session, so two
        # concurrent threads both produce request_id == "1". The registry must
        # be namespaced by session_key so a click on thread B's button resolves
        # ONLY thread B's pending tool — never thread A's.
        import asyncio as _asyncio

        async def scenario():
            dec_a = SlackApprovalDecider(session_key="thread-A")
            dec_b = SlackApprovalDecider(session_key="thread-B")
            ev = AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="1", options=[])
            task_a = _asyncio.create_task(dec_a(ev))
            task_b = _asyncio.create_task(dec_b(ev))
            for _ in range(1000):
                if dec_a._futures and dec_b._futures:
                    break
                await _asyncio.sleep(0)
            # Both deciders are registered under DISTINCT namespaced keys.
            assert SlackApprovalDecider._REGISTRY.get("thread-A:1") is dec_a
            assert SlackApprovalDecider._REGISTRY.get("thread-B:1") is dec_b
            # Approve ONLY thread B via its namespaced token.
            assert SlackApprovalDecider.resolve_global("thread-B:1", True) is True
            assert (await task_b) is True          # B approved
            # A is untouched and still pending — deny it to finish the test.
            assert not task_a.done()
            assert SlackApprovalDecider.resolve_global("thread-A:1", False) is True
            assert (await task_a) is False         # A independently denied

        _asyncio.run(scenario())


class _FakeClock:
    """Pops scripted monotonic values; clamps to the last value when drained."""

    def __init__(self, values):
        self._values = list(values)

    def __call__(self):
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


class _FlakyAppendSlack(_RecSlack):
    """append_stream fails the first time, forcing one stream rotation."""

    def __init__(self):
        super().__init__()
        self._n_append = 0

    async def append_stream(self, channel, ts, text):
        self._n_append += 1
        ok = self._n_append > 1  # first append fails -> rotation, retry succeeds
        self.calls.append(("append_stream", {"text": text, "ok": ok}))
        return ok


class _NoStreamSlack(_RecSlack):
    """start_stream returns None -> chat.update cursor fallback path."""

    async def start_stream(self, channel, thread_ts, **kw):
        self.calls.append(("start_stream", {"thread_ts": thread_ts}))
        return None


class TestStreamMachinery:
    def test_throttle_coalesces_chunks_within_interval(self):
        rec = _RecSlack()
        # 5 clock reads: turn_start, chunk A, B, C, on_done. A flushes
        # (now-0>=1.0); B and C arrive <1.0s later so they coalesce.
        # Chunks end with a space so the driver's StreamRedactor commits each
        # immediately (a trailing credential-class run would otherwise be held
        # back until flush) — this test targets the renderer throttle, not the
        # redactor holdback.
        clock = _FakeClock([1000.0, 1000.0, 1000.2, 1000.4, 1000.4])
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, now=clock)
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="A "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="B "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="C "),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))
        streamed = [kw["text"] for m, kw in rec.calls if m == "append_stream"]
        assert streamed == ["A ", "B C "], streamed

    def test_throttle_flushes_each_chunk_past_interval(self):
        rec = _RecSlack()
        clock = _FakeClock([1000.0, 1000.0, 1002.0, 1003.0])  # B is 2s after A
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, now=clock)
        # Trailing space so StreamRedactor commits each chunk immediately.
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="A "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="B "),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))
        streamed = [kw["text"] for m, kw in rec.calls if m == "append_stream"]
        assert streamed == ["A ", "B "], streamed

    def test_append_failure_triggers_one_rotation(self):
        rec = _FlakyAppendSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("x"))
        methods = [m for m, _ in rec.calls]
        # Initial open + one rotation = 2 start_streams; the failed append is
        # retried after rotation and succeeds.
        assert methods.count("start_stream") == 2, methods
        appends = [kw for m, kw in rec.calls if m == "append_stream"]
        assert appends[0]["ok"] is False and appends[-1]["ok"] is True, appends

    def test_no_stream_falls_back_to_chat_update(self):
        rec = _NoStreamSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="hello"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("x"))
        methods = [m for m, _ in rec.calls]
        # No streaming surface -> a placeholder is posted and edits go via
        # chat.update (update_message), never append_stream.
        assert "update_message" in methods, methods
        assert "append_stream" not in methods, methods
        assert "post_message" in methods, methods  # the _THINKING placeholder


class TestToolTimerAndWait:
    def test_elapsed_appended_to_completed_task_title(self):
        rec = _RecSlack()
        # Clock reads: turn_start, tool1(start timer @1000), tool2(elapsed=2s,
        # completes tool1), on_done. Tool1 ran 2s -> "⏱ 2.0s" in its title.
        clock = _FakeClock([1000.0, 1000.0, 1002.0, 1002.0])
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, now=clock)
        provider = _Provider([
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="a", title="grep", tool_final=False),
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="b", title="cat", tool_final=False),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("x"))
        completes = [
            kw["title"] for m, kw in rec.calls
            if m == "append_task" and kw["status"] == "complete"
        ]
        assert any("⏱" in t for t in completes), completes
        assert renderer._tool_timer_task is None  # timer cancelled on done

    def test_wait_tool_finalizes_stream(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        provider = _Provider([
            AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="w", title="wait", tool_final=False),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="back"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("x"))
        methods = [m for m, _ in rec.calls]
        # The wait tool finalizes (stop_stream) then a fresh stream opens for
        # the post-wait text -> a second start_stream appears.
        assert methods.count("start_stream") == 2, methods
        assert "stop_stream" in methods


class _StepClock:
    """Monotonic clock that advances a fixed step on every call, so every text
    chunk trips the edit-throttle (deterministic intermediate updates)."""

    def __init__(self, step: float = 2.0):
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


class TestNoStreamOptionsFiltering:
    """Regression: [OPTIONS:...] markup must never leak into no-stream
    chat.update calls (the streaming path already filters via bracket-hold)."""

    def test_options_markup_not_leaked_in_no_stream_updates(self):
        rec = _NoStreamSlack()
        renderer = SlackRenderer(
            rec, "C1", "t1", reactions_enabled=False, now=_StepClock(step=2.0)
        )
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Pick one: "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="A or B "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="[OPTIONS: A | B]"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))

        updates = [kw["text"] for m, kw in rec.calls if m == "update_message"]
        assert updates, "expected chat.update calls in the no-stream fallback"
        # Neither intermediate nor final updates leak the OPTIONS markup:
        #  - intermediate updates are filtered via the bracket-hold buffer,
        #  - the final update uses extract_options(_accumulated) clean text.
        assert all("[OPTIONS:" not in t for t in updates), updates
        # The visible text is the filtered content.
        assert any("Pick one" in t for t in updates), updates

    def test_thinking_tags_not_leaked_in_no_stream_updates(self):
        rec = _NoStreamSlack()
        renderer = SlackRenderer(
            rec, "C1", "t1", reactions_enabled=False, now=_StepClock(step=2.0)
        )
        provider = _Provider([
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Answer: "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="<thinking>secret</thinking>"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="42"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ])
        asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))

        updates = [kw["text"] for m, kw in rec.calls if m == "update_message"]
        assert updates, "expected chat.update calls in the no-stream fallback"
        # Intermediate renders carry the cursor (▍). They must never surface
        # <thinking>…</thinking>, mirroring the streaming path's
        # _flush_stream_buffer strip. (The final on_done render is produced by
        # extract_options — identical on both paths — and is out of scope here.)
        intermediate = [t for t in updates if "▍" in t]
        assert intermediate, updates
        assert all("<thinking>" not in t and "secret" not in t for t in intermediate), intermediate


class TestShowThinking:
    """slack.show_thinking gates surfacing the model's reasoning as a 💭 thread
    reply above the answer (matches native). Off => reasoning stays private."""

    def _events(self):
        return [
            AcpEvent(kind=EVENT_THINKING_CHUNK, text="let me think "),
            AcpEvent(kind=EVENT_THINKING_CHUNK, text="about it"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="the answer "),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

    def test_show_thinking_on_posts_reasoning_reply(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, show_thinking=True)
        asyncio.run(TurnDriver(_Provider(self._events()), renderer, approval_mode="auto").run("hi"))
        posts = [kw["text"] for m, kw in rec.calls if m == "post_message"]
        assert any(p.startswith("💭") and "let me think about it" in p for p in posts), posts

    def test_show_thinking_off_suppresses_reasoning(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, show_thinking=False)
        asyncio.run(TurnDriver(_Provider(self._events()), renderer, approval_mode="auto").run("hi"))
        posts = [kw["text"] for m, kw in rec.calls if m == "post_message"]
        assert not any(p.startswith("💭") for p in posts), posts

    def test_thinking_reply_is_redacted(self):
        rec = _RecSlack()
        renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False, show_thinking=True)
        events = [
            AcpEvent(kind=EVENT_THINKING_CHUNK, text="key AKIA1234567890ABCDEX here"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        asyncio.run(TurnDriver(_Provider(events), renderer, approval_mode="auto").run("hi"))
        thinking = [kw["text"] for m, kw in rec.calls if m == "post_message" and kw["text"].startswith("💭")]
        assert thinking, rec.calls
        assert "AKIA1234567890ABCDEX" not in thinking[0]
        assert "[REDACTED: credential]" in thinking[0]
