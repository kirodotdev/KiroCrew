"""Dashboard chat: slot lifecycle, permanent delete versus save, bulk cleanup, a task-less stop or delete."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

# ── Save vs. permanent delete ──


class TestSaveDoesNotResurrectDeletedSession:
    """A save must not recreate a session whose permanent delete committed.

    ``delete_session`` unlinks under the same ``_locked`` region the save
    holds, leaves no tombstone, and reports success. A save routed through
    ``save_slot_off_loop`` takes the patient acquire, so it can sit waiting
    while the delete runs to completion ahead of it — the sequential
    delete-then-save below exercises exactly the code path such a save takes
    once the lock is finally granted (file gone, slot still carrying its
    in-memory window).
    """

    def test_save_after_committed_delete_does_not_recreate_the_file(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("gone")
        slot.append("user", "hello")
        slot.append("assistant", "hi")
        slot.drain()
        # First save through the real path: the session now exists on disk and
        # the slot has learned it (``_disk_window_len`` > 0).
        assert _save_slot_to_history(state, slot, force=True) is True
        path = state.conversation_log._path("dashboard:gone")
        assert path.exists()
        assert slot._disk_window_len > 0

        # The permanent delete commits and reports success…
        assert state.conversation_log.delete_session("dashboard:gone") is True
        assert not path.exists()

        # …so a save that acquires the lock afterwards must NOT undo it, even
        # though its window still holds the whole conversation. The ``False``
        # return is the caller-visible signal that nothing was persisted.
        assert _save_slot_to_history(state, slot, force=True) is False
        assert not path.exists(), (
            "a save that lost the race to a committed permanent delete "
            "recreated the session file"
        )

    def test_resumed_slot_save_after_delete_does_not_recreate_the_file(self, tmp_path, monkeypatch):
        """The ``_resumed_count`` evidence arm: a slot restored from history
        (no save of its own yet) must also honor a committed delete."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:revived", "user", "old turn")
        slot = state.get_or_create_slot("revived")
        slot.append("user", "old turn")
        slot.drain()
        # As the restore path records them: the resumed count AND the observed
        # disk identity (every hydrate site records both).
        slot._resumed_count = 1
        slot._disk_meta_created_at = str(log.get_metadata("dashboard:revived")["created_at"])
        assert slot._disk_window_len == 0

        assert log.delete_session("dashboard:revived") is True
        path = log._path("dashboard:revived")
        assert not path.exists()

        _save_slot_to_history(state, slot, force=True)
        assert not path.exists(), "a resumed slot's save recreated a permanently deleted session"

    @pytest.mark.asyncio
    async def test_fork_aborts_even_after_a_flush_consumed_the_delete_signal(
        self, tmp_path, monkeypatch
    ):
        """The flush-ordering bypass: a NON-dirty deleted slot must still not fork.

        The periodic 5s flush can hit the delete-won guard first and clear
        ``_dirty``. The fork then skips its own flush arms entirely (they are
        gated on ``slot._dirty``), the disk read comes back empty, and
        ``all_messages`` falls back to the in-memory window — republishing the
        deleted conversation with no save ever returning ``False`` to the fork.
        The direct ``session_was_deleted`` probe must refuse it anyway.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkclean")
        slot.append("user", "keep me?", "msg msg-u")
        slot.append("assistant", "sure", "msg msg-a")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        assert state.conversation_log.delete_session("dashboard:forkclean") is True
        # Model the flush having consumed the delete-won signal already.
        slot._dirty = False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/forkclean/fork",
                json={"prompt": "forked"},
            )
            payload = await resp.json()

        assert (
            resp.status == 409
        ), f"non-dirty fork of a deleted session must abort, got {resp.status} {payload}"
        session_files = [p.name for p in tmp_path.glob("*.jsonl")]
        assert (
            session_files == []
        ), f"a fork of the deleted conversation was published: {session_files}"

    @pytest.mark.asyncio
    async def test_transfer_bundle_refuses_a_deleted_session(self, tmp_path, monkeypatch):
        """Same bypass on the export path: a non-dirty deleted slot must not bundle."""
        from kiro_crew.dashboard import session_transfer as st
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("bundlegone")
        slot.append("user", "ship me?", "msg msg-u")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        assert state.conversation_log.delete_session("dashboard:bundlegone") is True
        slot._dirty = False

        with pytest.raises(st.SnapshotUnstable, match="permanently deleted"):
            await st.build_transfer_bundle_async(state, slot, origin="test")

    @pytest.mark.asyncio
    async def test_transfer_refuses_a_delete_landing_during_assembly(self, tmp_path, monkeypatch):
        """A delete completing INSIDE the threaded bundle read must still refuse.

        The assembly read is the builder's longest await, so the pre-read probe
        can pass and the permanent delete complete before the bundle returns —
        the bundle in hand is the destroyed conversation. The post-assembly
        re-check must catch it.
        """
        from kiro_crew.dashboard import session_transfer as st
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("midassembly")
        slot.append("user", "racing", "msg msg-u")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        slot._dirty = False

        real_assemble = st._read_and_assemble

        def _assemble_with_delete_landing_inside(*args, **kwargs):
            bundle = real_assemble(*args, **kwargs)
            # The permanent delete commits while the worker thread is still
            # inside the assembly (after the pre-read probe passed).
            assert state.conversation_log.delete_session("dashboard:midassembly") is True
            return bundle

        monkeypatch.setattr(st, "_read_and_assemble", _assemble_with_delete_landing_inside)

        with pytest.raises(st.SnapshotUnstable, match="permanently deleted"):
            await st.build_transfer_bundle_async(state, slot, origin="test")

    def test_save_does_not_merge_into_a_file_recreated_after_the_delete(
        self, tmp_path, monkeypatch
    ):
        """Delete -> foreign append recreates the file -> pending save must skip.

        ``delete_session`` leaves no tombstone, so a channel/cron
        ``append_off_loop`` landing after the delete creates a FRESH session
        file (new metadata ``created_at``). A pending save that only checked
        file existence would merge the deleted window into that new transcript
        — resurrecting the destroyed conversation inside someone else's rows.
        The guard must recognize the new incarnation by its identity and skip.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("reborn")
        slot.append("user", "the deleted conversation", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_created_at, "save must record the disk identity"

        log = state.conversation_log
        assert log.delete_session("dashboard:reborn") is True
        # A foreign writer recreates the session file with a fresh identity.
        log.append("dashboard:reborn", "assistant", "foreign row in the new file")
        path = log._path("dashboard:reborn")
        assert path.exists()

        assert _save_slot_to_history(state, slot, force=True) is False
        content = path.read_text(encoding="utf-8")
        assert (
            "the deleted conversation" not in content
        ), "the pending save merged the deleted window into the recreated file"
        assert (
            "foreign row in the new file" in content
        ), "the guard must leave the new incarnation untouched"

    def test_channel_surfaced_slot_records_the_disk_identity(self, tmp_path, monkeypatch):
        """The channel surfacing path must arm the delete-won identity too.

        ``surface_channel_session`` adopts an append-created transcript — its
        slot never runs the dashboard rehydrate paths, so without recording
        ``_disk_meta_created_at`` here the identity arm of the delete-won
        guard fails open for every channel tab: delete -> inbound append
        recreates the file -> the pending save merges the deleted window into
        the new transcript.
        """
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        key = "slack:thread-77"
        log.append(key, "user", "channel turn")
        meta = log.get_metadata(key)
        assert meta.get("created_at"), "fixture transcript must carry an identity"

        slot = channel_slots.surface_channel_session(
            state,
            {"key": key, "title": "chan"},
            meta,
            log.read_messages_chained(key),
            session_key=key,
        )
        assert slot is not None
        assert slot._disk_meta_created_at == str(
            meta["created_at"]
        ), "channel surfacing must record the observed disk identity"

        # Delete, then a foreign append recreates the file with a new identity:
        # the surviving slot's pending save must skip, not merge.
        assert log.delete_session(key) is True
        log.append(key, "assistant", "new incarnation row")
        assert _save_slot_to_history(state, slot, force=True) is False
        content = log._path(key).read_text(encoding="utf-8")
        assert (
            "channel turn" not in content
        ), "the channel slot's save merged the deleted window into the recreated file"

    def test_failed_first_save_is_not_mistaken_for_deletion(self, tmp_path, monkeypatch):
        """A fork whose best-effort first save failed must still persist on retry.

        ``chat_fork`` saves the destination slot best-effort and then sets
        ``_resumed_count`` unconditionally — a transient first-write failure is
        swallowed (dirty re-armed) and leaves a slot with resumed evidence but
        NO file and NO observed identity. An evidence gate built on the
        counters alone would misread that as "was on disk, now deleted", skip
        the retry, clear ``_dirty``, and lose the acknowledged fork at the next
        restart. The gate must require an observed disk identity
        (``_disk_meta_created_at``), which only a real hydrate or a committed
        save records.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkchild")
        slot.append("user", "the forked conversation", "msg msg-u")
        slot.drain()
        # As chat_fork leaves the slot when its best-effort save failed: the
        # optimistic counter is set, no write ever committed, no identity.
        slot._resumed_count = len(slot.messages)
        assert slot._disk_meta_created_at == ""

        assert (
            _save_slot_to_history(state, slot, force=True) is True
        ), "the retry of a failed first save was misread as delete-won"
        path = state.conversation_log._path("dashboard:forkchild")
        assert path.exists(), "the acknowledged fork never reached disk"
        assert "the forked conversation" in path.read_text(encoding="utf-8")

    def test_zero_message_resumed_session_delete_still_wins(self, tmp_path, monkeypatch):
        """A restored EMPTY session's delete must beat the save of its first message.

        A zero-message restore leaves every window counter at 0, so an
        evidence gate that requires the counters alongside the identity skips
        the guard for exactly this slot — the save of the session's first
        message would recreate the deleted history. Identity alone must gate.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # A session file with metadata but no message rows — e.g. a titled
        # session whose turns were all trimmed/consolidated away.
        log.update_metadata("dashboard:empty", {"title": "empty but real"})
        meta = log.get_metadata("dashboard:empty")
        assert meta.get("created_at")

        slot = state.get_or_create_slot("empty")
        # As a restore of that session records it: identity observed, all
        # window counters zero (nothing to load).
        slot._disk_meta_created_at = str(meta["created_at"])
        assert slot._resumed_count == 0
        assert slot._disk_older_count == 0
        assert slot._disk_window_len == 0

        # First message arrives, then the permanent delete wins the lock.
        slot.append("user", "first message", "msg msg-u")
        slot.drain()
        assert log.delete_session("dashboard:empty") is True

        assert _save_slot_to_history(state, slot, force=True) is False
        assert not log._path(
            "dashboard:empty"
        ).exists(), "the first-message save recreated a deleted zero-message session"

    def test_unreadable_metadata_defers_the_save_instead_of_writing(self, tmp_path, monkeypatch):
        """A transient metadata read failure must fail CLOSED, not blank the check.

        ``get_metadata`` returns ``{}`` for both "no metadata" and "read
        failed"; blanking the identity comparison on a transient failure would
        let a pending save overwrite a replacement session with deleted
        content. The save must refuse to write (raising, so ``_dirty`` stays
        armed and the flush retries) until the metadata is readable again.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("flaky")
        slot.append("user", "hello", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        before = log._path("dashboard:flaky").read_text(encoding="utf-8")

        monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, False))
        with pytest.raises(OSError, match="unreadable"):
            _save_slot_to_history(state, slot, force=True)
        assert (
            log._path("dashboard:flaky").read_text(encoding="utf-8") == before
        ), "the save wrote through an unverifiable identity"

    def test_probe_refuses_the_copy_when_metadata_is_unreadable(self, tmp_path, monkeypatch):
        """``session_was_deleted`` must fail closed on an unreadable metadata line."""
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            session_was_deleted,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("murky")
        slot.append("user", "hello", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert session_was_deleted(state, slot) is False

        monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, False))
        assert (
            session_was_deleted(state, slot) is True
        ), "an unverifiable identity must refuse the copy, not allow it"

    def test_first_save_of_a_fresh_slot_still_creates_the_file(self, tmp_path, monkeypatch):
        """Control: the guard must not break the normal first create — a
        brand-new slot has no on-disk evidence and starts with no file."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("fresh")
        slot.append("user", "first message")
        slot.drain()

        _save_slot_to_history(state, slot, force=True)
        assert state.conversation_log._path(
            "dashboard:fresh"
        ).exists(), "the delete-won guard aborted a legitimate first save"

    @pytest.mark.asyncio
    async def test_fork_aborts_when_the_source_was_deleted(self, tmp_path, monkeypatch):
        """A fork must not republish a permanently deleted conversation.

        The fork's pre-copy flush runs with ``best_effort=False`` and must not
        treat any non-raising return as a confirmed durable write. A delete-won
        skip raises nothing, and the fork's DESTINATION slot is brand new — it
        carries no delete evidence, so its save would proceed and the destroyed
        conversation would come back under a fresh key. The fork must abort
        instead.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkgone")
        slot.append("user", "keep me?", "msg msg-u")
        slot.append("assistant", "sure", "msg msg-a")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        # New unpersisted activity keeps the slot dirty, so the fork takes its
        # durable-flush arm.
        slot.append("user", "one more", "msg msg-u")
        slot._dirty = True

        assert state.conversation_log.delete_session("dashboard:forkgone") is True

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/forkgone/fork",
                json={"prompt": "forked"},
            )
            payload = await resp.json()

        assert (
            resp.status == 409
        ), f"fork of a deleted session must abort, got {resp.status} {payload}"
        assert not state.conversation_log._path(
            "dashboard:forkgone"
        ).exists(), "the fork's flush recreated the deleted source session"
        # No fork session file may exist either — the copy was refused.
        session_files = [
            p.name for p in tmp_path.glob("*.jsonl") if "forkgone" in p.name or "fork" in p.name
        ]
        assert (
            session_files == []
        ), f"a fork of the deleted conversation was published: {session_files}"

    @pytest.mark.asyncio
    async def test_fork_rolls_back_a_delete_landing_during_the_destination_save(
        self, tmp_path, monkeypatch
    ):
        """A delete committing INSIDE the destination save must still win.

        The pre-copy probe passes (the source is alive when it runs) and the
        destination save takes the DESTINATION's history lock, which does not
        serialise against the source's delete. So the delete can commit inside
        that await, and the fork would be acknowledged holding a full copy of a
        conversation the user destroyed. The handler must refuse at the
        acknowledgment boundary and remove the copy it had already written --
        the same rule ``build_transfer_bundle_async`` applies by re-probing
        after assembly.
        """
        from kiro_crew.dashboard import chat_fork as fork_mod
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("forkrace")
        slot.append("user", "delete me", "msg msg-u")
        slot.append("assistant", "noted", "msg msg-a")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        # Not dirty: the handler's pre-copy flush arms are gated on ``_dirty``,
        # so the ONLY save in this fork is the destination's — which is exactly
        # where the race lives.
        slot._dirty = False
        source_path = state.conversation_log._path("dashboard:forkrace")
        assert source_path.exists()
        before = {p.resolve() for p in tmp_path.rglob("*.jsonl")}

        real_save = fork_mod.save_slot_off_loop
        dest_keys: list[str] = []

        async def _save_then_delete(st, target, *args, **kwargs):
            result = await real_save(st, target, *args, **kwargs)
            if target is not slot:
                dest_keys.append(target.key)
                # The permanent delete commits while the destination save is in
                # flight, i.e. after the pre-copy probe has already passed.
                assert st.conversation_log.delete_session("dashboard:forkrace") is True
            return result

        monkeypatch.setattr(fork_mod, "save_slot_off_loop", _save_then_delete)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/forkrace/fork",
                json={"prompt": "forked"},
            )
            payload = await resp.json()

        assert (
            resp.status == 409
        ), f"the delete must win over an unacknowledged fork, got {resp.status} {payload}"
        assert payload.get("code") == "fork_source_deleted"
        assert not source_path.exists()
        # No transcript may survive the rollback: comparing against the
        # pre-fork set keeps this independent of how the fork mints its key.
        leftover = sorted(p.name for p in tmp_path.rglob("*.jsonl") if p.resolve() not in before)
        assert leftover == [], f"a copy of the deleted conversation remains on disk: {leftover}"
        # …and the destination slot must be gone from the live set too. The key
        # comes from the save the handler actually made, not the 409 body.
        assert dest_keys, "the destination save never ran, so this pins nothing"
        assert state._slots.get(dest_keys[0]) is None, "the rolled-back fork is still a live slot"

    def test_probe_catches_a_delete_landing_between_its_stat_and_metadata_read(
        self, tmp_path, monkeypatch
    ):
        """The probe is lock-free, so a delete can land in the middle of it.

        ``get_metadata_status`` reports a file that does not exist as
        ``({}, True)`` -- by its own contract a GENUINE empty answer, not an
        unreadable one. So a delete committing between the probe's ``stat`` and
        its metadata read leaves an empty ``created_at`` that must not be read
        as "legacy metadata, fail open": that would answer "not deleted" for a
        session that is gone and let fork/transfer republish it. The save's own
        guard cannot hit this -- it reads both inside ``_locked``, the lock
        ``delete_session`` unlinks under.
        """
        from kiro_crew.dashboard.chat_persistence import session_was_deleted

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:midprobe", "user", "delete me")
        slot = state.get_or_create_slot("midprobe")
        slot._disk_meta_created_at = str(log.get_metadata("dashboard:midprobe")["created_at"])
        # Control: the session is alive, so the probe must not refuse the copy.
        assert session_was_deleted(state, slot) is False

        real_status = log.get_metadata_status

        def _delete_then_read(key):
            # The permanent delete commits after the probe's stat() has already
            # seen the file and before the probe reads its metadata.
            log.delete_session("dashboard:midprobe")
            return real_status(key)

        monkeypatch.setattr(log, "get_metadata_status", _delete_then_read)
        assert (
            session_was_deleted(state, slot) is True
        ), "the probe missed a delete landing between its stat and its metadata read"

    def test_probe_still_fails_open_on_legacy_metadata_without_created_at(
        self, tmp_path, monkeypatch
    ):
        """The other empty: a live file whose metadata carries no ``created_at``.

        The re-stat must not turn the documented fail-open for legacy metadata
        into a refusal -- the file is there, so the copy proceeds.
        """
        from kiro_crew.dashboard.chat_persistence import session_was_deleted

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:legacymeta", "user", "keep me")
        slot = state.get_or_create_slot("legacymeta")
        slot._disk_meta_created_at = str(log.get_metadata("dashboard:legacymeta")["created_at"])

        monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, True))
        assert (
            session_was_deleted(state, slot) is False
        ), "legacy metadata with no created_at must still fail open while the file exists"

    def test_write_target_drift_to_another_file_is_not_read_as_a_delete(
        self, tmp_path, monkeypatch
    ):
        """A routing drift to a DIFFERENT file is not a delete.

        A cron-born slot observes its identity under ``cron:<id>`` (file A).
        Later its ``linked_session_key`` lapses, so ``slot_history_key`` falls
        back to the ``dashboard:cron_<id>`` spelling (file B) — a legitimately
        existing transcript another writer created, with its own ``created_at``.
        The delete witnesses stat file B and compare its unrelated identity
        against the one observed from file A; without the observed-key check
        they read "different created_at" as "deleted and recreated", skip every
        save (fork then 409), and the session's new activity silently never
        persists. The guard must tell "different file" apart from "same file,
        new incarnation": a key mismatch is no delete evidence for the current
        target, so the save must land and ``session_was_deleted`` stay False.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            session_was_deleted,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log

        # File A: the cron transcript the slot observed its identity from.
        cron_key = "cron:job-42"
        slot = state.get_or_create_slot("cron_job-42")
        slot.linked_session_key = cron_key
        slot.append("user", "live cron activity", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_created_at, "the committed save must record identity"
        assert slot._disk_meta_key == cron_key, "identity must be keyed to file A"
        file_a_created = slot._disk_meta_created_at

        # File B: a DIFFERENT transcript at the dashboard: fallback spelling,
        # created independently with its own identity.
        # Pin B's identity explicitly: two clock reads can land in one tick on
        # coarse-resolution clocks and stamp both files identically.
        fallback_key = "dashboard:cron_job-42"
        log.append(fallback_key, "assistant", "unrelated row in the other file")
        log.update_metadata(fallback_key, {"created_at": "2001-01-01T00:00:00+00:00"})
        file_b_created = str(log.get_metadata(fallback_key)["created_at"])
        assert (
            file_b_created and file_b_created != file_a_created
        ), "fixture must give the two files distinct identities"

        # The routing drifts: linked_session_key lapses, so slot_history_key
        # now resolves to the dashboard: fallback (file B), not file A.
        slot.linked_session_key = ""
        from kiro_crew.dashboard.chat_utils import slot_history_key

        assert slot_history_key(slot) == fallback_key, "drift precondition"

        # The drift must NOT be read as a delete-recreate of the observed file.
        assert (
            session_was_deleted(state, slot) is False
        ), "a write-target drift to another file was misread as a delete"

        # And a new save must land, not abort under the delete-won guard.
        slot.append("user", "more cron activity after the drift", "msg msg-u2")
        slot.drain()
        assert (
            _save_slot_to_history(state, slot, force=True) is True
        ), "the save aborted because the drift was read as a delete-won race"

    def test_drifted_save_does_not_reuse_the_observed_files_prefix_cache(
        self, tmp_path, monkeypatch
    ):
        """A drifted save reads the target file even when its stat matches the cache.

        The frozen-prefix cache is keyed on the file's mtime and size only. It
        was filled by the save to file A. After the write target drifts to file
        B, a B whose stat happens to match those values (equal size on a
        coarse-mtime filesystem) would be served A's cached prefix, so the
        rewrite of B would never read B and would drop B's own rows. The test
        pins that coincidence by aligning the cache with B's stat.
        """
        import os

        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        observed_key = "cron:job-cache"
        target_key = "dashboard:cron_job-cache"
        slot = state.get_or_create_slot("cron_job-cache")
        slot.linked_session_key = observed_key
        slot.append("user", "row from A", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_key == observed_key
        assert slot._frozen_prefix_cache is not None, "fixture needs a filled cache"

        log.append(target_key, "assistant", "row only B has")
        slot.linked_session_key = ""
        slot.append("user", "row saved after the drift", "msg msg-u2")
        slot.drain()

        # Make B's stat match the cache, as a coarse-mtime filesystem can.
        st = os.stat(log._path(target_key))
        cache = slot._frozen_prefix_cache
        slot._frozen_prefix_cache = (st.st_mtime, st.st_size, *cache[2:])

        assert _save_slot_to_history(state, slot, force=True) is True
        target_text = log._path(target_key).read_text(encoding="utf-8")
        assert "row only B has" in target_text, "the drifted save dropped B's own row"
        assert "row saved after the drift" in target_text

    def test_delete_of_observed_file_before_drift_is_not_resurrected_in_the_new_target(
        self, tmp_path, monkeypatch
    ):
        """A permanent delete of the observed file wins even once the key drifts.

        The mirror of the drift case above. A cron-born slot observes its
        identity under ``cron:<id>`` (file A) and the user then permanently
        deletes A. Only after that does the slot's ``linked_session_key`` lapse,
        so ``slot_history_key`` drifts the write target to
        ``dashboard:cron_<id>`` (file B). The slot's window still holds A's rows.
        A mismatch branch that returned "not deleted" outright would merge those
        rows into B and bring the deleted conversation back under another key.
        The witnesses must stat the OBSERVED file (A) on a mismatch: A is gone,
        so the verdict is deleted and the save into B is refused.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            session_was_deleted,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log

        # File A: the cron transcript the slot observed its identity from.
        cron_key = "cron:job-77"
        slot = state.get_or_create_slot("cron_job-77")
        slot.linked_session_key = cron_key
        slot.append("user", "secret cron activity", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_key == cron_key, "identity must be keyed to file A"
        assert log._path(cron_key).exists()

        # The user permanently deletes A.
        assert log.delete_session(cron_key) is True
        assert not log._path(cron_key).exists()

        # Only then does the routing drift to the dashboard: fallback (file B).
        slot.linked_session_key = ""
        from kiro_crew.dashboard.chat_utils import slot_history_key

        fallback_key = "dashboard:cron_job-77"
        assert slot_history_key(slot) == fallback_key, "drift precondition"

        # The observed file A is gone, so the drift must read as a delete.
        assert (
            session_was_deleted(state, slot) is True
        ), "a delete of the observed file was missed once the target drifted"

        # A save must NOT merge A's surviving window rows into file B.
        slot.append("user", "more activity after the delete and drift", "msg msg-u2")
        slot.drain()
        assert (
            _save_slot_to_history(state, slot, force=True) is False
        ), "the deleted conversation was resurrected under the drifted key"
        assert not log._path(fallback_key).exists(), "B was written from the deleted session's rows"

    def test_observed_file_delete_during_empty_metadata_read_refuses_drifted_save(
        self, tmp_path, monkeypatch
    ):
        """A delete between A's stat and metadata read cannot republish A into B."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        observed_key = "cron:job-empty-race"
        target_key = "dashboard:cron_job-empty-race"
        slot = state.get_or_create_slot("cron_job-empty-race")
        slot.linked_session_key = observed_key
        slot.append("user", "private row from A", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_key == observed_key

        log.append(target_key, "assistant", "B stays independent")
        target_before = log._path(target_key).read_text(encoding="utf-8")
        slot.linked_session_key = ""
        slot.append("user", "unpersisted A row", "msg msg-u2")
        slot.drain()

        real_status = log.get_metadata_status
        deleted = False

        def _delete_a_then_read(key):
            nonlocal deleted
            if key == observed_key and not deleted:
                deleted = True
                assert log.delete_session(observed_key) is True
            return real_status(key)

        monkeypatch.setattr(log, "get_metadata_status", _delete_a_then_read)
        assert _save_slot_to_history(state, slot, force=True) is False
        assert deleted, "the metadata read never opened the deletion window"
        assert not log._path(observed_key).exists()
        assert log._path(target_key).read_text(encoding="utf-8") == target_before

    def test_drifted_save_holds_source_and_target_locks_through_commit(self, tmp_path, monkeypatch):
        """The A-to-B delete check and B commit share one sorted lock union.

        The test observes the real lock contexts rather than betting on a thread
        schedule. It fails if A is absent from the acquisition, if either lock is
        released before source verification or target commit, or if acquisition
        stops being deterministic.
        """
        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.history import transcript_lock_stems

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        observed_key = "cron:job-lock-span"
        target_key = "dashboard:cron_job-lock-span"
        slot = state.get_or_create_slot("cron_job-lock-span")
        slot.linked_session_key = observed_key
        slot.append("user", "private row from A", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_key == observed_key

        log.append(target_key, "assistant", "B starts independently")
        slot.linked_session_key = ""
        slot.append("user", "row saved after the drift", "msg msg-u2")
        slot.drain()

        expected = sorted(
            set(transcript_lock_stems(observed_key)) | set(transcript_lock_stems(target_key))
        )
        acquired: list[str] = []
        active: list[str] = []
        source_verified = False
        target_committed = False
        real_locked_stem = log._locked_stem
        real_status = log.get_metadata_status
        real_atomic_write = chat_persistence.atomic_write

        @contextmanager
        def _tracked_locked_stem(stem):
            acquired.append(stem)
            with real_locked_stem(stem):
                active.append(stem)
                try:
                    yield
                finally:
                    assert active.pop() == stem

        def _tracked_status(key):
            nonlocal source_verified
            if key == observed_key:
                assert active == expected, "source verification did not hold A and B"
                source_verified = True
            return real_status(key)

        def _tracked_atomic_write(path, payload, **kwargs):
            nonlocal target_committed
            if path == log._path(target_key):
                assert source_verified, "target committed before source verification"
                assert active == expected, "target commit did not retain A and B"
                target_committed = True
            return real_atomic_write(path, payload, **kwargs)

        monkeypatch.setattr(log, "_locked_stem", _tracked_locked_stem)
        monkeypatch.setattr(log, "get_metadata_status", _tracked_status)
        monkeypatch.setattr(chat_persistence, "atomic_write", _tracked_atomic_write)

        assert _save_slot_to_history(state, slot, force=True) is True
        assert acquired == expected
        assert source_verified is True
        assert target_committed is True
        assert active == []

    def test_transient_observed_stat_error_raises_and_keeps_the_flush_retry(
        self, tmp_path, monkeypatch
    ):
        """An unverifiable A raises for saves and refuses lock-free copies."""
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            session_was_deleted,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        observed_key = "cron:job-stat-retry"
        target_key = "dashboard:cron_job-stat-retry"
        slot = state.get_or_create_slot("cron_job-stat-retry")
        slot.linked_session_key = observed_key
        slot.append("user", "private row from A", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_key == observed_key

        log.append(target_key, "assistant", "B stays independent")
        target_before = log._path(target_key).read_text(encoding="utf-8")
        slot.linked_session_key = ""
        slot.append("user", "retry me", "msg msg-u2")
        slot.drain()
        assert slot._dirty is True

        real_path = log._path

        class _UnavailableObservedPath:
            def stat(self):
                raise OSError("observed file temporarily unavailable")

        def _path(key):
            if key == observed_key:
                return _UnavailableObservedPath()
            return real_path(key)

        monkeypatch.setattr(log, "_path", _path)
        with pytest.raises(OSError, match="temporarily unavailable"):
            _save_slot_to_history(state, slot, force=True)
        assert session_was_deleted(state, slot) is True

        state.flush_slot_now(slot)
        assert slot._dirty is True, "the transient save failure discarded its retry"
        assert real_path(target_key).read_text(encoding="utf-8") == target_before

        monkeypatch.setattr(log, "_path", real_path)
        real_status = log.get_metadata_status
        monkeypatch.setattr(
            log,
            "get_metadata_status",
            lambda key: ({}, False) if key == observed_key else real_status(key),
        )
        with pytest.raises(OSError, match="transiently unreadable"):
            _save_slot_to_history(state, slot, force=True)
        assert session_was_deleted(state, slot) is True
        state.flush_slot_now(slot)
        assert slot._dirty is True, "the unreadable metadata failure discarded its retry"
        assert real_path(target_key).read_text(encoding="utf-8") == target_before

    def test_alias_spellings_of_one_transcript_still_witness_a_delete(self, tmp_path, monkeypatch):
        """Two spellings of the SAME file are not a write-target drift.

        An unbound channel slot is observed under the stem spelling
        (``slack_<ts>``) and bound to the canonical ``slack:<ts>`` later; both
        fold onto one ``.jsonl``. A raw string comparison would read the bind as
        a drift to another file and switch the delete witnesses off, so a
        permanent delete landing before the next save would be undone.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            session_was_deleted,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log

        stem_key = "slack_1712345678.123456"
        canonical_key = "slack:1712345678.123456"
        assert log._path(stem_key) == log._path(canonical_key), "fixture: one file"

        slot = state.get_or_create_slot("aliasbind")
        slot.linked_session_key = stem_key
        slot.append("user", "thread activity", "msg msg-u")
        slot.drain()
        assert _save_slot_to_history(state, slot, force=True) is True
        assert slot._disk_meta_key == stem_key

        # The bind re-spells the key; the file is the same one.
        slot.linked_session_key = canonical_key
        assert log.delete_session(canonical_key) is True
        assert not log._path(canonical_key).exists()

        assert (
            session_was_deleted(state, slot) is True
        ), "an alias re-spelling switched off the delete witness"
        slot.append("user", "late activity", "msg msg-u2")
        slot.drain()
        assert (
            _save_slot_to_history(state, slot, force=True) is False
        ), "the save resurrected a deleted transcript under its alias spelling"
        assert not log._path(canonical_key).exists()


# ── Slot lifecycle ──


class TestSlotLifecycle:
    @pytest.mark.asyncio
    async def test_list_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("a")
        state.get_or_create_slot("b")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots")
            data = await resp.json()
            keys = [s["key"] for s in data]
            assert "a" in keys and "b" in keys

    @pytest.mark.asyncio
    async def test_list_slots_schedules_source_refresh_with_push_callback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.handlers import source_providers

        scheduler = MagicMock()
        monkeypatch.setattr(source_providers, "schedule_check_refresh", scheduler)
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slot = state.get_or_create_slot("source")
        url = "https://github.com/acme/repo/pull/12"
        slot.append("user", url)

        @web.middleware
        async def owner_auth(request, handler):
            request["user"] = "U_OWNER"
            request["app"] = ""
            return await handler(request)

        app = _make_app(state)
        app.middlewares.append(owner_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            assert resp.status == 200

        scheduler.assert_called_once()
        urls, callback = scheduler.call_args.args
        assert urls == [url]
        assert callback == state.push_slots_update

    @pytest.mark.asyncio
    async def test_list_slots_omits_status_and_refresh_for_non_owner(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import state as state_module
        from kiro_crew.dashboard.handlers import source_providers

        scheduler = MagicMock()
        monkeypatch.setattr(source_providers, "schedule_check_refresh", scheduler)
        monkeypatch.setattr(
            state_module,
            "_cached_check_status",
            lambda _url: {"ci": "passed", "state": "open"},
        )
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slot = state.get_or_create_slot("source")
        url = "https://github.com/acme/private/pull/12"
        slot.append("user", url)

        @web.middleware
        async def non_owner_auth(request, handler):
            request["user"] = "U_OTHER"
            request["app"] = ""
            return await handler(request)

        app = _make_app(state)
        app.middlewares.append(non_owner_auth)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/chat/slots")
            payload = await resp.json()

        assert resp.status == 200
        link = next(item for item in payload if item["key"] == "source")["source_links"][0]
        assert link == {
            "provider": "github",
            "number": 12,
            "url": url,
            "kind": "change",
            "label": "#12",
            "identity": '["github","github.com","acme","private",12,"","change",""]',
        }
        scheduler.assert_not_called()

    def test_slot_status_serialization_requires_owner_opt_in(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            state_module,
            "_cached_check_status",
            lambda _url: {"ci": "passed", "state": "open"},
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("source")
        slot.append("user", "https://github.com/acme/private/pull/12")

        generic_link = state.serialize_slots()[0]["source_links"][0]
        owner_link = state.serialize_slots(include_check_status=True)[0]["source_links"][0]

        assert "ci" not in generic_link
        assert "state" not in generic_link
        assert owner_link["ci"] == "passed"
        assert owner_link["state"] == "open"

    @pytest.mark.asyncio
    async def test_approve_no_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_approve_resolves_future(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            data = await resp.json()
            assert data["ok"] is True
            assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_trust_sets_flag_and_approves(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut
        slot.messages.append(
            {
                "role": "permission",
                "content": "Running: ls",
                "cls": json.dumps(
                    {
                        "request_id": "test",
                        "full_command": "ls",
                        "trust_grantable": "1",
                    }
                ),
            }
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust"})
            data = await resp.json()
            assert data["ok"] is True
            assert slot._trust is True
            assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_approve_broadcasts_approval_resolved_single_pending(self, tmp_path, monkeypatch):
        """Single pending future without explicit request_id: extracts id and broadcasts."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-abc"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "approved"})
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved",
                # ``slot`` keys the frame for the slot-scoped WS gate: without it
                # an app token never receives its OWN approval resolution.
                {"id": "req-abc", "approved": True, "slot": "s1"},
            )

    @pytest.mark.asyncio
    async def test_approve_broadcasts_with_explicit_request_id(self, tmp_path, monkeypatch):
        """Explicit request_id is forwarded in the broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-xyz"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "approved", "request_id": "req-xyz"}
            )
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved",
                # ``slot`` keys the frame for the slot-scoped WS gate: without it
                # an app token never receives its OWN approval resolution.
                {"id": "req-xyz", "approved": True, "slot": "s1"},
            )

    @pytest.mark.asyncio
    async def test_reject_broadcasts_approved_false(self, tmp_path, monkeypatch):
        """Rejection broadcasts approved=False."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-rej"] = fut
        state.broadcast_ws = MagicMock()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve", json={"action": "rejected", "request_id": "req-rej"}
            )
            assert (await resp.json())["ok"] is True
            state.broadcast_ws.assert_any_call(
                "approval_resolved",
                # ``slot`` keys the frame for the slot-scoped WS gate: without it
                # an app token never receives its OWN approval resolution.
                {"id": "req-rej", "approved": False, "slot": "s1"},
            )


# ── Tests: slot.task None guard ──


class TestSlotTaskNoneGuard:
    """stop/delete must not crash when slot.task is None."""

    @pytest.mark.asyncio
    async def test_stop_not_running(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        state.get_or_create_slot("s1")
        # task is None → running is False → stop is a no-op
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_delete_not_running(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        # task is None → running is False → delete skips cancel
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_stop_with_real_task_cancels(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        state.sessions.stop_turn = AsyncMock(return_value="soft")
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/stop")
            assert resp.status == 200
            state.sessions.stop_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_with_real_task_cancels(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            with patch("kiro_crew.dashboard.chat_handlers.save_slot_off_loop"):
                resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200
            assert slot.task.cancelled()


# ── Bulk cleanup tests ──


class TestBulkCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_archives_stale_sessions(self, tmp_path, monkeypatch):
        """Stale sessions are archived; fresh and pinned are kept."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()

        stale = state.get_or_create_slot("stale1")
        stale.append("user", "old msg", ts=old_ts)
        stale.drain()

        fresh = state.get_or_create_slot("fresh1")
        fresh.append("user", "new msg", ts=fresh_ts)
        fresh.drain()

        pinned = state.get_or_create_slot("pinned1")
        pinned.pinned = True
        pinned.append("user", "pinned msg", ts=old_ts)
        pinned.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3, "active_slot": "fresh1"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["archived"] == 1
            assert "stale1" in data["keys"]

        assert "stale1" not in state._slots
        assert "fresh1" in state._slots
        assert "pinned1" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_by_an_app_keeps_its_slot_linked_to_another_session(
        self, tmp_path, monkeypatch
    ):
        """An app's own slot running on a channel's session is not the app's to archive.

        The per-slot checkpoint refuses that slot on every ``/api/chat/slots/{slot}``
        route (``app_owns_slot_session``); the bulk cleanup applies the same rule.
        """
        from datetime import datetime, timedelta, timezone

        from slot_ownership_checkpoint_helpers import _as

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        for name in ("own", "linked"):
            slot = state.get_or_create_slot(name, app="crew-keyboard")
            slot.append("user", "old msg", ts=old_ts)
            slot.drain()
        state._slots["linked"].linked_session_key = "slack:1700000000.000100"

        app = _make_app(state)
        app.middlewares.insert(0, _as("crew-keyboard"))
        async with TestClient(TestServer(app)) as client:
            dry = await client.post(
                "/api/chat/slots/cleanup", json={"max_inactive_days": 3, "dry_run": True}
            )
            assert (await dry.json())["keys"] == ["own"]
            resp = await client.post("/api/chat/slots/cleanup", json={"max_inactive_days": 3})
            data = await resp.json()
            assert (data["archived"], data["keys"]) == (1, ["own"])

        assert "own" not in state._slots
        assert "linked" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_skips_active_slot(self, tmp_path, monkeypatch):
        """The active slot is never archived even if stale."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        slot = state.get_or_create_slot("active")
        slot.append("user", "old", ts=old_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1, "active_slot": "active"},
            )
            data = await resp.json()
            assert data["archived"] == 0
        assert "active" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_saves_to_history(self, tmp_path, monkeypatch):
        """Archived sessions are persisted to conversation log."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("to-archive")
        slot.append("user", "save me", ts=old_ts)
        slot.append("assistant", "saved", ts=old_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3},
            )

        msgs = state.conversation_log.read_messages("dashboard:to-archive")
        assert len(msgs) == 2
        assert msgs[0]["content"] == "save me"

    @pytest.mark.asyncio
    async def test_cleanup_defaults_to_3_days(self, tmp_path, monkeypatch):
        """Without max_inactive_days, defaults to 3."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        ts_2d = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        slot = state.get_or_create_slot("recent")
        slot.append("user", "hi", ts=ts_2d)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/cleanup", json={})
            data = await resp.json()
            assert data["archived"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["1e999", "-1e999", '"soon"'])
    async def test_cleanup_falls_back_to_3_days_on_an_unparsable_value(
        self, tmp_path, monkeypatch, raw
    ):
        """JSON reads ``1e999`` as an infinite float, which ``int`` cannot convert.

        It takes the same 3-day fallback as a non-numeric string: a slot idle for
        2 days stays and one idle for 4 days is archived.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        for name, days in (("two-days", 2), ("four-days", 4)):
            slot = state.get_or_create_slot(name)
            slot.append("user", "hi", ts=(now - timedelta(days=days)).isoformat())
            slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                data='{"max_inactive_days": %s}' % raw,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            data = await resp.json()
        assert data["archived"] == 1
        assert state.get_slot("two-days") is not None
        assert state.get_slot("four-days") is None

    @pytest.mark.asyncio
    async def test_cleanup_with_a_huge_integer_archives_nothing(self, tmp_path, monkeypatch):
        """A JSON integer too large for a float: no slot is that old, so nothing is
        archived, and the cutoff arithmetic does not raise."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        slot = state.get_or_create_slot("ten-years")
        slot.append(
            "user", "hi", ts=(datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()
        )
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                data='{"max_inactive_days": 1%s}' % ("0" * 400),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            data = await resp.json()
        assert data["archived"] == 0
        assert state.get_slot("ten-years") is not None

    @pytest.mark.asyncio
    async def test_cleanup_empty_slots_uses_created_at(self, tmp_path, monkeypatch):
        """Slots with no messages use created_at for staleness."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        slot = state.get_or_create_slot("empty-old")
        slot.created_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 7},
            )
            data = await resp.json()
            assert data["archived"] == 1
            assert "empty-old" in data["keys"]

    @pytest.mark.asyncio
    async def test_cleanup_no_stale_returns_zero(self, tmp_path, monkeypatch):
        """When all sessions are fresh, nothing is archived."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timezone

        fresh_ts = datetime.now(timezone.utc).isoformat()
        slot = state.get_or_create_slot("fresh")
        slot.append("user", "hi", ts=fresh_ts)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["archived"] == 0
            assert data["keys"] == []

    @pytest.mark.asyncio
    async def test_cleanup_rollback_on_save_failure(self, tmp_path, monkeypatch):
        """When _save_slot_to_history raises, slot is restored and reported as failed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("fail-save")
        slot.append("user", "msg", ts=old_ts)
        slot.drain()

        with patch(
            "kiro_crew.dashboard.chat_handlers.save_slot_off_loop",
            side_effect=OSError("disk full"),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/cleanup",
                    json={"max_inactive_days": 1},
                )
                data = await resp.json()
                assert data["archived"] == 0
                assert "fail-save" in data["failed"]

        # Slot must be restored (not lost)
        assert "fail-save" in state._slots
        # No history entry should exist (save failed)
        msgs = state.conversation_log.read_messages("dashboard:fail-save")
        assert len(msgs) == 0

    @pytest.mark.asyncio
    async def test_cleanup_cancels_running_task(self, tmp_path, monkeypatch):
        """Running tasks on stale slots are cancelled after archive."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        slot = state.get_or_create_slot("running1")
        slot.append("user", "msg", ts=old_ts)
        slot.drain()
        slot.task = asyncio.get_running_loop().create_future()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["archived"] == 1
        assert slot.task.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_skips_unparseable_timestamps(self, tmp_path, monkeypatch):
        """Slots with unparseable timestamps are skipped, not archived."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        slot = state.get_or_create_slot("bad-ts")
        slot.append("user", "hi", ts="not-a-date")
        slot.created_at = "also-not-a-date"
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 1},
            )
            data = await resp.json()
            assert data["archived"] == 0
        assert "bad-ts" in state._slots

    @pytest.mark.asyncio
    async def test_cleanup_dry_run_returns_keys_without_archiving(self, tmp_path, monkeypatch):
        """dry_run=True returns stale keys and active_is_stale but does not archive anything."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()

        stale = state.get_or_create_slot("stale1")
        stale.append("user", "old msg", ts=old_ts)
        stale.drain()

        active_stale = state.get_or_create_slot("active1")
        active_stale.append("user", "old active msg", ts=old_ts)
        active_stale.drain()

        fresh = state.get_or_create_slot("fresh1")
        fresh.append("user", "new msg", ts=fresh_ts)
        fresh.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/cleanup",
                json={"max_inactive_days": 3, "active_slot": "active1", "dry_run": True},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["dry_run"] is True
            assert "stale1" in data["keys"]
            assert "active1" not in data["keys"]
            assert data["count"] == 1
            assert data["active_is_stale"] is True

        # Slots should NOT have been removed
        assert "stale1" in state._slots
        assert "active1" in state._slots
        assert "fresh1" in state._slots
