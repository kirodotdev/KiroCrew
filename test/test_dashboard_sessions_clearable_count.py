"""Tests for ``GET /api/sessions/clearable/count`` and its shared selector.

A bulk delete's confirmation needs to state how many
sessions it will remove. ``DELETE /api/sessions`` offered no way to learn that
before committing, so this endpoint answers it and nothing else.

Two properties carry the feature and each has tests here:

- the endpoint is READ-ONLY. Proven by naming the survivors — the files are
  still on disk afterwards, byte-identical, with unchanged mtimes. An
  "assert nothing was deleted" would pass just as happily against an empty
  directory, which is why it is not the assertion used.
- the count and the delete resolve the SAME set, because they call one
  selector that takes no cutoff. A count the delete does not honour is worse
  than no count: a filtered count would report a subset of what the unfiltered
  delete permanently unlinks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import api_sessions_clear, api_sessions_clearable_count
from kiro_crew.dashboard.handlers.sessions import _clearable_history_keys, api_session_delete
from kiro_crew.history import ConversationLog

CLOSED_AT = 1_700_000_000.0


class _FakeSlot:
    """Minimal stand-in for ``_ChatSlot`` — only what the handler reads."""

    def __init__(self, key: str, *, pinned: bool = False, running: bool = False) -> None:
        self.key = key
        self.pinned = pinned
        self._running = running
        self.linked_session_key = ""
        self.channel_origin = False

    @property
    def running(self) -> bool:
        return self._running


def _history_key_for(key: str) -> str:
    from kiro_crew.dashboard.chat import _history_key_for as _hkf

    return _hkf(key)


def _fake_state(
    sessions: list[dict],
    *,
    slots: dict[str, _FakeSlot] | None = None,
    metadata: dict[str, dict] | None = None,
    unreadable_keys: set[str] | None = None,
    raising_keys: set[str] | None = None,
) -> tuple[MagicMock, list[str]]:
    """A state whose ``conversation_log`` records every delete it is asked for."""
    deleted_keys: list[str] = []
    metadata = metadata or {}
    unreadable_keys = unreadable_keys or set()
    raising_keys = raising_keys or set()

    conv_log = MagicMock()
    conv_log.list_sessions.return_value = sessions

    def _get_metadata_status(k: str) -> tuple[dict, bool]:
        if k in raising_keys:
            raise json.JSONDecodeError("bad", "", 0)
        if k in unreadable_keys:
            return {}, False
        return metadata.get(k, {}), True

    conv_log.get_metadata_status.side_effect = _get_metadata_status

    @contextlib.contextmanager
    def _locked_mock(k: str) -> Iterator[None]:
        yield

    conv_log._locked = _locked_mock

    def _delete(key: str, *, skip_pinned: bool = False) -> bool | None:
        if skip_pinned:
            if key in raising_keys or key in unreadable_keys:
                return None
            meta = metadata.get(key, {})
            if not isinstance(meta, dict) or meta.get("pinned"):
                return None
        deleted_keys.append(key)
        return True

    conv_log.delete_session.side_effect = _delete

    state = MagicMock()
    state.conversation_log = conv_log
    state._slots = slots or {}
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    return state, deleted_keys


def _request(state: MagicMock) -> web.Request:
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.query = {}
    return request


async def _call_count(state: MagicMock) -> tuple[int, dict]:
    resp = await api_sessions_clearable_count(_request(state))
    return resp.status, json.loads(resp.body.decode("utf-8"))


# ── what the selector counts ──


@pytest.mark.asyncio
async def test_no_conversation_log_is_a_bad_request() -> None:
    """Carries a machine-readable ``code``, which the error-code ratchet requires
    of any new error response and a frontend needs in order to branch."""
    state = MagicMock()
    state.conversation_log = None

    status, body = await _call_count(state)

    assert status == 400
    assert body["code"] == "count_unavailable"


@pytest.mark.asyncio
async def test_reports_only_the_count() -> None:
    """The response body is exactly the number.

    ``skipped`` and an echoed threshold both had zero consumers, and the endpoint
    promises the number and nothing else. Pinned so a future field has to justify
    itself against a caller.
    """
    state, _ = _fake_state([{"key": "a"}, {"key": "b"}])

    status, body = await _call_count(state)

    assert status == 200
    assert body == {"sessions": 2}


def test_open_tab_is_excluded_and_reported_as_skipped() -> None:
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    state, _ = _fake_state([{"key": k1}, {"key": k2}], slots={"chat-1": _FakeSlot("chat-1")})

    clearable, skipped = _clearable_history_keys(state, state.conversation_log)

    assert clearable == [k2]
    assert skipped == 1


def test_pinned_session_is_excluded() -> None:
    state, _ = _fake_state([{"key": "a"}, {"key": "b"}], metadata={"a": {"pinned": True}})

    clearable, skipped = _clearable_history_keys(state, state.conversation_log)

    assert clearable == ["b"]
    assert skipped == 1


def test_unreadable_metadata_is_excluded() -> None:
    """A transient read failure must not read as permission to delete."""
    state, _ = _fake_state([{"key": "a"}, {"key": "b"}], unreadable_keys={"a"})

    clearable, skipped = _clearable_history_keys(state, state.conversation_log)

    assert clearable == ["b"]
    assert skipped == 1


def test_unexpected_metadata_failure_is_excluded() -> None:
    """Not reachable through ``get_metadata_status``'s documented returns, but a
    genuinely unexpected failure must still not read as permission to delete."""
    state, _ = _fake_state([{"key": "a"}, {"key": "b"}], raising_keys={"a"})

    clearable, skipped = _clearable_history_keys(state, state.conversation_log)

    assert clearable == ["b"]
    assert skipped == 1


def test_rows_without_a_key_are_ignored() -> None:
    state, _ = _fake_state([{"key": ""}, {}, {"key": "b"}])

    clearable, _skipped = _clearable_history_keys(state, state.conversation_log)

    assert clearable == ["b"]


@pytest.mark.asyncio
async def test_unparseable_metadata_is_excluded_from_count_and_bulk_delete(
    tmp_path,
) -> None:
    """Unreadable identity cannot authorize deletion or inflate its preview."""
    log = ConversationLog(base_dir=tmp_path)
    await asyncio.to_thread(log.append, "malformed", "user", "hello")
    path = tmp_path / "malformed.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = "{this is not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = path.read_bytes()

    # Broken JSON is unreadable even though the display projection is empty.
    meta, readable = log.get_metadata_status("malformed")
    assert readable is False
    assert meta == {}

    state = MagicMock()
    state.conversation_log = log
    state._slots = {}

    status, body = await _call_count(state)

    assert status == 200
    assert body == {"sessions": 0}
    with patch("kiro_crew.dashboard.handlers.sel"):
        resp = await api_sessions_clear(_request(state))
    assert resp.status == 200
    assert json.loads(resp.body)["cleared"] == 0
    assert await asyncio.to_thread(log.delete_session, "malformed", skip_pinned=True) is None
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_an_individual_delete_can_remove_unparseable_metadata(tmp_path) -> None:
    """A corrupt session excluded from bulk clear still has an explicit recovery path."""
    log = ConversationLog(base_dir=tmp_path)
    await asyncio.to_thread(log.append, "malformed", "user", "hello")
    await asyncio.to_thread(log.append, "keep", "user", "keep this conversation")
    path = tmp_path / "malformed.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = "{this is not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    survivor = tmp_path / "keep.jsonl"
    survivor_before = survivor.read_bytes()
    assert await asyncio.to_thread(log.get_metadata_status, "malformed") == ({}, False)

    state = MagicMock()
    state.conversation_log = log
    request = _request(state)
    request.match_info = {"key": "malformed"}
    # Slot/provider teardown has its own coverage. Keep the HTTP handler and the
    # locked transcript deletion real, including the metadata readability check.
    with patch(
        "kiro_crew.dashboard.handlers.sessions._remove_slot_for_history_key",
        new_callable=AsyncMock,
    ) as remove_slot:
        response = await api_session_delete(request)

    assert response.status == 200
    assert json.loads(response.body) == {"ok": True}
    assert not path.exists()
    assert survivor.read_bytes() == survivor_before
    remove_slot.assert_awaited_once()
    assert remove_slot.await_args.args == (state, "malformed")
    claim = remove_slot.await_args.kwargs["delete_claim"]
    assert claim.slot is None
    state.push_refresh.assert_called_once_with("history")


# ── one selector: the count and the delete agree ──


@pytest.mark.asyncio
async def test_count_and_bulk_delete_resolve_the_same_set() -> None:
    """The property that makes the count worth displaying.

    Same fixture through both paths: whatever the selector reports is exactly what
    the delete removes, named explicitly on both sides. This is the only test in
    the suite that can see the two drifting apart — the delete's own suite stays
    green if the selector's pinned exclusion is removed, because the atomic
    ``skip_pinned`` still catches it and the counts work out identically.
    """
    k_open, k_pinned = _history_key_for("chat-1"), "pinned-one"
    sessions = [{"key": k_open}, {"key": k_pinned}, {"key": "plain-a"}, {"key": "plain-b"}]
    slots = {"chat-1": _FakeSlot("chat-1")}
    metadata = {k_pinned: {"pinned": True}}

    state, _ = _fake_state(sessions, slots=slots, metadata=metadata)
    counted, _skipped = _clearable_history_keys(state, state.conversation_log)

    delete_state, deleted = _fake_state(sessions, slots=slots, metadata=metadata)
    with (
        patch(
            "kiro_crew.dashboard.handlers._remove_slot_for_history_key",
            new=AsyncMock(return_value=None),
        ),
        patch("kiro_crew.dashboard.handlers.sel"),
    ):
        resp = await api_sessions_clear(_request(delete_state))
    assert resp.status == 200

    assert set(counted) == {"plain-a", "plain-b"}
    assert set(deleted) == {"plain-a", "plain-b"}
    assert set(counted) == set(deleted)


# ── the endpoint is read-only, proven by naming the survivors ──


@pytest.mark.asyncio
async def test_counting_never_calls_delete_session() -> None:
    state, deleted = _fake_state([{"key": "a"}, {"key": "b"}])

    status, body = await _call_count(state)

    assert status == 200
    assert body == {"sessions": 2}
    assert state.conversation_log.delete_session.call_count == 0
    assert deleted == []


@pytest.mark.asyncio
async def test_counting_leaves_every_session_file_byte_identical(tmp_path) -> None:
    """The survivor assertion, against real files.

    Named survivors with their bytes and mtimes, rather than "nothing was
    deleted" — the latter is satisfied by an empty directory and so proves
    nothing about a path that was supposed to leave two files alone.
    """
    log = ConversationLog(base_dir=tmp_path)
    log.append("older-a", "user", "first")
    log.append("older-b", "user", "second")

    paths = {key: tmp_path / f"{key}.jsonl" for key in ("older-a", "older-b")}
    before = {key: (path.read_bytes(), path.stat().st_mtime_ns) for key, path in paths.items()}

    state = MagicMock()
    state.conversation_log = log
    state._slots = {}

    status, body = await _call_count(state)

    assert status == 200
    assert body == {"sessions": 2}

    for key in ("older-a", "older-b"):
        assert paths[key].exists(), f"{key} must still be on disk after a count"
        assert paths[key].read_bytes() == before[key][0], f"{key} content changed"
        assert paths[key].stat().st_mtime_ns == before[key][1], f"{key} mtime changed"


@pytest.mark.asyncio
async def test_counting_does_not_clear_the_closed_marker(tmp_path) -> None:
    """Resuming an archived session clears ``closed`` on disk. Counting must not:
    a path that can modify anything is not a read.
    """
    log = ConversationLog(base_dir=tmp_path)
    log.append("archived", "user", "hello")
    path = tmp_path / "archived.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["closed"] = True
    meta["closed_at"] = CLOSED_AT
    lines[0] = json.dumps(meta)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state = MagicMock()
    state.conversation_log = log
    state._slots = {}

    status, _body = await _call_count(state)
    assert status == 200

    after = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert after["closed"] is True, "closed must survive a count"
    assert after["closed_at"] == CLOSED_AT, "closed_at must survive a count"
