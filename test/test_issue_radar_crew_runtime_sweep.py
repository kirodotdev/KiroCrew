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
    _FakeLoop,
    _FakeNudge,
    _FakeSlot,
    _FakeState,
    _isolated_crew_log,
    _item,
    _private_sel_root_per_test,
)
from off_loop_helpers import off_loop

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.apps.builtins.issue_radar.backend import provider
from kiro_crew.apps.builtins.issue_radar.backend import watch as watch_mod

_KEY = provider.key_from_parts(OWNER, REPO)


# ── turn dispatch runs under the background-turn cap ────────────────────────


class TestTurnDispatch(unittest.IsolatedAsyncioTestCase):
    """Every crew turn must be charged against the background-turn cap.

    The cap lives INSIDE ``DashboardState.run_background_turn``, so a dispatch that
    hands ``_run_chat`` straight to ``enqueue_or_run_prompt`` is not merely
    uncounted — it is uncapped. Crews are the one fleet in the product that arms N
    independent loops, so simultaneous wakes plus a human's guidance injection could
    put more turns on the runtime than the cap allows while its counters reported a
    smaller number, which reads as a healthy fleet.

    Both dispatch sites go through :func:`crew_runtime.dispatch_crew_turn`; the wake
    is pinned here and the guidance route in the route tests.
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
        """The review finding: a model picked in the crew's session is the person's.

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


class _FakeClient:
    """The gh layer, stubbed. Counts calls so the sweep's API cost is assertable."""

    def __init__(self, issue=None, pr=None, timeline=None, enriched=None):
        self.issue = issue or {"comments": 0, "state": "open"}
        self.pr = pr or {}
        self.timeline = timeline or []
        self.enriched = enriched or []
        self.calls: list[str] = []

    def get_issue_detail(self, owner, repo, number, **kw):
        self.calls.append(f"issue:{number}")
        return dict(self.issue)

    def get_pr_detail(self, owner, repo, number, **kw):
        self.calls.append(f"pr:{number}")
        return dict(self.pr)

    def list_issue_timeline(self, owner, repo, number, **kw):
        self.calls.append(f"timeline:{number}")
        return list(self.timeline)

    def enrich_pulls_by_number(self, owner, repo, pulls, **kw):
        self.calls.append("enrich")
        return list(self.enriched)


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

    The bug this pins: the watchdog re-establishes a live crew's missing nudge loop
    (correct after a restart, which drops the in-memory registry) and so undid the
    close handler's deliberate loop removal on the very next sweep, resurrecting a
    tab the user had just dismissed.

    Both directions are asserted here on purpose. Gating the re-arm on any signal
    idle archival also writes (``closed``/``closed_at`` — both paths stamp both)
    would trade a visible resurrection for a silent death: a crew that was merely
    quiet would never be re-armed and would sit enabled and stopped with nothing
    explaining why.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _teardown_mod(self):
        """Imported per-test, never in ``setUp``.

        A ``setUp`` that touches the hook registry makes EVERY test in the class
        fail with an ``AttributeError`` the moment the seam is missing — including
        ``test_a_crew_with_no_loop_and_no_dismissal_IS_re_armed``, whose whole job
        is to stay green when the fix is removed. A shared fixture that couples an
        independent assertion to the change under test destroys the only signal
        that assertion carries.
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

        ``_watch_loop`` sleeps ``POLL_INTERVAL_SEC`` before its first sweep, so a
        registration that only happens inside that sweep leaves the ✕ ignored for
        the first minute of every process — and the sweep that finally arrives is
        the thing that resurrects the crew the user just closed.

        ``_poll_once`` is stubbed to abort the loop, so the sweep (and therefore
        the watchdog's own idempotent registration) never runs. The hook can only
        be present here if the loop registered it on entry.
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

        Until the disable hook is registered, switching the app off only writes a
        flag: the crews keep their auto-approve grants and their armed loops until
        this loop next wakes, which is the whole window the hook exists to close. So
        it cannot wait for the first sweep either, and the state it captures must be
        the gateway's — a hook holding nothing revokes nothing.
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
        """The registry holds ONE hook per app, and the watchdog registers it per
        swept repo with that repo's PROVIDER root — so the last sweep's provider won.
        A mixed GitHub/GitLab install keeps each provider's records under its own
        root, so closing the tab of a crew on the losing provider found nothing: the
        crew stayed live and the next sweep re-armed its auto-approved session.

        Here the hook is registered scoped to a root that does NOT hold the crew, and
        the crew must still be found through the provider fallback.
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
        await teardown.notify_slot_closed(cr.APP_NAME, "crew-c_1")

    async def test_an_unknown_slot_key_is_ignored(self):
        with self._repos():
            await cr._on_slot_closed("crew-c_deadbeef", root=self.root)

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
