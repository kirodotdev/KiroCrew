"""Dashboard chat: folder tags, folder emoji generation and folder assignment persistence."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import AsyncIterator, _make_folder_app, _make_state
from dashboard_chat_helpers import _provider_mock


class TestFolderTags:
    """Folder `tags` — vocabulary-constrained list persisted on create/update,
    and stripped from folders when a tag is deleted."""

    @staticmethod
    def _seed_vocabulary(state, tag_ids):
        state._tags = [
            {"id": tid, "name": tid, "color": "#6b7280", "order": i}
            for i, tid in enumerate(tag_ids)
        ]
        # A directly-seeded vocabulary is authoritative by definition — the
        # constructor fails closed (False) until load_tags() runs.
        state._tags_authoritative = True

    @staticmethod
    def _app_with_tag_delete(state):
        """Folder endpoints plus the tag-delete route, on one app."""
        from kiro_crew.dashboard.chat_tags import api_chat_tag_delete

        app = _make_folder_app(state)
        app.router.add_delete("/api/chat/tags/{id}", api_chat_tag_delete)
        return app

    @pytest.mark.asyncio
    async def test_create_and_update_with_valid_tags_persists(self, tmp_path, monkeypatch):
        """(a) Create with valid tags persists them; PATCH replaces the set."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2", "t3"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Payments", "tags": ["t1", "t2"]}
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["tags"] == ["t1", "t2"]
            fid = data["id"]
            # Reloaded from the store reflects the persisted list.
            assert state._folders[0]["tags"] == ["t1", "t2"]

            # PATCH replaces the tag set.
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"tags": ["t3"]})
            assert resp.status == 200
            assert state._folders[0]["tags"] == ["t3"]

            # An empty list clears the key entirely (absent means no tags).
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"tags": []})
            assert resp.status == 200
            assert "tags" not in state._folders[0]

    @pytest.mark.asyncio
    async def test_create_dedupes_tags(self, tmp_path, monkeypatch):
        """Duplicates collapse (order preserved) and a valid set persists."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Dupes", "tags": ["t1", "t1", "t2"]}
            )
            assert resp.status == 201
            assert (await resp.json())["tags"] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_malformed_vocabulary_id_does_not_crash_validation(self, tmp_path, monkeypatch):
        """A malformed persisted vocabulary entry (non-string ``id`` — e.g. a
        hand-edited tags.json carrying a list) must degrade to "unknown id"
        rather than crash the validator's set build with an unhashable type
        (which would 500 every folder-tag operation)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        # Corrupt entries alongside the valid one: unhashable id, missing id.
        state._tags.append({"id": ["not", "a", "string"], "name": "bad"})
        state._tags.append({"name": "no-id"})
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Robust", "tags": ["t1", "t9"]}
            )
            assert resp.status == 201
            # The valid id survives; the unknown one is filtered; no crash.
            assert (await resp.json())["tags"] == ["t1"]

    @pytest.mark.asyncio
    async def test_unreadable_vocab_does_not_wipe_folder_tags(self, tmp_path, monkeypatch):
        """FAIL-OPEN parity with ``validate_folder_tag_ids``: when tags.json
        was unreadable at boot (vocabulary UNKNOWN, ``_tags_authoritative``
        False), a folder PATCH carrying ``tags`` must keep the ids rather than
        intersecting them against the unknown (empty) vocabulary — that
        intersection would silently wipe the folder's tags and the PATCH would
        persist the loss."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Payments", "tags": ["t1", "t2"]}
            )
            assert resp.status == 201
            fid = (await resp.json())["id"]

            # Simulate a later boot where tags.json failed to load.
            state._tags = []
            state._tags_authoritative = False

            # A PATCH that echoes the stored tags back (any rename flow does
            # this) must not wipe them; shape guards and dedupe still apply.
            resp = await client.patch(
                f"/api/chat/folders/{fid}",
                json={"name": "Renamed", "tags": ["t1", "t1", "t2", 7]},
            )
            assert resp.status == 200
            assert state._folders[0]["tags"] == ["t1", "t2"]  # preserved, not wiped

    @pytest.mark.asyncio
    async def test_folder_tag_writes_hold_the_tags_write_lock(self, tmp_path, monkeypatch):
        """The point-of-application intersection and the folders write are ONE
        critical section under ``tags_write_lock`` (the invariant every tag
        consumer follows): a tag deletion committing between them would slip a
        just-deleted id past the strip pass and back onto the folder."""
        from kiro_crew.dashboard.chat_tags import tags_write_lock

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        held: list[bool] = []
        orig_mutate = state.mutate_folders

        async def _spy(fn, **kwargs):
            held.append(tags_write_lock(state).locked())
            return await orig_mutate(fn, **kwargs)

        monkeypatch.setattr(state, "mutate_folders", _spy)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Locked", "tags": ["t1"]})
            assert resp.status == 201
            fid = (await resp.json())["id"]
            resp = await client.patch(f"/api/chat/folders/{fid}", json={"tags": ["t1"]})
            assert resp.status == 200
        assert held[:2] == [True, True], (
            "a folder tags write ran outside tags_write_lock; a concurrent tag "
            "deletion could resurrect a deleted id onto the folder"
        )

    @pytest.mark.asyncio
    async def test_unknown_tag_id_is_filtered_not_400(self, tmp_path, monkeypatch):
        """(b) An unknown tag id is silently dropped on create and update.

        Mirrors ``api_chat_slot_tags`` exactly: a dangling id can legitimately
        exist (the tag delete's best-effort folder strip can fail), and a
        strict 400 here would make every subsequent save of that folder fail —
        the "permanently uneditable folder" class. Filtering at the write
        sheds the stale reference instead.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/folders", json={"name": "Mixed", "tags": ["nope", "t1"]}
            )
            assert resp.status == 201
            fid = (await resp.json())["id"]
            # Unknown id dropped, known id kept.
            assert state._folders[0].get("tags") == ["t1"]

            bad_patch = await client.patch(f"/api/chat/folders/{fid}", json={"tags": ["ghost"]})
            assert bad_patch.status == 200
            # An all-unknown list filters down to empty — tags cleared, not 400.
            assert not state._folders[0].get("tags")

    @pytest.mark.asyncio
    async def test_non_array_tags_rejected_400(self, tmp_path, monkeypatch):
        """A non-array `tags` payload is a 400, not a 500."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1"])
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/folders", json={"name": "Bad", "tags": "t1"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "tags_invalid"

    @pytest.mark.asyncio
    async def test_deleting_a_tag_strips_it_from_folders(self, tmp_path, monkeypatch):
        """(f) Deleting a tag from the vocabulary removes its id from every folder."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        self._seed_vocabulary(state, ["t1", "t2"])
        app = self._app_with_tag_delete(state)
        async with TestClient(TestServer(app)) as client:
            # Two folders carrying t1; one also carries t2.
            a = await (
                await client.post("/api/chat/folders", json={"name": "A", "tags": ["t1", "t2"]})
            ).json()
            b = await (
                await client.post("/api/chat/folders", json={"name": "B", "tags": ["t1"]})
            ).json()

            resp = await client.delete("/api/chat/tags/t1")
            assert resp.status == 200

        fa = next(f for f in state._folders if f["id"] == a["id"])
        fb = next(f for f in state._folders if f["id"] == b["id"])
        # t1 stripped everywhere; t2 untouched; a folder left with no tags loses
        # the key entirely.
        assert fa["tags"] == ["t2"]
        assert "tags" not in fb


class TestGenerateEmojiForName:
    @pytest.mark.asyncio
    async def test_redaction_applied(self, tmp_path, monkeypatch):
        """generate_emoji_for_name (artifact-folder path) still redacts replies."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.dashboard.chat_folders import generate_emoji_for_name

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "🔥"
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = _provider_mock()
        mock_client.prompt = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)
        state.push_slots_update = MagicMock()

        with (
            patch(
                "kiro_crew.dashboard.chat_folders.redact_exfiltration_urls",
                return_value=("🔥", False),
            ) as mock_url,
            patch(
                "kiro_crew.dashboard.chat_folders.redact_credentials", return_value=("🔥", False)
            ) as mock_cred,
        ):
            icon = await generate_emoji_for_name(state, "Oncall")
            assert icon == "🔥"
            mock_url.assert_called_once()
            mock_cred.assert_called_once()

    @pytest.mark.asyncio
    async def test_variation_selector_emoji_accepted(self, tmp_path, monkeypatch):
        """Emoji with U+FE0F variation selector (e.g. ❤️) accepted by the
        emoji generator (still used for artifact-library folders)."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.chat_folders import generate_emoji_for_name

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_event = MagicMock()
        mock_event.kind = "text_chunk"
        mock_event.text = "\u2764\ufe0f"  # ❤️
        done_event = MagicMock()
        done_event.kind = "complete"
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_TEXT_CHUNK", "text_chunk")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_COMPLETE", "complete")
        monkeypatch.setattr("kiro_crew.providers.base.EVENT_PERMISSION_REQUEST", "permission")

        mock_client = _provider_mock()
        mock_client.prompt = MagicMock(return_value=AsyncIterator([mock_event, done_event]))
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        icon = await generate_emoji_for_name(state, "Love")
        assert icon == "\u2764\ufe0f"


class TestFolderAssignmentPersistence:
    @pytest.mark.asyncio
    async def test_folder_assignment_saves_to_history(self, tmp_path, monkeypatch):
        """api_chat_slot_folder should call _save_slot_to_history for new sessions."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.append("user", "hello")
        slot.drain()
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
            path = tmp_path / "dashboard_myslot.jsonl"
            assert path.exists()
            import json

            meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
            assert meta["folder_id"] == "f1"

    @pytest.mark.asyncio
    async def test_folder_assignment_persists_on_resumed_session(self, tmp_path, monkeypatch):
        """Regression: folder_id must reach disk even when slot is a resumed
        session with no new messages.

        Root cause: _save_slot_to_history had an early-return guard that
        skipped disk writes when ``slot._resumed_count > 0 and
        len(messages) <= slot._resumed_count``. Metadata-only changes like
        folder assignment don't grow the message count past the resumed
        marker, so the save was silently dropped — folder_id never reached
        disk and the move was lost on the next gateway restart.

        Fix: folder endpoint passes ``force=True`` which bypasses the guard.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("resumedslot")
        slot.append("user", "old message from before restart")
        slot.drain()
        # A genuinely resumed slot's file exists on disk (the restore read it).
        # Persist first so the fixture matches reality — a slot claiming
        # on-disk history whose file is missing is the delete-won state the
        # save now refuses to recreate.
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        _save_slot_to_history(state, slot, force=True)
        # Mark slot as a resumed session (simulates being restored from disk).
        # The guard fires when _resumed_count >= len(messages).
        slot._resumed_count = len(slot.messages)
        state._folders = [{"id": "f-resumed", "name": "Build", "order": 0, "collapsed": False}]
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/resumedslot/folder",
                json={"folder_id": "f-resumed"},
            )
            assert resp.status == 200
            path = tmp_path / "dashboard_resumedslot.jsonl"
            assert path.exists(), "fixture save should have created the session file"
            import json

            meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
            assert meta.get("folder_id") == "f-resumed", (
                "folder_id was silently dropped on resumed session — "
                "force=True must bypass the _resumed_count guard"
            )

    @pytest.mark.asyncio
    async def test_pin_toggle_persists_on_resumed_session(self, tmp_path, monkeypatch):
        """Regression: pinned flag must reach disk on resumed sessions.

        Same root cause as the folder regression — the resumed-count guard
        in _save_slot_to_history was blocking metadata-only writes. Pin
        endpoint now passes ``force=True``.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("pinslot")
        slot.append("user", "old message")
        slot.drain()
        # Persist first: a resumed slot's file exists on disk (see the folder
        # regression above — the missing-file variant is the delete-won state
        # the save refuses to recreate).
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        _save_slot_to_history(state, slot, force=True)
        slot._resumed_count = len(slot.messages)
        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch("/api/chat/slots/pinslot/pin", json={"pinned": True})
            assert resp.status == 200
            path = tmp_path / "dashboard_pinslot.jsonl"
            assert path.exists(), "fixture save should have created the session file"
            import json

            meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
            assert meta.get("pinned") is True, (
                "pinned was silently dropped on resumed session — "
                "force=True must bypass the _resumed_count guard"
            )

    def test_save_slot_force_bypasses_resumed_guard(self, tmp_path, monkeypatch):
        """Unit test: ``force=True`` must bypass the resumed-session guard.

        Without force, resumed sessions with no new messages skip the write.
        With force, the metadata-only mutation reaches disk regardless.
        """
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forceslot")
        slot.append("user", "hello")
        slot.drain()
        # Persist first: a genuinely resumed slot's file exists on disk. (The
        # missing-file variant is the delete-won state the save refuses to
        # recreate.)
        _save_slot_to_history(state, slot, force=True)
        path = tmp_path / "dashboard_forceslot.jsonl"
        assert path.exists()
        slot._resumed_count = len(slot.messages)
        # Model a genuinely-resumed, UNCHANGED slot: restore sets _dirty=False.
        # (A dirty slot — e.g. an in-place stop-event edit — must NOT be skipped;
        # see test_inplace_stop_resolution_persists.)
        slot._dirty = False
        slot.folder_id = "f-force"

        import json

        # Without force — the resumed-count guard skips the save, so the
        # metadata mutation must NOT reach disk.
        _save_slot_to_history(state, slot)
        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta.get("folder_id") is None, "guard must skip save when not forced"

        # With force — save bypasses the guard, folder_id is written.
        _save_slot_to_history(state, slot, force=True)

        meta = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        assert meta.get("folder_id") == "f-force"
