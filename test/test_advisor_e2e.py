"""Advisor end-to-end: observation to guarded delivery through the pump.

Contract under test (see docs/system-specs/modules/advisor.md):

- the reviewer's final text decodes through the platform's ``parse_llm_json``
  (plain JSON, a fenced block, or JSON embedded in prose); anything else
  reaches the dispatcher raw and is recorded as a degradation.
- ``render_update_prompt`` serializes an observation update with its
  identity (turn, epoch, seq, in-progress) and evidence.
- ``AdvisorService.pump_async`` drains the slot's observer, runs one bounded
  review through the pool, and dispatches the parsed result — the full
  observe -> review -> guard -> deliver path with a fake reviewer, no ACP.
- A reviewer failure degrades visibly and delivers nothing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.advisor.composition import render_update_prompt
from kiro_crew.advisor.service import (
    AdvisorService,
    attach_for_turn,
    complete_turn,
    observe_segment,
    observe_tool_result,
)


class TestRenderUpdatePrompt:
    def test_prompt_carries_identity_and_evidence(self):
        from kiro_crew.advisor.observation import AdvisorObserver

        obs = AdvisorObserver(parent_session_key="dashboard:a", turn_id="t1")
        obs.record_segment("I will delete the table.")
        obs.record_tool_result("fs_read", "schema body")
        update = obs.drain_update()
        prompt = render_update_prompt(update)
        assert "in progress" in prompt.lower()
        assert "I will delete the table." in prompt
        assert "fs_read" in prompt
        assert "schema body" in prompt
        assert str(update.seq) in prompt

    def test_final_update_is_marked_final(self):
        from kiro_crew.advisor.observation import AdvisorObserver

        obs = AdvisorObserver(parent_session_key="dashboard:a", turn_id="t1")
        obs.record_segment("final answer")
        update = obs.complete(stop_reason="end_turn")
        prompt = render_update_prompt(update)
        assert "final" in prompt.lower()
        assert "end_turn" in prompt


class FakePool:
    """Stands in for AdvisorReviewerRuntime: records reviews, returns canned text."""

    def __init__(self, result_text):
        self.result_text = result_text
        self.reviewed: list[tuple[str, dict]] = []
        self.statuses: dict[str, str] = {}

    async def acquire_session(self, key):
        return MagicMock(parent_session_key=key, session_id="advisor:9")

    async def review(self, key, payload):
        self.reviewed.append((key, payload))
        if isinstance(self.result_text, Exception):
            self.statuses[key] = "degraded"
            return None
        return self.result_text

    def status(self, key):
        return self.statuses.get(key, "watching")


def _running_slot(state, key="test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = MagicMock()
    client.supports_steer = True

    async def accept(message):
        return True

    client.steer = accept
    slot._acp_client = client
    return slot


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


def fresh_service(enabled=True) -> AdvisorService:
    import kiro_crew.advisor.service as service_mod

    service_mod._service = AdvisorService(enabled=enabled, reviewer_available=True)
    return service_mod._service


class TestSchedulePump:
    @pytest.mark.asyncio
    async def test_disabled_service_schedules_nothing(self, state):
        service = fresh_service(enabled=False)
        service.set_reviewer_pool(FakePool('{"version": 1, "notes": []}'))
        from kiro_crew.advisor.service import schedule_pump

        slot = _running_slot(state)
        state._background_tasks = set()
        schedule_pump(state, slot)
        assert state._background_tasks == set()

    @pytest.mark.asyncio
    async def test_enabled_with_observer_schedules_one_pump(self, state):
        import asyncio

        service = fresh_service(enabled=True)
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        from kiro_crew.advisor.service import schedule_pump

        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "work happened")
        state._background_tasks = set()
        schedule_pump(state, slot)
        assert len(state._background_tasks) == 1
        await asyncio.gather(*state._background_tasks)
        assert len(pool.reviewed) == 1

    @pytest.mark.asyncio
    async def test_no_pool_schedules_nothing(self, state):
        service = fresh_service(enabled=True)
        from kiro_crew.advisor.service import schedule_pump

        slot = _running_slot(state)
        attach_for_turn(slot)
        # attach binds a pool lazily by design; the property pinned HERE is
        # the pump's robustness when no pool exists (bind failed / shut down).
        service._pool = None
        service._pool_model = ""
        state._background_tasks = set()
        schedule_pump(state, slot)
        assert state._background_tasks == set()


class TestPumpEndToEnd:
    @pytest.mark.asyncio
    async def test_blocker_flows_from_observation_to_advisor_row(self, state):
        service = fresh_service()
        envelope_text = json.dumps(
            {
                "version": 1,
                "notes": [{"severity": "blocker", "text": "dropping the wrong table"}],
            }
        )
        service.set_reviewer_pool(FakePool(envelope_text))
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "I will drop table users_prod.")
        observe_tool_result(slot, "fs_read", "schema")
        await service.pump_async(state, slot)
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorSeverity"] == "blocker"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "wrap",
        [
            "Here is my review:\n```json\n{}\n```\nDone.",
            "I looked carefully. {} That is all.",
        ],
    )
    async def test_fenced_or_prose_wrapped_envelope_still_delivers(self, state, wrap):
        service = fresh_service()
        envelope = json.dumps({"version": 1, "notes": [{"severity": "blocker", "text": "x"}]})
        service.set_reviewer_pool(FakePool(wrap.format(envelope)))
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "I will drop table users_prod.")
        await service.pump_async(state, slot)
        assert slot.messages[-1]["role"] == "advisor"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", ["", "no json here", "{broken", "[1,2,3]"])
    async def test_junk_reply_delivers_nothing(self, state, junk):
        service = fresh_service()
        service.set_reviewer_pool(FakePool(junk))
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "text")
        before = len(slot.messages)
        await service.pump_async(state, slot)
        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_pump_with_nothing_recorded_reviews_nothing(self, state):
        service = fresh_service()
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        await service.pump_async(state, slot)
        assert pool.reviewed == []

    @pytest.mark.asyncio
    async def test_final_update_flows_after_complete(self, state):
        service = fresh_service()
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "the answer")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert len(pool.reviewed) == 1
        assert "FINAL update (turn completed" in pool.reviewed[0][1]["prompt"]

    @pytest.mark.asyncio
    async def test_reviewer_failure_delivers_nothing(self, state):
        service = fresh_service()
        service.set_reviewer_pool(FakePool(RuntimeError("boom")))
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "text")
        before = len(slot.messages)
        await service.pump_async(state, slot)
        assert len(slot.messages) == before

    @pytest.mark.asyncio
    async def test_disabled_service_pump_is_inert(self, state):
        service = fresh_service(enabled=False)
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        await service.pump_async(state, slot)
        assert pool.reviewed == []


class TestPumpWorkdirSupply:
    @pytest.mark.asyncio
    async def test_pump_passes_the_slots_project_as_work_dir(self, state):
        service = fresh_service()
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        slot.project = "/parent/project"
        attach_for_turn(slot)
        observe_segment(slot, "text")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert pool.reviewed[0][1]["work_dir"] == "/parent/project"


class TestEffectiveEnablementPump:
    """Round-6: the pump must honor the ATTACHED observer, not the global
    flag -- a session opted ON under a global-off default gets reviews."""

    @pytest.mark.asyncio
    async def test_override_on_under_global_off_still_pumps(self, state):
        service = fresh_service(enabled=False)  # global default OFF
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        slot.advisor_override = "on"
        assert attach_for_turn(slot) is not None
        observe_segment(slot, "work under per-session opt-in")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert len(pool.reviewed) == 1, "per-session opt-in must review under a global-off default"

    @pytest.mark.asyncio
    async def test_stale_review_never_dispatches_across_an_epoch_boundary(self, state):
        import asyncio

        service = fresh_service(enabled=True)

        class SlowPool(FakePool):
            def __init__(self, text):
                super().__init__(text)
                self.gate = asyncio.Event()

            async def review(self, key, payload):
                await self.gate.wait()
                return await super().review(key, payload)

        pool = SlowPool('{"version": 1, "notes": [{"severity": "nit", "text": "stale advice"}]}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "pre-reset work")
        complete_turn(slot, stop_reason="end_turn")
        task = asyncio.create_task(service.pump_async(state, slot))
        await asyncio.sleep(0)  # pump is now awaiting the slow review
        service.notify_boundary(f"dashboard:{slot.key}", "reset")  # epoch moves
        pool.gate.set()
        await task
        advisor_rows = [m for m in slot.messages if m.get("role") == "advisor"]
        assert advisor_rows == [], "a review raced by a reset must not dispatch into the new epoch"


class TestReviewThrottle:
    """Live-run finding: every checkpoint spawned a review (20 per turn).
    In-progress reviews are throttled per session; the final update always
    reviews."""

    @pytest.mark.asyncio
    async def test_rapid_checkpoints_yield_one_inflight_review(self, state):
        service = fresh_service(enabled=True)
        service.review_min_interval_secs = 300.0
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "one")
        await service.pump_async(state, slot)
        observe_segment(slot, "two")
        await service.pump_async(state, slot)  # throttled
        observe_segment(slot, "three")
        await service.pump_async(state, slot)  # throttled
        assert len(pool.reviewed) == 1

    @pytest.mark.asyncio
    async def test_final_update_reviews_despite_throttle(self, state):
        service = fresh_service(enabled=True)
        service.review_min_interval_secs = 300.0
        pool = FakePool('{"version": 1, "notes": []}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "one")
        await service.pump_async(state, slot)
        observe_segment(slot, "two")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert len(pool.reviewed) == 2, "the final update must always review"


class TestLateReviewPreservesInsteadOfSteering:
    """Round-10: a review finishing after the NEXT turn began must not steer
    stale advice into the successor turn -- it preserves (card + context)."""

    @pytest.mark.asyncio
    async def test_epoch_advance_during_review_forces_preserve(self, state):
        service = fresh_service(enabled=True)
        envelope_text = json.dumps(
            {"version": 1, "notes": [{"severity": "blocker", "text": "stale blocker"}]}
        )
        slot = _running_slot(state)
        steered = []

        async def record_steer(message):
            steered.append(message)
            return True

        slot._acp_client.steer = record_steer

        class _SlowPool(FakePool):
            async def review(self, session_key, payload):
                # The next turn begins while the review is in flight: the
                # observer re-primes onto a new epoch (same conversation, no
                # boundary), so the advice now describes a finished turn.
                service._observers[session_key].begin_turn()
                return await super().review(session_key, payload)

        service.set_reviewer_pool(_SlowPool(envelope_text))
        observer = attach_for_turn(slot)
        observe_segment(slot, "I will drop table users_prod.")
        observe_tool_result(slot, "fs_read", "schema")
        observer.complete()  # the turn finished; final review is due
        await service.pump_async(state, slot)
        # never steered into the successor turn; preserved card + context
        assert steered == []
        row = slot.messages[-1]
        assert row["role"] == "advisor"
        assert row["meta"]["advisorState"] == "preserved"
        assert slot._advisor_pending_context


class TestAcquireFailurePreservesUpdate:
    """Round-13: a runtime spawn failure at acquire must neither crash the
    pump task nor consume the drained update -- the next pump retries it."""

    @pytest.mark.asyncio
    async def test_failed_acquire_keeps_the_completed_update(self, state):
        service = fresh_service(enabled=True)

        class _BrokenPool(FakePool):
            async def acquire_session(self, key):
                raise RuntimeError("spawn failed")

        service.set_reviewer_pool(_BrokenPool("{}"))
        slot = _running_slot(state)
        observer = attach_for_turn(slot)
        observe_segment(slot, "some work")
        observer.complete()
        # must not raise
        await service.pump_async(state, slot)
        # the final update survives for a later retry
        assert observer.take_completed() is not None


class TestRebindDuringReviewDiscards:
    """Round-14: a slot rebound to a DIFFERENT conversation mid-review must
    not receive the prior session's advice -- the post-review identity check
    recomputes the slot's effective key."""

    @pytest.mark.asyncio
    async def test_linked_session_change_discards_the_review(self, state):
        service = fresh_service(enabled=True)
        envelope_text = json.dumps(
            {"version": 1, "notes": [{"severity": "blocker", "text": "old-session advice"}]}
        )
        slot = _running_slot(state)

        class _RebindPool(FakePool):
            async def review(self, session_key, payload):
                # A cron/workflow rebind lands while the review is in flight:
                # the slot now fronts a different conversation.
                slot.linked_session_key = "slack:99999.111"
                return await super().review(session_key, payload)

        service.set_reviewer_pool(_RebindPool(envelope_text))
        observer = attach_for_turn(slot)
        observe_segment(slot, "work in the ORIGINAL conversation")
        observer.complete()
        await service.pump_async(state, slot)
        # nothing persisted or staged into the rebound conversation
        assert not [m for m in slot.messages if m.get("role") == "advisor"]
        assert not getattr(slot, "_advisor_pending_context", [])


class TestHardKillDiscardsInFlightReview:
    """Round-20: a hard kill during pool.review must prevent the completing
    review from delivering anything -- the kill bumps the session's boundary
    generation, which the pump's post-review check already honors."""

    @pytest.mark.asyncio
    async def test_generation_bump_mid_review_discards(self, state):
        service = fresh_service(enabled=True)
        envelope_text = json.dumps(
            {"version": 1, "notes": [{"severity": "blocker", "text": "late advice"}]}
        )
        slot = _running_slot(state)

        class _KilledPool(FakePool):
            async def review(self, session_key, payload):
                # The user's hard kill lands while the review runs.
                from kiro_crew.advisor.service import notify_hard_kill

                notify_hard_kill(session_key)
                return await super().review(session_key, payload)

        service.set_reviewer_pool(_KilledPool(envelope_text))
        observer = attach_for_turn(slot)
        observe_segment(slot, "work")
        observer.complete()
        await service.pump_async(state, slot)
        assert not [m for m in slot.messages if m.get("role") == "advisor"]
        assert not getattr(slot, "_advisor_pending_context", [])


class TestModelAttributionSnapshot:
    """Round-23 B3: an in-flight review raced by a model change must be
    attributed to the model that SERVED it (the pool's model at dispatch),
    never the newly configured one."""


class TestHardKillClearsQueuedEvidence:
    """Round-25: a pump scheduled AFTER a hard kill snapshots the NEW
    generation, so the generation bump alone cannot stop it draining
    pre-kill evidence. The kill must also reset the observer's epoch and
    clear its guard."""

    @pytest.mark.asyncio
    async def test_pump_after_hard_kill_reviews_nothing(self, state):
        from kiro_crew.advisor.service import notify_hard_kill

        service = fresh_service(enabled=True)
        pool = FakePool('{"version": 1, "notes": [{"severity": "nit", "text": "stale"}]}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "pre-kill work the user threw away")
        complete_turn(slot, stop_reason="end_turn")
        notify_hard_kill(f"dashboard:{slot.key}")
        await service.pump_async(state, slot)
        assert pool.reviewed == [], "pre-kill evidence must not reach the reviewer"
        advisor_rows = [m for m in slot.messages if m.get("role") == "advisor"]
        assert advisor_rows == []


class TestOptOutDuringAcquisitionDiscards:
    """Round-26: an opt-out (or any boundary) landing while the pump awaits
    reviewer-session acquisition must stop the drain -- opted-out evidence
    must never reach pool.review()."""

    @pytest.mark.asyncio
    async def test_boundary_during_acquire_prevents_review(self, state):
        import asyncio

        from kiro_crew.advisor.service import apply_override_change

        service = fresh_service(enabled=True)

        class SlowAcquirePool(FakePool):
            def __init__(self, text):
                super().__init__(text)
                self.gate = asyncio.Event()

            async def acquire_session(self, key):
                await self.gate.wait()
                return await super().acquire_session(key)

        pool = SlowAcquirePool('{"version": 1, "notes": [{"severity": "nit", "text": "x"}]}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "evidence the user then opted out of sharing")
        complete_turn(slot, stop_reason="end_turn")
        task = asyncio.create_task(service.pump_async(state, slot))
        await asyncio.sleep(0)  # pump is now awaiting acquisition
        apply_override_change(f"dashboard:{slot.key}", "off")
        pool.gate.set()
        await task
        assert pool.reviewed == [], "opted-out evidence must not reach the reviewer"


class TestRebindDuringAcquisitionAborts:
    """Round-30 (Opus): a cron/workflow rebind landing during the reviewer's
    cold spawn moves the slot to a NEW conversation key -- the pump must
    abort before draining/reviewing, not call pool.review under a key it
    never acquired (which killed the task with a KeyError)."""

    @pytest.mark.asyncio
    async def test_rebind_during_acquire_prevents_review(self, state):
        import asyncio

        service = fresh_service(enabled=True)

        class SlowAcquirePool(FakePool):
            def __init__(self, text):
                super().__init__(text)
                self.gate = asyncio.Event()

            async def acquire_session(self, key):
                await self.gate.wait()
                return await super().acquire_session(key)

        pool = SlowAcquirePool('{"version": 1, "notes": [{"severity": "nit", "text": "x"}]}')
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "work for the ORIGINAL conversation")
        complete_turn(slot, stop_reason="end_turn")
        task = asyncio.create_task(service.pump_async(state, slot))
        await asyncio.sleep(0)  # pump is awaiting the cold spawn
        slot.linked_session_key = "workflow:rebound-elsewhere"  # in-place rebind
        pool.gate.set()
        await task  # must complete without raising
        assert pool.reviewed == [], "a rebound slot's evidence must not review"


class TestLateReviewUsesEpochLocalGuard:
    """Round-34: a review that completes AFTER the next turn began (preserve
    path) must not populate the session's live guard -- otherwise the
    stale advice's dedupe/budget suppresses a repeated blocker in the
    current turn."""

    @pytest.mark.asyncio
    async def test_late_review_leaves_live_guard_untouched(self, state):
        import asyncio

        service = fresh_service(enabled=True)

        class SlowPool(FakePool):
            def __init__(self, text):
                super().__init__(text)
                self.gate = asyncio.Event()

            async def review(self, key, payload):
                await self.gate.wait()
                return await super().review(key, payload)

        pool = SlowPool(
            '{"version": 1, "notes": [{"severity": "blocker", "text": "same blocker text"}]}'
        )
        service.set_reviewer_pool(pool)
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "turn N")
        complete_turn(slot, stop_reason="end_turn")
        task = asyncio.create_task(service.pump_async(state, slot))
        await asyncio.sleep(0)
        # next turn begins while the review is in flight
        slot._turn_generation += 1
        attach_for_turn(slot)
        observe_segment(slot, "turn N+1")
        pool.gate.set()
        await task
        key = f"dashboard:{slot.key}"
        live = service._guards.get(key)
        # the late review must not have seeded the live guard's dedupe set
        assert (
            live is None or not live._admitted
        ), "late (preserve-path) review contaminated the live guard"


class TestPumpPassesLiveAuthorizationPredicate:
    """Round-35: the pump hands the pool a predicate reflecting LIVE
    authorization (observer identity, boundary generation, slot key), so the
    pool can cancel a queued review after an opt-out."""

    @pytest.mark.asyncio
    async def test_predicate_flips_false_after_opt_out(self, state):
        from kiro_crew.advisor.service import apply_override_change

        service = fresh_service(enabled=True)
        captured = {}

        class CapturePool(FakePool):
            async def review(self, key, payload):
                captured["authorized"] = payload.get("_authorized")
                assert callable(captured["authorized"])
                assert captured["authorized"]() is True
                apply_override_change(key, "off")  # revoke while "queued"
                assert captured["authorized"]() is False
                return None

        service.set_reviewer_pool(CapturePool('{"version": 1, "notes": []}'))
        slot = _running_slot(state)
        attach_for_turn(slot)
        observe_segment(slot, "work")
        complete_turn(slot, stop_reason="end_turn")
        await service.pump_async(state, slot)
        assert "authorized" in captured


class TestTurnKeyIsPinnedAcrossARebind:
    """The observer is looked up by the slot's CURRENT session key on every
    checkpoint. A cron/workflow rebind (``linked_session_key`` swap) during a
    running turn would therefore route session A's remaining checkpoints into
    session B's observer, and A's staged advice into B's next prompt. The key
    is pinned at attach and every later lookup drops on a mismatch."""

    def test_checkpoints_after_a_rebind_do_not_reach_the_new_sessions_observer(self, state):
        service = fresh_service(enabled=True)
        slot_a = _running_slot(state, key="a")
        slot_b = _running_slot(state, key="b")
        slot_b.linked_session_key = "slack:1700000000.000200"
        attach_for_turn(slot_a)
        observer_b = attach_for_turn(slot_b)
        assert observer_b is not None
        before = (
            len(observer_b._pending_segments) if hasattr(observer_b, "_pending_segments") else None
        )
        # Rebind A's slot object onto B's session mid-turn.
        slot_a.linked_session_key = "slack:1700000000.000200"
        observe_segment(slot_a, "A's work, must not reach B")
        assert complete_turn(slot_a, stop_reason="end_turn") is None
        if before is not None:
            assert len(observer_b._pending_segments) == before
        assert "A's work" not in repr(vars(observer_b))
        assert service.observer_count() == 2

    def test_staged_advice_does_not_follow_the_slot_object_to_another_session(self, state):
        from kiro_crew.advisor.delivery import (
            _stage_pending_context,
            advisor_session_slots,
            peek_pending_advisor_context,
        )

        fresh_service(enabled=True)
        slot_a = _running_slot(state, key="a")
        _stage_pending_context(state, slot_a, "advice for session A")
        slot_a.linked_session_key = "slack:1700000000.000300"  # now fronts B
        staged = peek_pending_advisor_context(slot_a, siblings=advisor_session_slots(state, slot_a))
        assert staged == [], "A's advice must not be injected into B's turn"

    @pytest.mark.asyncio
    async def test_a_rebound_slot_does_not_pump_the_new_sessions_observer(self, state):
        """Verifier finding on the pin: the pump entry points also resolved the
        slot's CURRENT key, so A's slot object, rebound to B, reviewed B's
        pending evidence and steered it through A's live client."""
        service = fresh_service(enabled=True)
        pool = FakePool('{"version": 1, "notes": [{"severity": "nit", "text": "x"}]}')
        service.set_reviewer_pool(pool)
        slot_a = _running_slot(state, key="a")
        slot_b = _running_slot(state, key="b")
        slot_b.linked_session_key = "slack:1700000000.000200"
        attach_for_turn(slot_a)
        attach_for_turn(slot_b)
        observe_segment(slot_b, "B's evidence")
        complete_turn(slot_b, stop_reason="end_turn")
        slot_a.linked_session_key = "slack:1700000000.000200"  # A's object now fronts B
        await service.pump_async(state, slot_a)
        assert pool.reviewed == [], "A's rebound slot must not review B's evidence"
        await service.pump_async(state, slot_b)
        assert len(pool.reviewed) == 1, "B's own slot still reviews it"
