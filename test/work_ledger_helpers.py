"""Shared fixtures and builders for the conductor work-ledger store suites.

``test_work_ledger.py`` and its ``test_work_ledger_<topic>.py`` siblings import these by name:
the autouse data-home pin every store test runs under, the two session keys the suites write
as, and the small builders that mint an item or read its bytes back.
"""

from __future__ import annotations

import pytest

from kiro_crew import work_ledger as wl

CONDUCTOR = "chat-1-conductor"
WORKER = "chat-2-worker"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the test_work_ledger suites' autouse pin; none of them calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _new_item(*, title: str = "port the gate", acceptance: dict | None = None) -> str:
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    result = wl.apply_conductor_action(
        CONDUCTOR,
        "create",
        title=title,
        acceptance=acceptance if acceptance is not None else {"kind": "human_approval"},
    )
    return result["item"].item_id


def _bytes_on_disk(item_id: str) -> tuple[bytes, bytes]:
    item = wl.item_path(CONDUCTOR, item_id).read_bytes()
    events = wl.item_events_path(CONDUCTOR, item_id).read_bytes()
    return item, events


def _lift_goal_cap() -> None:
    """Raise the goal's concurrency cap to its ceiling, so a test of the store's own
    open cap reaches it on a board that recorded a goal."""
    wl.apply_conductor_action(CONDUCTOR, "goal", concurrency_cap=wl.MAX_GOAL_CONCURRENCY_CAP)
