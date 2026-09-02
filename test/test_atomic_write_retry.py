"""Tests for the Windows atomic-rename retry in ``atomic_write`` (issue #1105).

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

    This is the other half of the gate, and the reason it does not defeat
    #1105: ``AutoNudgeService`` reaches ``_write_state`` through
    ``run_in_executor``, so the case the issue reports keeps the retry even
    though the service is driven from the loop.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    target = tmp_path / "state.json"

    with replace_sharing_violation(match="state.json", times=2) as sim:
        await asyncio.to_thread(aw.atomic_write, target, "payload-v1")

    assert target.read_text() == "payload-v1"
    assert sim["n"] == 3
    assert _tmp_leftovers(tmp_path) == []


def test_autonudge_state_write_survives_the_rename_window(tmp_path, monkeypatch):
    """The concrete failure #1105 reports: AutoNudgeService losing a state save.

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
