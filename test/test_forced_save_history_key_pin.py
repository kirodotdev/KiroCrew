"""expected_history_key pin at the tags/folders forced-save call sites.

``save_slot_off_loop`` / ``_save_slot_to_history`` resolve their target
transcript from live routing at write time, so a ``linked_session_key`` rebind
during the persist await can redirect a durable write to a transcript the
caller never authorized against. An earlier change added the ``expected_history_key``
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
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state, _make_tags_app

from kiro_crew.dashboard.chat import api_chat_slot_mode
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop as _real_save
from kiro_crew.dashboard.chat_tags import tags_write_lock
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.snapshot_commit import close_in_flight
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
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        refusing = AsyncMock(return_value=False)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Del"})).json()
            slot = state.get_or_create_slot("s1")
            slot.tags = [tag["id"]]
            pinned = slot_history_key(slot)
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", refusing):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            # The vocabulary commit already made the deletion durable; the
            # refused strip is best-effort cleanup, so the delete still
            # succeeds and the slot is marked dirty for the flush to retry.
            assert resp.status == 200
            assert all(t["id"] != tag["id"] for t in state._tags)
            assert slot.tags == []
            assert slot._dirty is True
            assert refusing.await_args.kwargs["expected_history_key"] == pinned

    @pytest.mark.asyncio
    async def test_folder_delete_unfile_marks_dirty_and_delete_succeeds(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        refusing = AsyncMock(return_value=False)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.folder_id = folder["id"]
            pinned = slot_history_key(slot)
            with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", refusing):
                resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            # The unfile stands in memory (the folder is gone); the refused
            # persist marks the slot dirty so the flush re-persists wherever
            # the slot now routes.
            assert resp.status == 200
            assert slot.folder_id == ""
            assert slot._dirty is True
            assert refusing.await_args.kwargs["expected_history_key"] == pinned

    @pytest.mark.asyncio
    async def test_a_failed_folder_commit_leaves_every_slot_untouched(self, tmp_path, monkeypatch):
        """A failed folder commit must mutate NO slot -- not even transiently.

        The delete commits the folder removal BEFORE touching any slot, so there is
        nothing to roll back, and "nothing to roll back" is a stronger property than
        "the rollback restored it": an ordering that unfiles first has its restore
        withheld on the same identity test that withheld the original persist, so a
        slot whose restore is refused stays durably unfiled with nothing left to repair
        it. Asserting the save is never REACHED is what discriminates, since unfiling
        first calls it twice -- once to unfile, once to restore.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        refusing = AsyncMock(return_value=False)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.folder_id = folder["id"]
            slot._dirty = False
            with (
                patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", refusing),
                patch.object(state, "mutate_folders", AsyncMock(side_effect=OSError("disk full"))),
            ):
                resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            # The commit failure propagates -- the delete did NOT land...
            assert resp.status == 500
            assert slot.folder_id == folder["id"], (
                "the failed commit unfiled a slot anyway, so a folder that still "
                "exists has lost a conversation"
            )
            assert slot._dirty is False, (
                "the failed commit left the slot dirty, so the next save makes the "
                "unfile durable even though the folder was never removed"
            )
            assert refusing.await_count == 0, (
                "the commit failure still reached a slot save, so the ordering is "
                "unfile-then-commit and a refused restore cannot be repaired"
            )


class TestCancellationMidCommitStillSweepsAndAudits:
    """A client disconnecting mid-delete must not skip the cleanup or the audit line.

    ``asyncio.to_thread`` hands the vocabulary write to a worker that cannot be
    interrupted, so cancelling the handler does not stop the removal -- the bytes land
    anyway. Everything AFTER that await is what a bare propagation loses: the sweep that
    unfiles the slots naming the deleted folder, and the operation's only SEL emission.
    So the cancellation is captured, the removal is confirmed from the committed set, and
    it is re-raised only after both have run.
    """

    @staticmethod
    def _delete_request(app, fid):
        from aiohttp.test_utils import make_mocked_request

        return make_mocked_request(
            "DELETE", f"/api/chat/folders/{fid}", app=app, match_info={"id": fid}
        )

    @pytest.mark.asyncio
    async def test_a_cancellation_inside_the_commit_still_unfiles_and_still_audits(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        audited: list[dict] = []
        real_mutate = state.mutate_folders

        async def _commit_then_cancel(*args, **kwargs):
            # What a disconnect during the shielded write looks like to the handler: the
            # removal lands, then a cancellation arrives in the same await.
            await real_mutate(*args, **kwargs)
            raise asyncio.CancelledError()

        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.folder_id = folder["id"]
            saving = AsyncMock(return_value=True)
            with (
                patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", saving),
                patch(
                    "kiro_crew.dashboard.chat_folders.sel",
                    lambda: MagicMock(log_api_access=lambda **kw: audited.append(kw)),
                ),
                patch.object(state, "mutate_folders", _commit_then_cancel),
            ):
                with pytest.raises(asyncio.CancelledError):
                    from kiro_crew.dashboard.chat_folders import api_chat_folder_delete

                    await api_chat_folder_delete(self._delete_request(app, folder["id"]))

        # POSITIVE CONTROL that the commit really landed, so the assertions below are
        # about the post-commit half rather than about a delete that never happened.
        assert all(f["id"] != folder["id"] for f in state._folders)
        # Ordered first on purpose: a weakened fence then fails HERE, on behaviour,
        # rather than on an AttributeError that would prove nothing.
        assert [e["operation"] for e in audited] == ["chat.folder_delete"], (
            "the cancellation was re-raised before the audit emission, so the delete "
            "landed with no record of it at all"
        )
        # The sweep ran despite the cancellation: the slot is unfiled AND persisted.
        assert slot.folder_id == "", (
            "the cancellation skipped the unfile sweep, so this conversation still names "
            "a folder that no longer exists"
        )
        assert saving.await_count == 1
        # The removal is provable from DISK, which is what licensed the sweep to run at
        # all: the confirmation the handler passes fires only after this write lands.
        on_disk = json.loads((tmp_path / "folders.json").read_text())
        assert all(f["id"] != folder["id"] for f in on_disk)

    @pytest.mark.asyncio
    async def test_a_cancellation_before_the_commit_lands_leaves_the_filing_alone(
        self, tmp_path, monkeypatch
    ):
        """The other arm of the same fence: an unprovable removal must not unfile.

        A cancellation can arrive while awaiting the store lock, before any bytes are
        written. The post-commit confirmation never fires, which is what separates this
        from the landed case, and the only safe disposition is to re-raise untouched --
        unfiling here strands a conversation with no folder while the folder still
        exists, and nothing later puts the filing back.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        saving = AsyncMock(return_value=True)

        async def _cancel_before_the_write(*args, **kwargs):
            raise asyncio.CancelledError()

        raised: BaseException | None = None
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.folder_id = folder["id"]
            # POSITIVE CONTROL for the fence's premise: the folder is still on disk, so
            # this is the unprovable-removal arm rather than the landed one.
            on_disk = json.loads((tmp_path / "folders.json").read_text())
            assert any(f["id"] == folder["id"] for f in on_disk)
            with (
                patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", saving),
                patch.object(state, "mutate_folders", _cancel_before_the_write),
            ):
                from kiro_crew.dashboard.chat_folders import api_chat_folder_delete

                try:
                    await api_chat_folder_delete(self._delete_request(app, folder["id"]))
                except asyncio.CancelledError as exc:
                    raised = exc

        # Ordered first on purpose: this is the harm, so a weakened fence fails HERE.
        assert slot.folder_id == folder["id"], (
            "a cancellation that arrived before the removal landed still unfiled this "
            "conversation, so it names no folder while the folder itself still exists"
        )
        assert (
            saving.await_count == 0
        ), "the sweep persisted an unfile for a removal that was never proved to land"
        assert any(
            f["id"] == folder["id"] for f in state._folders
        ), "the folder is gone from memory, so this run does not exercise the fence"
        assert raised is not None, "the cancellation was swallowed instead of re-raised"


class TestSweepDoesNotOverwriteACompletedClose:
    """A close that COMPLETES during the vocabulary commit must survive the sweep.

    The pre-commit capture exists for a close whose save FAILS -- the handler restores
    the same object under the same map key, so the sweep must reach it. A close that
    SUCCEEDS leaves the slot popped for good, and its ``closed`` stamp already durable.
    Force-saving the captured object then writes a metadata line with no ``closed`` key,
    and absent means cleared for a slot-owned field, so the tab resurfaces.

    ``expected_history_key`` cannot separate the two: a close does not move routing, so
    the key is identical either way. ``expected_slot_name`` can, because it asks a
    different question -- does ``state._slots`` still hold THIS object under that name.
    """

    @pytest.mark.asyncio
    async def test_the_sweep_leaves_a_slot_a_completed_close_popped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            slot = state.get_or_create_slot("s1")
            slot.append("user", "hello")
            slot.drain()
            slot.folder_id = folder["id"]
            key = slot_history_key(slot)
            assert await _real_save(state, slot, closed=True, force=True)
            assert (state.conversation_log._read_metadata(key) or {}).get("closed")

            real_mutate = state.mutate_folders
            popped: dict = {}

            async def _commit_then_the_close_completes(*args, **kwargs):
                result = await real_mutate(*args, **kwargs)
                popped["slot"] = state._slots.pop("s1", None)
                return result

            with patch.object(state, "mutate_folders", _commit_then_the_close_completes):
                resp = await client.delete(f"/api/chat/folders/{folder['id']}")
            assert resp.status == 200
            # The close really did pop this object mid-commit, so the sweep below was
            # reached with it captured and out of the map.
            assert popped["slot"] is slot and "s1" not in state._slots
            assert (state.conversation_log._read_metadata(key) or {}).get("closed"), (
                "the sweep force-saved a slot a completed close had popped, so the "
                "closed stamp is gone and the tab resurfaces on the next load"
            )

    @pytest.mark.asyncio
    async def test_a_tag_cancellation_inside_the_commit_still_strips_and_still_audits(
        self, tmp_path, monkeypatch
    ):
        """The tag fence's landed arm: a proved removal must still sweep and audit.

        The confirmation fires because the write reached disk, so the cancellation is
        held until the strip and the single audit line are done. Re-raising at the
        cancellation instead leaves the tag gone with slots still naming it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        audited: list[dict] = []
        saving = AsyncMock(return_value=True)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            slot = state.get_or_create_slot("s1")
            slot.tags = [tag["id"]]

            import kiro_crew.dashboard.chat_tags as chat_tags_mod

            real_commit = chat_tags_mod._commit_tags_snapshot

            async def _commit_then_cancel(*args, **kwargs):
                await real_commit(*args, **kwargs)
                raise asyncio.CancelledError()

            from aiohttp.test_utils import make_mocked_request

            request = make_mocked_request(
                "DELETE",
                f"/api/chat/tags/{tag['id']}",
                app=app,
                match_info={"id": tag["id"]},
            )
            with (
                patch.object(chat_tags_mod, "save_slot_off_loop", saving),
                patch.object(chat_tags_mod, "_commit_tags_snapshot", _commit_then_cancel),
                patch.object(
                    chat_tags_mod,
                    "sel",
                    lambda: MagicMock(log_api_access=lambda **kw: audited.append(kw)),
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await chat_tags_mod.api_chat_tag_delete(request)

        # POSITIVE CONTROL that the commit really landed, so the assertions below are
        # about the post-commit half rather than about a delete that never happened.
        on_disk = json.loads((tmp_path / "tags.json").read_text())
        assert all(t["id"] != tag["id"] for t in on_disk)
        # Ordered first on purpose: a weakened fence fails HERE, on behaviour.
        assert [e["operation"] for e in audited] == ["chat.tag_delete"], (
            "the cancellation was re-raised before the audit emission, so the delete "
            "landed with no record of it at all"
        )
        assert slot.tags == [], (
            "the cancellation skipped the strip sweep, so this conversation still names "
            "a tag that no longer exists"
        )
        assert saving.await_count == 1

    @pytest.mark.asyncio
    async def test_a_tag_cancellation_before_the_commit_lands_leaves_assignments_alone(
        self, tmp_path, monkeypatch
    ):
        """The tag fence's other arm: an unprovable removal must not strip.

        A cancellation can arrive while awaiting the tag lock, before any bytes are
        written, and the post-commit confirmation never fires. Stripping here loses a
        slot's assignment while the tag itself still exists, and nothing puts it back.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        saving = AsyncMock(return_value=True)

        async def _cancel_before_the_write(*args, **kwargs):
            raise asyncio.CancelledError()

        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            slot = state.get_or_create_slot("s1")
            slot.tags = [tag["id"]]

            import kiro_crew.dashboard.chat_tags as chat_tags_mod

            # POSITIVE CONTROL for the fence's premise: the tag is still on disk, so this
            # is the unprovable-removal arm rather than the landed one.
            on_disk = json.loads((tmp_path / "tags.json").read_text())
            assert any(t["id"] == tag["id"] for t in on_disk)
            with (
                patch.object(chat_tags_mod, "save_slot_off_loop", saving),
                patch.object(chat_tags_mod, "_commit_tags_snapshot", _cancel_before_the_write),
            ):
                # Driven directly, not through the client: the re-raise tears down the
                # connection, so the client would report a disconnect instead.
                from aiohttp.test_utils import make_mocked_request

                request = make_mocked_request(
                    "DELETE",
                    f"/api/chat/tags/{tag['id']}",
                    app=app,
                    match_info={"id": tag["id"]},
                )
                try:
                    await chat_tags_mod.api_chat_tag_delete(request)
                except asyncio.CancelledError:
                    pass

        # Ordered first on purpose: this is the harm, so a weakened fence fails HERE.
        assert slot.tags == [tag["id"]], (
            "a cancellation that arrived before the removal landed still stripped this "
            "assignment, so the slot lost a tag that still exists"
        )
        # Memory must read the tag back too: a later snapshot write serializes
        # ``state._tags``, which would otherwise make the unproven removal durable with
        # no strip sweep at all.
        assert any(t["id"] == tag["id"] for t in state._tags), (
            "the unproven removal was left applied in memory, so the next tag write "
            "commits a delete that never landed and never swept"
        )
        assert (
            saving.await_count == 0
        ), "the sweep persisted a strip for a removal that was never proved to land"

    @pytest.mark.asyncio
    async def test_the_tag_sweep_leaves_a_slot_a_completed_close_popped(
        self, tmp_path, monkeypatch
    ):
        """The tag strip carries the same capture, so it needs the same guard.

        Its pre-commit capture reaches a slot a concurrent close popped, for the same
        reason the folder sweep's does, and force-saving a slot whose close COMPLETED
        writes a metadata line with no ``closed`` key.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            slot = state.get_or_create_slot("s1")
            slot.append("user", "hello")
            slot.drain()
            slot.tags = [tag["id"]]
            key = slot_history_key(slot)
            assert await _real_save(state, slot, closed=True, force=True)
            assert (state.conversation_log._read_metadata(key) or {}).get("closed")

            import kiro_crew.dashboard.chat_tags as chat_tags_mod

            real_commit = chat_tags_mod._commit_tags_snapshot
            popped: dict = {}

            async def _commit_then_the_close_completes(*args, **kwargs):
                await real_commit(*args, **kwargs)
                popped["slot"] = state._slots.pop("s1", None)

            with patch.object(
                chat_tags_mod, "_commit_tags_snapshot", _commit_then_the_close_completes
            ):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 200
            # The close really did pop this object mid-commit, so the strip pass below
            # was reached with it captured and out of the map.
            assert popped["slot"] is slot and "s1" not in state._slots
            assert (state.conversation_log._read_metadata(key) or {}).get("closed"), (
                "the tag strip force-saved a slot a completed close had popped, so the "
                "closed stamp is gone and the tab resurfaces on the next load"
            )


class TestSweepReachesACloseAlreadyInFlight:
    """A close that popped its slot BEFORE the delete's capture must still be swept.

    The capture reads ``state._slots``, so a slot popped earlier is in no view it can
    see. When that close's own save then FAILS the handler restores the same object
    under the same key, still carrying the deleted id, and the periodic flush makes it
    durable -- with no later pass able to reach it, because the object was in no
    snapshot while it was out.

    ``close_in_flight`` is the production registry ``close_slot`` wraps its whole frame
    in, so these tests drive the real mechanism rather than a model of it.
    """

    @pytest.mark.asyncio
    async def test_the_folder_sweep_reaches_a_slot_popped_before_the_capture(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            folder = await (await client.post("/api/chat/folders", json={"name": "F"})).json()
            gone = state.get_or_create_slot("s-closing")
            gone.folder_id = folder["id"]
            live = state.get_or_create_slot("s-live")
            live.folder_id = folder["id"]

            state._slots.pop("s-closing", None)
            with close_in_flight(state, "s-closing", gone):
                resp = await client.delete(f"/api/chat/folders/{folder['id']}")

            assert resp.status == 200
            assert "s-closing" not in state._slots, "the fixture never popped the slot"
            assert gone.folder_id == "", (
                "a slot whose close popped it before the capture kept the deleted "
                "folder id; its failed save restores this same object, so the id "
                "becomes durable and no later pass can reach it"
            )
            assert live.folder_id == "", (
                "the still-present slot was not unfiled either -- the sweep stopped "
                "working rather than being widened"
            )

    @pytest.mark.asyncio
    async def test_the_tag_sweep_reaches_a_slot_popped_before_the_capture(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T"})).json()
            gone = state.get_or_create_slot("s-closing")
            gone.tags = [tag["id"]]
            live = state.get_or_create_slot("s-live")
            live.tags = [tag["id"]]

            state._slots.pop("s-closing", None)
            with close_in_flight(state, "s-closing", gone):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")

            assert resp.status == 200
            assert "s-closing" not in state._slots, "the fixture never popped the slot"
            assert tag["id"] not in gone.tags, (
                "a slot whose close popped it before the capture kept the deleted tag "
                "id; its failed save restores this same object, so the id becomes "
                "durable and no later pass can reach it"
            )
            assert tag["id"] not in live.tags, (
                "the still-present slot was not stripped either -- the sweep stopped "
                "working rather than being widened"
            )


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
            target.mode = "design-critique"
            return False

        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _concurrent_writer_wins):
            async with TestClient(TestServer(_make_mode_app(state))) as client:
                resp = await client.patch(
                    "/api/chat/slots/test/mode", json={"mode": "orchestrator"}
                )
                assert resp.status == 409
        assert slot.mode == "design-critique"

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
