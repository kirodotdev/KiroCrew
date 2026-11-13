"""Tests for the Windows atomic-rename retry in ``atomic_write``.

``os.replace`` on Windows raises ``PermissionError`` while any other handle is
open on either path, so an indexer or AV scanner touching the freshly written
temp file can defeat an otherwise-correct atomic write. POSIX permits the
replace, so the failure never reproduces on a dev machine and only surfaces on
the ``Backend Tests (Windows)`` matrix.

``windows_sim.replace_sharing_violation`` reproduces the fault deterministically
on any OS, so these drive the exact code path a Windows host would take. They do
NOT prove the real OS behaviour end-to-end, only that the retry is wired,
bounded, gated to Windows, and gated off the event loop.
"""

from __future__ import annotations

import asyncio

import pytest
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.testing.clock import ManualClock


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Keep the bounded retry loop instant; attempt COUNT is what these pin."""
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def _tmp_leftovers(directory):
    return sorted(p.name for p in directory.glob("*.tmp"))


def test_windows_rename_contention_is_retried_until_it_succeeds(tmp_path, monkeypatch):
    """Two faulted renames then a real one: the payload still lands."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "state.json"

    with replace_sharing_violation(match="state.json", times=2) as sim:
        aw.atomic_write(target, "payload-v1")

    assert target.read_text() == "payload-v1"
    # 2 faults + the successful third call.
    assert sim["n"] == 3
    assert _tmp_leftovers(tmp_path) == []


def test_windows_rename_gives_up_after_the_budget_and_removes_the_temp(tmp_path, monkeypatch):
    """A permanent fault still raises, and does not litter a temp file."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "state.json"

    with replace_sharing_violation(match="state.json", times=10_000) as sim:
        with pytest.raises(PermissionError):
            aw.atomic_write(target, "payload-v1")

    assert sim["n"] == aw._REPLACE_MAX_ATTEMPTS
    assert not target.exists()
    assert _tmp_leftovers(tmp_path) == []


def test_posix_permission_error_surfaces_without_retrying(tmp_path, monkeypatch):
    """On POSIX a PermissionError is a real fault, so it must not be slept over.

    This is the non-vacuity proof for the platform gate: with the same simulator
    settings the Windows case above recovers, and this one must not.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    target = tmp_path / "state.json"

    with replace_sharing_violation(match="state.json", times=1) as sim:
        with pytest.raises(PermissionError):
            aw.atomic_write(target, "payload-v1")

    # Exactly one attempt: no retry was made.
    assert sim["n"] == 1
    assert _tmp_leftovers(tmp_path) == []


def test_a_zero_budget_still_renames_once_instead_of_doing_nothing(tmp_path, monkeypatch):
    """A misconfigured budget must degrade to a plain rename, never a no-op.

    The retry count is a module constant someone may tune. If the final attempt
    lived inside ``range(_REPLACE_MAX_ATTEMPTS)``, a budget of 0 would skip the
    body, return without renaming, and let ``atomic_write`` report success over
    a target that was never written: a silent lost write plus a leaked temp
    file. Pinning the degenerate value keeps the final attempt unconditional.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_MAX_ATTEMPTS", 0)
    target = tmp_path / "state.json"

    with replace_sharing_violation(match="state.json", times=0) as sim:
        aw.atomic_write(target, "payload-v1")

    assert target.read_text() == "payload-v1"
    # Exactly one rename happened; the budget did not suppress it entirely.
    assert sim["n"] == 1
    assert _tmp_leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_a_caller_on_the_event_loop_reraises_instead_of_sleeping(tmp_path, monkeypatch):
    """The retry sleeps, so it must never run on the single gateway loop.

    Attempt count is the proof: one call means the loop thread went straight
    back to the caller rather than pausing for the budget. Pinning the count
    rather than elapsed time keeps this deterministic (the autouse fixture
    zeroes the backoff, so a timing assertion could not discriminate).
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "state.json"

    with replace_sharing_violation(match="state.json", times=2) as sim:
        with pytest.raises(PermissionError):
            aw.atomic_write(target, "payload-v1")

    assert sim["n"] == 1
    assert not target.exists()
    assert _tmp_leftovers(tmp_path) == []


@pytest.mark.asyncio
async def test_offloading_from_the_loop_restores_the_retry(tmp_path, monkeypatch):
    """A worker thread has no loop of its own, so offloaded writes still retry.

    This is the other half of the gate: ``AutoNudgeService`` reaches
    ``_write_state`` through ``run_in_executor``, so an offloaded write keeps the
    retry even though the service is driven from the loop.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "state.json"

    with replace_sharing_violation(match="state.json", times=2) as sim:
        await asyncio.to_thread(aw.atomic_write, target, "payload-v1")

    assert target.read_text() == "payload-v1"
    assert sim["n"] == 3
    assert _tmp_leftovers(tmp_path) == []


@pytest.fixture
def _manual_clock(monkeypatch):
    """Drive ``replace_with_retry``'s budget arm on a manual clock.

    The budget arm retries until ``budget_s`` of monotonic time has elapsed.
    Real sleeps would make the test slow and timing-fragile, so a ``ManualClock``
    is installed on ``atomic_write``'s OWN ``time`` binding (not the stdlib's, so
    the rest of the worker keeps real time): its ``sleep`` advances the virtual
    instant by the slept amount and ``monotonic`` reads it, so the budget
    boundary is exact and the loop runs the real number of attempts with no
    wall-clock wait. ``clock.sleeps`` is the backoff schedule the tests assert.
    """
    clock = ManualClock(monotonic_start=0.0)
    clock.install(monkeypatch, aw)
    return clock


def test_a_budget_outlasts_a_reader_the_count_loop_gives_up_on(
    tmp_path, monkeypatch, _manual_clock
):
    """A held reader defeats the count loop but not an elapsed-time budget.

    With ``budget_s`` set the loop keeps retrying past the 10-attempt count until
    the budget is spent, so a reader that closes after more faults than the count
    arm tolerates still lets the rename land. This is the held-transcript case the
    issue describes, reduced to the helper.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "session.jsonl"
    src = tmp_path / "new.tmp"
    target.write_text('{"_type": "metadata"}\n')
    src.write_text('{"_type": "metadata", "x": 1}\n')

    # 11 faults is past the count arm's 10-attempt ceiling, yet the reader
    # closes while the 3 s budget (doubling 50 ms -> 400 ms sleeps) is still
    # running, so the budget arm lets the rename land.
    with replace_sharing_violation(match="session.jsonl", times=11) as sim:
        aw.replace_with_retry(src, target, budget_s=3.0)

    assert target.read_text() == '{"_type": "metadata", "x": 1}\n'
    assert sim["n"] == 12  # 11 faults + the one that succeeded
    # The SAME 11 faults with no budget give up at the count arm's 10 attempts.
    # The count arm sleeps a fixed backoff and never reads monotonic for a
    # deadline, so the manual clock's position does not matter here.
    target.write_text('{"_type": "metadata"}\n')
    src.write_text('{"_type": "metadata", "x": 1}\n')
    with replace_sharing_violation(match="session.jsonl", times=11) as sim2:
        with pytest.raises(PermissionError):
            aw.replace_with_retry(src, target)
    assert sim2["n"] == aw._REPLACE_MAX_ATTEMPTS


def test_budget_backoff_doubles_within_the_ceiling_and_never_overshoots(
    tmp_path, monkeypatch, _manual_clock
):
    """Sleeps double from the floor to the ceiling and stop at the budget."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "session.jsonl"
    src = tmp_path / "new.tmp"
    target.write_text("old\n")
    src.write_text("new\n")

    with replace_sharing_violation(match="session.jsonl", times=10_000):
        with pytest.raises(PermissionError):
            aw.replace_with_retry(src, target, budget_s=1.0)

    sleeps = _manual_clock.sleeps
    # Doubling from 50 ms, capped at 400 ms: 0.05, 0.1, 0.2, 0.4, 0.4, ...
    assert sleeps[0] == pytest.approx(aw._REPLACE_BUDGET_MIN_BACKOFF_SECONDS)
    assert max(sleeps) <= aw._REPLACE_BUDGET_MAX_BACKOFF_SECONDS + 1e-9
    for earlier, later in zip(sleeps, sleeps[1:]):
        assert later >= earlier or later <= aw._REPLACE_BUDGET_MAX_BACKOFF_SECONDS
    # The loop never sleeps past the budget.
    assert sum(sleeps) <= 1.0 + 1e-9


def test_an_exhausted_budget_raises_after_one_warning(tmp_path, monkeypatch, _manual_clock, caplog):
    """A reader that never closes still raises, after exactly one WARNING.

    Acceptance criterion 2: a hold longer than ``budget_s`` raises, and one
    WARNING names the path and the elapsed time so the failure is attributable.
    """
    import logging

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "session.jsonl"
    src = tmp_path / "new.tmp"
    target.write_text("old\n")
    src.write_text("new\n")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.atomic_write"):
        with replace_sharing_violation(match="session.jsonl", times=10_000):
            with pytest.raises(PermissionError):
                aw.replace_with_retry(src, target, budget_s=1.0)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "session.jsonl" in warnings[0].getMessage()
    assert not target.read_text() == "new\n"  # the old bytes are untouched


def test_budget_on_posix_surfaces_without_retrying(tmp_path, monkeypatch, _manual_clock):
    """On POSIX the budget arm re-raises at once: a PermissionError is genuine."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    target = tmp_path / "session.jsonl"
    src = tmp_path / "new.tmp"
    target.write_text("old\n")
    src.write_text("new\n")

    with replace_sharing_violation(match="session.jsonl", times=1) as sim:
        with pytest.raises(PermissionError):
            aw.replace_with_retry(src, target, budget_s=3.0)

    assert sim["n"] == 1
    assert _manual_clock.sleeps == ()  # no retry was attempted


@pytest.mark.asyncio
async def test_budget_on_the_event_loop_still_reraises_at_once(
    tmp_path, monkeypatch, _manual_clock
):
    """A budget never makes the single gateway loop sleep.

    The budget exists for the OFF-loop rewrite (the consolidation pass and the
    slot save both offload). On the loop, the budget arm re-raises on the first
    fault exactly as the count arm does, so the loop is never paused for seconds.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "session.jsonl"
    src = tmp_path / "new.tmp"
    target.write_text("old\n")
    src.write_text("new\n")

    with replace_sharing_violation(match="session.jsonl", times=2) as sim:
        with pytest.raises(PermissionError):
            aw.replace_with_retry(src, target, budget_s=3.0)

    assert sim["n"] == 1
    assert _manual_clock.sleeps == ()


def test_a_zero_budget_degrades_to_a_single_rename(tmp_path, monkeypatch, _manual_clock):
    """A budget of 0 must rename once, never skip the body and report success."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "session.jsonl"
    src = tmp_path / "new.tmp"
    target.write_text("old\n")
    src.write_text("new\n")

    with replace_sharing_violation(match="session.jsonl", times=0) as sim:
        aw.replace_with_retry(src, target, budget_s=0.0)

    assert target.read_text() == "new\n"
    assert sim["n"] == 1
    assert _manual_clock.sleeps == ()


def test_atomic_write_rejects_a_budget_with_a_pinned_parent(tmp_path):
    """The budget rides the by-name rename; a pinned-parent publish cannot honour it.

    ``atomic_write`` rejects the argument pair before it touches the descriptor,
    so a dummy fd of 0 drives the refusal without opening a real directory --
    which Windows refuses, where opening a real one would make this test red on
    that platform alone.
    """
    target = tmp_path / "session.jsonl"
    with pytest.raises(ValueError, match="replace_budget_s cannot be combined"):
        aw.atomic_write(target, "x", parent_dir_fd=0, replace_budget_s=3.0)


def test_autonudge_state_write_survives_the_rename_window(tmp_path, monkeypatch):
    """AutoNudgeService must not lose a state save during the rename window.

    ``_write_state`` keeps its own mkstemp/fsync dance but routes the rename
    through the shared helper, so it inherits the retry.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    svc = AutoNudgeService(base_dir=tmp_path)

    with replace_sharing_violation(match="autonudge.json", times=2) as sim:
        svc._save()

    assert svc._path.exists()
    assert sim["n"] == 3
    assert _tmp_leftovers(svc._path.parent) == []
