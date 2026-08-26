"""Tests for ``api_sessions_clear`` scope.

Regression guard for ``DELETE /api/sessions`` used to
unconditionally delete ALL history sessions, including pinned ones.
The handler is now history-only — it skips:

- any slot currently open in the sidebar (pinned or not, running or idle),
- any session whose on-disk metadata has ``pinned=True``.

Bulk-archiving *open* unpinned/idle sessions is out of scope and is
tracked separately by (Clean Up button).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import api_sessions_clear


def _history_key_for(key: str) -> str:
    from kiro_crew.dashboard.chat import _history_key_for as _hkf
    return _hkf(key)


class _FakeSlot:
    """Minimal stand-in for ``_ChatSlot`` — carries only what the handler reads."""

    def __init__(
        self,
        key: str,
        *,
        pinned: bool = False,
        running: bool = False,
        linked_session_key: str = "",
        channel_origin: bool = False,
    ) -> None:
        self.key = key
        self.pinned = pinned
        self._running = running
        # The handler resolves each slot's TRANSCRIPT via ``slot_history_key``,
        # which reads these. A channel tab the session map could not resolve
        # carries no linked key, and its transcript is identified by the
        # ``channel_origin`` provenance flag plus the slot name.
        self.linked_session_key = linked_session_key
        self.channel_origin = channel_origin

    @property
    def running(self) -> bool:
        return self._running


def _make_request(
    sessions: list[dict],
    *,
    slots: dict[str, _FakeSlot] | None = None,
    metadata: dict[str, dict] | None = None,
) -> tuple[web.Request, MagicMock, list[str]]:
    """Build a minimal ``web.Request`` with a fake ``conversation_log`` + ``_slots``.

    Returns (request, state, deleted_keys) where ``deleted_keys`` is populated
    by ``delete_session`` so tests can assert exactly which keys were removed.
    """
    deleted_keys: list[str] = []
    metadata = metadata or {}

    conv_log = MagicMock()
    conv_log.list_sessions.return_value = sessions
    conv_log.get_metadata.side_effect = lambda k: metadata.get(k, {})

    def _delete(key: str) -> bool:
        deleted_keys.append(key)
        return True

    conv_log.delete_session.side_effect = _delete

    state = MagicMock()
    state.conversation_log = conv_log
    state._slots = slots or {}
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()

    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    return request, state, deleted_keys


async def _call_and_parse(request: web.Request) -> tuple[int, dict]:
    """Invoke the handler and return (status, JSON body)."""
    from unittest.mock import patch

    with patch(
        "kiro_crew.dashboard.handlers._remove_slot_for_history_key",
        new=AsyncMock(return_value=None),
    ), patch("kiro_crew.dashboard.handlers.sel"):
        resp = await api_sessions_clear(request)
    return resp.status, json.loads(resp.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_clears_all_when_nothing_protected() -> None:
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    request, _state, deleted = _make_request(sessions)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 2, "skipped": 0, "failed": 0}
    assert set(deleted) == {k1, k2}


@pytest.mark.asyncio
async def test_skips_pinned_slot_in_memory() -> None:
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    slots = {"chat-1": _FakeSlot("chat-1", pinned=True)}
    request, _state, deleted = _make_request(sessions, slots=slots)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}
    assert deleted == [k2]


@pytest.mark.asyncio
async def test_skips_running_slot_in_memory() -> None:
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    slots = {"chat-1": _FakeSlot("chat-1", running=True)}
    request, _state, deleted = _make_request(sessions, slots=slots)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}
    assert deleted == [k2]


@pytest.mark.asyncio
async def test_skips_pinned_via_on_disk_metadata() -> None:
    """Pinned session that exists only on disk (no in-memory slot) is protected."""
    k_old, k2 = _history_key_for("chat-old"), _history_key_for("chat-2")
    sessions = [{"key": k_old}, {"key": k2}]
    metadata = {k_old: {"pinned": True}}
    request, _state, deleted = _make_request(sessions, metadata=metadata)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}
    assert deleted == [k2]


@pytest.mark.asyncio
async def test_returns_400_when_no_conversation_log() -> None:
    state = MagicMock()
    state.conversation_log = None
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}

    resp = await api_sessions_clear(request)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_skips_any_open_slot_even_if_unpinned_and_idle() -> None:
    """Any slot present in ``state._slots`` is protected — Clear All is history-only.

    Bulk-archiving *open* unpinned/idle sessions is the upstream project's job (Clean Up
    button), not this handler's. See scope.
    """
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    slots = {"chat-1": _FakeSlot("chat-1", pinned=False, running=False)}
    request, _state, deleted = _make_request(sessions, slots=slots)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}
    assert deleted == [k2]


@pytest.mark.asyncio
async def test_none_metadata_does_not_crash() -> None:
    """get_metadata returning None (corrupt/missing file) skips session (deny-by-default)."""
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    metadata = {k1: None}  # simulate corrupt metadata
    request, _state, deleted = _make_request(sessions, metadata=metadata)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}
    assert deleted == [k2]


@pytest.mark.asyncio
async def test_skips_open_slot_with_filesystem_underscore_key() -> None:
    """list_sessions() returns underscore keys (dashboard_chat-X) from path.stem,
    but _history_key_for returns colon keys (dashboard:chat-X). The handler must
    protect both formats so open sessions aren't deleted.

    Regression test for the key format mismatch bug found during testing.
    """
    # Simulate what list_sessions actually returns: underscore format from filesystem
    fs_key_1 = _history_key_for("chat-1-123").replace(":", "_", 1)  # open in sidebar
    fs_key_2 = _history_key_for("chat-2-456").replace(":", "_", 1)  # not open
    sessions = [{"key": fs_key_1}, {"key": fs_key_2}]
    # Slot key is the raw form without prefix
    slots = {"chat-1-123": _FakeSlot("chat-1-123", pinned=False, running=False)}
    request, _state, deleted = _make_request(sessions, slots=slots)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}
    assert deleted == [fs_key_2]


@pytest.mark.asyncio
async def test_skips_all_sessions_no_refresh() -> None:
    """When every session is protected, nothing is cleared and no UI refresh fires."""
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    slots = {
        "chat-1": _FakeSlot("chat-1", pinned=True),
        "chat-2": _FakeSlot("chat-2", running=True),
    }
    request, state, deleted = _make_request(sessions, slots=slots)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 0, "skipped": 2, "failed": 0}
    assert deleted == []
    state.push_slots_update.assert_not_called()
    state.push_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_skips_session_when_metadata_raises() -> None:
    """If get_metadata raises (corrupt JSON), the session is skipped, not deleted."""
    k1 = _history_key_for("chat-1")
    k2 = _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    request, state, deleted = _make_request(sessions)

    # k1 raises, k2 returns normal metadata
    def _meta(key: str) -> dict:
        if key == k1:
            raise json.JSONDecodeError("bad", "", 0)
        return {}

    state.conversation_log.get_metadata.side_effect = _meta

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}
    assert deleted == [k2]


@pytest.mark.asyncio
async def test_delete_failure_tracked_as_failed() -> None:
    """When delete_session returns False the session counts as failed, not cleared."""
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    request, state, _ = _make_request(sessions)

    # k1 succeeds, k2 fails
    state.conversation_log.delete_session.side_effect = lambda k: k == k1

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": False, "cleared": 1, "skipped": 0, "failed": 1}


@pytest.mark.asyncio
async def test_delete_exception_tracked_as_failed() -> None:
    """When delete_session raises, the session counts as failed and loop continues."""
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    request, state, _ = _make_request(sessions)

    def _delete(key: str) -> bool:
        if key == k1:
            raise PermissionError("access denied")
        return True

    state.conversation_log.delete_session.side_effect = _delete

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": False, "cleared": 1, "skipped": 0, "failed": 1}


@pytest.mark.asyncio
async def test_all_failed_returns_ok_false() -> None:
    """When every deletion fails, ok=False but status is still 200."""
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    request, state, _ = _make_request(sessions)
    state.conversation_log.delete_session.side_effect = lambda k: False

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": False, "cleared": 0, "skipped": 0, "failed": 2}


@pytest.mark.asyncio
async def test_skips_both_candidate_transcripts_of_a_channel_shaped_slot() -> None:
    """Clear All must not depend on provenance resolving correctly.

    A legacy channel tab carries no persisted marker, so it restores as an
    ordinary dashboard slot writing ``dashboard:<stem>`` while the conversation
    on screen still lives in the channel transcript. Protecting only the write
    target would delete what the tab is displaying, so BOTH candidates are
    protected -- the worst case is skipping a transcript nobody is reading.
    """
    stem = "slack_1783733803.877979"
    other = _history_key_for("chat-9-1")
    sessions = [{"key": stem}, {"key": _history_key_for(stem)}, {"key": other}]
    slots = {stem: _FakeSlot(stem)}  # no channel_origin, no linked key
    request, _state, deleted = _make_request(sessions, slots=slots)

    status, _body = await _call_and_parse(request)

    assert status == 200
    assert stem not in deleted
    assert deleted == [other]


@pytest.mark.asyncio
async def test_skips_the_transcript_an_unbound_channel_tab_is_reading() -> None:
    """An open channel tab's transcript must survive Clear All.

    A channel tab the session map could not resolve carries no
    ``linked_session_key``, so it RUNS under ``dashboard:<stem>`` while its
    conversation lives in the channel transcript, listed as the bare stem. The
    protection set used to be built from the session key, which contributed two
    names matching no file and left the real transcript unprotected — so Clear
    All permanently deleted the conversation the open tab was displaying.
    """
    stem = "slack_1783733803.877979"
    other = _history_key_for("chat-9-1")
    sessions = [{"key": stem}, {"key": other}]
    slots = {stem: _FakeSlot(stem, channel_origin=True)}
    request, _state, deleted = _make_request(sessions, slots=slots)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert stem not in deleted
    assert deleted == [other]
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}


@pytest.mark.asyncio
async def test_skips_the_transcript_a_bound_channel_tab_is_reading() -> None:
    """Same protection for the tab that DID resolve — via its linked key's stem."""
    stem = "slack_1783733803.877979"
    other = _history_key_for("chat-9-1")
    sessions = [{"key": stem}, {"key": other}]
    slots = {stem: _FakeSlot(stem, linked_session_key="slack:1783733803.877979")}
    request, _state, deleted = _make_request(sessions, slots=slots)

    status, body = await _call_and_parse(request)

    assert status == 200
    assert deleted == [other]
