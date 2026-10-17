"""The app serial-lock done-probe must not report a queued spawn as finished.

A spawn admitted behind the concurrency / adaptive cap returns its real id to
the caller but has no ``_agents`` entry until it drains, and the pump briefly
holds it in ``_dispatch_window_ids`` across the pop-to-claim / retained-claim
window when it is in neither map. The serial lane's done-probe reads such an id
as untracked and would report it done, releasing the guard and letting the
caller queue a duplicate of work that has not run. ``is_queued`` names that
window so the probe keeps the guard."""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.apps.spawn_sdk import build_done_probe


def _subagents(*, tracked=None, queued_ids=()):
    tracked = tracked or {}
    return SimpleNamespace(
        get=lambda sid: tracked.get(sid),
        is_queued=lambda sid: sid in set(queued_ids),
    )


def test_an_untracked_id_reads_as_done() -> None:
    """The reaper prunes finished runs; a "gone" id must release the guard."""
    assert build_done_probe(_subagents())("gone") is True


def test_a_tracked_running_run_holds_the_guard() -> None:
    probe = build_done_probe(_subagents(tracked={"r": SimpleNamespace(done=False)}))
    assert probe("r") is False


def test_a_tracked_finished_run_releases_the_guard() -> None:
    probe = build_done_probe(_subagents(tracked={"r": SimpleNamespace(done=True)}))
    assert probe("r") is True


def test_a_queued_run_holds_the_guard_despite_no_agents_row() -> None:
    """The core fix: an id with no ``_agents`` entry but still queued is pending
    work, so the probe reports NOT done and the serial guard is held."""
    assert build_done_probe(_subagents(queued_ids=("q",)))("q") is False


def test_a_dispatching_window_id_holds_the_guard() -> None:
    """``is_queued`` also covers the pop-to-claim / retained-claim window the
    pump tracks in ``_dispatch_window_ids`` -- so a probe sampling that window
    does not release the guard."""
    assert build_done_probe(_subagents(queued_ids=("dispatching",)))("dispatching") is False


def test_probe_tolerates_a_manager_without_is_queued() -> None:
    """A manager that predates ``is_queued`` keeps the base contract: an
    untracked id reads as done (no attribute error)."""
    assert build_done_probe(SimpleNamespace(get=lambda sid: None))("gone") is True


def test_probe_handles_no_manager_and_empty_id() -> None:
    assert build_done_probe(None)("x") is True
    assert build_done_probe(_subagents())("") is True
