"""A row whose addressing field cannot be vetted is held aside, not dropped.

Rewriting an ``id`` or ``slot_key`` would leave a row the client cannot act on, so a
credential-shaped or non-printable one is written to a quarantine sidecar and withheld
from the live map instead. These pin that write path: the sidecar rename is durable
before the store drops the rows, an unreadable sidecar refuses writes rather than being
unlinked, and compaction retires only the rows this instance repaired.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, AutoNudgeStoreUnvetted


def _moved_aside_sidecar(base_dir: Path) -> Path:
    """The single ``.corrupt-<ts>`` copy an unreadable sidecar is renamed to."""
    matches = sorted(base_dir.glob("autonudge.quarantine.json.corrupt-*"))
    assert len(matches) == 1, f"expected exactly one moved-aside copy, got {matches!r}"
    return matches[0]


class TestTheQuarantineSidecarIsWrittenSafely:
    """The sidecar must not be able to corrupt or crash the store it protects.

    Ordering: it is written BEFORE the main store, so a sidecar failure leaves the old
    consistent file. Shape: a non-object sidecar is ignored rather than fatal.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _store_with_one_unusable_row(self, tmp_path):
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": [good, unusable]}), encoding="utf-8")
        return store

    @pytest.mark.asyncio
    async def test_a_refused_store_does_not_record_a_delivered_cycle(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): a runtime sidecar refusal left delivered cycles undurable.

        ``_persist_soon`` only LOGS a failed persist, so once the sidecar refusal latch is
        set every write raises while the post-fire ``cycle_count`` bump keeps advancing in
        memory. Cycles are then spent against a count no restart will have seen, so
        ``max_cycles`` bounds nothing durable. Firing must stop instead.
        """
        import kiro_crew.autonudge as _an

        fired: list[object] = []

        async def on_fire(loop):
            fired.append(loop)
            return True

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._on_fire = on_fire
        try:
            await svc.start()
            # Real sleep here, so arming does NOT fire before the latch is set.
            loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
            svc._timers[loop.id].cancel()

            async def _nosleep(_secs):
                return None

            monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
            # The sidecar became unreadable while the service was live.
            svc._load_refused = True
            await svc._timer(loop)

            assert not fired, "a cycle was delivered while no write could be recorded"
            assert svc._loops[loop.id].cycle_count == 0, (
                "cycle_count advanced in memory while persistence was refused, so the "
                "budget a restart reads is already wrong"
            )
        finally:
            # The latch is the condition under test, not the teardown: leaving it set
            # makes a teardown persist raise and mask the assertions above.
            svc._load_refused = False
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_malformed_row_survives_the_rewrite_its_dirty_sibling_triggers(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): ``_unparsed_rows`` had no producer, so the row was deleted.

        The parse ``except`` only logged and continued, so a row this loader cannot read
        reached neither ``_loops`` nor ``_unparsed_rows``. Any sibling that flags the store
        dirty -- the quarantined addressing field here -- then makes the rewrite persist a
        payload the row is simply absent from, with a warning as its only surviving trace.
        """
        malformed = "not-a-row"
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [malformed, unusable]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert malformed in svc._unparsed_rows, "the unreadable row was dropped on load"
        assert svc._store_dirty is True, "the quarantined sibling did not arm a rewrite"
        assert (
            malformed in svc._serialize_state()["loops"]
        ), "the rewrite would have deleted the unreadable row permanently"

    @pytest.mark.asyncio
    async def test_a_non_object_root_refuses_instead_of_crashing_boot(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a non-dict root bypassed the list guard and aborted boot.

        ``"loops" in []`` is False, so a hand-edited list or scalar root reached the row
        loop and raised out of the unguarded ``_load`` that ``start()`` calls. Refusing is
        the same fail-closed answer the not-a-list arm already gives.
        """
        (tmp_path / "autonudge.json").write_text("[]", encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert svc._load_refused is True, "a non-object root did not refuse writes"
        assert svc._loops == {}, "a non-object root armed something"

    @pytest.mark.asyncio
    async def test_one_coincidental_field_does_not_retire_an_unrelated_held_row(
        self, tmp_path
    ) -> None:
        """GPT 5.6: a held row with an unknown field plus one match read as 'repaired'.

        Unknown keys were skipped, so a row this code cannot compare was judged against
        whatever remained -- one coincidentally-equal field stood for the whole row and
        deleted its only durable copy. An unknown key must never match.
        """
        held = {
            "id": self.SECRET,
            "idle_secs": 300,
            "some_future_field": "carried by a newer writer",
        }
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-2",
            "message": "a different instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        ids = [row.get("id") for row in svc._quarantined]
        assert (
            self.SECRET in ids
        ), f"an unrelated held row was retired on a coincidental match: {ids!r}"

    @pytest.mark.asyncio
    async def test_a_repaired_id_keeps_its_held_aside_copy(self, tmp_path) -> None:
        """HOLD AND WARN, on the id path too: only the operator removes a held row.

        Deleting it here needed a fuzzy match on the fields a repair leaves alone, and
        two rows differing only in an unsafe id match the SAME repaired loop. The load
        warning tells the operator to MOVE the row, which empties the sidecar without
        anything guessing.
        """
        held = {
            "id": self.SECRET,
            "slot_key": "chat-9-9",
            "message": "the operator instruction",
            "idle_secs": 300,
        }
        repaired = {
            "id": "loop-clean-id",
            "slot_key": "chat-9-9",
            "message": "the operator instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [repaired]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert "loop-clean-id" in svc._loops, "the repaired row did not arm from the store"
        ids = [row.get("id") for row in svc._quarantined]
        assert (
            self.SECRET in ids
        ), f"the held copy was deleted -- only the operator may remove it: {ids!r}"

    @pytest.mark.asyncio
    async def test_a_repaired_row_keeps_its_held_aside_copy(self, tmp_path) -> None:
        """HOLD AND WARN: nothing auto-retires a held row, because matching is fuzzy.

        The auto-retire matcher was deleted: repair-by-move already empties the sidecar,
        so it earned nothing, while a false positive deleted the held row's only durable
        copy. The residual cost is a repeated load-time warning and a stale sidecar row --
        recoverable, unlike a deletion.
        """
        held = {
            "id": "loop-repairable",
            "slot_key": self.SECRET,
            "message": "x",
            "idle_secs": 300,
        }
        repaired = {
            "id": "loop-repairable",
            "slot_key": "chat-4-4",
            "message": "x",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [repaired]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert "loop-repairable" in svc._loops, "the repaired row did not arm from the store"
        keys = [row.get("slot_key") for row in svc._quarantined]
        assert (
            self.SECRET in keys
        ), f"the held copy was deleted -- only the operator may remove it: {keys!r}"

    @pytest.mark.asyncio
    async def test_a_row_in_both_files_is_quarantined_once_not_twice(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a failed replacement made the next load DUPLICATE a row.

        Once the sidecar write has landed and the main-store replacement then fails, the
        unsafe row is in BOTH files. The load loop reaches it twice and appended it twice,
        so each failed replacement accumulated another copy of the same quarantine record.
        """
        unusable = {
            "id": self.SECRET,
            "slot_key": "chat-9-9",
            "message": "x",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [unusable]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unusable]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            held = [row.get("id") for row in svc._quarantined]
            assert len(held) == 1, (
                "the row present in BOTH files was quarantined twice, so every failed "
                f"replacement accumulates another duplicate record: {len(held)} copies"
            )
            assert not svc._loops, "the unsafe row armed instead of being held aside"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_failed_sidecar_write_leaves_the_store_file_untouched(self, tmp_path) -> None:
        """ORDERING: the main store is not replaced when the sidecar cannot be written."""
        store = self._store_with_one_unusable_row(tmp_path)
        before = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"

            def _boom() -> None:
                raise OSError("sidecar volume is full")

            svc._write_quarantine_sidecar = _boom  # type: ignore[method-assign]
            with pytest.raises(OSError):
                svc._write_state(svc._serialize_state())
            assert store.read_text(encoding="utf-8") == before, (
                "the store was replaced before the sidecar was durable, so a sidecar "
                "failure left committed disk state inconsistent"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_non_object_sidecar_refuses_the_store_rather_than_arming(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): tolerating a bad sidecar armed loops that could not persist.

        The OPPOSITE assertion -- that a list-shaped sidecar is
        "ignored rather than fatal" and the store's own quarantined row survived in
        memory. That tolerance was the defect: writes were already refused, so a loop
        armed under it delivers a cycle it cannot record, and the next restart re-fires
        that cycle past its own cap.

        So the contract is now REFUSE, and the assertions below cover both halves of it:
        nothing arms, and the file survives for the operator to repair. `_load` must
        still not RAISE -- a startup crash would be a third failure mode.
        """
        self._store_with_one_unusable_row(tmp_path)
        sidecar = tmp_path / "autonudge.quarantine.json"
        # A list, not an object -- `raw.get` on this would raise AttributeError.
        sidecar.write_text("[]", encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()  # must not raise
            assert svc._load_refused is True, "a non-object sidecar left writes enabled"
            assert not svc._loops, (
                "loops armed under a refused store; a delivered cycle cannot record "
                f"itself, so a restart repeats it. armed={sorted(svc._loops)!r}"
            )
            assert not svc._quarantined, (
                "rows were held in memory under a refused store, which cannot be "
                "persisted and so is lost silently on restart"
            )
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == "[]"
            ), "the sidecar bytes were lost, so the operator has nothing to repair"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_sidecar_rename_is_durable_before_the_store_drops_the_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the sidecar's RENAME was never flushed.

        The bytes were fsynced and the rename was atomic, so the ordering test above
        passed -- but an atomic rename is only durable once the PARENT DIRECTORY is
        synced. Until then a power-off can return from the replacement and still come
        back to the old directory entry. The main store lands immediately afterwards
        and drops the quarantined rows, so those rows' only remaining copy is a name
        recorded nowhere.

        The assertion is on ORDER, not on the call's existence: syncing the directory
        after the store has already dropped the rows would close no window.
        """
        store = self._store_with_one_unusable_row(tmp_path)
        events: list[str] = []
        real_fsync_dir = _an.fsync_dir
        real_replace = _an.replace_with_retry

        def _record_fsync(path, **kwargs):
            events.append(f"fsync_dir:{Path(path).name}")
            return real_fsync_dir(path, **kwargs)

        def _record_replace(src, dst):
            events.append(f"replace:{Path(dst).name}")
            return real_replace(src, dst)

        monkeypatch.setattr(_an, "fsync_dir", _record_fsync)
        monkeypatch.setattr(_an, "replace_with_retry", _record_replace)

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"

            svc._write_state(svc._serialize_state())

            assert (
                f"replace:{store.name}" in events
            ), f"the main store never landed, so this run proves nothing: {events!r}"
            sidecar_synced = [i for i, e in enumerate(events) if e.startswith("fsync_dir:")]
            assert sidecar_synced, (
                "the sidecar's parent directory was never synced, so its rename is not "
                f"durable when the store drops the quarantined rows: {events!r}"
            )
            assert sidecar_synced[0] < events.index(f"replace:{store.name}"), (
                "the directory sync happened AFTER the store replacement, so there is "
                f"still a window where the held rows exist nowhere durable: {events!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_defaulted_field_is_not_identity_evidence(self, tmp_path) -> None:
        """A hand-edited row matched an unrelated loop on ``active`` alone.

        The gate accepted ANY non-addressing field as corroboration, and ``active``
        defaults to True -- so a two-field row carrying an unsafe ``id`` plus a
        ``slot_key`` a live loop happens to share was retired and compacted away. Only a
        NO-DEFAULT field is identity evidence, and a row lacking one is retained.
        """
        sparse = {"id": self.SECRET, "slot_key": "chat-live-1", "active": True}
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-1",
            "message": "a wholly different instruction",
            "active": True,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [sparse]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert (
                svc._quarantined
            ), "a row with no no-default field was retired on a defaulted boolean"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_two_held_rows_matching_one_repair_are_both_kept(self, tmp_path) -> None:
        """An ambiguous repair match must retire NEITHER row, not both.

        ``_is_repair_of`` answered "some armed loop matches", so two held rows differing
        only in their unsafe ``id`` both matched the SAME repaired loop and both were
        retired -- deleting the only durable copy of the one that was never repaired.
        """
        first = {"id": self.SECRET, "slot_key": "chat-1", "message": "same", "idle_secs": 300}
        second = {
            "id": f"{self.SECRET}-other",
            "slot_key": "chat-1",
            "message": "same",
            "idle_secs": 300,
        }
        repaired = {"id": "repaired-1", "slot_key": "chat-1", "message": "same", "idle_secs": 300}
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [first, second]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [repaired]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            ids = [row.get("id") for row in svc._quarantined]
            assert (
                len(svc._quarantined) == 2
            ), f"an ambiguous match retired a row it could not have repaired: {ids!r}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_sparse_held_row_is_not_mistaken_for_a_repair(self, tmp_path) -> None:
        """Absence of contradicting fields must not read as evidence of a repair.

        The comparison iterated only the fields PRESENT in the held row, so a two-field
        hand-edited row -- an unsafe ``id`` plus a ``slot_key`` that happens to match a
        live entry -- had nothing left to contradict it and was retired, deleting its only
        durable copy. Sparse hand-edited rows are the expected input class here, so the
        match now needs positive evidence: a non-addressing field that actually agrees.
        """
        sparse = {"id": self.SECRET, "slot_key": "chat-live-1"}
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-1",
            "message": "a wholly different instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [sparse]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            keys = [row.get("slot_key") for row in svc._quarantined]
            assert "chat-live-1" in keys, (
                "a sparse row was retired as a repair of an unrelated loop sharing its "
                f"slot_key; still held: {svc._quarantined!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_post_commit_dir_sync_failure_does_not_report_a_rollback(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the rename is the commit point, so nothing past it may raise.

        ``fsync_dir`` sat INSIDE the try that follows the rename, so a directory-sync
        error propagated -- the caller rolled its in-memory loop back while DISK KEPT the
        change, and a restart resurrected a mutation this process reported as rejected.
        The compaction below already carried this exact reasoning in a comment.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()

            compacted: list[int] = []
            monkeypatch.setattr(
                _an, "fsync_dir", lambda _p: (_ for _ in ()).throw(OSError("no fsync"))
            )
            monkeypatch.setattr(
                type(svc),
                "_compact_quarantine_sidecar",
                lambda self: compacted.append(1),
            )

            payload = {"version": 1, "loops": [{"id": "committed-1", "idle_secs": 300}]}
            svc._write_state(payload)  # must NOT raise

            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert (
                on_disk["loops"][0]["id"] == "committed-1"
            ), "the rename committed but the write was reported as failed"
            assert compacted == [], (
                "compaction ran after an unsynced rename; it DELETES rows, so a crash "
                "could drop a repaired row from both files"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_two_unsafe_addressing_fields_do_not_retire_on_field_residue(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): both addressing fields unsafe exempted BOTH from matching.

        The docstring claimed a safe addressing field must match, but nothing enforced it:
        with ``id`` AND ``slot_key`` both credential-shaped -- producible by hand-edit --
        every addressing field is skipped, so a sparse held row matched an unrelated armed
        loop on ``idle_secs`` alone and its only durable copy was deleted.
        """
        held = {"id": self.SECRET, "slot_key": self.SECRET, "idle_secs": 300}
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-2",
            "message": "a different instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        keys = [row.get("slot_key") for row in svc._quarantined]
        assert (
            self.SECRET in keys
        ), f"an unrelated loop retired the held row on field residue alone: {keys!r}"

    @pytest.mark.asyncio
    async def test_a_sidecar_missing_its_key_refuses_rather_than_reading_empty(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a dict with no ``quarantined`` key read as no rows.

        ``raw.get`` answers None, ``_rows_or_empty`` turns that into ``[]``, and the loader
        then reports nothing held aside -- so the next persist unlinks a file whose shape
        this process never actually understood. Absent-key must refuse, unlike an explicit
        empty list, which is legitimately empty.
        """
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert svc._load_refused is True, (
            "a sidecar with no `quarantined` key was read as empty, so the next write "
            "would unlink it"
        )

    @pytest.mark.asyncio
    async def test_the_store_rename_is_durable_before_compaction_deletes_a_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the MAIN STORE's rename was never dir-synced.

        The sidecar's own rename is dir-synced, and compaction then DELETES a held row
        from it -- so the deletion was durable while the store write meant to carry the
        repaired row forward was not. A power-off in that window came back to the old
        store directory entry with the sidecar copy already gone: the row is lost from
        both files. Asserted on ORDER, since syncing after compaction closes no window.
        """
        store = self._store_with_one_unusable_row(tmp_path)
        events: list[str] = []
        real_fsync_dir = _an.fsync_dir
        real_replace = _an.replace_with_retry

        def _record_fsync(path, **kwargs):
            events.append(f"fsync_dir:{Path(path).name}")
            return real_fsync_dir(path, **kwargs)

        def _record_replace(src, dst):
            events.append(f"replace:{Path(dst).name}")
            return real_replace(src, dst)

        monkeypatch.setattr(_an, "fsync_dir", _record_fsync)
        monkeypatch.setattr(_an, "replace_with_retry", _record_replace)

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"

            compacted: list[str] = []
            real_compact = svc._compact_quarantine_sidecar

            def _record_compact():
                compacted.append("compact")
                events.append("compact")
                return real_compact()

            monkeypatch.setattr(svc, "_compact_quarantine_sidecar", _record_compact)
            svc._write_state(svc._serialize_state())

            assert compacted, "compaction never ran, so this run proves nothing"
            store_replace = events.index(f"replace:{store.name}")
            synced_after_store = [
                i for i, e in enumerate(events) if e.startswith("fsync_dir:") and i > store_replace
            ]
            assert synced_after_store, (
                "the store's parent directory was never synced after its rename, so the "
                f"store write is not durable when compaction deletes a row: {events!r}"
            )
            assert synced_after_store[0] < events.index("compact"), (
                "the store rename was synced only AFTER compaction, so a crash still "
                f"loses the repaired row from both files: {events!r}"
            )
        finally:
            svc.stop()


class TestIdCollidingHeldRowsStayQuarantined:
    """Two held rows on one ``id`` must not collapse under the last-wins insertion.

    ``_load`` applies held rows before store rows so the store wins a shared ``id``.
    That same last-wins insertion silently drops one of TWO held rows sharing an id,
    and compaction would then delete the loser as though the store carried it.
    """

    @staticmethod
    def _write(tmp_path, loops, quarantined) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": loops}),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"version": 1, "quarantined": quarantined}),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_the_losing_row_is_kept_rather_than_dropped(self, tmp_path) -> None:
        """The second held row on a shared id stays quarantined, not overwritten.

        ``_loops`` is keyed by id, so its SIZE is 1 either way -- the row that proves
        the fix is the loser surviving in quarantine instead of being forgotten.
        """
        first = {"id": "dup", "slot_key": "chat-1-1", "message": "first", "idle_secs": 300}
        second = {"id": "dup", "slot_key": "chat-2-2", "message": "second", "idle_secs": 300}
        self._write(tmp_path, [], [first, second])

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        held = [row.get("slot_key") for row in svc._quarantined]
        assert "chat-2-2" in held, "the id-colliding held row was dropped, not quarantined"

    @pytest.mark.asyncio
    async def test_a_committed_write_survives_a_failing_compaction(self, tmp_path) -> None:
        """A post-commit compaction error must not report a landed write as failed."""
        row = {"id": "held", "slot_key": "chat-9-9", "message": "keep", "idle_secs": 300}
        self._write(tmp_path, [], [row])
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        def _boom() -> None:
            raise OSError("sidecar compaction failed")

        svc._compact_quarantine_sidecar = _boom  # type: ignore[method-assign]

        # No raise: the main store is already committed, so rolling the caller back
        # would leave live state disagreeing with the file on disk.
        svc._write_state(svc._serialize_state())

        assert json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))


class TestSidecarRecoveryCannotAbortStartup:
    """Recovery from an unreadable sidecar must not end the process.

    ``_refuse_writes_and_preserve_sidecar`` arms the write refusal and then moves the
    unreadable sidecar aside. That move takes the sidecar lock, and a DIRECTORY standing
    where the lock file belongs makes ``open`` raise ``IsADirectoryError`` -- which
    propagated out of ``_load`` and ended the gateway during startup. The rename and the
    name reservation were already guarded; acquiring the lock was not.
    """

    def test_an_unopenable_lock_keeps_writes_refused_and_retains_the_bytes(self, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantine_path.write_text("{ not json", encoding="utf-8")
            lock = svc._quarantine_path.with_name(svc._quarantine_path.name + ".lock")
            lock.mkdir()
            assert lock.is_dir(), "precondition: the lock path must be unopenable"

            svc._refuse_writes_and_preserve_sidecar()

            assert svc._load_refused is True, (
                "the write refusal was lost, so a later persist would compact around rows "
                "nothing enumerated"
            )
            assert svc._quarantine_path.exists(), "the unreadable bytes were not retained"
            assert svc._quarantine_path.read_text(encoding="utf-8") == "{ not json"
            assert not list(
                tmp_path.glob("*.corrupt-*")
            ), "a move-aside was recorded that cannot have happened"
        finally:
            svc.stop()


class TestTheWriteRefusalIsReachableWithRowsInMemory:
    """``_write_quarantine_sidecar_locked`` latches the persist refusal from inside a
    write, so the next persist meets the guard with rows still held in memory."""

    @pytest.mark.asyncio
    async def test_the_refusal_is_reached_after_a_clean_load_armed_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._load_refused is False, "precondition: the store must load cleanly"
            await svc.add("chat-1-1785", "keep checking the pull request", idle_secs=300)
            assert svc._loops, "precondition: a loop must be armed, or the map is empty anyway"

            # Unreadable only AFTER a clean load: the window no load-time setter describes.
            monkeypatch.setattr(svc, "_quarantine_rows_on_disk", lambda: None)
            with pytest.raises(AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert svc._load_refused is True, "the mid-write refusal did not latch"
            assert (
                svc._loops
            ), "the map emptied, so this case would not differ from the load-time one"

            with pytest.raises(AutoNudgeStoreUnvetted) as caught:
                svc._write_state(svc._serialize_state())
            assert "could not vet the store" in str(caught.value), (
                "the second persist raised from the sidecar writer rather than from the "
                f"latched guard, so the guard never executed: {caught.value}"
            )
        finally:
            svc.stop()
