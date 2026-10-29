"""Removing a person cancels only their live work, once, and survives a racing update.

The admission service is a small fake with the four members ``stop_principals``
reads; the cancel outcomes (stale revision, invalid transition, unknown id) are
what a real service raises.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.shared_crew.admission import InvalidTransition
from kiro_crew.shared_crew.admission import OperationState as S
from kiro_crew.shared_crew.admission import ReasonCode, StaleRevision
from kiro_crew.shared_crew.admission_revoke import stop_principals


def _work(key: str, state: S, revision: int = 1) -> Any:
    return SimpleNamespace(budget_key=key, state=state, revision=revision)


class _Service:
    def __init__(self, live: dict[str, Any], outcomes: dict[str, list[Any]] | None = None):
        self._live = live
        self._outcomes = outcomes or {}
        self.cancelled: list[tuple[str, int, ReasonCode]] = []

    def live_work(self, work_id: str) -> Any:
        entry = self._live.get(work_id)
        return entry.work if entry is not None else None

    async def cancel(self, work_id: str, *, expected_revision: int, reason: ReasonCode) -> None:
        outcome = (self._outcomes.get(work_id) or [None]).pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.cancelled.append((work_id, expected_revision, reason))


def _live(work: Any, merged_into: str = "") -> Any:
    return SimpleNamespace(work=work, merged_into=merged_into)


@pytest.mark.asyncio
async def test_no_principals_cancels_nothing() -> None:
    service = _Service({"w1": _live(_work("bob", S.QUEUED))})
    assert await stop_principals(service, frozenset()) == {"queued": 0, "running": 0}  # type: ignore[arg-type]
    assert service.cancelled == []


@pytest.mark.asyncio
async def test_only_the_removed_persons_live_unmerged_work_is_cancelled() -> None:
    service = _Service(
        {
            "queued": _live(_work("bob", S.QUEUED)),
            "running": _live(_work("bob", S.RUNNING)),
            "done": _live(_work("bob", S.COMPLETED)),
            "stopping": _live(_work("bob", S.CANCELLING)),
            "merged": _live(_work("bob", S.QUEUED), merged_into="running"),
            "someone-else": _live(_work("ann", S.RUNNING)),
        }
    )
    counts = await stop_principals(service, frozenset({"bob"}))  # type: ignore[arg-type]
    assert counts == {"queued": 1, "running": 1}
    assert [c[0] for c in service.cancelled] == ["queued", "running"]
    assert all(c[2] is ReasonCode.ACCESS_REMOVED for c in service.cancelled)


@pytest.mark.asyncio
async def test_a_stale_revision_is_retried_once_and_a_finished_item_is_left() -> None:
    service = _Service(
        {
            "retry": _live(_work("bob", S.QUEUED)),
            "invalid": _live(_work("bob", S.ADMITTED)),
            "gone": _live(_work("bob", S.QUEUED)),
        },
        outcomes={
            "retry": [StaleRevision(1, 2), None],
            "invalid": [InvalidTransition(S.ADMITTED, S.CANCELLED)],
            "gone": [KeyError("gone")],
        },
    )
    counts = await stop_principals(service, frozenset({"bob"}))  # type: ignore[arg-type]
    assert counts == {"queued": 1, "running": 0}
    assert [c[0] for c in service.cancelled] == ["retry"]


@pytest.mark.asyncio
async def test_work_that_vanishes_or_settles_mid_walk_is_skipped() -> None:
    live = {"w1": _live(_work("bob", S.QUEUED)), "w2": _live(_work("bob", S.QUEUED))}
    service = _Service(live)

    def vanish(work_id: str) -> Any:
        if work_id == "w1":
            return None
        return _work("bob", S.COMPLETED)

    service.live_work = vanish  # type: ignore[method-assign]
    assert await stop_principals(service, frozenset({"bob"})) == {"queued": 0, "running": 0}  # type: ignore[arg-type]
    assert service.cancelled == []
