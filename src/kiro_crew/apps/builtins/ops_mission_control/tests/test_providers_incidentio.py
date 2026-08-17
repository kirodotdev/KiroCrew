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
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    ACTION_ACK,
    ACTION_COMMENT,
    ACTION_RESOLVE,
    ACTION_SILENCE,
    Signal,
)
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
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import incidentio

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
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

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
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

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
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)

        def _fmt(seconds: int) -> str:
            return (now + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "user": {"id": user_id, "name": "me"},
            "start_at": _fmt(start_delta),
            "end_at": _fmt(end_delta),
        }

    def _status_for(self, entry: dict[str, Any]):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

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
    def _alert(alert_id: str, dedup: str = "") -> dict[str, Any]:
        return {
            "id": alert_id,
            "title": f"alert {alert_id}",
            "status": "firing",
            "deduplication_key": dedup,
            "alert_source_id": "SRC1",
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

        An estate just over one page used to report truncated on every cycle, which
        stopped ``reconcile`` from ever resolving one of this source's signals.
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

    def test_client_side_source_filtering_preserves_the_truncation_verdict(self) -> None:
        """Filtering after the fetch must not turn a partial estate into a full one.

        The surviving count can fall to zero while the provider still had more, so the
        verdict is taken from the walk rather than from what is left.
        """
        incidentio, adapter = self._adapter()
        pages = [
            self._page([self._alert(f"P{page}-{i}") for i in range(50)], after=f"cursor-{page}")
            for page in range(6)
        ]

        with mock.patch.object(incidentio, "request_json", side_effect=pages):
            with mock.patch.object(incidentio, "config_list", return_value=["OTHER-SOURCE"]):
                with mock.patch.object(adapter, "configured", return_value=True):
                    signals = adapter._poll_sync()

        self.assertEqual(len(signals), 0, "no alert came from the configured source")
        self.assertIsInstance(signals, TruncatedSignals)

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
