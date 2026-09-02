"""An auto-approve expiry that lands on an unattended run must not be silent.

An ordinary expiry degrades gracefully: the next tool call asks a human, and a
human is there to answer. An expiry that lands while a monitor loop is driving the
session degrades into nothing at all -- the loop keeps waking on its interval,
dispatches a tool, waits out the approval window with nobody present, and
accomplishes no work until someone notices. The observed shape was twenty
two-hour turn deaths in one night, seventeen of them approval-waits.

Nothing here changes how long a grant lasts. It makes the moment it ends
attributable on BOTH surfaces -- the dashboard feed and the owner's push channel --
and points at the setting (`until_shutdown`) that already exists for runs meant to
go unattended.

Every behavioural claim below is mutation-verified; the mutation is named in the
test so a future reader can repeat it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.server import (
    _armed_unattended_loops,
    _notify_unattended_expiry,
    _unattended_expiry_text,
)
from kiro_crew.safety_override import SafetyOverride, reset_singleton


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_singleton()
    yield
    reset_singleton()


def _quiet_sel():
    """Silence the SEL sink the way the sibling suites do."""
    return patch("kiro_crew.safety_override.sel", return_value=MagicMock())


def _fake_state() -> SimpleNamespace:
    return SimpleNamespace(notify=MagicMock(), _background_tasks=set())


def _svc_with(*loops) -> MagicMock:
    svc = MagicMock()
    svc.list_all.return_value = list(loops)
    return svc


def _loop(active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id="L1", active=active, idle_secs=420)


class TestWhichLoopsCount:
    def test_only_active_loops_are_counted(self) -> None:
        """Mutation: drop the `active` filter -- a stopped loop produces a notice
        about a run that is not running, and this test fails.
        """
        svc = _svc_with(_loop(active=True), _loop(active=False))
        with patch("kiro_crew.dashboard.server._autonudge_get", return_value=svc):
            assert len(_armed_unattended_loops()) == 1

    def test_no_service_means_no_loops(self) -> None:
        with patch("kiro_crew.dashboard.server._autonudge_get", return_value=None):
            assert _armed_unattended_loops() == []

    def test_an_enumeration_failure_reads_as_no_loops(self) -> None:
        """Losing the notice is a missing message; letting the exception escape would
        break the demotion that runs alongside it, which is a security failure.

        Mutation: remove the try/except -- the exception propagates and this fails.
        """
        svc = MagicMock()
        svc.list_all.side_effect = RuntimeError("registry unavailable")
        with patch("kiro_crew.dashboard.server._autonudge_get", return_value=svc):
            assert _armed_unattended_loops() == []


class TestBothSurfacesAreNotified:
    """The operator this notice exists for is, by definition, not looking at a
    dashboard. Delivering only to the dashboard feed would aim the one
    absent-operator notice at the one surface that requires presence.
    """

    def _drive(self, state, svc):
        async def _run():
            with patch("kiro_crew.dashboard.server._autonudge_get", return_value=svc):
                with patch("kiro_crew.dashboard.server._dm_owner") as dm:
                    dm.return_value = asyncio.sleep(0)
                    _notify_unattended_expiry(state, "dashboard")
                    # Let the scheduled DM task run to completion.
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    return dm

        return asyncio.run(_run())

    def test_the_dashboard_feed_gets_the_notice(self) -> None:
        state = _fake_state()
        self._drive(state, _svc_with(_loop()))
        assert state.notify.call_count == 1
        args = state.notify.call_args.args
        assert "unattended run" in args[1]

    def test_the_owner_push_channel_also_gets_it(self) -> None:
        """Mutation: delete the `_dm_owner` scheduling -- the away operator gets
        nothing until they open the dashboard, and this test fails.
        """
        state = _fake_state()
        dm = self._drive(state, _svc_with(_loop()))
        assert dm.call_count == 1, "the owner DM was never scheduled"
        assert "until_shutdown" in dm.call_args.args[1]

    def test_both_surfaces_carry_the_same_body(self) -> None:
        """One source of truth for the text, so the pushed message and the stored
        note cannot drift into saying different things about the same event."""
        state = _fake_state()
        dm = self._drive(state, _svc_with(_loop()))
        body = _unattended_expiry_text(1)
        assert state.notify.call_args.args[2] == body
        assert body in dm.call_args.args[1]

    def test_nothing_is_sent_when_no_loop_is_running(self) -> None:
        """An ordinary expiry is already reported by the existing path; this notice
        must not duplicate it.

        Mutation: drop the `if not armed: return` guard -- every ordinary expiry
        produces a second, wrong notice and this test fails.
        """
        state = _fake_state()
        dm = self._drive(state, _svc_with(_loop(active=False)))
        assert state.notify.call_count == 0
        assert dm.call_count == 0

    def test_a_failed_dashboard_note_does_not_block_the_push(self) -> None:
        """The two surfaces are independent: a broken notification bus must not cost
        the away operator their only push.

        Mutation: move the DM scheduling inside the notify try-block -- a raising
        notify swallows the DM and this test fails.
        """
        state = _fake_state()
        state.notify.side_effect = RuntimeError("bus down")
        dm = self._drive(state, _svc_with(_loop()))
        assert dm.call_count == 1

    def test_no_running_loop_degrades_to_the_dashboard_note_only(self) -> None:
        """Called from a synchronous context (the expiry callback can be reached from
        one), there is no loop to schedule the DM on. The note must still land
        rather than the whole notice being lost.
        """
        state = _fake_state()
        svc = _svc_with(_loop())
        with patch("kiro_crew.dashboard.server._autonudge_get", return_value=svc):
            _notify_unattended_expiry(state, "dashboard")
        assert state.notify.call_count == 1


class TestTheNoticeNamesTheRemedy:
    def test_the_body_points_at_the_existing_non_expiring_option(self) -> None:
        """The cheapest half of this problem is that operators do not know
        `until_shutdown` exists. Saying so at the moment it would have helped is
        worth more than the notice itself.

        Mutation: drop the mention -- this test fails.
        """
        body = _unattended_expiry_text(3)
        assert "until_shutdown" in body
        assert "yolo_duration" in body

    def test_the_body_states_the_consequence_not_just_the_event(self) -> None:
        """"Your grant expired" is not actionable at 4am; "your run is now waiting
        on approvals nobody will give" is."""
        body = _unattended_expiry_text(2)
        assert "2 monitor loop(s)" in body
        assert "approval" in body


class TestTheExpiryCallbackFiresExactlyOnce:
    """The notice needs no dedupe, and that is a property of the override rather
    than of the notifier -- so it is asserted here rather than assumed.

    Lazy expiry clears ``_active`` INSIDE the lock before invoking the callback, so
    every later ``is_active()`` returns early without re-firing. If that ever
    changes, a per-cycle alarm appears and the notice must grow a dedupe.
    """

    def test_repeated_is_active_calls_fire_the_callback_once(self) -> None:
        """Mutation: move the `self._active = False` assignment after the callback
        invocation -- the callback fires on every poll and this test fails.
        """
        override = SafetyOverride()
        fired: list[str] = []
        override.on_expired = lambda source: fired.append(source)

        with _quiet_sel():
            override.activate("dashboard", ttl=600)
        anchor = override._activated_at

        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=anchor + 700
        ):
            for _ in range(5):
                assert override.is_active() is False

        assert fired == ["dashboard"], fired

    def test_a_permanent_grant_never_reaches_the_expiry_path(self) -> None:
        """`until_shutdown` and the declared grant are exactly the configurations
        this notice recommends, so they must not produce it themselves.
        """
        override = SafetyOverride()
        fired: list[str] = []
        override.on_expired = lambda source: fired.append(source)

        with _quiet_sel():
            override.activate_declared()
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic",
            return_value=override._activated_at + 30 * 24 * 3600,
        ):
            assert override.is_active() is True

        assert fired == []


class TestTheNoticeDoesNotOverclaimTheStall:
    """Global auto-approve is not the only path to one.

    A slot carrying its own trust grant is approved by ``slot._trust`` independently
    of the grant (``chat_runner``: ``if slot._trust or yolo_active``), so its cycles
    keep running after this expiry. Stating the stall as fact would send an operator
    to rescue a run that is fine, which costs exactly the attention this notice
    exists to spend well.
    """

    def test_the_stall_is_conditional_not_asserted_of_every_cycle(self) -> None:
        """Mutation: restore the unconditional 'each cycle now waits' -- fails here."""
        body = _unattended_expiry_text(1)
        assert "each cycle now waits" not in body
        assert "relied on it" in body

    def test_the_trust_exception_is_named(self) -> None:
        """An operator whose loop keeps working needs to know why, or the notice
        reads as a bug in the notice rather than a description of their setup.
        """
        assert "trust" in _unattended_expiry_text(1).lower()


class TestTheNoticeHasItsOwnChannel:
    """The bus maps a legacy ``kind`` to ``system.<kind>`` and falls back to
    ``system.agent`` for anything unregistered -- a path whose own comment says it
    is defensive because "nothing emits unknown kinds today".

    Emitting `safety_override` without registering it would have made live code the
    first thing to violate that, turning a compatibility shim for old JSONL rows
    into a normal delivery path and making the comment false. Registering the
    channel is what keeps the fallback defensive.
    """

    def test_the_kind_routes_to_its_own_channel_not_the_fallback(self) -> None:
        """Mutation: remove `system.safety_override` from `SYSTEM_CHANNELS` -- the
        note silently lands on `system.agent` and this test fails.
        """
        from kiro_crew.notifications.bus import _FALLBACK_CHANNEL, payload_from_legacy

        payload = payload_from_legacy("safety_override", "t", "b")
        assert payload.channel == "system.safety_override"
        assert payload.channel != _FALLBACK_CHANNEL

    def test_the_notifier_emits_that_kind(self) -> None:
        """Ties the registration to the caller: registering a channel nothing emits,
        or emitting a kind matching no channel, are both silently wrong.
        """
        state = _fake_state()
        svc = _svc_with(_loop())
        with patch("kiro_crew.dashboard.server._autonudge_get", return_value=svc):
            _notify_unattended_expiry(state, "dashboard")
        assert state.notify.call_args.args[0] == "safety_override"

    def test_it_is_default_priority_not_critical(self) -> None:
        """The prompt that actually blocks a turn has its own critical channel
        (`system.approval`); this is the report about it. Asserted so an escalation
        to critical is a deliberate edit rather than a drift.
        """
        from kiro_crew.notifications.bus import SYSTEM_CHANNELS

        assert SYSTEM_CHANNELS["system.safety_override"] == SYSTEM_CHANNELS["system.skills"]
        assert SYSTEM_CHANNELS["system.safety_override"] != SYSTEM_CHANNELS["system.approval"]
