"""A live goal-draft save whose answer was lost, re-sent on its old base.

The first PUT committed and moved the canonical stamp, but the browser never
read the answer (a timeout during a slow fsync, or a dropped mobile link). Its
retry still carries the old ``base_updated_at``. Identical content is the
user's own committed text, so the store answers that record; different content
on the same stale base is still a conflict.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard.handlers import autonudge as handlers
from kiro_crew.goal_drafts import GoalDraftConflict, GoalDraftStore

_SLOT = "chat-1"


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        yield self._body


class _Request(dict):
    def __init__(self, body: Any) -> None:
        super().__init__()
        self.match_info = {"slot_key": _SLOT}
        encoded = json.dumps(body).encode("utf-8")
        self.content = _Content(encoded)
        self.content_length = len(encoded)
        self.charset = "utf-8"
        self.content_type = "application/json"
        self.can_read_body = True
        self.headers: dict[str, str] = {}
        self.remote = "127.0.0.1"
        self.path = f"/api/autonudge/draft/slot/{_SLOT}"
        self.app: dict[str, Any] = {}


def _live_put(store: GoalDraftStore, message: str | None, base: int, now: int):
    return store.put(
        _SLOT,
        message=message,
        idle_secs=None if message is None else 90,
        max_cycles=None if message is None else 3,
        base_updated_at=base,
        now_ms=now,
    )


def _committed_edit(store: GoalDraftStore, now: int):
    """The original record, and the edit whose answer the browser lost."""

    original = _live_put(store, "original goal", 0, now - 10)
    committed = _live_put(store, "my saved goal", original.updated_at, now - 5)
    assert committed.updated_at != original.updated_at
    return original, committed


def _wire(message: str, base: int, now: int) -> dict[str, Any]:
    return {
        "base_updated_at": base,
        "draft": {"message": message, "idle_secs": 90, "max_cycles": 3},
    }


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GoalDraftStore:
    goal_store = GoalDraftStore(tmp_path)
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: goal_store)
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)
    return goal_store


@pytest.mark.asyncio
async def test_resent_live_put_with_identical_content_answers_the_stored_record(
    store: GoalDraftStore,
) -> None:
    now = int(time.time() * 1000)
    original, committed = _committed_edit(store, now)

    response = await handlers.api_goal_draft_put(
        _Request(_wire("my saved goal", original.updated_at, now))
    )

    assert response.status == 200
    assert json.loads(response.text) == committed.to_public_dict()
    assert store.get(_SLOT, now_ms=now) == committed


@pytest.mark.asyncio
async def test_live_put_with_different_content_on_a_stale_base_still_answers_409(
    store: GoalDraftStore,
) -> None:
    now = int(time.time() * 1000)
    original, committed = _committed_edit(store, now)

    response = await handlers.api_goal_draft_put(
        _Request(_wire("a different goal", original.updated_at, now))
    )

    assert response.status == 409
    body = json.loads(response.text)
    assert body["code"] == "goal_draft_conflict"
    assert body["current"] == committed.to_public_dict()
    assert store.get(_SLOT, now_ms=now) == committed


def test_resent_clear_on_a_stale_base_answers_the_stored_tombstone(
    store: GoalDraftStore,
) -> None:
    now = int(time.time() * 1000)
    _original, committed = _committed_edit(store, now)
    cleared = _live_put(store, None, committed.updated_at, now - 1)
    assert cleared.draft is None

    assert _live_put(store, None, committed.updated_at, now) == cleared
    with pytest.raises(GoalDraftConflict) as conflict:
        _live_put(store, "typed after the clear", committed.updated_at, now)
    assert conflict.value.current == cleared
