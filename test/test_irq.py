"""Contract tests for the watch kernel and the GitHub-PR probe on top of it.

The kernel's whole value is that a poller no longer hand-rolls dedupe, epoch
resets, an error backstop or a convergence window -- so these tests pin the
behaviours that are easy to regress into something which still LOOKS like
success: a dedupe that goes permanently silent, a window that never fires, a
blind probe that skips quietly forever.

The coalescing-window cases encode a measured defect. On a repository whose ~65
checks finish over about twenty minutes, the previous fire-on-first-anomaly
logic woke the operator twice on ONE head -- once for a 34-second body gate
and again for a 4m55s reviewer lane -- while 24 checks were still pending.
``test_coalescing_folds_staggered_reds_into_one_wake`` is that scenario.
"""

from __future__ import annotations

import json
import time
import types

import pytest

from kiro_crew.cron_script import Done, Report, Skip
from kiro_crew.irq import (
    Observation,
    Outcome,
    Probe,
    Severity,
    Tick,
)
from kiro_crew.irq import _dedupe_key as dedupe_key
from kiro_crew.irq import (
    load_state,
)
from kiro_crew.irq import poll as irq_poll
from kiro_crew.irq import (
    run,
    sanitize_label,
    state_path,
)

#: Grace floor used by tests that need a window to close. Paired with
#: :func:`_settle`, which steps the fake clock past it.
_COALESCE = 0.01


class _FakeClock:
    """A wall clock that moves only when a test advances it.

    Installed over the ``time`` name that :mod:`kiro_crew.irq` reads (see
    :func:`_deterministic_clock`), so every interval the kernel measures is
    exact by construction. The assertions this protects are the ones that
    need an interval to stay SHORT -- two back-to-back ``_verdict()`` calls
    asserting a floor has NOT closed yet had only ``_COALESCE`` (10 ms) of
    real wall clock between them, which a loaded CI runner can overshoot.
    With a clock that nothing but an explicit :meth:`advance` moves, no
    scheduling stall can age a window between two calls.
    """

    def __init__(self, start: float) -> None:
        self._now = start

    def time(self) -> float:
        return self._now

    def advance(self, secs: float) -> None:
        self._now += secs

    def reset(self, start: float) -> None:
        self._now = start

    def __getattr__(self, name: str) -> object:
        raise AttributeError(
            f"_FakeClock does not fake time.{name}; kiro_crew.irq grew a clock "
            "read beyond time.time(). Cover it here (see _deterministic_clock)."
        )


#: The clock every test in this module runs on; re-seeded per test by the
#: autouse :func:`_deterministic_clock` fixture. Nothing outside a test body
#: may read or cache it -- the per-test reset is what keeps tests independent.
_clock = _FakeClock(0.0)


def _settle() -> None:
    """Advance the clock past ``_COALESCE`` so an OPEN window may now fire.

    A window cannot open and fire within one tick -- ``elapsed`` is zero at
    the moment it opens -- so every coalesced wake costs at least one extra
    tick. See ``test_coalesced_wake_always_costs_an_extra_tick``.
    """
    _clock.advance(_COALESCE * 3)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the kernel's state directory at a private tmp home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _deterministic_clock(monkeypatch):
    """Give ``kiro_crew.irq`` a clock that only this module can move.

    The patch targets the module attribute ``kiro_crew.irq.time`` -- the name
    the kernel's ``time.time()`` reads resolve -- never the stdlib module
    object, so the fake is scoped to the kernel and nothing else in the
    process sees it. Seeding from the real clock keeps the absolute values
    plausible; determinism comes from the clock being frozen BETWEEN explicit
    advances, not from the seed.
    """
    _clock.reset(time.time())
    monkeypatch.setattr("kiro_crew.irq.time", _clock)
    return _clock


def _ctx(message: str = "{}", job_id: str = "job-1") -> types.SimpleNamespace:
    return types.SimpleNamespace(job=types.SimpleNamespace(id=job_id), message=message)


class ScriptedProbe(Probe):
    """A probe that replays a list of pre-built ticks, one per run()."""

    def __init__(
        self, ticks: list[Tick], subject: str = "sub-1", coalesce_secs: float | None = None
    ) -> None:
        self._ticks = list(ticks)
        self._subject = subject
        self._coalesce_secs = coalesce_secs
        self.identity_calls = 0

    def tuning(self) -> dict[str, float]:
        # Declared through the probe's own hook, which is how a real probe asks
        # for a floor. poll() forwards no bounds of its own.
        if self._coalesce_secs is None:
            return {}
        return {"coalesce_secs": self._coalesce_secs}

    def identity(self, ctx: object) -> tuple[str, str]:
        self.identity_calls += 1
        return ("test-kind", self._subject)

    def observe(self, ctx: object) -> Tick:
        return self._ticks.pop(0)


def _verdict(probe: Probe, ctx=None, **kwargs):
    """Run one tick and return the raised verdict exception."""
    try:
        run(ctx or _ctx(), probe, **kwargs)
    except (Skip, Report, Done) as exc:
        return exc
    raise AssertionError("run() returned without raising a verdict")


def _wake(observation_key: str, brief: str = "brief") -> Observation:
    return Observation(observation_key, Severity.WAKE, brief)


# ---------------------------------------------------------------- identity


def test_identity_value_error_becomes_done():
    """A permanently-malformed config must remove the job, not crash-loop."""

    class BadProbe(Probe):
        def identity(self, ctx):
            raise ValueError("message must be a JSON object")

        def observe(self, ctx):  # pragma: no cover -- never reached
            raise AssertionError("observe must not run when identity fails")

    verdict = _verdict(BadProbe())
    assert isinstance(verdict, Done)
    assert "message must be a JSON object" in str(verdict)


def test_identity_called_exactly_once_per_tick():
    """identity() parses the cron message; calling it twice would double-parse
    and, worse, put one call outside the ValueError-to-Done conversion."""
    probe = ScriptedProbe([Tick(epoch="e1", pending=1)])
    _verdict(probe)
    assert probe.identity_calls == 1


def test_state_path_separates_two_jobs_on_one_subject():
    """Two sessions babysitting one subject must not share a dedupe memory --
    one watch's dedupe suppressing the other's delivery is a lost signal."""
    a = state_path("gh-pr", "owner/name#1", "job-a")
    b = state_path("gh-pr", "owner/name#1", "job-b")
    assert a != b


def test_state_path_does_not_collide_on_fold_equivalent_subjects():
    """The digest covers the exact subject id, so two ids that fold to the
    same filesystem-safe characters still get separate files."""
    a = state_path("gh-pr", "own.er/name#1", "job")
    b = state_path("gh-pr", "own-er/name#1", "job")
    assert a != b


# ------------------------------------------------------------ error backstop


def test_blind_probe_reports_at_threshold_not_only_at_equality():
    """Fire at >= threshold. An exact-equality gate turns ONE lost delivery
    into permanent silence: the persisted count passes the threshold and never
    equals it again."""
    probe = ScriptedProbe([Tick(fetch_ok=False)])
    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"errors": 11}), encoding="utf-8")

    verdict = _verdict(probe, max_consecutive_errors=6)
    assert isinstance(verdict, Report)
    assert "12 consecutive" in str(verdict)


def test_blind_alert_dedupes_then_skips_quietly():
    probe = ScriptedProbe([Tick(fetch_ok=False), Tick(fetch_ok=False)])
    first = _verdict(probe, max_consecutive_errors=1)
    assert isinstance(first, Report)
    second = _verdict(probe, max_consecutive_errors=1)
    assert isinstance(second, Skip)


def test_recovered_streak_clears_blind_marker():
    """A recovered streak must not leave the next streak inheriting hours of
    dedupe from the previous one."""
    probe = ScriptedProbe([Tick(fetch_ok=False), Tick(epoch="e1", pending=1), Tick(fetch_ok=False)])
    assert isinstance(_verdict(probe, max_consecutive_errors=1), Report)
    assert isinstance(_verdict(probe, max_consecutive_errors=1), Skip)
    third = _verdict(probe, max_consecutive_errors=1)
    assert isinstance(third, Report), "second streak must alert promptly again"

    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert state["errors"] == 1


def test_observations_ignored_when_fetch_failed():
    """A failed observation must not be read as 'nothing is wrong'."""
    probe = ScriptedProbe([Tick(observations=[_wake("red:x")], fetch_ok=False)])
    verdict = _verdict(probe, max_consecutive_errors=6)
    assert isinstance(verdict, Skip)


# ------------------------------------------------------------------ terminal


def test_terminal_observation_raises_done():
    probe = ScriptedProbe(
        [Tick(epoch="e1", observations=[Observation("merged", Severity.TERMINAL, "MERGED")])]
    )
    verdict = _verdict(probe)
    assert isinstance(verdict, Done)
    assert "MERGED" in str(verdict)


def test_terminal_wins_over_an_open_window():
    """The subject is gone; there is nothing left to converge toward."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=3),
            Tick(
                epoch="e1",
                observations=[Observation("closed", Severity.TERMINAL, "CLOSED")],
                pending=3,
            ),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=999), Skip)
    assert isinstance(_verdict(probe, coalesce_secs=999), Done)


# -------------------------------------------------------------------- exempt


def test_nmi_bypasses_the_coalescing_window():
    """A conflict dispatches no checks, so pending never drains and waiting
    observes nothing -- it must fire immediately even mid-window."""
    probe = ScriptedProbe(
        [
            Tick(
                epoch="e1",
                observations=[Observation("conflict", Severity.NMI, "CONFLICTING")],
                pending=9,
            )
        ]
    )
    verdict = _verdict(probe, coalesce_secs=999, coalesce_max_secs=9999)
    assert isinstance(verdict, Report)
    assert "CONFLICTING" in str(verdict)


# ----------------------------------------------------------------- coalescing


def test_window_holds_the_first_anomaly():
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=24)])
    verdict = _verdict(probe, coalesce_secs=999)
    assert isinstance(verdict, Skip)
    assert "coalescing window open" in str(verdict)


def test_coalesced_wake_always_costs_an_extra_tick():
    """A window cannot open and fire in the same tick: ``elapsed`` is zero at
    the moment it opens. So enabling coalescing buys coalescing at the price of at
    least one cron interval of latency -- on a 60s cron, at least 60s. This is
    the central trade of the feature and must not regress silently."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
        ]
    )
    first = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(first, Skip), "the opening tick can never fire"
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Report)


def test_coalescing_folds_staggered_reds_into_one_wake():
    """The measured defect: one head, two reds arriving minutes apart, 24
    checks still pending. Old behaviour was two wakes; this is one."""
    probe = ScriptedProbe(
        [
            # t=34s: fast body gate flips red, rest still running
            Tick(epoch="e1", observations=[_wake("red:Screenshot Evidence", "sshot")], pending=24),
            # t=4m55s: reviewer lane flips red, still running
            Tick(
                epoch="e1",
                observations=[
                    _wake("red:Screenshot Evidence", "sshot"),
                    _wake("red:GPT 5.6 Review", "gpt"),
                ],
                pending=12,
            ),
            # converged: both reds still there, nothing pending
            Tick(
                epoch="e1",
                observations=[
                    _wake("red:Screenshot Evidence", "sshot"),
                    _wake("red:GPT 5.6 Review", "gpt"),
                ],
                pending=0,
            ),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    final = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(final, Report)
    body = str(final)
    assert "sshot" in body and "gpt" in body, "both reds must arrive in ONE wake"


def test_coalesce_secs_zero_restores_fire_on_first_anomaly():
    """The migration setting: a poller ports onto the kernel with the window
    off, so wake timing is unchanged and the flip is a separate step."""
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=24)])
    verdict = _verdict(probe, coalesce_secs=0)
    assert isinstance(verdict, Report)


def test_floor_blocks_a_premature_converged_wake():
    """Right after an epoch change the rollup can be nearly empty, so
    pending==0 is briefly true while nothing has run. Firing then reports a
    convergence that never happened."""
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=0)])
    verdict = _verdict(probe, coalesce_secs=999)
    assert isinstance(verdict, Skip), "the floor must outrank pending==0"


def test_hard_cap_fires_when_pending_never_drains():
    """A wedged queued check must cost a delayed wake, never a lost one."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=5),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=5),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=999, coalesce_max_secs=9999), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=_COALESCE, coalesce_max_secs=_COALESCE)
    assert isinstance(verdict, Report)


def test_hard_cap_outranks_a_floor_set_above_it():
    """The cap is an absolute wall and must not be gated behind the floor. With
    a floor above the cap -- legal, both finite and positive -- and a pending
    count that never drains, gating the cap behind the floor voided the only
    guarantee the window makes: delayed, never dropped."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=5),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=5),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=999, coalesce_max_secs=_COALESCE), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=999, coalesce_max_secs=_COALESCE)
    assert isinstance(verdict, Report), "the cap must fire even below the floor"


def test_the_hard_cap_flushes_the_WHOLE_window_not_only_the_capped_entry():
    """The cap stays a WINDOW-level wall even though the floor is per entry.

    Making the cap per entry as well was tried and reverted here: for a subject
    whose `pending` never drains, withholding an unconverged neighbour from a cap
    wake turns ONE flush into one wake per entry, spaced by their arrival
    spacing. That is the per-wake cost the window exists to remove, and it is the
    opposite of the payload rule -- riding along cannot add a wake, withholding
    guarantees one. The floor is what a joining entry must serve; the cap is the
    promise that a wedged queue costs a delay and not a wake per signal.
    """
    first = _wake("red:a", "first red")
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[first], pending=5),
            # A second red arrives long after the first opened, so on a per-entry
            # cap it would be nowhere near its own wall.
            Tick(epoch="e1", observations=[first, _wake("red:b", "second red")], pending=5),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=999, coalesce_max_secs=9999), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=999, coalesce_max_secs=_COALESCE)
    assert isinstance(verdict, Report)
    body = str(verdict)
    assert "first red" in body
    assert "second red" in body, "the cap flushes the window, not just what aged out"


def test_an_entry_left_by_a_partial_fire_still_reaches_the_cap():
    """The cap's promise is a bounded delay, and a partial fire must not extend it.

    The window's age is now the oldest SURVIVING entry's age, so a partial fire
    that delivers the entry which opened the window moves the cap clock onto the
    survivor. That is a bound, not a reset: an entry is flushed no later than the
    cap measured from its own arrival, which is what "delayed, never dropped"
    means per entry. Seeded from disk rather than slept for, so the bound is
    asserted rather than raced.
    """
    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = dedupe_key(_wake("red:a"))
    path.write_text(
        json.dumps(
            {
                "epoch": "e1",
                # What a partial fire leaves: the sticky half already delivered,
                # one epoch-scoped survivor carrying the age it earned.
                "coalescing": {key: {"brief": "a red", "opened_at": _clock.time() - 600}},
            }
        ),
        encoding="utf-8",
    )
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a", "a red")], pending=4)])
    # The floor is unreachable, so only the cap can produce this wake.
    verdict = _verdict(probe, coalesce_secs=9999, coalesce_max_secs=300)
    assert isinstance(verdict, Report), "the survivor's own cap must still fire"
    assert "a red" in str(verdict)


def test_window_state_survives_across_ticks():
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=3)])
    _verdict(probe, coalesce_secs=999)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    # Asserted through the kernel's own key helper rather than a literal: the
    # test's subject is that the window PERSISTED, not how a key is spelled.
    key = dedupe_key(_wake("red:a"))
    assert list(state["coalescing"]) == [key]
    # The age lives on the ENTRY. There is no window-level stamp to assert: one
    # scalar could not serve entries admitted at different times, which is the
    # defect `test_an_entry_joining_after_a_partial_fire_serves_its_own_floor`
    # pins.
    assert state["coalescing"][key]["opened_at"] > 0


def test_window_cleared_after_it_fires():
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Report)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert not state.get("coalescing")
    assert "coalesce_started_at" not in state
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)


# ---------------------------------------------------------------- epoch reset


def test_epoch_change_wipes_dedupe_and_drops_the_open_window():
    """A force-push means the anomalies in flight described a commit that no
    longer exists."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e2", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e2", observations=[_wake("red:a")], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Report)
    # Same key on a NEW epoch must wake again rather than read as deduped.
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Report)


def test_same_key_same_epoch_is_deduped():
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Report)
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)


def test_dedupe_rearms_after_the_realert_window():
    """Dedupe is a bounded delay, not a permanent acknowledgement: the script
    cannot observe delivery, so a permanent marker would turn one lost
    delivery into a permanently suppressed signal."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE, realert_secs=3600), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE, realert_secs=3600), Report)
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE, realert_secs=3600), Skip)
    # A stale marker re-arms the key, which OPENS a fresh window -- so the
    # re-alert also pays the one-extra-tick cost of the coalescing floor.
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE, realert_secs=0), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=_COALESCE, realert_secs=0)
    assert isinstance(verdict, Report), "a stale marker must re-arm"


def test_future_timestamp_reads_as_stale_not_as_forever_suppression():
    """Clock rollback or corrupt state must not silence a watch indefinitely."""
    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"epoch": "e1", "alerted": {"red:a": 2**40}}),
        encoding="utf-8",
    )
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Report)


# ------------------------------------------------------------------- pruning


def test_cleared_anomaly_is_pruned_from_an_open_window():
    """An anomaly that resolves while the window is open must not be reported.

    Without pruning it would be announced as failing in the same brief as the
    observation that replaced it -- a wake that contradicts itself, with no way
    for the operator to tell which half is current.
    """
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a", "A failed")], pending=3),
            # red:a reran green; only red:b is still observed this tick
            Tick(epoch="e1", observations=[_wake("red:b", "B failed")], pending=0),
            Tick(epoch="e1", observations=[_wake("red:b", "B failed")], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    # red:b joined on this tick, so it serves a floor of its OWN rather than
    # inheriting the age of the window red:a opened. It is the wake below that
    # must not carry red:a, and the extra tick is what red:b's own floor costs.
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(verdict, Report)
    body = str(verdict)
    assert "B failed" in body
    assert "A failed" not in body, "a cleared anomaly must not ride along"


def test_window_closes_when_everything_in_flight_clears():
    """All entries pruned must also drop the start stamp, or the next anomaly
    inherits this window's age and fires with no settling time of its own."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=3),
            Tick(epoch="e1", observations=[], pending=3),
            Tick(epoch="e1", observations=[_wake("red:b")], pending=3),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert not state.get("coalescing")
    assert "coalesce_started_at" not in state
    # A new anomaly must open a FRESH window rather than fire on the old age.
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)


def test_unwritable_state_delivers_instead_of_swallowing_the_window():
    """A window has to REMEMBER when it opened, so an unwritable state directory
    does not merely degrade coalescing -- it destroys it: every subprocess
    reloads an empty window, elapsed is always zero, and the fire condition can
    never be reached. The wake would be lost, not delayed. Deliver now instead,
    carrying the warning. Without coalescing this hazard did not exist, so it
    arrived with the window."""
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a", "A")], pending=9)])
    import kiro_crew.irq as irq_mod

    original = irq_mod.save_state
    try:
        irq_mod.save_state = lambda *a, **k: False  # type: ignore[assignment]
        verdict = _verdict(probe, coalesce_secs=999)
    finally:
        irq_mod.save_state = original  # type: ignore[assignment]
    assert isinstance(verdict, Report), "an unrememberable window must fire, not wait"
    assert "unwritable" in str(verdict)


def test_non_finite_bounds_fall_back_to_defaults_instead_of_crashing():
    """json.loads turns 1e309 into inf. A non-finite bound reaches int() in the
    Skip message and raises OverflowError on every tick, and a cron that raises
    every tick is auto-paused -- the watch dies silently from one bad number.
    Defended in the kernel as well as the probe, because the value can arrive
    from a run() argument, a probe attribute, or a cron message."""
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=3)])
    verdict = _verdict(probe, coalesce_secs=float("inf"), coalesce_max_secs=float("nan"))
    assert isinstance(verdict, Skip), "must degrade to the default window, not crash"


def test_partially_persisted_streak_still_alerts():
    """A streak that persisted 1 and THEN lost its directory would otherwise be
    permanently silent: every later tick reloads 1, reaches 2, and is neither
    == 1 nor >= the threshold. Repeats are the right degradation, because dedupe
    needs the same storage that just failed."""
    import kiro_crew.irq as irq_mod

    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"errors": 1}), encoding="utf-8")

    probe = ScriptedProbe([Tick(fetch_ok=False)])
    original = irq_mod.save_state
    try:
        irq_mod.save_state = lambda *a, **k: False  # type: ignore[assignment]
        verdict = _verdict(probe, max_consecutive_errors=6)
    finally:
        irq_mod.save_state = original  # type: ignore[assignment]
    assert isinstance(verdict, Report)
    assert "unwritable" in str(verdict)


def test_probe_tuning_overrides_a_bound():
    class TunedProbe(ScriptedProbe):
        def tuning(self):
            return {"coalesce_secs": 0}

    probe = TunedProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=9)])
    verdict = _verdict(probe, coalesce_secs=999)
    assert isinstance(verdict, Report), "tuning() must beat the call's argument"


def test_probe_tuning_cannot_hand_the_kernel_a_fatal_bound():
    """tuning() is probe-authored and reaches the same int() formatting, so the
    ONE recognized key is validated like every other source rather than trusted,
    and an unrecognized key is ignored rather than crashed on.

    Both halves are asserted because the narrowing to a single key silently
    turned the second half into a no-op the first time it was written.
    """

    class HostileProbe(ScriptedProbe):
        def tuning(self):
            # coalesce_secs is recognized -> must be refused down to the default.
            # realert_secs has no producer, so it is NOT recognized -> ignored,
            # which must not become a crash either.
            return {"coalesce_secs": 10**400, "realert_secs": float("nan")}

    probe = HostileProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=3)])
    assert isinstance(_verdict(probe), Skip)

    # The unrecognized key really is ignored, not quietly applied: a NaN
    # realert_secs would poison every dedupe comparison, so a wake that fires
    # normally on the next converged tick proves it never reached the bounds.
    # This probe returns ONLY the unrecognized key, so coalesce_secs still comes
    # from the call's argument -- a recognized override would replace it (and be
    # refused down to the 240s default, which is what the first half asserts).
    class UnknownKeyProbe(ScriptedProbe):
        def tuning(self):
            return {"realert_secs": float("nan")}

    probe2 = UnknownKeyProbe(
        [
            Tick(epoch="e2", observations=[_wake("red:b")], pending=0),
            Tick(epoch="e2", observations=[_wake("red:b")], pending=0),
        ]
    )
    assert isinstance(_verdict(probe2, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe2, coalesce_secs=_COALESCE), Report)


def test_probe_tuning_raising_does_not_kill_the_tick():
    class BrokenTuning(ScriptedProbe):
        def tuning(self):
            raise RuntimeError("boom")

    probe = BrokenTuning([Tick(epoch="e1", observations=[], pending=1)])
    assert isinstance(_verdict(probe), Skip)


# ------------------------------------------------------- malformed state load


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[]",
        '{"alerted": "nope", "errors": -3, "epoch": 5}',
        '{"alerted": {"k": NaN}}',
        '{"alerted": {"k": Infinity}}',
        '{"coalescing": {"k": 7}, "coalesce_started_at": "soon"}',
    ],
)
def test_malformed_state_degrades_to_fresh(tmp_path, payload):
    """One duplicate wake is the correct cost. A crash loop would auto-pause
    the cron and take the watch down entirely."""
    path = tmp_path / "watch" / "test-kind" / "x.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    state = load_state(path)
    assert isinstance(state, dict)
    assert "nope" not in str(state.get("alerted", {}))
    assert state.get("errors", 0) >= 0


def test_sanitize_label_strips_control_and_markup():
    assert sanitize_label("ok name (1)") == "ok name (1)"
    assert "\n" not in sanitize_label("bad\nname")
    assert sanitize_label(None) == ""
    assert len(sanitize_label("x" * 500)) == 120


# ------------------------------------------------- epoch-independent keys
#
# Some signals a probe reports through the same tick are not properties of the
# epoch. A comment on a pull request belongs to the conversation, not to the
# commit under review, and does not stop having happened because the head moved.
# These keys therefore survive the reset that wipes everything else.


def _sticky(observation_key: str, brief: str = "brief") -> Observation:
    return Observation(observation_key, Severity.WAKE, brief, epoch_scoped=False)


def test_a_sticky_key_survives_an_epoch_change():
    """The load-bearing invariant. Without it, one force-push replays every
    epoch-independent signal the watch has ever reported."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_sticky("comment:1")]),
            Tick(epoch="e2", observations=[_sticky("comment:1")]),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=0), Report)
    assert isinstance(_verdict(probe, coalesce_secs=0), Skip)


def test_an_epoch_scoped_key_is_still_wiped_by_an_epoch_change():
    """The carry-over filter must keep exactly one half. A filter that kept
    everything would silently disable the epoch reset itself."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")]),
            Tick(epoch="e2", observations=[_wake("red:a")]),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=0), Report)
    assert isinstance(_verdict(probe, coalesce_secs=0), Report)


def test_the_two_key_spaces_do_not_collide():
    """Same probe key, different scoping: two independent signals, so the second
    must not be suppressed by the first's dedupe entry."""
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("dup"), _sticky("dup")])])
    verdict = _verdict(probe, coalesce_secs=0)
    assert isinstance(verdict, Report)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert len(state["alerted"]) == 2


def test_a_sticky_key_is_dropped_once_past_the_realert_window():
    """Epoch-scoped keys are bounded by the reset that wipes them; sticky keys
    have no such bound, so a long-lived watch would grow its state forever.
    Dropping them past the re-alert window frees state without changing any
    decision -- they no longer suppress anything at that age."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_sticky("comment:1")]),
            Tick(epoch="e1", observations=[]),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=0, realert_secs=0.01), Report)
    # Crosses the 0.01s re-alert window -- an irq-measured interval, so it is a
    # clock advance, not a real sleep.
    _clock.advance(0.05)
    assert isinstance(_verdict(probe, coalesce_secs=0, realert_secs=0.01), Skip)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert not [k for k in state.get("alerted", {}) if "comment:1" in k]


def test_the_blind_marker_is_not_carried_across_an_epoch_change():
    """It records that the probe could not observe AT ALL, which is a judgement
    a fresh epoch deserves to make again rather than inherit."""

    class FlakyProbe(Probe):
        def __init__(self) -> None:
            self.ticks = [
                Tick(fetch_ok=False),
                Tick(epoch="e2", observations=[]),
            ]

        def identity(self, ctx):
            return ("test-kind", "sub-1")

        def observe(self, ctx):
            return self.ticks.pop(0)

    probe = FlakyProbe()
    _verdict(probe, coalesce_secs=0, max_consecutive_errors=1)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert "blind" in state.get("alerted", {})
    _verdict(probe, coalesce_secs=0, max_consecutive_errors=1)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert "blind" not in state.get("alerted", {})


def test_state_written_before_the_sentinels_existed_costs_no_extra_wake():
    """An upgrade must not re-report anomalies the previous version already
    delivered. A bare key is adopted as epoch scoped, which is what every
    pre-sentinel key was."""
    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"epoch": "e1", "alerted": {"red:a": _clock.time()}}), encoding="utf-8"
    )
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a")])])
    assert isinstance(_verdict(probe, coalesce_secs=0), Skip)


def test_an_open_sticky_wake_is_not_pruned_when_the_probe_stops_reporting_it():
    """The prune assumes "no longer observed" means "cleared", which is true of a
    check and false of a comment: a probe with a horizon stops reporting a signal
    that is still just as true. Pruning on that destroys the wake instead of
    delaying it -- reachable on shipped defaults, because a signal first seen
    minutes before its horizon expires ages out mid-window."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_sticky("comment:1")], pending=0),
            # Aged past the probe's horizon: gone from the tick, still true.
            Tick(epoch="e1", observations=[], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(verdict, Report)
    assert "brief" in str(verdict)


def test_an_open_epoch_scoped_wake_is_still_pruned_when_it_clears():
    """The other half of the same asymmetry: a check that reran green must not be
    announced as failing, so the prune has to keep applying to epoch-scoped
    entries. Exempting everything would reintroduce the self-contradicting wake.
    """
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e1", observations=[], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)


def test_an_open_epoch_scoped_window_is_dropped_by_an_epoch_change():
    """The complement: window entries describing checks on a commit that is no
    longer under review must still be discarded, or a force-push would announce
    the old head's reds against the new one."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a")], pending=0),
            Tick(epoch="e2", observations=[], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)


def test_a_fresh_epoch_anomaly_still_gets_a_full_settling_floor():
    """The floor exists because a freshly pushed commit briefly shows an
    almost-empty rollup, so `pending == 0` can be true while nothing has run.
    Carrying the OLD window's start stamp onto the new epoch left that floor
    already satisfied, so a `ready` observation on the new head fired at once and
    announced all-checks-green before its checks existed. One stamp cannot serve
    an entry that has waited and one that has not.
    """
    probe = ScriptedProbe(
        [
            # A comment lands while checks run, so the window opens and cannot
            # converge (pending > 0).
            Tick(epoch="e1", observations=[_sticky("comment:1")], pending=3),
            # Force-push. The fresh head momentarily looks converged.
            Tick(
                epoch="e2",
                observations=[_wake("ready", "all checks green")],
                pending=0,
            ),
            Tick(
                epoch="e2",
                observations=[_wake("ready", "all checks green")],
                pending=0,
            ),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()  # the OLD window is now older than the floor
    carried = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(carried, Report)
    assert "brief" in str(carried)
    assert "green" not in str(carried)
    _settle()
    fresh = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(fresh, Report)
    assert "green" in str(fresh)


def test_a_fresh_epoch_anomaly_does_not_ride_on_a_carried_entrys_hard_cap():
    """The cap belongs to the old window, not to a fresh-head observation.

    A carried sticky entry can already be older than the cap on the transition
    tick. The new observation must still serve its own floor before it can ride
    on any wake, including the cap flush.
    """
    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = dedupe_key(_sticky("comment:1", "said"))
    path.write_text(
        json.dumps(
            {
                "epoch": "e1",
                "coalescing": {key: {"brief": "said", "opened_at": time.time() - 600}},
            }
        ),
        encoding="utf-8",
    )
    probe = ScriptedProbe(
        [Tick(epoch="e2", observations=[_wake("ready", "all checks green")], pending=0)]
    )
    verdict = _verdict(probe, coalesce_secs=300, coalesce_max_secs=30)
    assert isinstance(verdict, Report)
    assert "said" in str(verdict)
    assert "green" not in str(verdict)


def test_an_unstamped_carried_sticky_entry_restarts_instead_of_disappearing():
    """Recoverable persisted rows survive an epoch reset.

    Normalization owns repairing a missing timestamp. Filtering the row before
    that repair turns a recoverable delay into a permanent lost wake.
    """
    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = dedupe_key(_sticky("comment:1", "said"))
    path.write_text(
        json.dumps({"epoch": "e1", "coalescing": {key: {"brief": "said"}}}),
        encoding="utf-8",
    )
    probe = ScriptedProbe([Tick(epoch="e2", observations=[], pending=0)])
    assert isinstance(_verdict(probe, coalesce_secs=300), Skip)
    state = load_state(path)
    assert state["coalescing"][key]["brief"] == "said"
    assert state["coalescing"][key]["opened_at"] > 0


def test_a_carried_sticky_entry_keeps_its_served_floor_across_epoch_change():
    """`alerted` stops a DELIVERED signal repeating; an open `coalescing` entry
    holds one that has NOT been delivered. Dropping the window at the epoch reset
    destroys that wake outright, because the probe may legitimately stop
    reporting the observation (a comment aged past its horizon) so nothing puts
    it back -- reachable on shipped defaults by observing a near-horizon comment
    then pushing a fix.

    Carrying the entry with its own open time preserves the settling floor it
    already served before the push. The probe reports nothing on the new-epoch
    tick, but the carried entry must still fire without paying the same floor a
    second time."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_sticky("comment:1")], pending=3),
            Tick(epoch="e2", observations=[], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(verdict, Report)
    assert "brief" in str(verdict)


# ------------------------------------------------- per-wake footer, not per brief


class FootedProbe(ScriptedProbe):
    """A probe that supplies a per-wake footer, and can be made to raise in it."""

    def __init__(self, ticks: list[Tick], suffix: object = "FOOTER", subject: str = "sub-1"):
        super().__init__(ticks, subject=subject)
        self._suffix = suffix

    def wake_suffix(self):
        if isinstance(self._suffix, Exception):
            raise self._suffix
        return self._suffix


def test_the_wake_footer_is_emitted_once_not_once_per_observation():
    """Standing instructions describe the WAKE, so N coalesced observations must
    not pay N copies of them.

    This is the regression that made coalescing cost what it saved: on a measured
    six-observation wake the per-observation form was 56% of the delivered bytes,
    and the ratio got WORSE as coalescing got better -- every extra signal folded
    into one wake added another copy of the same paragraph.
    """
    ticks = [
        Tick(epoch="e1", observations=[_wake("red:a", "first"), _wake("red:b", "second")]),
        Tick(epoch="e1", observations=[_wake("red:a", "first"), _wake("red:b", "second")]),
    ]
    probe = FootedProbe(ticks)
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    verdict = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(verdict, Report)
    body = str(verdict)
    # Both signals survive...
    assert "first" in body and "second" in body
    # ...and the footer is paid once for the wake, not once per signal.
    assert body.count("FOOTER") == 1


def test_a_single_observation_wake_still_carries_the_footer():
    """The footer moved from the brief to the kernel, so every delivery path has
    to apply it -- an NMI fires one observation without ever passing through the
    coalescing join, and would silently lose its instructions."""
    probe = FootedProbe(
        [Tick(epoch="e1", observations=[Observation("conflict", Severity.NMI, "dirty")])]
    )
    verdict = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(verdict, Report)
    assert "dirty" in str(verdict)
    assert str(verdict).count("FOOTER") == 1


def test_a_footer_that_raises_costs_the_footer_not_the_tick():
    """A missing footer is recoverable; a watch that raises every tick is
    auto-paused, which loses the watch itself."""
    probe = FootedProbe(
        [Tick(epoch="e1", observations=[Observation("conflict", Severity.NMI, "dirty")])],
        suffix=RuntimeError("probe is broken"),
    )
    verdict = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(verdict, Report)
    assert "dirty" in str(verdict)
    assert "probe is broken" not in str(verdict)


# --------------------------------- pending gates checks, never the conversation


def test_a_sticky_wake_fires_at_the_floor_while_checks_are_still_pending():
    """``pending`` counts CHECKS. A comment is complete the moment it is posted,
    so holding it until an unrelated check drains buys no observation and costs
    up to the hard cap -- measured at the full 30 minutes on a real pull request
    with 18 checks in flight."""
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_sticky("comment:1")], pending=7),
            Tick(epoch="e1", observations=[_sticky("comment:1")], pending=7),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Report)


def test_the_sticky_half_fires_while_the_epoch_scoped_half_keeps_waiting():
    """The two populations fire on their OWN readiness, and both halves of that
    matter.

    Firing the whole window when a sticky signal is ready would announce an
    epoch-scoped `ready` while checks were still draining -- the
    convergence-that-never-happened the floor exists to prevent. Holding the
    sticky signal until the checks drain is the defect above. So the wake is
    split, and the remainder keeps the ORIGINAL start stamp: it has been waiting
    since then, and restarting its clock on every partial fire would let a
    talkative pull request defer the check anomaly indefinitely.
    """
    conversation = _sticky("comment:1", "said")
    checks = _wake("ready", "green")
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[conversation, checks], pending=4),
            Tick(epoch="e1", observations=[conversation, checks], pending=4),
            Tick(epoch="e1", observations=[checks], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()

    partial = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(partial, Report)
    assert "said" in str(partial)
    assert "green" not in str(partial), "a converged-looking check must not ride out early"

    # No _settle() here on purpose: the carried entry kept its stamp, so the very
    # next converged tick delivers it rather than serving a fresh floor.
    rest = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(rest, Report)
    assert "green" in str(rest)


def test_a_partial_fire_defers_to_the_unwritable_state_fallback():
    """Withholding the epoch-scoped half is a DELAY only while the window can be
    remembered.

    With an unwritable state directory the next tick reloads an empty window, so
    the withheld half is LOST -- the exact hazard the persistence fallback exists
    to close, reintroduced for the half the split holds back. So the partial fire
    is gated on the write succeeding, and a failed write delivers EVERYTHING now.
    """
    conversation = _sticky("comment:1", "said")
    checks = _wake("ready", "green")
    probe = ScriptedProbe(
        [
            Tick(epoch="e1", observations=[conversation, checks], pending=4),
            Tick(epoch="e1", observations=[conversation, checks], pending=4),
        ]
    )
    import kiro_crew.irq as irq_mod

    # First tick writes normally so the window exists and can age.
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()

    original = irq_mod.save_state
    try:
        irq_mod.save_state = lambda *a, **k: False  # type: ignore[assignment]
        verdict = _verdict(probe, coalesce_secs=_COALESCE)
    finally:
        irq_mod.save_state = original  # type: ignore[assignment]

    assert isinstance(verdict, Report)
    body = str(verdict)
    assert "said" in body, "the sticky half must still be delivered"
    assert "green" in body, "the withheld half must NOT be held back into a lost window"
    assert "unwritable" in body, "the operator has to be told why repeats are coming"


# ------------------------------------------- one window, entries of different ages


def test_an_entry_joining_after_a_partial_fire_serves_its_own_floor():
    """A partial fire leaves the window open, and the next tick can add a signal
    that has not waited at all. One window-level start stamp handed that signal
    an age it never spent, so it was delivered on the very next tick with no
    settling window of its own -- and the signal arriving seconds behind it then
    needed a wake of its own too, which is the per-wake cost coalescing exists to
    remove.

    Both halves are pinned here: the joining entry does not ride out early, and
    it is DELAYED rather than dropped -- it arrives once its own floor closes, in
    ONE wake together with the signal that joined behind it.
    """
    conversation = _sticky("comment:1", "said")
    checks = _wake("red:a", "a red")
    probe = ScriptedProbe(
        [
            # A comment lands while checks run, so the window opens and cannot
            # converge.
            Tick(epoch="e1", observations=[conversation, checks], pending=4),
            # Past the floor: the sticky half fires and the check entry stays, so
            # the window -- and its one start stamp -- survives the partial fire.
            Tick(epoch="e1", observations=[conversation, checks], pending=4),
            # A second comment joins the aged window having waited nothing.
            Tick(epoch="e1", observations=[checks, _sticky("comment:2", "two")], pending=4),
            # And a third joins behind it, inside the second one's floor.
            Tick(
                epoch="e1",
                observations=[
                    checks,
                    _sticky("comment:2", "two"),
                    _sticky("comment:3", "three"),
                ],
                pending=4,
            ),
            Tick(
                epoch="e1",
                observations=[
                    checks,
                    _sticky("comment:2", "two"),
                    _sticky("comment:3", "three"),
                ],
                pending=4,
            ),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_COALESCE), Skip)
    _settle()

    partial = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(partial, Report)
    assert "said" in str(partial)

    # No _settle() on purpose: comment:2 has waited nothing.
    joined = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(joined, Skip), "a joining entry must not inherit the window's age"

    # Still no _settle(): comment:3 lands inside comment:2's floor, which is what
    # gives the two something to coalesce INTO.
    behind = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(behind, Skip)

    _settle()
    late = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(late, Report), "a held entry must be delayed, never dropped"
    body = str(late)
    assert "two" in body and "three" in body, "both joiners belong in ONE wake"


def test_a_window_written_before_per_entry_ages_keeps_the_age_it_had():
    """An upgrade must not restart the clock of a window already open on disk.

    State written before entries carried their own open time has one window-level
    stamp and bare brief strings. Reading that shape has to seed every entry from
    the old stamp, or an in-flight watch silently serves a second floor across the
    version change -- a delay the operator cannot see the reason for.
    """
    path = state_path("test-kind", "sub-1", "job-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = dedupe_key(_sticky("comment:1"))
    path.write_text(
        json.dumps(
            {
                "epoch": "e1",
                "coalescing": {key: "said"},
                "coalesce_started_at": _clock.time() - 600,
            }
        ),
        encoding="utf-8",
    )
    probe = ScriptedProbe(
        [Tick(epoch="e1", observations=[_sticky("comment:1", "said")], pending=4)]
    )
    verdict = _verdict(probe, coalesce_secs=_COALESCE)
    assert isinstance(verdict, Report), "the entry had already waited ten minutes"
    assert "said" in str(verdict)


# ------------------------------------------------------- driver front door (poll)


class _RaisingProbe(Probe):
    """A probe with a defect: it raises instead of reporting fetch_ok=False."""

    def identity(self, ctx: object) -> tuple[str, str]:
        return ("test-kind", "sub-raise")

    def observe(self, ctx: object) -> Tick:
        raise RuntimeError("probe defect")


class _ReturningKernelProbe(Probe):
    """Identity only; used with a patched run() that returns without raising."""

    def identity(self, ctx: object) -> tuple[str, str]:
        return ("test-kind", "sub-return")

    def observe(self, ctx: object) -> Tick:
        return Tick()


def test_poll_maps_a_quiet_tick_to_QUIET():
    probe = ScriptedProbe([Tick()])
    verdict = irq_poll("loop-1", "{}", probe)
    assert verdict.outcome is Outcome.QUIET


def test_poll_maps_a_wake_to_WAKE_and_carries_the_brief():
    probe = ScriptedProbe(
        [Tick(observations=[_wake("red:build", "build went red")])], coalesce_secs=0
    )
    verdict = irq_poll("loop-1", "{}", probe)
    assert verdict.outcome is Outcome.WAKE
    assert "build went red" in verdict.body


def test_poll_refuses_to_call_a_failed_fetch_quiet():
    """A probe that could not observe the subject has not shown it unchanged.

    The cron driver sleeps on every Skip alike, so this distinction never had to
    exist. A driver that decides whether to SPEND on the strength of a Skip does
    need it: mapping a failed fetch to QUIET is how an expired credential or a
    network outage becomes an indefinitely silent watch.
    """
    probe = ScriptedProbe([Tick(fetch_ok=False), Tick(fetch_ok=False)])
    first = irq_poll("loop-blind", "{}", probe)
    second = irq_poll("loop-blind", "{}", probe)
    assert Outcome.QUIET not in (first.outcome, second.outcome)
    assert Outcome.FALLBACK in (first.outcome, second.outcome)


def test_poll_carries_the_terminal_keys_for_classification():
    """A driver recording a durable outcome must tell merged from closed.

    Matching on the delivered prose would break the first time that wording is
    edited, so the probe's own keys travel with the verdict.
    """
    probe = ScriptedProbe(
        [Tick(observations=[Observation("closed", Severity.TERMINAL, "closed unmerged")])]
    )
    verdict = irq_poll("loop-1", "{}", probe)
    assert verdict.outcome is Outcome.TERMINAL
    assert verdict.keys == ("closed",)


def test_poll_maps_a_terminal_observation_to_TERMINAL():
    probe = ScriptedProbe(
        [Tick(observations=[Observation("merged", Severity.TERMINAL, "the PR merged")])]
    )
    verdict = irq_poll("loop-1", "{}", probe)
    assert verdict.outcome is Outcome.TERMINAL
    assert "the PR merged" in verdict.body


def test_poll_maps_a_malformed_config_to_TERMINAL():
    class _BadConfig(Probe):
        def identity(self, ctx: object) -> tuple[str, str]:
            raise ValueError("pr must be a positive integer")

        def observe(self, ctx: object) -> Tick:  # pragma: no cover - never reached
            raise AssertionError("observe must not run for a malformed config")

    verdict = irq_poll("loop-1", "{}", _BadConfig())
    assert verdict.outcome is Outcome.TERMINAL


def test_a_probe_defect_falls_back_instead_of_going_quiet():
    """The fail-safe direction: a bug must not silence the driver's schedule.

    QUIET would tell the caller "nothing to service", so a broken probe would
    stop every wake and the watched work would stall with no signal. FALLBACK
    tells the caller to keep the schedule it already had.
    """
    verdict = irq_poll("loop-1", "{}", _RaisingProbe())
    assert verdict.outcome is Outcome.FALLBACK
    assert verdict.outcome is not Outcome.QUIET


def test_a_kernel_that_returns_without_a_verdict_falls_back(monkeypatch):
    monkeypatch.setattr("kiro_crew.irq.run", lambda *a, **k: None)
    verdict = irq_poll("loop-1", "{}", _ReturningKernelProbe())
    assert verdict.outcome is Outcome.FALLBACK


def test_poll_keys_dedupe_state_by_the_identity_it_is_given():
    """Two drivers on one subject must not share alert memory."""
    first = ScriptedProbe(
        [Tick(observations=[_wake("red:build")])], subject="shared", coalesce_secs=0
    )
    second = ScriptedProbe(
        [Tick(observations=[_wake("red:build")])], subject="shared", coalesce_secs=0
    )
    assert irq_poll("loop-A", "{}", first).outcome is Outcome.WAKE
    # Same subject, same observation key, different identity -> its own memory,
    # so it wakes too rather than being deduped against the other driver.
    assert irq_poll("loop-B", "{}", second).outcome is Outcome.WAKE


def test_poll_dedupes_a_repeat_within_one_identity():
    probe = ScriptedProbe(
        [Tick(observations=[_wake("red:build")]), Tick(observations=[_wake("red:build")])],
        subject="same",
        coalesce_secs=0,
    )
    assert irq_poll("loop-A", "{}", probe).outcome is Outcome.WAKE
    assert irq_poll("loop-A", "{}", probe).outcome is Outcome.QUIET


def test_poll_passes_the_message_through_to_the_probe():
    seen: list[str] = []

    class _MessageProbe(Probe):
        def identity(self, ctx: object) -> tuple[str, str]:
            seen.append(getattr(ctx, "message", ""))
            return ("test-kind", "sub-msg")

        def observe(self, ctx: object) -> Tick:
            return Tick()

    irq_poll("loop-1", '{"repo": "o/n", "pr": 7}', _MessageProbe())
    assert seen == ['{"repo": "o/n", "pr": 7}']
