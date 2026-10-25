"""Tests for chat_tags — when the disk, a concurrent writer or the body fails a request.

A corrupt tags.json is never overwritten, a write that cannot persist rolls back,
concurrent writers serialize, an update racing a delete answers 404, and a
non-object body is a 400, never a 5xx.
"""

from __future__ import annotations

from unittest.mock import patch

import chat_tags_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard import chat_tags as chat_tags_module
from kiro_crew.dashboard.state import _ChatSlot

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret


# ── load_tags safety: do not overwrite a present-but-corrupt tags.json ──


class TestLoadTagsSafety:
    def test_load_failure_does_not_overwrite_with_defaults(self, tmp_path, monkeypatch):
        """If tags.json exists but cannot be parsed, never silently overwrite it
        with the seed vocabulary — that would destroy the user's data."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Write an unreadable / corrupt tags file
        corrupt = tmp_path / "tags.json"
        corrupt.write_text("not-json-at-all", encoding="utf-8")
        original = corrupt.read_text(encoding="utf-8")
        state = _make_state(tmp_path)
        state.load_tags()
        # On parse failure: vocabulary stays empty AND the file is untouched.
        assert state._tags == []
        assert corrupt.read_text(encoding="utf-8") == original

    def test_missing_file_seeds_defaults(self, tmp_path, monkeypatch):
        """If tags.json doesn't exist, seed the default 5 status tags."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.load_tags()
        names = {t["name"] for t in state._tags}
        assert names == {"Planned", "ToDo", "Implementation", "Review", "Done"}
        assert (tmp_path / "tags.json").exists()

    def test_first_boot_crash_after_the_grant_seed_keeps_defaults_grantable(
        self, tmp_path, monkeypatch
    ):
        """The default grants are seeded BEFORE the vocabulary commit. A first
        boot that dies between the two leaves inert grant rows and no tags.json;
        the next boot is still a fresh install, seeds the vocabulary, and the
        defaults resolve agent-drivable. With the reverse order that boot reads
        as an upgraded install and seeds an EMPTY store, so every default
        workflow state silently becomes human-only."""
        from kiro_crew.dashboard import chat_tags as ct
        from kiro_crew.dashboard import state as state_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        crashed = _make_state(tmp_path)
        real_seed = state_mod.seed_default_grants

        def _seed_then_die(ids):
            real_seed(ids)
            raise KeyboardInterrupt  # the process dies right after the seed

        monkeypatch.setattr(state_mod, "seed_default_grants", _seed_then_die)
        with pytest.raises(KeyboardInterrupt):
            crashed.load_tags()
        # The vocabulary commit comes AFTER the seed, so nothing about the
        # vocabulary is durable yet: the next boot is still a fresh install.
        assert not (tmp_path / "tags.json").exists()
        monkeypatch.setattr(state_mod, "seed_default_grants", real_seed)

        state = _make_state(tmp_path)
        state.load_tags()
        assert (tmp_path / "tags.json").exists()
        ct.refresh_cache()
        for tag in state._tags:
            assert ct.agent_tag_grant(tag) == ("add-remove", True), tag["name"]

    def test_explicitly_empty_file_is_not_reseeded(self, tmp_path, monkeypatch):
        """If tags.json contains [], the user explicitly cleared every tag —
        do not re-seed defaults across restart."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text("[]", encoding="utf-8")
        state = _make_state(tmp_path)
        state.load_tags()
        assert state._tags == []
        # And the file content is preserved (no re-seed write).
        assert (tmp_path / "tags.json").read_text(encoding="utf-8") == "[]"

    def test_load_prunes_dangling_column_tag_ids(self, tmp_path, monkeypatch):
        """A crash mid-tag-delete can leave a deleted tag id on a column.
        Load must prune it: the column API rejects unknown ids, so a dangling
        id left in place would make that column's filter un-editable."""
        import json as _json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text(
            _json.dumps([{"id": "live1", "name": "Live", "color": "#111111", "order": 0}]),
            encoding="utf-8",
        )
        (tmp_path / "tag_boards.json").write_text(
            _json.dumps(
                [
                    {
                        "id": "c1",
                        "name": "L",
                        "tag_ids": ["live1", "ghost"],
                        "mode": "any",
                        "order": 0,
                    }
                ]
            ),
            encoding="utf-8",
        )
        state = _make_state(tmp_path)
        state.load_tags()
        assert state._tag_boards[0]["tag_ids"] == ["live1"]

    def test_load_keeps_column_tag_ids_when_vocabulary_unknown(self, tmp_path, monkeypatch):
        """A corrupt tags.json means the vocabulary is UNKNOWN — pruning
        against it would wipe every column filter. Fail open, same rule as
        the slot-restore prune."""
        import json as _json

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text("not-json-at-all", encoding="utf-8")
        (tmp_path / "tag_boards.json").write_text(
            _json.dumps([{"id": "c1", "name": "L", "tag_ids": ["t1"], "mode": "any", "order": 0}]),
            encoding="utf-8",
        )
        state = _make_state(tmp_path)
        state.load_tags()
        assert state._tag_boards[0]["tag_ids"] == ["t1"]


# ── F1: DELETE fail-closed propagation from save_slot_off_loop ──


class TestDeleteSlotPersistPropagatesFromUnderlying:
    """Crash-atomic contract: an underlying slot write failure during the
    post-commit strip does NOT fail the deletion — the vocab commit already
    made it durable; the slot is marked dirty for periodic-flush retry."""

    @pytest.mark.asyncio
    async def test_delete_succeeds_despite_underlying_write_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Under"})).json()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot

            async def _failing_save(state_arg, slot_arg, **kwargs):
                raise IOError("disk full")

            with patch.object(chat_tags_module, "save_slot_off_loop", _failing_save):
                resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 200
            # Tag removed from vocabulary; slot stripped in memory; the
            # failed write left the slot dirty so the flush retries it.
            assert all(t["id"] != tag["id"] for t in state._tags)
            assert tag["id"] not in slot.tags
            assert getattr(slot, "_dirty", False) is True


# ── F2: Tag create surfaces persist failure and rolls back ──


class TestTagCreatePersistFailure:
    """F2: save_tags_snapshot now uses strict writes; a create that fails to
    persist must return 5xx and NOT leave the tag in state._tags."""

    @pytest.mark.asyncio
    async def test_create_returns_500_and_rolls_back_on_write_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            with patch.object(
                state.__class__,
                "_atomic_write_json_strict",
                side_effect=IOError("disk full"),
            ):
                resp = await client.post("/api/chat/tags", json={"name": "Ghost"})
            assert resp.status == 500
            body = await resp.json()
            assert body["code"] == "persist_failed"
            # Tag must NOT be in state
            assert not any(t.get("name") == "Ghost" for t in state._tags)


# ── F3: Board column create concurrency test ──


class TestBoardColumnConcurrency:
    """F3: Two concurrent column creates must both appear in state and the
    final snapshot (lock serializes them)."""

    @pytest.mark.asyncio
    async def test_concurrent_column_creates_both_persisted(self, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        snapshots_written: list[list[dict]] = []
        _real_save = state.save_tag_boards_snapshot

        def _recording_save(snapshot):
            snapshots_written.append(snapshot)
            _real_save(snapshot)

        async with TestClient(TestServer(app)) as client:
            with patch.object(state, "save_tag_boards_snapshot", _recording_save):
                results = await asyncio.gather(
                    client.post("/api/chat/tag-columns", json={"mode": "any"}),
                    client.post("/api/chat/tag-columns", json={"mode": "any"}),
                )
            assert all(r.status == 201 for r in results)
            col_ids = [await r.json() for r in results]
            ids_created = {c["id"] for c in col_ids}
            # Both columns in state
            state_ids = {c["id"] for c in state._tag_boards}
            assert ids_created.issubset(state_ids)
            # Final snapshot (last written) contains both
            assert len(snapshots_written) >= 2
            final_snap_ids = {c["id"] for c in snapshots_written[-1]}
            assert ids_created.issubset(final_snap_ids)


# ── F1-ext: Delete vocab-write failure triggers compensating rollback ──


class TestDeleteVocabWriteCompensation:
    """Crash-atomic contract: the vocabulary write is the single durable
    commit. If it fails, NOTHING else has been touched — slots are never
    stripped and no compensation writes are needed."""

    @pytest.mark.asyncio
    async def test_vocab_write_failure_leaves_slots_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "VocabFail"})).json()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot

            slot_saves: list[str] = []

            async def _recording_save(state_arg, slot_arg, **kwargs):
                slot_saves.append(slot_arg.key)

            with patch.object(chat_tags_module, "save_slot_off_loop", _recording_save):
                with patch.object(
                    chat_tags_module, "_write_tags_snapshot", side_effect=IOError("disk full")
                ):
                    resp = await client.delete(f"/api/chat/tags/{tag['id']}")
            assert resp.status == 500
            body = await resp.json()
            assert body["code"] == "persist_failed"
            # Vocabulary restored in memory; slot NEVER touched (no strip, no
            # save) — the failed vocab write is the only attempted mutation.
            assert any(t["id"] == tag["id"] for t in state._tags)
            assert tag["id"] in slot.tags
            assert slot_saves == []


# ── F2-ext: Update race with concurrent DELETE ──


class TestUpdateDeleteRace:
    """F2: The PATCH handler must resolve the tag INSIDE the write lock.
    A concurrent DELETE that wins the lock first must cause PATCH to 404
    (not silently mutate a detached dict)."""

    @pytest.mark.asyncio
    async def test_concurrent_delete_causes_update_to_404(self, tmp_path, monkeypatch):
        import asyncio as _asyncio

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Race"})).json()
            tid = tag["id"]

            # Make _write_tags_snapshot slow so the DELETE holds the lock long
            # enough for PATCH to queue behind it.
            original_write = __import__(
                "kiro_crew.dashboard.chat_tags", fromlist=["_write_tags_snapshot"]
            )._write_tags_snapshot

            def _slow_write(st, snap):
                # flake-ok: pre-existing 150 ms sleep in the stubbed write, holding the lock so the PATCH queues behind the DELETE; moved verbatim, tracked for a de-flake pass
                __import__("time").sleep(0.15)
                return original_write(st, snap)

            # Run DELETE and PATCH concurrently.
            # DELETE wins the lock; PATCH waits, then re-resolves -> 404.
            with patch(
                "kiro_crew.dashboard.chat_tags._write_tags_snapshot",
                side_effect=_slow_write,
            ):
                delete_task = _asyncio.ensure_future(client.delete(f"/api/chat/tags/{tid}"))
                # Small delay so DELETE grabs the lock first.
                # flake-ok: pre-existing 10 ms barrier that lets the DELETE take the lock first; moved verbatim, tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms barrier that lets the DELETE take the lock first; moved verbatim, tracked for a de-flake pass
                await _asyncio.sleep(0.01)
                patch_task = _asyncio.ensure_future(
                    client.patch(f"/api/chat/tags/{tid}", json={"name": "Updated"})
                )

                delete_resp, patch_resp = await _asyncio.gather(delete_task, patch_task)

            # DELETE should succeed.
            assert delete_resp.status == 200

            # PATCH must 404 (tag deleted under the lock before PATCH could resolve it).
            assert patch_resp.status == 404
            patch_body = await patch_resp.json()
            assert patch_body["code"] == "not_found"

            # The tag must NOT be in state (no silent ghost mutation).
            assert not any(t["id"] == tid for t in state._tags)


class TestTagOrderInfinity:
    @pytest.mark.parametrize("raw", ["1e999", "-1e999"])
    @pytest.mark.asyncio
    async def test_infinite_order_is_skipped_not_a_500(self, tmp_path, monkeypatch, raw):
        """JSON reads ``1e999`` as an infinite float: the field is skipped, as for "abc"."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Inf"})).json()
            resp = await client.patch(
                f"/api/chat/tags/{tag['id']}",
                data=f'{{"order": {raw}}}',
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            assert (await resp.json())["order"] == tag["order"]


class TestNonObjectBodiesAcrossConvertedHandlers:
    """Every converted handler answers 400, never 5xx, on a non-object body.

    ``[]`` / ``"s"`` / ``5`` / ``true`` / ``null`` are all VALID JSON, so
    ``request.json()`` returned them and the ``.get()`` (or ``in``) that each
    handler performs next raised from OUTSIDE the parse ``try`` -- a 500 for
    what is really malformed client input. Driven through a real
    client so the shared guard's 64 KB pre-decode cap is exercised on the wire,
    which is how these endpoints now read their body; the cap decision for each
    site is recorded in ``_CAP_REGISTER`` in ``test_json_object_body_guard.py``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], "a string", 5, 1.5, True, None], ids=repr)
    async def test_non_object_body_is_400_not_500(self, payload, tmp_path) -> None:
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            # Seed a real tag and column: the {id} routes check existence BEFORE
            # parsing, so a made-up id would 404 and never reach the guard --
            # the test would pass for the wrong reason.
            created = await client.post("/api/chat/tags", json={"name": "T"})
            assert created.status in (200, 201), await created.text()
            tag_id = (await created.json())["id"]
            col = await client.post("/api/chat/tag-columns", json={"name": "C"})
            assert col.status in (200, 201), await col.text()
            col_id = (await col.json())["id"]

            routes = [
                ("post", "/api/chat/tags"),
                ("patch", f"/api/chat/tags/{tag_id}"),
                ("post", "/api/chat/tag-columns"),
                ("put", "/api/chat/tag-columns/order"),
                ("patch", f"/api/chat/tag-columns/{col_id}"),
            ]
            # api_chat_slot_tags and api_chat_slot_drop are converted too, but
            # they resolve the slot BEFORE parsing, so reaching their guard
            # needs a live slot rather than a seeded tag. Their call sites are
            # pinned in _CAP_REGISTER, and their guard is the same shared call.
            for method, path in routes:
                # ``json=None`` makes the client send NO body, which is a
                # different fact (invalid_json) from a body containing the JSON
                # literal ``null`` -- send that one as raw bytes.
                if payload is None:
                    resp = await getattr(client, method)(
                        path,
                        data=b"null",
                        headers={"Content-Type": "application/json"},
                    )
                else:
                    resp = await getattr(client, method)(path, json=payload)
                assert resp.status == 400, (path, payload, resp.status)
                assert (await resp.json())["code"] == "body_not_object", path
