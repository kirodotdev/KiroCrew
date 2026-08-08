"""Tests for the Stop-hook block-decision continuation.

A Stop hook that exits 0 and prints ``{"decision": "block", "reason": ...}`` on
stdout asks the harness to continue the turn with ``reason`` as the next
message. These cover the parse, the suppression guard, and the provenance the
queued entry must carry so the transcript renders it as machine orchestration
rather than user speech.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND, is_synthetic_recovery_item
from kiro_crew.dashboard.state import (
    HOOK_CONTINUATION_RECOVERY_PREFIX,
    STOP_REASON_CANCELLED,
    _ChatSlot,
    parse_hook_continuations,
    should_queue_hook_continuation,
)
from kiro_crew.hooks import HOOK_EVENT_STOP, ScriptHookStore


class TestParseHookContinuations:
    def test_block_decision_yields_its_reason(self) -> None:
        out = parse_hook_continuations(['{"decision": "block", "reason": "keep reading"}'])

        assert out == ["keep reading"]

    def test_firing_order_is_preserved_across_hooks(self) -> None:
        out = parse_hook_continuations(
            [
                '{"decision": "block", "reason": "first"}',
                '{"decision": "block", "reason": "second"}',
            ]
        )

        assert out == ["first", "second"]

    def test_plain_logging_output_is_ignored(self) -> None:
        """An ordinary Stop hook prints diagnostics and must still stop the turn."""
        assert parse_hook_continuations(["checked 3 files, all clean"]) == []

    def test_non_block_decision_is_ignored(self) -> None:
        assert parse_hook_continuations(['{"decision": "approve"}']) == []

    def test_block_without_a_reason_is_ignored(self) -> None:
        """``reason`` IS the continuation, so a block with none has nothing to inject."""
        assert parse_hook_continuations(['{"decision": "block"}']) == []

    def test_blank_reason_is_ignored(self) -> None:
        assert parse_hook_continuations(['{"decision": "block", "reason": "   "}']) == []

    def test_non_object_json_is_ignored(self) -> None:
        assert parse_hook_continuations(["[1, 2, 3]", '"a string"', "42"]) == []

    def test_blocked_marker_from_an_exit_2_hook_is_ignored(self) -> None:
        """``_fire`` mixes ``BLOCKED:...`` denial markers into the same list."""
        assert parse_hook_continuations(["BLOCKED:system:hook store not initialized"]) == []

    def test_deeply_nested_json_does_not_error_the_turn(self) -> None:
        """``json.loads`` raises RecursionError -- a RuntimeError, not a ValueError."""
        assert parse_hook_continuations(["[" * 20000] + ['{"decision": "block", "reason": "ok"}']) == [
            "ok"
        ]

    def test_a_valid_decision_survives_a_malformed_sibling(self) -> None:
        out = parse_hook_continuations(["not json at all", '{"decision": "block", "reason": "go on"}'])

        assert out == ["go on"]


class TestShouldQueueHookContinuation:
    def test_normal_turn_end_allows_a_continuation(self) -> None:
        assert should_queue_hook_continuation(False, False, "end_turn") is True

    def test_user_stop_suppresses_it(self) -> None:
        """A hook must never be able to override the Stop button."""
        assert should_queue_hook_continuation(True, False, "end_turn") is False

    def test_pending_session_reset_suppresses_it(self) -> None:
        assert should_queue_hook_continuation(False, True, "end_turn") is False

    def test_cancelled_turn_suppresses_it(self) -> None:
        assert should_queue_hook_continuation(False, False, STOP_REASON_CANCELLED) is False


class TestQueuedContinuationProvenance:
    """The entry must be classifiable BOTH ways: structurally and by content.

    The ``kind`` tag survives queue transformations and drives dequeue routing;
    the content prefix is what remains once the entry is flattened to a string
    for the render and Slack-mirror paths, where the metadata is gone.
    """

    def test_entry_carries_the_synthetic_recovery_kind(self) -> None:
        slot = _ChatSlot("test-slot")

        slot.queue_insert(
            0,
            f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nkeep reading",
            kind=SYNTHETIC_RECOVERY_KIND,
        )

        assert is_synthetic_recovery_item(slot._queue[0])

    def test_entry_content_opens_with_the_marker(self) -> None:
        slot = _ChatSlot("test-slot")

        slot.queue_insert(
            0,
            f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nkeep reading",
            kind=SYNTHETIC_RECOVERY_KIND,
        )

        assert slot._queue[0]["content"].startswith(HOOK_CONTINUATION_RECOVERY_PREFIX)

    def test_marker_does_not_claim_a_recovery(self) -> None:
        """The turn COMPLETED and a hook asked for another; nothing was recovered.

        The constant is named into the ``*_RECOVERY_PREFIX`` family so the
        cross-language drift guard in test_recovery_card_prefixes.py sees it, but
        the VALUE is what reaches the user.
        """
        assert "recovery" not in HOOK_CONTINUATION_RECOVERY_PREFIX.lower()
        assert HOOK_CONTINUATION_RECOVERY_PREFIX.startswith("[")
        assert HOOK_CONTINUATION_RECOVERY_PREFIX.endswith("]")


def _hook_result(stdout: str) -> SimpleNamespace:
    """A hook-store result as ``_fire`` reads it: exit 0 with stdout."""
    return SimpleNamespace(exit_code=0, stdout=stdout, stderr="", hook_name="stop-gate")


def _harness(tmp_path, hook_stdout: str | None):
    """A slot whose turn completes normally, with a Stop hook that prints ``hook_stdout``.

    The stub answers EVERY hook event with the same result rather than keying on
    the Stop event: only the Stop handler parses continuations, so a single
    queued entry also proves no other event path queues one.
    """
    from kiro_crew.dashboard.chat_runner import _run_chat
    from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

    state = _make_state(tmp_path)
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()

    state._hook_store = MagicMock()
    results = [_hook_result(hook_stdout)] if hook_stdout is not None else []
    state._hook_store.fire = AsyncMock(return_value=results)

    slot = state.get_or_create_slot("stop-hook-slot")
    slot._titled = True
    slot.append("user", "hello", "msg msg-u")

    client = state.sessions.get_or_create.return_value[0]
    client.shutdown = AsyncMock()

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="all done")

    client.stream = _stream
    client.stream_command = _stream
    return state, slot, _run_chat


async def _run_turn(state, slot, run_chat) -> None:
    """Run one turn without letting the dequeue loop dispatch what it queued."""
    with patch(
        "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await run_chat(state, slot, "do the thing")


class TestRunnerWiring:
    @pytest.mark.asyncio
    async def test_block_decision_queues_the_reason_as_the_next_turn(self, tmp_path) -> None:
        state, slot, run_chat = _harness(
            tmp_path, '{"decision": "block", "reason": "Read the log first."}'
        )

        await _run_turn(state, slot, run_chat)

        assert slot._queue == [
            {
                "id": slot._queue[0]["id"],
                "content": f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nRead the log first.",
                "kind": SYNTHETIC_RECOVERY_KIND,
            }
        ]
        assert is_synthetic_recovery_item(slot._queue[0])

    @pytest.mark.asyncio
    async def test_an_ordinary_stop_hook_ends_the_turn(self, tmp_path) -> None:
        """Exit 0 with plain output is the common case and must not continue."""
        state, slot, run_chat = _harness(tmp_path, "checked 3 files, all clean")

        await _run_turn(state, slot, run_chat)

        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_no_hook_output_ends_the_turn(self, tmp_path) -> None:
        state, slot, run_chat = _harness(tmp_path, None)

        await _run_turn(state, slot, run_chat)

        assert slot._queue == []


def _sandbox_backend_available() -> bool:
    """Whether this host can actually execute a hook subprocess.

    Hook execution goes through the OS sandbox, and ``wrap_argv`` fails closed
    when no backend exists (a pen-test finding) unless the operator opts into
    ``agent.sandbox_allow_unsandboxed_exec``. A host without user namespaces —
    ``unshare(CLONE_NEWUSER)`` returning EPERM, common on locked-down dev
    machines — therefore returns exit_code -1 for every hook. Skip rather than
    fail there: the assertions below are about the hook's stdout reaching the
    parser, not about sandbox enforcement.
    """
    try:
        from kiro_crew.sandbox import detect_backend

        return detect_backend() not in ("", "none", None)
    except Exception:
        return False


@pytest.mark.skipif(
    not _sandbox_backend_available(),
    reason="no OS sandbox backend on this host; hook subprocesses cannot be spawned",
)
class TestRealHookProcess:
    """The parser against a REAL hook process, not a stubbed store.

    Everything else in this file stubs the hook store: it proves the runner
    queues what the parser returns, given some stdout. These tests close the
    other half — that an executable Stop hook on disk, run as a real subprocess
    by the real ``ScriptHookStore``, produces stdout the parser accepts. The
    seam they share is the exit-0-stdout list, so together they cover the path
    from hook process to queued turn.
    """

    @staticmethod
    def _write_hook(tmp_path: Path, sentinel: Path) -> str:
        """A real Stop hook: block on first run, silent after.

        Silence on the second run is the terminating condition a gate hook
        relies on, so it is exercised here rather than assumed. Invoked through
        ``sys.executable`` so the test holds on Windows too.
        """
        script = tmp_path / "stop_gate.py"
        script.write_text(
            "import json, pathlib, sys\n"
            "json.load(sys.stdin)\n"  # a real hook consumes the event payload
            f"sentinel = pathlib.Path({str(sentinel)!r})\n"
            "if not sentinel.exists():\n"
            "    sentinel.touch()\n"
            '    print(json.dumps({"decision": "block", "reason": "Read the log first."}))\n',
            encoding="utf-8",
        )
        return f"{sys.executable} {script}"

    @pytest.mark.asyncio
    async def test_a_real_hook_process_yields_a_continuation_then_stops(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        sentinel = tmp_path / "fired.once"
        store.create(
            {
                "name": "stop-gate",
                "event": HOOK_EVENT_STOP,
                "command": self._write_hook(tmp_path, sentinel),
                "timeout": 30,
            }
        )

        first = await store.fire(HOOK_EVENT_STOP, "the final assistant segment")
        assert [r.exit_code for r in first] == [0]
        # The parser consumes exactly what chat_runner's _fire collects: the
        # stdout of exit-0 hooks.
        assert parse_hook_continuations([r.stdout for r in first if r.exit_code == 0]) == [
            "Read the log first."
        ]
        assert sentinel.exists()

        second = await store.fire(HOOK_EVENT_STOP, "the next final segment")
        assert [r.exit_code for r in second] == [0]
        assert parse_hook_continuations([r.stdout for r in second if r.exit_code == 0]) == []

    @pytest.mark.asyncio
    async def test_a_real_hook_that_only_logs_yields_no_continuation(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        script = tmp_path / "noisy.py"
        script.write_text(
            "import json, sys\njson.load(sys.stdin)\nprint('gate: tests passed, allowing stop')\n",
            encoding="utf-8",
        )
        store.create(
            {
                "name": "noisy-gate",
                "event": HOOK_EVENT_STOP,
                "command": f"{sys.executable} {script}",
                "timeout": 30,
            }
        )

        results = await store.fire(HOOK_EVENT_STOP, "done")
        assert [r.exit_code for r in results] == [0]
        assert results[0].stdout.strip() == "gate: tests passed, allowing stop"
        assert parse_hook_continuations([r.stdout for r in results]) == []


class TestStopHookContinuationCount:
    """The Stop stdin payload reports how deep an unbroken hook-continuation run is.

    Kiro's Stop contract defines no cap, so a naive always-block hook would loop
    an unattended session (cron, Slack-away) until a human presses Stop.
    ``hook_continuation_count`` is the number of consecutive hook continuations
    that produced the turn that just ended (0 for a normal turn); the hook may use
    it however it likes — self-limit, diagnose, or surface to the model.
    ``stop_hook_active`` is the derived convenience boolean (``count > 0``). The
    contract defines neither field, so both are additive advisory signals, not
    conformance requirements.
    """

    @staticmethod
    async def _fire_capturing_event(tmp_path, event: str, **fire_kwargs) -> dict:
        """Fire one hook for ``event`` and return the stdin payload it was given.

        ``run_script_hook`` is patched so no subprocess runs (host-independent):
        it records the event dict ``fire()`` built and returns a benign result.
        """
        store = ScriptHookStore(tmp_path)
        store.create({"name": "probe", "event": event, "command": "true", "timeout": 30})
        seen: dict = {}

        async def _capture(hook, context, hook_event):
            seen.clear()
            seen.update(hook_event)
            return SimpleNamespace(
                exit_code=0, stdout="", stderr="", hook_name=hook.name, duration_ms=0
            )

        with patch("kiro_crew.hooks.run_script_hook", side_effect=_capture):
            await store.fire(event, "the final assistant segment", **fire_kwargs)
        return seen

    @pytest.mark.asyncio
    async def test_stop_payload_reports_the_iteration_count(self, tmp_path) -> None:
        seen = await self._fire_capturing_event(
            tmp_path, HOOK_EVENT_STOP, hook_continuation_count=2
        )

        assert seen["hook_continuation_count"] == 2
        assert seen["stop_hook_active"] is True

    @pytest.mark.asyncio
    async def test_stop_payload_zero_count_is_inactive(self, tmp_path) -> None:
        """A first, human-initiated turn ending is iteration 0."""
        seen = await self._fire_capturing_event(tmp_path, HOOK_EVENT_STOP)

        assert seen["hook_continuation_count"] == 0
        assert seen["stop_hook_active"] is False

    @pytest.mark.asyncio
    async def test_non_stop_events_carry_no_continuation_fields(self, tmp_path) -> None:
        """The fields are meaningful only for Stop; other events must not gain them."""
        from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT

        seen = await self._fire_capturing_event(tmp_path, HOOK_EVENT_USER_PROMPT_SUBMIT)

        assert "hook_continuation_count" not in seen
        assert "stop_hook_active" not in seen

    @staticmethod
    def _stop_counts(state) -> list:
        """The hook_continuation_count each Stop fire was given, in order."""
        return [
            c.kwargs.get("hook_continuation_count")
            for c in state._hook_store.fire.call_args_list
            if c.args and c.args[0] == HOOK_EVENT_STOP
        ]

    @pytest.mark.asyncio
    async def test_count_increments_across_consecutive_continuations(self, tmp_path) -> None:
        """A human turn is 0; each consecutive hook continuation is one deeper."""
        state, slot, run_chat = _harness(tmp_path, "checked 3 files, all clean")
        cont = f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nkeep going"

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await run_chat(state, slot, "an ordinary user turn")
            await run_chat(state, slot, cont)
            await run_chat(state, slot, cont)

        assert self._stop_counts(state) == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_count_resets_after_a_non_continuation_turn(self, tmp_path) -> None:
        """A real user turn breaks the run, so the next depth starts from 0 again."""
        state, slot, run_chat = _harness(tmp_path, "checked 3 files, all clean")
        cont = f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nkeep going"

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await run_chat(state, slot, cont)
            await run_chat(state, slot, "a fresh user message")

        assert self._stop_counts(state) == [1, 0]
