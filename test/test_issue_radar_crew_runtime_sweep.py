"""Which turn a crew runs, and what the watcher's sweep wakes.

Every crew turn is charged against the background-turn cap and the triage model is chosen
from the ledger before each one. Each unblock signal has a detector and a cold fingerprint
reports nothing; the sweep seeds, wakes the owning crew within its API cost and leaves a
failed read's mark alone; and closing a crew's chat tab pauses that crew, and only that
case. The shared fakes and autouse fixtures live in ``issue_radar_crew_runtime_helpers.py``.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from issue_radar_crew_runtime_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    OWNER,
    REPO,
    _app,
    _app_is_on,
    _crew,
    _FakeClient,
    _FakeLoop,
    _FakeNudge,
    _FakeSlot,
    _FakeState,
    _isolated_crew_log,
    _item,
    _private_sel_root_per_test,
    _unit_for,
)
from off_loop_helpers import off_loop

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider
from kiro_crew.apps.builtins.issue_radar.backend import store as store_mod
from kiro_crew.apps.builtins.issue_radar.backend import watch as watch_mod

_KEY = provider.key_from_parts(OWNER, REPO)


# ── turn dispatch runs under the background-turn cap ────────────────────────


class TestTurnDispatch(unittest.IsolatedAsyncioTestCase):
    """Every crew turn must be charged against the background-turn cap.

    The cap lives INSIDE ``DashboardState.run_background_turn``, so handing
    ``_run_chat`` straight to ``enqueue_or_run_prompt`` would be uncapped, and crews
    arm N independent loops. Dispatch goes through
    :func:`crew_runtime.dispatch_crew_turn`; the wake is pinned here.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def test_a_wake_hands_the_slot_the_capped_runner(self):
        crew = _crew(self.root, unattended=True)
        state = _FakeState()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        started = await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertTrue(started)
        self.assertEqual(slot.runners, [cr._capped_run_chat])

    async def test_the_dispatched_turn_actually_goes_through_the_cap(self):
        """Not just "a wrapper was handed over": the wrapper is run, and the turn it
        starts is the one the cap holds a permit for."""
        state = _FakeState()
        slot = _FakeSlot()
        ran: list[str] = []
        origins: list[bool | None] = []
        actors: list[str | None] = []

        # ``**_rest`` on purpose: this double stands in for ``_run_chat``, whose
        # keyword surface grows, and a double that enumerates it fails on the next
        # argument added rather than on anything this test is about. The two
        # keywords it DOES name are the two it asserts on.
        async def _turn(
            _state: Any,
            _slot: Any,
            prompt: str,
            *,
            _directive_user_origin: bool | None = None,
            _turn_actor: str | None = None,
            **_rest: Any,
        ) -> None:
            ran.append(prompt)
            origins.append(_directive_user_origin)
            actors.append(_turn_actor)

        with mock.patch.object(cr, "_run_chat", _turn):
            self.assertTrue(cr.dispatch_crew_turn(state, slot, "advance one item"))
            await slot.runners[-1](state, slot, slot.prompts[-1])
        self.assertEqual(state.capped, [slot.key])
        self.assertEqual(ran, ["advance one item"])
        self.assertEqual(origins, [False])
        # A crew-composed prompt is not a person typing, and the session ledger
        # records who caused a turn as fact.
        self.assertEqual(actors, ["crew"])

    async def test_a_turn_that_never_got_a_permit_says_so_in_the_transcript(self):
        """A refused turn and a finished one must not look the same.

        ``run_background_turn`` queues rather than rejecting, so reaching this means
        the cap's whole wait budget expired and NOTHING ran. Reported in the crew's
        own session because that is where a human looks when a crew seems stalled.
        """
        state = _FakeState()
        state.permit_timeout = True
        slot = _FakeSlot()

        async def _turn(
            _state: Any,
            _slot: Any,
            prompt: str,
            *,
            _directive_user_origin: bool | None = None,
            **_rest: Any,
        ) -> None:
            raise AssertionError("the turn must not run without a permit")

        with mock.patch.object(cr, "_run_chat", _turn):
            cr.dispatch_crew_turn(state, slot, "advance one item")
            await slot.runners[-1](state, slot, slot.prompts[-1])
        cards = [m for m in slot.messages if m["role"] == "error"]
        self.assertEqual(len(cards), 1)
        self.assertIn("never started", cards[0]["content"])

    async def test_a_turn_that_ran_leaves_no_card(self):
        state = _FakeState()
        slot = _FakeSlot()
        with mock.patch.object(cr, "_run_chat", mock.AsyncMock()):
            cr.dispatch_crew_turn(state, slot, "advance one item")
            await slot.runners[-1](state, slot, slot.prompts[-1])
        self.assertEqual([m for m in slot.messages if m["role"] == "error"], [])


class TestTriageModel(unittest.IsolatedAsyncioTestCase):
    """A crew's triage model is chosen from its ledger before every turn.

    Pinned at the two places a turn's model is decided: the wake, right before the
    dispatch, and the watchdog pass that keeps the idle-timer turn in step and
    escalates a triage turn that found work.
    """

    WORK = "claude-opus-4.5"
    TRIAGE = "claude-haiku-4.5"

    class _Slot(_FakeSlot):
        """The slot surface the shared model-switch helpers read."""

        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self._model_pick_lock = asyncio.Lock()
            self._eager_spawn_task = None
            self._pending_reset_history_key: str | None = None
            self._model_pick_gen = 0

    class _Sessions:
        def get_provider(self, key: str) -> None:
            return None

    class _State(_FakeState):
        def __init__(self) -> None:
            super().__init__()
            self.sessions = TestTriageModel._Sessions()
            self._slots = self.slots

        def get_or_create_slot(self, name: str = "", model: str = "", **kw: Any) -> _FakeSlot:
            if name not in self.slots:
                self.slots[name] = TestTriageModel._Slot(name, model=model)
            return super().get_or_create_slot(name=name, model=model, **kw)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        from kiro_crew.apps.builtins.issue_radar.backend import crew_model

        crew_model._withheld.clear()
        crew_model._written.clear()
        crew_model._held.clear()
        crew_model._hold_seen_at.clear()
        self.addCleanup(crew_model._withheld.clear)
        self.addCleanup(crew_model._written.clear)
        self.addCleanup(crew_model._held.clear)
        self.addCleanup(crew_model._hold_seen_at.clear)

    async def _wake(self, crew: dict[str, Any]) -> tuple[_FakeSlot, list[str]]:
        state = self._State()
        slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
        seen: list[str] = []
        real = slot.enqueue_or_run_prompt

        def _record(prompt: str, runner: Any, st: Any) -> bool:
            seen.append(slot.model)
            return real(prompt, runner, st)

        slot.enqueue_or_run_prompt = _record  # type: ignore[method-assign]
        self.assertTrue(await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root))
        return slot, seen

    async def test_without_a_triage_model_the_wake_runs_on_the_work_model(self):
        crew = _crew(self.root, unattended=True, model=self.WORK)
        _slot, seen = await self._wake(crew)
        self.assertEqual(seen, [self.WORK])

    async def test_a_wake_with_nothing_acting_runs_on_the_triage_model(self):
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        _item(self.root, crew["id"], 12, phase="awaiting-ci", pr_number=40)
        _slot, seen = await self._wake(crew)
        self.assertEqual(seen, [self.TRIAGE])

    async def test_a_wake_with_an_acting_item_runs_on_the_work_model(self):
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        _item(self.root, crew["id"], 12, phase="implementing")
        _slot, seen = await self._wake(crew)
        self.assertEqual(seen, [self.WORK])

    async def test_the_watchdog_keeps_the_idle_turn_in_step_with_the_ledger(self):
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            self.assertEqual(slot.model, self.TRIAGE)
            _item(self.root, crew["id"], 12, phase="claimed")
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            self.assertEqual(slot.model, self.WORK)

    async def test_a_triage_turn_that_found_work_is_escalated_at_once(self):
        """CI red -> a triage turn records the move -> the next pass dispatches the
        work-model turn itself, instead of leaving it for the idle timer."""
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        _item(self.root, crew["id"], 12, phase="awaiting-ci", pr_number=40)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
            self.assertEqual(slot.model, self.TRIAGE)
            self.assertEqual(len(slot.prompts), 1)
            # What the triage turn did: moved the item into an acting phase.
            _item(self.root, crew["id"], 12, phase="addressing-review")
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertEqual(slot.model, self.WORK)
        self.assertEqual(len(slot.prompts), 2)
        self.assertIn("[crew wake: an item needs the work model]", slot.prompts[-1])

    async def test_a_pass_with_nothing_newly_acting_dispatches_nothing(self):
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertEqual(slot.model, self.TRIAGE)
        self.assertEqual(slot.prompts, [])

    async def test_a_hand_pick_survives_watchdog_passes_and_wakes(self):
        """A model picked in the crew's session is the person's.

        Neither a watchdog pass nor a wake switches over it, the switch back to the
        work model included, and the hold is put on the record so a restart keeps it.
        """
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            self.assertEqual(slot.model, self.TRIAGE)
            # What the model picker commits.
            slot.model = "claude-sonnet-4.5"
            slot._model_pick_gen += 1
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            self.assertEqual(slot.model, "claude-sonnet-4.5")
            _item(self.root, crew["id"], 12, phase="claimed")
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertEqual(slot.model, "claude-sonnet-4.5")
        record = off_loop(cs.read_crew, OWNER, REPO, crew["id"], self.root)
        self.assertIs(record["model_held"], True)
        # An edit to the crew's models hands the slot back.
        off_loop(
            cs.update_crew, OWNER, REPO, crew["id"], {"triage_model": "claude-haiku-5"}, self.root
        )
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertEqual(slot.model, self.WORK)

    async def test_a_held_record_survives_a_restart(self):
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            slot.model = "claude-sonnet-4.5"
            off_loop(
                cs.set_crew_model_held,
                OWNER,
                REPO,
                crew["id"],
                model=self.WORK,
                triage_model=self.TRIAGE,
                seen_releases=cs.hold_releases(crew["id"]),
                root=self.root,
            )
            # A fresh process: nothing in memory, the record says held.
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertEqual(slot.model, "claude-sonnet-4.5")

    async def test_a_wake_records_a_hand_pick_it_sees(self):
        """The GPT finding: a hold is persisted before the page offers to release it.

        The pick is first seen by a wake, with no watchdog pass in between, and the
        wake puts it on the record, which is the only hold the crew page shows.
        """
        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            self.assertEqual(slot.model, self.TRIAGE)
            slot.model = "claude-sonnet-4.5"
            slot._model_pick_gen += 1
            await cr.wake_crew(state, OWNER, REPO, crew, "ci-changed", self.root)
        self.assertEqual(slot.model, "claude-sonnet-4.5")
        record = off_loop(cs.read_crew, OWNER, REPO, crew["id"], self.root)
        self.assertIs(record["model_held"], True)

    async def test_every_watchdog_pass_retries_a_hold_not_yet_recorded(self):
        """A held slot reads no items, and the pass still writes the hold, so a write
        that failed once lands on the next pass rather than never."""
        from kiro_crew.apps.builtins.issue_radar.backend import crew_model

        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            slot.model = "claude-sonnet-4.5"
            # Seen in this process, not on the record (the earlier write failed).
            crew_model._held[crew["id"]] = (self.WORK, self.TRIAGE)
            crew_model._hold_seen_at[crew["id"]] = cs.hold_releases(crew["id"])
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
        self.assertEqual(slot.model, "claude-sonnet-4.5")
        record = off_loop(cs.read_crew, OWNER, REPO, crew["id"], self.root)
        self.assertIs(record["model_held"], True)

    async def test_a_hold_write_queued_across_a_release_does_not_undo_it(self):
        """The second GPT finding: queue a hold write, release, then flush.

        The pick is seen and its record write is queued; "Switch back to triage
        model" lands before the queued write runs; the write then runs. The release
        must win: the record stays released, and so does the next pass.
        """
        from kiro_crew.apps.builtins.issue_radar.backend import crew_model

        crew = _crew(self.root, unattended=True, model=self.WORK, triage_model=self.TRIAGE)
        state = self._State()
        svc = _FakeNudge([_FakeLoop("nl_0", cr._slot_key(crew))])
        real_write = cs.set_crew_model_held
        released: list[dict[str, Any]] = []

        def _release_then_flush(*args: Any, **kwargs: Any) -> bool:
            # The release lands while the hold write is queued behind the lock.
            if not released:
                released.append(
                    cs.update_crew(OWNER, REPO, crew["id"], {"model_held": False}, self.root)
                )
            return real_write(*args, **kwargs)

        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            slot = await cr.ensure_crew_session(state, OWNER, REPO, crew)
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            self.assertEqual(slot.model, self.TRIAGE)
            slot.model = "claude-sonnet-4.5"
            slot._model_pick_gen += 1
            with mock.patch.object(cs, "set_crew_model_held", _release_then_flush):
                await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)
            self.assertEqual(len(released), 1)
            record = off_loop(cs.read_crew, OWNER, REPO, crew["id"], self.root)
            self.assertIs(record["model_held"], False, "the queued hold write undid the release")
            # The route's own step after the release, then the next pass.
            crew_model.note_triage_change(slot, crew, released[0], released=True)
            await cr.watchdog_cycle(state, OWNER, REPO, [record], self.root)
        record = off_loop(cs.read_crew, OWNER, REPO, crew["id"], self.root)
        self.assertIs(record["model_held"], False)

    def test_the_nudge_tells_the_crew_how_a_triage_turn_ends(self):
        crew = _crew(self.root, triage_model=self.TRIAGE)
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertIn(f"Triage model `{self.TRIAGE}`", nudge)
        self.assertIn("end the turn", nudge)
        plain = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, _crew(self.root, "Leo"), self.root))
        self.assertNotIn("Triage model", plain)

    def test_the_nudge_says_nothing_of_triage_while_it_is_not_in_effect(self):
        """A held or withheld triage model leaves every turn on the session's model,
        so a turn must not be told to stop short and leave the work for the idle gap."""
        from kiro_crew.apps.builtins.issue_radar.backend import crew_model

        crew = _crew(self.root, model=self.WORK, triage_model=self.TRIAGE)
        cs.set_crew_model_held(
            OWNER,
            REPO,
            crew["id"],
            model=self.WORK,
            triage_model=self.TRIAGE,
            seen_releases=cs.hold_releases(crew["id"]),
            root=self.root,
        )
        held = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, held, self.root))
        self.assertNotIn("Triage model", nudge)
        crew_model._withheld[crew["id"]] = self.TRIAGE
        nudge = cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root))
        self.assertNotIn("Triage model", nudge)


# ── unblock signal detection (pure) ─────────────────────────────────────────


class TestDetectUnblocks(unittest.TestCase):
    BASE = {
        "issue_comments": 2,
        "checks": "failure",
        "check_counts": {"failure": 1, "success": 40, "running": 0, "other": 2},
        "review_decision": "",
        "conflicted": False,
        "merged": False,
        "pr_comments": 3,
    }

    def _detect(self, **changes):
        return cr.detect_unblocks(dict(self.BASE), {**self.BASE, **changes})

    def test_first_observation_reports_nothing(self):
        # Cold start seeds the mark. Reporting here would wake every crew on every
        # open item the moment the gateway restarts.
        self.assertEqual(cr.detect_unblocks(None, dict(self.BASE)), [])
        self.assertEqual(cr.detect_unblocks({}, dict(self.BASE)), [])

    def test_no_change_reports_nothing(self):
        self.assertEqual(self._detect(), [])

    def test_requester_replied(self):
        self.assertEqual(self._detect(issue_comments=3), [cr.SIG_REPLY])

    def test_ci_state_changed(self):
        self.assertEqual(self._detect(checks="success"), [cr.SIG_CI])

    def test_ci_counts_changed_without_the_rollup_moving(self):
        counts = {"failure": 1, "success": 41, "running": 0, "other": 2}
        self.assertEqual(self._detect(check_counts=counts), [cr.SIG_CI])

    def test_unknown_ci_is_not_a_ci_change(self):
        # A failed enrichment call reports None. Treating unknown-vs-known as
        # movement would wake the crew every time the GraphQL leg flakes.
        self.assertEqual(self._detect(checks=None, check_counts=None), [])

    def test_review_approved_and_changes_requested(self):
        self.assertEqual(self._detect(review_decision="approved"), [cr.SIG_REVIEW])
        self.assertEqual(self._detect(review_decision="changes_requested"), [cr.SIG_REVIEW])

    def test_a_withdrawn_verdict_is_not_a_signal(self):
        prev = {**self.BASE, "review_decision": "approved"}
        self.assertEqual(cr.detect_unblocks(prev, dict(self.BASE)), [])

    def test_merge_conflict_appeared(self):
        self.assertEqual(self._detect(conflicted=True), [cr.SIG_CONFLICT])

    def test_conflict_already_known_is_not_re_reported(self):
        prev = {**self.BASE, "conflicted": True}
        cur = {**self.BASE, "conflicted": True}
        self.assertEqual(cr.detect_unblocks(prev, cur), [])

    def test_pr_merged(self):
        self.assertEqual(self._detect(merged=True), [cr.SIG_MERGED])

    def test_post_merge_comment(self):
        prev = {**self.BASE, "merged": True}
        cur = {**self.BASE, "merged": True, "pr_comments": 4}
        self.assertEqual(cr.detect_unblocks(prev, cur), [cr.SIG_POST_MERGE])

    def test_uncomputed_mergeability_is_not_a_conflict(self):
        # GitHub answers mergeable: null on a cold read and computes it in the
        # background; truthiness would report every cold read as a conflict.
        self.assertFalse(cr._is_conflicted(None, "unknown"))
        self.assertTrue(cr._is_conflicted(False, "unknown"))
        self.assertTrue(cr._is_conflicted(None, "dirty"))

    def test_every_signal_has_a_detector(self):
        # Guards against a signal being added to the table and never wired up.
        seen = set()
        for changes in (
            {"issue_comments": 9},
            {"checks": "success"},
            {"review_decision": "approved"},
            {"conflicted": True},
            {"merged": True},
        ):
            seen.update(self._detect(**changes))
        prev = {**self.BASE, "merged": True}
        seen.update(cr.detect_unblocks(prev, {**prev, "pr_comments": 99}))
        # The dependency-unblocked signal fires on a >0 → 0 blocker transition,
        # which BASE cannot express (it has no blockers), so it gets its own pair.
        seen.update(
            cr.detect_unblocks(
                {**self.BASE, "open_blockers": 1},
                {**self.BASE, "open_blockers": 0},
            )
        )
        self.assertEqual(seen, set(cr.UNBLOCK_SIGNALS))


# ── the sweep ───────────────────────────────────────────────────────────────


class TestSweep(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def _sweep(self, client, state=None):
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=mock.AsyncMock(return_value=True)) as wake,
        ):
            woken = await cr.sweep_repo(_app(state), _KEY, self.root)
        return woken, wake

    async def test_first_sweep_seeds_without_waking(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        client = _FakeClient(issue={"comments": 2, "state": "open"})
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        # The mark is stored, so the SECOND sweep has something to compare against.
        stored = cr.read_signals(OWNER, REPO, self.root)
        self.assertIn(f"{crew['id']}:2201", stored)

    async def test_second_sweep_wakes_the_owning_crew_on_a_reply(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="awaiting-reply")
        client = _FakeClient(issue={"comments": 2, "state": "open"})
        await self._sweep(client, _FakeState())
        # Backdate the mark so the phase's recheck interval has elapsed.
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.issue = {"comments": 3, "state": "open"}
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {crew["id"]: [cr.SIG_REPLY]})
        wake.assert_awaited_once()
        self.assertIn("requester-replied", wake.await_args.args[4])

    async def test_selected_items_are_never_fetched(self):
        # Pre-claim and local only: there is nothing public to watch, and reading
        # it would cost an API call per crew per minute for every shortlisted issue.
        crew = _crew(self.root)
        _item(self.root, crew["id"], 42, phase="selected")
        client = _FakeClient()
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        self.assertEqual(client.calls, [])

    async def test_a_retired_crew_is_not_swept(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        cs.retire_crew(OWNER, REPO, crew["id"], self.root)
        client = _FakeClient()
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        self.assertEqual(client.calls, [])

    async def test_api_cost_per_item_is_two_reads_plus_one_batched_enrichment(self):
        crew = _crew(self.root)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-ci", pr_number=101)
        _item(self.root, cid, 2202, phase="awaiting-ci", pr_number=102)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[
                {"number": 101, "checks_state": "success", "checks_counts": {}},
                {"number": 102, "checks_state": "success", "checks_counts": {}},
            ],
        )
        await self._sweep(client, _FakeState())
        # One issue read + one PR read per item, and ONE batched enrichment for the
        # whole repo (two GraphQL round-trips inside it) — not one per PR.
        self.assertEqual(client.calls.count("enrich"), 1)
        self.assertEqual(client.calls.count("issue:2201"), 1)
        self.assertEqual(client.calls.count("pr:101"), 1)
        self.assertEqual(len([c for c in client.calls if c.startswith("timeline")]), 2)

    async def test_review_read_is_skipped_when_the_pr_did_not_move(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci", pr_number=101)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[{"number": 101, "checks_state": "success", "checks_counts": {}}],
        )
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)
        client.calls.clear()
        await self._sweep(client, _FakeState())
        # updated_at unchanged -> the paginated timeline read is not paid again.
        self.assertNotIn("timeline:101", client.calls)

    async def test_review_verdict_is_read_when_the_pr_moved(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="addressing-review", pr_number=101)
        client = _FakeClient(
            issue={"comments": 1, "state": "open"},
            pr={"comments": 0, "updated_at": "t0", "merged": False, "mergeable": True},
            enriched=[{"number": 101, "checks_state": "success", "checks_counts": {}}],
        )
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.pr = {"comments": 0, "updated_at": "t1", "merged": False, "mergeable": True}
        client.timeline = [
            {"kind": "reviewed", "review_state": "APPROVED", "created_at": "t1"},
        ]
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {crew["id"]: [cr.SIG_REVIEW]})
        wake.assert_awaited_once()

    async def test_a_failed_item_read_leaves_its_mark_untouched(self):
        crew = _crew(self.root)
        _item(self.root, crew["id"], 2201, phase="awaiting-ci")
        client = _FakeClient()
        client.get_issue_detail = mock.Mock(side_effect=RuntimeError("gh exploded"))
        woken, _ = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {})
        # No mark: the change (whatever it was) is still pending next cycle rather
        # than being silently consumed by the error.
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})

    async def test_recheck_cadence_is_phase_aware(self):
        self.assertLess(cr.RECHECK_SEC["awaiting-ci"], cr.RECHECK_SEC["awaiting-reply"])
        stored = {"checked_at": 1000.0}
        self.assertTrue(cr._is_due({"phase": "awaiting-ci"}, stored, 1000.0 + 61))
        self.assertFalse(cr._is_due({"phase": "awaiting-reply"}, stored, 1000.0 + 61))
        # A mark in the future (clock correction) must not park the item forever.
        self.assertTrue(cr._is_due({"phase": "awaiting-reply"}, stored, 900.0))

    async def test_two_signalling_items_wake_the_crew_once_with_both_reasons(self):
        crew = _crew(self.root, unattended=True)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-reply")
        _item(self.root, cid, 2202, phase="awaiting-reply")
        client = _FakeClient(issue={"comments": 1, "state": "open"})
        await self._sweep(client, _FakeState())
        stored = cr.read_signals(OWNER, REPO, self.root)
        for k in stored:
            stored[k]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

        client.issue = {"comments": 5, "state": "open"}
        woken, wake = await self._sweep(client, _FakeState())
        self.assertEqual(woken, {cid: [cr.SIG_REPLY, cr.SIG_REPLY]})
        # ONE turn, both reasons — the second call would have been dropped as
        # mid-turn, so the crew would only have heard about the first item.
        wake.assert_awaited_once()
        reason = wake.await_args.args[4]
        self.assertIn("#2201", reason)
        self.assertIn("#2202", reason)

    async def test_marks_for_finished_items_are_pruned(self):
        crew = _crew(self.root, unattended=True)
        cid = crew["id"]
        _item(self.root, cid, 2201, phase="awaiting-ci")
        client = _FakeClient(issue={"comments": 1, "state": "open"})
        await self._sweep(client, _FakeState())
        self.assertIn(f"{cid}:2201", cr.read_signals(OWNER, REPO, self.root))
        # Resolved items leave the open set, so their fingerprints must go too —
        # otherwise a long-lived crew rewrites every issue it ever closed, every
        # minute, forever.
        _item(self.root, cid, 2201, phase="resolved")
        await self._sweep(client, _FakeState())
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})

    async def test_schema_mismatch_is_a_cache_miss(self):
        cr.signals_path(OWNER, REPO, self.root).write_text(
            '{"schema": 999, "items": {"c_x:1": {"fp": {}}}}'
        )
        self.assertEqual(cr.read_signals(OWNER, REPO, self.root), {})


# ── how the sweep is gated in the poll loop ─────────────────────────────────


class TestDismissal(unittest.IsolatedAsyncioTestCase):
    """Closing a crew's chat tab must PAUSE that crew — and only that case.

    The watchdog re-arms a live crew's missing nudge loop (right after a restart), so
    without the pause it would resurrect a dismissed tab. Gating on ``closed``/
    ``closed_at`` instead would also stop merely idle-archived crews, so both
    directions are asserted.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _teardown_mod(self):
        """Imported per-test, never in ``setUp``.

        Otherwise a missing seam would fail every test in the class, including
        ``test_a_crew_with_no_loop_and_no_dismissal_IS_re_armed``, which must not depend on it.
        """
        from kiro_crew.apps import teardown

        self.addCleanup(teardown.unregister_slot_close_hook, cr.APP_NAME)
        return teardown

    def _repos(self):
        """``list_connected_repos`` is what turns a bare slot key into a repo."""
        return mock.patch.object(
            cr.store,
            "list_connected_repos",
            return_value=[{"owner": OWNER, "repo": REPO}],
        )

    async def _sweep(self, crew, state, nudge):
        with mock.patch.object(cr, "_autonudge_instance", return_value=nudge):
            await cr.watchdog_cycle(state, OWNER, REPO, [crew], self.root)

    # ── the fix ─────────────────────────────────────────────────────────────

    async def test_dismissing_the_tab_pauses_the_crew(self):
        crew = _crew(self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertFalse(after["enabled"])
        self.assertEqual(after["paused_reason"], cr.DISMISSED_PAUSE_REASON)
        # Visible AND not live, so the sweep's existing revocation branch takes it
        # from here — no new watchdog state.
        self.assertFalse(cr.is_live(after))

    async def test_taking_the_dismissal_back_resumes_the_crew(self):
        """A close that failed after the pause must not leave the worker stopped.

        The close cannot make the slot table, the history file and the crew store
        atomic, so the pause has to be undoable: without this the user gets an
        error AND a silently disabled crew.
        """
        crew = _crew(self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
            paused = cs.read_crew(OWNER, REPO, crew["id"], self.root)
            assert paused is not None
            self.assertFalse(paused["enabled"])
            await cr._on_slot_close_undone(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertTrue(after["enabled"], "the crew stayed paused after a failed close")
        self.assertFalse(after.get("paused_reason"))

    async def test_taking_it_back_never_resumes_a_pause_someone_else_set(self):
        """Only THIS hook's own pause is undone.

        ``_on_slot_closed`` refuses to overwrite an existing ``paused_reason``, so a
        crew stopped for any other cause was never touched by the close — resuming
        it would turn a failed tab close into a worker restart nobody asked for.
        """
        crew = _crew(self.root)
        cs.set_crew_paused(OWNER, REPO, crew["id"], True, "you paused this", self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
            await cr._on_slot_close_undone(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertFalse(after["enabled"], "someone else's pause was overridden")
        self.assertEqual(after.get("paused_reason"), "you paused this")

    async def test_a_dismissed_crew_is_not_re_armed(self):
        """The regression. A live crew, loop removed, tab dismissed."""
        crew = _crew(self.root)
        state = _FakeState()
        nudge = _FakeNudge()  # the close handler already removed the loop
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        dismissed = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert dismissed is not None
        await self._sweep(dismissed, state, nudge)
        self.assertEqual(nudge.added, [])
        self.assertEqual(state.created, [])

    async def test_a_crew_with_no_loop_and_no_dismissal_IS_re_armed(self):
        """The behaviour the fix must not cost: recovery after a restart.

        Same observable input as the test above — live crew, no loop, no resident
        slot — differing only in that nobody dismissed it. An unattended crew with
        no loop has no clock at all, so this MUST re-arm.
        """
        crew = _crew(self.root)
        state = _FakeState()
        nudge = _FakeNudge()
        with mock.patch.object(cr, "_rehydrate", new=mock.AsyncMock(return_value=None)):
            await self._sweep(crew, state, nudge)
        self.assertEqual(nudge.added, [crew["slot_key"]])

    async def test_idle_archival_does_not_reach_the_hook(self):
        """Only a deliberate ✕ pauses. Quietness must leave the record alone."""
        teardown = self._teardown_mod()
        crew = _crew(self.root)
        calls: list[str] = []

        async def _hook(slot_key: str) -> None:
            calls.append(slot_key)

        teardown.register_slot_close_hook(cr.APP_NAME, _hook)
        # The bulk idle-archive path persists ``closed=True`` + ``closed_at`` for
        # this same slot and never notifies — which is the whole distinction.
        await teardown.notify_slot_closed("some-other-app", crew["slot_key"])
        self.assertEqual(calls, [])
        await teardown.notify_slot_closed(cr.APP_NAME, crew["slot_key"])
        self.assertEqual(calls, [crew["slot_key"]])

    # ── the seam ────────────────────────────────────────────────────────────

    async def test_the_watchdog_re_registers_the_hook_after_a_restart(self):
        """The registry is process memory, so a one-shot registration would leave
        the ✕ silently ignored for the rest of that process's life."""
        teardown = self._teardown_mod()
        crew = _crew(self.root)
        teardown.unregister_slot_close_hook(cr.APP_NAME)
        await self._sweep(crew, _FakeState(), _FakeNudge())
        self.assertIn(cr.APP_NAME, teardown._SLOT_CLOSE_HOOKS)

    async def test_the_watcher_registers_the_hook_before_it_ever_sweeps(self):
        """The watchdog's registration is a full poll interval away.

        ``_watch_loop`` sleeps ``POLL_INTERVAL_SEC`` before its first sweep, so the
        loop must register on entry. ``_poll_once`` aborts the loop so no sweep runs.
        """
        from kiro_crew.apps.builtins.issue_radar.backend import watch

        teardown = self._teardown_mod()
        teardown.unregister_slot_close_hook(cr.APP_NAME)

        async def _abort(_app):
            raise asyncio.CancelledError

        with (
            mock.patch.object(watch, "POLL_INTERVAL_SEC", 0),
            mock.patch.object(watch, "_poll_once", _abort),
        ):
            await watch._watch_loop(mock.MagicMock())

        self.assertIn(cr.APP_NAME, teardown._SLOT_CLOSE_HOOKS)

    async def test_the_disable_hook_is_installed_before_the_first_sleep(self):
        """Same timing argument, sharper consequence.

        Until it is registered, disabling the app leaves crews their auto-approve grants
        and armed loops. It must capture the gateway's state; a hook holding nothing
        revokes nothing.
        """
        installed: list[Any] = []

        async def _abort(_app):
            raise asyncio.CancelledError

        with (
            mock.patch.object(watch_mod, "POLL_INTERVAL_SEC", 0),
            mock.patch.object(watch_mod, "_poll_once", _abort),
            mock.patch.object(
                watch_mod.crew_runtime,
                "install_app_disable_hook",
                side_effect=lambda state: bool(installed.append(state)) or True,
            ),
        ):
            await watch_mod._watch_loop(cast(Any, {"state": "the-gateway-state"}))
        self.assertEqual(installed, ["the-gateway-state"])

    async def test_a_crew_on_another_providers_root_is_still_found(self):
        """The one per-app hook carries the last swept repo's PROVIDER root.

        Registered on a root that does NOT hold the crew, it must still find it through
        the provider fallback (mixed GitHub/GitLab installs).
        """
        teardown = self._teardown_mod()
        crew = _crew(self.root)
        elsewhere = Path(self._tmp.name) / "other-provider"
        elsewhere.mkdir(parents=True, exist_ok=True)

        # Registered against a root with no crews in it at all.
        cr.install_slot_close_hook(elsewhere)
        with (
            self._repos(),
            mock.patch.object(cr, "_lookup_scopes", return_value=[elsewhere, self.root]),
        ):
            await teardown.notify_slot_closed(cr.APP_NAME, str(crew["slot_key"]))

        after = cs.read_crew(OWNER, REPO, str(crew["id"]), self.root)
        assert after is not None
        self.assertEqual(after.get("paused_reason"), cr.DISMISSED_PAUSE_REASON)

    async def test_a_failing_hook_never_blocks_the_close(self):
        teardown = self._teardown_mod()

        async def _boom(slot_key: str) -> None:
            raise RuntimeError("store busy")

        teardown.register_slot_close_hook(cr.APP_NAME, _boom)
        # Reported, not raised: the close path refuses to proceed on False.
        self.assertFalse(await teardown.notify_slot_closed(cr.APP_NAME, "crew-c_1"))

    async def test_an_unknown_slot_key_is_ignored(self):
        crew = _crew(self.root)
        with self._repos():
            await cr._on_slot_closed("crew-c_deadbeef", root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertTrue(after["enabled"])
        self.assertEqual(after.get("paused_reason") or "", "")

    async def test_a_specific_pause_reason_is_not_overwritten(self):
        crew = _crew(self.root)
        cs.set_crew_paused(OWNER, REPO, crew["id"], True, "waiting on a decision", self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        after = cs.read_crew(OWNER, REPO, crew["id"], self.root)
        assert after is not None
        self.assertEqual(after["paused_reason"], "waiting on a decision")

    async def test_resuming_clears_the_reason_and_re_arms(self):
        """Reversible: the existing pause route's resume brings the crew back."""
        crew = _crew(self.root)
        with self._repos():
            await cr._on_slot_closed(crew["slot_key"], root=self.root)
        resumed = cs.set_crew_paused(OWNER, REPO, crew["id"], False, "", self.root)
        self.assertTrue(resumed["enabled"])
        self.assertEqual(resumed["paused_reason"], "")
        self.assertTrue(cr.is_live(resumed))
        nudge = _FakeNudge()
        with mock.patch.object(cr, "_rehydrate", new=mock.AsyncMock(return_value=None)):
            await self._sweep(resumed, _FakeState(), nudge)
        self.assertEqual(nudge.added, [crew["slot_key"]])


# ── a human answer hands a needs-human pass back ────────────────────────────


#: The crew log's clock, pinned per test so a pass, a label removal and a re-skip
#: are ordered by construction rather than by how fast the test ran.
_T0_MS = 1_800_000_000_000
_NEEDS_HUMAN = "crew: needs human"


def _iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _AnswerClient(_FakeClient):
    """The forge, as the answer detection sees it: who we are, and label events."""

    def __init__(self, label_events=None, login="crew-bot"):
        super().__init__()
        self.label_events = list(label_events or [])
        self.login = login

    def get_current_login(self, **kw):
        self.calls.append("login")
        return self.login

    def list_issue_timeline(self, owner, repo, number, **kw):
        self.calls.append(f"label-events:{number}")
        return list(self.label_events)

    #: Set by the test's sweep: the forge's open issues are the ones the test wrote.
    forge_root: Path | None = None
    forge_fails = False

    def list_open_issues_first_page(self, owner, repo, **kw):
        self.calls.append("open-issues")
        if self.forge_fails:
            raise RuntimeError("forge down")
        return store_mod.read_issues_cache(owner, repo, self.forge_root, state="open") or []


class _OpenIssuesAnswerClient(_AnswerClient):
    """An answer client that can also list open issues from the forge."""

    def __init__(self, open_issues, label_events=None, login="crew-bot"):
        super().__init__(label_events, login)
        self.open_issues = list(open_issues)

    def list_open_issues_first_page(self, owner, repo, **kw):
        self.calls.append("open-issues")
        return [dict(i) for i in self.open_issues]


def _unlabeled(actor: str, at_ms: int, label: str = _NEEDS_HUMAN) -> dict[str, Any]:
    return {
        "kind": "unlabeled",
        "actor": actor,
        "created_at": _iso(at_ms),
        "label": {"name": label},
    }


def _labeled(actor: str, at_ms: int, label: str = _NEEDS_HUMAN) -> dict[str, Any]:
    return {**_unlabeled(actor, at_ms, label), "kind": "labeled"}


class TestForgeIdentityFresh(unittest.TestCase):
    """The answer sweep must see a login switched mid-run, on Azure too, whose client
    caches the identity for the life of the process."""

    def test_azure_identity_is_read_past_its_cache(self):
        from kiro_crew.apps.builtins.issue_radar.backend import azure_client as az

        guid = "11111111-2222-3333-4444-555555555555"
        fresh = {"authenticatedUser": {"id": guid, "uniqueName": "bob"}}
        with (
            mock.patch.dict(az._identity_cache, {"contoso": {"id": guid, "login": "ada"}}),
            mock.patch.object(az, "_default_organization", return_value="contoso"),
            mock.patch.object(az, "_az_invoke", return_value=fresh),
        ):
            self.assertEqual(az.get_current_login(), "ada")  # cached
            self.assertEqual(az.get_current_login_uncached(), "bob")

    def test_azure_identity_asks_the_repos_organization_not_the_cli_default(self):
        """A repo connected by URL needs no CLI default organization."""
        from kiro_crew.apps.builtins.issue_radar.backend import azure_client as az

        guid = "11111111-2222-3333-4444-555555555555"
        fresh = {"authenticatedUser": {"id": guid, "uniqueName": "bob"}}
        with (
            mock.patch.dict(az._identity_cache, {}, clear=True),
            mock.patch.object(
                az, "_default_organization", side_effect=AssertionError("no default")
            ),
            mock.patch.object(az, "_az_invoke", return_value=fresh) as invoke,
        ):
            self.assertEqual(az.get_current_login_uncached(owner="contoso/Widgets"), "bob")
        self.assertIn("contoso", str(invoke.call_args))

    def test_the_sweep_uses_the_uncached_login_read_when_a_client_has_one(self):
        client = mock.Mock()
        client.get_current_login_uncached.return_value = "bob"
        client.get_current_login.return_value = "ada"
        key = provider.RepoKey(
            provider=provider.AZURE, host="dev.azure.com", owner="contoso/Widgets", repo="ledger"
        )
        with mock.patch.object(cr.provider, "client_for", return_value=client):
            self.assertEqual(cr._forge_identity(key), "bob")
        client.get_current_login.assert_not_called()
        self.assertEqual(
            client.get_current_login_uncached.call_args.kwargs["owner"], "contoso/Widgets"
        )


class TestHumanAnswer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.now = [_T0_MS]
        from kiro_crew.crew_log import store as crew_log_store

        clock = mock.patch.object(crew_log_store, "now_ms", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.crew = _crew(self.root, unattended=True)
        self.cid = self.crew["id"]

    def _skip(self, number: int, scope: str = "needs-decision") -> dict[str, Any]:
        return off_loop(
            cs.commit_work_progress,
            OWNER,
            REPO,
            self.cid,
            number,
            {"phase": "skipped"},
            "skip",
            "asked which fix to take",
            skip_reason="two valid fixes; a maintainer has to pick",
            skip_scope=scope,
            root=self.root,
            session_id=_unit_for(self.cid),
        )

    def _issue(self, number: int, labels: list[str]) -> None:
        store_mod.write_issues_cache(
            OWNER, REPO, [{"number": number, "state": "open", "labels": labels}], root=self.root
        )

    def _skipped(self) -> set[int]:
        return {row["number"] for row in off_loop(cs.read_skips, OWNER, REPO, self.root).values()}

    async def _sweep(self, client):
        if isinstance(client, _AnswerClient):
            client.forge_root = self.root
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=mock.AsyncMock(return_value=True)) as wake,
        ):
            woken = await cr.sweep_repo(_app(_FakeState()), _KEY, self.root)
        return woken, wake

    async def test_label_removed_by_a_human_unskips_and_wakes_the_skipping_crew(self):
        self._skip(7)
        self._issue(7, labels=["bug"])
        self.assertIn(7, self._skipped())
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})
        wake.assert_awaited_once()
        self.assertEqual(wake.await_args.args[4], "#7 human-answered by @alice")
        self.assertNotIn(7, self._skipped())
        answers = off_loop(cs.read_answers, OWNER, REPO, self.root)
        self.assertEqual(answers["7"]["actor"], "alice")
        # The pass is kept as the audit trail; only the index stops answering for it.
        ledger = off_loop(cs.read_ledger, OWNER, REPO, self.cid, self.root)
        self.assertIn("7", ledger["skips"])
        # The answer is kept in the app's own file, not in the crew log.
        self.assertTrue(cr.answer_checks_path(OWNER, REPO, self.root).is_file())
        self.assertNotIn("reply", [event["kind"] for event in ledger["events"]])
        item = off_loop(cs.read_work_item, OWNER, REPO, self.cid, 7, self.root)
        self.assertEqual(item["phase"], "skipped")

        # Recorded once: the next sweep has nothing left to read or wake.
        client.calls.clear()
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {})
        self.assertNotIn("label-events:7", client.calls)

    async def test_a_label_put_back_after_its_removal_is_no_answer(self):
        """Removed and then re-added: the forge still shows the label, so the question
        stands and the issue stays skipped."""
        self._skip(7)
        client = _OpenIssuesAnswerClient(
            [{"number": 7, "state": "open", "labels": [_NEEDS_HUMAN]}],
            [_unlabeled("alice", _T0_MS + 60_000)],
        )
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertIn(7, self._skipped())

    async def test_a_removal_the_label_was_put_back_after_does_not_count(self):
        """A maintainer removed the label and put it back; the crew's login then
        removed it. Only the last removal counts, and it is the crew's own."""
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient(
            [
                _unlabeled("alice", _T0_MS + 60_000),
                _labeled("alice", _T0_MS + 70_000),
                _unlabeled("crew-bot", _T0_MS + 80_000),
            ],
            login="crew-bot",
        )
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertIn(7, self._skipped())

    def test_only_the_newest_removal_counts_on_ties_and_without_an_actor(self):
        since = cs.normalize_forge_time(_iso(_T0_MS))
        tie = [
            _unlabeled("alice", _T0_MS + 60_000),
            _labeled("alice", _T0_MS + 60_000),
            _unlabeled("crew-bot", _T0_MS + 60_000),
        ]
        self.assertIsNone(cr.find_human_answer(tie, _NEEDS_HUMAN, since, "crew-bot"))
        anonymous = [
            _unlabeled("alice", _T0_MS + 60_000),
            _labeled("alice", _T0_MS + 70_000),
            _unlabeled("", _T0_MS + 80_000),
        ]
        self.assertIsNone(cr.find_human_answer(anonymous, _NEEDS_HUMAN, since, "crew-bot"))
        put_back = [_unlabeled("alice", _T0_MS + 60_000), _labeled("bob", _T0_MS + 70_000)]
        self.assertIsNone(cr.find_human_answer(put_back, _NEEDS_HUMAN, since, "crew-bot"))
        plain = [_unlabeled("alice", _T0_MS + 60_000)]
        self.assertEqual(
            cr.find_human_answer(plain, _NEEDS_HUMAN, since, "crew-bot")["actor"], "alice"
        )

    async def test_an_answer_evicted_as_it_is_written_wakes_no_one(self):
        """Past the store's answer bound an old removal is evicted the moment it is
        written: the pass still stands, so no wake, and it is not written again."""
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as pr

        self._skip(7)
        self._skip(8)
        self._issue(7, labels=[_NEEDS_HUMAN])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 90_000)])
        self.now[0] = _T0_MS + 120_000
        with mock.patch.object(pr, "ANSWERS_LIMIT", 1):
            store_mod.write_issues_cache(
                OWNER,
                REPO,
                [{"number": n, "state": "open", "labels": [_NEEDS_HUMAN]} for n in (7,)]
                + [{"number": 8, "state": "open", "labels": []}],
                root=self.root,
            )
            woken, _ = await self._sweep(client)
            self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})
            # Now #7's older removal: it is the oldest answer, so it is evicted.
            client.label_events = [_unlabeled("alice", _T0_MS + 60_000)]
            store_mod.write_issues_cache(
                OWNER,
                REPO,
                [{"number": n, "state": "open", "labels": []} for n in (7, 8)],
                root=self.root,
            )
            woken, wake = await self._sweep(client)
            self.assertEqual(woken, {})
            wake.assert_not_awaited()
            self.assertIn(7, self._skipped())
            client.calls.clear()
            woken, _ = await self._sweep(client)
            self.assertEqual(woken, {})
            self.assertNotIn("label-events:7", client.calls)

    async def test_removing_another_label_is_no_answer(self):
        self._skip(7)
        self._issue(7, labels=["bug"])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000, label="bug")])
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertIn(7, self._skipped())

    async def test_an_answer_on_an_issue_the_stale_cache_never_saw_still_wakes(self):
        """The dashboard cache is refreshed only when a person opens Issue Radar. An issue
        filed after that is not in it, and the answer must still be read from the forge."""
        self._skip(8)
        self._issue(3, labels=[_NEEDS_HUMAN])  # the cache: an older issue, no #8
        client = _OpenIssuesAnswerClient(
            [{"number": 8, "state": "open", "labels": ["bug"]}],
            [_unlabeled("alice", _T0_MS + 60_000)],
        )
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})
        self.assertEqual(wake.await_args.args[4], "#8 human-answered by @alice")
        self.assertNotIn(8, self._skipped())

    async def test_the_forge_list_wins_over_a_cache_that_still_shows_the_label(self):
        self._skip(7)
        self._issue(7, labels=[_NEEDS_HUMAN])  # stale: the label was removed since
        client = _OpenIssuesAnswerClient(
            [{"number": 7, "state": "open", "labels": []}],
            [_unlabeled("alice", _T0_MS + 60_000)],
        )
        self.now[0] = _T0_MS + 120_000

        woken, _wake = await self._sweep(client)

        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

    async def test_an_answer_off_the_first_page_is_found_by_reading_the_issue(self):
        """Newer activity pushed the answered issue off the one-page read: the issue
        is read directly, and the answer still hands it back."""
        self._skip(8)
        page = [{"number": 100 + i, "state": "open", "labels": []} for i in range(3)]
        client = _OpenIssuesAnswerClient(page, [_unlabeled("alice", _T0_MS + 60_000)])
        client.issue = {"number": 8, "state": "open", "labels": [], "updated_at": "u"}
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})
        self.assertEqual(wake.await_args.args[4], "#8 human-answered by @alice")
        self.assertNotIn(8, self._skipped())

    async def test_an_off_page_issue_is_read_at_most_once_per_window(self):
        """Still labelled, or closed: one direct read, then nothing until the retry
        window passes, so off-page passes cannot spend every sweep's budget."""
        self._skip(8)
        self._skip(9)
        client = _OpenIssuesAnswerClient([{"number": 100, "state": "open", "labels": []}])
        details = {
            8: {"number": 8, "state": "open", "labels": [{"name": _NEEDS_HUMAN}]},
            9: {"number": 9, "state": "closed", "labels": []},
        }
        reads: list[int] = []

        def detail(owner, repo, number, **kw):
            reads.append(number)
            return details[number]

        client.get_issue_detail = detail
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        self.assertEqual(sorted(reads), [8, 9])
        await self._sweep(client)
        self.assertEqual(sorted(reads), [8, 9])
        later = cr._answer_clock() + cr.INCOMPLETE_READ_RETRY_SECS + 1
        with mock.patch.object(cr, "_answer_clock", return_value=later):
            await self._sweep(client)
        self.assertEqual(sorted(reads), [8, 8, 9, 9])
        self.assertIn(8, self._skipped())
        self.assertIn(9, self._skipped())

    async def test_an_off_page_read_is_recorded_when_the_identity_is_unknown(self):
        """The direct read was spent but the crew's login cannot be read: the read
        is still recorded, so the same issue is not read again every sweep."""
        self._skip(8)
        client = _OpenIssuesAnswerClient(
            [{"number": 100, "state": "open", "labels": []}], [], login=""
        )
        reads: list[int] = []

        def detail(owner, repo, number, **kw):
            reads.append(number)
            return {"number": number, "state": "open", "labels": [], "updated_at": "u"}

        client.get_issue_detail = detail
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        await self._sweep(client)
        self.assertEqual(reads, [8])

    async def test_an_off_page_answer_behind_closed_passes_is_still_found(self):
        """Closed passes ahead of it in probe order use up the budget; the issue read
        is only spent when its timeline read fits too, so the answer is found."""
        closed = list(range(1, cr.MAX_ANSWER_READS_PER_SWEEP))
        for n in [*closed, 50]:
            self._skip(n)
        client = _OpenIssuesAnswerClient(
            [{"number": 900, "state": "open", "labels": []}],
            [_unlabeled("alice", _T0_MS + 60_000)],
        )

        def detail(owner, repo, number, **kw):
            state = "open" if number == 50 else "closed"
            return {"number": number, "state": state, "labels": [], "updated_at": "u"}

        client.get_issue_detail = detail
        self.now[0] = _T0_MS + 120_000
        found: dict[str, Any] = {}
        for _ in range(3):
            woken, _wake = await self._sweep(client)
            found.update(woken)
        self.assertEqual(found, {self.cid: [cr.SIG_HUMAN_ANSWERED]})
        self.assertNotIn(50, self._skipped())

    async def test_off_page_reads_rotate_under_the_budget(self):
        """More off-page passes than one sweep's budget: each sweep reads the ones
        read least recently, so all of them are reached."""
        numbers = list(range(1, cr.MAX_ANSWER_READS_PER_SWEEP + 4))
        for n in numbers:
            self._skip(n)
        client = _OpenIssuesAnswerClient([{"number": 900, "state": "open", "labels": []}])
        reads: list[int] = []

        def detail(owner, repo, number, **kw):
            reads.append(number)
            return {"number": number, "state": "open", "labels": [{"name": _NEEDS_HUMAN}]}

        client.get_issue_detail = detail
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        # One read is always kept for the timeline an open, unlabelled issue needs.
        self.assertEqual(len(reads), cr.MAX_ANSWER_READS_PER_SWEEP - 1)
        await self._sweep(client)
        self.assertEqual(sorted(set(reads)), numbers)

    def test_the_forge_read_keeps_only_number_labels_and_update_time(self):
        client = _OpenIssuesAnswerClient(
            [{"number": 8, "labels": ["x"], "updated_at": "9" * 500, "body": "b" * 10_000}]
        )
        with mock.patch.object(cr.provider, "client_for", return_value=client):
            got = cr._read_updated_open_issues(_KEY)
        assert got is not None
        self.assertEqual(set(got[0]), {"number", "labels", "updated_at"})
        self.assertEqual(len(got[0]["updated_at"]), 40)

    async def test_a_mark_is_re_armed_when_the_issue_is_updated_again(self):
        """A read that finds no human removal is remembered only against the issue's
        update time. If the timeline lagged the list, anything that moves the issue
        later re-arms the read, and the answer is found then."""
        self._skip(7)
        client = _OpenIssuesAnswerClient(
            [{"number": 7, "labels": [], "updated_at": "2026-01-01T00:01:00Z"}],
            [_labeled("crew-bot", _T0_MS)],
        )
        self.now[0] = _T0_MS + 120_000
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {})
        # Same update time: the mark holds and nothing is read.
        client.calls.clear()
        await self._sweep(client)
        self.assertNotIn("label-events:7", client.calls)
        # The issue moved: read again, and the late removal is found.
        client.open_issues = [{"number": 7, "labels": [], "updated_at": "2026-01-01T00:05:00Z"}]
        client.label_events = [_unlabeled("alice", _T0_MS + 60_000)]
        client.calls.clear()
        woken, _ = await self._sweep(client)
        self.assertIn("label-events:7", client.calls)
        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

    async def test_a_timeline_without_the_label_is_read_again_after_the_window(self):
        """A GitLab or Azure label-event fetch that failed comes back empty instead of
        raising. That is no lasting "no": the unchanged issue is read again once the
        retry window has passed, and the answer is found then."""
        self._skip(7)
        client = _OpenIssuesAnswerClient(
            [{"number": 7, "labels": [], "updated_at": "2026-01-01T00:01:00Z"}], []
        )
        self.now[0] = _T0_MS + 120_000
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {})
        client.label_events = [_labeled("crew-bot", _T0_MS), _unlabeled("alice", _T0_MS + 60_000)]
        # Inside the window: not read again, so it cannot eat every sweep's budget.
        client.calls.clear()
        woken, _ = await self._sweep(client)
        self.assertNotIn("label-events:7", client.calls)
        # Past it: read again, and the removal is found.
        later = cr._answer_clock() + cr.INCOMPLETE_READ_RETRY_SECS + 1
        client.calls.clear()
        with mock.patch.object(cr, "_answer_clock", return_value=later):
            woken, _ = await self._sweep(client)
        self.assertIn("label-events:7", client.calls)
        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

    async def test_a_timeline_that_lags_the_list_is_read_again_after_the_window(self):
        """The list shows the label off, but the timeline still ends with the label
        being added: the removal has not reached it yet, so the read is retried."""
        self._skip(7)
        client = _OpenIssuesAnswerClient(
            [{"number": 7, "labels": [], "updated_at": "u"}], [_labeled("crew-bot", _T0_MS)]
        )
        self.now[0] = _T0_MS + 120_000
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {})
        client.label_events = [_labeled("crew-bot", _T0_MS), _unlabeled("alice", _T0_MS + 60_000)]
        later = cr._answer_clock() + cr.INCOMPLETE_READ_RETRY_SECS + 1
        with mock.patch.object(cr, "_answer_clock", return_value=later):
            woken, _ = await self._sweep(client)
        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

    async def test_a_timeline_ending_in_an_older_cycle_is_read_again(self):
        """The timeline still ends with a removal from before this pass: the new
        removal the list reflects has not reached it, so the read is retried."""
        self.now[0] = _T0_MS + 60_000
        self._skip(7)
        client = _OpenIssuesAnswerClient(
            [{"number": 7, "labels": [], "updated_at": "u"}],
            [_labeled("crew-bot", _T0_MS - 60_000), _unlabeled("alice", _T0_MS)],
        )
        self.now[0] = _T0_MS + 120_000
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {})
        client.label_events = [
            *client.label_events,
            _labeled("crew-bot", _T0_MS + 60_000),
            _unlabeled("bob", _T0_MS + 90_000),
        ]
        later = cr._answer_clock() + cr.INCOMPLETE_READ_RETRY_SECS + 1
        with mock.patch.object(cr, "_answer_clock", return_value=later):
            woken, _ = await self._sweep(client)
        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

    async def test_a_timeline_read_that_raises_is_retried_after_the_window(self):
        self._skip(7)

        class _Client(_OpenIssuesAnswerClient):
            def list_issue_timeline(self, owner, repo, number, **kw):
                self.calls.append(f"label-events:{number}")
                raise RuntimeError("timeline down")

        client = _Client([{"number": 7, "labels": [], "updated_at": "u"}])
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        client.calls.clear()
        await self._sweep(client)
        self.assertNotIn("label-events:7", client.calls)
        later = cr._answer_clock() + cr.INCOMPLETE_READ_RETRY_SECS + 1
        with mock.patch.object(cr, "_answer_clock", return_value=later):
            await self._sweep(client)
        self.assertIn("label-events:7", client.calls)
        self.assertIn(7, self._skipped())

    async def test_old_passes_without_the_label_do_not_starve_a_real_answer(self):
        """More unlabelled passes than one sweep's read budget: the answer on a
        higher-numbered issue is still found within a couple of sweeps."""
        legacy = list(range(1, cr.MAX_ANSWER_READS_PER_SWEEP + 3))
        for n in [*legacy, 50]:
            self._skip(n, scope="needs-decision")

        class _Client(_OpenIssuesAnswerClient):
            def list_issue_timeline(self, owner, repo, number, **kw):
                self.calls.append(f"label-events:{number}")
                if number == 50:
                    return [_labeled("crew-bot", _T0_MS), _unlabeled("alice", _T0_MS + 60_000)]
                return []

        client = _Client([{"number": n, "labels": [], "updated_at": "u"} for n in [*legacy, 50]])
        self.now[0] = _T0_MS + 120_000
        found: dict[str, Any] = {}
        for _ in range(3):
            woken, _wake = await self._sweep(client)
            found.update(woken)
        self.assertEqual(found, {self.cid: [cr.SIG_HUMAN_ANSWERED]})
        self.assertNotIn(50, self._skipped())

    async def test_a_removal_under_the_crews_own_login_never_counts(self):
        """The crew acts through this login, so a removal under it is never an
        answer: not right away, and not after any wait either."""
        self._skip(7)
        client = _OpenIssuesAnswerClient(
            [{"number": 7, "labels": []}],
            [_unlabeled("Crew-Bot", _T0_MS + 60_000)],
            login="crew-bot",
        )
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertIn(7, self._skipped())
        # Remembered against the pass: the same labelless issue is not re-read,
        # however much later the next sweep runs.
        client.calls.clear()
        self.now[0] = _T0_MS + 86_400_000
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {})
        self.assertNotIn("label-events:7", client.calls)

    async def test_a_failed_forge_read_reads_nothing_and_keeps_the_pass(self):
        """No cache fallback: when the forge page cannot be read, the sweep reads no
        timeline and writes no mark; the next sweep tries again and finds the answer."""
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)], login="crew-bot")
        client.forge_fails = True
        self.now[0] = _T0_MS + 120_000

        woken, _ = await self._sweep(client)

        self.assertEqual(woken, {})
        self.assertNotIn("label-events:7", client.calls)
        self.assertIn(7, self._skipped())
        client.forge_fails = False
        client.calls.clear()
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

    async def test_a_removal_under_the_crew_login_is_logged_once(self):
        """An operator who shares the gateway's login learns why the hand-back did
        nothing: one warning naming the login, not one per sweep."""
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("crew-bot", _T0_MS + 60_000)], login="crew-bot")
        self.now[0] = _T0_MS + 120_000
        with self.assertLogs(cr.logger, level="WARNING") as logs:
            await self._sweep(client)
            await self._sweep(client)
        warned = [m for m in logs.output if "own forge login" in m]
        self.assertEqual(len(warned), 1)
        self.assertIn("@crew-bot", warned[0])

    async def test_a_consumed_answer_never_wakes_again_after_a_slow_clock_reskip(self):
        """A re-skip stamped before the answer (slow clock) still carries the answer it
        followed; the old removal is consumed and wakes no one on later sweeps."""
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000
        woken, _ = await self._sweep(client)
        self.assertEqual(woken, {self.cid: [cr.SIG_HUMAN_ANSWERED]})

        self.now[0] = _T0_MS + 30_000
        self._skip(7, scope="needs-investigation")
        for _ in range(2):
            woken, wake = await self._sweep(client)
            self.assertEqual(woken, {})
            wake.assert_not_awaited()
        self.assertIn(7, self._skipped())

    async def test_the_forge_login_is_read_on_every_sweep(self):
        """A login switched while the gateway runs is seen by the next sweep."""
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)], login="crew-bot")
        self.now[0] = _T0_MS + 120_000
        with mock.patch.object(cr, "find_human_answer", return_value=None):
            await self._sweep(client)
            first = client.calls.count("login")
            # #7 is marked now; a second pass on #8 needs a read, and so the login.
            self._skip(8)
            store_mod.write_issues_cache(
                OWNER,
                REPO,
                [{"number": n, "state": "open", "labels": []} for n in (7, 8)],
                root=self.root,
            )
            await self._sweep(client)
        self.assertGreaterEqual(first, 1)
        self.assertGreater(client.calls.count("login"), first)

    async def test_a_comment_alone_stays_skipped_and_costs_no_call(self):
        self._skip(7)
        # The human commented; the label is still on the issue.
        self._issue(7, labels=[_NEEDS_HUMAN])
        client = _AnswerClient([])

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertIn(7, self._skipped())
        self.assertEqual([c for c in client.calls if c.startswith("label-events")], [])

    async def test_a_duplicate_pass_stays_skipped_whoever_removes_the_label(self):
        self._skip(7, scope="duplicate")
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertIn(7, self._skipped())
        self.assertEqual([c for c in client.calls if c.startswith("label-events")], [])

    async def test_a_scope_gate_pass_stays_skipped_when_a_human_removes_the_label(self):
        """Handing a scope-gate pass back to a crew needs an accepted design first, so
        a person removing the label leaves every scope gate skipped, with no read."""
        for n, scope in enumerate(
            ("architecture", "new-feature", "needs-design", "breaking-change")
        ):
            self._skip(10 + n, scope=scope)
        store_mod.write_issues_cache(
            OWNER,
            REPO,
            [{"number": 10 + n, "state": "open", "labels": []} for n in range(4)],
            root=self.root,
        )
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertTrue({10, 11, 12, 13} <= self._skipped())
        self.assertEqual([c for c in client.calls if c.startswith("label-events")], [])

    async def test_a_needs_investigation_pass_is_handed_back_by_a_maintainer(self):
        self._skip(7, scope="needs-investigation")
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000

        woken, wake = await self._sweep(client)

        self.assertNotIn(7, self._skipped())
        wake.assert_awaited()

    async def test_a_removal_before_the_pass_is_not_an_answer(self):
        self.now[0] = _T0_MS + 60_000
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS)])

        woken, _ = await self._sweep(client)

        self.assertEqual(woken, {})
        self.assertIn(7, self._skipped())

    async def test_a_skip_after_the_answer_indexes_the_number_again(self):
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        self.assertNotIn(7, self._skipped())

        # Still undecided: the crew publishes the question again and skips again. The
        # reply tells the crew what the shared index now holds: the new pass.
        self.now[0] = _T0_MS + 180_000
        reply = self._skip(7, scope="needs-investigation")

        self.assertIn(7, self._skipped())
        row = off_loop(cs.read_skips, OWNER, REPO, self.root)["7"]
        self.assertEqual(row["scope"], "needs-investigation")
        self.assertEqual(reply["skip"]["scope"], "needs-investigation")
        # Once the entry has drained, the fold's own row is the old pass; the reply
        # must still name the pass the index stands on.
        again = off_loop(
            cs._standing_skip, OWNER, REPO, self.root, "7", dict(row, scope="needs-decision")
        )
        self.assertEqual(again["scope"], "needs-investigation")
        # The old removal predates the new pass, so the next sweep leaves it skipped.
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {})
        wake.assert_not_awaited()
        self.assertIn(7, self._skipped())

    async def test_a_reskip_stamped_before_the_answer_by_a_slow_clock_still_stands(self):
        """The local clock runs behind the forge: the re-skip's own time is earlier
        than the answer it follows. The writer saw the answer before recording, and
        that observation, not the clock, puts the pass back in the shared index."""
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        self.assertNotIn(7, self._skipped())

        self.now[0] = _T0_MS + 30_000  # clock stepped back past the forge's answer time
        self._skip(7, scope="needs-investigation")

        self.assertIn(7, self._skipped())
        row = off_loop(cs.read_skips, OWNER, REPO, self.root)["7"]
        self.assertEqual(row["scope"], "needs-investigation")

    async def test_an_identical_skip_after_the_answer_indexes_the_number_again(self):
        """The same pass, word for word, recorded after the answer is a NEW decision.
        The fold's repeat-collapse must not drop it as a retry of the first."""
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        self.assertNotIn(7, self._skipped())

        self.now[0] = _T0_MS + 180_000
        self._skip(7)

        self.assertIn(7, self._skipped())

    async def test_a_reskip_supersedes_an_answer_written_into_another_crews_log(self):
        """The owner was paused, so the sweep wrote the answer where it woke: crew B.
        Crew A's own fold never sees it, and its re-skip must still stand."""
        other = _crew(self.root, name="Bode", unattended=True)
        self._skip(7)
        self.now[0] = _T0_MS + 60_000
        off_loop(
            cs.record_human_answer,
            OWNER,
            REPO,
            other["id"],
            7,
            "alice",
            _iso(_T0_MS + 60_000),
            self.root,
        )
        self.assertNotIn(7, self._skipped())

        self.now[0] = _T0_MS + 120_000
        self._skip(7)

        self.assertIn(7, self._skipped())
        row = off_loop(cs.read_skips, OWNER, REPO, self.root)["7"]
        self.assertEqual(row["crew_id"], self.cid)

    async def test_an_answer_never_promotes_a_deferred_pass_that_lost(self):
        """Crew B passed as a duplicate while crew A's needs-decision stood, so B's row
        is deferred. The answer hands back A's standing row; B's must not take over."""
        other = _crew(self.root, name="Bode", unattended=True)
        self._skip(7)
        self.now[0] = _T0_MS + 30_000
        reply = off_loop(
            cs.commit_work_progress,
            OWNER,
            REPO,
            other["id"],
            7,
            {"phase": "skipped"},
            "skip",
            "same as #3",
            skip_reason="duplicate of #3",
            skip_scope="duplicate",
            root=self.root,
            session_id=_unit_for(other["id"]),
        )
        self.assertEqual(reply["skip"]["scope"], "needs-decision")
        self.now[0] = _T0_MS + 60_000
        off_loop(
            cs.record_human_answer,
            OWNER,
            REPO,
            self.cid,
            7,
            "alice",
            _iso(_T0_MS + 60_000),
            self.root,
        )

        self.assertNotIn(7, self._skipped())

    async def test_a_reskip_whose_append_fails_still_leaves_the_issue_skipped(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store_ledger as ledger

        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        self.assertNotIn(7, self._skipped())
        with mock.patch.object(ledger, "_append", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self._skip(7, scope="needs-investigation")
        # The mark was written first: the issue is skipped again, not handed out.
        self.assertIn(7, self._skipped())

    async def test_a_refused_reskip_never_undoes_another_crews_mark(self):
        from kiro_crew.apps.builtins.issue_radar.backend import crew_answer_store as answers
        from kiro_crew.apps.builtins.issue_radar.backend import crew_store_ledger as ledger

        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)])
        self.now[0] = _T0_MS + 120_000
        await self._sweep(client)
        at = off_loop(cs.read_answers, OWNER, REPO, self.root)["7"]["at"]
        real = answers.supersede

        def another_crew_marked_first(*args, **kwargs):
            # A second crew's re-skip landed and marked the answer just before ours.
            real(OWNER, REPO, self.root, 7, at, {"crew_id": "other", "scope": "architecture"})
            return real(*args, **kwargs)

        refused = cs.CrewLedgerNotRecorded("nothing changed")
        with (
            mock.patch.object(answers, "supersede", side_effect=another_crew_marked_first),
            mock.patch.object(ledger, "_append", side_effect=refused),
        ):
            with self.assertRaises(cs.CrewLedgerNotRecorded):
                self._skip(7, scope="needs-investigation")
        held = off_loop(cs.read_answers, OWNER, REPO, self.root)["7"]
        self.assertEqual(held["pass"]["crew_id"], "other")  # its mark is kept
        self.assertIn(7, self._skipped())
        # Alone, a refused re-skip undoes its own mark: the answer still stands.
        answers.unsupersede(OWNER, REPO, self.root, 7, at)
        with mock.patch.object(ledger, "_append", side_effect=refused):
            with self.assertRaises(cs.CrewLedgerNotRecorded):
                self._skip(7, scope="needs-investigation")
        self.assertNotIn(7, self._skipped())

    async def test_an_unknown_forge_identity_fires_nothing(self):
        self._skip(7)
        self._issue(7, labels=[])
        client = _AnswerClient([_unlabeled("alice", _T0_MS + 60_000)], login="")
        self.now[0] = _T0_MS + 120_000

        woken, _ = await self._sweep(client)

        self.assertEqual(woken, {})
        self.assertIn(7, self._skipped())
        self.assertNotIn("label-events:7", client.calls)

    def test_forge_times_normalize_to_the_folds_spelling(self):
        self.assertEqual(
            cs.normalize_forge_time("2026-10-16T10:00:00Z"), "2026-10-16T10:00:00.000000Z"
        )
        self.assertEqual(
            cs.normalize_forge_time("2026-10-16T12:00:00.123+02:00"),
            "2026-10-16T10:00:00.123000Z",
        )
        self.assertEqual(cs.normalize_forge_time("yesterday"), "")
