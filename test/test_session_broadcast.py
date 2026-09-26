"""`session_broadcast`: who it reaches, in which mode, and what a partial run reports.

Three things can go wrong here and each gets its own class. The AUDIENCE, because a
default audience that is not exactly "the sessions this caller created" either
misses a worker (silently, which is the whole failure this verb is supposed to
prevent) or reaches a session the caller may not touch. The MODE, because `queue`
and `steer` are different instructions and a mistake either way is invisible:
downgrading a steer loses the urgency the caller asked for, upgrading a queue
interrupts eight turns nobody asked to interrupt. And PARTIAL DELIVERY, which is the
normal outcome rather than an error path — one target's refusal must be a row in the
report and must not cost the other targets their message.

Authorization is deliberately NOT re-tested here. Every delivery goes through
`send_to_target`, whose gate is pinned in `test_session_control.py`; what this file
pins is that the broadcast really does route each target through it, which the
fence tests below do by asserting the refusal comes back as a row.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Every test runs in the shipped (enabled) state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _child(state, name: str, creator) -> Any:
    """A session *creator* opened, as `create_session` leaves it."""
    slot = state.get_or_create_slot(name)
    slot._created_by = creator.key
    return slot


def _busy(slot):
    """Make *slot* look like a turn is in flight.

    ``running`` is derived from the task, so a busy slot is expressed by its task —
    assigning ``running`` would raise.
    """
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _broadcast(state, caller, message="rebase first", mode="queue", targets=None, **kw):
    return asyncio.run(
        sc.broadcast_to_targets(
            state,
            caller_session_key=_key(caller),
            message=message,
            mode=mode,
            targets=targets,
            **kw,
        )
    )


class _Sends:
    """Records every `send_to_target` call and answers each one successfully.

    Patching the delivery is what makes these tests about the BROADCAST: with the
    real delivery in place a failure could come from either layer, and the layer
    under test would not be the one that moved.
    """

    def __init__(self, fail: "dict[str, sc.SessionControlError] | None" = None) -> None:
        self.calls: list[dict] = []
        self.fail = fail or {}
        #: Call order, recorded as (enter, exit) pairs so a gathered implementation
        #: is distinguishable from a sequential one.
        self.overlap = 0
        self._inside = 0

    async def __call__(self, state, **kwargs):
        self._inside += 1
        self.overlap = max(self.overlap, self._inside)
        try:
            self.calls.append(dict(kwargs))
            target = kwargs["target"]
            if target in self.fail:
                raise self.fail[target]
            # A real send suspends; this one does too, so a gathered
            # implementation would actually interleave and be caught by `overlap`.
            await asyncio.sleep(0)
            return {"ok": True, "target": target, "started": True, "steered": False}
        finally:
            self._inside -= 1

    def targets(self) -> list[str]:
        return [c["target"] for c in self.calls]


def _patched(state, caller, sends, **kw):
    with patch.object(sc, "send_to_target", new=sends):
        return _broadcast(state, caller, **kw)


# ── The audience ─────────────────────────────────────────────────────────────


class TestDefaultAudience:
    def test_it_reaches_every_session_the_caller_created(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _child(state, "chat-3", caller)
        sends = _Sends()
        out = _patched(state, caller, sends)
        assert sends.targets() == ["chat-2", "chat-3"]
        assert out["requested"] == 2 and out["delivered"] == 2

    def test_a_session_the_caller_did_not_create_is_not_in_the_default_audience(self, tmp_path):
        """The default audience is a SUBSET of what the fence admits, by construction.

        Mutation guard: widen `broadcast_audience` to every slot and this call
        reaches the user's own conversation. That is the one outcome the verb must
        never have, and it would not show up as a refusal — an unfenced caller is
        allowed to reach a peer, so the delivery would simply succeed.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _slot(state, "chat-9")  # the person's own tab; nobody created it
        sends = _Sends()
        _patched(state, caller, sends)
        assert sends.targets() == ["chat-2"]

    def test_the_caller_is_never_in_its_own_audience(self, tmp_path):
        """A self-target is refused by the gate, so including it would guarantee
        one refused row on every broadcast and teach the caller to ignore rows."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        # A slot with no creator matches `not _created_by_other` for nobody, but
        # the caller's own key must be excluded by the explicit key test rather
        # than by accident: mark it as self-created to prove that.
        caller._created_by = caller.key
        _child(state, "chat-2", caller)
        sends = _Sends()
        _patched(state, caller, sends)
        assert caller.key not in sends.targets()

    def test_no_children_is_reported_as_an_empty_audience_not_an_error(self, tmp_path):
        """A conductor before its first dispatch is in this state.

        Raising would make the caller handle a refusal for a state that is merely
        early, and "delivered 0 of 0" reads like a failure.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        sends = _Sends()
        out = _patched(state, caller, sends)
        assert out["ok"] is True and out["audience_empty"] is True
        assert out["requested"] == 0 and sends.calls == []

    def test_the_audience_is_ordered_the_same_way_on_every_call(self, tmp_path):
        """A broadcast is not atomic, so a caller comparing two reports needs one
        order in both."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for name in ("chat-5", "chat-2", "chat-9"):
            _child(state, name, caller)
        first = _Sends()
        second = _Sends()
        _patched(state, caller, first)
        _patched(state, caller, second)
        assert first.targets() == second.targets() == ["chat-2", "chat-5", "chat-9"]


class TestExplicitTargets:
    def test_a_named_subset_is_delivered_in_the_callers_order(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for name in ("chat-2", "chat-3", "chat-4"):
            _child(state, name, caller)
        sends = _Sends()
        _patched(state, caller, sends, targets=["chat-4", "chat-2"])
        assert sends.targets() == ["chat-4", "chat-2"]

    def test_a_repeated_target_is_delivered_to_once(self, tmp_path):
        """Delivering twice is the failure that matters: the second copy reads to
        the target as a second instruction."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        sends = _Sends()
        _patched(state, caller, sends, targets=["chat-2", "chat-2", "chat-2"])
        assert sends.targets() == ["chat-2"]

    def test_two_spellings_of_one_target_are_delivered_once(self, tmp_path):
        """Resolution, not caller spelling, defines the delivery identity."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        worker = _child(state, "chat-2", caller)
        worker.title = "Rebase the watchdog PR"
        sends = _Sends()
        _patched(
            state,
            caller,
            sends,
            targets=["chat-2", "Rebase the watchdog PR"],
        )
        assert sends.targets() == ["chat-2"]

    def test_an_all_blank_list_is_refused_rather_than_widened_or_no_opped(self, tmp_path):
        """Both silent answers are wrong for a list that names no session.

        Falling back to the default audience turns a malformed argument into an
        accidental fleet-wide send; answering "delivered 0 of 0" hides a call that
        never had a chance of working.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        sends = _Sends()
        with pytest.raises(sc.SessionControlError) as exc:
            _patched(state, caller, sends, targets=["", "   "])
        assert exc.value.code == "target_required"
        assert sends.calls == []

    def test_a_named_target_the_caller_may_not_touch_comes_back_as_a_refused_row(self, tmp_path):
        """Naming a target does not widen the fence; the per-target gate still rules.

        This is also how the suite proves each delivery really goes through
        `send_to_target` rather than around it: the refusal the gate raises is
        what lands in the row.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _slot(state, "chat-9")
        sends = _Sends(
            fail={
                "chat-9": sc.SessionControlError(
                    "an agent-created session can only control sessions it created itself",
                    code="not_creator",
                )
            }
        )
        out = _patched(state, caller, sends, targets=["chat-9"])
        assert out["delivered"] == 0
        assert out["results"] == [
            {
                "target": "chat-9",
                "ok": False,
                "code": "not_creator",
                "error": "an agent-created session can only control sessions it created itself",
            }
        ]


class TestTheCap:
    def test_an_oversized_audience_is_refused_rather_than_truncated(self, tmp_path):
        """A silently-cut broadcast is one the caller believes reached everyone,
        and the sessions past the cut are the ones it will never think to check."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        names = [f"chat-{i}" for i in range(sc.MAX_BROADCAST_TARGETS + 1)]
        sends = _Sends()
        with pytest.raises(sc.SessionControlError) as exc:
            _patched(state, caller, sends, targets=names)
        assert exc.value.code == "too_many_targets"
        assert sends.calls == [], "nothing may be delivered when the audience is refused"

    def test_exactly_the_cap_is_allowed(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        names = [f"chat-{i}" for i in range(sc.MAX_BROADCAST_TARGETS)]
        sends = _Sends()
        out = _patched(state, caller, sends, targets=names)
        assert out["requested"] == sc.MAX_BROADCAST_TARGETS


# ── The two modes ────────────────────────────────────────────────────────────


class TestModes:
    @pytest.mark.parametrize("mode,steer", [("queue", False), ("steer", True)])
    def test_the_mode_selects_the_delivery_for_every_target(self, tmp_path, mode, steer):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _child(state, "chat-3", caller)
        sends = _Sends()
        out = _patched(state, caller, sends, mode=mode)
        assert [c["steer"] for c in sends.calls] == [steer, steer]
        assert out["mode"] == mode

    def test_an_unknown_mode_is_refused_and_delivers_nothing(self, tmp_path):
        """No default either way. Defaulting to the queue swallows a caller's
        request to interrupt; defaulting to the steer interrupts sessions it only
        meant to leave a note for."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        sends = _Sends()
        with pytest.raises(sc.SessionControlError) as exc:
            _patched(state, caller, sends, mode="interrupt")
        assert exc.value.code == "invalid_broadcast_mode"
        assert sends.calls == []

    def test_the_target_sees_the_broadcast_verb_in_its_provenance_envelope(self, tmp_path):
        """A worker must be able to tell an instruction its siblings also got.

        Mutation guard: drop the ``via`` argument and every broadcast arrives in
        the target's transcript labelled ``session_send``, so the worker reads a
        fleet-wide instruction as one aimed at it alone.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        sends = _Sends()
        _patched(state, caller, sends)
        assert sends.calls[0]["via"] == sc.BROADCAST_VIA == "session_broadcast"

    def test_the_envelope_the_target_actually_receives_names_the_broadcast(
        self, tmp_path, monkeypatch
    ):
        """The guard above only pins the argument. This one pins the TEXT that
        reaches the target's turn, through the real delivery, so a formatter that
        ignores ``via`` is caught."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        ran: dict[str, str] = {}

        async def _fake_run_chat(_state, slot, prompt):
            ran["prompt"] = prompt

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

        async def _drive():
            out = await sc.broadcast_to_targets(
                state,
                caller_session_key=_key(caller),
                message="the base moved",
                mode="queue",
            )
            # Let the turn `enqueue_or_run_prompt` started actually run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return out

        asyncio.run(_drive())
        assert "via session_broadcast" in ran["prompt"]
        assert ran["prompt"].endswith("the base moved")


# ── Partial delivery ─────────────────────────────────────────────────────────


class TestPartialDelivery:
    def test_one_refusal_does_not_cost_the_other_targets_their_message(self, tmp_path):
        """Aborting on the first refusal leaves a broadcast half-delivered with
        nothing saying which half — the one outcome a caller cannot recover from."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for name in ("chat-2", "chat-3", "chat-4"):
            _child(state, name, caller)
        sends = _Sends(
            fail={"chat-3": sc.SessionControlError("gone", code="target_not_found", status=404)}
        )
        out = _patched(state, caller, sends)
        assert sends.targets() == ["chat-2", "chat-3", "chat-4"]
        assert out["requested"] == 3 and out["delivered"] == 2
        by_target = {r["target"]: r for r in out["results"]}
        assert by_target["chat-3"]["ok"] is False
        assert by_target["chat-3"]["code"] == "target_not_found"
        assert by_target["chat-2"]["ok"] is True and by_target["chat-4"]["ok"] is True

    def test_an_unexpected_failure_is_a_row_with_its_own_code_not_a_refusal(self, tmp_path):
        """A refusal carries a code the caller can act on; a crash does not, and
        reporting one as the other invites a retry that cannot work."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _child(state, "chat-3", caller)

        async def _boom(state_, **kwargs):
            if kwargs["target"] == "chat-2":
                raise RuntimeError("the loop went away")
            return {"ok": True, "target": kwargs["target"], "started": True, "steered": False}

        with patch.object(sc, "send_to_target", new=_boom):
            out = _broadcast(state, caller)
        by_target = {r["target"]: r for r in out["results"]}
        assert by_target["chat-2"]["code"] == "delivery_failed"
        assert by_target["chat-3"]["ok"] is True

    def test_deliveries_are_sequential_never_gathered(self, tmp_path):
        """Each delivery takes the same slot locks, runs the same containment
        re-checks and (for a steer) suspends on an RPC. Interleaving those windows
        across sessions buys nothing a caller can observe — the report is only read
        once every delivery is done either way."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for name in ("chat-2", "chat-3", "chat-4"):
            _child(state, name, caller)
        sends = _Sends()
        _patched(state, caller, sends)
        assert sends.overlap == 1, "two deliveries were in flight at once"

    def test_the_steer_outcome_is_carried_per_target(self, tmp_path):
        """A steer that fell back to the queue on ONE target must be readable as
        that target's outcome, not averaged into the call's."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _child(state, "chat-3", caller)

        async def _mixed(state_, **kwargs):
            steered = kwargs["target"] == "chat-2"
            return {
                "ok": True,
                "target": kwargs["target"],
                "started": False,
                "steered": steered,
            }

        with patch.object(sc, "send_to_target", new=_mixed):
            out = _broadcast(state, caller, mode="steer")
        by_target = {r["target"]: r for r in out["results"]}
        assert by_target["chat-2"]["steered"] is True
        assert by_target["chat-3"]["steered"] is False


class TestATargetThatNeverAnswers:
    """A delivery that never returns must cost ONE target, not the fleet.

    Sequential delivery is what makes this the sharpest failure in the verb: the
    steer arm suspends on `client.steer` -> `stdin.drain()` with no `wait_for`
    beneath it, so before the per-target bound existed one wedged session held the
    loop forever and targets behind it never heard a "stop, the issue was already
    fixed" at all. The caller did not even get a report — the client's request
    budget expired and the whole broadcast read as failed.

    The hang is expressed with an Event that is never set, so nothing here waits on
    real time; the bound itself is patched down to keep the run instant. Separate
    cases cancel at both awaits before authorization, retain the text as a pending
    steer or queue entry, and remove the slot before the observation.
    """

    @staticmethod
    def _hangs_on(target_name: str):
        """A `send_to_target` that never returns for *target_name*, and the log."""
        called: list[str] = []
        never = asyncio.Event()

        async def _send(state_, **kwargs):
            target = kwargs["target"]
            called.append(target)
            if target == target_name:
                await never.wait()
            return {"ok": True, "target": target, "started": True, "steered": False}

        return _send, called

    def test_a_hanging_target_does_not_starve_the_targets_behind_it(self, tmp_path):
        """Mutation guard: drop the `wait_for` and this test hangs rather than
        failing, which is precisely the defect — the loop has no way to move on."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for name in ("chat-2", "chat-3", "chat-4"):
            _child(state, name, caller)
        send, called = self._hangs_on("chat-3")

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=send):
                out = _broadcast(state, caller, mode="steer")

        # The target AFTER the hanging one was reached: that is the starvation.
        assert called == ["chat-2", "chat-3", "chat-4"]
        by_target = {r["target"]: r for r in out["results"]}
        assert by_target["chat-2"]["ok"] is True
        assert by_target["chat-4"]["ok"] is True
        # And the whole call still returns a report, rather than the caller's own
        # client budget expiring on a broadcast that never came back.
        assert out["requested"] == 3 and out["delivered"] == 2

    @pytest.mark.asyncio
    async def test_a_cancelled_authorized_delivery_keeps_its_target_audit(
        self, tmp_path, monkeypatch
    ):
        """The target gate already allowed this send before its delivery stalls.

        `wait_for` turns the inner cancellation into `TimeoutError` for the
        broadcast, but the authorized send itself receives `CancelledError` and
        must audit that distinct, unknowable outcome before re-raising. The
        re-raise is pinned by both the timeout row and the following target.
        """
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for name in ("chat-2", "chat-3", "chat-4"):
            _busy(_child(state, name, caller))

        never = asyncio.Event()
        inner_exceptions: list[type[BaseException]] = []

        async def _steer(_state, slot, _message, **_kwargs):
            if slot.key == "chat-3":
                try:
                    await never.wait()
                except BaseException as exc:
                    inner_exceptions.append(type(exc))
                    raise
            return STEER_STEERED

        audits: list[dict] = []
        monkeypatch.setattr("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", _steer)
        monkeypatch.setattr(sc, "_audit", lambda **kwargs: audits.append(kwargs))
        monkeypatch.setattr(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05)

        out = await sc.broadcast_to_targets(
            state,
            caller_session_key=_key(caller),
            message="stop, the issue was already fixed",
            mode="steer",
        )

        assert inner_exceptions == [asyncio.CancelledError]
        cancelled = [
            row for row in audits if row["operation"] == "send" and row["slot_key"] == "chat-3"
        ]
        assert len(cancelled) == 1
        assert cancelled[0]["outcome"] == "cancelled"
        assert cancelled[0]["detail"]["delivery_result"] == "unknown"

        by_target = {row["target"]: row for row in out["results"]}
        assert by_target["chat-3"]["code"] == "delivery_timeout"
        assert "UNKNOWN" in by_target["chat-3"]["error"]
        assert "Re-sending the same text is safe" not in by_target["chat-3"]["error"]
        assert by_target["chat-4"]["ok"] is True

    @pytest.mark.asyncio
    async def test_a_consumed_steer_timing_out_in_containment_stop_is_not_safe_to_resend(
        self, tmp_path, monkeypatch
    ):
        """A post-hand-over timeout cannot be inferred from emptied target state."""
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        target = _busy(_child(state, "chat-2", caller))
        never = asyncio.Event()

        async def _stop_never_returns(*_args, **_kwargs):
            await never.wait()

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_delivery.steer_into_running_turn",
            AsyncMock(return_value=STEER_STEERED),
        )
        monkeypatch.setattr(sc, "newly_held_constraints", lambda *_args: ["mirror_unverified"])
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _stop_never_returns)
        monkeypatch.setattr(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05)

        out = await sc.broadcast_to_targets(
            state,
            caller_session_key=_key(caller),
            message="run exactly once",
            mode="steer",
        )

        error = out["results"][0]["error"]
        assert "UNKNOWN" in error
        assert "Re-sending the same text is safe" not in error
        assert target._pending_steers == []
        assert target._queue == []
        assert target._steer_delivery_ids == {}

    @pytest.mark.parametrize("mode", ["queue", "steer"])
    @pytest.mark.parametrize("await_site", ["sel", "prewarm"])
    def test_a_cancel_before_handover_reports_that_resending_is_safe(
        self, tmp_path, monkeypatch, mode, await_site
    ):
        """Both pre-authorization awaits can consume the delivery allowance.

        Neither one has an authorized target slot yet, so cancellation there cannot
        leave the text in ``_pending_steers`` or ``_queue``. The result must use that
        observed absence in both modes and must not carry the duplicate warning
        that belongs only to text the target actually retains.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        target = _child(state, "chat-2", caller)
        never = asyncio.Event()

        if await_site == "sel":

            async def _to_thread(func, *args, **kwargs):
                if func is sc.sel:
                    await never.wait()
                raise AssertionError("the test intercepted an unexpected to_thread call")

            monkeypatch.setattr(asyncio, "to_thread", _to_thread)
            monkeypatch.setattr(sc, "prewarm_enabled_check", AsyncMock(return_value=None))
        else:
            prewarm_calls = 0

            async def _prewarm():
                nonlocal prewarm_calls
                prewarm_calls += 1
                if prewarm_calls > 1:
                    await never.wait()

            monkeypatch.setattr(sc, "prewarm_enabled_check", _prewarm)

        audits: list[dict] = []
        monkeypatch.setattr(sc, "_audit", lambda **kwargs: audits.append(kwargs))
        monkeypatch.setattr(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05)

        out = _broadcast(state, caller, message="rebase first", mode=mode)

        row = out["results"][0]
        assert row["code"] == "delivery_timeout"
        assert row["error"] == (
            "this target did not finish its delivery within 0.05s, so the broadcast "
            "stopped waiting and moved on; the target has neither a pending steer "
            "nor a queued copy, so the delivery did not reach the hand-over. "
            "Re-sending the same text is safe"
        )
        assert target._pending_steers == []
        assert target._queue == []
        assert not [row for row in audits if row["operation"] == "send"]

    def test_the_timed_out_target_is_its_own_refused_row(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _child(state, "chat-3", caller)
        send, _called = self._hangs_on("chat-2")

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=send):
                out = _broadcast(state, caller, mode="steer")

        by_target = {r["target"]: r for r in out["results"]}
        row = by_target["chat-2"]
        assert row["ok"] is False
        # Its OWN code, not the crash code: a crash did not deliver, a timeout
        # might have, and a caller that cannot tell them apart either re-sends an
        # instruction the target is already acting on or skips one it never got.
        assert row["code"] == "delivery_timeout"
        assert row["code"] != "delivery_failed"
        assert row["code"] == sc.BROADCAST_TIMEOUT_CODE

    @pytest.mark.parametrize("retained_in", ["pending", "queue"])
    def test_retained_text_forbids_resending(self, tmp_path, retained_in):
        """A matching live entry can run after this side stops waiting."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        target = _child(state, "chat-2", caller)
        never = asyncio.Event()

        async def _retains_then_hangs(state_, **kwargs):
            prompt = sc._SEND_PROVENANCE.format(
                caller=caller.key, via=sc.BROADCAST_VIA
            ) + sc.sanitize_outbound(kwargs["message"])
            if retained_in == "pending":
                target._pending_steers.append(prompt)
            else:
                target.queue_append(prompt)
            await never.wait()

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=_retains_then_hangs):
                out = _broadcast(state, caller, mode="steer")

        error = out["results"][0]["error"]
        assert error == (
            "this target did not finish its delivery within 0.05s, so the broadcast "
            "stopped waiting and moved on; whether the message has run is UNKNOWN. "
            "The same text is pending or queued on that target and may still run. "
            "Do NOT re-send it: a duplicate could run too"
        )
        for claim in ("was delivered", "has been delivered", "the target received"):
            assert claim not in error.lower(), f"the row claims delivery: {error}"
        for certain in ("will run", "will execute", "is guaranteed", "certainly"):
            assert certain not in error.lower(), f"the row promises execution: {error}"

    def test_an_unavailable_target_gets_unknown_without_resend_advice(self, tmp_path):
        """A missing original slot cannot support either delivery conclusion."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        never = asyncio.Event()

        async def _remove_then_hang(state_, **kwargs):
            state_._slots.pop(kwargs["target"])
            await never.wait()

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=_remove_then_hang):
                out = _broadcast(state, caller, mode="queue")

        error = out["results"][0]["error"]
        assert error == (
            "this target did not finish its delivery within 0.05s, so the broadcast "
            "stopped waiting and moved on; the target is unavailable, so whether "
            "the message has run or is pending or queued is UNKNOWN"
        )
        assert "re-send" not in error.lower()
        assert "re-sending" not in error.lower()
        for claim in ("was delivered", "has been delivered", "the target received"):
            assert claim not in error.lower(), f"the row claims delivery: {error}"
        for certain in ("will run", "will execute", "is guaranteed", "certainly"):
            assert certain not in error.lower(), f"the row promises execution: {error}"

    def test_the_no_handover_row_claims_neither_delivery_nor_execution(self, tmp_path):
        """Observed absence permits a retry without claiming what the target ran."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        send, _called = self._hangs_on("chat-2")

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=send):
                out = _broadcast(state, caller, mode="steer")

        error = out["results"][0]["error"]
        assert error == (
            "this target did not finish its delivery within 0.05s, so the broadcast "
            "stopped waiting and moved on; the target has neither a pending steer "
            "nor a queued copy, so the delivery did not reach the hand-over. "
            "Re-sending the same text is safe"
        )
        for delivered in ("was delivered", "has been delivered", "the target received"):
            assert delivered not in error.lower(), f"the row claims delivery: {error}"
        for certain in ("will run", "will execute", "is guaranteed", "certainly"):
            assert certain not in error.lower(), f"the row promises execution: {error}"

    def test_the_queue_mode_is_bounded_too(self, tmp_path):
        """The queue arm suspends before it delivers as well, so a stall there
        starves the fleet just as thoroughly. Same bound, same row."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _child(state, "chat-3", caller)
        send, called = self._hangs_on("chat-2")

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=send):
                out = _broadcast(state, caller, mode="queue")

        assert called == ["chat-2", "chat-3"]
        by_target = {r["target"]: r for r in out["results"]}
        assert by_target["chat-2"]["code"] == "delivery_timeout"
        assert by_target["chat-3"]["ok"] is True

    def test_the_bound_is_per_delivery_not_a_budget_over_the_fan_out(self, tmp_path):
        """A shared budget the early targets could spend would starve the late
        ones by a different route, so the allowance is charged per delivery."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for name in ("chat-2", "chat-3", "chat-4"):
            _child(state, name, caller)
        never = asyncio.Event()

        async def _all_hang(state_, **kwargs):
            await never.wait()

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=_all_hang):
                out = _broadcast(state, caller, mode="steer")

        # Every target was tried and every one got its own row: a shared budget
        # would have produced rows only for the first.
        assert [r["code"] for r in out["results"]] == ["delivery_timeout"] * 3
        assert out["delivered"] == 0

    def test_the_bound_matches_the_clients_per_target_allowance(self):
        """The backend bound and the MCP client's request budget size the same
        fan-out from opposite ends. A per-delivery bound above the client's
        per-target share lets a full audience expire the request anyway, which
        discards the per-target report the verb exists to produce.

        ONE name now serves both, in `validation`, so this asserts identity rather
        than an inequality between two literals. The identity is the stronger pin:
        an inequality still passes while the two are separately maintained, and the
        failure it guards against is someone raising one and forgetting the other.
        """
        from kiro_crew import mcp_dashboard, validation

        assert (
            sc.BROADCAST_TARGET_ALLOWANCE_SECS
            is validation.BROADCAST_TARGET_ALLOWANCE_SECS
            is mcp_dashboard.BROADCAST_TARGET_ALLOWANCE_SECS
        ), "the enforced bound and the client's per-target allowance are not one name"
        assert not hasattr(mcp_dashboard, "_BROADCAST_TARGET_ALLOWANCE_SECS"), (
            "a second private literal reappeared in the MCP client; two literals "
            "bounding one population is the drift this name exists to prevent"
        )


# ── The caller gate ──────────────────────────────────────────────────────────


class TestCallerGate:
    def test_a_disabled_surface_refuses_before_any_audience_is_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        sends = _Sends()
        with pytest.raises(sc.SessionControlError) as exc:
            _patched(state, caller, sends)
        assert exc.value.code == "session_control_disabled"
        assert sends.calls == []

    def test_an_ephemeral_caller_cannot_broadcast(self, tmp_path):
        """A session the user asked to leave no trace must not write into eight
        persistent ones."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller.memory_mode = "incognito"
        _child(state, "chat-2", caller)
        sends = _Sends()
        with pytest.raises(sc.SessionControlError) as exc:
            _patched(state, caller, sends)
        assert exc.value.code == "ephemeral_caller"
        assert sends.calls == []

    def test_a_workflow_caller_cannot_broadcast(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, f"{sc.WORKFLOW_SLOT_PREFIX}abc")
        sends = _Sends()
        with pytest.raises(sc.SessionControlError) as exc:
            _patched(state, caller, sends)
        assert exc.value.code == "unattended_caller"
        assert sends.calls == []

    def test_an_unidentifiable_caller_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        _slot(state, "chat-1")
        sends = _Sends()
        with patch.object(sc, "send_to_target", new=sends):
            with pytest.raises(sc.SessionControlError) as exc:
                asyncio.run(
                    sc.broadcast_to_targets(
                        state, caller_session_key="", message="hi", mode="queue"
                    )
                )
        assert exc.value.code == "caller_unidentified"
        assert sends.calls == []


class TestMessageBounds:
    def test_an_empty_message_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        with pytest.raises(sc.SessionControlError) as exc:
            _broadcast(state, caller, message="   ")
        assert exc.value.code == "message_empty"

    def test_an_overlong_message_is_refused_before_any_delivery(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        sends = _Sends()
        with pytest.raises(sc.SessionControlError) as exc:
            _patched(state, caller, sends, message="x" * (sc.MAX_SEND_MESSAGE_CHARS + 1))
        assert exc.value.code == "message_too_long"
        assert sends.calls == []


# ── The route ────────────────────────────────────────────────────────────────


class TestTheRoute:
    """The handler needs its own tests: a route-level defect ships the verb dead.

    That happened once already on this surface — the create route was missing from
    the strict internal path list, so every call refused while the handler-level
    tests still passed.
    """

    def _request(self, state, caller, body, *, internal=True):
        request = MagicMock()
        request.app = {"state": state}
        request.path = "/api/session-control/broadcast"
        request.method = "POST"
        request.headers = {"X-Session-Key": _key(caller)}
        request.query = {}
        request.get = lambda key, default=None: (
            True if (key in ("internal_auth", "peer_verified") and internal) else default
        )

        async def _json():
            return body

        request.json = _json
        return request

    def _body(self, response):
        import json

        return json.loads(response.body.decode())

    def test_without_the_secret_it_is_forbidden(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        req = self._request(state, caller, {"message": "hi", "mode": "queue"}, internal=False)
        resp = asyncio.run(handlers_sc.api_session_control_broadcast(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_the_route_is_on_the_strict_internal_path_list(self):
        """Registered but unlisted means the middleware ignores the secret, and the
        route is unreachable in production while every handler test still passes."""
        from kiro_crew.dashboard import server as srv

        listed = set(srv._STRICT_INTERNAL_API_PATHS)
        assert "/api/session-control/broadcast" in listed
        assert "/api/session-control/status" in listed

    def test_it_delivers_and_reports_per_target(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        sends = _Sends()
        req = self._request(state, caller, {"message": "rebase", "mode": "queue"})
        with patch.object(sc, "send_to_target", new=sends):
            resp = asyncio.run(handlers_sc.api_session_control_broadcast(req))
        assert resp.status == 200
        body = self._body(resp)
        assert body["delivered"] == 1 and body["results"][0]["target"] == "chat-2"

    def test_a_missing_mode_is_refused_at_the_route(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        req = self._request(state, caller, {"message": "hi"})
        resp = asyncio.run(handlers_sc.api_session_control_broadcast(req))
        assert resp.status == 400
        assert self._body(resp)["code"] == "invalid_broadcast_mode"

    def test_a_bare_string_targets_is_refused_rather_than_iterated(self, tmp_path):
        """This body is model-controlled. A bare string iterates as its characters,
        so an unchecked value broadcasts to one session per letter."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        req = self._request(state, caller, {"message": "hi", "mode": "queue", "targets": "chat-2"})
        resp = asyncio.run(handlers_sc.api_session_control_broadcast(req))
        assert resp.status == 400
        assert self._body(resp)["code"] == "invalid_field_type"

    def test_a_missing_message_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        req = self._request(state, caller, {"mode": "queue"})
        resp = asyncio.run(handlers_sc.api_session_control_broadcast(req))
        assert resp.status == 400
        assert self._body(resp)["code"] == "message_required"


def test_the_audit_records_which_audience_the_caller_chose(tmp_path):
    """ "Who chose these targets" is a question the trail must answer: an explicit
    list is a caller naming sessions, the default is the fence's own set."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _child(state, "chat-2", caller)
    sends = _Sends()
    audits: list[dict] = []
    with patch.object(sc, "_audit", side_effect=lambda **kw: audits.append(kw)):
        _patched(state, caller, sends)
        _patched(state, caller, sends, targets=["chat-2"])
    kinds = [a["detail"]["audience"] for a in audits if a["operation"] == "broadcast"]
    assert kinds == ["created", "explicit"]


def test_a_steer_broadcast_reaches_a_busy_target_through_the_real_delivery(tmp_path):
    """End to end on the steer arm: a busy target with a steer-capable client is
    cut into, and the report says `steered` rather than `queued`."""
    from kiro_crew.dashboard.chat_delivery import STEER_STEERED

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _busy(_child(state, "chat-2", caller))
    with patch(
        "kiro_crew.dashboard.chat_delivery.steer_into_running_turn",
        new=AsyncMock(return_value=STEER_STEERED),
    ):
        out = _broadcast(state, caller, mode="steer")
    assert out["results"] == [{"target": "chat-2", "ok": True, "started": False, "steered": True}]


def test_broadcast_audience_reads_the_same_field_the_fence_reads(tmp_path):
    """The default audience must be a subset of what the fence admits, and the only
    way to guarantee that is for both to read ONE field.

    Mutation guard: point `broadcast_audience` at a second notion of parentage (the
    crew log tree, a title convention) and a fenced caller's default broadcast
    starts collecting `not_creator` rows for sessions it can see but not reach.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    mine = _child(state, "chat-2", caller)
    theirs = _slot(state, "chat-3")
    theirs._created_by = "chat-99"
    audience = sc.broadcast_audience(state, caller.key)
    assert audience == [mine.key]
    # And the fence agrees, read through its own predicate.
    assert sc._created_by_other(theirs, caller.key) is True
    assert sc._created_by_other(mine, caller.key) is False


def test_a_cron_caller_broadcasts_only_to_what_it_dispatched(tmp_path):
    """A scheduled run is admitted to this surface and fenced to its own children,
    the same posture `session_send` gives it. The default audience must therefore
    already be that set rather than relying on per-target refusals to trim it."""
    state = _make_state(tmp_path)
    caller = _slot(state, "cron-abc123")
    state.crons.list_jobs.return_value = [SimpleNamespace(id="abc123", created_by="U0123ABCD")]
    _child(state, "chat-2", caller)
    _slot(state, "chat-9")  # the person's own tab
    sends = _Sends()
    _patched(state, caller, sends)
    assert sends.targets() == ["chat-2"]


class TestEndToEnd:
    """One chain, no layer stubbed: the MCP tool call, the HTTP route, the backend,
    and two real slots taking a real delivery.

    Every seam already has its own tests, and they are the ones that localize a
    failure — but each of them stubs the layer below, so a mismatch BETWEEN two
    layers passes all of them. The mode the tool sends and the mode the route reads,
    the audience the route asks for and the one the backend builds, the report the
    backend returns and the prose the tool renders: each of those is a contract two
    files agree on separately, and this is the only test that would notice if they
    stopped agreeing.
    """

    def _wire(self, state, caller, monkeypatch):
        """Point the MCP client's transport at the real aiohttp handlers."""

        def _post(path, payload, *, timeout=30, session_key=""):
            request = MagicMock()
            request.app = {"state": state}
            request.path = path
            request.method = "POST"
            request.headers = {"X-Session-Key": session_key or _key(caller)}
            request.query = {}
            request.get = lambda key, default=None: (
                True if key in ("internal_auth", "peer_verified") else default
            )

            async def _json():
                return payload

            request.json = _json
            import json

            resp = asyncio.run(handlers_sc.api_session_control_broadcast(request))
            assert isinstance(resp.body, (bytes, bytearray))
            return json.loads(resp.body.decode())

        def _get(path, session_key=""):
            request = MagicMock()
            request.app = {"state": state}
            request.path = path.split("?")[0]
            request.method = "GET"
            request.headers = {"X-Session-Key": session_key or _key(caller)}
            request.query = {}
            request.get = lambda key, default=None: (
                True if key in ("internal_auth", "peer_verified") else default
            )
            import json

            resp = asyncio.run(handlers_sc.api_session_control_status(request))
            assert isinstance(resp.body, (bytes, bytearray))
            return json.loads(resp.body.decode())

        monkeypatch.setattr("kiro_crew.mcp_dashboard._post", _post)
        monkeypatch.setattr("kiro_crew.mcp_dashboard._get", _get)
        monkeypatch.setattr(
            "kiro_crew.mcp_core._resolve_session_key_strict", lambda *a, **k: _key(caller)
        )

    def test_a_queue_broadcast_lands_in_both_targets_transcripts(self, tmp_path, monkeypatch):
        from kiro_crew.mcp_dashboard import _call_tool_inner

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        a = _busy(_child(state, "chat-2", caller))
        b = _busy(_child(state, "chat-3", caller))
        self._wire(state, caller, monkeypatch)

        out = _call_tool_inner("session_broadcast", {"message": "the base moved", "mode": "queue"})

        assert "2/2" in out and "chat-2" in out and "chat-3" in out
        # Busy targets, so the message is QUEUED on each rather than run — which is
        # what makes the delivery observable without executing a turn.
        for slot in (a, b):
            queued = [str(e.get("content", "")) for e in slot._queue]
            assert any(
                "via session_broadcast" in text and text.endswith("the base moved")
                for text in queued
            ), f"{slot.key} did not receive the broadcast"

    def test_an_explicitly_empty_target_list_is_refused_without_delivery(
        self, tmp_path, monkeypatch
    ):
        """Filtering to no targets must not widen into the default audience."""
        from kiro_crew.mcp_dashboard import _call_tool_inner

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        target = _busy(_child(state, "chat-2", caller))
        self._wire(state, caller, monkeypatch)

        out = _call_tool_inner(
            "session_broadcast",
            {"message": "stand down", "mode": "queue", "targets": []},
        )

        assert out.startswith("Error:")
        assert "names no session" in out
        assert target._queue == []

    def test_one_dead_target_does_not_cost_the_live_one_its_message(self, tmp_path, monkeypatch):
        """The partial-delivery contract, through every layer: the refused row
        reaches the model's prose AND the other target really got the message."""
        from kiro_crew.mcp_dashboard import _call_tool_inner

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        live = _busy(_child(state, "chat-2", caller))
        self._wire(state, caller, monkeypatch)

        out = _call_tool_inner(
            "session_broadcast",
            {"message": "stand down", "mode": "queue", "targets": ["chat-2", "chat-404"]},
        )

        assert "1/2" in out
        assert "chat-404" in out and "target_not_found" in out
        assert "Some targets were not reached" in out
        assert any("stand down" in str(e.get("content", "")) for e in live._queue)

    def test_the_status_listing_reflects_what_the_broadcast_just_did(self, tmp_path, monkeypatch):
        """The two verbs are meant to be used together — broadcast, then patrol —
        so the roster must show the queue the broadcast created."""
        from kiro_crew.mcp_dashboard import _call_tool_inner

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _busy(_child(state, "chat-2", caller))
        self._wire(state, caller, monkeypatch)

        _call_tool_inner("session_broadcast", {"message": "rebase", "mode": "queue"})
        out = _call_tool_inner("session_status", {})

        assert "chat-2" in out
        # Busy with a message waiting behind the running turn.
        assert "working" in out and "1 queued" in out


class TestBlockingFindingRegressions:
    def test_a_resolved_title_is_frozen_to_its_slot_key_before_delivery(self, tmp_path):
        """A title moving between audience resolution and delivery cannot redirect it."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        intended = _busy(_child(state, "chat-2", caller))
        replacement = _busy(_child(state, "chat-3", caller))
        intended.title = "Moving target"
        replacement.title = "Other target"
        real_send = sc.send_to_target

        async def _move_title_then_send(state_, **kwargs):
            intended.title = "Former target"
            replacement.title = "Moving target"
            return await real_send(state_, **kwargs)

        with patch.object(sc, "send_to_target", new=_move_title_then_send):
            out = _broadcast(
                state,
                caller,
                message="stay with the resolved session",
                mode="queue",
                targets=["Moving target"],
            )

        assert out["results"][0]["target"] == intended.key
        assert any(
            str(entry.get("content", "")).endswith("stay with the resolved session")
            for entry in intended._queue
        )
        assert replacement._queue == []

    @pytest.mark.parametrize(
        ("matching_slots", "expected_code"),
        [(0, "target_not_found"), (2, "ambiguous_target")],
        ids=["unresolved", "ambiguous"],
    )
    def test_a_name_that_does_not_resolve_uniquely_is_retained_for_its_refusal(
        self, tmp_path, matching_slots, expected_code
    ):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for index in range(matching_slots):
            candidate = _child(state, f"chat-{index + 2}", caller)
            candidate.title = "Shared title"

        out = _broadcast(state, caller, targets=["Shared title"])

        assert out["results"][0]["target"] == "Shared title"
        assert out["results"][0]["code"] == expected_code

    def test_a_consumed_steer_never_advises_resending(self, tmp_path):
        """A returned steer call remains unsafe after target state is cleared."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        target = _child(state, "chat-2", caller)
        never = asyncio.Event()

        async def _consume_then_hang(state_, **kwargs):
            progress = kwargs["_delivery_progress"]
            progress.steer_await_entered = True
            progress.handover_returned = True
            await never.wait()

        with patch.object(sc, "BROADCAST_TARGET_ALLOWANCE_SECS", 0.05):
            with patch.object(sc, "send_to_target", new=_consume_then_hang):
                out = _broadcast(
                    state,
                    caller,
                    message="run exactly once",
                    mode="steer",
                )

        error = out["results"][0]["error"]
        assert "UNKNOWN" in error
        assert "Re-sending the same text is safe" not in error
        assert target._pending_steers == []
        assert target._queue == []
        assert target._steer_delivery_ids == {}

    def test_the_timeout_classifier_reads_delivery_progress_and_retention_containers(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(sc._timeout_delivery_observation)))
        slot_reads = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "slot"
        }
        progress_reads = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "delivery_progress"
        }
        assert {"_pending_steers", "_queue"} <= slot_reads
        assert "_steer_delivery_ids" not in slot_reads
        assert {"steer_await_entered", "handover_returned"} <= progress_reads
