from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import goal_drafts as goal_drafts_module
from kiro_crew import sandbox, security
from kiro_crew.dashboard.handlers import _shared as shared
from kiro_crew.dashboard.handlers import autonudge as handlers
from kiro_crew.goal_drafts import (
    GOAL_DRAFT_MAX_MESSAGE_CHARS,
    GOAL_DRAFT_TTL_MS,
    GOAL_DRAFTS_DIR_NAME,
    MAX_GOAL_DRAFTS,
    GoalDraftStore,
)


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        yield self._body


class _Request(dict):
    def __init__(self, slot: str, body: Any = None) -> None:
        super().__init__()
        self.match_info = {"slot_key": slot}
        self._body = body
        encoded = json.dumps(body).encode("utf-8")
        self.content = _Content(encoded)
        self.content_length = len(encoded)
        self.charset = "utf-8"
        self.can_read_body = True
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
    migration: bool = True,
):
    return store.put(
        "chat-1",
        message=message,
        idle_secs=None if message is None else 60,
        max_cycles=None if message is None else 0,
        updated_at=stamp,
        migration=migration,
        now_ms=now,
    )


def test_default_store_uses_the_agent_fenced_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_drafts_module, "data_home", lambda: tmp_path)

    store = GoalDraftStore()
    _put(store, "canonical goal", 1_000)

    assert (tmp_path / GOAL_DRAFTS_DIR_NAME / "goal_drafts.json").is_file()
    assert not (tmp_path / "goal_drafts.json").exists()
    assert GOAL_DRAFTS_DIR_NAME in security._CREW_SECRET_LEAVES
    assert GOAL_DRAFTS_DIR_NAME in sandbox._CREW_HIDDEN_LEAVES
    assert GOAL_DRAFTS_DIR_NAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES


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


def test_live_edit_uses_server_order_when_browser_clock_is_behind(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    _put(store, "desktop goal", 9_000)

    edited = _put(
        store,
        "mobile edit made later",
        100,
        now=10_001,
        migration=False,
    )

    assert edited.draft is not None
    assert edited.draft.message == "mobile edit made later"
    assert edited.updated_at == 10_001


def test_live_writes_follow_server_arrival_order(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    _put(store, "initial", 1_000)
    first = _put(
        store,
        "first arrival from future clock",
        50_000,
        now=10_001,
        migration=False,
    )

    last = _put(
        store,
        "last arrival from behind clock",
        10,
        now=10_002,
        migration=False,
    )

    assert first.updated_at == 10_001
    assert last.draft is not None
    assert last.draft.message == "last arrival from behind clock"
    assert last.updated_at == 10_002
    assert store.get("chat-1", now_ms=10_002) == last


def test_live_write_survives_capacity_prune_with_future_migrations(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    now = 1_000_000
    for index in range(MAX_GOAL_DRAFTS):
        store.put(
            f"future-{index}",
            message=f"future goal {index}",
            idle_secs=60,
            max_cycles=0,
            updated_at=now + index + 1,
            migration=True,
            now_ms=now,
        )

    accepted = store.put(
        "live",
        message="accepted live goal",
        idle_secs=60,
        max_cycles=0,
        updated_at=0,
        migration=False,
        now_ms=now,
    )

    raw = json.loads((tmp_path / "goal_drafts.json").read_text(encoding="utf-8"))
    assert accepted.draft is not None
    assert accepted.draft.message == "accepted live goal"
    assert len(raw["drafts"]) == MAX_GOAL_DRAFTS
    assert "live" in raw["drafts"]
    assert "future-0" not in raw["drafts"]


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
        migration=True,
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
            migration=True,
            now_ms=now,
        )
    raw = json.loads((tmp_path / "goal_drafts.json").read_text(encoding="utf-8"))
    assert len(raw["drafts"]) == MAX_GOAL_DRAFTS
    assert "slot-0" not in raw["drafts"]
    assert f"slot-{MAX_GOAL_DRAFTS + 4}" in raw["drafts"]


def test_put_returns_missing_when_the_incoming_migration_is_evicted(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    now = GOAL_DRAFT_TTL_MS + 100_000
    for index in range(MAX_GOAL_DRAFTS):
        store.put(
            f"newer-{index}",
            message=f"goal {index}",
            idle_secs=60,
            max_cycles=0,
            updated_at=now + index,
            migration=True,
            now_ms=now,
        )

    evicted = store.put(
        "older-migration",
        message="older but still within ttl",
        idle_secs=60,
        max_cycles=0,
        updated_at=now - 1,
        migration=True,
        now_ms=now,
    )

    assert evicted.draft is None
    assert evicted.updated_at == 0
    assert store.get("older-migration", now_ms=now) == evicted


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
            "migration": True,
            "draft": {"message": "latest desktop goal", "idle_secs": 90, "max_cycles": 4},
        },
    )
    desktop_response = await handlers.api_goal_draft_put(desktop)
    assert desktop_response.status == 200

    mobile = _Request(
        "chat-1",
        {
            "updated_at": now - 1_000,
            "migration": True,
            "draft": {"message": "stale mobile goal", "idle_secs": 60, "max_cycles": 0},
        },
    )
    mobile_response = await handlers.api_goal_draft_put(mobile)
    assert mobile_response.status == 200
    assert _body(mobile_response)["draft"]["message"] == "latest desktop goal"

    get_response = await handlers.api_goal_draft_get(_Request("chat-1"))
    assert get_response.status == 200
    assert _body(get_response) == _body(desktop_response)


@pytest.mark.parametrize(
    "message",
    [
        "x" * (GOAL_DRAFT_MAX_MESSAGE_CHARS - 1),
        "x" * GOAL_DRAFT_MAX_MESSAGE_CHARS,
        "😀" * GOAL_DRAFT_MAX_MESSAGE_CHARS,
    ],
    ids=["ascii-below", "ascii-boundary", "unicode-boundary"],
)
def test_message_limit_accepts_python_unicode_code_points(
    tmp_path: Path,
    message: str,
) -> None:
    saved = _put(GoalDraftStore(tmp_path), message, 1_000)

    assert saved.draft is not None
    assert saved.draft.message == message
    assert len(saved.draft.message) <= GOAL_DRAFT_MAX_MESSAGE_CHARS


@pytest.mark.parametrize(
    "message",
    [
        "x" * (GOAL_DRAFT_MAX_MESSAGE_CHARS + 1),
        "😀" * (GOAL_DRAFT_MAX_MESSAGE_CHARS + 1),
    ],
    ids=["ascii-over", "unicode-over"],
)
def test_message_limit_rejects_one_excess_unicode_code_point(
    tmp_path: Path,
    message: str,
) -> None:
    with pytest.raises(ValueError, match="message too long"):
        _put(GoalDraftStore(tmp_path), message, 1_000)


@pytest.mark.asyncio
async def test_http_rejects_invalid_goal_draft_without_erasing_the_valid_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GoalDraftStore(tmp_path)
    _put(store, "keep this goal", 1_000)
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: store)
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)

    invalid_bodies = (
        {"updated_at": 2_000},
        {"updated_at": 2_000, "draft": {}},
        {
            "updated_at": 2_000,
            "draft": {"message": None, "idle_secs": 60, "max_cycles": 0},
        },
    )
    for body in invalid_bodies:
        response = await handlers.api_goal_draft_put(_Request("chat-1", body))
        assert response.status == 400
        assert _body(response)["code"] == "invalid_goal_draft"

    saved = store.get("chat-1", now_ms=10_000)
    assert saved.draft is not None
    assert saved.draft.message == "keep this goal"


@pytest.mark.asyncio
async def test_http_rejects_parser_recursion_without_erasing_the_valid_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GoalDraftStore(tmp_path)
    _put(store, "keep this goal", 1_000)
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: store)
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)

    class _ParserStackOverflow:
        @staticmethod
        def loads(_text: str) -> Any:
            raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(shared, "json", _ParserStackOverflow)
    response = await handlers.api_goal_draft_put(
        _Request(
            "chat-1",
            {
                "updated_at": 2_000,
                "draft": {"message": "replace me", "idle_secs": 60, "max_cycles": 0},
            },
        )
    )

    assert response.status == 400
    assert _body(response)["code"] == "invalid_json"
    saved = store.get("chat-1", now_ms=10_000)
    assert saved.draft is not None
    assert saved.draft.message == "keep this goal"
