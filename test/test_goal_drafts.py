from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard.handlers import autonudge as handlers
from kiro_crew.goal_drafts import (
    GOAL_DRAFT_TTL_MS,
    MAX_GOAL_DRAFTS,
    GoalDraftStore,
)


class _Request(dict):
    def __init__(self, slot: str, body: Any = None) -> None:
        super().__init__()
        self.match_info = {"slot_key": slot}
        self._body = body
        self.headers: dict[str, str] = {}
        self.remote = "127.0.0.1"
        self.path = f"/api/autonudge/draft/slot/{slot}"
        self.app: dict[str, Any] = {}

    async def json(self) -> Any:
        return self._body


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.text)


def _put(
    store: GoalDraftStore,
    message: str | None,
    stamp: int,
    *,
    now: int = 10_000,
):
    return store.put(
        "chat-1",
        message=message,
        idle_secs=None if message is None else 60,
        max_cycles=None if message is None else 0,
        updated_at=stamp,
        now_ms=now,
    )


def test_newer_desktop_draft_beats_stale_mobile_migration(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    desktop = _put(store, "latest desktop goal", 2_000)
    stale = _put(store, "old mobile goal", 1_000)

    assert stale == desktop
    assert store.get("chat-1", now_ms=10_000).draft == desktop.draft


def test_clear_tombstone_prevents_stale_draft_resurrection(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    _put(store, "goal to clear", 1_000)
    cleared = _put(store, None, 3_000)
    stale = _put(store, "stale browser copy", 2_000)

    assert cleared.draft is None
    assert stale == cleared
    assert store.get("chat-1", now_ms=10_000).updated_at == 3_000


def test_store_prunes_expired_and_oldest_records(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    now = GOAL_DRAFT_TTL_MS + 100_000
    # One expired record never survives the next transaction.
    store.put(
        "expired",
        message="old",
        idle_secs=60,
        max_cycles=0,
        updated_at=1,
        now_ms=now,
    )
    assert store.get("expired", now_ms=now).draft is None

    for index in range(MAX_GOAL_DRAFTS + 5):
        store.put(
            f"slot-{index}",
            message=f"goal {index}",
            idle_secs=60,
            max_cycles=0,
            updated_at=now + index,
            now_ms=now,
        )
    raw = json.loads((tmp_path / "goal_drafts.json").read_text(encoding="utf-8"))
    assert len(raw["drafts"]) == MAX_GOAL_DRAFTS
    assert "slot-0" not in raw["drafts"]
    assert f"slot-{MAX_GOAL_DRAFTS + 4}" in raw["drafts"]


@pytest.mark.asyncio
async def test_http_put_and_get_return_canonical_newest_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GoalDraftStore(tmp_path)
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: store)
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)

    now = int(time.time() * 1000)
    desktop = _Request(
        "chat-1",
        {
            "updated_at": now,
            "draft": {"message": "latest desktop goal", "idle_secs": 90, "max_cycles": 4},
        },
    )
    desktop_response = await handlers.api_goal_draft_put(desktop)
    assert desktop_response.status == 200

    mobile = _Request(
        "chat-1",
        {
            "updated_at": now - 1_000,
            "draft": {"message": "stale mobile goal", "idle_secs": 60, "max_cycles": 0},
        },
    )
    mobile_response = await handlers.api_goal_draft_put(mobile)
    assert mobile_response.status == 200
    assert _body(mobile_response)["draft"]["message"] == "latest desktop goal"

    get_response = await handlers.api_goal_draft_get(_Request("chat-1"))
    assert get_response.status == 200
    assert _body(get_response) == _body(desktop_response)


@pytest.mark.asyncio
async def test_http_rejects_invalid_goal_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: GoalDraftStore(tmp_path))
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)

    response = await handlers.api_goal_draft_put(
        _Request("chat-1", {"updated_at": 1, "draft": {"message": "", "idle_secs": 0}})
    )
    assert response.status == 400
    assert _body(response)["code"] == "invalid_goal_draft"
