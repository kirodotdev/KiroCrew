"""The watcher's pickup-due wake, and the day-long idle backstop it makes possible.

A crew with a free slot and something it could claim is woken by the sweep, without a
model, when that grows: a claimable issue it had not seen, or more free slots. A shrink
(its own claim, a candidate leaving) costs no turn, and neither does anything else about
an idle crew: the auto-nudge idle clock defaults to a day. The shared
fakes and autouse fixtures live in ``issue_radar_crew_runtime_helpers.py``.
"""

import tempfile
import unittest
from pathlib import Path
from typing import Any
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

_KEY = provider.key_from_parts(OWNER, REPO)
_OLD = "2020-01-01T00:00:00Z"
_PICKUP = "pickup-due"


class _PickupClient:
    """The forge as the sweep reads it: one open-issues page, and per-item reads for the
    crew's own claimed items. No login, so mention detection stays out of the way."""

    def __init__(self) -> None:
        self.open: list[dict[str, Any]] = []
        self.fail_open_read = False
        self.comments: dict[int, int] = {}

    def add_issue(self, number: int, labels=(), updated_at: str = _OLD) -> None:
        self.open = [i for i in self.open if i["number"] != number]
        self.open.append({"number": number, "labels": list(labels), "updated_at": updated_at})

    def list_open_issues_first_page(self, owner, repo, **kw):
        if self.fail_open_read:
            raise RuntimeError("forge unavailable")
        return [dict(i) for i in self.open]

    def get_current_login(self, **kw):
        return None

    def get_issue_detail(self, owner, repo, number, **kw):
        return {"comments": self.comments.get(number, 0), "state": "open"}

    def get_pr_detail(self, owner, repo, number, **kw):
        return {}

    def list_issue_timeline(self, owner, repo, number, **kw):
        return []

    def enrich_pulls_by_number(self, owner, repo, pulls, **kw):
        return []


class TestPickupDue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.wake_result = True

    async def _sweep(self, client):
        wake = mock.AsyncMock(side_effect=lambda *a, **k: self.wake_result)
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=wake),
        ):
            woken = await cr.sweep_repo(_app(_FakeState()), _KEY, self.root)
        return woken, wake

    @staticmethod
    def _reasons(wake) -> list[str]:
        return [call.args[4] for call in wake.await_args_list]

    def _pickup_wakes(self, wake) -> list[str]:
        return [r for r in self._reasons(wake) if _PICKUP in r]

    def _resolve(self, crew_id: str, number: int) -> None:
        off_loop(
            cs.commit_work_progress,
            OWNER,
            REPO,
            crew_id,
            number,
            {"phase": "resolved"},
            "merge",
            "merged",
            root=self.root,
            session_id=_unit_for(crew_id),
        )

    def _claim(self, client: _PickupClient, crew_id: str, number: int) -> None:
        """A claimed item parked on CI: it holds a slot, and no turn has to advance it."""
        _item(self.root, crew_id, number, phase="awaiting-ci")
        client.add_issue(number, ["bug", cr.CLAIM_LABEL])

    async def test_a_crew_with_room_and_claimable_work_is_woken_once(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        client.add_issue(5, ["bug"])
        client.add_issue(6, ["Bug", "ui"])
        client.add_issue(7, ["docs"])  # out of scope

        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {crew["id"]: [_PICKUP]})
        self.assertEqual(self._reasons(wake), ["pickup-due (2 claimable, 3 of 3 slots free)"])

        # Same candidates, same room: nothing for a turn to decide differently.
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {})
        wake.assert_not_awaited()

    async def test_a_freed_slot_wakes_the_crew_for_candidates_it_already_saw(self):
        crew = _crew(self.root, labels=["bug"], max_open=2)
        client = _PickupClient()
        self._claim(client, crew["id"], 11)
        self._claim(client, crew["id"], 12)
        client.add_issue(5, ["bug"])
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()  # full: #5 is seen, but there is no room for it

        self._resolve(crew["id"], 12)
        client.open = [i for i in client.open if i["number"] != 12]  # merged, so closed
        woken, wake = await self._sweep(client)

        self.assertEqual(woken, {crew["id"]: [_PICKUP]})
        self.assertEqual(self._reasons(wake), ["pickup-due (1 claimable, 1 of 2 slots free)"])

    async def test_room_lost_and_regained_with_the_same_candidates_wakes_again(self):
        crew = _crew(self.root, labels=["bug"], max_open=2)
        client = _PickupClient()
        self._claim(client, crew["id"], 11)
        client.add_issue(5, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(len(self._pickup_wakes(wake)), 1)

        self._claim(client, crew["id"], 12)  # full now
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), [])

        self._resolve(crew["id"], 12)
        client.open = [i for i in client.open if i["number"] != 12]
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 1 of 2 slots free)"])

    async def test_a_crew_at_max_open_never_gets_a_pickup_wake(self):
        crew = _crew(self.root, labels=["bug"], max_open=1)
        client = _PickupClient()
        self._claim(client, crew["id"], 11)
        client.add_issue(5, ["bug"])
        for number in (6, 7, 8):
            _, wake = await self._sweep(client)
            self.assertEqual(self._pickup_wakes(wake), [])
            client.add_issue(number, ["bug"])
        # The scope signal still says what arrived; only the pickup wake waits for room.
        _, wake = await self._sweep(client)
        self.assertEqual(self._reasons(wake), ["#8 scope-label"])

    async def test_a_wake_that_started_no_turn_is_tried_again_next_sweep(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        client.add_issue(5, ["bug"])

        self.wake_result = False  # mid-turn: the wake was dropped
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 3 of 3 slots free)"])

        self.wake_result = True
        woken, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 3 of 3 slots free)"])
        self.assertIn(crew["id"], woken)

        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_a_claim_with_room_left_brings_the_next_pick_and_no_more(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        client.add_issue(5, ["bug"])
        client.add_issue(6, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(len(self._pickup_wakes(wake)), 1)

        # The turn claimed #5 and the item parked: a turn takes one issue, and there
        # is room and #6 left, so the next pick is due.
        self._claim(client, crew["id"], 5)
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 2 of 3 slots free)"])

        # That turn looked at #6 and took nothing: the same choice wakes nobody again.
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        stored = cr.read_wake_checks(OWNER, REPO, self.root)["pickup"][crew["id"]]
        self.assertEqual(stored, {"numbers": [6], "free": 2, "held": [5]})

    async def test_a_claim_that_offsets_a_closed_item_still_brings_the_next_pick(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        self._claim(client, crew["id"], 11)
        self._claim(client, crew["id"], 12)
        client.add_issue(5, ["bug"])
        client.add_issue(6, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (2 claimable, 1 of 3 slots free)"])
        _, wake = await self._sweep(client)  # the turn took nothing
        wake.assert_not_awaited()

        # #11 merged and the crew claimed #5 before the next look: one slot still free.
        self._resolve(crew["id"], 11)
        client.open = [i for i in client.open if i["number"] != 11]
        self._claim(client, crew["id"], 5)
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 1 of 3 slots free)"])

    async def test_candidates_dropping_off_the_page_do_not_wake(self):
        _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        for number in (5, 6, 7):
            client.add_issue(number, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(len(self._pickup_wakes(wake)), 1)

        client.open = [i for i in client.open if i["number"] != 7]
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        client.open = [i for i in client.open if i["number"] != 6]
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_one_more_free_slot_with_the_same_candidates_wakes(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        self._claim(client, crew["id"], 11)
        self._claim(client, crew["id"], 12)
        client.add_issue(5, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 1 of 3 slots free)"])

        self._resolve(crew["id"], 12)
        client.open = [i for i in client.open if i["number"] != 12]
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 2 of 3 slots free)"])

    async def test_a_candidate_that_leaves_and_returns_wakes(self):
        _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        client.add_issue(5, ["bug"])
        client.add_issue(6, ["bug"])
        await self._sweep(client)

        client.open = [i for i in client.open if i["number"] != 6]
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

        client.add_issue(6, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (2 claimable, 3 of 3 slots free)"])

    async def test_a_closed_candidate_on_a_shorter_page_is_not_growth(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        for number in (5, 6, 7):
            client.add_issue(number, ["bug"])
        await self._sweep(client)
        self.assertEqual(
            cr.read_wake_checks(OWNER, REPO, self.root)["pickup"][crew["id"]],
            {"numbers": [5, 6, 7], "free": 3, "held": []},
        )
        client.open = [i for i in client.open if i["number"] != 5]  # closed: page is 2 rows
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_a_new_claimable_issue_wakes(self):
        _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        client.add_issue(5, ["bug"])
        await self._sweep(client)
        client.add_issue(6, ["bug"])
        _, wake = await self._sweep(client)
        # The scope signal names #6; the pickup reason is folded into that one wake.
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()

    async def test_a_failed_forge_read_wakes_nobody_and_writes_nothing(self):
        _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        client.add_issue(5, ["bug"])
        client.fail_open_read = True
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        self.assertFalse(cr.wake_checks_path(OWNER, REPO, self.root).exists())

        client.fail_open_read = False
        await self._sweep(client)
        path = cr.wake_checks_path(OWNER, REPO, self.root)
        before = path.read_bytes()
        client.add_issue(6, ["bug"])  # a change the failed read must not consume
        client.fail_open_read = True
        _, wake = await self._sweep(client)
        wake.assert_not_awaited()
        self.assertEqual(path.read_bytes(), before)

        client.fail_open_read = False
        _, wake = await self._sweep(client)
        # #6 is still news: one wake, carrying the scope signal that names it.
        self.assertEqual(self._reasons(wake), ["#6 scope-label"])

    async def test_a_quiet_day_costs_no_turn(self):
        _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        client.add_issue(5, ["bug"])
        client.add_issue(9, ["docs"])
        _, wake = await self._sweep(client)
        self.assertEqual(len(self._pickup_wakes(wake)), 1)

        # Polls with activity that changes nothing claimable: a comment bumps the update
        # time, an out-of-scope issue opens. None of it is worth a turn.
        turns = 0
        for poll in range(30):
            client.add_issue(5, ["bug"], updated_at=f"2020-01-02T00:{poll:02d}:00Z")
            client.add_issue(100 + poll, ["docs"])
            _, wake = await self._sweep(client)
            turns += wake.await_count
        self.assertEqual(turns, 0)

    async def test_an_empty_scope_counts_every_unclaimed_open_issue(self):
        crew = _crew(self.root, max_open=3)
        client = _PickupClient()
        client.add_issue(5, [])
        client.add_issue(6, ["docs"])
        client.add_issue(7, [cr.CLAIM_LABEL])
        client.add_issue(8, ["crew: needs human"])
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {crew["id"]: [_PICKUP]})
        self.assertEqual(self._reasons(wake), ["pickup-due (2 claimable, 3 of 3 slots free)"])

    async def test_a_crew_with_an_acting_item_is_not_woken_to_pick_up_until_it_parks(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        _item(self.root, crew["id"], 11, phase="implementing")
        client.add_issue(11, ["bug", cr.CLAIM_LABEL])
        client.add_issue(5, ["bug"])
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), [])

        _item(self.root, crew["id"], 11, phase="awaiting-ci")
        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 2 of 3 slots free)"])

    async def test_a_turn_woken_by_an_item_signal_leaves_the_pickup_for_the_next_sweep(self):
        crew = _crew(self.root, labels=["bug"], max_open=2)
        client = _PickupClient()
        self._claim(client, crew["id"], 11)
        _item(self.root, crew["id"], 11, phase="awaiting-reply")
        self._claim(client, crew["id"], 12)
        client.add_issue(5, ["bug"])
        _, wake = await self._sweep(client)  # full: seeds the item marks, no pickup
        self.assertEqual(self._pickup_wakes(wake), [])

        self._resolve(crew["id"], 12)
        client.open = [i for i in client.open if i["number"] != 12]
        client.comments[11] = 1  # the requester replied on #11 in the same sweep
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew['id']}:11"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)
        _, wake = await self._sweep(client)
        # One turn, for the reply: that turn advances #11 before it would select.
        self.assertEqual(self._reasons(wake), ["#11 requester-replied"])

        _, wake = await self._sweep(client)
        self.assertEqual(self._pickup_wakes(wake), ["pickup-due (1 claimable, 1 of 2 slots free)"])

    async def test_an_issue_the_crew_resolved_and_someone_reopened_is_a_candidate(self):
        crew = _crew(self.root, labels=["bug"], max_open=3)
        client = _PickupClient()
        _item(self.root, crew["id"], 5, phase="awaiting-merge")
        self._resolve(crew["id"], 5)
        client.add_issue(5, ["bug"])  # reopened
        woken, wake = await self._sweep(client)
        self.assertEqual(woken, {crew["id"]: [_PICKUP]})
        self.assertEqual(self._reasons(wake), ["pickup-due (1 claimable, 3 of 3 slots free)"])


class TestDroppedWakeKeepsItsSignal(unittest.IsolatedAsyncioTestCase):
    """An item signal whose wake started no turn is raised again on the next sweep:
    the idle backstop is a day away, so it cannot be what picks the change up."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def _sweep(self, client, started: bool):
        wake = mock.AsyncMock(return_value=started)
        with (
            mock.patch.object(provider, "client_for", return_value=client),
            mock.patch.object(cr.provider, "client_for", return_value=client),
            mock.patch.object(cr, "wake_crew", new=wake),
        ):
            woken = await cr.sweep_repo(_app(_FakeState()), _KEY, self.root)
        return woken, wake

    def _make_due(self, crew_id: str) -> None:
        stored = cr.read_signals(OWNER, REPO, self.root)
        stored[f"{crew_id}:2201"]["checked_at"] = 0
        cr.write_signals(OWNER, REPO, stored, self.root)

    async def test_a_reply_seen_while_the_crew_is_mid_turn_wakes_it_next_sweep(self):
        crew = _crew(self.root, unattended=True)
        _item(self.root, crew["id"], 2201, phase="awaiting-reply")
        client = _FakeClient(issue={"comments": 2, "state": "open"})
        await self._sweep(client, started=True)
        self._make_due(crew["id"])

        client.issue = {"comments": 3, "state": "open"}
        _, wake = await self._sweep(client, started=False)  # mid-turn: dropped
        self.assertIn("requester-replied", wake.await_args.args[4])

        self._make_due(crew["id"])
        woken, wake = await self._sweep(client, started=True)
        self.assertEqual(woken, {crew["id"]: [cr.SIG_REPLY]})
        self.assertIn("requester-replied", wake.await_args.args[4])

        self._make_due(crew["id"])
        woken, wake = await self._sweep(client, started=True)
        self.assertEqual(woken, {})


class TestIdleBackstop(unittest.IsolatedAsyncioTestCase):
    """The loop's interval: the active gap while an item is acting, the backstop
    otherwise. An interval that is neither was set by an operator and is kept."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.crew = _crew(self.root, unattended=True)

    async def _armed(self) -> list[int]:
        svc = _FakeNudge()
        armed: list[int] = []
        plain_add = svc.add

        async def add(slot_key="", message="", **kw):
            armed.append(kw.get("idle_secs"))
            return await plain_add(slot_key, message, **kw)

        svc.add = add  # type: ignore[method-assign]
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.launch_crew(_FakeState(), OWNER, REPO, self.crew, self.root)
        return armed

    async def _watchdog(self, loop_secs: int) -> list[tuple[str, dict[str, Any]]]:
        """One watchdog pass over a loop currently at *loop_secs*; its idle updates."""
        state = _FakeState()
        await cr.ensure_crew_session(state, OWNER, REPO, self.crew)
        loop = _FakeLoop("nl_0", f"crew-{self.crew['id']}")
        loop.idle_secs = loop_secs  # type: ignore[attr-defined]
        svc = _FakeNudge([loop])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [self.crew], self.root)
        return [
            (lid, {k: v for k, v in patch.items() if k != "precondition"})
            for lid, patch in svc.updates
            if "idle_secs" in patch
        ]

    def _resolve(self, number: int) -> None:
        off_loop(
            cs.commit_work_progress,
            OWNER,
            REPO,
            self.crew["id"],
            number,
            {"phase": "resolved"},
            "merge",
            "merged",
            root=self.root,
            session_id=_unit_for(self.crew["id"]),
        )

    async def test_launch_arms_a_day_long_idle_backstop_by_default(self):
        self.assertEqual(await self._armed(), [86400])

    async def test_a_crew_relaunched_mid_item_is_armed_at_the_active_gap(self):
        _item(self.root, self.crew["id"], 7, phase="implementing")
        self.assertEqual(await self._armed(), [300])
        self.assertEqual(cr.ACTIVE_IDLE_SECS, 300)

    async def test_a_crew_only_waiting_on_the_forge_gets_the_backstop(self):
        _item(self.root, self.crew["id"], 7, phase="awaiting-ci")
        _item(self.root, self.crew["id"], 8, phase="awaiting-reply")
        self.assertEqual(await self._armed(), [86400])

    async def test_the_watchdog_retimes_between_acting_and_idle(self):
        _item(self.root, self.crew["id"], 7, phase="claimed")
        self.assertEqual(await self._watchdog(86400), [("nl_0", {"idle_secs": 300})])
        self._resolve(7)
        self.assertEqual(await self._watchdog(300), [("nl_0", {"idle_secs": 86400})])

    async def test_an_unchanged_interval_writes_nothing(self):
        self.assertEqual(await self._watchdog(86400), [])
        _item(self.root, self.crew["id"], 7, phase="investigating")
        self.assertEqual(await self._watchdog(300), [])

    async def test_an_interval_an_operator_set_is_kept(self):
        self.assertEqual(await self._watchdog(900), [])
        _item(self.root, self.crew["id"], 7, phase="implementing")
        self.assertEqual(await self._watchdog(60), [])

    async def test_the_retime_holds_only_while_the_loop_keeps_the_interval_it_read(self):
        _item(self.root, self.crew["id"], 7, phase="implementing")
        state = _FakeState()
        await cr.ensure_crew_session(state, OWNER, REPO, self.crew)
        loop = _FakeLoop("nl_0", f"crew-{self.crew['id']}")
        loop.idle_secs = 86400  # type: ignore[attr-defined]
        svc = _FakeNudge([loop])
        with mock.patch.object(cr, "_autonudge_instance", lambda: svc):
            await cr.watchdog_cycle(state, OWNER, REPO, [self.crew], self.root)
        (patch,) = [p for _lid, p in svc.updates if "idle_secs" in p]
        self.assertEqual(patch["idle_secs"], 300)
        # The service re-takes this under its lock: an interval an operator set after
        # the watchdog read the loop refuses the write.
        self.assertTrue(patch["precondition"](loop))
        loop.idle_secs = 900  # type: ignore[attr-defined]
        self.assertFalse(patch["precondition"](loop))
