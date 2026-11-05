from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import autonudge_authz, constants
from kiro_crew import goal_drafts as goal_drafts_module
from kiro_crew import sandbox, security
from kiro_crew.dashboard.handlers import _shared as shared
from kiro_crew.dashboard.handlers import autonudge as handlers
from kiro_crew.goal_drafts import (
    GOAL_DRAFT_MAX_MESSAGE_CHARS,
    GOAL_DRAFT_TTL_MS,
    GOAL_DRAFTS_DIR_NAME,
    MAX_GOAL_DRAFTS,
    GoalDraftConflict,
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
        # The dashboard client declares JSON; the bounded parser refuses any other media type.
        self.content_type = "application/json"
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
    *,
    now: int = 10_000,
    base_updated_at: int | None = None,
    slot: str = "chat-1",
):
    """Write on ``base_updated_at``, by default the record's current stamp."""
    if base_updated_at is None:
        base_updated_at = store.get(slot, now_ms=now).updated_at
    return store.put(
        slot,
        message=message,
        idle_secs=None if message is None else 60,
        max_cycles=None if message is None else 0,
        base_updated_at=base_updated_at,
        now_ms=now,
    )


def test_default_store_uses_the_agent_fenced_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_drafts_module, "data_home", lambda: tmp_path)

    store = GoalDraftStore()
    _put(store, "canonical goal")

    assert (tmp_path / GOAL_DRAFTS_DIR_NAME / "goal_drafts.json").is_file()
    assert not (tmp_path / "goal_drafts.json").exists()
    assert GOAL_DRAFTS_DIR_NAME in security._CREW_SECRET_LEAVES
    assert GOAL_DRAFTS_DIR_NAME in sandbox._CREW_HIDDEN_LEAVES
    assert GOAL_DRAFTS_DIR_NAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES


def test_an_empty_slot_takes_a_write_on_base_zero(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)

    first = _put(store, "first goal", base_updated_at=0)

    assert first.draft is not None and first.draft.message == "first goal"
    assert first.updated_at == 10_000
    assert store.get("chat-1", now_ms=10_000) == first


def test_clear_tombstone_prevents_stale_draft_resurrection(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    original = _put(store, "goal to clear")
    cleared = _put(store, None, now=10_001)
    with pytest.raises(GoalDraftConflict) as conflict:
        _put(store, "stale browser copy", now=10_002, base_updated_at=original.updated_at)

    assert cleared.draft is None
    assert conflict.value.current == cleared
    assert store.get("chat-1", now_ms=10_002) == cleared


def test_stale_write_conflicts_without_replacing_the_newer_goal(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    original = _put(store, "original")
    newer = _put(store, "newer device edit", now=10_001)

    with pytest.raises(GoalDraftConflict) as conflict:
        _put(store, "stale device edit", now=10_002, base_updated_at=original.updated_at)

    assert conflict.value.current == newer
    assert store.get("chat-1", now_ms=10_002) == newer


def test_writes_take_server_stamps_in_arrival_order(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    _put(store, "initial", now=10_000)
    first = _put(store, "first arrival", now=10_000)
    last = _put(store, "last arrival", now=9_000)

    assert first.updated_at == 10_001
    assert last.draft is not None and last.draft.message == "last arrival"
    assert last.updated_at == 10_002
    assert store.get("chat-1", now_ms=10_002) == last


def test_capacity_prune_never_evicts_the_record_just_written(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    now = 1_000_000
    for index in range(MAX_GOAL_DRAFTS):
        _put(store, f"newer goal {index}", now=now + 10 + index, slot=f"newer-{index}")

    # A server clock behind every stored stamp makes this the oldest record.
    accepted = _put(store, "accepted goal", now=now, slot="written")

    raw = json.loads((tmp_path / "goal_drafts.json").read_text(encoding="utf-8"))
    assert accepted.draft is not None and accepted.draft.message == "accepted goal"
    assert len(raw["drafts"]) == MAX_GOAL_DRAFTS
    assert "written" in raw["drafts"]
    assert "newer-0" not in raw["drafts"]
    assert store.get("written", now_ms=now + 100) == accepted


def test_store_prunes_expired_and_oldest_records(tmp_path: Path) -> None:
    store = GoalDraftStore(tmp_path)
    now = GOAL_DRAFT_TTL_MS + 100_000
    # One expired record never survives the next transaction.
    _put(store, "old", now=1, slot="expired")
    assert store.get("expired", now_ms=now).draft is None

    for index in range(MAX_GOAL_DRAFTS + 5):
        _put(store, f"goal {index}", now=now + index, slot=f"slot-{index}")
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

    desktop = _Request(
        "chat-1",
        {
            "base_updated_at": 0,
            "draft": {"message": "latest desktop goal", "idle_secs": 90, "max_cycles": 4},
        },
    )
    desktop_response = await handlers.api_goal_draft_put(desktop)
    assert desktop_response.status == 200

    mobile = _Request(
        "chat-1",
        {
            "base_updated_at": 0,
            "draft": {"message": "stale mobile goal", "idle_secs": 60, "max_cycles": 0},
        },
    )
    mobile_response = await handlers.api_goal_draft_put(mobile)
    assert mobile_response.status == 409
    assert _body(mobile_response)["code"] == "goal_draft_conflict"
    assert _body(mobile_response)["current"] == _body(desktop_response)

    get_response = await handlers.api_goal_draft_get(_Request("chat-1"))
    assert get_response.status == 200
    assert _body(get_response) == _body(desktop_response)


@pytest.mark.asyncio
async def test_http_put_returns_current_snapshot_on_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GoalDraftStore(tmp_path)
    now = int(time.time() * 1000)
    original = _put(store, "original", now=now - 2)
    newer = _put(store, "newer device edit", now=now - 1)
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: store)
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)

    response = await handlers.api_goal_draft_put(
        _Request(
            "chat-1",
            {
                "base_updated_at": original.updated_at,
                "draft": {"message": "stale device edit", "idle_secs": 60, "max_cycles": 0},
            },
        )
    )

    assert response.status == 409
    assert _body(response) == {
        "error": "goal draft changed since it was loaded",
        "code": "goal_draft_conflict",
        "current": newer.to_public_dict(),
    }
    assert store.get("chat-1", now_ms=now) == newer


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
    saved = _put(GoalDraftStore(tmp_path), message)

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
        _put(GoalDraftStore(tmp_path), message)


@pytest.mark.asyncio
async def test_draft_store_and_loop_authorizers_bound_the_message_with_one_constant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start submits a saved draft as the loop message, so the draft store and both
    loop authorizers bound that text with the one constant ``constants`` defines."""
    shared = constants.MAX_NUDGE_MESSAGE_CHARS
    # The same object, not an equal literal: each module binds the shared name.
    assert goal_drafts_module.GOAL_DRAFT_MAX_MESSAGE_CHARS is shared
    assert autonudge_authz.MAX_NUDGE_MESSAGE_CHARS is shared

    # Every enforcement site reads that name when it checks, so the bounds move together.
    monkeypatch.setattr(goal_drafts_module, "GOAL_DRAFT_MAX_MESSAGE_CHARS", 5)
    monkeypatch.setattr(autonudge_authz, "MAX_NUDGE_MESSAGE_CHARS", 5)
    monkeypatch.setattr(
        autonudge_authz, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **_kw: None)
    )
    store = GoalDraftStore(tmp_path)
    assert _put(store, "x" * 5).draft is not None
    with pytest.raises(ValueError, match=r"max 5 chars"):
        _put(store, "x" * 6)

    svc = SimpleNamespace(get_by_id=lambda _loop_id: None)
    _, update_error, update_status = await autonudge_authz.authorize_and_update_nudge(
        svc=svc, loop_id="loop-1", message="x" * 6, source="dashboard"
    )
    assert (update_status, update_error) == (400, "message too long (max 5 chars)")
    slot = SimpleNamespace(workspace="default", mode="", memory_mode="persistent", is_closing=False)
    _, add_error, add_status = await autonudge_authz.authorize_and_add_nudge(
        svc=svc,
        state=SimpleNamespace(_slots={"chat-1-1": slot}, sessions=None, channel_transports={}),
        slot_key="chat-1-1",
        message="x" * 6,
        source="dashboard",
    )
    assert (add_status, add_error) == (400, "message too long (max 5 chars)")


@pytest.mark.asyncio
async def test_http_rejects_invalid_goal_draft_without_erasing_the_valid_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GoalDraftStore(tmp_path)
    _put(store, "keep this goal")
    monkeypatch.setattr(handlers, "get_goal_draft_store", lambda: store)
    monkeypatch.setattr(handlers, "is_owner_dashboard_request", lambda _request: True)

    invalid_bodies = (
        {"base_updated_at": 10_000},
        {"base_updated_at": 10_000, "draft": {}},
        {
            "base_updated_at": 10_000,
            "draft": {"message": None, "idle_secs": 60, "max_cycles": 0},
        },
        {"draft": {"message": "no base", "idle_secs": 60, "max_cycles": 0}},
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
    _put(store, "keep this goal")
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
                "base_updated_at": 10_000,
                "draft": {"message": "replace me", "idle_secs": 60, "max_cycles": 0},
            },
        )
    )

    assert response.status == 400
    assert _body(response)["code"] == "invalid_json"
    saved = store.get("chat-1", now_ms=10_000)
    assert saved.draft is not None
    assert saved.draft.message == "keep this goal"
