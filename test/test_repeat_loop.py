"""The repeat-loop tracker: same call, same result, told once."""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.dashboard.chat_runner import _steer_repeat_loop_notice
from kiro_crew.repeat_loop import (
    _EXEMPT_TOOL_IDS,
    _MAX_TRACKED,
    REPEAT_LOOP_THRESHOLD,
    RepeatLoopTracker,
    is_exempt_tool,
)


def _run(
    tracker: RepeatLoopTracker, n: int, *, cmd: str = "make test", output: str = "boom"
) -> list[str]:
    notices = []
    for i in range(n):
        cid = f"{cmd}-{i}"
        tracker.note_call(cid, "shell", cmd, f"Run {cmd}")
        notices.append(tracker.note_result(cid, status="failed", output=output))
    return notices


def test_threshold_identical_results_send_one_notice() -> None:
    notices = _run(RepeatLoopTracker(), REPEAT_LOOP_THRESHOLD + 3)
    sent = [n for n in notices if n]
    assert len(sent) == 1
    assert notices[REPEAT_LOOP_THRESHOLD - 1] == sent[0]
    assert "Run make test" in sent[0]
    assert f"{REPEAT_LOOP_THRESHOLD} times" in sent[0]


def test_below_threshold_is_silent() -> None:
    assert not any(_run(RepeatLoopTracker(), REPEAT_LOOP_THRESHOLD - 1))


def test_changing_result_resets_the_streak() -> None:
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", "make test", "Run make test")
        assert tracker.note_result(f"c{i}", status="failed", output=f"error {i % 2}") == ""


def test_different_input_is_a_different_call() -> None:
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", f"make test-{i}", "Run make test")
        assert tracker.note_result(f"c{i}", status="failed", output="boom") == ""


def test_interleaved_calls_still_count_per_call() -> None:
    tracker = RepeatLoopTracker()
    sent = []
    for i in range(REPEAT_LOOP_THRESHOLD):
        for cmd in ("a", "b"):
            cid = f"{cmd}{i}"
            tracker.note_call(cid, "shell", cmd, cmd)
            sent.append(tracker.note_result(cid, status="failed", output="no"))
    assert len([n for n in sent if n]) == 2


def test_output_from_an_earlier_frame_is_kept() -> None:
    """Output and terminal status may arrive in separate frames."""
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", "make", "make")
        assert tracker.note_result(f"c{i}", status="", output=f"out {i}", terminal=False) == ""
        assert tracker.note_result(f"c{i}", status="completed", output="") == ""


def test_unknown_or_inputless_call_is_ignored() -> None:
    tracker = RepeatLoopTracker()
    for i in range(REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"c{i}", "shell", "", "empty")
        assert tracker.note_result(f"c{i}", status="failed", output="x") == ""
        assert tracker.note_result("never-seen", status="failed", output="x") == ""


def test_retained_fields_are_bounded() -> None:
    tracker = RepeatLoopTracker()
    huge_id, huge_title = "i" * 100_000, "t" * 100_000
    tracker.note_call(huge_id, "shell", "make", huge_title)
    ((key, (_, title)),) = tracker._calls.items()
    assert len(key) == 64 and len(title) <= 200
    tracker.note_call("long", "n" * 100_000, "make", "t")
    assert list(tracker._names.values()) == ["shell"]


def test_pending_calls_are_capped() -> None:
    tracker = RepeatLoopTracker()
    for i in range(_MAX_TRACKED):
        tracker.note_call(f"c{i}", "shell", f"cmd {i}", "t")
    before = dict(tracker._calls)
    tracker.note_call("c0", "shell", "refined", "t")
    assert tracker._calls != before
    tracker.note_call("overflow", "shell", "cmd", "t")
    assert len(tracker._calls) == _MAX_TRACKED
    assert tracker.note_result("overflow", status="failed", output="x") == ""
    assert len(tracker._calls) == _MAX_TRACKED


def test_streak_signatures_are_capped() -> None:
    tracker = RepeatLoopTracker()
    for i in range(_MAX_TRACKED):
        tracker.note_call(f"c{i}", "shell", f"cmd {i}", "t")
        tracker.note_result(f"c{i}", status="failed", output="x")
    assert len(tracker._streak) == _MAX_TRACKED
    notices = _run(tracker, REPEAT_LOOP_THRESHOLD, cmd="new")
    assert notices == [""] * REPEAT_LOOP_THRESHOLD
    assert len(tracker._streak) == _MAX_TRACKED


class _Client:
    def __init__(self, capable: bool) -> None:
        self.supports_refusal_steer = capable
        self.sent: list[str] = []

    async def steer(self, text: str) -> bool:
        self.sent.append(text)
        return True


def test_steer_is_gated_on_capability() -> None:
    capable, plain = _Client(True), _Client(False)
    assert asyncio.run(_steer_repeat_loop_notice(capable, "note")) is True
    assert asyncio.run(_steer_repeat_loop_notice(plain, "note")) is False
    assert capable.sent == ["note"] and plain.sent == []


# Tools that act outside the turn: the result text repeats, the page does not.

_PRESS_KEY = '{"op": "press_key", "args": {"key": "ArrowDown"}}'
_PRESS_KEY_OUT = 'Browser press_key: {"key": "ArrowDown", "modifiers": 0}'


def _repeat(
    tracker: RepeatLoopTracker, n: int, name: str, tool_input: str, title: str
) -> list[str]:
    notices = []
    for i in range(n):
        tracker.note_call(f"x{i}", name, tool_input, title)
        notices.append(tracker.note_result(f"x{i}", status="completed", output=_PRESS_KEY_OUT))
    return notices


def test_browser_press_key_never_draws_the_notice() -> None:
    for name in ("browser", "kirocrew-core::browser", "mcp__kirocrew-core__browser"):
        tracker = RepeatLoopTracker()
        assert not any(_repeat(tracker, 10, name, _PRESS_KEY, "Drive browser"))
        assert tracker._calls == {} and tracker._streak == {}


def test_browser_through_the_tool_call_wrapper_never_draws_the_notice() -> None:
    wrapped = json.dumps({"tool_id": "kirocrew-core::browser", "arguments": json.loads(_PRESS_KEY)})
    for name in ("tool_call", ""):
        tracker = RepeatLoopTracker()
        assert not any(_repeat(tracker, 10, name, wrapped, ""))
        assert tracker._streak == {}
    # A wrapper input cut short still names its tool.
    assert not any(_repeat(RepeatLoopTracker(), 10, "tool_call", wrapped[:60], ""))


def test_computer_use_tools_are_exempt_too() -> None:
    wrapped = json.dumps({"tool_id": "kirocrew-computer::computer_press_key", "arguments": {}})
    assert not any(_repeat(RepeatLoopTracker(), 10, "computer_press_key", '{"key": "a"}', "k"))
    assert not any(_repeat(RepeatLoopTracker(), 10, "tool_call", wrapped, ""))
    assert all(is_exempt_tool(name) for name in _EXEMPT_TOOL_IDS)


def test_a_refinement_naming_an_exempt_tool_drops_the_record() -> None:
    tracker = RepeatLoopTracker()
    tracker.note_call("c", "", '{"op": "press_key"}', "")
    tracker.note_call("c", "browser", _PRESS_KEY, "")
    assert tracker._calls == {}


def test_other_tools_keep_the_notice() -> None:
    sent = [n for n in _repeat(RepeatLoopTracker(), 3, "shell", "ls", "Run ls") if n]
    assert len(sent) == 1 and "3 times" in sent[0]
    # A wrapper around a non-exempt tool is still tracked.
    wrapped = json.dumps({"tool_id": "kirocrew-core::send_message", "arguments": {}})
    assert any(_repeat(RepeatLoopTracker(), 3, "tool_call", wrapped, ""))


def test_empty_title_shows_the_tool_name() -> None:
    (notice,) = [n for n in _repeat(RepeatLoopTracker(), 3, "shell", "ls", "") if n]
    assert "a tool call" not in notice and notice.rstrip().split("\n")[0].endswith(": shell")
    wrapped = json.dumps({"tool_id": "kirocrew-core::send_message", "arguments": {}})
    (notice,) = [n for n in _repeat(RepeatLoopTracker(), 3, "tool_call", wrapped, "") if n]
    assert notice.split("\n")[0].endswith(": kirocrew-core::send_message")


def _repeat_refined(
    tracker: RepeatLoopTracker, n: int, name: str, tool_input: str, title: str
) -> list[str]:
    """Each call named on its first frame only; a refinement re-sends the input."""
    notices = []
    for i in range(n):
        tracker.note_call(f"r{i}", name, tool_input, title)
        tracker.note_call(f"r{i}", "", tool_input, "")
        notices.append(tracker.note_result(f"r{i}", status="completed", output=_PRESS_KEY_OUT))
    return notices


def test_a_refinement_naming_no_tool_keeps_the_first_frames_tool() -> None:
    tracker = RepeatLoopTracker()
    assert not any(_repeat_refined(tracker, 10, "browser", _PRESS_KEY, "browser"))
    assert tracker._calls == {} and tracker._streak == {} and tracker._names == {}
    (notice,) = [n for n in _repeat_refined(RepeatLoopTracker(), 3, "read_path", "{}", "") if n]
    assert notice.split("\n")[0].endswith(": read_path")


def test_a_first_frame_with_no_input_yet_still_names_the_call() -> None:
    # claude-agent-acp opens a call with an empty rawInput and refines it.
    tracker = RepeatLoopTracker()
    for i in range(10):
        tracker.note_call(f"e{i}", "browser", "", "browser")
        tracker.note_call(f"e{i}", "", _PRESS_KEY, "")
        assert tracker.note_result(f"e{i}", status="completed", output=_PRESS_KEY_OUT) == ""
    assert tracker._calls == {} and tracker._streak == {} and tracker._names == {}


def _repeat_wire_named(
    tracker: RepeatLoopTracker, n: int, wire_title: str, tool_input: str
) -> list[str]:
    """Each call named only by its wire title; a refinement names nothing."""
    notices = []
    for i in range(n):
        tracker.note_call(f"w{i}", "", tool_input, "", wire_title=wire_title)
        tracker.note_call(f"w{i}", "", tool_input, "")
        notices.append(tracker.note_result(f"w{i}", status="completed", output=_PRESS_KEY_OUT))
    return notices


def test_a_call_named_only_by_its_wire_title() -> None:
    for wire in ("@kirocrew-core/browser", "Running: @kirocrew-core/browser"):
        tracker = RepeatLoopTracker()
        assert not any(_repeat_wire_named(tracker, 10, wire, _PRESS_KEY))
        assert tracker._calls == {} and tracker._streak == {}
    (notice,) = [n for n in _repeat_wire_named(RepeatLoopTracker(), 10, "@probe/echo", "{}") if n]
    assert notice.split("\n")[0].endswith(": @probe/echo")
    # A wire title with whitespace is a command, not a tool name.
    (notice,) = [
        n for n in _repeat_wire_named(RepeatLoopTracker(), 3, "Running: cat a/browser", "{}") if n
    ]
    assert notice.split("\n")[0].endswith(": a tool call")


# A real dashboard turn fed KAS-shaped frames (as recorded in
# test/fixtures/acp_frames/kas/): a resident MCP call names its tool only in the
# wire title, the in_progress update re-sends rawInput with no title, and the
# completed update carries the title and the result.


def _kas_frames(server: str, tool: str, args: dict, out: str, n: int) -> list[dict]:
    meta = {"kiro": {"serverName": server, "toolOrigin": "client"}}
    title = f"@{server}/{tool}"
    frames: list[dict] = []
    for i in range(n):
        cid = f"kas-toolcall-{i}"
        frames += [
            {
                "sessionUpdate": "tool_call",
                "toolCallId": cid,
                "title": title,
                "kind": "other",
                "status": "pending",
                "rawInput": args,
                "_meta": meta,
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": cid,
                "status": "in_progress",
                "rawInput": args,
                "_meta": meta,
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": cid,
                "status": "completed",
                "title": title,
                "rawInput": args,
                "rawOutput": {"response": out, "imageBase64Urls": [], "message": out},
                "content": [{"type": "content", "content": {"type": "text", "text": out}}],
                "_meta": meta,
            },
        ]
    return frames


async def _kas_turn_notices(server: str, tool: str, args: dict, out: str) -> list[str]:
    from turn_harness import TurnScript, run_turn

    from kiro_crew.acp._dispatch import parse_session_update
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        STOP_REASON_END_TURN,
        AcpEvent,
    )
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    caches: dict = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "harness_tool_name_cache": {},
        "tool_kind_cache": {},
    }
    events: list = []
    for frame in _kas_frames(server, tool, args, out, REPEAT_LOOP_THRESHOLD):
        events += parse_session_update(frame, harness_backend=ACP_BACKEND_KAS, **caches)
    assert events and not any(event.tool_name for event in events)
    events += [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]
    record = await run_turn(TurnScript(events=events))
    return [c.args[0] for c in record.calls("steer") if "same tool call" in c.args[0]]


class TestAKasTurn:
    @pytest.mark.asyncio
    async def test_repeated_browser_keys_draw_no_notice(self) -> None:
        args = {"op": "press_key", "args": {"key": "Tab"}}
        assert await _kas_turn_notices("kirocrew-core", "browser", args, _PRESS_KEY_OUT) == []

    @pytest.mark.asyncio
    async def test_a_repeated_other_tool_draws_one_notice_titled_by_its_tool(self) -> None:
        (notice,) = await _kas_turn_notices("probe", "echo", {"text": "hi"}, "ECHO:hi")
        assert notice.split("\n")[0].endswith(": @probe/echo")


def test_names_left_by_finished_calls_never_crowd_out_a_live_one() -> None:
    # KAS re-titles a call after its terminal result, so finished calls leave names.
    tracker = RepeatLoopTracker()
    wire = "@kirocrew-core/browser"
    for i in range(_MAX_TRACKED + REPEAT_LOOP_THRESHOLD * 2):
        tracker.note_call(f"k{i}", "", _PRESS_KEY, "", wire_title=wire)
        tracker.note_call(f"k{i}", "", _PRESS_KEY, "")
        assert tracker.note_result(f"k{i}", status="completed", output=_PRESS_KEY_OUT) == ""
        tracker.note_call(f"k{i}", "", _PRESS_KEY, "", wire_title=wire)
    assert len(tracker._names) == _MAX_TRACKED and tracker._streak == {}
