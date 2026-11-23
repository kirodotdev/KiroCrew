"""Channel turns claim the out-of-band directive record on a backend with no tool name.

KAS stamps a ``tool_call`` frame with the server name and the wire title
``@kirocrew-core/<tool>`` but no ``_meta.kiro`` tool name, so the marker gate in
``messaging/directive_tracker.py`` records nothing and every directive from a
Slack/Telegram/Teams turn was dropped while the tool said "requested". The tool
also parks its validated payload on the gateway (``kiro_crew.directive_queue``);
these tests pin the channel side's claim of that record, and the limits it keeps:
the call input selects the record, a native sub-agent never claims its parent's
record, a parent call sharing a sub-agent's digest is refused, a record from an
earlier turn is not claimed, and each record applies once.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kiro_crew import directive_queue, session_directive
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard.session_directive_apply import DirectiveOutcome
from kiro_crew.messaging import TransportCapabilities, TurnDriver
from kiro_crew.messaging.dispatch import build_directive_consumer
from kiro_crew.messaging.renderer import Renderer

KEY = "slack:1755000000.1"
CALL_ARGS = {"message": "watch PR #1", "idle_secs": 300}
#: What the gateway derived and parked; deliberately not the call input, so a
#: test can tell which one was applied.
PARKED_ARGS = {"message": "watch PR #1", "idle_secs": 300, "max_cycles": 24}
DIGEST = session_directive.call_input_digest("monitor_start", CALL_ARGS)
NOT_APPLIED = "session-directive NOT APPLIED"


@pytest.fixture(autouse=True)
def _clean_queue():
    directive_queue.reset()
    yield
    directive_queue.reset()


class _Renderer(Renderer):
    def __init__(self):
        super().__init__(TransportCapabilities())

    async def on_text_chunk(self, text):
        pass

    async def on_thinking(self, text):
        pass

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        pass

    async def on_prompt_choice(self, options, request_id):
        pass

    async def on_compaction(self, pct):
        pass

    async def on_done(self, stop_reason=""):
        pass

    async def on_steer_consumed(self, summary=""):
        pass


class _Provider:
    """Yields events; a callable in the script runs instead (the tool's publish)."""

    def __init__(self, script):
        self._script = script

    async def stream(self, message):
        for step in self._script:
            if callable(step):
                step()
            else:
                yield step

    async def approve_tool(self, request_id, *, always=False):
        pass

    async def reject_tool(self, request_id):
        pass


class _Consumer:
    """A channel consumer with the out-of-band claim bound to *KEY*."""

    def __init__(self, key: str = KEY):
        self.key = key
        self.applied: list[tuple[str, dict]] = []

    async def __call__(self, kind, args):
        self.applied.append((kind, args))

    def claim_parked(self, input_digest, not_before):
        return directive_queue.claim(self.key, input_digest, not_before=not_before)


def _park(args=PARKED_ARGS, digest=DIGEST, key=KEY):
    return lambda: directive_queue.publish(key, "monitor_start", dict(args), digest)


def _kas_call(tcid="tc-kas", raw=CALL_ARGS, wire_title="@kirocrew-core/monitor_start"):
    """The KAS shape: server name, wire title, rawInput -- no tool name."""
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=tcid,
        title=wire_title,
        wire_title=wire_title,
        tool_kind="other",
        tool_name="",
        mcp_server_name=session_directive.CORE_MCP_SERVER,
        raw_tool_params=dict(raw),
    )


def _result(tcid="tc-kas", text="Monitor loop requested.", final=True):
    return AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tcid, tool_output=text, tool_final=final)


def _child(tcid):
    return AcpEvent(kind="subagent_activity", sub_session_id="sub-1", tool_call_id=tcid)


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")


def _run(script, consumer):
    driver = TurnDriver(_Provider(script), _Renderer(), directive_consumer=consumer)
    asyncio.run(driver.run("hello"))


class TestKasFrameClaims:
    def test_kas_call_applies_the_parked_record(self, caplog):
        consumer = _Consumer()
        with caplog.at_level(logging.INFO, logger="kiro_crew.messaging.directive_tracker"):
            _run([_kas_call(), _park(), _result(), _DONE], consumer)
        assert consumer.applied == [("monitor_start", PARKED_ARGS)]
        assert directive_queue.depth(KEY) == 0
        assert not any(NOT_APPLIED in r.getMessage() for r in caplog.records)

    def test_kas_title_with_running_prefix_applies(self):
        consumer = _Consumer()
        _run(
            [_kas_call(wire_title="Running: @kirocrew-core/monitor_start"), _park(), _result()],
            consumer,
        )
        assert consumer.applied == [("monitor_start", PARKED_ARGS)]

    def test_two_result_frames_apply_once(self):
        consumer = _Consumer()
        _run(
            [_kas_call(), _park(), _park(), _result(), _result(), _DONE],
            consumer,
        )
        assert consumer.applied == [("monitor_start", PARKED_ARGS)]
        # The second identical record waits for a second call of its own.
        assert directive_queue.depth(KEY) == 1

    def test_mid_stream_frame_leaves_the_claim_to_the_final_frame(self):
        consumer = _Consumer()
        _run(
            [_kas_call(), _result(final=False), _park(), _result(), _DONE],
            consumer,
        )
        assert consumer.applied == [("monitor_start", PARKED_ARGS)]

    def test_record_under_other_arguments_is_not_claimed(self):
        consumer = _Consumer()
        other = session_directive.call_input_digest("monitor_start", {"message": "x"})
        _run([_kas_call(), _park(digest=other), _result(), _DONE], consumer)
        assert consumer.applied == []
        assert directive_queue.depth(KEY) == 1

    def test_record_for_another_session_is_not_claimed(self):
        consumer = _Consumer()
        _run([_kas_call(), _park(key="slack:other"), _result(), _DONE], consumer)
        assert consumer.applied == []
        assert directive_queue.depth("slack:other") == 1

    def test_model_written_display_title_selects_nothing(self):
        """Only the backend's wire title names the tool; ``title`` is not read."""
        consumer = _Consumer()
        call = _kas_call(wire_title="")
        call.title = "@kirocrew-core/monitor_start"
        call.mcp_server_name = ""
        _run([call, _park(), _result(), _DONE], consumer)
        assert consumer.applied == []
        assert directive_queue.depth(KEY) == 1


class TestKiroCliFrameUnchanged:
    def test_marker_path_applies_once_and_leaves_the_twin(self):
        """kiro-cli: trusted identity plus marker. The marker applies, exactly as
        before; the parked twin is not claimed, so nothing applies twice."""
        consumer = _Consumer()
        call = AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-1",
            title="monitor_start",
            tool_name="monitor_start",
            mcp_server_name=session_directive.CORE_MCP_SERVER,
            raw_tool_params=dict(CALL_ARGS),
        )
        marker = session_directive.encode("monitor_start", dict(CALL_ARGS), "requested")
        _run([call, _park(), _result("tc-1", marker), _DONE], consumer)
        assert consumer.applied == [("monitor_start", CALL_ARGS)]
        assert directive_queue.depth(KEY) == 1


class TestSubAgentIsolation:
    def test_native_child_call_is_refused_and_its_record_retired(self):
        consumer = _Consumer()
        _run(
            [_child("tc-child"), _kas_call("tc-child"), _park(), _result("tc-child"), _DONE],
            consumer,
        )
        assert consumer.applied == []
        assert directive_queue.depth(KEY) == 0, "the child's record must not wait for the parent"

    def test_parent_call_sharing_a_child_digest_is_refused(self, caplog):
        consumer = _Consumer()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.messaging.directive_tracker"):
            _run(
                [
                    _child("tc-child"),
                    _kas_call("tc-child"),
                    _kas_call("tc-parent"),
                    _park(),
                    _park(),
                    _result("tc-parent"),
                    _result("tc-child"),
                    _DONE,
                ],
                consumer,
            )
        assert consumer.applied == []
        assert any("NOT CLAIMED" in r.getMessage() for r in caplog.records)

    def test_parent_sharing_a_child_digest_is_refused_when_the_child_frame_lands_first(self):
        consumer = _Consumer()
        _run(
            [
                _child("tc-child"),
                _kas_call("tc-child"),
                _kas_call("tc-parent"),
                _park(),
                _park(),
                _result("tc-child"),
                _result("tc-parent"),
                _DONE,
            ],
            consumer,
        )
        assert consumer.applied == []

    def test_parent_call_with_other_arguments_still_applies(self):
        consumer = _Consumer()
        parent_args = {"message": "parent"}
        parent_digest = session_directive.call_input_digest("monitor_start", parent_args)
        _run(
            [
                _child("tc-child"),
                _kas_call("tc-child"),
                _kas_call("tc-parent", raw=parent_args),
                _park(),
                _park(args=parent_args, digest=parent_digest),
                _result("tc-child"),
                _result("tc-parent"),
                _DONE,
            ],
            consumer,
        )
        assert consumer.applied == [("monitor_start", parent_args)]
        assert directive_queue.depth(KEY) == 0


class TestTurnBound:
    def test_record_parked_before_this_turn_is_not_claimed(self):
        consumer = _Consumer()
        _park()()  # an earlier turn's record, parked before this tracker exists
        # Back-date it explicitly: two monotonic reads can share one clock tick
        # (about 15.6 ms on Windows), so "parked before" must not rest on them.
        directive_queue.DIRECTIVES.pending[KEY][0]["at"] -= 60.0
        _run([_kas_call(), _result(), _DONE], consumer)
        assert consumer.applied == []
        assert directive_queue.depth(KEY) == 1


class TestBuildDirectiveConsumerClaim:
    def test_a_record_the_gateway_parks_is_claimable_by_the_channel_consumer(self):
        """The gateway route parks through the dashboard import path; the channel
        consumer claims through the neutral one. Both must be the one store."""
        from kiro_crew.dashboard import directive_queue as gateway_side

        assert gateway_side is directive_queue
        gateway_side.publish(KEY, "monitor_start", dict(PARKED_ARGS), DIGEST)
        consume = build_directive_consumer(session_key=KEY, sessions=object())
        record = consume.claim_parked(DIGEST, 0.0)
        assert record is not None and record["args"] == PARKED_ARGS

    @pytest.mark.asyncio
    async def test_claim_is_bound_to_the_consumer_session(self, monkeypatch):
        seen: list = []

        async def _spy(state, slot, session_key, kind, args, **_kw):
            seen.append((session_key, kind, args))
            return DirectiveOutcome("ok")

        monkeypatch.setattr(
            "kiro_crew.dashboard.session_directive_apply.apply_session_directive_outcome", _spy
        )
        consume = build_directive_consumer(session_key=KEY, sessions=object())
        directive_queue.publish("slack:other", "monitor_start", dict(PARKED_ARGS), DIGEST)
        assert consume.claim_parked(DIGEST, 0.0) is None
        directive_queue.publish(KEY, "monitor_start", dict(PARKED_ARGS), DIGEST)
        record = consume.claim_parked(DIGEST, 0.0)
        assert record is not None and record["args"] == PARKED_ARGS
        await consume("monitor_start", dict(PARKED_ARGS))
        assert seen == [(KEY, "monitor_start", PARKED_ARGS)]
