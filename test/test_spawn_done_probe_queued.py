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

import pytest

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


@pytest.mark.parametrize(
    "done",
    [
        pytest.param(False, id="test_a_tracked_running_run_holds_the_guard"),
        pytest.param(True, id="test_a_tracked_finished_run_releases_the_guard"),
    ],
)
def test_a_tracked_run_reports_its_own_state(done: bool) -> None:
    probe = build_done_probe(_subagents(tracked={"r": SimpleNamespace(done=done)}))
    assert probe("r") is done


@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("q", id="test_a_queued_run_holds_the_guard_despite_no_agents_row"),
        # ``is_queued`` also covers the pop-to-claim / retained-claim window the
        # pump tracks in ``_dispatch_window_ids``.
        pytest.param("dispatching", id="test_a_dispatching_window_id_holds_the_guard"),
    ],
)
def test_a_queued_id_holds_the_guard(run_id: str) -> None:
    """An id with no ``_agents`` entry but still queued is pending work, so the
    probe reports NOT done and the serial guard is held."""
    assert build_done_probe(_subagents(queued_ids=(run_id,)))(run_id) is False


def test_probe_tolerates_a_manager_without_is_queued() -> None:
    """A manager that predates ``is_queued`` keeps the base contract: an
    untracked id reads as done (no attribute error)."""
    assert build_done_probe(SimpleNamespace(get=lambda sid: None))("gone") is True


def test_probe_handles_no_manager_and_empty_id() -> None:
    assert build_done_probe(None)("x") is True
    assert build_done_probe(_subagents())("") is True


@pytest.mark.parametrize(
    ("record", "succeeded"),
    [
        pytest.param(SimpleNamespace(done=True, outcome="completed"), True, id="completed"),
        pytest.param(SimpleNamespace(done=True, outcome="failed"), False, id="failed"),
        pytest.param(SimpleNamespace(done=True, outcome="stopped"), False, id="stopped"),
        pytest.param(SimpleNamespace(done=False, outcome="completed"), False, id="running"),
        pytest.param(None, False, id="unknown_id"),
    ],
)
def test_only_a_run_the_host_reports_completed_reads_as_a_success(record, succeeded) -> None:
    """An app marks work done only on a success; a failed, stopped, still running
    or forgotten run must read as no success so the app retries or reports it."""
    from kiro_crew.apps import spawn_sdk

    tracked = {} if record is None else {"r": record}
    assert spawn_sdk.build_success_probe(_subagents(tracked=tracked))("r") is succeeded


def test_the_success_probe_handles_no_manager_and_empty_id() -> None:
    from kiro_crew.apps import spawn_sdk

    assert spawn_sdk.build_success_probe(None)("x") is False
    assert spawn_sdk.build_success_probe(_subagents(tracked={"": None}))("") is False


def test_the_sdk_reads_a_success_from_its_probe_and_no_probe_as_none() -> None:
    from kiro_crew.apps.spawn_sdk import SpawnSDK

    def _impl(*_a: object) -> str:
        return "r"

    assert SpawnSDK("app", _impl, success_probe=lambda sid: sid == "r").succeeded("r") is True
    assert SpawnSDK("app", _impl).succeeded("r") is False

    def _broken(_sid: str) -> bool:
        raise RuntimeError("probe broke")

    assert SpawnSDK("app", _impl, success_probe=_broken).succeeded("r") is False
