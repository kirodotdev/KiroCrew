"""Closing or trashing a session takes its MCP resource subscriptions with it.

``rfc-mcp-resource-subscriptions.md`` §4.7. Two call sites drop a session's
holders and record: the dashboard slot close, once the close has committed, and
the session-storage trash and cleanup routes, for the sessions they moved. A
close that is refused keeps them, because the session is still open.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload
from chat_test_helpers import _make_state

from kiro_crew import session_storage as session_storage_module
from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard.handlers import session_storage as storage_handler
from kiro_crew.mcp_gateway import resource_subscriptions as rs
from kiro_crew.mcp_gateway.resource_subscription_store import (
    HolderRecord,
    ResourceSubscriptionStore,
)
from kiro_crew.testing.wait import async_wait_until, finished

_HANG_SECS = 30.0  # only turns a hang into a named failure; the signal decides

NAME = "chat-held-1"
_DAY = 86400.0


class _Req:
    def __init__(self, state, slot: str) -> None:
        self.app = {"state": state}
        self.match_info = {"slot": slot}

    def get(self, key: str, default: str = "") -> str:
        del key
        return default


def _record(session_key: str) -> HolderRecord:
    return HolderRecord(
        session_key=session_key,
        channel_id="",
        server="fake",
        register={"server_name": "fake"},
        initialize={"method": "initialize", "params": {}},
        uris=["crux://reviews/CR-1"],
    )


@pytest.fixture(autouse=True)
def _no_leftover_retire_state():
    """No process-wide resource-subscription cleanup state crosses tests."""
    rs._UNRETIRED.clear()
    rs._LATE_RETIRES.clear()
    rs.set_active_service(None)
    yield
    rs.set_active_service(None)
    rs._UNRETIRED.clear()
    rs._LATE_RETIRES.clear()


@pytest.fixture
def retired(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def _retire(session_key: str, **kwargs) -> int:
        keep = kwargs.get("keep")
        if keep is not None and keep(session_key):
            return 0
        calls.append(session_key)
        return 1

    monkeypatch.setattr(rs, "retire_session", _retire)
    return calls


def _state_with_slot(tmp_path: Path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "watch the CR")
    slot.drain()
    return state, slot


@pytest.mark.asyncio
async def test_a_committed_close_retires_the_sessions_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    state, slot = _state_with_slot(tmp_path)
    key = handlers.effective_session_key(slot)

    async def _persist(*_a, **_kw) -> None:
        assert retired == [], "retired before the close was durable"

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    assert retired == [key]


@pytest.mark.asyncio
async def test_a_close_whose_session_remove_raises_still_retires_the_sessions_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    state, slot = _state_with_slot(tmp_path)
    key = handlers.effective_session_key(slot)
    state.sessions.session_keys.return_value = {key}

    async def _persist(*_a, **_kw) -> None:
        return None

    async def pop_then_fail(_key: str) -> None:
        state.sessions.session_keys.return_value = set()
        raise RuntimeError("provider shutdown failed")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove.side_effect = pop_then_fail
    with pytest.raises(RuntimeError, match="provider shutdown failed"):
        await handlers.api_chat_slot_delete(_Req(state, NAME))

    state.sessions.remove.assert_awaited_once()
    assert retired == [key]


@pytest.mark.asyncio
async def test_a_refused_close_keeps_the_sessions_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    state, _slot = _state_with_slot(tmp_path)

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert retired == []


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    session_storage_module.invalidate_scan_cache()
    crew_home = tmp_path / "crew"
    kiro_home = crew_home / "kiro"
    (crew_home / "sessions" / "archive").mkdir(parents=True)
    (kiro_home / "sessions" / "cli").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    rs.set_active_service(None)
    return crew_home, kiro_home


def _old_transcript(crew_home: Path, stem: str) -> None:
    path = crew_home / "sessions" / f"{stem}.jsonl"
    path.write_text('{"role": "user", "content": "x"}\n', encoding="utf-8")
    mtime = time.time() - 60 * _DAY
    os.utime(path, (mtime, mtime))


def _request(path: str, body: dict):
    raw = json.dumps(body).encode()
    req = make_mocked_request(
        "POST",
        path,
        headers={
            "X-Session-Key": "dashboard:ui",
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
        },
        payload=BodyStreamPayload(raw),
    )
    state = MagicMock()
    state._restricted_keys = set()
    req.app["state"] = state
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["trash", "cleanup"])
async def test_trashing_a_session_retires_its_subscriptions(
    stores: tuple[Path, Path], route: str
) -> None:
    crew_home, _ = stores
    _old_transcript(crew_home, "dashboard_chat-old")
    store = ResourceSubscriptionStore(data_home() / rs.STORE_DIR_LEAF / rs.STORE_LEAF)
    store.put(_record("dashboard:chat-old"))
    store.put(_record("dashboard:kept"))

    if route == "trash":
        req = _request("/api/system/session-storage/trash", {"uids": ["dashboard_chat-old"]})
        call = storage_handler.api_session_inventory_trash
    else:
        req = _request("/api/system/session-storage/cleanup", {"older_than_days": 30})
        call = storage_handler.api_session_storage_cleanup
    with patch.object(storage_handler, "_sel", return_value=MagicMock()):
        resp = await call(req)

    body = json.loads(resp.body)
    assert resp.status == 200, body
    assert body["sessions"] == 1, body
    assert [r.session_key for r in store.load()] == ["dashboard:kept"]


# -- R2 repairs -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_idle_cleanup_retires_the_archived_sessions_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    """The cleanup button archives like a tab close, so it retires the same way."""
    from unittest.mock import AsyncMock

    from test_slot_close_recreation_race import _Req as _CleanupReq

    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    slot = state.get_or_create_slot(NAME)
    key = handlers.effective_session_key(slot)
    slot.created_at = "2020-01-01T00:00:00+00:00"

    resp = await handlers.api_chat_slots_cleanup(_CleanupReq(state, NAME))

    assert resp.status == 200
    assert json.loads(resp.body)["keys"] == [NAME]
    assert retired == [key]


def _delete_claim():
    from kiro_crew.dashboard.handlers.sessions import _HistoryDeleteClaim

    return _HistoryDeleteClaim(
        registry_key=None,
        slot=None,
        history_key=None,
        session_key=None,
        session_generation=1,
        path_match_verified=False,
        complete=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("still_owned", [False, True])
async def test_a_permanent_delete_retires_the_sessions_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str], still_owned: bool
) -> None:
    from remove_slot_for_history_key_helpers import _make_state as _delete_state

    from kiro_crew.dashboard.handlers.sessions_parts.slot_teardown import (
        _remove_slot_for_history_key,
    )

    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record("dashboard:chat-gone"))
    store.put(_record("dashboard:kept"))
    monkeypatch.setattr(rs, "default_store", lambda: store)
    monkeypatch.setattr(rs, "_ACTIVE", None)
    state = _delete_state({})
    if still_owned:
        # A live runtime still serves the key: its subscriptions stay, as its crons do.
        state.sessions.session_keys.return_value = {"dashboard:chat-gone"}

    await _remove_slot_for_history_key(state, "dashboard:chat-gone", delete_claim=_delete_claim())

    assert retired == ([] if still_owned else ["dashboard:chat-gone"])


@pytest.mark.asyncio
async def test_trashing_retires_only_the_sessions_the_move_took(
    stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.session_storage_parts.store_layout import TrashBatch

    crew_home, _ = stores
    for stem in ("dashboard_chat-moved", "dashboard_chat-stayed"):
        _old_transcript(crew_home, stem)
    store = ResourceSubscriptionStore(data_home() / rs.STORE_DIR_LEAF / rs.STORE_LEAF)
    store.put(_record("dashboard:chat-moved"))
    store.put(_record("dashboard:chat-stayed"))

    def move_one(uids, **_kwargs) -> TrashBatch:
        assert sorted(uids) == ["dashboard_chat-moved", "dashboard_chat-stayed"]
        # The search index refused to release "stayed", so it was left in place.
        return TrashBatch("b1", 0.0, "manual", 1, 1, moved=("dashboard_chat-moved",))

    monkeypatch.setattr(storage_handler, "move_to_trash", move_one)
    req = _request(
        "/api/system/session-storage/trash",
        {"uids": ["dashboard_chat-moved", "dashboard_chat-stayed"]},
    )
    with patch.object(storage_handler, "_sel", return_value=MagicMock()):
        resp = await storage_handler.api_session_inventory_trash(req)

    assert resp.status == 200, resp.body
    assert [r.session_key for r in store.load()] == ["dashboard:chat-stayed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["trash", "cleanup"])
async def test_a_session_that_took_the_trashed_key_after_the_move_keeps_its_subscriptions(
    stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A slot key is recycled: a new conversation can own the key once the move is done."""
    crew_home, _ = stores
    _old_transcript(crew_home, "dashboard_chat-old")
    store = ResourceSubscriptionStore(data_home() / rs.STORE_DIR_LEAF / rs.STORE_LEAF)
    store.put(_record("dashboard:chat-old"))
    real_move = storage_handler.move_to_trash
    if route == "trash":
        req = _request("/api/system/session-storage/trash", {"uids": ["dashboard_chat-old"]})
        call = storage_handler.api_session_inventory_trash
    else:
        req = _request("/api/system/session-storage/cleanup", {"older_than_days": 30})
        call = storage_handler.api_session_storage_cleanup
    state = req.app["state"]

    def move_then_recycle(uids, **kwargs):
        batch = real_move(uids, **kwargs)
        # The slot key is reused by a new conversation between the move and the retire.
        state.sessions.session_keys.return_value = {"dashboard:chat-old"}
        return batch

    monkeypatch.setattr(storage_handler, "move_to_trash", move_then_recycle)
    with patch.object(storage_handler, "_sel", return_value=MagicMock()):
        resp = await call(req)

    body = json.loads(resp.body)
    assert resp.status == 200, body
    assert body["sessions"] == 1, body
    assert [r.session_key for r in store.load()] == ["dashboard:chat-old"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["trash", "cleanup"])
async def test_an_unreadable_owner_check_on_a_trash_route_names_that_route(
    stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture, route: str
) -> None:
    import logging

    crew_home, _ = stores
    _old_transcript(crew_home, "dashboard_chat-old")
    store = ResourceSubscriptionStore(data_home() / rs.STORE_DIR_LEAF / rs.STORE_LEAF)
    store.put(_record("dashboard:chat-old"))
    if route == "trash":
        req = _request("/api/system/session-storage/trash", {"uids": ["dashboard_chat-old"]})
        call = storage_handler.api_session_inventory_trash
    else:
        req = _request("/api/system/session-storage/cleanup", {"older_than_days": 30})
        call = storage_handler.api_session_storage_cleanup
    req.app["state"].sessions.session_keys.side_effect = RuntimeError("manager wedged")

    with (
        caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.sessions"),
        patch.object(storage_handler, "_sel", return_value=MagicMock()),
    ):
        resp = await call(req)

    assert resp.status == 200, json.loads(resp.body)
    owner_lines = [r.getMessage() for r in caplog.records if "owners unreadable" in r.getMessage()]
    assert owner_lines, caplog.text
    assert all(
        f"the sessions trashed by POST /api/system/session-storage/{route};" in line
        for line in owner_lines
    ), owner_lines
    # Unreadable owners fail safe: the subscription stays.
    assert [r.session_key for r in store.load()] == ["dashboard:chat-old"]


@pytest.mark.asyncio
async def test_the_trash_retire_waits_no_longer_than_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retire queued behind a subscribe's holder wait must not hold the route."""
    import asyncio

    release = asyncio.Event()

    async def stuck_retire(stems, **_kwargs) -> int:
        del stems
        await release.wait()
        return 0

    monkeypatch.setattr(rs, "RETIRE_WAIT_SECS", 0.05)
    monkeypatch.setattr(rs, "retire_sessions_with_stems", stuck_retire)
    try:
        await asyncio.wait_for(
            storage_handler._retire_trashed_subscriptions(
                {"u": ("s",)}, ("u",), lambda _session_key: False
            ),
            _HANG_SECS,
        )
    finally:
        release.set()


# -- R3 repairs -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_idle_cleanup_retires_every_archived_session_under_one_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """N archived slots must not cost N bounded waits in one request."""
    import asyncio
    import logging
    from unittest.mock import AsyncMock

    from test_slot_close_recreation_race import _Req as _CleanupReq

    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    names = [NAME, "chat-held-2"]
    keys = []
    for name in names:
        slot = state.get_or_create_slot(name)
        slot.created_at = "2020-01-01T00:00:00+00:00"
        keys.append(handlers.effective_session_key(slot))

    release = asyncio.Event()
    entered: list[tuple[str, int]] = []

    def bounds_spent() -> int:
        return sum("still being removed" in r.getMessage() for r in caplog.records)

    async def stuck_retire(session_key: str, **_kwargs) -> int:
        # How many bounded waits had already run out when this retire began.
        entered.append((session_key, bounds_spent()))
        await release.wait()
        return 1

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(rs, "retire_session", stuck_retire)
    # chat_handlers owns the globals its re-exported handlers read.
    monkeypatch.setattr(handlers, "_RESOURCE_SUBSCRIPTION_RETIRE_SECS", 0.05)
    try:
        resp = await asyncio.wait_for(
            handlers.api_chat_slots_cleanup(_CleanupReq(state, NAME)), _HANG_SECS
        )
    finally:
        release.set()

    assert sorted(json.loads(resp.body)["keys"]) == sorted(names)
    assert sorted(entered) == sorted((key, 0) for key in keys)
    assert bounds_spent() == 1
    # The removal that outlived its bound finishes here, not in a later test.
    await asyncio.wait_for(asyncio.gather(*list(rs._LATE_RETIRES)), _HANG_SECS)
    assert rs._LATE_RETIRES == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["close", "trash"])
async def test_a_removal_failing_after_its_bound_is_retrieved_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, route: str
) -> None:
    import asyncio
    import logging

    from kiro_crew.testing.wait import async_wait_until

    release = asyncio.Event()

    async def late_failure(*_args, **_kwargs) -> int:
        await release.wait()
        raise RuntimeError("store wedged")

    caplog.set_level(logging.WARNING)
    if route == "close":
        monkeypatch.setattr(rs, "retire_session", late_failure)
        # chat_handlers owns the globals its re-exported handlers read.
        monkeypatch.setattr(handlers, "_RESOURCE_SUBSCRIPTION_RETIRE_SECS", 0.01)
        await asyncio.wait_for(
            handlers._retire_resource_subscriptions(MagicMock(), "dashboard:gone"),
            _HANG_SECS,
        )
    else:
        monkeypatch.setattr(rs, "RETIRE_WAIT_SECS", 0.01)
        await asyncio.wait_for(
            rs.retire_within_bound(late_failure(), "the trashed sessions"), _HANG_SECS
        )
    release.set()

    def logged_failure() -> bool:
        return any(
            r.levelno == logging.ERROR
            and r.exc_info is not None
            and isinstance(r.exc_info[1], RuntimeError)
            for r in caplog.records
        )

    await async_wait_until(logged_failure, describe=lambda: "the late failure was never logged")


# -- R4 repairs -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retire_that_raises_never_reaches_the_committed_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close's tail (archive retention among it) runs whatever the retire raises."""
    state, _slot = _state_with_slot(tmp_path)
    archived: list[tuple] = []

    async def broken_retire(_session_key: str, **_kwargs) -> int:
        raise RuntimeError("retire broke")

    async def _persist(*_a, **_kw) -> None:
        return None

    monkeypatch.setattr(rs, "retire_session", broken_retire)
    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    monkeypatch.setattr(
        "kiro_crew.coder.retention.schedule_archived", lambda keys, *_a: archived.append(keys)
    )
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    assert archived, "the committed close skipped its archive retention"


@pytest.mark.asyncio
async def test_one_sessions_retire_failure_keeps_every_others_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio
    import logging

    first_failed = asyncio.Event()
    finished: list[str] = []

    async def retire(session_key: str, **_kwargs) -> int:
        if session_key == "dashboard:a":
            first_failed.set()
            raise RuntimeError("a broke")
        await first_failed.wait()
        finished.append(session_key)
        if session_key == "dashboard:c":
            raise ValueError("c broke")
        return 1

    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(rs, "retire_session", retire)
    await asyncio.wait_for(
        handlers._retire_resource_subscriptions(
            MagicMock(), "dashboard:a", "dashboard:b", "dashboard:c"
        ),
        _HANG_SECS,
    )

    assert sorted(finished) == ["dashboard:b", "dashboard:c"]
    logged = {
        type(r.exc_info[1])
        for r in caplog.records
        if r.levelno == logging.ERROR and r.exc_info is not None
    }
    assert logged == {RuntimeError, ValueError}


@pytest.mark.asyncio
async def test_a_removal_outliving_a_cancelled_route_is_held_and_its_failure_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio
    import logging

    from kiro_crew.testing.wait import async_wait_until

    release = asyncio.Event()
    started = asyncio.Event()

    async def late_failure() -> int:
        started.set()
        await release.wait()
        raise RuntimeError("store wedged")

    caplog.set_level(logging.ERROR)
    route = asyncio.create_task(rs.retire_within_bound(late_failure(), "x", timeout=30))
    await asyncio.wait_for(started.wait(), _HANG_SECS)
    route.cancel()
    with pytest.raises(asyncio.CancelledError):
        await route
    # Held while it runs, though the route that started it is gone.
    assert len(rs._LATE_RETIRES) == 1
    release.set()

    def logged_failure() -> bool:
        return any(
            r.levelno == logging.ERROR
            and r.exc_info is not None
            and isinstance(r.exc_info[1], RuntimeError)
            for r in caplog.records
        )

    await async_wait_until(logged_failure, describe=lambda: "the late failure was never logged")
    assert rs._LATE_RETIRES == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["leave", "find"])
async def test_retire_state_one_test_leaves_never_reaches_the_next(step: str) -> None:
    """The autouse reset: the ``find`` run must not see what ``leave`` left."""
    import asyncio

    if step == "leave":
        rs._UNRETIRED.add("dashboard:left-behind")
        rs._LATE_RETIRES.add(asyncio.get_running_loop().create_future())
    else:
        assert rs._UNRETIRED == set()
        assert rs._LATE_RETIRES == set()


# -- R8 repairs -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement_key", "runtime_owns", "expected_retired"),
    [
        ("", False, ["slack:C1:1700000000.000100"]),
        ("", True, []),
        ("slack:C1:1700000000.000100", False, []),
    ],
    ids=["different-key", "live-runtime", "same-key"],
)
async def test_close_retires_by_session_key_after_a_same_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retired: list[str],
    replacement_key: str,
    runtime_owns: bool,
    expected_retired: list[str],
) -> None:
    import asyncio

    state, original = _state_with_slot(tmp_path)
    closing_key = "slack:C1:1700000000.000100"
    original.linked_session_key = closing_key
    state.sessions.session_keys.return_value = {closing_key} if runtime_owns else set()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def save_during_replacement(*_args, **_kwargs) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(handlers, "save_slot_off_loop", save_during_replacement)
    closing = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await asyncio.wait_for(entered.wait(), _HANG_SECS)
    replacement = state.get_or_create_slot(NAME, linked_session_key=replacement_key)
    release.set()
    response = await asyncio.wait_for(closing, _HANG_SECS)

    assert response.status == 200
    assert state._slots[NAME] is replacement
    assert retired == expected_retired
    state.sessions.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_cleanup_retires_a_replaced_linked_sessions_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retired: list[str]
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from test_slot_close_recreation_race import _Req as _CleanupReq

    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    state.sessions.session_keys.return_value = set()
    original = state.get_or_create_slot(NAME)
    closing_key = "cron:closed-job"
    original.linked_session_key = closing_key
    original.created_at = "2020-01-01T00:00:00+00:00"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def save_during_replacement(*_args, **_kwargs) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(handlers, "save_slot_off_loop", save_during_replacement)
    cleaning = asyncio.create_task(handlers.api_chat_slots_cleanup(_CleanupReq(state, NAME)))
    await asyncio.wait_for(entered.wait(), _HANG_SECS)
    replacement = state.get_or_create_slot(NAME)
    release.set()
    response = await asyncio.wait_for(cleaning, _HANG_SECS)

    assert response.status == 200
    assert state._slots[NAME] is replacement
    assert json.loads(response.body)["keys"] == []
    assert retired == [closing_key]
    state.sessions.remove.assert_not_awaited()


class _ReopenedHolder:
    def __init__(self, uri: str) -> None:
        self._uris = [uri]
        self.paused_reason = ""
        self.paused_code = ""

    @property
    def uris(self) -> list[str]:
        return list(self._uris)

    async def subscribe(self, uri: str) -> dict:
        self._uris.append(uri)
        return {"jsonrpc": "2.0", "id": "x", "result": {}}

    async def unsubscribe(self, uri: str) -> None:
        self._uris.remove(uri)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["close", "cleanup"])
async def test_deferred_retirement_keeps_a_reopened_slots_new_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from unittest.mock import AsyncMock

    from test_slot_close_recreation_race import _Req as _CleanupReq

    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    state.sessions.session_keys.return_value = set()
    original = state.get_or_create_slot(NAME)
    original.append("user", "watch the resource")
    original.drain()
    if route == "cleanup":
        original.created_at = "2000-01-01T00:00:00+00:00"
        for message in original.messages:
            message["ts"] = "2000-01-01T00:00:00+00:00"

    closing_key = handlers.effective_session_key(original)
    store = ResourceSubscriptionStore(tmp_path / f"reopened-{route}.json")
    record = _record(closing_key)
    store.put(record)
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = rs.ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=rs.RecordingSink(), settings=lambda: settings
    )
    service._records[record.key] = record
    holder = _ReopenedHolder(record.uris[0])
    service._holders[record.key] = holder  # type: ignore[assignment]
    real_retire = rs.retire_session

    async def reopen_then_retire(session_key: str, **kwargs) -> int:
        state.get_or_create_slot(NAME)
        rs.set_active_service(service)
        assert "result" in await service.subscribe(
            closing_key, "fake", "crux://reviews/CR-reopened"
        )
        return await real_retire(session_key, **kwargs)

    monkeypatch.setattr(rs, "retire_session", reopen_then_retire)
    if route == "close":
        monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock())
        response = await handlers.api_chat_slot_delete(_Req(state, NAME))
    else:
        response = await handlers.api_chat_slots_cleanup(_CleanupReq(state, NAME))

    assert response.status == 200
    assert service.subscription(closing_key, "fake", "crux://reviews/CR-reopened") is not None
    assert service.records(closing_key) == store.load()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["session", "stems"])
async def test_a_retirement_waiting_on_a_store_write_keeps_a_session_reclaimed_meanwhile(
    tmp_path: Path, retirement: str
) -> None:
    from kiro_crew.history_files import transcript_stems

    session_key = f"dashboard:{NAME}"
    store = ResourceSubscriptionStore(tmp_path / "reclaimed.json")
    record = _record(session_key)
    store.put(record)
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = rs.ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=rs.RecordingSink(), settings=lambda: settings
    )
    service._records[record.key] = record
    reclaimed = [False]

    def keep(_candidate: str) -> bool:
        return reclaimed[0]

    await service._write_lock.acquire()
    try:
        if retirement == "session":
            retiring = asyncio.create_task(service.remove_session(session_key, keep=keep))
        else:
            stems = set(transcript_stems(session_key))
            retiring = asyncio.create_task(service.remove_sessions_with_stems(stems, keep))
        await async_wait_until(
            service._lock.locked,
            timeout=_HANG_SECS,
            describe=lambda: "the retirement never passed its first ownership check",
        )
        reclaimed[0] = True
    finally:
        service._write_lock.release()

    assert await finished(retiring, "the retirement answered", timeout=_HANG_SECS) == 0
    assert service.records(session_key) == store.load() == [record]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["close", "cleanup"])
async def test_shared_transcript_replacement_retires_only_the_closed_session_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retired: list[str],
    route: str,
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from slot_close_recreation_race_helpers import _arm_running_turn, _reached
    from test_slot_close_recreation_race import _Req as _CleanupReq

    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    state.sessions.session_keys.return_value = set()
    name = "slack_1785370133.085469"
    closing_key = "slack:1785370133.085469"
    original = state.get_or_create_slot(name, linked_session_key=closing_key)
    original.append("user", "watch the PR")
    original.drain()
    if route == "cleanup":
        original.created_at = "2000-01-01T00:00:00+00:00"
        for message in original.messages:
            message["ts"] = "2000-01-01T00:00:00+00:00"

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    async def persist_handover(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(handlers, "save_slot_off_loop", persist_handover)
    if route == "close":
        closing = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, name)))
    else:
        closing = asyncio.create_task(handlers.api_chat_slots_cleanup(_CleanupReq(state, name)))
    await _reached(entered, closing, seam="the task-cancel wait")
    replacement = state.get_or_create_slot(name, channel_origin=True)
    assert handlers._replacement_shares_transcript(state, name, original)
    assert handlers.effective_session_key(replacement) != closing_key
    release.set()
    response = await asyncio.wait_for(closing, _HANG_SECS)

    assert response.status == 200
    assert state._slots[name] is replacement
    assert retired == [closing_key]
    state.sessions.remove.assert_not_awaited()
    if route == "cleanup":
        assert name not in json.loads(response.body)["keys"]


# -- answer-bound subscribe compensation --------------------------------------


def _answer_budget_service(
    tmp_path: Path, *records: HolderRecord, subscribe_budget: float = 0.4
) -> rs.ResourceSubscriptionService:
    store = ResourceSubscriptionStore(tmp_path / "answer-budget.json")
    for record in records:
        store.put(record)
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = rs.ResourceSubscriptionService(
        store=store,
        socket_path=lambda: tmp_path / "gateway.sock",
        sink=rs.RecordingSink(),
        settings=lambda: settings,
        subscribe_budget_secs=subscribe_budget,
        subscribe_commit_reserve_secs=0.1,
        unsubscribe_budget_secs=0.3,
    )
    for record in records:
        service._records[record.key] = record
    service._started = True
    return service


class _AcceptedHolder:
    def __init__(self) -> None:
        self._uris: list[str] = []
        self.paused_reason = ""
        self.paused_code = ""
        self.ready = asyncio.Event()
        self.accepted = asyncio.Event()
        self.answer = asyncio.Event()
        self.released = asyncio.Event()
        self.closed = False

    @property
    def uris(self) -> list[str]:
        return list(self._uris)

    async def wait_ready(self, _timeout: float) -> None:
        self.ready.set()

    async def subscribe(self, uri: str) -> dict:
        self.accepted.set()
        await self.answer.wait()
        self._uris.append(uri)
        return {"result": {}}

    async def unsubscribe(self, uri: str) -> None:
        if uri in self._uris:
            self._uris.remove(uri)
        self.released.set()

    async def close(self) -> None:
        self.closed = True


def _install_accepted_holder(
    monkeypatch: pytest.MonkeyPatch,
    service: rs.ResourceSubscriptionService,
    holder: _AcceptedHolder,
) -> None:
    async def describe(session_key: str, server: str) -> HolderRecord:
        record = _record(session_key)
        record.server = server
        record.uris = []
        return record

    def start(record: HolderRecord, **_kwargs) -> _AcceptedHolder:
        service._holders[record.key] = holder  # type: ignore[assignment]
        return holder

    monkeypatch.setattr(service, "_describe", describe)
    monkeypatch.setattr(service, "_start_holder", start)


@pytest.mark.asyncio
async def test_subscribe_install_refuses_when_the_write_lock_uses_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _answer_budget_service(tmp_path)
    holder = _AcceptedHolder()
    _install_accepted_holder(monkeypatch, service, holder)

    await service._write_lock.acquire()
    try:
        with pytest.raises(rs.SubscribeRefused) as refused:
            await finished(
                service.subscribe("install-lock", "fake", "crux://reviews/install"),
                "the write-lock-blocked install answered",
                timeout=_HANG_SECS,
            )
    finally:
        service._write_lock.release()

    assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE
    assert "still in progress" in str(refused.value)
    assert not holder.ready.is_set()
    assert service.records() == service._store.load() == []
    assert service._holders == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["delete", "trash"])
async def test_a_retire_reaching_a_closing_service_still_removes_the_stored_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A broker stop closes the service before it stops being the active one."""
    from kiro_crew.history_files import transcript_stems

    service = _answer_budget_service(tmp_path, _record("dashboard:chat-gone"))
    monkeypatch.setattr(rs, "default_store", lambda: service._store)
    rs.set_active_service(service)
    await service.close()

    if route == "delete":
        removed = await rs.retire_session("dashboard:chat-gone")
    else:
        removed = await rs.retire_sessions_with_stems(transcript_stems("dashboard:chat-gone"))

    assert removed == 1
    assert service._store.load() == []
    assert "dashboard:chat-gone" not in rs._UNRETIRED


class _ResumingHolder:
    """A restarted holder that reports a gap for its carried URIs as it connects."""

    def __init__(self, service: rs.ResourceSubscriptionService, key: tuple[str, str]) -> None:
        self._listener = rs._Listener(service, key)
        self._listener.holder = self  # type: ignore[assignment]
        self._uris: list[str] = []
        self.paused_reason = ""
        self.paused_code = ""
        self.refuses: set[str] = set()

    @property
    def uris(self) -> list[str]:
        return list(self._uris)

    def carry(self, uris) -> None:
        self._uris = list(uris)

    async def wait_ready(self, _timeout: float) -> None:
        self._listener.on_gap(self.uris)

    async def subscribe(self, uri: str) -> dict:
        if uri in self.refuses:
            return {"error": {"code": -32602, "message": "unknown resource"}}
        self._uris.append(uri)
        return {"result": {}}

    async def unsubscribe(self, uri: str) -> None:
        if uri in self._uris:
            self._uris.remove(uri)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["committed", "commit_failed", "new_uri_refused"])
@pytest.mark.parametrize("whole_holder_paused", [True, False], ids=["holder", "one_uri"])
async def test_a_resumed_holders_gap_is_published_only_once_the_resume_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, whole_holder_paused: bool
) -> None:
    carried_uri = "crux://reviews/CR-1"
    paused_record = _record("resume-gap")
    if whole_holder_paused:
        paused_record.paused_reason = "backend refused"
    else:
        paused_record.uris = []
        paused_record.paused_uris = {carried_uri: "backend refused"}
    store = ResourceSubscriptionStore(tmp_path / "resume-gap.json")
    store.put(paused_record)
    published: list[tuple[str, str, bool]] = []

    def sink(event: rs.ResourceEvent) -> None:
        # A gap published while its URI is still paused is dropped by the wake layer.
        still_paused = service.paused(event.session_key, event.server, event.uri)
        published.append((event.kind, event.uri, still_paused))

    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = rs.ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=sink, settings=lambda: settings
    )
    service._records[paused_record.key] = paused_record
    service._started = True
    holder = _ResumingHolder(service, paused_record.key)

    async def describe(session_key: str, server: str) -> HolderRecord:
        record = _record(session_key)
        record.server = server
        record.uris = []
        return record

    def start(record: HolderRecord, *, uris, **_kwargs) -> _ResumingHolder:
        holder.carry(uris)
        service._holders[record.key] = holder  # type: ignore[assignment]
        return holder

    monkeypatch.setattr(service, "_describe", describe)
    monkeypatch.setattr(service, "_start_holder", start)
    if outcome == "commit_failed":

        def refuse_put(_record: HolderRecord) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "put", refuse_put)
        with pytest.raises(rs.SubscribeRefused, match="could not persist"):
            await service.subscribe("resume-gap", "fake", "crux://reviews/CR-2")
        assert published == []
    else:
        if outcome == "new_uri_refused":
            # The refusal still commits the carried subscription's resume.
            holder.refuses.add("crux://reviews/CR-2")
        reply = await service.subscribe("resume-gap", "fake", "crux://reviews/CR-2")
        assert ("error" in reply) == (outcome == "new_uri_refused")
        assert published == [(rs.EVENT_MAY_HAVE_CHANGED, carried_uri, False)]
        assert not service.paused("resume-gap", "fake", carried_uri)
    assert service._unsaved == {}


@pytest.mark.asyncio
async def test_subscribe_commit_refusal_releases_the_server_and_finishes_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _answer_budget_service(tmp_path)
    holder = _AcceptedHolder()
    _install_accepted_holder(monkeypatch, service, holder)
    subscribing = asyncio.create_task(
        service.subscribe("commit-lock", "fake", "crux://reviews/commit")
    )
    await async_wait_until(
        holder.accepted.is_set, timeout=_HANG_SECS, describe=lambda: "the subscribe was not sent"
    )
    await service._write_lock.acquire()
    holder.answer.set()
    try:
        with pytest.raises(rs.SubscribeRefused) as refused:
            await finished(
                subscribing,
                "the write-lock-blocked commit answered",
                timeout=_HANG_SECS,
            )
        assert service.holder("commit-lock", "fake") is None
        assert service.records() == service._store.load() == []
        assert holder.closed
        assert not service.sink.events  # type: ignore[attr-defined]
    finally:
        service._write_lock.release()

    assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE
    assert "still in progress" in str(refused.value)
    assert not holder.released.is_set()


@pytest.mark.asyncio
async def test_removal_refuses_when_the_write_lock_uses_its_budget(tmp_path: Path) -> None:
    record = _record("removal-lock")
    service = _answer_budget_service(tmp_path, record)

    await service._write_lock.acquire()
    try:
        with pytest.raises(rs.SubscribeRefused) as refused:
            await finished(
                service.unsubscribe("removal-lock", "fake", record.uris[0]),
                "the write-lock-blocked removal answered",
                timeout=_HANG_SECS,
            )
        assert service.records() == [record]
        assert service._store.load() == [record]
    finally:
        service._write_lock.release()

    assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE
    assert "still in progress" in str(refused.value)
    assert await finished(
        service.unsubscribe("removal-lock", "fake", record.uris[0]),
        "the retried removal answered",
        timeout=_HANG_SECS,
    )
    assert service.records() == service._store.load() == []


@pytest.mark.asyncio
async def test_subscribe_refuses_when_prior_cleanup_cannot_take_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_key = "unretired-lock"
    service = _answer_budget_service(tmp_path)
    holder = _AcceptedHolder()
    _install_accepted_holder(monkeypatch, service, holder)
    rs._UNRETIRED.add(session_key)

    await service._write_lock.acquire()
    try:
        with pytest.raises(rs.SubscribeRefused) as refused:
            await finished(
                service.subscribe(session_key, "fake", "crux://reviews/unretired"),
                "the write-lock-blocked cleanup answered",
                timeout=_HANG_SECS,
            )
    finally:
        service._write_lock.release()

    assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE
    assert "still in progress" in str(refused.value)
    assert session_key in rs._UNRETIRED
    assert not holder.ready.is_set()
    assert service.records() == service._store.load() == []


class _CarryingAcceptedHolder(_AcceptedHolder):
    def __init__(self, uris: tuple[str, ...]) -> None:
        super().__init__()
        self._uris = list(uris)
        self.unsubscribed: list[str] = []

    async def unsubscribe(self, uri: str) -> None:
        self.unsubscribed.append(uri)
        await super().unsubscribe(uri)


class _PausedClosingHolder(_CarryingAcceptedHolder):
    def __init__(self, uris: tuple[str, ...]) -> None:
        super().__init__(uris)
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def wait_ready(self, _timeout: float) -> None:
        self.ready.set()
        await self.answer.wait()
        raise rs.HolderPaused("the holder stopped before the subscribe")

    async def close(self) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed = True


class _StallingUnsubscribeHolder(_CarryingAcceptedHolder):
    def __init__(self, uris: tuple[str, ...]) -> None:
        super().__init__(uris)
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()

    async def unsubscribe(self, uri: str) -> None:
        self.unsubscribed.append(uri)
        if uri in self._uris:
            self._uris.remove(uri)
        self.release_started.set()
        await self.release_gate.wait()


class _RefusingCarryingHolder(_CarryingAcceptedHolder):
    def __init__(self, uris: tuple[str, ...]) -> None:
        super().__init__(uris)
        self.write_lock: asyncio.Lock | None = None
        self.refusal_answered = False
        self.checked_cleanup_read = False

    @property
    def uris(self) -> list[str]:
        if self.refusal_answered and not self.checked_cleanup_read:
            assert (
                self.write_lock is not None and self.write_lock.locked()
            ), "the carried URIs were read without the cleanup write lock"
            self.checked_cleanup_read = True
        return list(self._uris)

    async def subscribe(self, uri: str) -> dict:
        del uri
        self.accepted.set()
        await self.answer.wait()
        self.refusal_answered = True
        return {"error": {"code": -32000, "message": "refused"}}


def _expire_budget_at(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    class _ExpiringBudget:
        def __init__(self, total: float) -> None:
            self.total = total

        def remaining(self) -> float:
            return _HANG_SECS

        def allowance(self, what: str, *, reserve: float = 0.0) -> float:
            del reserve
            if what == step:
                raise rs.SubscribeRefused(
                    f"this call reached its bound of {self.total:g}s before {what}; try again",
                    rs.REFUSED_GATEWAY_UNAVAILABLE,
                )
            return _HANG_SECS

        def holder_call(self) -> float:
            return _HANG_SECS

    monkeypatch.setattr(rs, "_Budget", _ExpiringBudget)


@pytest.mark.asyncio
async def test_subscribe_checks_its_budget_before_starting_the_durable_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record("durable-subscribe")
    service = _answer_budget_service(tmp_path, record)
    holder = _CarryingAcceptedHolder(tuple(record.uris))
    holder.answer.set()
    service._holders[record.key] = holder  # type: ignore[assignment]
    new_uri = "crux://reviews/durable-subscribe"
    _expire_budget_at(monkeypatch, "the durable subscribe commit")

    def unexpected_put(_record: HolderRecord) -> None:
        raise AssertionError("the durable subscribe commit started after its budget")

    monkeypatch.setattr(service._store, "put", unexpected_put)
    with pytest.raises(rs.SubscribeRefused, match="before the durable subscribe commit"):
        await finished(
            service.subscribe(record.session_key, record.server, new_uri),
            "the expired subscribe answered",
            timeout=_HANG_SECS,
        )

    assert holder.unsubscribed == [new_uri]
    assert holder.uris == record.uris
    assert service.records() == service._store.load() == [record]
    assert not service.sink.events  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_removal_checks_its_budget_before_starting_the_durable_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record("durable-removal")
    service = _answer_budget_service(tmp_path, record)
    _expire_budget_at(monkeypatch, "the durable removal commit")

    def unexpected_remove(_session_key: str, _server: str = "") -> int:
        raise AssertionError("the durable removal commit started after its budget")

    monkeypatch.setattr(service._store, "remove", unexpected_remove)
    with pytest.raises(rs.SubscribeRefused, match="before the durable removal commit"):
        await finished(
            service.unsubscribe(record.session_key, record.server, record.uris[0]),
            "the expired removal answered",
            timeout=_HANG_SECS,
        )

    assert service.records() == service._store.load() == [record]
    assert not service.sink.events  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_commit_refusal_drops_a_started_holder_and_restores_its_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _record("started-holder-refusal")
    service = _answer_budget_service(tmp_path, previous)
    holder = _CarryingAcceptedHolder(tuple(previous.uris))
    holder.answer.set()
    _install_accepted_holder(monkeypatch, service, holder)
    _expire_budget_at(monkeypatch, "the durable subscribe commit")
    refused_uri = "crux://reviews/started-holder-refused"

    with pytest.raises(rs.SubscribeRefused, match="before the durable subscribe commit"):
        await finished(
            service.subscribe(previous.session_key, previous.server, refused_uri),
            "the started-holder commit refusal answered",
            timeout=_HANG_SECS,
        )

    assert service.holder(previous.session_key, previous.server) is None
    assert service.records() == service._store.load() == [previous]
    assert holder.closed
    assert not holder.released.is_set()
    assert not service.sink.events  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_commit_refusal_bounds_existing_holder_unsubscribe(
    tmp_path: Path,
) -> None:
    previous = _record("existing-holder-refusal")
    service = _answer_budget_service(tmp_path, previous)
    holder = _StallingUnsubscribeHolder(tuple(previous.uris))
    service._holders[previous.key] = holder  # type: ignore[assignment]
    refused_uri = "crux://reviews/existing-holder-refused"
    subscribing = asyncio.create_task(
        service.subscribe(previous.session_key, previous.server, refused_uri)
    )
    await async_wait_until(
        holder.accepted.is_set,
        timeout=_HANG_SECS,
        describe=lambda: "the existing holder did not accept the subscribe",
    )
    await service._write_lock.acquire()
    holder.answer.set()
    try:
        with pytest.raises(rs.SubscribeRefused) as refused:
            await finished(
                subscribing,
                "the existing holder cleanup answered",
                timeout=_HANG_SECS,
            )
        assert holder.release_started.is_set()
        assert holder.uris == previous.uris
        assert service.records() == service._store.load() == [previous]
        assert service.holder(previous.session_key, previous.server) is holder
        assert not service.sink.events  # type: ignore[attr-defined]
    finally:
        holder.release_gate.set()
        service._write_lock.release()
        await asyncio.gather(subscribing, return_exceptions=True)

    assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE


@pytest.mark.asyncio
async def test_refused_started_holder_records_carried_uris_under_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _record("carried-refusal")
    service = _answer_budget_service(tmp_path, previous)
    holder = _RefusingCarryingHolder(tuple(previous.uris))
    holder.write_lock = service._write_lock
    holder.answer.set()
    _install_accepted_holder(monkeypatch, service, holder)
    writes: list[tuple[list[str], bool]] = []
    put = service._store.put

    def observed_put(record: HolderRecord) -> None:
        writes.append((list(record.uris), service._write_lock.locked()))
        put(record)

    monkeypatch.setattr(service._store, "put", observed_put)
    reply = await finished(
        service.subscribe(previous.session_key, previous.server, "crux://reviews/server-refused"),
        "the server-refused subscribe answered",
        timeout=_HANG_SECS,
    )

    assert reply == {"error": {"code": -32000, "message": "refused"}}
    assert holder.checked_cleanup_read
    assert writes == [(previous.uris, True)]
    assert service.holder(previous.session_key, previous.server) is holder
    assert not holder.closed
    assert service.records() == service._store.load()


@pytest.mark.asyncio
async def test_refused_started_holder_drops_when_cleanup_lock_uses_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _record("carried-lock-refusal")
    service = _answer_budget_service(tmp_path, previous)
    holder = _RefusingCarryingHolder(tuple(previous.uris))
    holder.write_lock = service._write_lock
    _install_accepted_holder(monkeypatch, service, holder)
    subscribing = asyncio.create_task(
        service.subscribe(previous.session_key, previous.server, "crux://reviews/server-refused")
    )
    await async_wait_until(
        holder.accepted.is_set,
        timeout=_HANG_SECS,
        describe=lambda: "the server-refused subscribe was not sent",
    )
    await service._write_lock.acquire()
    holder.answer.set()
    try:
        reply = await finished(
            subscribing,
            "the cleanup-lock-blocked subscribe answered",
            timeout=_HANG_SECS,
        )
        assert service.holder(previous.session_key, previous.server) is None
        assert service.records() == service._store.load() == [previous]
        assert holder.closed
    finally:
        service._write_lock.release()

    assert reply == {"error": {"code": -32000, "message": "refused"}}
    assert not holder.checked_cleanup_read


@pytest.mark.asyncio
async def test_refused_started_holder_checks_budget_before_its_durable_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _record("carried-budget-refusal")
    service = _answer_budget_service(tmp_path, previous)
    holder = _RefusingCarryingHolder(tuple(previous.uris))
    holder.write_lock = service._write_lock
    holder.answer.set()
    _install_accepted_holder(monkeypatch, service, holder)
    _expire_budget_at(monkeypatch, "the durable refused subscribe cleanup")

    def unexpected_put(_record: HolderRecord) -> None:
        raise AssertionError("the refused cleanup write started after its budget")

    monkeypatch.setattr(service._store, "put", unexpected_put)
    reply = await finished(
        service.subscribe(previous.session_key, previous.server, "crux://reviews/server-refused"),
        "the budget-expired cleanup answered",
        timeout=_HANG_SECS,
    )

    assert reply == {"error": {"code": -32000, "message": "refused"}}
    assert holder.checked_cleanup_read
    assert service.holder(previous.session_key, previous.server) is None
    assert service.records() == service._store.load() == [previous]
    assert holder.closed


@pytest.mark.asyncio
async def test_refused_started_holder_drops_when_its_durable_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _record("carried-store-refusal")
    service = _answer_budget_service(tmp_path, previous)
    holder = _RefusingCarryingHolder(tuple(previous.uris))
    holder.write_lock = service._write_lock
    holder.answer.set()
    _install_accepted_holder(monkeypatch, service, holder)

    def failed_put(_record: HolderRecord) -> None:
        raise OSError("store unavailable")

    monkeypatch.setattr(service._store, "put", failed_put)
    reply = await finished(
        service.subscribe(previous.session_key, previous.server, "crux://reviews/server-refused"),
        "the failed durable cleanup answered",
        timeout=_HANG_SECS,
    )

    assert reply == {"error": {"code": -32000, "message": "refused"}}
    assert holder.checked_cleanup_read
    assert service.holder(previous.session_key, previous.server) is None
    assert service.records() == service._store.load() == [previous]
    assert holder.closed


@pytest.mark.asyncio
async def test_refused_subscribe_restores_its_record_before_a_slow_holder_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = _record("slow-close")
    service = _answer_budget_service(tmp_path, previous)
    holder = _PausedClosingHolder(tuple(previous.uris))
    _install_accepted_holder(monkeypatch, service, holder)
    subscribing = asyncio.create_task(
        service.subscribe(previous.session_key, previous.server, "crux://reviews/slow-close")
    )
    await async_wait_until(
        holder.ready.is_set,
        timeout=_HANG_SECS,
        describe=lambda: "the holder did not reach its refusal",
    )
    await service._write_lock.acquire()
    holder.answer.set()
    try:
        await async_wait_until(
            holder.close_started.is_set,
            timeout=_HANG_SECS,
            describe=lambda: "the refused holder close did not start",
        )
        assert service.holder(previous.session_key, previous.server) is None
        assert service.records() == service._store.load() == [previous]
        assert service._records[previous.key] is previous
        assert not holder.closed
        with pytest.raises(rs.SubscribeRefused, match="holder stopped"):
            await finished(
                subscribing,
                "the refusal answered before the slow holder close",
                timeout=_HANG_SECS,
            )
        answered_records = service.records()
        assert not holder.closed
    finally:
        holder.close_release.set()
        service._write_lock.release()
        await asyncio.gather(subscribing, return_exceptions=True)

    await async_wait_until(
        lambda: holder.closed and not rs._LATE_HOLDER_CLOSES,
        timeout=_HANG_SECS,
        describe=lambda: "the slow refused-holder close did not finish",
    )
    assert service.records() == service._store.load() == answered_records


class _ObservedWriteLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.blocked_acquires = 0
        self.second_blocked_acquire = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.blocked_acquires += 1
            if self.blocked_acquires == 2:
                self.second_blocked_acquire.set()
        return await super().acquire()


class _RetryRaceHolder(_CarryingAcceptedHolder):
    def __init__(
        self,
        uris: tuple[str, ...],
        *,
        blocked_uri: str,
        retry_uri: str,
        retry_reached: asyncio.Event,
        retry_answer: asyncio.Event,
    ) -> None:
        super().__init__(uris)
        self.blocked_uri = blocked_uri
        self.retry_uri = retry_uri
        self.retry_reached = retry_reached
        self.retry_answer = retry_answer
        self.blocked_release = asyncio.Event()
        self.release_gate = asyncio.Event()

    async def subscribe(self, uri: str) -> dict:
        reply = await super().subscribe(uri)
        if uri == self.retry_uri:
            self.retry_reached.set()
            await self.retry_answer.wait()
        return reply

    async def unsubscribe(self, uri: str) -> None:
        self.unsubscribed.append(uri)
        if uri in self._uris:
            self._uris.remove(uri)
        if uri == self.blocked_uri:
            self.blocked_release.set()
            await self.release_gate.wait()
        self.released.set()


@pytest.mark.asyncio
async def test_refused_subscribe_does_not_change_state_after_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    carried_uri = "crux://reviews/carried"
    refused_uri = "crux://reviews/refused"
    retry_uri = "crux://reviews/retry"
    previous = _record("retry-after-refusal")
    previous.uris = [carried_uri]
    service = _answer_budget_service(tmp_path, previous, subscribe_budget=0.6)
    retry_reached = asyncio.Event()
    retry_answer = asyncio.Event()
    first = _RetryRaceHolder(
        (carried_uri,),
        blocked_uri=refused_uri,
        retry_uri=retry_uri,
        retry_reached=retry_reached,
        retry_answer=retry_answer,
    )
    second = _CarryingAcceptedHolder((carried_uri,))
    second.answer.set()
    holders: list[_AcceptedHolder] = [first, second]
    write_lock = _ObservedWriteLock()
    service._write_lock = write_lock
    describe_calls = 0

    async def describe(session_key: str, server: str) -> HolderRecord:
        nonlocal describe_calls
        describe_calls += 1
        if describe_calls == 2:
            retry_reached.set()
        record = _record(session_key)
        record.server = server
        record.uris = []
        return record

    def start(record: HolderRecord, **_kwargs) -> _AcceptedHolder:
        holder = holders.pop(0)
        service._holders[record.key] = holder  # type: ignore[assignment]
        return holder

    monkeypatch.setattr(service, "_describe", describe)
    monkeypatch.setattr(service, "_start_holder", start)

    first_call = asyncio.create_task(
        service.subscribe(previous.session_key, previous.server, refused_uri)
    )
    await async_wait_until(
        first.accepted.is_set,
        timeout=_HANG_SECS,
        describe=lambda: "the first server subscribe was not accepted",
    )
    await write_lock.acquire()
    first.answer.set()
    second_call: asyncio.Task[dict[str, object]] | None = None
    try:
        with pytest.raises(rs.SubscribeRefused):
            await finished(
                first_call,
                "the first write-lock-blocked subscribe answered",
                timeout=_HANG_SECS,
            )

        second_call = asyncio.create_task(
            service.subscribe(previous.session_key, previous.server, retry_uri)
        )
        await async_wait_until(
            retry_reached.is_set,
            timeout=_HANG_SECS,
            describe=lambda: "the retry did not reach its lock or holder",
        )
        first.release_gate.set()
        await async_wait_until(
            write_lock.second_blocked_acquire.is_set,
            timeout=_HANG_SECS,
            describe=lambda: "the post-answer writer did not reach the write lock",
        )
        retry_answer.set()
        with pytest.raises(rs.SubscribeRefused):
            await finished(
                second_call,
                "the retry against the stalled write lock answered",
                timeout=_HANG_SECS,
            )

        answered_memory = service.records()
        answered_store = service._store.load()
        answered_holder = service.holder(previous.session_key, previous.server)
        answered_holder_uris = first.uris
        answered_events = list(service.sink.events)  # type: ignore[attr-defined]
    finally:
        first.release_gate.set()
        retry_answer.set()
        write_lock.release()
        await asyncio.gather(
            first_call,
            *([second_call] if second_call is not None else []),
            return_exceptions=True,
        )

    await finished(
        write_lock.acquire(),
        "writers queued before the refused answers finished",
        timeout=_HANG_SECS,
    )
    write_lock.release()

    assert service.records() == answered_memory == [previous]
    assert service._store.load() == answered_store == [previous]
    assert service.holder(previous.session_key, previous.server) is answered_holder is None
    assert first.uris == answered_holder_uris
    assert list(service.sink.events) == answered_events == []  # type: ignore[attr-defined]

    reply = await finished(
        service.subscribe(previous.session_key, previous.server, retry_uri),
        "the retry after the write lock was released answered",
        timeout=_HANG_SECS,
    )
    assert reply == {"result": {}}
    assert service.holder(previous.session_key, previous.server) is second
    assert service.records() == service._store.load()
    assert service.records()[0].uris == second.uris == [carried_uri, retry_uri]
