"""A turn that promises a LATER follow-up must say so when nothing will wake it.

The incident: the agent ends a turn with "I'll report back when the build
finishes", sets up no monitor, scheduled job or background run, and the session
then sits idle until the person writes again. The runner now appends one notice
saying no follow-up is scheduled. It never re-runs the turn and never arms a
wake on the model's behalf.

Three layers, each tested here: the text detector
(``has_deferred_followup_promise``), the tool-call check
(``turn_armed_a_wake``), and the runtime probe (``background_wake_absent``),
plus the runner arm that combines them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_utils


def _promise(text: str) -> bool:
    detector = getattr(chat_utils, "has_deferred_followup_promise", None)
    assert detector is not None, "the deferred follow-up detector is missing"
    return bool(detector(text))


def _armed(*tool_names: str) -> bool:
    check = getattr(chat_utils, "turn_armed_a_wake", None)
    assert check is not None, "the wake-arming tool check is missing"
    return bool(check(tuple((f"tc-{i}", "", name, True) for i, name in enumerate(tool_names))))


# --- the text detector -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    (
        "The build is running. I'll report back when it finishes.",
        "I'll let you know once the pipeline is green.",
        "I will keep an eye on the deployment and ping you if it fails.",
        "Kicked off the tests. I'll get back to you with the results.",
        "I’ll follow up when CI completes.",
        "I'm going to watch the pipeline and act when the condition is met.",
        "I'll then circle back with a summary.",
        "I'll report back when it's done. It usually takes about 20 minutes.",
        "I'll post an update here when the canary finishes.",
    ),
)
def test_deferred_followup_promise_is_detected(text: str) -> None:
    assert _promise(text)


@pytest.mark.parametrize(
    "text",
    (
        "",
        "   ",
        "The build passed and the PR is open.",
        # The person's next message is the trigger: nothing needs to wake.
        "Let me know when you're ready and I'll report back.",
        "Reply here and I'll follow up.",
        "If you want, I'll monitor it.",
        "Want me to keep an eye on it?",
        "If you'd like, I'll set up a monitor to watch the build.",
        "If you would prefer, I'll keep an eye on it.",
        "I'll monitor it if you would like.",
        "I'd be happy to monitor it if you would like.",
        # watch/monitor/poll as a noun or a later clause is not the commitment.
        "Next I'll explain how to poll the API.",
        "I'll add a monitor section to the docs.",
        # Negated: not a promise.
        "I won't monitor this one; the deploy is already done.",
        "I can't watch the pipeline from here.",
        # A plan stated early, then delivered: only the closing sentences count.
        "I'll report back on each step. Step one passed. Step two passed. All done.",
        # Quoted text and button labels are not the agent's own commitment.
        "Here is the old reply:\n```\nI'll report back when it finishes.\n```",
        "Done.\n[OPTIONS: I'll check back later | Merge it now]",
        # A colon lead-in delivers the promised update in the same reply.
        "I'll give you a summary: the build passed and all twelve tests passed.",
        "I'll report back: CI is green on the new head.",
        # Third person: a monitor that really runs on its own.
        "The monitor will report back when CI finishes.",
    ),
)
def test_non_promises_are_not_detected(text: str) -> None:
    assert not _promise(text)


# --- the tool-call check -----------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    (
        "monitor_start",
        "monitor_watch",
        "cron_add",
        "spawn_run",
        "workflow_run",
        "register_hook",
        # Server-qualified spellings different backends use.
        "mcp__kirocrew-core__monitor_start",
        "kirocrew-cron/cron_add",
        "@kirocrew-core/spawn_run",
    ),
)
def test_wake_arming_tool_counts(tool_name: str) -> None:
    assert _armed("read", tool_name)


def test_ordinary_tools_and_wait_do_not_count_as_a_wake() -> None:
    # ``wait`` blocks inside the turn; nothing is left to wake the session after it.
    assert not _armed()
    assert not _armed("read", "shell", "grep", "wait", "spawn_steer")


def test_malformed_identity_fails_toward_armed() -> None:
    # A wrong True only hides a notice; a wrong False claims nothing is scheduled.
    assert chat_utils.turn_armed_a_wake((("tc-1", "", None, True),))  # type: ignore[arg-type]
    assert chat_utils.turn_armed_a_wake(("not-a-tuple",))  # type: ignore[arg-type]


def test_unnamed_tool_call_fails_toward_armed() -> None:
    # claude-agent-acp and opencode frames carry no tool name, so a ``task_run``
    # there reaches this check as ``""``: it must not read as "nothing armed".
    assert chat_utils.turn_armed_a_wake((("tc-1", "", "", False),))
    assert chat_utils.turn_armed_a_wake((("tc-1", "", "kirocrew-core/", False),))


# --- the runtime probe -------------------------------------------------------


def _probe_state(*, jobs=(), subagents_attached=False, workflow_pending=False):
    state = SimpleNamespace()
    state.subagents = MagicMock()
    state.subagents.running_agents_for.return_value = ["child"] if subagents_attached else []
    state.subagents._queued_depth.return_value = 0
    state.subagents.has_in_memory_pending_work_for.return_value = False
    state.subagents.queued_count_for_async = AsyncMock(return_value=0)
    state.workflow_service = SimpleNamespace(
        registry=SimpleNamespace(has_pending_work_for=lambda _k: workflow_pending)
    )
    state.crons = SimpleNamespace(list_jobs_async=AsyncMock(return_value=list(jobs)))
    return state


def _probe_slot(key: str = "chat-77"):
    return SimpleNamespace(key=key, _pending_synthesis=False, _subagent_deliveries_inflight=0)


def _absent(state, slot, *, loop=None) -> bool:
    service = SimpleNamespace(get_by_slot=lambda _k: loop)
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: s.key),
    ):
        return asyncio.run(chat_utils.background_wake_absent(state, slot))


def test_probe_reports_absent_when_nothing_owns_a_later_turn() -> None:
    assert _absent(_probe_state(), _probe_slot())


def test_probe_sees_an_active_monitor() -> None:
    assert not _absent(_probe_state(), _probe_slot(), loop=SimpleNamespace(active=True))
    assert _absent(_probe_state(), _probe_slot(), loop=SimpleNamespace(active=False))


def test_probe_sees_a_cron_job_this_session_created() -> None:
    mine = SimpleNamespace(session_key="chat-77")
    other = SimpleNamespace(session_key="chat-12")
    assert not _absent(_probe_state(jobs=[other, mine]), _probe_slot())
    assert _absent(_probe_state(jobs=[other]), _probe_slot())


def test_probe_sees_the_cron_job_whose_runs_land_in_this_tab() -> None:
    # A ``cron-<id>`` tab is linked to ``cron:<id>``; the job was created elsewhere.
    job = SimpleNamespace(id="abc123", session_key="dashboard:chat-1")
    tab = _probe_slot("cron-abc123")
    with patch("kiro_crew.dashboard.chat_utils.effective_session_key", lambda _s: "cron:abc123"):
        service = SimpleNamespace(get_by_slot=lambda _k: None)
        with patch("kiro_crew.autonudge.get_instance", return_value=service):
            assert not asyncio.run(chat_utils.background_wake_absent(_probe_state(jobs=[job]), tab))


def test_probe_sees_children_workflows_and_synthesis() -> None:
    assert not _absent(_probe_state(subagents_attached=True), _probe_slot())
    assert not _absent(_probe_state(workflow_pending=True), _probe_slot())
    slot = _probe_slot()
    slot._pending_synthesis = True
    assert not _absent(_probe_state(), slot)


def test_probe_sees_a_pending_follow_up_watcher() -> None:
    # A ``spawn_steer(mode="follow_up")`` watcher outlives its child's run.
    state = _probe_state()
    state.subagents.has_in_memory_pending_work_for.return_value = True
    assert not _absent(state, _probe_slot())
    # An unreadable answer is not a definite "no pending work".
    state.subagents.has_in_memory_pending_work_for.return_value = None
    assert not _absent(state, _probe_slot())


def test_probe_sees_a_sub_agent_result_waiting_in_the_slot_queue() -> None:
    # The child finished and left the registry; its completion event waits in
    # the slot queue and starts the next turn once this one ends.
    from kiro_crew.dashboard.chat_utils_injected_rows import SUBAGENT_COMPLETION_KIND

    slot = _probe_slot()
    slot._queue = [{"kind": SUBAGENT_COMPLETION_KIND}]
    assert not _absent(_probe_state(), slot)
    slot._queue = []
    assert _absent(_probe_state(), slot)


def test_probe_sees_a_cron_notification_waiting_in_the_slot_queue() -> None:
    # A one-shot job fired during the turn, queued its notification and was
    # removed, so the job list does not name it; the queued entry still
    # starts the next turn.
    slot = _probe_slot()
    slot._queue = [{"kind": "cron_notification", "content": "job finished"}]
    assert not _absent(_probe_state(jobs=()), slot)


def test_probe_failure_never_claims_nothing_is_scheduled() -> None:
    state = _probe_state()
    state.crons.list_jobs_async = AsyncMock(side_effect=OSError("store unreadable"))
    assert not _absent(state, _probe_slot())


# --- the Stop-hook check -----------------------------------------------------


def _stop_check(*, store_hooks=(), spec_hooks=(), store=True) -> bool:
    from kiro_crew.dashboard.chat_turn.turn_ending import _stop_hook_configured

    hook_store = MagicMock(list_all=MagicMock(return_value=list(store_hooks))) if store else None
    turn = SimpleNamespace(
        state=SimpleNamespace(_hook_store=hook_store), _spec_hooks=list(spec_hooks)
    )
    return _stop_hook_configured(turn)


def test_stop_hook_check_counts_stored_and_spec_stop_hooks() -> None:
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_STOP

    stop = SimpleNamespace(enabled=True, event=HOOK_EVENT_STOP)
    off = SimpleNamespace(enabled=False, event=HOOK_EVENT_STOP)
    other = SimpleNamespace(enabled=True, event=HOOK_EVENT_PRE_TOOL_USE)
    assert _stop_check(store_hooks=[stop])
    assert _stop_check(spec_hooks=[stop])
    assert not _stop_check(store_hooks=[off, other])
    # No store: _fire runs no hook at all, spec hooks included.
    assert not _stop_check(spec_hooks=[stop], store=False)


def test_unreadable_hook_store_counts_as_a_stop_hook() -> None:
    from kiro_crew.dashboard.chat_turn.turn_ending import _stop_hook_configured

    store = MagicMock(list_all=MagicMock(side_effect=OSError("unreadable")))
    turn = SimpleNamespace(state=SimpleNamespace(_hook_store=store), _spec_hooks=[])
    assert _stop_hook_configured(turn)


# --- the runner arm ----------------------------------------------------------


class TestRunnerNotice:
    """The runner appends the notice only when the promise is real and unbacked."""

    def _state_and_slot(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()
        slot = state.get_or_create_slot("deferred-followup-slot")
        slot.append("user", "watch the build", "msg msg-u")
        client = state.sessions.get_or_create.return_value[0]
        client.is_kiro_backend = True
        client.context_usage_pct = MagicMock(return_value=50.0)
        client.shutdown = AsyncMock()
        return state, slot, client, _run_chat

    async def _run(
        self,
        tmp_path: Path,
        text: str,
        tool_name: str = "",
        *,
        absent: bool = True,
        stop_hook: bool = False,
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._state_and_slot(tmp_path)
        if stop_hook:
            from kiro_crew.hooks import HOOK_EVENT_STOP

            # An enabled Stop hook that does not continue: the chain cannot
            # know that yet when it decides the notice.
            state._hook_store = MagicMock()
            state._hook_store.list_all = MagicMock(
                return_value=[SimpleNamespace(enabled=True, event=HOOK_EVENT_STOP)]
            )
            state._hook_store.fire = AsyncMock(return_value=[])

        async def _stream(_msg):
            if tool_name:
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title=tool_name,
                    tool_kind="other",
                    tool_input="{}",
                    tool_call_id="tc-1",
                    tool_name=tool_name,
                    mcp_server_name="kirocrew-core",
                    tool_identity_trusted=True,
                )
                yield LLMEvent(
                    kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_output="ok", tool_final=True
                )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream
        with (
            patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "kiro_crew.dashboard.chat_runner.background_wake_absent",
                new=AsyncMock(return_value=absent),
            ),
        ):
            await _run_chat(state, slot, "watch the build", _directive_user_origin=True)
            tasks = list(state._background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        return slot

    @staticmethod
    def _notices(slot) -> list[str]:
        return [m["content"] for m in slot.messages if m.get("role") == "notice"]

    @pytest.mark.asyncio
    async def test_unbacked_promise_gets_the_notice(self, tmp_path: Path) -> None:
        slot = await self._run(
            tmp_path, "The build is started. I'll report back when it finishes.", "shell"
        )
        assert chat_utils.DEFERRED_FOLLOWUP_NOTICE in self._notices(slot)
        # Notice-only: nothing is re-queued.
        assert not slot._queue

    @pytest.mark.asyncio
    async def test_promise_backed_by_a_monitor_this_turn_is_quiet(self, tmp_path: Path) -> None:
        slot = await self._run(
            tmp_path, "Monitor armed. I'll report back when it finishes.", "monitor_start"
        )
        assert chat_utils.DEFERRED_FOLLOWUP_NOTICE not in self._notices(slot)

    @pytest.mark.asyncio
    async def test_promise_backed_by_existing_background_work_is_quiet(
        self, tmp_path: Path
    ) -> None:
        slot = await self._run(
            tmp_path, "I'll report back when it finishes.", "shell", absent=False
        )
        assert chat_utils.DEFERRED_FOLLOWUP_NOTICE not in self._notices(slot)

    @pytest.mark.asyncio
    async def test_promise_with_a_stop_hook_configured_is_quiet(self, tmp_path: Path) -> None:
        # The Stop hook fires after the ending chain and may continue the turn.
        slot = await self._run(
            tmp_path,
            "The build is started. I'll report back when it finishes.",
            "shell",
            stop_hook=True,
        )
        assert chat_utils.DEFERRED_FOLLOWUP_NOTICE not in self._notices(slot)

    @pytest.mark.asyncio
    async def test_finished_answer_is_quiet(self, tmp_path: Path) -> None:
        slot = await self._run(tmp_path, "The build passed and the PR is open.", "shell")
        assert chat_utils.DEFERRED_FOLLOWUP_NOTICE not in self._notices(slot)
