"""Tests for the local notification channel.

The load-bearing properties, in order of what would hurt most if broken:

1. **The in-process path is not a bypass.** It skips the HTTP handler, so it has to
   re-implement the handler's two guards — the manifest-declared-channel check and the
   per-app rate limiter — and it must consume from the SAME limiter instance the HTTP
   path would. If this regresses, a local-first app gains an unthrottled push.
2. **Silent by default.** A fresh install must produce nothing at all.
3. **One push per STATE CHANGE.** A source that is still failing and an incident that
   is still parked on an approval must not re-notify. This is the shipped noise stance,
   and at a 120-second heartbeat a regression here is 30 toasts an hour.
4. **Never fatal.** No bus, no limiter, no manifest, a raising bus — every one of those
   is a quiet False, exactly as a missing Slack client is.
5. **Redacted at the sink.** The text originates in a provider payload and lands in the
   OS notification centre and in persisted JSONL.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, notify_out
from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal
from kiro_crew.notifications.bus import NotificationPayload, NotificationValidationError
from kiro_crew.notifications.rate_limit import AppRateLimiter

_APP = notify_out.APP_NAME

#: The channels app.json declares, mirrored so a manifest edit that drops one fails here
#: rather than at 3am. Keys are ids; values are the default priority the bus must get.
_DECLARED = {
    "waiting-on-you": "critical",
    "source-health": "default",
    "incident-released": "passive",
}


class _FakeBus:
    """Records pushes the way NotificationBus would receive them."""

    def __init__(self, *, raise_on_push: bool = False) -> None:
        self.pushed: list[NotificationPayload] = []
        self.registered: dict[str, str] = {}
        self._raise = raise_on_push

    def is_registered(self, channel: str) -> bool:
        return channel in self.registered

    def register_channel(self, channel: str, default_priority: str = "default") -> None:
        self.registered[channel] = default_priority

    def push(self, payload: NotificationPayload) -> dict[str, Any]:
        if self._raise:
            raise NotificationValidationError("bus said no")
        self.pushed.append(payload)
        return {"channel": payload.channel}


class _FakeState:
    """The two attributes notify_out reads off DashboardState, and nothing else."""

    def __init__(self, bus: Any = None, limiter: Any = None) -> None:
        if bus is not None:
            self.notification_bus = bus
        if limiter is not None:
            self.notification_rate_limiter = limiter


class _NotifyBase(unittest.TestCase):
    """Isolated data home, app reported ENABLED, manifest channels stubbed.

    ``declared_channels`` reads the INSTALLED manifest out of the data home, which a
    temp dir does not have — so it is patched to the real app.json's declaration. The
    contract between that patch and the file on disk is pinned separately by
    ``TestChannelsMatchTheManifest``, so this stub cannot drift into fiction.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self._tmp.name
        # Patched ON THE MODULE UNDER TEST, not as "kiro_crew.apps.manager.is_app_enabled".
        # A dotted patch walks package attributes, and `test_ledger_sync_git` evicts this
        # app's modules from `sys.modules` to simulate two processes — after which the two
        # routes can resolve DIFFERENT copies of the manager module, so the patch lands on
        # one and the gate reads the other. That produced 19 order-dependent failures that
        # all looked like "the push did not happen".
        self._enabled = mock.patch.object(notify_out, "is_app_enabled", return_value=True)
        self._enabled.start()
        self._channels = mock.patch.object(
            notify_out,
            "declared_channels",
            return_value=[
                {"id": cid, "name": cid, "icon": "", "default_priority": prio}
                for cid, prio in _DECLARED.items()
            ],
        )
        self._channels.start()

    def tearDown(self) -> None:
        self._channels.stop()
        self._enabled.stop()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        self._tmp.cleanup()

    def _state(self, **kw: Any) -> tuple[_FakeState, _FakeBus, AppRateLimiter]:
        bus = _FakeBus(**kw)
        limiter = AppRateLimiter()
        return _FakeState(bus, limiter), bus, limiter


class TestChannelsMatchTheManifest(unittest.TestCase):
    """The module's channel constants and app.json must agree, or nothing is delivered.

    ``_push`` refuses an id the manifest does not declare — which is the whole point of
    replicating the handler's check — so a rename on one side alone produces a channel
    that silently pushes nothing. That failure is invisible at runtime (a log line), so
    it has to be visible here.
    """

    def _manifest(self) -> dict[str, Any]:
        path = Path(notify_out.__file__).resolve().parents[1] / "app.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_manifest_declares_exactly_the_three_channels_the_module_pushes_to(
        self,
    ) -> None:
        declared = {c["id"] for c in self._manifest()["notifications"]["channels"]}
        self.assertEqual(
            declared,
            {
                notify_out.CHANNEL_WAITING_ON_YOU,
                notify_out.CHANNEL_SOURCE_HEALTH,
                notify_out.CHANNEL_INCIDENT_RELEASED,
            },
        )

    def test_declared_priorities_are_the_ones_the_tests_assume(self) -> None:
        """Pins the stub in ``_NotifyBase`` to the real file."""
        actual = {
            c["id"]: c.get("defaultPriority", "default")
            for c in self._manifest()["notifications"]["channels"]
        }
        self.assertEqual(actual, _DECLARED)

    def test_waiting_on_you_is_not_a_protected_channel(self) -> None:
        """An app must not be able to hand itself an unmutable channel.

        ``waiting-on-you`` is critical, which is defensible — it is the one state that
        blocks an agent turn — but critical is not the same as unsilenceable. Only
        ``system.approval`` is protected, and an app channel joining that set would take
        a control away from the operator.
        """
        from kiro_crew.notifications.settings import PROTECTED_CHANNELS

        for cid in _DECLARED:
            self.assertNotIn(f"{_APP}.{cid}", PROTECTED_CHANNELS)


class TestBuiltinManifestsCarryTheirChannels(unittest.TestCase):
    """The discovery converter must not drop ``notifications``.

    ``register_builtin_apps`` persists ``_manifest_to_builtin_dict``'s output as the
    app's on-disk ``app.json``, and ``get_app_manifest`` reads that file — so a field the
    converter omits does not exist as far as every consumer is concerned. It omitted
    ``notifications`` entirely, and because ``notifications`` is a ``_KNOWN_FIELDS``
    member it did not survive in ``extra`` either. Nothing caught it because no builtin
    had ever declared a channel.
    """

    def test_declared_channels_survive_the_builtin_dict_conversion(self) -> None:
        from kiro_crew.apps.discovery import _manifest_to_builtin_dict
        from kiro_crew.apps.manifest import AppManifest

        path = Path(notify_out.__file__).resolve().parents[1] / "app.json"
        converted = _manifest_to_builtin_dict(AppManifest.from_json_file(path))
        ids = {c["id"] for c in converted["notifications"]["channels"]}
        self.assertEqual(ids, set(_DECLARED))


class TestSilentByDefault(_NotifyBase):
    def test_a_fresh_install_notifies_nothing(self) -> None:
        state, bus, _ = self._state()
        self.assertFalse(notify_out.configured())
        self.assertFalse(
            notify_out.notify_needs_human(state, "inc-1", "DLQ depth above threshold")
        )
        self.assertEqual(bus.pushed, [])

    def test_status_names_the_toggle_when_off(self) -> None:
        state, _, _ = self._state()
        status = notify_out.status(state)
        self.assertFalse(status["ready"])
        self.assertTrue(status["bus_available"])
        self.assertIn("off", status["detail"].lower())

    def test_status_distinguishes_a_process_with_no_bus(self) -> None:
        """Off and no-bus need different fixes, so they must not read the same.

        A CLI or a test process holds no ``DashboardState``, so there is nothing to push
        through and no operator action would change that — telling them to flip a toggle
        would be advice that cannot work.
        """
        notify_out.set_settings(enabled=True)
        status = notify_out.status(None)
        self.assertTrue(status["enabled"])
        self.assertFalse(status["bus_available"])
        self.assertFalse(status["ready"])
        self.assertIn("bus", status["detail"].lower())

    def test_enabling_writes_no_secret_material(self) -> None:
        notify_out.set_settings(enabled=True)
        secrets = Path(self._tmp.name) / "ops_mission_control_secrets.json"
        self.assertFalse(secrets.exists())


class TestTheGuardsTheHttpHandlerOwnsAreReplicated(_NotifyBase):
    """Decision (a): in-process, therefore both guards re-implemented here."""

    def setUp(self) -> None:
        super().setUp()
        notify_out.set_settings(enabled=True)

    def test_an_undeclared_channel_is_refused(self) -> None:
        """The manifest check is what stops this path being a bypass.

        The HTTP handler 400s an undeclared id. In-process there is nobody to 400, so
        the refusal has to happen here or the manifest declaration becomes decorative.
        """
        state, bus, _ = self._state()
        self.assertFalse(
            notify_out._push(state, "not-declared", "t", "b", group_key="inc-1")
        )
        self.assertEqual(bus.pushed, [])
        self.assertEqual(bus.registered, {})

    def test_a_channel_registers_once_with_its_manifest_priority(self) -> None:
        state, bus, _ = self._state()
        notify_out.notify_needs_human(state, "inc-1", "DLQ depth")
        self.assertEqual(bus.registered, {f"{_APP}.waiting-on-you": "critical"})
        # A second push must not re-register: doing so would stomp a runtime priority
        # override the operator set in Settings → Notifications.
        bus.registered[f"{_APP}.waiting-on-you"] = "passive"
        notify_out.notify_needs_human(state, "inc-2", "Other alarm")
        self.assertEqual(bus.registered[f"{_APP}.waiting-on-you"], "passive")

    def test_the_state_owned_limiter_is_the_one_consumed(self) -> None:
        """One budget, not two.

        A fresh ``AppRateLimiter`` here would mean the in-process path and any future
        HTTP push each had their own 30-per-300s allowance — twice the ceiling the RFC
        set, reached by a path nobody audited.
        """
        state, bus, limiter = self._state()
        notify_out.notify_needs_human(state, "inc-1", "DLQ depth")
        self.assertEqual(len(bus.pushed), 1)
        # Drain the SHARED bucket from the outside and the next push must be refused.
        while limiter.allow(_APP):
            pass
        self.assertFalse(notify_out.notify_needs_human(state, "inc-2", "Other alarm"))
        self.assertEqual(len(bus.pushed), 1)

    def test_an_invalid_payload_does_not_drain_the_budget(self) -> None:
        """Validation runs BEFORE the limiter, same order as the handler.

        An invalid payload delivers nothing, so charging it a token would let a bug
        429-block the notifications that are fine. An empty title is the cheapest way to
        make the payload invalid.
        """
        state, bus, limiter = self._state()
        self.assertFalse(
            notify_out._push(state, "source-health", "", "body", group_key="s:1")
        )
        self.assertEqual(bus.pushed, [])
        spent = 0
        while limiter.allow(_APP):
            spent += 1
        self.assertEqual(spent, 10, "a refused payload must cost no tokens")

    def test_a_disabled_app_cannot_push(self) -> None:
        """Deny-by-default, the same gate every route already applies."""
        state, bus, _ = self._state()
        with mock.patch.object(notify_out, "is_app_enabled", return_value=False):
            self.assertFalse(notify_out.notify_needs_human(state, "inc-1", "DLQ depth"))
        self.assertEqual(bus.pushed, [])


class TestNeverFatal(_NotifyBase):
    def setUp(self) -> None:
        super().setUp()
        notify_out.set_settings(enabled=True)

    def test_no_state_is_a_quiet_false(self) -> None:
        self.assertFalse(notify_out.notify_needs_human(None, "inc-1", "DLQ depth"))

    def test_a_state_without_a_bus_is_a_quiet_false(self) -> None:
        self.assertFalse(
            notify_out.notify_needs_human(_FakeState(), "inc-1", "DLQ depth")
        )

    def test_a_state_without_a_limiter_still_delivers(self) -> None:
        """A missing limiter must not silently stop notifications.

        It cannot happen on a real gateway (``DashboardState`` always constructs one),
        so the choice is between failing closed on an impossible condition and
        delivering. Delivering is right: the limiter exists to cap a flood, and the
        absence of the object is not a flood.
        """
        bus = _FakeBus()
        self.assertTrue(
            notify_out.notify_needs_human(_FakeState(bus), "inc-1", "DLQ depth")
        )
        self.assertEqual(len(bus.pushed), 1)

    def test_a_raising_bus_is_a_quiet_false(self) -> None:
        state, _, _ = self._state(raise_on_push=True)
        self.assertFalse(notify_out.notify_needs_human(state, "inc-1", "DLQ depth"))

    def test_an_unreadable_manifest_is_a_quiet_false(self) -> None:
        state, bus, _ = self._state()
        with mock.patch.object(notify_out, "declared_channels", return_value=[]):
            self.assertFalse(notify_out.notify_needs_human(state, "inc-1", "DLQ depth"))
        self.assertEqual(bus.pushed, [])

    def test_declared_channels_never_raises_without_an_install(self) -> None:
        """It runs inside ``status()``, which ``/state`` calls on every dashboard poll."""
        self._channels.stop()
        try:
            self.assertEqual(notify_out.declared_channels(), [])
        finally:
            self._channels.start()


class TestOutboundTextIsRedacted(_NotifyBase):
    def setUp(self) -> None:
        super().setUp()
        notify_out.set_settings(enabled=True)

    def test_a_credential_in_a_provider_title_never_reaches_the_bus(self) -> None:
        """A provider alarm description can carry anything; this one lands on a desktop.

        Central redaction in ``DashboardState._deliver_note`` also covers this, but the
        two are not redundant: this module is the registered egress sink, and belt-and-
        braces at the producer means a future sink refactor cannot quietly un-redact it.
        """
        state, bus, _ = self._state()
        notify_out.notify_needs_human(
            state, "inc-1", "auth failed for AKIAIOSFODNN7EXAMPLE on the queue"
        )
        self.assertEqual(len(bus.pushed), 1)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", bus.pushed[0].title)

    def test_a_provider_failure_reason_is_redacted_too(self) -> None:
        state, bus, _ = self._state()
        notify_out.notify_source_unhealthy(
            state, "datadog", "401 from https://api.datadoghq.com?api_key=deadbeefcafe1234"
        )
        self.assertEqual(len(bus.pushed), 1)
        self.assertNotIn("deadbeefcafe1234", bus.pushed[0].body)


class TestNotificationShape(_NotifyBase):
    def setUp(self) -> None:
        super().setUp()
        notify_out.set_settings(enabled=True)

    def test_the_group_key_is_the_incident_id_so_repeats_collapse(self) -> None:
        """The feed stacks on ``group_key``; the incident is the thing being reported."""
        state, bus, _ = self._state()
        notify_out.notify_needs_human(state, "inc-42", "DLQ depth")
        self.assertEqual(bus.pushed[0].group_key, "inc-42")

    def test_a_source_note_groups_by_source_not_by_incident(self) -> None:
        """Consecutive failures of one source are one condition, so they stack."""
        state, bus, _ = self._state()
        notify_out.notify_source_unhealthy(state, "cloudwatch", "timed out after 10s")
        self.assertEqual(bus.pushed[0].group_key, "source:cloudwatch")

    def test_the_deep_link_is_the_app_page_and_never_a_per_incident_query(self) -> None:
        """The page selects incidents from React state and reads no query parameter.

        An ``?id=`` link would be the notification promising a jump the UI cannot make —
        the overstated-claim defect. Path-only is also the only thing
        ``bus._validate_internal_url`` accepts.
        """
        state, bus, _ = self._state()
        notify_out.notify_needs_human(state, "inc-1", "DLQ depth")
        self.assertEqual(bus.pushed[0].url, "/ops-mission-control")

    def test_only_the_released_note_expires(self) -> None:
        """A release is history once work resumes; a person is still waiting until acted on."""
        state, bus, _ = self._state()
        notify_out.notify_incidents_released(state, ["inc-1"])
        notify_out.notify_needs_human(state, "inc-2", "DLQ depth")
        notify_out.notify_source_unhealthy(state, "cloudwatch", "timed out")
        by_channel = {p.channel.rsplit(".", 1)[-1]: p for p in bus.pushed}
        self.assertEqual(by_channel["incident-released"].ttl, notify_out.RELEASED_TTL_SECS)
        self.assertIsNone(by_channel["waiting-on-you"].ttl)
        self.assertIsNone(by_channel["source-health"].ttl)

    def test_the_blocked_reason_is_included_when_known(self) -> None:
        """"Needs human" alone does not say whether a click or a decision is wanted."""
        state, bus, _ = self._state()
        notify_out.notify_needs_human(state, "inc-1", "DLQ depth", "awaiting_approval")
        self.assertIn("awaiting approval", bus.pushed[0].body)

    def test_one_note_per_released_incident_not_one_summary(self) -> None:
        """A summary could not carry a per-incident ``group_key``, so it would pile up."""
        state, bus, _ = self._state()
        self.assertEqual(notify_out.notify_incidents_released(state, ["a", "b"]), 2)
        self.assertEqual([p.group_key for p in bus.pushed], ["a", "b"])

    def test_an_enormous_provider_title_is_clipped_not_refused(self) -> None:
        """The bus caps a title at 500 chars; being refused would lose the alarm."""
        state, bus, _ = self._state()
        self.assertTrue(notify_out.notify_needs_human(state, "inc-1", "x" * 5000))
        self.assertLessEqual(len(bus.pushed[0].title), 500)


class TestOnePushPerStateChange(_NotifyBase):
    """The shipped noise stance, applied to the cycle's health diff.

    ``SKILL.md`` forbids re-notifying for an unchanged condition. The dispatch cron runs
    every 120 seconds, so a source that is merely STILL failing would otherwise produce
    a toast every two minutes for as long as the outage lasts.
    """

    def setUp(self) -> None:
        super().setUp()
        notify_out.set_settings(enabled=True)

    def test_a_source_that_just_failed_notifies_once(self) -> None:
        state, bus, _ = self._state()
        before = {"cloudwatch": {"ok": True}}
        after = {"cloudwatch": {"ok": False, "detail": "timed out after 10s"}}
        dispatch._notify_cycle_changes(state, before, after, [])
        self.assertEqual(len(bus.pushed), 1)
        self.assertIn("timed out", bus.pushed[0].body)

    def test_a_source_that_is_still_failing_notifies_nothing(self) -> None:
        state, bus, _ = self._state()
        failing = {"cloudwatch": {"ok": False, "detail": "timed out after 10s"}}
        dispatch._notify_cycle_changes(state, failing, failing, [])
        self.assertEqual(bus.pushed, [])

    def test_a_first_failure_on_a_never_polled_source_is_news(self) -> None:
        """Unknown counts as "was ok" on purpose.

        A source absent from the health map has not been polled this process. Treating
        that as already-failing would swallow the one notification that matters most: the
        provider an operator has just configured does not work.
        """
        state, bus, _ = self._state()
        dispatch._notify_cycle_changes(
            state, {}, {"datadog": {"ok": False, "detail": "401 Unauthorized"}}, []
        )
        self.assertEqual(len(bus.pushed), 1)

    def test_a_recovered_source_notifies_nothing(self) -> None:
        """Recovery is good news and this channel is for what needs a person."""
        state, bus, _ = self._state()
        dispatch._notify_cycle_changes(
            state, {"cloudwatch": {"ok": False}}, {"cloudwatch": {"ok": True}}, []
        )
        self.assertEqual(bus.pushed, [])

    def test_released_incidents_notify_once_each(self) -> None:
        state, bus, _ = self._state()
        dispatch._notify_cycle_changes(state, {}, {}, ["inc-1", "inc-2"])
        self.assertEqual({p.group_key for p in bus.pushed}, {"inc-1", "inc-2"})

    def test_a_cycle_with_no_state_notifies_nothing_and_does_not_raise(self) -> None:
        """Every non-gateway caller of ``run_cycle`` passes no state."""
        dispatch._notify_cycle_changes(None, {}, {"x": {"ok": False}}, ["inc-1"])

    def test_a_raising_notifier_cannot_fail_the_cycle(self) -> None:
        state, _, _ = self._state()
        with mock.patch.object(
            notify_out, "notify_source_unhealthy", side_effect=RuntimeError("boom")
        ):
            dispatch._notify_cycle_changes(state, {}, {"x": {"ok": False}}, [])


class TestNothingIsPushedOnAClaim(unittest.TestCase):
    """A claim is the heartbeat working correctly, so it is deliberately silent.

    Stated as a test and not only as a comment because "the app claimed something and
    did not tell me" reads like an omission to a later reader, and the fix they would
    reach for turns this channel into the heartbeat feed the design refuses. The board
    and the Slack mirror already carry a claim.
    """

    def test_no_claim_path_calls_the_notifier(self) -> None:
        for module in (dispatch, __import__(
            "kiro_crew.apps.builtins.ops_mission_control.backend.routes",
            fromlist=["routes"],
        )):
            source = Path(module.__file__ or "").read_text(encoding="utf-8")
            for line in source.splitlines():
                if "notify_out.notify_" in line:
                    self.assertNotIn("claim", line.lower())

    def test_the_module_records_the_omission(self) -> None:
        source = Path(notify_out.__file__).read_text(encoding="utf-8")
        self.assertIn("claim", source.lower())


class TestSignalTitlesSurviveTheTrip(_NotifyBase):
    """A smoke test over a real ``Signal``, so the shape the callers pass is exercised."""

    def setUp(self) -> None:
        super().setUp()
        notify_out.set_settings(enabled=True)

    def test_a_real_signal_title_reaches_the_body(self) -> None:
        state, bus, _ = self._state()
        signal = Signal.from_dict(
            {
                "id": "sig-1",
                "source": "cloudwatch",
                "title": "DLQ depth above threshold",
                "severity": "warning",
                "state": "firing",
            }
        )
        notify_out.notify_needs_human(state, "inc-1", signal.title, "awaiting_input")
        self.assertIn("DLQ depth above threshold", bus.pushed[0].body)
        self.assertIn("inc-1", bus.pushed[0].title)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
