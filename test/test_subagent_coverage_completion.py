"""Coverage tests for what ``kiro_crew.subagent`` does once a run finishes.

The injection-failure notice and its outcome-aware copy, continuable-conversation
retention (keep, promote, release, sweep), wave and batch bookkeeping with the
done-but-unreported hold that keeps a wave open, digest-hold release and the
announce path. Every process boundary is stubbed, as in
``test_subagent_coverage.py``: no test here launches a binary or opens a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_coverage_helpers import _info, _manager, _sessions

from kiro_crew import subagent as sa
from kiro_crew.subagent import SubagentDelivery, SubagentInfo, SubagentManager

# Ceiling for a wait on an announce task the test ends itself: far above what the
# in-memory work needs, so a regression that keeps it running fails at the wait by
# name instead of hanging the worker.
_ANNOUNCE_WAIT_SECS = 60.0


# ── Manager: injection-failure notice ─────────────────────────────────────


class TestNotifyInjectionFailed:
    def test_parent_without_a_tab_is_skipped(self) -> None:
        on_event = AsyncMock()
        mgr = _manager(on_event=on_event)
        info = _info(parent_session_key="cron:1")
        with patch(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=""
        ) as slot_key:
            mgr.notify_injection_failed(info)
        slot_key.assert_called_once_with("cron:1")
        # The record still learns its report was not delivered; only the notice is skipped.
        assert info._report_undelivered is True
        on_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_queues_failure_for_the_next_turn(self, tmp_path: Path) -> None:
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        result = tmp_path / "result.txt"
        result.write_text("hello", newline="\n")
        mgr = _manager(on_event=_on_event)
        info = _info(parent_session_key="dash:1", result_path=str(result))
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info)
        # flake-ok: two zero-delay yields let the task notify_injection_failed queued run first; moved verbatim
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen and seen[0]["slot"] == "slot-1"
        assert "Result saved at" in seen[0]["failure_msg"]

    @pytest.mark.asyncio
    async def test_unreadable_result_path_omits_size(self, tmp_path: Path) -> None:
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        mgr = _manager(on_event=_on_event)
        info = _info(parent_session_key="dash:1", result_path=str(tmp_path / "absent.txt"))
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info)
        # flake-ok: two zero-delay yields let the task notify_injection_failed queued run first; moved verbatim
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen and "bytes" not in seen[0]["failure_msg"]

    def test_lookup_failure_is_swallowed(self) -> None:
        on_event = AsyncMock()
        mgr = _manager(on_event=on_event)
        info = _info(parent_session_key="dash:1")
        with patch(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
            side_effect=RuntimeError("no dashboard"),
        ) as slot_key:
            mgr.notify_injection_failed(info)
        slot_key.assert_called_once_with("dash:1")
        assert info._report_undelivered is True
        on_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_teardown_cancelled_run_announces_nothing(self, tmp_path: Path) -> None:
        """A run whose parent ended must not queue a failure into that parent.

        The notice is drained into the LLM's context on the parent key's next
        turn, so a queued one outlives the conversation it describes.
        """
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        mgr = _manager(on_event=_on_event)
        info = _info(parent_session_key="dash:1")
        mgr._teardown_cancelled_ids.add(info.id)
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info)
        # flake-ok: two zero-delay yields let the task notify_injection_failed queued run first; moved verbatim
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_teardown_cancelled_member_arms_no_digest_flush(self) -> None:
        """The hold-expiry sweep must not arm a flush for a wave whose parent ended.

        A wave member parks its siblings' announces on its own digest. When the hold
        ages out the reaper forces a partial flush -- and that flush is a SYNTHETIC
        record with a fresh id, so the delivery gate keyed on run ids can never
        recognise it. It would reach ``_on_done`` on its own and rebuild the retired
        parent's conversation minutes after the teardown. The member is therefore
        skipped at the source, in the expiry scan.
        """
        import kiro_crew.subagent as _facade

        mgr = _manager(on_done=AsyncMock())
        held = _info(parent_session_key="dash:1")
        held.batch_id = "wave-1"
        held.batch_total = 2
        held._digest_held_at = 1.0
        mgr._agents[held.id] = held

        scan = mgr._waves._expired_digest_holds
        with patch.object(_facade, "DIGEST_HOLD_SECS", 5.0):
            assert scan(1000.0), (
                "the scan found no expiry for an aged hold, so this test is not "
                "exercising the gate it claims to"
            )
            mgr._teardown_cancelled_ids.add(held.id)
            assert scan(1000.0) == []

    @pytest.mark.asyncio
    async def test_a_suppressed_report_drops_its_own_digest_hold(self) -> None:
        """Skipping the delivery also releases what the run was holding.

        Leaving ``_digest_held_at`` set would let the next expiry sweep arm the flush
        this run's own suppression exists to prevent, and leaving
        ``_digest_settle_deliveries``
        on the record would leave its held siblings with an ungated delivery. The
        siblings are marked, not tombstoned: their results never reached a parent, so
        orphan reconciliation must still be able to find them.
        """
        mgr = _manager(on_done=AsyncMock())
        info = _info(parent_session_key="dash:1")
        info.done = False
        info._digest_held_at = 1.0
        info._digest_settle_deliveries = [
            SubagentDelivery("sib-1", 1.0, 0.1),
            SubagentDelivery("sib-2", 2.0, 0.2),
        ]
        mgr._agents[info.id] = info
        mgr._teardown_cancelled_ids.add(info.id)

        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="delivery timed out",
            mark_delivered_on_success=False,
        )

        assert info._digest_held_at == 0.0
        assert info._digest_settle_deliveries == []
        assert "sib-1" in mgr._teardown_cancelled_ids
        assert "sib-2" in mgr._teardown_cancelled_ids
        mgr._on_done.assert_not_awaited()

    def test_the_teardown_gate_forgets_by_age_and_never_by_count(self) -> None:
        """Eviction is AGE, because a capacity rule can drop a live gate.

        One parent with more queued children than any capacity would evict its own
        earliest ids while their reports were still being spawned, and those reports then
        walk through the gate and rebuild the conversation the teardown took down. An age
        rule cannot: the TTL exceeds every window in which a marked run has an announce
        left. A read must not extend an entry's life either, or the TTL stops describing
        what is retained.
        """
        from kiro_crew.subagent import _AgingIdSet

        ids = _AgingIdSet(3600.0)
        ids.update(str(n) for n in range(10_000))
        assert len(ids) == 10_000, "a count rule evicted ids whose runs can still announce"
        assert "0" in ids

        aged = _AgingIdSet(1.0)
        # flake-ok: pins subagent's monotonic clock for one synchronous _AgingIdSet call in a sync test that starts no loop or thread; moved verbatim
        with patch("kiro_crew.subagent.time.monotonic", return_value=1_000.0):
            aged.update(["old-1", "old-2"])
        # flake-ok: pins subagent's monotonic clock for one synchronous _AgingIdSet call in a sync test that starts no loop or thread; moved verbatim
        with patch("kiro_crew.subagent.time.monotonic", return_value=1_000.5):
            assert "old-1" in aged  # a read does not refresh it
        # flake-ok: pins subagent's monotonic clock for one synchronous _AgingIdSet call in a sync test that starts no loop or thread; moved verbatim
        with patch("kiro_crew.subagent.time.monotonic", return_value=1_100.0):
            aged.add("new-1")
        assert list(aged) == ["new-1"]

    @pytest.mark.asyncio
    async def test_a_suppressed_report_discards_its_own_gate_entry(self) -> None:
        """The gate keeps an id until that run's delivery is suppressed, not for a span.

        Age is only the backstop. Once the report has been dropped, ``_on_done`` was never
        called, so none of the gateway's injection paths can fire for this run either --
        the entry has done its job. Leaving it to expire instead made the gate depend on a
        TTL in the ordinary case, and an approval-parked run (deliberately not cancelled,
        because the approval is a person's to answer) can outlive any short one.
        """
        mgr = _manager(on_done=AsyncMock())
        info = _info(parent_session_key="dash:1")
        info.done = False
        mgr._agents[info.id] = info
        mgr._teardown_cancelled_ids.add(info.id)

        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="delivery timed out",
            mark_delivered_on_success=False,
        )

        mgr._on_done.assert_not_awaited()
        assert info.id not in mgr._teardown_cancelled_ids, (
            "the gate entry outlived the suppression it existed for, so the set depends on "
            "its age backstop even on the ordinary path"
        )

    @pytest.mark.usefixtures("healthy_host_memory")
    @pytest.mark.asyncio
    async def test_a_rejected_spawn_approval_after_teardown_does_not_recreate_the_conversation(
        self,
    ) -> None:
        """The REJECTION path announces too, and it never reached ``_report_terminal``.

        A spawn parked on its approval is marked by the teardown and deliberately not
        cancelled, because the approval is a person's to answer. When that person then
        DECLINES, the spawn gate takes its own terminal exit: it claims the finalize and
        calls ``_safe_announce`` directly, without passing through the terminal report
        where the delivery gate lives. So the one representation the teardown is certain
        to have marked walked straight into ``_on_done``, which resolves the parent key
        through the session registry and CREATES a session when none is live -- rebuilding
        the conversation the teardown had just taken down and seeding it with the refusal
        of a spawn that key never asked for.

        Driven through the real gate rather than a hand-built record: ``spawn`` registers,
        the approval callback parks, ``snapshot_teardown_children`` arms the mark exactly
        as ``session_lifecycle`` does, and only then is the prompt answered ``False``.
        """
        approval_gate = asyncio.Event()

        async def _park_then_decline(
            _request_id: str, _description: str, _parent: str = ""
        ) -> bool:
            # flake-ok: the test sets approval_gate itself further down, so this wait always ends; moved verbatim
            await approval_gate.wait()
            return False

        sessions = _sessions()
        # "ask" is what makes the spawn consult the interactive callback at all;
        # the agent seams below are what let vetting pass on a bare double.
        sessions.get_approval_policy = MagicMock(return_value="ask")
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("built", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = False

        mgr = _manager(
            sessions=sessions,
            ctx_builder=ctx,
            on_spawn_approval=_park_then_decline,
            on_done=AsyncMock(),
            is_yolo=lambda: False,
        )
        info = mgr.spawn("t", parent_session_key="dash:1")
        assert info is not None
        for _ in range(20):
            # flake-ok: twenty zero-delay yields let the spawned run reach its parked approval; moved verbatim
            await asyncio.sleep(0)
        registered = mgr._agents.get(info.id)
        assert registered is not None, f"spawn did not register: {info.error!r}"
        assert registered._awaiting_approval is True, "precondition: parked on its prompt"

        # The teardown's synchronous half. It returns nothing to cancel for a parked
        # run and marks it for delivery suppression instead -- that split is the point.
        assert mgr.snapshot_teardown_children("dash:1") == ()
        assert info.id in mgr._teardown_cancelled_ids

        approval_gate.set()
        for _ in range(200):
            # flake-ok: bounded zero-delay yields until the declined run reports done; moved verbatim
            await asyncio.sleep(0)
            if registered.done:
                break

        assert registered.done is True, "the decline did not reach its terminal exit"
        mgr._on_done.assert_not_awaited()

        for task in list(mgr._tasks.values()):
            task.cancel()
        # flake-ok: one zero-delay yield lets the cancelled tasks unwind; moved verbatim
        await asyncio.sleep(0)

    def test_the_gate_backstop_outlasts_a_human_approval_window(self) -> None:
        """The age backstop must not prune a run that is waiting on a person.

        An approval-parked run is deliberately not cancelled, so it can sit for as long as
        the person takes. An hour let a later teardown prune its mark while it waited, and
        the completion then injected into whatever the key served by then.
        """
        from kiro_crew.subagent import _TEARDOWN_GATE_TTL_SECS

        assert _TEARDOWN_GATE_TTL_SECS >= 86400.0, (
            "the backstop is shorter than a person plausibly takes to answer an approval, "
            f"so a waiting run's mark can be pruned before it completes: {_TEARDOWN_GATE_TTL_SECS}"
        )

    @pytest.mark.asyncio
    async def test_a_sibling_of_a_teardown_cancelled_run_still_announces(self) -> None:
        """The gate is per-id: a run the teardown did not touch is unaffected."""
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        mgr = _manager(on_event=_on_event)
        mgr._teardown_cancelled_ids.add("some-other-agent")
        info = _info(parent_session_key="dash:1")
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info)
        # flake-ok: two zero-delay yields let the task notify_injection_failed queued run first; moved verbatim
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen and seen[0]["slot"] == "slot-1"


# ── Injection-failure notice: outcome-aware copy ──────────────────────────


class TestInjectionNoticeOutcome:
    """The pure helper maps a terminal record to a truthful outcome line."""

    def test_completed_states_the_outcome_without_a_mechanism(self) -> None:
        info = _info(done=True, result="ok")
        line = sa._injection_notice_outcome(info)
        assert line == "The agent finished, but its result could not be delivered."
        # The cause belongs to ``reason``, printed on the line above this one.
        assert "timed out" not in line

    def test_failed_run_does_not_claim_finished(self) -> None:
        info = _info(done=True, error="Timed out after 30 minutes", _exec_started=123.0)
        line = sa._injection_notice_outcome(info)
        assert line == "The agent failed before a result could be delivered."

    def test_stopped_mid_run_reads_as_stopped(self) -> None:
        info = _info(
            done=True, user_stopped=True, _exec_started=123.0, tool_count=3, result="partial"
        )
        assert sa._injection_notice_outcome(info) == "The run was stopped before it completed."

    def test_stopped_in_startup_window_is_not_before_start(self) -> None:
        # Execution began (_exec_started set) but no turn, tool call, or output
        # landed yet — the run DID start, so the before-start copy would lie.
        info = _info(done=True, user_stopped=True, _exec_started=123.0)
        assert sa._injection_notice_outcome(info) == "The run was stopped before it completed."

    def test_stopped_before_execution_says_it_never_ran(self) -> None:
        # A stop landing on a registered run before _run_inner ever executed:
        # no _exec_started marker and no output of any kind.
        info = _info(done=True, user_stopped=True)
        assert sa._injection_notice_outcome(info) == (
            "The run was stopped before it started, so there is no result to deliver."
        )

    def test_rejections_read_as_failed_before_start(self) -> None:
        # Every spawn-rejection site constructs its record without
        # _exec_started, so the never-ran refinement covers them all — the
        # sentinel-worded ones AND the unknown-agent refusal, whose exact
        # wording is consumed by _is_unknown_agent_refusal and cannot change.
        for err in (
            "spawn rejected",
            "spawn refused: only 1.0 GB memory available (need 4 GB)",
            "agent 'ghost' not found; available: scout, probe",
        ):
            info = _info(done=True, error=err)
            assert sa._injection_notice_outcome(info) == (
                "The run failed before it started, so there is no result to deliver."
            ), err

    def test_error_with_output_keeps_the_recovery_hint_honest(self) -> None:
        # Defensive: a record carrying output must never be described as
        # having no result to deliver — the generic failed copy stays truthful.
        info = _info(done=True, error="spawn rejected", result_path="/tmp/r.txt")
        assert sa._injection_notice_outcome(info) == (
            "The agent failed before a result could be delivered."
        )


class TestInjectionNoticeDoesNotContradictItsReason:
    """A completed run's outcome line must not name a mechanism.

    ``notify_injection_failed`` prints ``reason`` one line above the outcome
    line, and most of its callers pass something that is not a timeout:
    ``slack/gateway.py`` sends "provider dead after prompt-busy retries",
    "ACP process died", a raw ``str(exception)`` from a failed injection turn,
    and the last injection-failure reason after the attempt cap. A completed
    branch that asserts "delivery timed out" contradicts every one of them, in
    a message whose reader is the LLM deciding what to do next.
    """

    #: The reasons the four non-timeout call sites actually pass, verbatim.
    NON_TIMEOUT_REASONS = [
        "provider dead after prompt-busy retries",
        "ACP process died",
        "AcpError: stream closed while waiting for result",
        "no active session for parent",
    ]

    async def _notice_for(self, info: SubagentInfo, reason: str) -> str:
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        mgr = _manager(on_event=_on_event)
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info, reason=reason)
        # flake-ok: two zero-delay yields let the task notify_injection_failed queued run first; moved verbatim
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen, "expected a subagent_injection_failed event"
        return str(seen[0]["failure_msg"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", NON_TIMEOUT_REASONS)
    async def test_a_non_timeout_reason_is_not_overridden_by_the_outcome_line(
        self, reason: str
    ) -> None:
        info = _info(parent_session_key="dash:1", done=True, result="ok", _exec_started=123.0)
        msg = await self._notice_for(info, reason)
        assert reason in msg, "the caller's reason must still be shown"
        assert (
            "timed out" not in msg
        ), f"the notice says the delivery timed out while its own reason says {reason!r}"
        assert "The agent finished, but its result could not be delivered." in msg

    @pytest.mark.asyncio
    async def test_a_real_timeout_still_reads_as_one(self) -> None:
        """Neutral copy loses nothing: the timeout callers say so in ``reason``."""
        info = _info(parent_session_key="dash:1", done=True, result="ok", _exec_started=123.0)
        msg = await self._notice_for(info, "injection timed out after 300s")
        assert "injection timed out after 300s" in msg
        assert "The agent finished, but its result could not be delivered." in msg

    @pytest.mark.asyncio
    async def test_the_result_recovery_hint_still_agrees_with_the_line(self) -> None:
        """ "could not be delivered" must not read as "there is nothing to read":
        when a result file exists the notice still points at it."""
        info = _info(
            parent_session_key="dash:1",
            done=True,
            result="ok",
            result_path="/tmp/subagent-result.txt",
            _exec_started=123.0,
        )
        with patch("os.path.getsize", return_value=42):
            msg = await self._notice_for(info, "ACP process died")
        assert "The agent finished, but its result could not be delivered." in msg
        assert "/tmp/subagent-result.txt" in msg
        assert "Use the read tool" in msg


class TestNotifyInjectionFailedOutcomeCopy:
    """End-to-end: the queued failure message carries the outcome-aware line.

    Four regression paths: stop before execution, approval rejection, queued
    rejection, and the ordinary post-run delivery timeout (whose copy is
    unchanged).
    """

    async def _notice_for(self, info: SubagentInfo) -> str:
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        mgr = _manager(on_event=_on_event)
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info)
        # flake-ok: two zero-delay yields let the task notify_injection_failed queued run first; moved verbatim
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen, "expected a subagent_injection_failed event"
        return str(seen[0]["failure_msg"])

    @pytest.mark.asyncio
    async def test_stop_before_execution_does_not_claim_the_agent_finished(self) -> None:
        # A user stop landing on a registered run before execution began marks
        # the record user_stopped with no _exec_started and no output: it never
        # ran. (A cancel on a spawn still WAITING in the stagger queue is
        # unqueued without a record and never reaches this notice at all.)
        info = _info(parent_session_key="dash:1", done=True, user_stopped=True)
        msg = await self._notice_for(info)
        assert "The run was stopped before it started" in msg
        assert "finished" not in msg

    @pytest.mark.asyncio
    async def test_approval_rejection_reads_as_never_started(self) -> None:
        info = _info(parent_session_key="dash:1", done=True, error="spawn rejected")
        msg = await self._notice_for(info)
        assert "The run failed before it started" in msg
        assert "finished" not in msg

    @pytest.mark.asyncio
    async def test_queued_rejection_reads_as_never_started_without_mechanism_detail(self) -> None:
        info = _info(
            parent_session_key="dash:1",
            done=True,
            error="spawn refused: only 1.0 GB memory available (need 4 GB)",
        )
        msg = await self._notice_for(info)
        assert "The run failed before it started" in msg
        # Mechanism details stay out of the user-facing notice.
        assert "GB" not in msg
        assert "finished" not in msg

    @pytest.mark.asyncio
    async def test_post_run_delivery_failure_keeps_the_completed_copy_and_hint(
        self, tmp_path: Path
    ) -> None:
        result = tmp_path / "result.txt"
        result.write_text("hello", newline="\n")
        info = _info(
            parent_session_key="dash:1", done=True, result="hello", result_path=str(result)
        )
        msg = await self._notice_for(info)
        assert "The agent finished, but its result could not be delivered." in msg
        assert "Result saved at" in msg


# ── Manager: continuable conversations ────────────────────────────────────


class TestConversationBusy:
    def test_live_run_is_busy(self) -> None:
        mgr = _manager()
        mgr._agents["c1"] = _info("c1")
        busy = mgr._conversation_busy("subagent:c1")
        assert busy is not None and busy.id == "c1"

    def test_queued_continuation_is_busy(self) -> None:
        mgr = _manager()
        mgr._queue = [{"conversation_key": "subagent:c1", "_preassigned_id": "q1"}]
        busy = mgr._conversation_busy("subagent:c1")
        assert busy is not None and busy.queued is True

    def test_queued_without_explicit_key_matches_preassigned_id(self) -> None:
        mgr = _manager()
        mgr._queue = [{"_preassigned_id": "q1"}]
        assert mgr._conversation_busy("subagent:q1") is not None

    def test_idle_conversation_is_not_busy(self) -> None:
        assert _manager()._conversation_busy("subagent:nope") is None


class TestKeepRecordedOnDisk:
    def test_non_subagent_key_short_circuits(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state") as read:
            assert mgr._keep_recorded_on_disk("dash:1") is False
        read.assert_not_called()

    def test_keep_true_recorded(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", return_value={"keep": True}):
            assert mgr._keep_recorded_on_disk("subagent:c1") is True

    def test_missing_state_is_false(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", return_value=None):
            assert mgr._keep_recorded_on_disk("subagent:c1") is False

    def test_read_failure_is_false(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", side_effect=OSError):
            assert mgr._keep_recorded_on_disk("subagent:c1") is False


class TestPromoteConversation:
    def test_writes_all_three_retention_surfaces(self) -> None:
        mgr = _manager()
        with patch.object(sa, "update_state") as update:
            mgr._promote_conversation("c1", "subagent:c1", last_used=1234.0)
        update.assert_called_once_with("c1", keep=True)
        mgr._sessions.mark_continuable.assert_called_once_with("subagent:c1")
        assert mgr._conversations["subagent:c1"] == 1234.0

    def test_state_write_failure_still_registers(self) -> None:
        mgr = _manager()
        with patch.object(sa, "update_state", side_effect=OSError):
            mgr._promote_conversation("c1", "subagent:c1")
        assert "subagent:c1" in mgr._conversations


class TestScanKeepStates:
    def test_missing_base_dir_returns_empty(self, tmp_path: Path) -> None:
        mgr = _manager()
        with patch.object(sa, "_subagents_dir", return_value=tmp_path / "absent"):
            assert mgr._scan_keep_states() == []

    def test_collects_only_keep_runs(self, tmp_path: Path) -> None:
        mgr = _manager()
        (tmp_path / "c1").mkdir()
        (tmp_path / "c2").mkdir()
        (tmp_path / "loose.txt").write_text("x", newline="\n")
        states = {
            "c1": {
                "keep": True,
                "conversation_key": "subagent:c1",
                "session_id": "sid-1",
                "provider": "acp",
                "cwd": str(tmp_path),
                "updated_at": 42.0,
            },
            "c2": {"keep": False},
        }
        with (
            patch.object(sa, "_subagents_dir", return_value=tmp_path),
            patch.object(sa, "read_state", side_effect=lambda name: states.get(name)),
        ):
            rows = mgr._scan_keep_states()
        assert [r[0] for r in rows] == ["c1"]
        assert rows[0][2] == "sid-1"
        assert rows[0][5] == 42.0

    def test_unreadable_entry_is_skipped(self, tmp_path: Path) -> None:
        mgr = _manager()
        (tmp_path / "c1").mkdir()
        with (
            patch.object(sa, "_subagents_dir", return_value=tmp_path),
            patch.object(sa, "read_state", side_effect=OSError),
        ):
            assert mgr._scan_keep_states() == []

    def test_scan_error_returns_empty(self) -> None:
        mgr = _manager()
        with patch.object(sa, "_subagents_dir", side_effect=RuntimeError("no home")):
            assert mgr._scan_keep_states() == []


class TestReleaseConversation:

    def test_released_deletes_session_files(self) -> None:
        mgr = _manager()
        mgr._conversations["subagent:c1"] = 1.0
        mgr._sessions.forget_conversation.return_value = "sid-1"
        with (
            patch.object(sa, "update_state") as update,
            patch.object(sa, "_cleanup_session_files_sync") as cleanup,
        ):
            ok, detail = mgr.release_conversation("c1")
        assert (ok, detail) == (True, "released")
        update.assert_called_once_with("c1", keep=False)
        cleanup.assert_called_once_with("sid-1", "acp")
        assert "subagent:c1" not in mgr._conversations

    def test_file_cleanup_failure_still_reports_released(self) -> None:
        mgr = _manager()
        mgr._sessions.forget_conversation.return_value = "sid-1"
        with (
            patch.object(sa, "update_state"),
            patch.object(sa, "_cleanup_session_files_sync", side_effect=OSError),
        ):
            assert mgr.release_conversation("c1")[0] is True


class TestSweepConversations:
    @pytest.mark.asyncio
    async def test_fresh_conversation_kept(self) -> None:
        mgr = _manager()
        mgr._conversations["subagent:c1"] = 1000.0
        await mgr._sweep_conversations_async(now=1001.0)
        assert "subagent:c1" in mgr._conversations


class TestInheritedContextGroups:
    def test_live_record_wins(self) -> None:
        mgr = _manager()
        mgr._agents["c1"] = _info("c1", include_memory=False)
        assert mgr._inherited_context_groups("c1") == (False, True, True)

    def test_empty_scope_means_every_group_withheld(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", return_value={"context_groups": ""}):
            assert mgr._inherited_context_groups("c1") == (False, False, False)


# ── Manager: wave / batch bookkeeping ─────────────────────────────────────


class TestBatchMembersPending:
    def test_no_batch_id_is_never_pending(self) -> None:
        assert _manager().batch_members_pending("") is False

    def test_submissions_in_flight_are_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 3]
        assert mgr.batch_members_pending("w1") is True

    def test_live_member_is_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        mgr._agents["a"] = _info("a", batch_id="w1")
        assert mgr.batch_members_pending("w1") is True

    def test_queued_member_is_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        mgr._queue = [{"batch_id": "w1"}]
        assert mgr.batch_members_pending("w1") is True

    def test_closed_wave_not_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        mgr._agents["a"] = _info("a", batch_id="w1", done=True)
        assert mgr.batch_members_pending("w1") is False


class TestFinalizeBatch:
    def test_prunes_every_wave_map(self) -> None:
        mgr = _manager()
        mgr._seen_batches.add("w1")
        mgr._batch_submitted["w1"] = [1, 1]
        mgr._batch_progress_ts["w1"] = 1.0
        mgr.finalize_batch("w1")
        assert "w1" not in mgr._seen_batches
        assert "w1" not in mgr._batch_submitted
        assert "w1" not in mgr._batch_progress_ts

    def test_empty_id_is_a_noop(self) -> None:
        mgr = _manager()
        mgr._seen_batches.add("w1")
        mgr.finalize_batch("")
        assert "w1" in mgr._seen_batches


class TestRecordLostSubmission:
    def test_empty_batch_id_is_a_noop(self) -> None:
        mgr = _manager()
        mgr.record_lost_submission("", 2, "transport error")
        assert mgr._batch_submitted == {}

    @pytest.mark.asyncio
    async def test_counts_submission_and_announces_failure(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        with patch.object(sa, "sel"):
            mgr.record_lost_submission("w1", 3, "POST timed out", parent_session_key="dash:1")
        # flake-ok: awaits the manager's in-memory announce tasks, which end without outside help; moved verbatim
        await asyncio.gather(*mgr._tasks.values())
        assert mgr._batch_submitted["w1"][0] == 1
        assert announced and announced[0].batch_id == "w1"
        assert announced[0].done is True
        assert "spawn submission lost" in announced[0].error

    def test_audit_failure_does_not_block_accounting(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            mgr.record_lost_submission("w1", 2, "boom")
        assert mgr._batch_submitted["w1"][0] == 1

    def test_without_completion_consumer_only_accounting_runs(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel"):
            mgr.record_lost_submission("w1", 2, "boom")
        assert mgr._batch_submitted["w1"] == [1, 2]
        assert mgr._tasks == {}

    def test_negative_total_is_floored_at_zero(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel"):
            mgr.record_lost_submission("w1", -5, "boom")
        assert mgr._batch_submitted["w1"][1] == 0


class TestSweepStuckWaves:
    def test_complete_wave_skipped(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=1e9)
            rec.assert_not_called()
            # Control: the same wave one submission short is reconciled.
            mgr._batch_submitted["w1"] = [1, 2]
            mgr._sweep_stuck_waves(now=1e9)
        assert rec.call_count == 1

    def test_within_grace_window_skipped(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 1000.0
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=1001.0)
            rec.assert_not_called()
            # Control: once the grace window has passed, the wave is reconciled.
            mgr._sweep_stuck_waves(now=1000.0 + sa._WAVE_STUCK_SECS)
        assert rec.call_count == 1

    def test_live_member_defers_reconciliation(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 0.0
        mgr._agents["a"] = _info("a", batch_id="w1")
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
            rec.assert_not_called()
            # Control: once the member ends, the wave is reconciled.
            mgr._agents["a"].done = True
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
        assert rec.call_count == 1

    def test_queued_member_defers_reconciliation(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 0.0
        mgr._queue = [{"batch_id": "w1"}]
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
            rec.assert_not_called()
            # Control: with the queue drained, the wave is reconciled.
            mgr._queue = []
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
        assert rec.call_count == 1

    def test_wedged_wave_reconciled_once(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 0.0
        mgr._agents["a"] = _info("a", batch_id="w1", done=True, parent_session_key="dash:1")
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
        assert rec.call_count == 1
        assert rec.call_args.kwargs["parent_session_key"] == "dash:1"


def _sweep_digest_holds(mgr: SubagentManager, form: str, *, now: float) -> None:
    """The digest-hold sweep in *form*: the sync entry, or the awaited one the
    reaper loop runs."""
    if form == "sync":
        mgr._sweep_digest_holds(now=now)
    else:
        asyncio.run(mgr._sweep_digest_holds_async(now))


class TestSweepDigestHolds:
    def test_no_completion_consumer_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager()
        held = _info("a", batch_id="w1", batch_total=2, done=True, parent_session_key="dash:1")
        held._digest_held_at = 1.0
        mgr._agents["a"] = held
        mgr._batch_submitted["w1"] = [1, 2]
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1e9)
            flush.assert_not_called()
            # Control: the same expired hold is flushed once a consumer exists.
            mgr._on_done = AsyncMock()
            mgr._sweep_digest_holds(now=1e9)
        assert flush.call_count == 1

    @pytest.mark.parametrize("form", ["sync", "async"])
    def test_closing_wave_not_forced(self, monkeypatch: pytest.MonkeyPatch, form: str) -> None:
        """With no member outstanding and the final report in flight, the real
        wave-close digest lands when that report is consumed; a forced partial
        digest here would race it and could deliver the same members twice."""
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager(on_done=AsyncMock())
        held = _info("a", batch_id="w1", batch_total=1, done=True)
        held._digest_held_at = 1.0
        mgr._agents["a"] = held
        mgr._batch_submitted["w1"] = [1, 1]
        mgr.arm_report_in_flight(held)
        with patch.object(mgr, "force_digest_flush") as flush:
            _sweep_digest_holds(mgr, form, now=1e6)
        flush.assert_not_called()
        # The in-flight report is what held the flush back; without the arm the
        # same state is force-flushed (test_stranded_hold_past_deadline_is_forced).
        assert mgr.batch_reports_in_flight("w1") is True
        assert mgr.batch_members_pending("w1") is False

    @pytest.mark.parametrize("form", ["sync", "async"])
    def test_stranded_hold_past_deadline_is_forced(
        self, monkeypatch: pytest.MonkeyPatch, form: str
    ) -> None:
        """No pending members AND no report in flight, yet a hold aged past the
        deadline: the wave-close flush is never coming (a terminal report ended
        without reaching the consumer — injection-timeout / announce-failure
        arms release the hold but land no accounting). The sweep is the only
        remaining exit; before the in-flight-aware guard this state was
        unsweepable and the held sibling results stranded until restart."""
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager(on_done=AsyncMock())
        held = _info("a", batch_id="w1", batch_total=1, done=True, parent_session_key="dash:1")
        held._digest_held_at = 1.0
        mgr._agents["a"] = held
        mgr._batch_submitted["w1"] = [1, 1]  # no pending members
        # No arm: the member's report ended without reaching the consumer.
        with patch.object(mgr, "force_digest_flush") as flush:
            _sweep_digest_holds(mgr, form, now=1e6)
        assert flush.call_count == 1
        assert flush.call_args[0][0] == "w1"
        assert flush.call_args[0][1] == "dash:1"

    def test_expired_hold_forces_partial_flush(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager(on_done=AsyncMock())
        held = _info("a", batch_id="w1", batch_total=2, done=True, parent_session_key="dash:1")
        held._digest_held_at = 100.0
        newer = _info("b", batch_id="w1", batch_total=2, done=True)
        newer._digest_held_at = 500.0
        mgr._agents.update({"a": held, "b": newer})
        mgr._batch_submitted["w1"] = [1, 2]  # still pending
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1000.0)
        assert flush.call_count == 1
        assert flush.call_args[0][0] == "w1"
        assert flush.call_args[0][1] == "dash:1"
        assert flush.call_args[0][3] == pytest.approx(900.0)  # oldest hold wins

    def test_unheld_members_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager(on_done=AsyncMock())
        mgr._agents["a"] = _info("a", batch_id="w1")
        mgr._agents["b"] = _info("b")
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1e9)
            flush.assert_not_called()
            # Control: once the wave member holds a result, the sweep flushes it.
            mgr._agents["a"]._digest_held_at = 1.0
            mgr._sweep_digest_holds(now=1e9)
        assert flush.call_count == 1
        assert flush.call_args[0][0] == "w1"


class TestForceDigestFlush:
    def test_missing_batch_id_is_a_noop(self) -> None:
        mgr = _manager(on_done=AsyncMock())
        mgr.force_digest_flush("", "dash:1", 2, 300.0)
        assert mgr._tasks == {}

    def test_without_completion_consumer_is_a_noop(self) -> None:
        mgr = _manager()
        mgr.force_digest_flush("w1", "dash:1", 2, 300.0)
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_announces_flush_only_record(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        with patch.object(sa, "sel"):
            mgr.force_digest_flush("w1", "dash:1", 3, 300.0)
        # flake-ok: awaits the manager's in-memory announce tasks, which end without outside help; moved verbatim
        await asyncio.gather(*mgr._tasks.values())
        assert announced and announced[0]._digest_flush_only is True
        assert announced[0].batch_id == "w1"
        assert announced[0].batch_total == 3

    @pytest.mark.asyncio
    async def test_audit_failure_still_schedules(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            mgr.force_digest_flush("w1", "dash:1", 1, 1.0)
        # flake-ok: awaits the manager's in-memory announce tasks, which end without outside help; moved verbatim
        await asyncio.gather(*mgr._tasks.values())
        assert announced and announced[0].batch_id == "w1"


class TestAnnounceDigestFlush:
    @pytest.mark.asyncio
    async def test_settles_holds_after_clean_handoff(self) -> None:
        mgr = _manager(on_done=AsyncMock())
        info = _info(batch_id="w1")
        info._digest_settle_deliveries = [
            SubagentDelivery("m1", 1.0, 0.1),
            SubagentDelivery("m2", 2.0, 0.2),
        ]
        with patch.object(sa, "mark_delivered") as mark:
            await mgr._announce_digest_flush(info)
        assert [c[0][0] for c in mark.call_args_list] == ["m1", "m2"]
        assert info._digest_settle_deliveries == []

    @pytest.mark.asyncio
    async def test_routing_failure_leaves_holds_unsettled(self) -> None:
        mgr = _manager(on_done=AsyncMock(side_effect=RuntimeError("route down")))
        info = _info(batch_id="w1")
        info._digest_settle_deliveries = [SubagentDelivery("m1", 1.0, 0.1)]
        with patch.object(sa, "mark_delivered") as mark:
            await mgr._announce_digest_flush(info)
        mark.assert_not_called()
        assert [d.agent_id for d in info._digest_settle_deliveries] == ["m1"]

    @pytest.mark.asyncio
    async def test_settle_swallows_tombstone_failure(self) -> None:
        mgr = _manager()
        info = _info()
        info._digest_settle_deliveries = [SubagentDelivery("m1", 1.0, 0.1)]
        with patch.object(sa, "mark_delivered", side_effect=OSError):
            await mgr._settle_digest_holds(info)
        assert info._digest_settle_deliveries == []


class TestAnnounceRejection:
    def test_non_batch_rejection_is_not_announced(self) -> None:
        mgr = _manager(on_done=AsyncMock())
        info = _info(done=True, error="rejected")
        assert mgr._announce_rejection(info) is info
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_batch_rejection_reaches_the_wave(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        info = _info(done=True, error="rejected", batch_id="w1")
        mgr._announce_rejection(info)
        # flake-ok: awaits the manager's in-memory announce tasks, which end without outside help; moved verbatim
        await asyncio.gather(*mgr._tasks.values())
        assert announced == [info]

    @pytest.mark.asyncio
    async def test_finished_batch_rejection_announce_leaves_task_registry(self) -> None:
        mgr = _manager(on_done=AsyncMock())
        info = _info(done=True, error="rejected", batch_id="w1")
        mgr._announce_rejection(info)
        assert list(mgr._tasks) == [f"reject-{info.id}"]  # registered while in flight
        await asyncio.wait_for(asyncio.gather(*mgr._tasks.values()), _ANNOUNCE_WAIT_SECS)
        # flake-ok: one zero-delay yield lets the done callbacks run before the assert; moved verbatim
        await asyncio.sleep(0)  # done callbacks run on the next loop pass
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_safe_announce_swallows_callback_failure(self) -> None:
        on_done = AsyncMock(side_effect=RuntimeError("boom"))
        mgr = _manager(on_done=on_done)
        info = _info(done=True, batch_id="w1")
        await mgr._safe_announce(info)
        on_done.assert_awaited_once_with(info)
        # A failed announce still releases the in-flight hold it armed.
        assert mgr.batch_reports_in_flight("w1") is False


# ── Manager: done-but-unreported wave accounting ────────────


class TestBatchReportsInFlight:
    """``batch_reports_in_flight`` holds the wave-close fallback open across the
    done-but-unreported window: ``info.done`` has flipped (so
    ``batch_members_pending`` does not count the member) but its terminal
    report has not yet been consumed, so the wave's done-count does not include
    it either. Without the hold, a sibling completion landing in that window
    finalizes the wave early and the in-flight report finalizes it again."""

    def test_no_batch_id_is_never_in_flight(self) -> None:
        assert _manager().batch_reports_in_flight("") is False

    @pytest.mark.parametrize(
        "armed_batch, expected",
        [
            pytest.param("w1", True, id="test_done_but_unreported_member_is_in_flight"),
            pytest.param("w2", False, id="test_other_waves_members_do_not_count"),
        ],
    )
    def test_armed_member_holds_only_its_own_wave(self, armed_batch: str, expected: bool) -> None:
        mgr = _manager()
        info = _info("a", batch_id=armed_batch, done=True)
        mgr._agents["a"] = info
        mgr.arm_report_in_flight(info)
        assert mgr.batch_reports_in_flight("w1") is expected

    def test_consumed_report_is_not_in_flight(self) -> None:
        mgr = _manager()
        info = _info("a", batch_id="w1", done=True)
        mgr._agents["a"] = info
        mgr.arm_report_in_flight(info)
        mgr.consume_report_hold("w1", "a")
        assert mgr.batch_reports_in_flight("w1") is False
        # The consumed wave's registry entry is pruned, not left empty.
        assert "w1" not in mgr._reports_in_flight

    def test_operator_clear_cannot_drop_the_hold(self) -> None:
        """The hold lives in the manager-level registry, NOT on `_agents`
        membership. An operator clear-completed (DELETE /api/spawn) pops every
        done member — landing inside the done-but-unreported window it must NOT
        release the hold, or a sibling completion would finalize the wave early
        and the in-flight report would finalize it a second time."""
        mgr = _manager()
        info = _info("a", batch_id="w1", done=True)
        mgr._agents["a"] = info
        mgr.arm_report_in_flight(info)
        # What the DELETE /api/spawn handler does to done members:
        mgr._agents.pop("a")
        assert mgr.batch_reports_in_flight("w1") is True
        # The consumer still holds `info` by reference and releases on landing.
        mgr.consume_report_hold("w1", "a")
        assert mgr.batch_reports_in_flight("w1") is False

    def test_flush_only_records_never_arm(self) -> None:
        """Flush-only synthetics skip the consumer's accounting block, so a
        hold would only ever be released structurally — transiently pinning
        the wave open for no accounting reason."""
        mgr = _manager()
        info = _info("a", batch_id="w1", done=True)
        info._digest_flush_only = True
        mgr.arm_report_in_flight(info)
        assert mgr.batch_reports_in_flight("w1") is False

    def test_consume_is_idempotent_and_unarmed_release_is_a_noop(self) -> None:
        mgr = _manager()
        mgr.consume_report_hold("w1", "never-armed")
        assert mgr.batch_reports_in_flight("w1") is False
        info = _info("a", batch_id="w1", done=True)
        mgr.arm_report_in_flight(info)
        mgr.consume_report_hold("w1", "a")
        mgr.consume_report_hold("w1", "a")
        assert mgr.batch_reports_in_flight("w1") is False

    def test_finalize_batch_prunes_the_hold_registry(self) -> None:
        mgr = _manager()
        info = _info("a", batch_id="w1", done=True)
        mgr.arm_report_in_flight(info)
        mgr.finalize_batch("w1")
        assert mgr.batch_reports_in_flight("w1") is False
        assert "w1" not in mgr._reports_in_flight

    def test_live_member_is_not_in_flight(self) -> None:
        """A member still RUNNING is `batch_members_pending`'s job — the arm
        happens only at the report's done-flip, so a live member never holds
        here and the two predicates stay complementary, not redundant."""
        mgr = _manager()
        mgr._agents["a"] = _info("a", batch_id="w1", done=False)
        assert mgr.batch_reports_in_flight("w1") is False


class TestReportsInFlightStrandBackstops:
    """Every terminal arm that can end a member's report WITHOUT reaching the
    completion consumer must clear the hold, or `batch_reports_in_flight`
    would strand the wave-close fallback forever. The wave keeps its degraded
    a-sibling-can-close liveness instead."""

    @pytest.mark.asyncio
    async def test_report_injection_timeout_clears_the_hold(self) -> None:
        """`asyncio.wait_for(self._on_done(info), …)` timing out is terminal for
        the report: nothing further reaches the consumer for this member. The
        structural `finally` must release the hold the report armed."""
        seen: list[bool] = []

        async def _consumer(info: SubagentInfo) -> None:
            seen.append(_manager_ref[0].batch_reports_in_flight("w1"))
            raise asyncio.TimeoutError

        mgr = _manager(on_done=_consumer)
        _manager_ref = [mgr]
        info = _info("a", batch_id="w1", done=True)
        mgr._agents["a"] = info

        await mgr._run_terminal_report(
            info,
            source="Test",
            injection_timeout_reason="delivery timed out (test)",
            mark_delivered_on_success=False,
        )

        # The report armed the hold at its done-flip (visible mid-announce)…
        assert seen == [True]
        # …and the structural `finally` released it when the report ended.
        assert mgr.batch_reports_in_flight("w1") is False

    @pytest.mark.asyncio
    async def test_report_announce_failure_clears_the_hold(self) -> None:
        seen: list[bool] = []

        async def _consumer(info: SubagentInfo) -> None:
            seen.append(_manager_ref[0].batch_reports_in_flight("w1"))
            raise RuntimeError("boom")

        mgr = _manager(on_done=_consumer)
        _manager_ref = [mgr]
        info = _info("a", batch_id="w1", done=True)
        mgr._agents["a"] = info

        await mgr._run_terminal_report(
            info,
            source="Test",
            injection_timeout_reason="delivery timed out (test)",
            mark_delivered_on_success=False,
        )

        assert seen == [True]
        assert mgr.batch_reports_in_flight("w1") is False

    @pytest.mark.asyncio
    async def test_the_report_body_releases_its_hold_without_its_task(self) -> None:
        """The report body's own ``finally`` is a release on its own, not only
        the task done-callback's twin: run with no task around it, a report
        whose consumer fails before landing the count leaves no hold."""

        async def _consumer(info: SubagentInfo) -> None:
            raise RuntimeError("boom")

        mgr = _manager(on_done=_consumer)
        info = _info("a", batch_id="w1", done=True)
        mgr._agents["a"] = info

        await mgr._report_terminal(
            info,
            source="Test",
            injection_timeout_reason="delivery timed out (test)",
            mark_delivered_on_success=False,
        )

        assert mgr.batch_reports_in_flight("w1") is False

    @pytest.mark.asyncio
    async def test_safe_announce_failure_clears_the_hold(self) -> None:
        """The registered approval-parked rejection announces through
        `_safe_announce`; it must arm before the announce (its done-flip
        equivalent) and a raising consumer must not leave it holding."""
        seen: list[bool] = []

        async def _consumer(info: SubagentInfo) -> None:
            seen.append(_manager_ref[0].batch_reports_in_flight("w1"))
            raise RuntimeError("boom")

        mgr = _manager(on_done=_consumer)
        _manager_ref = [mgr]
        info = _info("a", batch_id="w1", done=True)
        mgr._agents["a"] = info

        await mgr._safe_announce(info)

        assert seen == [True]
        assert mgr.batch_reports_in_flight("w1") is False

    @pytest.mark.asyncio
    async def test_hold_is_visible_to_the_consumer_and_released_after(self) -> None:
        """On the happy path the hold is armed BEFORE the announce and the
        CONSUMER owns the release (same synchronous block as the done-count
        increment) — so the wave-close fallback stays held open for exactly
        the window between the done-flip and the count landing. The report's
        structural `finally` then re-releases as an idempotent no-op."""
        seen: list[bool] = []

        async def _consumer(info: SubagentInfo) -> None:
            seen.append(_manager_ref[0].batch_reports_in_flight("w1"))

        mgr = _manager(on_done=_consumer)
        _manager_ref = [mgr]
        info = _info("a", batch_id="w1", done=True)
        mgr._agents["a"] = info

        await mgr._run_terminal_report(
            info,
            source="Test",
            injection_timeout_reason="delivery timed out (test)",
            mark_delivered_on_success=False,
        )

        assert seen == [True]
        assert mgr.batch_reports_in_flight("w1") is False

    @pytest.mark.asyncio
    async def test_cancelled_recovery_arm_never_strands_the_wave(self) -> None:
        """The cancelled-recovery arm is deliberately report-free (limbo
        avoidance — no finalize claim, no terminal report ever runs). Holds are
        armed only at a report's done-flip, so a record terminalized here never
        carries one — the wave must read as hold-free throughout."""
        mgr = _manager()
        info = _info("a", batch_id="w1")
        info.started = 1.0
        mgr._agents["a"] = info

        with patch.object(mgr, "_write_tombstone"):
            # From the test task, `_resume` awaits the ORIGINAL task — this
            # test's own — which never finishes first: a deterministic block
            # point with no timing dependence.
            mgr._schedule_cancel_recovery(info)
            recovery = mgr._tasks["a:recovery"]
            # flake-ok: one zero-delay yield parks _resume on the wait before the cancel; moved verbatim
            await asyncio.sleep(0)  # let _resume start and park on the wait
            recovery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                # flake-ok: awaits the recovery task cancelled just before this with block; moved verbatim
                await recovery

        assert info.done is True
        assert mgr.batch_reports_in_flight("w1") is False


# ── Structural guard: every terminal done-flip arms the wave-close hold ──────


class TestEveryDoneFlipArmsTheHold:
    """A done-but-unreported hold protects wave close only if EVERY terminal
    ``info.done = True`` flip arms it in the same synchronous block. Hand-placed
    arms are fragile — one missed flip (the common success path) reopens the
    double-finalize race. This source-scan fails on a bare ``.done = True`` that
    is not followed by an ``arm_report_in_flight`` call, so a new flip site
    cannot silently bypass the hold. The one documented exception is the
    cancelled-recovery limbo arm, which takes no finalize claim and whose
    ``not info.done`` guard proves no report ever runs — it carries an explicit
    ``report-free`` marker on an adjacent line."""

    def test_no_terminal_done_flip_is_left_unarmed(self) -> None:
        import re

        root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "subagent_manager"
        files = sorted(root.rglob("*.py"))
        assert files, "no subagent_manager sources found"

        flip_re = re.compile(r"^\s*(\w+)\.done\s*=\s*True\s*$")
        unarmed: list[str] = []
        for path in files:
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                m = flip_re.match(line)
                if not m:
                    continue
                target = m.group(1)  # the object whose .done flips (usually "info")
                window = "\n".join(lines[i : i + 16])
                armed = f"arm_report_in_flight({target})" in window
                # Documented report-free exception: a flip that takes no
                # finalize claim and spawns no report (limbo-avoidance only).
                exempt = "report-free" in window
                if not armed and not exempt:
                    unarmed.append(f"{path.name}:{i + 1}: {line.strip()}")

        assert not unarmed, (
            "terminal `.done = True` flip(s) do not arm the wave-close hold "
            "(and carry no `report-free` exemption marker) — a sibling "
            "completion can close the wave in the done-but-unarmed window and "
            "the late report re-finalizes it:\n  " + "\n  ".join(unarmed)
        )

    def test_every_deferred_safe_announce_is_armed_first(self) -> None:
        """A record flipped ``done=True`` elsewhere and then announced through a
        DEFERRED ``ensure_future(_safe_announce(info))`` is in the same
        done-but-unreported window as a flip-site: the arm inside
        ``_safe_announce`` runs only on the task's first step, one event-loop
        turn later, so a sibling completing in that turn closes the wave early.
        Every such scheduling must be preceded by a synchronous
        ``arm_report_in_flight(info)`` in the same block. The one exception is a
        NON-batch rejection (``_drain_queue``'s path), which carries no
        ``batch_id`` and so holds nothing — marked ``non-batch`` nearby."""
        import re

        root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "subagent_manager"
        sched_re = re.compile(r"ensure_future\(\s*self\._manager\._safe_announce\((\w+)\)")
        split_re = re.compile(r"ensure_future\(\s*$")  # the call split across two lines
        unarmed: list[str] = []
        for path in sorted(root.rglob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                m = sched_re.search(line)
                target = None
                if m:
                    target = m.group(1)
                elif split_re.search(line) and i + 1 < len(lines):
                    nxt = re.search(r"self\._manager\._safe_announce\((\w+)\)", lines[i + 1])
                    if nxt:
                        target = nxt.group(1)
                if target is None:
                    continue
                back = "\n".join(lines[max(0, i - 20) : i + 1])
                armed = f"arm_report_in_flight({target})" in back
                # NON-batch rejections announce off a record with no batch_id,
                # so there is no wave hold to arm (the drain path). Matched
                # case-insensitively against a `non-batch` marker nearby.
                exempt = "non-batch" in back.lower()
                if not armed and not exempt:
                    unarmed.append(f"{path.name}:{i + 1}: {line.strip()}")

        assert not unarmed, (
            "deferred `_safe_announce` scheduling(s) are not preceded by a "
            "synchronous `arm_report_in_flight` (and carry no `non-batch` "
            "marker) — the arm inside `_safe_announce` lands one event-loop "
            "turn late, leaving a window where a sibling closes the wave "
            "early:\n  " + "\n  ".join(unarmed)
        )
