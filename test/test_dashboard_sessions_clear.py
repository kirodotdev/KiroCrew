"""Tests for ``api_sessions_clear`` scope.

``DELETE /api/sessions`` must not delete ALL history sessions, pinned ones
included. The handler is history-only — it skips:

- any slot currently open in the sidebar (pinned or not, running or idle),
- any session whose on-disk metadata has ``pinned=True``.

Bulk-archiving *open* unpinned/idle sessions is out of scope and is
tracked separately by (Clean Up button).
"""

from __future__ import annotations

import contextlib
import json
from typing import Iterator
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
    unreadable_keys: set[str] | None = None,
    raising_keys: set[str] | None = None,
) -> tuple[web.Request, MagicMock, list[str]]:
    """Build a minimal ``web.Request`` with a fake ``conversation_log`` + ``_slots``.

    Returns (request, state, deleted_keys) where ``deleted_keys`` is populated
    by ``delete_session`` so tests can assert exactly which keys were removed.

    Args:
        unreadable_keys: Keys for which get_metadata_status returns ({}, False)
            simulating a transient read failure (Windows indexer/AV hold).
        raising_keys: Keys for which get_metadata_status raises an exception
            simulating corrupt metadata.
    """
    deleted_keys: list[str] = []
    metadata = metadata or {}
    unreadable_keys = unreadable_keys or set()
    raising_keys = raising_keys or set()

    conv_log = MagicMock()
    # Like the real catalog, a listing taken after a delete omits the deleted key.
    conv_log.list_sessions.side_effect = lambda *a, **kw: [
        s for s in sessions if s.get("key") not in deleted_keys
    ]
    conv_log.get_metadata.side_effect = lambda k: metadata.get(k, {})

    def _get_metadata_status(k: str) -> tuple[dict, bool]:
        if k in raising_keys:
            raise json.JSONDecodeError("bad", "", 0)
        if k in unreadable_keys:
            return {}, False  # transient read failure
        return metadata.get(k, {}), True

    conv_log.get_metadata_status.side_effect = _get_metadata_status

    # Mock _locked as a reentrant context manager (no-op for tests)
    @contextlib.contextmanager
    def _locked_mock(k: str) -> Iterator[None]:
        yield

    conv_log._locked = _locked_mock

    def _delete(key: str, *, skip_pinned: bool = False) -> bool | None:
        """Mock delete_session returning canned values per key.

        The skip_pinned invariant (pinned/unreadable/raising -> None) is
        already tested by 4 real-lock tests in test_history.py. Here we
        just return canned values so api_sessions_clear's counting is
        exercised.
        """
        if skip_pinned:
            if key in raising_keys or key in unreadable_keys:
                return None
            meta = metadata.get(key, {})
            if not isinstance(meta, dict):
                return None  # corrupt metadata -> skip
            if meta.get("pinned"):
                return None
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
    """If get_metadata_status raises (corrupt JSON), the session is skipped, not deleted."""
    k1 = _history_key_for("chat-1")
    k2 = _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    # k1 raises (via raising_keys), k2 returns normal metadata
    request, state, deleted = _make_request(sessions, raising_keys={k1})

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

    # k1 succeeds, k2 fails (simulating unlink failure)
    def _delete(key: str, *, skip_pinned: bool = False) -> bool | None:
        return key == k1

    state.conversation_log.delete_session.side_effect = _delete

    status, body = await _call_and_parse(request)

    assert status == 200
    assert body == {"ok": False, "cleared": 1, "skipped": 0, "failed": 1}


@pytest.mark.asyncio
async def test_delete_exception_tracked_as_failed() -> None:
    """When delete_session raises, the session counts as failed and loop continues."""
    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    sessions = [{"key": k1}, {"key": k2}]
    request, state, _ = _make_request(sessions)

    def _delete(key: str, *, skip_pinned: bool = False) -> bool | None:
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
    state.conversation_log.delete_session.side_effect = lambda k, *, skip_pinned=False: False

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
    conversation lives in the channel transcript, listed as the bare stem. A
    protection set built from the session key contributes two names matching no
    file and leaves the real transcript unprotected, so Clear All permanently
    deletes the conversation the open tab is displaying.
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


@pytest.mark.asyncio
async def test_skips_session_with_transient_unreadable_metadata() -> None:
    """A session whose metadata is transiently unreadable is SKIPPED, not deleted.

    Regression test for data-loss bug: get_metadata() returns {} on transient
    read failure (Windows indexer/AV holding the file) WITHOUT raising, so
    {}.get("pinned") is falsy and a PINNED session gets permanently deleted.

    The fix is to use get_metadata_status() which returns (meta, readable=False)
    on transient failure, and skip when not readable.
    """
    k_pinned = _history_key_for("chat-pinned")
    k_normal = _history_key_for("chat-normal")
    sessions = [{"key": k_pinned}, {"key": k_normal}]
    # k_pinned is actually pinned on disk, but its metadata is transiently unreadable
    metadata = {k_pinned: {"pinned": True}, k_normal: {}}
    # Simulate transient read failure for k_pinned
    request, _state, deleted = _make_request(
        sessions, metadata=metadata, unreadable_keys={k_pinned}
    )

    status, body = await _call_and_parse(request)

    assert status == 200
    # k_pinned should be SKIPPED (unreadable), not deleted
    assert k_pinned not in deleted, "Pinned session with unreadable metadata was deleted!"
    assert deleted == [k_normal]
    assert body == {"ok": True, "cleared": 1, "skipped": 1, "failed": 0}


@pytest.mark.asyncio
async def test_bulk_clear_reaps_untouched_image_copies_of_deleted_sessions() -> None:
    """Chat images are exempt from the widget sweep, so a permanent delete is their
    only reclamation path — the bulk clear must take them with the transcript,
    exactly like the single-session delete, and only for the sessions it removed."""
    from unittest.mock import patch

    k1, k2 = _history_key_for("chat-1"), _history_key_for("chat-2")
    k_pinned = _history_key_for("chat-pinned")
    sessions = [{"key": k1}, {"key": k2}, {"key": k_pinned}]
    request, _state, deleted = _make_request(sessions, metadata={k_pinned: {"pinned": True}})

    store = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sessions.get_default_store", return_value=store):
        status, body = await _call_and_parse(request)

    assert status == 200
    assert body["cleared"] == 2 and set(deleted) == {k1, k2}
    store.delete_auto_images_for_sessions.assert_called_once()
    (keys,), _ = store.delete_auto_images_for_sessions.call_args
    # The bare spelling of each deleted session is present; the pinned one is not.
    assert {"chat-1", "chat-2"} <= keys
    assert not any("chat-pinned" in k for k in keys)


@pytest.mark.asyncio
async def test_bulk_clear_with_nothing_deleted_does_not_touch_the_store() -> None:
    from unittest.mock import patch

    k_pinned = _history_key_for("chat-pinned")
    request, _state, _deleted = _make_request(
        [{"key": k_pinned}], metadata={k_pinned: {"pinned": True}}
    )
    with patch("kiro_crew.dashboard.handlers.sessions.get_default_store") as gds:
        await _call_and_parse(request)
    gds.assert_not_called()


def test_every_permanent_delete_handler_reaps_image_copies() -> None:
    """Chat images are exempt from the widget sweep, so the per-session reap is
    their ONLY reclamation. This pins the invariant in code: every dashboard
    function that calls ``delete_session`` on the conversation log must also
    call ``_reap_session_images`` — unless it is listed below with the reason
    it owns no image copies. Adding a new permanent-delete path without the
    reap (or without a reasoned entry here) fails this test."""
    import ast
    import inspect

    from kiro_crew.dashboard import chat_fork
    from kiro_crew.dashboard.handlers import sessions

    # The fork rollback removes a transcript that was never acknowledged and
    # never ran a turn, so `register_images` never wrote a copy under its key.
    exempt = {"chat_fork.api_chat_slot_fork"}

    deleting: dict[str, bool] = {}
    for mod in (sessions, chat_fork):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            # `delete_session` is handed to asyncio.to_thread rather than called
            # directly, so look at every name/attribute the body references.
            names = {
                n.attr if isinstance(n, ast.Attribute) else n.id
                for n in ast.walk(node)
                if isinstance(n, (ast.Attribute, ast.Name))
            }
            if "delete_session" in names:
                qual = f"{mod.__name__.rsplit('.', 1)[-1]}.{node.name}"
                deleting[qual] = "_reap_session_images" in names or qual in exempt
    assert set(deleting) == {
        "sessions.api_session_delete",
        "sessions.api_sessions_clear",
        "chat_fork.api_chat_slot_fork",
    }, deleting
    assert all(deleting.values()), deleting


def test_image_session_key_spellings_match_what_register_images_records() -> None:
    """``register_images`` records the bare ``slot.key``; the delete handlers get
    the transcript's history key or file stem. The lookup set must contain the
    bare form for either input and nothing invented."""
    from kiro_crew.dashboard.handlers.sessions import _image_artifact_session_keys

    assert _image_artifact_session_keys({"dashboard:chat-1"}) == {"dashboard:chat-1", "chat-1"}
    assert _image_artifact_session_keys({"dashboard_chat-1"}) == {"dashboard_chat-1", "chat-1"}
    assert _image_artifact_session_keys({"chat-1", ""}) == {"chat-1"}
    # A cron transcript is `cron:<id>` / `cron_<id>`; its copies are owned by the
    # tab, `cron-<id>`. Deleting the transcript must reach them.
    assert "cron-42" in _image_artifact_session_keys({"cron:42"})
    assert "cron-42" in _image_artifact_session_keys({"cron_42"})


# --- fork lineage and the image reap -------------------------------------
#
# A fork copies messages with their ts intact and an image copy's slug derives
# from ts alone, so a fork renders its source's artifacts. The reap must never
# remove a copy a surviving descendant still renders, must fail closed when it
# cannot know, and must eventually reclaim ancestors once their line dies out.


def _catalog(
    sessions: dict[str, str | list[str] | None],
    unreadable: set[str] = frozenset(),
    *,
    materialized: bool = True,
) -> MagicMock:
    """A conversation-log stand-in: ``{bare_key: forked_from | full_chain | None}``.

    A ``str`` value is the immediate source; the persisted chain is derived from
    the dict while its links are present. A ``list`` value is the chain exactly
    as the fork's own record carries it (nearest first) — the production shape
    that survives deletion of an intermediate. With ``materialized=False`` only
    ``forked_from`` is written, the shape of forks made before the chain was
    recorded.
    """
    log = MagicMock()
    log.list_sessions.return_value = [{"key": _history_key_for(k)} for k in sessions]

    def _parent(bare: str) -> str | None:
        v = sessions.get(bare)
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def _chain(bare: str) -> list[str]:
        v = sessions.get(bare)
        if isinstance(v, list):
            return [_history_key_for(x) for x in v]
        out: list[str] = []
        cur = v
        while cur and cur not in out:
            out.append(cur)
            cur = _parent(cur)
        return [_history_key_for(x) for x in out]

    def _status(key: str) -> tuple[dict, bool]:
        bare = key.removeprefix("dashboard:").removeprefix("dashboard_")
        if bare in unreadable:
            return {}, False
        parent = _parent(bare)
        if not parent:
            return {}, True
        # The fork handler records `effective_session_key(source)`, i.e. the
        # history key (`dashboard:<slot>`), never a bare slot name.
        meta: dict = {"forked_from": _history_key_for(parent)}
        if materialized:
            meta["fork_ancestors"] = _chain(bare)
        return meta, True

    log.get_metadata_status.side_effect = _status
    return log


def _reaped(before_log: MagicMock, after_log: MagicMock, deleted: set[str]) -> set[str] | None:
    """Run the reap the way a handler does and return the bare keys it reaped
    (``None`` when it did not touch the store)."""
    import asyncio
    from unittest.mock import patch

    from kiro_crew.dashboard.handlers.sessions import _fork_lineage, _reap_session_images

    before = _fork_lineage(before_log)
    store = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sessions.get_default_store", return_value=store):
        asyncio.run(_reap_session_images(after_log, before, {_history_key_for(k) for k in deleted}))
    if not store.delete_auto_images_for_sessions.called:
        return None
    (keys,), _ = store.delete_auto_images_for_sessions.call_args
    # Reduce every spelling the store was handed to the bare session name.
    return {k.removeprefix("dashboard:").removeprefix("dashboard_") for k in keys}


def test_channel_transcript_stems_and_live_keys_fold_to_one_lineage() -> None:
    """The catalog lists a channel transcript by its file stem (``slack_<ts>``)
    while a fork's ``forked_from`` carries the live key (``slack:<ts>``). Both
    must fold to the same lineage node, or the fork's protection is missed."""
    import asyncio
    from unittest.mock import patch

    from kiro_crew.dashboard.handlers.sessions import _fork_lineage, _reap_session_images

    def _log(entries: dict[str, str | None]) -> MagicMock:
        log = MagicMock()
        log.list_sessions.return_value = [{"key": k} for k in entries]

        def _status(key: str) -> tuple[dict, bool]:
            parent = entries.get(key)
            return ({"forked_from": parent} if parent else {}), True

        log.get_metadata_status.side_effect = _status
        return log

    before = _log({"slack_1700.42": None, "dashboard_fork": "slack:1700.42"})
    after = _log({"dashboard_fork": "slack:1700.42"})
    store = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sessions.get_default_store", return_value=store):
        asyncio.run(_reap_session_images(after, _fork_lineage(before), {"slack:1700.42"}))
    store.delete_auto_images_for_sessions.assert_not_called()

    # A Slack thread that predates the canonical key still logs under its bare
    # thread_ts stem; the fork's `forked_from` names the canonical key. They
    # must still meet as one lineage node.
    before = _log({"1700.42": None, "dashboard_fork": "slack:1700.42"})
    after = _log({"dashboard_fork": "slack:1700.42"})
    store = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sessions.get_default_store", return_value=store):
        asyncio.run(_reap_session_images(after, _fork_lineage(before), {"slack:1700.42"}))
    store.delete_auto_images_for_sessions.assert_not_called()


def test_deleting_a_fork_source_keeps_images_a_surviving_fork_still_renders() -> None:
    before = _catalog({"src": None, "fork": "src"})
    after = _catalog({"fork": "src"})
    assert _reaped(before, after, {"src"}) is None


def test_deleting_source_and_fork_together_reaps_both() -> None:
    before = _catalog({"src": None, "fork": "src"})
    after = _catalog({})
    assert _reaped(before, after, {"src", "fork"}) == {"src", "fork"}


def test_fork_chain_protects_the_grandparent_through_a_deleted_intermediate() -> None:
    before = _catalog({"a": None, "b": "a", "c": "b"})
    after = _catalog({"c": "b"})
    assert _reaped(before, after, {"a", "b"}) is None


def test_deleting_the_last_fork_reaps_the_ancestor_kept_for_it() -> None:
    # `src` was deleted earlier while `fork` survived, so its copies were kept.
    # Now `fork` goes: nothing descends from `src` any more -> reap both lines.
    before = _catalog({"fork": "src"})  # src is already gone from the catalog
    after = _catalog({})
    assert _reaped(before, after, {"fork"}) == {"fork", "src"}


def test_a_sibling_fork_keeps_the_shared_ancestor_alive() -> None:
    before = _catalog({"f1": "src", "f2": "src"})  # src already gone
    after = _catalog({"f2": "src"})
    assert _reaped(before, after, {"f1"}) == {"f1"}


def test_unreadable_fork_metadata_fails_closed() -> None:
    # `fork` may descend from `src`; we cannot tell -> reap nothing.
    before = _catalog({"src": None, "fork": None}, unreadable={"fork"})
    after = _catalog({"fork": None}, unreadable={"fork"})
    assert _reaped(before, after, {"src"}) is None


def test_a_fork_acknowledged_after_the_snapshot_still_protects_its_source() -> None:
    before = _catalog({"src": None})  # fork not yet in the catalog
    after = _catalog({"fork": "src"})  # it landed between snapshot and delete
    assert _reaped(before, after, {"src"}) is None


def test_a_session_recreated_under_the_deleted_key_is_not_reaped() -> None:
    """The post-delete read is authoritative: a stem back in the catalog was
    recreated under the same key while the delete ran (a channel session that
    received a new message), and the new incarnation's images must stay."""
    before = _catalog({"chan": None, "other": None})
    after = _catalog({"chan": None})  # recreated during cleanup
    assert _reaped(before, after, {"chan", "other"}) == {"other"}


def test_reap_handles_a_forked_from_cycle_without_hanging() -> None:
    before = _catalog({"a": "b", "b": "a", "c": None})
    after = _catalog({"a": "b", "b": "a"})
    assert _reaped(before, after, {"c"}) == {"c"}


def test_a_very_deep_fork_chain_still_protects_its_root() -> None:
    # root <- f1 <- f2 <- … <- f200 (only the leaf survives): deleting the root
    # and every intermediate must reap nothing — no depth cap may cut the walk.
    chain = {"root": None}
    prev = "root"
    for i in range(1, 201):
        chain[f"f{i}"] = prev
        prev = f"f{i}"
    before = _catalog(chain)
    after = _catalog({"f200": "f199"})
    deleted = set(chain) - {"f200"}
    assert _reaped(before, after, deleted) is None


def test_root_kept_earlier_stays_protected_after_the_intermediate_is_gone() -> None:
    # A <- B <- C. A was deleted earlier (kept for B/C). Now B is deleted while C
    # survives. B's record is gone, but C's own `fork_ancestors` names A, so A
    # remains protected and only B is reaped.
    before = _catalog({"b": ["a"], "c": ["b", "a"]})  # a already absent
    after = _catalog({"c": ["b", "a"]})
    assert _reaped(before, after, {"b"}) is None  # b is c's ancestor too


def test_legacy_forks_without_a_materialized_chain_still_use_the_snapshot() -> None:
    # Forks made before `fork_ancestors` existed carry only `forked_from`; the
    # pre-delete snapshot still links the chain for the delete that follows.
    before = _catalog({"a": None, "b": "a", "c": "b"}, materialized=False)
    after = _catalog({"c": "b"}, materialized=False)
    assert _reaped(before, after, {"a", "b"}) is None


@pytest.mark.asyncio
async def test_single_delete_quiesces_the_slot_before_draining_and_reaping() -> None:
    """A turn that finalizes AFTER the drain snapshot would register a copy behind
    the reap. Removing the slot first (which kills its kiro-cli session) closes
    that window, so the order must be: remove slot -> drain -> reap."""
    from unittest.mock import AsyncMock, patch

    from kiro_crew.dashboard.handlers import api_session_delete

    order: list[str] = []
    conv_log = MagicMock()
    conv_log.delete_session.return_value = True
    conv_log.list_sessions.return_value = []
    state = MagicMock()
    state.conversation_log = conv_log
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.match_info = {"key": _history_key_for("chat-1")}

    async def _remove(_state: object, _key: str) -> None:
        order.append("remove_slot")

    async def _drain(_keys: set[str], **_kw: object) -> bool:
        order.append("drain")
        return True

    store = MagicMock()
    store.delete_auto_images_for_sessions.side_effect = lambda _k: order.append("reap") or 0
    with (
        patch("kiro_crew.dashboard.handlers.sessions._remove_slot_for_history_key", new=_remove),
        patch(
            "kiro_crew.dashboard.handlers.sessions.drain_registrations",
            new=AsyncMock(side_effect=_drain),
        ),
        patch("kiro_crew.dashboard.handlers.sessions.get_default_store", return_value=store),
    ):
        resp = await api_session_delete(request)
    assert resp.status == 200
    assert order == ["remove_slot", "drain", "reap"]


@pytest.mark.asyncio
async def test_a_slow_registration_defers_the_reap_instead_of_dropping_it(monkeypatch) -> None:
    """If an image registration outlives the delete request's bounded wait, the
    reap must run once it finishes — never be skipped — or the late copy would
    outlive its deleted transcript for good."""
    import asyncio
    from unittest.mock import patch

    from kiro_crew import image_artifacts as ia
    from kiro_crew.dashboard.handlers import sessions as mod

    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    ia.track_registration("chat-slow", task)

    # First wait is capped very short so the delete path takes the deferred branch.
    real_drain = ia.drain_registrations

    async def _short_first(keys: set[str], *, timeout: float | None = 30.0) -> bool:
        return await real_drain(keys, timeout=0.01 if timeout is not None else None)

    monkeypatch.setattr(mod, "drain_registrations", _short_first)
    store = MagicMock()
    lineage = mod._fork_lineage(_catalog({"slow": None}))
    with patch("kiro_crew.dashboard.handlers.sessions.get_default_store", return_value=store):
        await mod._reap_session_images(_catalog({}), lineage, {_history_key_for("chat-slow")})
        # Not reaped yet: the registration is still running.
        store.delete_auto_images_for_sessions.assert_not_called()
        assert mod._DEFERRED_REAPS, "no deferred reap was scheduled"
        gate.set()
        await asyncio.gather(*mod._DEFERRED_REAPS)
    store.delete_auto_images_for_sessions.assert_called_once()
    (keys,), _ = store.delete_auto_images_for_sessions.call_args
    assert "chat-slow" in {k.removeprefix("dashboard:").removeprefix("dashboard_") for k in keys}
