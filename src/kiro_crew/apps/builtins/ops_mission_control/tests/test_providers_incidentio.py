"""Tests for the incident.io adapter.

The properties worth pinning here are the ones an API shape can quietly violate: a
rotation source must not read a colleague's shift as this instance's own, a poll that
saw only the first page must not read as a complete estate, and a sink must not offer a
verb its provider cannot perform.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_ACK,
    ACTION_COMMENT,
    ACTION_RESOLVE,
    ACTION_SILENCE,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import incidentio
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    DEFAULT_POLL_LIMIT,
    TruncatedSignals,
)


class _IncidentIoCase(unittest.IsolatedAsyncioTestCase):
    """Redirects ``KIROCREW_HOME`` so config and keystone reads see a fresh install.

    Without it these tests read the operator's live data home, so enabling the provider
    in the real dashboard would change what they assert.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        # Cleanup registered at creation, not in a separate tearDown: the teardown runs
        # even on failure either way, but registering here means the directory cannot be
        # orphaned by an early return added to this method later.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev = os.environ.get("KIROCREW_HOME")
        self.addCleanup(self._restore_home)
        os.environ["KIROCREW_HOME"] = self.tmp

    def _restore_home(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev

    @staticmethod
    def _adapter():
        return incidentio, incidentio.IncidentIoAdapter()


class TestIncidentIoCannotBorrowATeammatesShift(_IncidentIoCase):
    """An identity this instance cannot prove is an identity it must not assume."""

    def test_a_blank_identity_abstains_instead_of_claiming_the_shift(self) -> None:
        incidentio, adapter = self._adapter()
        called: list[dict[str, Any]] = []

        def _fake_request(url, headers=None, params=None):
            called.append(dict(params or {}))
            return {
                "schedule_entries": {
                    "final": [{"user": {"id": "USOMEONEELSE", "name": "alice"}, "end_at": ""}]
                }
            }

        with mock.patch.object(incidentio, "config_list", return_value=["SCHED1"]):
            with mock.patch.object(incidentio, "request_json", side_effect=_fake_request):
                status = adapter._on_shift_sync()

        self.assertTrue(status.unknown, "a source with no identity must abstain")
        self.assertEqual(called, [], "it must not even ask: there is nothing to match against")

    def test_clearing_schedule_ids_cannot_manufacture_an_abstention(self) -> None:
        """An agent-writable field must not be able to switch this source off.

        ``schedule_ids`` lives in ``config_fields`` and is therefore agent-writable, while
        the user id is operator-only on the keystone. Treating an empty schedule list as
        ``unknown`` regardless would let the constrained party produce the non-vote the
        tier gate honours, and the off-shift refusal would stop firing without anything
        about who is on call having changed.
        """
        incidentio, adapter = self._adapter()
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")

        with mock.patch.object(incidentio, "config_list", return_value=[]):
            status = adapter._on_shift_sync()

        self.assertFalse(status.unknown, "an operator-configured rotation must not abstain")
        self.assertFalse(status.on_shift, "an empty agent-writable list is a vote, not a shrug")

    def test_a_genuinely_unconfigured_provider_still_abstains(self) -> None:
        """No identity and no schedules is a solo install, not an off-shift verdict."""
        incidentio, adapter = self._adapter()

        with mock.patch.object(incidentio, "config_list", return_value=[]):
            status = adapter._on_shift_sync()

        self.assertTrue(status.unknown)
        self.assertTrue(status.on_shift, "an unconfigured rotation must fail open")

    def test_the_effective_schedule_decides_not_the_rotation_rules(self) -> None:
        """``final`` is read, never ``scheduled``.

        Overrides are how a swapped shift is expressed. Reading the pre-override list would
        report a covered shift as still ours, and our own override as not ours at all.
        """
        incidentio, adapter = self._adapter()
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")

        def _fake_request(url, headers=None, params=None):
            return {
                "schedule_entries": {
                    "scheduled": [{"user": {"id": "UME", "name": "me"}, "end_at": "later"}],
                    "final": [{"user": {"id": "UCOVER", "name": "bob"}, "end_at": "later"}],
                }
            }

        with mock.patch.object(incidentio, "config_list", return_value=["SCHED1"]):
            with mock.patch.object(incidentio, "request_json", side_effect=_fake_request):
                status = adapter._on_shift_sync()

        self.assertFalse(
            status.on_shift,
            "an override handed this shift to someone else; the pre-override list must not win",
        )


class TestIncidentIoAuthorizesOnlyTheShiftInForce(_IncidentIoCase):
    """A shift that has not started yet is not authority to act.

    ``entry_window_start/end`` selects entries OVERLAPPING the window, so the endpoint
    also returns the next shift when it begins within ``_SHIFT_WINDOW``. Reading that as
    on_shift authorized writes while the outgoing engineer still held the page. These
    pin containment of the current instant, so the window's width stops deciding who is
    judged on call.
    """

    @staticmethod
    def _entry(start_delta: int, end_delta: int, user_id: str = "UME") -> dict[str, Any]:
        now = datetime.now(timezone.utc)

        def _fmt(seconds: int) -> str:
            return (now + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "user": {"id": user_id, "name": "me"},
            "start_at": _fmt(start_delta),
            "end_at": _fmt(end_delta),
        }

    def _status_for(self, entry: dict[str, Any]):
        incidentio, adapter = self._adapter()
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")

        def _fake_request(url, headers=None, params=None):
            return {"schedule_entries": {"final": [entry]}}

        with mock.patch.object(incidentio, "config_list", return_value=["SCHED1"]):
            with mock.patch.object(incidentio, "request_json", side_effect=_fake_request):
                return adapter._on_shift_sync()

    def test_a_shift_starting_shortly_does_not_authorize_yet(self) -> None:
        status = self._status_for(self._entry(start_delta=30, end_delta=28800))
        self.assertFalse(
            status.on_shift,
            "a shift beginning in 30s must not authorize a write before handoff",
        )

    def test_the_shift_in_force_still_authorizes(self) -> None:
        status = self._status_for(self._entry(start_delta=-3600, end_delta=25200))
        self.assertTrue(status.on_shift, "the shift containing now must still be honoured")
        self.assertEqual(status.who, "me")

    def test_an_entry_with_no_start_bound_fails_closed(self) -> None:
        entry = self._entry(start_delta=-3600, end_delta=25200)
        del entry["start_at"]
        status = self._status_for(entry)
        self.assertFalse(
            status.on_shift,
            "an entry whose window cannot be evaluated must not grant authority",
        )

    def test_an_unparseable_bound_fails_closed(self) -> None:
        entry = self._entry(start_delta=-3600, end_delta=25200)
        entry["end_at"] = "whenever"
        status = self._status_for(entry)
        self.assertFalse(status.on_shift)


class TestIncidentIoOffersNoVerbItCannotPerform(_IncidentIoCase):
    """The API has no acknowledge and no snooze, so neither may be advertised.

    Advertising one would pass the autonomy gate and fail at execute time, after the
    board had already recorded the action as granted.
    """

    def test_only_resolve_and_comment_are_supported(self) -> None:
        _, adapter = self._adapter()
        self.assertEqual(adapter.supported_actions(), frozenset({ACTION_RESOLVE, ACTION_COMMENT}))

    async def test_an_unsupported_verb_is_refused_without_calling_the_provider(self) -> None:
        incidentio, adapter = self._adapter()
        signal = Signal.create(
            source="incidentio",
            native_id="alert/A1",
            title="disk full",
            labels={"incidentio_alert_id": "A1"},
        )

        with mock.patch.object(incidentio, "request_json") as sent:
            with mock.patch.object(adapter, "configured", return_value=True):
                for verb in (ACTION_ACK, ACTION_SILENCE):
                    with self.subTest(action=verb):
                        result = await adapter.execute(signal, verb, {})
                        self.assertFalse(result.ok)
        sent.assert_not_called()


class TestIncidentIoPollTruncationIsNotRecovery(_IncidentIoCase):
    """A page is not an estate, and a cursor is not truncation.

    The alerts endpoint caps ``page_size`` at 50, well under the registry's own signal
    cap, so a next-page cursor means "keep walking" rather than "give up". Truncation is
    reported only when the estate exceeds what a poll may carry, because absence from a
    non-truncated poll is read as recovery.
    """

    @staticmethod
    def _alert(alert_id: str, dedup: str = "", source: str = "SRC1") -> dict[str, Any]:
        return {
            "id": alert_id,
            "title": f"alert {alert_id}",
            "status": "firing",
            "deduplication_key": dedup,
            "alert_source_id": source,
            "created_at": "2026-08-17T10:00:00Z",
        }

    @staticmethod
    def _page(alerts: list[dict[str, Any]], after: str = "") -> dict[str, Any]:
        meta: dict[str, Any] = {"page_size": 50}
        if after:
            meta["after"] = after
        return {"alerts": alerts, "pagination_meta": meta}

    def test_a_cursor_is_followed_rather_than_reported_as_truncation(self) -> None:
        """The regression guard for a permanently non-authoritative poll.

        Without cursor-following, an estate just over one page reports truncated on
        every cycle, which stops ``reconcile`` from ever resolving one of this
        source's signals.
        """
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert(f"A{i}") for i in range(50)], after="cursor-1"),
            self._page([self._alert("B1")]),
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages) as sent:
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(sent.call_count, 2, "the second page must actually be fetched")
        self.assertNotIsInstance(signals, TruncatedSignals)
        self.assertEqual(len(signals), 51)
        self.assertIn("after", sent.call_args_list[1].kwargs["params"])

    def test_an_estate_over_the_poll_cap_is_reported_as_truncated(self) -> None:
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert(f"P{page}-{i}") for i in range(50)], after=f"cursor-{page}")
            for page in range(6)
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages):
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertIsInstance(signals, TruncatedSignals)
        self.assertLessEqual(len(signals), DEFAULT_POLL_LIMIT)

    def test_an_empty_page_carrying_a_cursor_is_truncation_not_completion(self) -> None:
        """The provider says more exists while handing back nothing.

        The walk must stop (an empty page cannot advance it) but must NOT claim the estate
        was captured whole, or a still-firing omitted alert reads as recovered.
        """
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert("A1")], after="cursor-1"),
            self._page([], after="cursor-2"),
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages) as sent:
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(sent.call_count, 2, "the walk must not spin on an empty page")
        self.assertEqual(len(signals), 1)
        self.assertIsInstance(signals, TruncatedSignals)

    def test_an_empty_terminal_page_without_a_cursor_stays_authoritative(self) -> None:
        """An exhausted estate is complete, so absence from it may be read as recovery."""
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert("A1")], after="cursor-1"),
            self._page([]),
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages):
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertNotIsInstance(signals, TruncatedSignals)
        self.assertEqual(len(signals), 1)

    def test_a_terminal_page_that_overshoots_the_cap_reports_truncated(self) -> None:
        """The regression guard for the drop-and-call-it-complete defect.

        A last page (no cursor) can still carry the accumulator past the cap. Ending the
        walk on `not cursor` before checking the cap left the verdict at False while the
        slice discarded the overshoot, so `reconcile` read still-firing alerts as
        recovered. The verdict is now derived from the slice, so the two cannot disagree.
        """
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert(f"P0-{i}") for i in range(50)], after="cursor-1"),
            self._page([self._alert(f"P1-{i}") for i in range(50)], after="cursor-2"),
            self._page([self._alert(f"P2-{i}") for i in range(10)]),  # terminal, no cursor
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages):
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(len(signals), DEFAULT_POLL_LIMIT, "the slice bounds the poll")
        self.assertIsInstance(
            signals, TruncatedSignals, "dropping alerts must never read as a complete estate"
        )

    def test_an_estate_exactly_at_the_cap_is_not_reported_as_truncated(self) -> None:
        """Captured whole is authoritative, even at exactly the cap.

        `base.py`'s invariant: requesting exactly the cap makes "full" and "capped"
        indistinguishable, so the walk must see past it before claiming truncation.
        Breaking at `>=` wrapped a complete estate as truncated and reintroduced the
        permanently non-authoritative poll this walk exists to remove.
        """
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert(f"P0-{i}") for i in range(50)], after="cursor-1"),
            self._page([self._alert(f"P1-{i}") for i in range(50)], after="cursor-2"),
            self._page([]),
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages):
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(len(signals), DEFAULT_POLL_LIMIT)
        self.assertNotIsInstance(signals, TruncatedSignals)

    def test_a_single_complete_page_is_an_authoritative_snapshot(self) -> None:
        incidentio, adapter = self._adapter()

        with mock.patch.object(
            incidentio, "request_json", return_value=self._page([self._alert("A1")])
        ):
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertNotIsInstance(signals, TruncatedSignals)
        self.assertEqual(len(signals), 1)

    def test_a_cursor_that_never_terminates_is_bounded_by_the_page_ceiling(self) -> None:
        """A misbehaving API cannot spin the walk, and cannot under-report silently.

        A page whose items are all non-dict never grows ``alerts``, so neither the
        poll cap nor the empty-page break can end the walk while the provider keeps
        handing back a cursor. The ceiling refuses loudly instead of reporting a
        partial estate as a complete snapshot.
        """
        incidentio, adapter = self._adapter()
        junk_pages = [
            {"alerts": ["not-a-dict"], "pagination_meta": {"after": f"cursor-{i}"}}
            for i in range(incidentio._MAX_ALERT_PAGES + 5)
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=junk_pages) as sent:
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    with self.assertRaises(RuntimeError):
                        adapter._poll_sync()

        self.assertEqual(sent.call_count, incidentio._MAX_ALERT_PAGES)

    def test_a_selected_alert_survives_a_storm_of_unselected_alerts(self) -> None:
        """The cap must be spent on matching alerts, never on filtered-out ones.

        With the slice applied before the source filter, 150 alerts from an unselected
        source ended the walk on the cap and the selected firing alert on the next page
        was never fetched — a silent drop that absence-as-recovery turns into a false
        resolve, at exactly the moment (an alert storm) the tool matters most.
        """
        incidentio, adapter = self._adapter()
        pages = [
            self._page(
                [self._alert(f"N{page}-{i}", source="NOISE") for i in range(50)],
                after=f"cursor-{page}",
            )
            for page in range(3)
        ]
        pages.append(self._page([self._alert("WANTED-1", source="SRC-WANTED")]))

        with mock.patch.object(incidentio, "request_json", side_effect=pages) as sent:
            with mock.patch.object(incidentio, "config_list", return_value=["SRC-WANTED"]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(sent.call_count, 4, "the walk must continue past unselected alerts")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].labels["incidentio_alert_id"], "WANTED-1")
        self.assertNotIsInstance(signals, TruncatedSignals)

    def test_the_truncation_verdict_is_derived_from_matching_alerts(self) -> None:
        """With a filter active, an over-cap matching estate still reports truncated."""
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert(f"P{page}-{i}") for i in range(50)], after=f"cursor-{page}")
            for page in range(6)
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages):
            with mock.patch.object(incidentio, "config_list", return_value=["SRC1"]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertIsInstance(signals, TruncatedSignals)
        self.assertEqual(len(signals), DEFAULT_POLL_LIMIT)

    def test_an_exhausted_estate_with_no_matching_alerts_is_authoritative(self) -> None:
        """A fully walked estate that matched nothing is a complete empty, not a partial.

        The walk saw every alert the provider holds and none came from a selected
        source, so absence from this poll IS recovery — reporting it truncated would
        recreate the permanently non-authoritative poll this walk exists to remove.
        """
        incidentio, adapter = self._adapter()
        pages = [
            self._page(
                [self._alert(f"N{page}-{i}", source="NOISE") for i in range(50)],
                after="cursor-1" if page == 0 else "",
            )
            for page in range(2)
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages):
            with mock.patch.object(incidentio, "config_list", return_value=["OTHER-SOURCE"]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(len(signals), 0, "no alert came from the configured source")
        self.assertNotIsInstance(signals, TruncatedSignals)

    def test_the_dedup_key_is_the_exact_match_key_when_present(self) -> None:
        """The dedup key identifies the recurring condition; the alert id is per firing."""
        incidentio, adapter = self._adapter()
        payload = {"alerts": [self._alert("A1", dedup="checkout-5xx")]}

        with mock.patch.object(incidentio, "request_json", return_value=payload):
            with mock.patch.object(incidentio, "config_list", return_value=[]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(signals[0].provider_key, "incidentio:alert/checkout-5xx")
        self.assertEqual(signals[0].labels["incidentio_alert_id"], "A1")


class TestIncidentIoRoster(_IncidentIoCase):
    """The operator's upcoming shifts, in the schedule-file roster's shape.

    Display only: nothing here may change the off-shift vote, so these tests pin what the
    board is shown and what the read costs, not who is authorized.
    """

    NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        super().setUp()
        incidentio.reset_roster_cache()
        self.addCleanup(incidentio.reset_roster_cache)
        for name, value in (
            ("provider_enabled", True),
            ("has_secrets", True),
            ("_headers", {}),
        ):
            patcher = mock.patch.object(incidentio, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.schedule_ids = ["SCHED1"]
        patcher = mock.patch.object(
            incidentio, "config_list", side_effect=lambda *_a: list(self.schedule_ids)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entry(self, uid: str, name: str, start_h: int, end_h: int) -> dict[str, Any]:
        return {
            "user": {"id": uid, "name": name},
            "start_at": incidentio._iso(self.NOW + timedelta(hours=start_h)),
            "end_at": incidentio._iso(self.NOW + timedelta(hours=end_h)),
        }

    def test_no_identity_means_no_roster_and_no_request(self) -> None:
        """Without a fenced identity there is no "you" to build the roster around."""
        with mock.patch.object(incidentio, "request_json") as request:
            self.assertEqual(incidentio.roster(self.NOW), {})
        request.assert_not_called()

    def test_an_identity_without_schedules_is_reported_not_hidden(self) -> None:
        """The state that votes off shift on every check must be visible on the board."""
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        self.schedule_ids = []
        with mock.patch.object(incidentio, "request_json") as request:
            result = incidentio.roster(self.NOW)
        request.assert_not_called()
        self.assertEqual(result["source"], "incidentio")
        self.assertEqual(result["members"], [])
        # A code the board translates, beside the English kept for logs.
        self.assertEqual(result["error_code"], incidentio.ROSTER_ERROR_NO_SCHEDULES)
        self.assertIn("schedule_ids", result["error"])

    def test_the_effective_schedule_builds_the_roster_in_shift_order(self) -> None:
        """``final`` is read, never ``scheduled``, and members keep first-shift order."""
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        final = [
            self._entry("UME", "Me", 30, 54),
            self._entry("UALICE", "Alice", -2, 6),
            self._entry("UME", "Me", 6, 30),
        ]

        def _fake_request(url, headers=None, params=None):
            return {
                "schedule_entries": {
                    "final": final,
                    "scheduled": [self._entry("UNOTREAL", "Pre-override", -2, 6)],
                },
                "pagination_meta": {},
            }

        with mock.patch.object(incidentio, "request_json", side_effect=_fake_request):
            result = incidentio.roster(self.NOW)

        self.assertEqual([m["login"] for m in result["members"]], ["UALICE", "UME"])
        self.assertEqual([m["name"] for m in result["members"]], ["Alice", "Me"])
        by_id = {m["login"]: m for m in result["members"]}
        self.assertTrue(by_id["UALICE"]["on_call_now"])
        self.assertFalse(by_id["UME"]["on_call_now"])
        self.assertEqual(by_id["UME"]["shifts"], 2)
        self.assertEqual(result["me"], "UME")
        self.assertTrue(result["me_on_roster"])
        self.assertEqual([w["current"] for w in result["windows"]], [True, False, False])
        self.assertEqual(result["error"], "")

    def test_not_on_any_shift_is_distinguished_from_off_shift_now(self) -> None:
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        entries = {"schedule_entries": {"final": [self._entry("UALICE", "Alice", -2, 6)]}}
        with mock.patch.object(incidentio, "request_json", return_value=entries):
            result = incidentio.roster(self.NOW)
        self.assertFalse(result["me_on_roster"])

    def test_the_cursor_goes_back_as_the_window_start_with_the_end_unchanged(self) -> None:
        """The endpoint's documented paging: a partial walk would hide a later shift."""
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        calls: list[dict[str, Any]] = []
        pages = [
            {
                "schedule_entries": {"final": [self._entry("UALICE", "Alice", -2, 6)]},
                "pagination_meta": {"after": "CURSOR2"},
            },
            {"schedule_entries": {"final": [self._entry("UME", "Me", 150, 170)]}},
        ]

        def _fake_request(url, headers=None, params=None):
            calls.append(dict(params or {}))
            return pages[len(calls) - 1]

        with mock.patch.object(incidentio, "request_json", side_effect=_fake_request):
            result = incidentio.roster(self.NOW)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["entry_window_start"], "CURSOR2")
        self.assertEqual(calls[1]["entry_window_end"], calls[0]["entry_window_end"])
        self.assertTrue(result["me_on_roster"], "the shift on page two must not be lost")

    def test_the_polled_board_reads_the_api_once_per_ttl(self) -> None:
        """``/state`` is polled; a schedule walk per poll would spend the rate limit."""
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        entries = {"schedule_entries": {"final": [self._entry("UME", "Me", -1, 1)]}}
        with mock.patch.object(incidentio, "request_json", return_value=entries) as request:
            incidentio.roster(self.NOW)
            incidentio.roster(self.NOW)
            self.assertEqual(request.call_count, 1)
            # A changed schedule list is a different question, answered at once.
            self.schedule_ids = ["SCHED1", "SCHED2"]
            incidentio.roster(self.NOW)
            self.assertEqual(request.call_count, 3)

    def test_a_failed_read_degrades_to_an_error_instead_of_raising(self) -> None:
        """``describe`` backs the board's main poll; a down API must not 500 it."""
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        with mock.patch.object(
            incidentio, "request_json", side_effect=incidentio.HttpError(503, "unavailable")
        ):
            result = incidentio.roster(self.NOW)
        self.assertEqual(result["members"], [])
        # The status reaches the card; the vendor's error body stays in the log.
        self.assertEqual(result["error"], "incident.io did not answer (HTTP 503)")
        self.assertEqual(result["error_code"], incidentio.ROSTER_ERROR_UNREACHABLE)
        self.assertEqual(result["error_status"], 503)

    def test_the_roster_is_bounded_while_walking_and_in_every_string(self) -> None:
        """``schedule_ids`` is agent-writable and the roster is cached on a polled route.

        The walk covers at most ``_MAX_ROSTER_SCHEDULES`` schedules and keeps only the
        soonest ``_MAX_ROSTER_WINDOWS`` shifts as they stream in; a refused shift leaves no
        member behind, and ids and names are clamped where they are retained.
        """
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        self.schedule_ids = ["S1", "S2", "S3"]
        asked: list[str] = []
        long_name = "N" * 1000
        pages = {
            "S1": [self._entry("ULATE", "Late", 30, 40), self._entry("UME", long_name, 1, 2)],
            "S2": [self._entry("UALICE", "Alice", 5, 6)],
            "S3": [self._entry("UNEVER", "Never walked", 0, 1)],
        }

        def _fake_request(url, headers=None, params=None):
            asked.append(params["schedule_id"])
            return {"schedule_entries": {"final": pages[params["schedule_id"]]}}

        with mock.patch.object(incidentio, "_MAX_ROSTER_WINDOWS", 2):
            with mock.patch.object(incidentio, "_MAX_ROSTER_SCHEDULES", 2):
                with mock.patch.object(incidentio, "request_json", side_effect=_fake_request):
                    with self.assertLogs(incidentio.logger, level="WARNING") as logs:
                        result = incidentio.roster(self.NOW)

        self.assertEqual(asked, ["S1", "S2"], "the schedule walk itself is capped")
        self.assertEqual(len(result["windows"]), 2)
        self.assertEqual([m["login"] for m in result["members"]], ["UME", "UALICE"])
        self.assertLessEqual(len(result["members"][0]["name"]), incidentio._MAX_ROSTER_TEXT)
        self.assertIn("left out 1", logs.output[0], "the overflow is said once per read")

    def test_a_read_past_its_deadline_ends_as_an_error_not_a_partial_roster(self) -> None:
        """A slow-but-answering vendor must not hold a cache-miss poll for minutes."""
        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        with mock.patch.object(incidentio, "_ROSTER_WALK_DEADLINE_SECS", 0.0):
            with mock.patch.object(incidentio, "request_json") as request:
                result = incidentio.roster(self.NOW)
        request.assert_not_called()
        self.assertEqual(result["members"], [])
        self.assertEqual(result["error_code"], incidentio.ROSTER_ERROR_UNREACHABLE)

    def test_the_board_falls_back_to_this_roster_without_a_schedule_file(self) -> None:
        """The wiring: ``describe``'s roster slot, not just the function."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        policy_store.put(policy_store.INCIDENTIO_USER_KEY, "UME")
        entries = {"schedule_entries": {"final": [self._entry("UME", "Me", -1, 1)]}}
        with mock.patch.object(incidentio, "request_json", return_value=entries):
            result = rotation._roster_safely()
        self.assertEqual(result["source"], "incidentio")
        self.assertTrue(result["members"])


class TestIncidentIoUserExists(_IncidentIoCase):
    """The save-time identity check: only a definite 404 may refuse."""

    def _configured(self) -> None:
        for name, value in (("provider_enabled", True), ("has_secrets", True), ("_headers", {})):
            patcher = mock.patch.object(incidentio, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_without_a_key_the_answer_is_unknown_and_nothing_is_asked(self) -> None:
        with mock.patch.object(incidentio, "request_json") as request:
            self.assertIsNone(incidentio.user_exists("01ABC"))
        request.assert_not_called()

    def test_a_404_is_the_only_no_and_any_other_failure_cannot_say(self) -> None:
        """An outage must not read as "no such user" and lock the operator out."""
        self._configured()
        with mock.patch.object(
            incidentio, "request_json", side_effect=incidentio.HttpError(404, "not found")
        ):
            self.assertIs(incidentio.user_exists("Sam Example"), False)
        for status in (0, 401, 429, 503):
            with mock.patch.object(
                incidentio, "request_json", side_effect=incidentio.HttpError(status, "x")
            ):
                self.assertIsNone(incidentio.user_exists("01ABC"), status)

    def test_a_known_user_is_yes_and_the_id_is_path_escaped(self) -> None:
        self._configured()
        with mock.patch.object(incidentio, "request_json", return_value={"user": {}}) as request:
            self.assertIs(incidentio.user_exists("a b/c"), True)
        self.assertTrue(request.call_args.args[0].endswith("/v2/users/a%20b%2Fc"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
