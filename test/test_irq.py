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
    Probe,
    Severity,
    Tick,
    load_state,
    run,
    sanitize_label,
    state_path,
)

#: Grace floor used by tests that need a window to close. Paired with
#: :func:`_settle`, which pushes wall-clock past it.
_COALESCE = 0.01


def _settle() -> None:
    """Advance wall clock past ``_COALESCE`` so an OPEN window may now fire.

    A window cannot open and fire within one tick -- ``elapsed`` is zero at
    the moment it opens -- so every coalesced wake costs at least one extra
    tick. See ``test_coalesced_wake_always_costs_an_extra_tick``.
    """
    time.sleep(_COALESCE * 3)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the kernel's state directory at a private tmp home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _ctx(message: str = "{}", job_id: str = "job-1") -> types.SimpleNamespace:
    return types.SimpleNamespace(job=types.SimpleNamespace(id=job_id), message=message)


class ScriptedProbe(Probe):
    """A probe that replays a list of pre-built ticks, one per run()."""

    def __init__(self, ticks: list[Tick], subject: str = "sub-1") -> None:
        self._ticks = list(ticks)
        self._subject = subject
        self.identity_calls = 0

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


def test_window_state_survives_across_ticks():
    probe = ScriptedProbe([Tick(epoch="e1", observations=[_wake("red:a")], pending=3)])
    _verdict(probe, coalesce_secs=999)
    state = load_state(state_path("test-kind", "sub-1", "job-1"))
    assert list(state["coalescing"]) == ["red:a"]
    assert state["coalesce_started_at"] > 0


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
        ]
    )
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
