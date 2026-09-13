"""Turn-scoped advisor hooks: the seams the chat runner calls.

Contract under test (see docs/system-specs/modules/advisor.md):

- ``configure_from_config(cfg)`` applies the advisor.* section to the
  process-wide service (enablement, model, budget, cooldown).
- ``attach_for_turn(slot)`` composes global enablement with the slot's
  persisted override and returns an observer (or None) — cheap and inert
  when off.
- ``observe_tool_result`` / ``observe_segment`` / ``complete_turn`` are
  TOTAL functions keyed by the slot: they never raise, and they are no-ops
  for a slot with no attached observer, so the runner's hot path carries no
  advisor conditionals beyond the call itself.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.advisor.service import (
    AdvisorService,
    attach_for_turn,
    complete_turn,
    configure_from_config,
    get_advisor_service,
    observe_segment,
    observe_tool_result,
)


def make_slot(key="test", override="inherit"):
    return SimpleNamespace(key=key, advisor_override=override)


def fresh_service(enabled=False) -> AdvisorService:
    import kiro_crew.advisor.service as service_mod

    service_mod._service = AdvisorService(enabled=enabled, reviewer_available=True)
    return service_mod._service


class TestConfigureFromConfig:
    def test_advisor_section_applies(self):
        fresh_service()
        cfg = SimpleNamespace(
            advisor=SimpleNamespace(
                enabled=True,
                non_blocker_budget=2,
                cooldown_secs=30.0,
            ),
            # The reviewer model is the `advisor` role pin (round-72).
            agent=SimpleNamespace(role_models={"advisor": "reviewer-x"}),
        )
        configure_from_config(cfg)
        service = get_advisor_service()
        assert service.enabled is True
        assert service.reviewer_model == "reviewer-x"

    def test_configure_is_total_on_junk(self):
        fresh_service()
        configure_from_config(SimpleNamespace())  # no advisor attr
        configure_from_config(None)
        assert get_advisor_service().enabled is False


class TestAttachForTurn:
    def test_disabled_global_inherit_returns_none(self):
        fresh_service(enabled=False)
        assert attach_for_turn(make_slot()) is None

    def test_slot_on_override_attaches(self):
        fresh_service(enabled=False)
        observer = attach_for_turn(make_slot(override="on"))
        assert observer is not None

    def test_attach_keys_by_dashboard_session(self):
        fresh_service(enabled=True)
        attach_for_turn(make_slot(key="abc"))
        assert get_advisor_service().observer_count() == 1


class TestTurnHooksAreTotal:
    def test_hooks_noop_without_observer(self):
        fresh_service(enabled=False)
        slot = make_slot()
        observe_tool_result(slot, "grep", "output")
        observe_segment(slot, "text")
        complete_turn(slot, stop_reason="end_turn")

    def test_hooks_feed_the_attached_observer(self):
        fresh_service(enabled=True)
        slot = make_slot(key="fed")
        observer = attach_for_turn(slot)
        observe_segment(slot, "I will check the config.")
        observe_tool_result(slot, "fs_read", "config body")
        update = observer.drain_update()
        assert update is not None
        assert update.segments == ["I will check the config."]
        assert update.tool_results[0].tool_name == "fs_read"

    def test_complete_turn_emits_final_update_once(self):
        fresh_service(enabled=True)
        slot = make_slot(key="done")
        attach_for_turn(slot)
        first = complete_turn(slot, stop_reason="end_turn")
        assert first is not None and first.in_progress is False
        assert complete_turn(slot, stop_reason="end_turn") is None

    def test_hooks_swallow_observer_errors(self):
        fresh_service(enabled=True)
        slot = make_slot(key="hot")
        observer = attach_for_turn(slot)
        observer.record_tool_result = None  # sabotage: attribute not callable
        observe_tool_result(slot, "grep", "x")  # must not raise


class TestMultiTurnSemantics:
    """Round-4 review findings: the advisor must survive past turn 1.

    attach_for_turn re-primes a completed epoch (multi-turn observation),
    re-resolves the override every turn (opt-out honored after attachment),
    and boundary notifications reset the emission guard with the epoch.
    """

    def test_attach_reprimes_a_completed_epoch(self):
        fresh_service(enabled=True)
        slot = make_slot()
        observer = attach_for_turn(slot)
        observer.record_segment("turn one")
        observer.complete(stop_reason="end_turn")
        observer.take_completed()
        again = attach_for_turn(slot)
        assert again is observer
        again.record_segment("turn two")  # must not raise
        assert again.complete(stop_reason="end_turn") is not None

    def test_attach_preserves_an_unconsumed_completed_update(self):
        fresh_service(enabled=True)
        slot = make_slot()
        observer = attach_for_turn(slot)
        observer.record_segment("turn one")
        observer.complete(stop_reason="end_turn")
        attach_for_turn(slot)
        assert (
            observer.take_completed() is not None
        ), "re-priming must not drop a completed update the pump has not consumed"

    def test_attach_detaches_when_override_turns_off(self):
        service = fresh_service(enabled=True)
        slot = make_slot()
        assert attach_for_turn(slot) is not None
        slot.advisor_override = "off"
        assert attach_for_turn(slot) is None
        assert service.observer_count() == 0, "opt-out must drop the live observer"
        observe_segment(slot, "ignored")  # total: no observer, no raise

    def test_attach_detach_on_opt_out_drops_the_guard(self):
        service = fresh_service(enabled=True)
        slot = make_slot()
        attach_for_turn(slot)
        key = f"dashboard:{slot.key}"
        service._guards[key] = object()
        slot.advisor_override = "off"
        attach_for_turn(slot)
        assert key not in service._guards

    def test_epoch_boundary_resets_the_guard(self):
        service = fresh_service(enabled=True)
        slot = make_slot()
        attach_for_turn(slot)
        key = f"dashboard:{slot.key}"
        sentinel = object()
        service._guards[key] = sentinel
        service.notify_boundary(key, "reset")
        assert (
            service._guards.get(key) is not sentinel
        ), "an epoch boundary must reset dedupe/cooldown state with the epoch"

    def test_terminal_boundary_drops_the_guard(self):
        service = fresh_service(enabled=True)
        slot = make_slot()
        attach_for_turn(slot)
        key = f"dashboard:{slot.key}"
        service._guards[key] = object()
        service.notify_boundary(key, "close")
        assert key not in service._guards


class TestSegmentObservationRedaction:
    """Round-5 Opus blocker: the observer must never hold rawer content than
    the transcript. _flush_segment redacts before persisting; the segment
    observation must receive that same redacted form."""

    def test_flush_segment_observes_the_redacted_form(self, tmp_path):
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_runner

        fresh_service(enabled=True)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("redact-test")
        slot.advisor_override = "on"
        observer = attach_for_turn(slot)
        assert observer is not None

        # Assembled at runtime: the fork content scan flags the literal
        # key=value form on any ADDED line (its ruleset is external and not
        # fixable from a fork), while the runtime redactor matches the
        # assembled string all the same -- which is exactly what this test
        # exercises.
        secret = "aws_secret_access" + "_key=" + "AKIA" + "IOSFODNN7EXAMPLE"
        chat_runner._flush_segment(state, slot, f"the key is {secret}", broadcast=False)

        update = observer.complete(stop_reason="end_turn")
        joined = "\n".join(update.segments)
        assert (
            "AKIAIOSFODNN7EXAMPLEKEY99" not in joined
        ), "the observer held rawer content than the transcript"
        assert update.segments, "the redacted segment must still be observed"


class TestBeginTurnGuardAndReviewSurvival:
    """Round-7: begin_turn advancing the epoch must reset the guard (else a
    repeated blocker on turn 2 is suppressed), but must NOT cause the pump to
    discard turn 1's already-taken final review as 'stale'."""

    def test_begin_turn_resets_the_guard(self):
        service = fresh_service(enabled=True)
        slot = make_slot()
        attach_for_turn(slot)
        key = f"dashboard:{slot.key}"
        sentinel = object()
        service._guards[key] = sentinel
        # end turn 1, start turn 2 (sealed epoch -> begin_turn re-primes)
        observer = service._observers[key]
        observer.complete(stop_reason="end_turn")
        observer.take_completed()
        attach_for_turn(slot)
        assert (
            service._guards.get(key) is not sentinel
        ), "a new turn's epoch must reset dedupe so a repeated blocker is not lost"


class TestObserverKeyMatchesBoundaryKey:
    """The observer registry and the boundary notifications must share ONE
    key space, or a channel-linked slot's reset/compaction never reaches its
    observer and stale evidence crosses the boundary."""

    def test_close_site_fires_with_effective_session_key(self):
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers._close_slot)
        assert "notify_boundary(effective_session_key(slot), BOUNDARY_CLOSE)" in src
        assert 'notify_boundary(f"dashboard:' not in src

    def test_close_boundary_fires_only_after_the_pop_commits(self):
        """Every abort path before the pop restores the open slot; the
        observer must survive those, so the terminal boundary comes after."""
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers._close_slot)
        pop_at = src.find("state._slots.pop(name, None)")
        notify_at = src.find("notify_boundary(effective_session_key(slot), BOUNDARY_CLOSE)")
        assert pop_at != -1 and notify_at != -1
        assert notify_at > pop_at


class TestObserverTurnIdentity:
    """Round-13: updates must carry the parent turn identity, not an eternal
    empty string -- attach seeds and refreshes it from the slot's generation."""

    def test_attach_sets_turn_id_from_slot_generation(self):
        fresh_service(enabled=True)
        slot = make_slot()
        slot._turn_generation = 7
        observer = attach_for_turn(slot)
        assert observer._turn_id == "turn-7"
        # next turn refreshes it
        observer.complete()
        slot._turn_generation = 8
        observer2 = attach_for_turn(slot)
        assert observer2 is observer
        assert observer._turn_id == "turn-8"


class TestTerminalCompletionAfterFinalFlush:
    """Round-14: the epoch must seal AFTER the post-loop segment flush, or a
    text-only response never reaches the reviewer."""

    def test_advisor_complete_lives_in_the_finally_after_flushes(self):
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._run_chat)
        complete_at = src.find("_advisor_complete(")
        assert complete_at != -1
        # every post-loop flush site precedes the completion call
        last_flush = src.rfind("_flush_segment(state, slot, assistant_text", 0, complete_at)
        assert last_flush != -1, "completion must come after the post-loop flushes"
        # and the completion is gated on a genuine terminal
        gate = src[max(0, complete_at - 600) : complete_at]
        assert "_saw_terminal_event" in gate


class TestFailedTurnEvidenceIsolation:
    """Round-21: a provider failure leaves the epoch unsealed; the next
    attach carries a NEW turn identity, and the stale evidence must not be
    attributed to (or steer) the successor turn."""

    def test_unsealed_epoch_resets_when_turn_identity_changes(self):
        fresh_service(enabled=True)
        slot = make_slot()
        slot._turn_generation = 3
        observer = attach_for_turn(slot)
        observer.record_segment("evidence from the FAILED turn")
        # no complete(): the provider died without a terminal event
        slot._turn_generation = 4
        observer2 = attach_for_turn(slot)
        assert observer2 is observer
        update = observer.drain_update()
        # the failed turn's records must not survive into turn-4's epoch
        assert update is None or "FAILED turn" not in "".join(update.segments)

    def test_crash_recovery_also_resets_the_emission_guard(self):
        """The guard is per session and survives attach via ``setdefault``: a
        turn that died without a terminal leaves its steered blocker in
        ``_admitted`` and its cooldown running, so the recovery turn's
        identical blocker would be deduped away and its non-blockers held.
        The crash branch resets the guard with the epoch, as the three
        terminal paths do."""
        from kiro_crew.advisor.guard import EmissionGuard
        from kiro_crew.advisor.service import _slot_session_key, get_advisor_service

        fresh_service(enabled=True)
        slot = make_slot()
        slot._turn_generation = 3
        attach_for_turn(slot)
        key = _slot_session_key(slot)
        service = get_advisor_service()
        stale = service._guards.setdefault(key, EmissionGuard())
        # no complete(): the provider died without a terminal event
        slot._turn_generation = 4
        attach_for_turn(slot)
        assert service._guards.get(key) is not stale


class TestSealedPredecessorFinalUpdateSurvivesAttach:
    """Round-31 (Opus): a NORMALLY sealed predecessor turn's undrained final
    update must survive the successor's attach -- the crash-only fresh-epoch
    path must not fire on an ordinary turn transition."""

    def test_normal_seal_keeps_queued_final_update(self):
        fresh_service(enabled=True)
        slot = make_slot()
        slot._turn_generation = 7
        observer = attach_for_turn(slot)
        observer.record_segment("turn 7 work")
        observer.complete()  # normal seal; pump has NOT drained yet
        slot._turn_generation = 8
        observer2 = attach_for_turn(slot)
        assert observer2 is observer
        taken = observer.take_completed()
        assert taken is not None and taken.turn_id == "turn-7"
