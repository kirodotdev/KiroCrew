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
an open sticky window's ``opened_at``, and a cleared ``REVISION`` entry is pruned
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
from kiro_crew.irq import Observation, ResetsOn, Severity, Tick
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
    return Observation(key, Severity.WAKE, brief, resets_on=ResetsOn.NEVER)


def _immediate(key: str, brief: str = "immediate brief") -> Observation:
    return Observation(key, Severity.IMMEDIATE, brief)


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


def test_an_immediate_bypasses_the_coalescing_delay_but_not_the_dedupe_mask():
    """An IMMEDIATE skips the window and is still told only once per epoch.

    The severity answers one question -- whether waiting could observe anything
    -- and for a dirty pull request it cannot, because a dirty pull request
    dispatches no checks so ``pending`` never drains. That is not a licence to
    repeat.

    ``test_immediate_bypasses_the_coalescing_window`` pins the bypass and the probe's
    ``test_conflict_wakes_once_per_head`` pins the pairing end to end. The
    kernel's own mask on an IMMEDIATE -- the half a reader drops by reading "bypasses
    the window" as "bypasses everything" -- is what this pins.
    """
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_immediate("conflict", "CONFLICTING")], pending=7),
            Tick(epoch="e1", observations=[_immediate("conflict", "CONFLICTING")], pending=7),
        ]
    )
    first = _verdict(probe, coalesce_secs=_FLOOR)
    assert isinstance(first, Report), "an IMMEDIATE must not wait for the floor"

    _clock.advance(1.0)
    assert isinstance(
        _verdict(probe, coalesce_secs=_FLOOR), Skip
    ), "the same IMMEDIATE inside the realert window must not repeat"


def test_a_masked_immediate_does_not_swallow_a_wake_beside_it():
    """A masked IMMEDIATE falls through to the wake path instead of ending the tick.

    The IMMEDIATE scan runs BEFORE wakes are computed, so a short circuit there loses
    every wake arriving on a tick where the conflict is still outstanding -- and
    a conflict outstands for as long as it takes a human to rebase, which is
    exactly when reds and comments arrive.
    """
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[_immediate("conflict", "CONFLICTING")], pending=0),
            Tick(
                epoch="e1",
                observations=[_immediate("conflict", "CONFLICTING"), _wake("red:a", "a real red")],
                pending=0,
            ),
        ]
    )
    assert isinstance(_verdict(probe, coalesce_secs=0), Report)

    _clock.advance(1.0)
    verdict = _verdict(probe, coalesce_secs=0)
    assert isinstance(verdict, Report), "a masked IMMEDIATE must not end the tick"
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
    red = _wake("red:a", "a REVISION red")
    comment = _sticky("comment:1", "a fresh review comment")
    probe = _ScriptedProbe(
        [
            Tick(epoch="e1", observations=[red], pending=checks_pending),
            Tick(epoch="e1", observations=[red, comment], pending=checks_pending),
            Tick(epoch="e1", observations=[red, comment], pending=checks_pending),
        ]
    )

    # The red opens the window. It is ``REVISION`` and the checks have not
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
    assert "a REVISION red" not in body


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
    red = _wake("red:a", "a REVISION red")
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
    assert "a REVISION red" not in str(verdict), "the REVISION entry is dropped"


def test_a_cleared_revision_entry_is_pruned_before_the_window_extends():
    """An anomaly that cleared while the window was open is not delivered.

    Before the window extends, a ``REVISION`` entry the probe does not report
    this tick is dropped from it. So a check that reads green again is gone from
    the window by the time it fires, and the wake carries only checks still
    failing -- never a cleared check announced as failing beside the observation
    that replaced it.

    The prune is ``REVISION``-only: a sticky entry stays (a probe that stops
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


# ----------------------------------------------------------------- the fetcher


def _iso(age_secs: float) -> str:
    """An ISO-8601 UTC stamp ``age_secs`` in the past, spelled the way gh does."""
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg(**overrides) -> str:
    base: dict = {"repo": "acme/widgets", "pr": 42, "host": "github.com"}
    base.update(overrides)
    return json.dumps(base)


def _core(**overrides) -> dict:
    base = {
        "state": "OPEN",
        "mergedAt": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "reviewDecision": "REVIEW_REQUIRED",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "comments": [],
        "reviews": [],
    }
    base.update(overrides)
    return base


def _wire(monkeypatch, *, core: dict, check_rows: list[dict] | None = None) -> list[list[str]]:
    """Route every gh call in the fetcher to canned JSON. Returns the argv log.

    ``run_gh`` is the seam, because it is the single chokepoint every spawn in that
    module goes through and faking anything above it would leave the real
    subprocess in the path. Headers are prepended the way ``gh api --include``
    emits them, so the rate-limit reader is exercised rather than bypassed.
    """
    seen: list[list[str]] = []
    rows = check_rows or []

    def _fake(argv, **kwargs):
        seen.append(list(argv))
        args = list(argv)[1:]
        if args[:2] == ["pr", "view"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(core), stderr="")
        target = args[-1]
        if "check-runs" in target:
            page = 1
            for part in target.split("&"):
                if part.startswith("page="):
                    page = int(part.split("=", 1)[1])
            body = {"total_count": len(rows), "check_runs": rows if page == 1 else []}
        else:
            body = {"state": "pending", "statuses": []}
        head = "HTTP/2.0 200 OK\r\nx-ratelimit-remaining: 4900\r\n\r\n"
        return types.SimpleNamespace(returncode=0, stdout=head + json.dumps(body), stderr="")

    monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_pr, "run_gh", _fake)
    return seen


def test_a_legacy_status_row_is_bucketed_by_its_state_field(monkeypatch):
    """A row that carries ``context``/``state`` is read like any other.

    The two sequences a head carries mix shapes: a check run carries
    ``status``/``conclusion``, a commit status carries ``context``/``state`` and
    neither of the other two. Reading only the check-run spelling makes every
    commit-status gate unreadable, and a required gate is exactly the row most
    likely to be published that way.
    """
    _wire(
        monkeypatch, core=_core(), check_rows=[{"context": "ci/legacy-status", "state": "FAILURE"}]
    )
    observation = gh_pr.fetch(_msg())
    assert observation.bucket("failing") == ("ci/legacy-status",)


def test_a_nameless_check_row_keeps_a_stable_identity(monkeypatch):
    """A row with no name of its own is still named, not keyed on the empty string.

    The name is both what a reader sees and what one identity folds on, so an empty
    string collapses every nameless row onto one and reports a board shorter than
    the one that exists.
    """
    _wire(
        monkeypatch,
        core=_core(),
        check_rows=[
            {"conclusion": "FAILURE", "status": "COMPLETED", "workflowName": "one"},
            {"conclusion": "FAILURE", "status": "COMPLETED", "workflowName": "two"},
        ],
    )
    observation = gh_pr.fetch(_msg())
    assert observation.bucket("failing") == (
        "one / (unnamed check)",
        "two / (unnamed check)",
    )


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing(monkeypatch):
    """A remark stamp with no offset is treated as UTC.

    An age compares against an aware UTC clock, so a naive stamp cannot be
    subtracted at all -- the comparison raises rather than misreading. Reading it as
    UTC is what keeps one unusually-spelled timestamp from taking the tick down.
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
        "body": "a question",
    }
    _wire(monkeypatch, core=_core(comments=[fresh]))
    observation = gh_pr.fetch(_msg())
    assert [r.author for r in observation.remarks] == ["reviewer-bot"]


def test_every_gh_call_carries_the_watch_audit_tag_and_a_bounded_timeout(monkeypatch):
    """Every spawn goes through the repo's chokepoint, tagged and time-bounded.

    Losing the tag makes the watch's calls unattributable in the audit record;
    losing the timeout lets one hung gh hold an executor thread open. The binary is
    the validated absolute path, so a writable PATH entry cannot shadow it.
    """
    seen: dict = {}

    def _fake(argv, **kwargs):
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_pr, "run_gh", _fake)

    transport = gh_pr._Transport("github.com")
    assert transport.call(["pr", "view", "42"]).ok
    assert seen["audit_caller"] == "core:babysit-pr-watch"
    assert 0 < seen["timeout"] <= gh_pr._GH_TIMEOUT_SECS
    assert seen["pin_host"] == "github.com"
    assert seen["argv"][0] == "/usr/bin/gh", "the validated absolute path, not a PATH lookup"


def test_a_runner_failure_reads_as_one_unavailable_reading_not_a_crash(monkeypatch):
    """Any failure reaching the gh seam becomes one unreadable tick.

    A missing gh, an unavailable audit sink and a timeout all arrive as exceptions,
    and all three mean the same thing: this tick could not observe. Letting one
    escape kills the watch, and a dead watch is silent for the reason an owner would
    least expect.
    """

    def _boom(argv, **kwargs):
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(gh_pr, "resolve_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_pr, "run_gh", _boom)
    monkeypatch.setattr(gh_pr._Transport, "_sleep", lambda self, seconds: None)
    observation = gh_pr.fetch(_msg())
    assert observation.status == gh_pr.STATUS_UNAVAILABLE
    assert not observation.reached and not observation.is_terminal


def test_build_hands_out_a_fresh_probe_and_none_for_an_unknown_kind():
    """``build`` returns a fresh probe per call, and ``None`` is a real answer.

    A fresh instance matters because a probe holds one watch's parsed configuration
    and one tick's reading, so a shared one would serve another watch's subject.
    ``None`` for an unknown kind is supported rather than an error: a monitor whose
    subject nothing observes degrades to its driver's own schedule, never to silence.
    """
    first = probes.build(probes.GH_PR)
    second = probes.build(probes.GH_PR)
    assert isinstance(first, gh_pr.PrWatchProbe)
    assert isinstance(second, gh_pr.PrWatchProbe)
    assert first is not second, "one probe holds one watch's config and reading"
    assert probes.build("no-such-kind") is None
