"""Port baseline: watch-kernel edge cases that nothing else pins.

:mod:`kiro_crew.irq` and its ``gh-pr`` probe carry a long tail of edge cases, and
most of them already have a named test in ``test/test_irq.py`` or
``test/test_babysit_pr_watch.py``. This module holds the remainder: behaviours
that are load-bearing in the kernel and would survive a re-implementation
elsewhere with nothing turning red.

Each test states the behaviour it pins and why the behaviour matters. Where an
existing test already covers a neighbouring behaviour it is cited by name rather
than repeated, so this file adds coverage instead of duplicating it.

Two tests pin an answer the project intends to revisit -- how a signal joining an
open coalescing window is aged, and which entries ride along on a wake they could
not have triggered. Pinning today's answer is what makes a future change to it a
visible diff rather than a silent one.

Three more tests pin behaviours the rest of this file leaves uncovered: the hard
cap fires on its own schedule regardless of the floor, an epoch change preserves
an open sticky window's ``opened_at``, and a cleared epoch-scoped entry is pruned
before the window extends. Three related behaviours are covered elsewhere and are
out of scope here: the ``blind`` consecutive-error backstop, the sticky-key
garbage collection past the re-alert window, and the hostile-input hardening in
``_coerce_ts`` / ``_usable_bound``.
"""

from __future__ import annotations

import json
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew import irq, probes
from kiro_crew.cron_script import Done, Report, Skip
from kiro_crew.irq import Observation, Severity, Tick
from kiro_crew.irq import _dedupe_key as dedupe_key
from kiro_crew.irq import load_state, run, state_path
from kiro_crew.probes import gh_pr

#: A floor big enough that the fake clock can sit clearly inside and outside it.
#: Real time never advances here, so the value only has to keep the arithmetic in
#: each test readable.
_FLOOR = 10.0


class _FakeClock:
    """A wall clock that moves only when a test advances it.

    Installed over the ``time`` name :mod:`kiro_crew.irq` reads, so every
    interval the kernel measures is exact by construction. The assertions that
    need this are the ones asserting a floor has NOT been reached yet: on real
    wall clock a loaded runner can age a window between two calls and turn a
    correct "not yet" into a flake.
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
            "read beyond time.time(). Cover it here."
        )


_clock = _FakeClock(0.0)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the kernel's state directory at a private tmp home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _deterministic_clock(monkeypatch):
    """Give the kernel a clock only this module can move.

    Patched on the module attribute the kernel's ``time.time()`` resolves
    through, never the stdlib module object, so nothing else in the process sees
    the fake.
    """
    _clock.reset(time.time())
    monkeypatch.setattr("kiro_crew.irq.time", _clock)
    return _clock


def _ctx(message: str = "{}", job_id: str = "job-1") -> types.SimpleNamespace:
    return types.SimpleNamespace(job=types.SimpleNamespace(id=job_id), message=message)


class _ScriptedProbe(irq.Probe):
    """Replays pre-built ticks, one per ``run()``. Owns no clock and no IO."""

    def __init__(self, ticks: list[Tick]) -> None:
        self._ticks = list(ticks)

    def identity(self, ctx: object) -> tuple[str, str]:
        return ("test-kind", "sub-1")

    def observe(self, ctx: object) -> Tick:
        return self._ticks.pop(0)


def _verdict(probe: irq.Probe, ctx=None, **kwargs):
    """Run one tick and return the raised verdict exception."""
    try:
        run(ctx or _ctx(), probe, **kwargs)
    except (Skip, Report, Done) as exc:
        return exc
    raise AssertionError("run() returned without raising a verdict")


def _wake(key: str, brief: str = "brief") -> Observation:
    return Observation(key, Severity.WAKE, brief)


def _sticky(key: str, brief: str = "sticky brief") -> Observation:
    """A signal about the SUBJECT rather than the current epoch -- a comment."""
    return Observation(key, Severity.WAKE, brief, epoch_scoped=False)


def _nmi(key: str, brief: str = "nmi brief") -> Observation:
    return Observation(key, Severity.NMI, brief)


def _window() -> dict:
    return load_state(state_path("test-kind", "sub-1", "job-1")).get("coalescing") or {}


# ------------------------------------------------------------------ the kernel


def test_re_observing_an_open_entry_refreshes_its_brief_and_never_its_stamp():
    """A window entry is aged from when it opened, not from when it was last seen.

    A probe re-reports an unresolved anomaly on EVERY tick until it clears, so a
    kernel that restamped an entry on each sighting would reset its clock every
    tick and the floor would never be reached. The failure mode is the dangerous
    kind: a window holding a real signal, looking healthy, silent forever.

    ``test_window_state_survives_across_ticks`` pins that the stamp persists at
    all. This pins that a re-sighting updates the TEXT and leaves the clock
    alone.
    """
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a", "first sighting")], pending=1),
            Tick(epoch="e1", observations=[_wake("red:a", "second sighting")], pending=1),
            Tick(epoch="e1", observations=[_wake("red:a", "third sighting")], pending=0),
        ]
    )
    key = dedupe_key(_wake("red:a"))

    assert isinstance(_verdict(probe, coalesce_secs=_FLOOR), Skip)
    opened = _window()[key]["opened_at"]

    _clock.advance(_FLOOR * 0.6)
    assert isinstance(_verdict(probe, coalesce_secs=_FLOOR), Skip)
    row = _window()[key]
    assert row["opened_at"] == opened, "a re-sighting must not restart the clock"
    assert row["brief"] == "second sighting", "a re-sighting must refresh the text"

    # Total age is now 1.2 floors, which is only reachable because the stamp
    # survived both re-sightings: a restamping kernel would be at 0.6 and skip.
    _clock.advance(_FLOOR * 0.6)
    verdict = _verdict(probe, coalesce_secs=_FLOOR)
    assert isinstance(verdict, Report)
    body = str(verdict)
    assert "third sighting" in body, "the delivered brief must be the latest one"
    assert "first sighting" not in body


def test_an_nmi_bypasses_the_coalescing_delay_but_not_the_dedupe_mask():
    """An NMI skips the window and is still told only once per epoch.

    The severity answers one question -- whether waiting could observe anything
    -- and for a dirty pull request it cannot, because a dirty pull request
    dispatches no checks so ``pending`` never drains. That is not a licence to
    repeat.

    ``test_nmi_bypasses_the_coalescing_window`` pins the bypass and the probe's
    ``test_conflict_wakes_once_per_head`` pins the pairing end to end. The
    kernel's own mask on an NMI -- the half a reader drops by reading "bypasses
    the window" as "bypasses everything" -- is what this pins.
    """
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_nmi("conflict", "CONFLICTING")], pending=7),
            Tick(epoch="e1", observations=[_nmi("conflict", "CONFLICTING")], pending=7),
        ]
    )
    first = _verdict(probe, coalesce_secs=_FLOOR)
    assert isinstance(first, Report), "an NMI must not wait for the floor"

    _clock.advance(1.0)
    assert isinstance(
        _verdict(probe, coalesce_secs=_FLOOR), Skip
    ), "the same NMI inside the realert window must not repeat"


def test_a_masked_nmi_does_not_swallow_a_wake_beside_it():
    """A masked NMI falls through to the wake path instead of ending the tick.

    The NMI scan runs BEFORE wakes are computed, so a short circuit there loses
    every wake arriving on a tick where the conflict is still outstanding -- and
    a conflict outstands for as long as it takes a human to rebase, which is
    exactly when reds and comments arrive.
    """
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_nmi("conflict", "CONFLICTING")], pending=0),
            Tick(
                epoch="e1",
                observations=[_nmi("conflict", "CONFLICTING"), _wake("red:a", "a real red")],
                pending=0,
            ),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=0), Report)

    _clock.advance(1.0)
    verdict = _verdict(probe, coalesce_secs=0)
    assert isinstance(verdict, Report), "a masked NMI must not end the tick"
    assert "a real red" in str(verdict)


def test_a_signal_joining_an_open_window_serves_its_own_full_floor():
    """A joiner is aged from its own arrival, so it cannot fire on the tick it
    appears.

    One age per window cannot answer this, because one window holds signals of
    different ages: a joiner would inherit time it never spent and a burst
    arriving one signal at a time would cost one wake each -- the cost the window
    exists to remove.

    Pinned in the plain case: the joiner is the only entry the population gate
    admits, so its own age is the only thing that can fire the window.
    ``test_an_entry_joining_after_a_partial_fire_serves_its_own_floor`` pins the
    same rule after a partial fire.

    The full floor here is the answer the project intends to revisit -- a joiner
    may instead serve the window's remaining time. Pinning today's answer is what
    makes that change visible.
    """
    checks_pending = 3
    red = _wake("red:a", "an epoch-scoped red")
    comment = _sticky("comment:1", "a fresh review comment")
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[red], pending=checks_pending),
            Tick(epoch="e1", observations=[red, comment], pending=checks_pending),
            Tick(epoch="e1", observations=[red, comment], pending=checks_pending),
        ]
    )

    # The red opens the window. It is epoch-scoped and the checks have not
    # drained, so it cannot fire on its own.
    assert isinstance(_verdict(probe, coalesce_secs=_FLOOR), Skip)

    # The comment joins a window already open longer than the floor. It is
    # sticky, so the population gate admits it without waiting for the checks --
    # its own age is the only thing still holding it.
    _clock.advance(_FLOOR * 1.5)
    assert isinstance(
        _verdict(probe, coalesce_secs=_FLOOR), Skip
    ), "the joiner must not inherit the window's age"

    # Once the comment has served a floor of its own it fires, and it fires
    # alone: the red is still waiting on checks that have not drained.
    _clock.advance(_FLOOR * 1.2)
    verdict = _verdict(probe, coalesce_secs=_FLOOR)
    assert isinstance(verdict, Report)
    body = str(verdict)
    assert "a fresh review comment" in body
    assert "an epoch-scoped red" not in body


def test_an_admitted_entry_rides_along_on_a_wake_it_could_not_have_triggered():
    """Whether an entry may TRIGGER a wake and whether it rides along on one are
    separate questions.

    The second answer stays generous on purpose: holding an admitted entry back
    guarantees a second wake later, which is coalescing that does not coalesce.
    So an entry too young to fire on its own is still delivered beside an entry
    that did fire.
    """
    early = _wake("red:early", "the early red")
    late = _wake("red:late", "the late red")
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[early], pending=1),
            Tick(epoch="e1", observations=[early, late], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_FLOOR), Skip)

    _clock.advance(_FLOOR * 1.2)
    verdict = _verdict(probe, coalesce_secs=_FLOOR)
    assert isinstance(verdict, Report)
    body = str(verdict)
    assert "the early red" in body, "the aged entry triggers the wake"
    assert "the late red" in body, "the young entry rides along rather than buying a second wake"
    assert _window() == {}, "a full fire leaves nothing behind to wake again"


def test_the_hard_cap_is_not_gated_behind_the_floor():
    """The absolute wall flushes even when the floor is never reached.

    ``coalesce_max_secs`` is a wall-clock bound measured from the oldest entry,
    independent of the floor and of ``pending``. With a floor set above the cap
    (both finite and positive, which ``run`` accepts) and a ``pending`` that
    never drains, the floor is never reached, yet the window still flushes once
    the oldest entry's age passes the cap. The other tests here use the default
    ordering where the floor is below the cap, so this is the only case that
    exercises the cap on its own.
    """
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_wake("red:a", "stuck red")], pending=1),
            Tick(epoch="e1", observations=[_wake("red:a", "stuck red")], pending=1),
        ]
    )
    # Floor deliberately ABOVE the cap: both legal, and the cap is what must
    # still fire while the floor stays unreached.
    floor = 100.0
    cap = 5.0

    assert isinstance(_verdict(probe, coalesce_secs=floor, coalesce_max_secs=cap), Skip)

    # Past the cap but nowhere near the floor, and pending never drained.
    _clock.advance(cap * 2)
    verdict = _verdict(probe, coalesce_secs=floor, coalesce_max_secs=cap)
    assert isinstance(
        verdict, Report
    ), "the hard cap flushes on its own even when the floor is unreached and pending never drains"
    assert "stuck red" in str(verdict)
    assert _window() == {}, "a cap flush delivers the whole window"


def test_an_epoch_change_carries_an_open_sticky_window_with_its_stamp():
    """A sticky entry open but undelivered survives an epoch change, aged from
    when it opened.

    A sticky signal (a comment) is a property of the subject, not of the epoch,
    so an epoch change keeps an open sticky window entry and keeps the
    ``opened_at`` it already holds. The carried entry therefore fires on the age
    it has already served rather than starting a fresh floor after the change.

    ``test_a_signal_joining_an_open_window_serves_its_own_full_floor`` pins
    sticky admission WITHIN one epoch; this pins that an open sticky window
    survives the epoch boundary.
    """
    comment = _sticky("comment:1", "a review comment")
    red = _wake("red:a", "an epoch-scoped red")
    probe = _ScriptedProbe(
        [
            # e1: the comment opens the window; the red keeps pending non-zero
            # so nothing fires yet.
            Tick(epoch="e1", observations=[comment, red], pending=2),
            # e2: force-push. The red belongs to the old commit and is absent;
            # the comment is re-observed. The comment's original stamp is
            # retained, so it is already past its floor and fires now.
            Tick(epoch="e2", observations=[comment], pending=0),
        ]
    )

    assert isinstance(_verdict(probe, coalesce_secs=_FLOOR), Skip)

    # Advance past the floor, THEN change epoch. The carried entry fires
    # immediately because the age it served survives the reset. The delivered
    # wake is the property; how the age is stored is not asserted, so a design
    # that keeps the stamp elsewhere passes too.
    _clock.advance(_FLOOR * 1.5)
    verdict = _verdict(probe, coalesce_secs=_FLOOR)
    assert isinstance(
        verdict, Report
    ), "an open sticky window survives the epoch change carrying the age it served"
    assert "a review comment" in str(verdict)
    assert "an epoch-scoped red" not in str(verdict), "the epoch-scoped entry is dropped"


def test_a_cleared_epoch_scoped_entry_is_pruned_before_the_window_extends():
    """An anomaly that cleared while the window was open is not delivered.

    Before the window extends, an epoch-scoped entry the probe does not report
    this tick is dropped from it. So a check that reads green again is gone from
    the window by the time it fires, and the wake carries only checks still
    failing -- never a cleared check announced as failing beside the observation
    that replaced it.

    The prune is epoch-scoped only: a sticky entry stays (a probe that stops
    reporting a comment has stopped LOOKING, not seen it clear), which the
    sticky tests cover.
    """
    stale = _wake("red:a", "check A is failing")
    fresh = _wake("red:b", "check B is failing")
    probe = _ScriptedProbe(
        [
            # Both reds open the window together, so both age from the same
            # instant. This keeps the case about the prune rather than the
            # joiner-serves-its-own-floor rule, which needs an entry aged from 0.
            Tick(epoch="e1", observations=[stale, fresh], pending=1),
            # red:a is absent this tick; red:b is still failing.
            Tick(epoch="e1", observations=[fresh], pending=0),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=_FLOOR), Skip)

    _clock.advance(_FLOOR * 1.2)
    verdict = _verdict(probe, coalesce_secs=_FLOOR)
    assert isinstance(verdict, Report)
    body = str(verdict)
    assert "check B is failing" in body, "the still-failing check wakes"
    assert (
        "check A is failing" not in body
    ), "a check absent this tick is pruned, not delivered as still failing"


# ------------------------------------------------------------------- the probe


def _iso(age_secs: float) -> str:
    """An ISO-8601 UTC stamp ``age_secs`` in the past, spelled the way gh does."""
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg(**overrides) -> str:
    # coalesce_secs=0 fires on the first anomaly, which keeps these cases about
    # the probe's classification rather than the kernel's window -- that has its
    # own tests above and in test/test_irq.py.
    base: dict = {"repo": "acme/widgets", "pr": 42, "coalesce_secs": 0}
    base.update(overrides)
    return json.dumps(base)


def _payload(checks: list[dict], **overrides) -> dict:
    base = {
        "state": "OPEN",
        "mergedAt": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "headRefOid": "a" * 40,
        "statusCheckRollup": checks,
        "comments": [],
        "reviews": [],
    }
    base.update(overrides)
    return base


def _drive(monkeypatch, payload: dict, message: str | None = None):
    """Run one probe tick against a faked gh, and return the verdict.

    The seam is the probe's own ``_run_gh``, which every gh spawn in that module
    goes through. Faking anything above it would leave the real subprocess in the
    path.
    """
    monkeypatch.setattr(gh_pr, "_run_gh", lambda args, pin_host="": (0, json.dumps(payload)))
    probe = gh_pr.PrWatchProbe()
    return _verdict(probe, _ctx(message or _msg(), job_id="job-probe"))


def test_the_probe_asks_the_kernel_for_exactly_one_bound():
    """``tuning()`` declares the window the watch was armed with, and nothing else.

    It is the probe's only say over kernel bounds, and the kernel honours exactly
    the one key that has a producer. A second key would be an untested contract;
    dropping this one would silently take the kernel default in place of the
    window the operator asked for.
    """
    probe = gh_pr.PrWatchProbe()
    probe.identity(_ctx(_msg(coalesce_secs=45)))
    assert probe.tuning() == {"coalesce_secs": 45.0}

    default = gh_pr.PrWatchProbe()
    default.identity(_ctx(json.dumps({"repo": "acme/widgets", "pr": 42})))
    assert default.tuning() == {"coalesce_secs": irq.DEFAULT_COALESCE_SECS}


def test_a_legacy_status_row_is_bucketed_by_its_state_field(monkeypatch):
    """A rollup row that carries ``context``/``state`` is read like any other.

    ``statusCheckRollup`` mixes two shapes: a CheckRun carries
    ``status``/``conclusion``, a StatusContext carries ``context``/``state`` and
    neither of the other two. Reading only the CheckRun spelling makes every
    commit-status check unreadable, and an unreadable check is not a quiet one --
    unknown vocabulary buckets as failing, so it wakes on every head.
    """
    verdict = _drive(monkeypatch, _payload([{"context": "ci/legacy-status", "state": "FAILURE"}]))
    assert isinstance(verdict, Report)
    assert "ci/legacy-status" in str(verdict)


def test_a_nameless_check_row_keeps_a_stable_identity(monkeypatch):
    """A row with no name of its own is still named, not keyed on the empty string.

    The name is both what the operator reads and what dedupe keys on, so an empty
    string collapses every nameless row onto one key and delivers a brief that
    reads as truncated.
    """
    verdict = _drive(monkeypatch, _payload([{"conclusion": "FAILURE", "status": "COMPLETED"}]))
    assert isinstance(verdict, Report)
    assert "(unnamed check)" in str(verdict)


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing(monkeypatch):
    """A conversation stamp with no offset is treated as UTC.

    Freshness compares against an aware UTC clock, so a naive stamp cannot be
    subtracted at all -- the comparison raises rather than misreading. Reading it
    as UTC is what keeps one unusually-spelled timestamp from taking the whole
    tick down.
    """
    aware = gh_pr._age_secs(_iso(120))
    naive = gh_pr._age_secs(_iso(120).rstrip("Z"))
    assert aware is not None and naive is not None
    assert abs(aware - naive) < 2, "a naive stamp must read as UTC, not as local time"

    fresh = {
        "id": "IC_naive",
        "createdAt": _iso(30).rstrip("Z"),
        "author": {"login": "reviewer-bot"},
        "viewerDidAuthor": False,
        "body": "",
    }
    verdict = _drive(monkeypatch, _payload([], comments=[fresh]))
    assert isinstance(verdict, Report)
    assert "reviewer-bot" in str(verdict)


def test_the_wake_tail_warns_that_check_names_are_untrusted_data(monkeypatch):
    """The wake tail tells the woken agent that quoted check names are data.

    A check name is authored by whoever authored the workflow, and it is quoted
    verbatim into a brief delivered to an agent as a real turn. The tail is where
    the agent is told to treat those names as identifiers to look up rather than
    as instructions, so the warning is part of the contract and not decoration.

    The tests around it treat the tail as an opaque constant and pin only that it
    is delivered once. This pins what it has to SAY.
    """
    tail = gh_pr._WAKE_TAIL.lower()
    assert "untrusted" in tail
    assert "never as instructions" in tail

    verdict = _drive(monkeypatch, _payload([{"name": "Body Gate", "conclusion": "FAILURE"}]))
    assert isinstance(verdict, Report)
    assert "never as instructions" in str(verdict).lower(), "the warning must reach the wake"


def test_every_gh_call_carries_the_watch_audit_tag_and_a_bounded_timeout(monkeypatch):
    """Every gh spawn goes through the repo's chokepoint, tagged and time-bounded.

    Losing the tag makes the watch's calls unattributable in the audit record;
    losing the timeout lets one hung gh hold a cron subprocess open
    indefinitely. The binary is the validated absolute path, so a writable PATH
    entry cannot shadow it.
    """
    seen: dict = {}

    def _fake_run_gh(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return types.SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_pr, "run_gh", _fake_run_gh)

    assert gh_pr._run_gh(["pr", "view", "42"], pin_host="github.com") == (0, "{}")
    assert seen["audit_caller"] == "core:babysit-pr-watch"
    assert seen["timeout"] == gh_pr._GH_TIMEOUT_SECS
    assert seen["pin_host"] == "github.com"
    assert seen["argv"][0] == "/usr/bin/gh", "the validated absolute path, not a PATH lookup"


def test_a_runner_failure_reads_as_one_failed_tick_not_a_crash(monkeypatch):
    """Any failure reaching the gh seam becomes one unobservable tick.

    A missing gh, an unavailable audit sink and a timeout all arrive as
    exceptions, and all three mean the same thing to the watch: this tick could
    not observe. Letting one escape kills the cron, and a dead cron is a watch
    that is silent for the reason an operator would least expect.
    """

    def _boom(argv, **kwargs):
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_pr, "run_gh", _boom)
    assert gh_pr._run_gh(["pr", "view", "42"]) == (1, "")


def test_build_hands_out_a_fresh_probe_and_none_for_an_unknown_kind():
    """``build`` returns a fresh probe per call, and ``None`` is a real answer.

    A fresh instance matters because a probe holds one watch's parsed
    configuration -- repo, pull request, known reds, window -- so a shared one
    would serve another watch's subject. ``None`` for an unknown kind is
    supported rather than an error: a monitor whose subject nothing observes
    degrades to its driver's own schedule, never to silence.
    """
    first = probes.build(probes.GH_PR)
    second = probes.build(probes.GH_PR)
    assert isinstance(first, gh_pr.PrWatchProbe)
    assert isinstance(second, gh_pr.PrWatchProbe)
    assert first is not second, "one probe holds one watch's config; sharing leaks it"
    assert probes.build("no-such-kind") is None
