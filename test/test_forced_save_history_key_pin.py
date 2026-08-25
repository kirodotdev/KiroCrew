"""expected_history_key pin at the tags/folders forced-save call sites (#7519).

``save_slot_off_loop`` / ``_save_slot_to_history`` resolve their target
transcript from live routing at write time, so a ``linked_session_key`` rebind
during the persist await can redirect a durable write to a transcript the
caller never authorized against. PR #7346 added the ``expected_history_key``
refuse-if-moved pin (save returns ``False``, writing nothing, when the live
key has moved off the pinned one) and wired it at the autocompact endpoint;
these tests pin the REMAINING forced-save sites the same way, mirroring
``test_session_autocompact_override.TestExpectedHistoryKeyPin``:

- ``chat_tags``: the tag-delete slot strip, PUT slot tags, and the drag-drop
  status reassign.
- ``chat_folders``: the folder-delete unfile loop and its restore rollback,
  PATCH slot folder, PATCH slot pin, and PATCH slot mode.

Two layers:

- Real-save tests drive an endpoint through the REAL save with the routing
  rebound mid-persist, asserting the refusal propagates (409, rollback) and
  that neither the pinned nor the foreign transcript received the write.
- Disposition tests stub the save to refuse (``False``) and assert each call
  site's documented handling: the direct mutation endpoints roll back and
  return 409 ``session_gone`` (the autocompact disposition); the drag-drop
  endpoint answers in its own rejection shape (``ok: False`` + reason); the
  best-effort cleanup loops mark the slot dirty and keep going. Every stubbed
  call also proves the pin was captured from the PRE-rebind routing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state, _make_tags_app

from kiro_crew.dashboard.chat import api_chat_slot_mode
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop as _real_save
from kiro_crew.dashboard.chat_tags import tags_write_lock
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

_FOREIGN_KEY = "channel:foreign:123"


def _rebinding_save(slot: _ChatSlot):
    """A save double that rebinds *slot* then delegates to the REAL save.

    Simulates the rebind window the pin exists for: the routing moves after
    the caller captured its authorized key but before the save's own routing
    snapshot, so the real refusal path (not a stub) produces the ``False``.
    """

    async def _save(state, target, *args, **kwargs):
        target.linked_session_key = _FOREIGN_KEY
        return await _real_save(state, target, *args, **kwargs)

    return _save


def _make_mode_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_patch("/api/chat/slots/{slot}/mode", api_chat_slot_mode)
    return app


class TestRealSaveRefusalThroughEndpoints:
    """The rebind refusal, end to end through the real save."""

    @pytest.mark.asyncio
    async def test_slot_tags_put_refuses_409_and_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Spike"})).json()
            slot = state.get_or_create_slot("s1")
            slot.append("user", "hello")
            slot.drain()
            pinned = slot_history_key(slot)
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", _rebinding_save(slot)):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [tag["id"]]})
            assert resp.status == 409
            assert (await resp.json())["code"] == "session_gone"
            # Live field rolled back — the acknowledged state never diverges
            # from what the caller was told.
            assert slot.tags == []
            # Nothing was written to either transcript.
            assert not (state.conversation_log._read_metadata(pinned) or {}).get("tags")
            foreign_meta = state.conversation_log._read_metadata(_FOREIGN_KEY)
            assert not (foreign_meta or {}).get("tags")

    @pytest.mark.asyncio
    async def test_slot_folder_patch_refuses_409_and_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.append("user", "hello")
            slot.drain()
            pinned = slot_history_key(slot)
            with patch(
                "kiro_crew.dashboard.chat_folders.save_slot_off_loop", _rebinding_save(slot)
            ):
                resp = await client.patch(
                    "/api/chat/slots/s1/folder", json={"folder_id": folder["id"]}
                )
            assert resp.status == 409
            assert (await resp.json())["code"] == "session_gone"
            assert slot.folder_id == ""
            assert slot._folder_changed is False
            assert not (state.conversation_log._read_metadata(pinned) or {}).get("folder_id")
            foreign_meta = state.conversation_log._read_metadata(_FOREIGN_KEY)
            assert not (foreign_meta or {}).get("folder_id")


class TestRefusalDispositionPerSite:
    """Each site's handling of a refused save, with the pin capture proven."""

    @pytest.mark.asyncio
    async def test_slot_pin_rolls_back_and_409(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        refusing = AsyncMock(return_value=False)
        async with TestClient(TestServer(app)) as client:
            slot = state.get_or_create_slot("s1")
            pinned = slot_history_key(slot)
            with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", refusing):
                resp = await client.patch("/api/chat/slots/s1/pin", json={"pinned": True})
            assert resp.status == 409
            assert (await resp.json())["code"] == "session_gone"
            assert slot.pinned is False
            assert refusing.await_args.kwargs["expected_history_key"] == pinned

    @pytest.mark.asyncio
    async def test_slot_mode_rolls_back_both_fields_and_409(self):
        slot = _ChatSlot("test")
        slot.mode = "orchestrator"
        slot._auto_run = True
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.push_slots_update = MagicMock()
        pinned = slot_history_key(slot)
        refusing = AsyncMock(return_value=False)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", refusing):
            async with TestClient(TestServer(_make_mode_app(state))) as client:
                resp = await client.patch("/api/chat/slots/test/mode", json={"mode": ""})
                assert resp.status == 409
                assert (await resp.json())["code"] == "session_gone"
        # Both live fields restored: the mode AND the auto-run flag the
        # transition cleared on the way through.
        assert slot.mode == "orchestrator"
        assert slot._auto_run is True
        assert refusing.await_args.kwargs["expected_history_key"] == pinned

    @pytest.mark.asyncio
    async def test_slot_drop_answers_in_rejection_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        refusing = AsyncMock(return_value=False)
        async with TestClient(TestServer(app)) as client:
            tag = await (
                await client.post("/api/chat/tags", json={"name": "Doing", "status": True})
            ).json()
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"tag_ids": [tag["id"]], "mode": "any"}
                )
            ).json()
            slot = state.get_or_create_slot("s1")
            pinned = slot_history_key(slot)
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", refusing):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            # This endpoint reports rejections as ok:False in a 200 body (the
            # card stays put); the refusal takes the same shape, with the
            # rolled-back tag list so the client renders the true state.
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is False
            assert body["reason"] == "session was deleted or rebound"
            assert body["tags"] == []
            assert slot.tags == []
            assert refusing.await_args.kwargs["expected_history_key"] == pinned

    @pytest.mark.asyncio
    async def test_tag_delete_strip_marks_dirty_and_delete_succeeds(self, tmp_path, monkeypatch):
        # The strip no longer calls save_slot_off_loop from chat_tags: it routes
        # through persist_swept_slot_meta, which owns the identity re-check and the
        # dirty-arming so a sweep site cannot omit either. Refuse at that helper's
        # merge instead, and assert the same disposition this site documents.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        from kiro_crew.dashboard.chat_persistence import SweepMergeOutcome

        unconfirmed = AsyncMock(return_value=(SweepMergeOutcome.UNCONFIRMED, {}))
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Del"})).json()
            slot = state.get_or_create_slot("s1")
            slot.tags = [tag["id"]]
            with patch("kiro_crew.dashboard.chat_persistence._merge_slot_meta", unconfirmed):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            # The vocabulary commit already made the deletion durable; the
            # refused strip is best-effort cleanup, so the delete still
            # succeeds and the slot is marked dirty for the flush to retry.
            assert resp.status == 200
            assert all(t["id"] != tag["id"] for t in state._tags)
            assert slot.tags == []
            assert slot._dirty is True
            # The refusal was reached through the shared helper, which is what
            # enforces the pin now that the site does not pass it itself.
            assert unconfirmed.await_count >= 1

    @pytest.mark.asyncio
    async def test_folder_delete_unfile_marks_dirty_and_delete_succeeds(
        self, tmp_path, monkeypatch
    ):
        # Same relocation as the tag strip: the unfile is the post-commit re-scan
        # and persists through persist_swept_slot_meta, so refuse at that helper.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        from kiro_crew.dashboard.chat_persistence import SweepMergeOutcome

        unconfirmed = AsyncMock(return_value=(SweepMergeOutcome.UNCONFIRMED, {}))
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.folder_id = folder["id"]
            with patch("kiro_crew.dashboard.chat_persistence._merge_slot_meta", unconfirmed):
                resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            # The unfile stands in memory (the folder is gone); the refused
            # persist marks the slot dirty so the flush re-persists wherever
            # the slot now routes.
            assert resp.status == 200
            assert slot.folder_id == ""
            assert slot._dirty is True
            assert unconfirmed.await_count >= 1

    @pytest.mark.asyncio
    async def test_a_failed_folder_commit_mutates_no_slot(self, tmp_path, monkeypatch):
        """Supersedes the restore-rollback disposition: there is nothing to restore.

        The delete now COMMITS the folder removal before touching any slot, so a
        failed commit has mutated nothing and no rollback runs — which is why
        ``_restore_unfiled`` is gone. That is a strictly stronger guarantee than
        restoring afterwards: the previous ordering could leave a conversation
        durably unfiled when its restore save was refused on the same identity
        test that refused the original persist, and nothing repaired it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.folder_id = folder["id"]
            with patch.object(state, "mutate_folders", AsyncMock(side_effect=OSError("disk full"))):
                resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            # The commit failure propagates (the delete did NOT land)…
            assert resp.status == 500
            # …and the slot was never touched, so there is no half-applied state
            # for a rollback to repair.
            assert slot.folder_id == folder["id"]


class TestRefusalRollbackHardening:
    """The reviewer-caught refinements: the rollback must not clobber state
    it does not own — a pending breadcrumb latch from an EARLIER successful
    move, or a CONCURRENT writer's acknowledged commit — and a rebind during
    an await before the mutation must be refused before anything mutates.
    """

    @pytest.mark.asyncio
    async def test_folder_refusal_preserves_pending_breadcrumb_latch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        refusing = AsyncMock(return_value=False)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            # An earlier successful move armed the latch; the session has not
            # taken its next turn yet, so the breadcrumb re-injection is
            # still pending and must survive this request's refusal.
            slot._folder_changed = True
            with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", refusing):
                resp = await client.patch(
                    "/api/chat/slots/s1/folder", json={"folder_id": folder["id"]}
                )
            assert resp.status == 409
            assert slot._folder_changed is True

    @pytest.mark.asyncio
    async def test_mode_refusal_does_not_clobber_concurrent_writer(self):
        slot = _ChatSlot("test")
        assert slot.mode == ""
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.push_slots_update = MagicMock()

        async def _concurrent_writer_wins(st, target, *args, **kwargs):
            # While THIS request's save awaits, a concurrent writer commits
            # and is acknowledged; then this save is refused. The stale
            # rollback must not erase the newer value.
            target.mode = "crew"
            return False

        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _concurrent_writer_wins):
            async with TestClient(TestServer(_make_mode_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode", json={"mode": "orchestrator"}
                )
                assert resp.status == 409
        assert slot.mode == "crew"

    @pytest.mark.asyncio
    async def test_tags_rebind_while_waiting_on_lock_is_refused_before_mutation(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        recording = AsyncMock(return_value=True)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            slot = state.get_or_create_slot("s1")
            lock = tags_write_lock(state)
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", recording):
                # Hold the tags write lock so the PUT parks on a REAL await
                # window, rebind the slot while it waits, then release. The
                # post-await re-check must refuse before any mutation.
                await lock.acquire()
                try:
                    put = asyncio.ensure_future(
                        client.put("/api/chat/slots/s1/tags", json={"tags": [tag["id"]]})
                    )
                    await asyncio.sleep(0.05)  # let the PUT reach the lock
                    slot.linked_session_key = _FOREIGN_KEY
                finally:
                    lock.release()
                resp = await put
            assert resp.status == 409
            assert (await resp.json())["code"] == "session_gone"
            assert slot.tags == []
            recording.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drop_does_not_resurrect_a_deleted_target_tag(self, tmp_path, monkeypatch):
        """The column and vocabulary are resolved UNDER the tags lock.

        A tag deletion completing while the drop waits on the lock removes the
        target from the vocabulary; resolving before the lock would re-add and
        persist the stale id onto the slot after the vocabulary commit removed
        it. Resolved under the lock, the dead target reads as "not a status
        lane" and the drop is a rejected no-op.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        recording = AsyncMock(return_value=True)
        async with TestClient(TestServer(app)) as client:
            # The column still references the tag id, but the vocabulary no
            # longer carries it — the state a tag delete leaves for a drop
            # that lost the lock race.
            state._tags = []
            state._tag_boards = [
                {"id": "col1", "name": "Doing", "tag_ids": ["ghost"], "mode": "any", "order": 0}
            ]
            slot = state.get_or_create_slot("s1")
            slot.tags = ["keep-me"]
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", recording):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": "col1"})
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is False
            assert body["reason"] == "column is not a status lane"
            assert slot.tags == ["keep-me"]
            recording.assert_not_awaited()
