"""The sources.sync_status COLUMN is the single source of truth.

A source's sync state used to live in two places: the ``sources.sync_status``
column and a ``sync_status`` key inside the ``properties`` JSON blob. Writers
were split across the two -- most transitions wrote the column only, while the
watcher's 'missing' marker went into the blob only -- and readers were split the
same way, so each side saw a state the other had not written.

These tests pin the converged contract on the watcher paths that read and write
it: the pre-scan skip reads the column, and the missing marker is written to the
column and can be left again.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.watcher import KnowledgeWatcher


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


def _watcher(store) -> KnowledgeWatcher:
    pipeline = MagicMock()
    # No embedder configured -> _scan skips the self-heal re-embed branch.
    pipeline.embedder = None
    watcher = KnowledgeWatcher(store=store, pipeline=pipeline)
    # Discovery registers workspace folders from live config; irrelevant here
    # and it would put rows in the table the assertions do not expect.
    watcher._maybe_reembed_stale = AsyncMock()  # type: ignore[method-assign]
    return watcher


def _status(store, sid: str) -> str:
    return store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()[
        "sync_status"
    ]


def _props(store, sid: str) -> dict:
    raw = store.db.execute("SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()[
        "properties"
    ]
    return json.loads(raw or "{}")


def _hash(path) -> str:
    """The digest the watcher itself would compute for this file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestFolderPreScanSkip:
    @pytest.mark.asyncio
    async def test_a_paused_folder_is_not_walked(self, store, tmp_path):
        """A pause recorded in the column stops the sweep.

        The skip used to read the properties copy, so a pause the column knew
        about still walked and delete-reconciled the whole folder every sweep.
        """
        folder = tmp_path / "vault"
        folder.mkdir()
        sid = store.add_source("vault", "local_folder", str(folder))
        store.db.execute("UPDATE sources SET sync_status = 'paused' WHERE id = ?", (sid,))
        store.db.commit()
        assert "sync_status" not in _props(store, sid), "the JSON copy must not exist"

        watcher = _watcher(store)
        scan = AsyncMock(return_value={})
        watcher._folder_watcher.scan_source = scan  # type: ignore[method-assign]
        await watcher._scan()

        scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unconfirmed_folder_is_not_walked(self, store, tmp_path):
        folder = tmp_path / "vault"
        folder.mkdir()
        sid = store.add_source(
            "vault", "local_folder", str(folder), properties={"sync_status": "pending_confirmation"}
        )
        assert _status(store, sid) == "pending_confirmation"

        watcher = _watcher(store)
        scan = AsyncMock(return_value={})
        watcher._folder_watcher.scan_source = scan  # type: ignore[method-assign]
        await watcher._scan()

        scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_active_folder_is_still_walked(self, store, tmp_path):
        """The guard rejects only the two states, so 'active' still scans."""
        folder = tmp_path / "vault"
        folder.mkdir()
        store.add_source("vault", "local_folder", str(folder), properties={"sync_status": "active"})

        watcher = _watcher(store)
        scan = AsyncMock(return_value={})
        watcher._folder_watcher.scan_source = scan  # type: ignore[method-assign]
        await watcher._scan()

        scan.assert_called_once()


class TestSingleFileMissingMarker:
    @pytest.mark.asyncio
    async def test_a_vanished_file_marks_the_column_missing(self, store, tmp_path):
        """The Library renders the column, so that is where 'missing' belongs.

        Marking it in the properties JSON instead left the visible state stale:
        a file that was gone went on reading 'synced'.
        """
        gone = tmp_path / "gone.md"
        gone.write_text("# gone")
        sid = store.add_source("gone.md", "local_file", str(gone))
        store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
        store.db.commit()
        gone.unlink()

        await _watcher(store)._scan()

        assert _status(store, sid) == "missing"
        assert "sync_status" not in _props(store, sid), "no second copy is written"

    @pytest.mark.asyncio
    async def test_a_returning_file_leaves_missing(self, store, tmp_path):
        """'missing' must be a state a source can leave.

        An unchanged file is not re-ingested, so nothing else moves the column
        back and the source would read missing for as long as it existed.
        """
        back = tmp_path / "back.md"
        back.write_text("# back")
        # A stored mtime in the future keeps the re-ingest out of it: the file's
        # content hash matches, so the returning-file read finds it unchanged.
        sid = store.add_source(
            "back.md",
            "local_file",
            str(back),
            properties={"mtime": 1 << 40, "content_hash": _hash(back)},
        )
        store.db.execute("UPDATE sources SET sync_status = 'missing' WHERE id = ?", (sid,))
        store.db.commit()

        await _watcher(store)._scan()

        assert _status(store, sid) == "synced"

    @pytest.mark.asyncio
    async def test_a_restore_under_an_unadvanced_mtime_is_not_called_synced(self, store, tmp_path):
        """A returning file is read, not assumed, whatever its mtime says.

        A restore that preserves the archived mtime (cp -p, rsync -t, tar -x) can
        put DIFFERENT content on disk under an mtime that never advanced. Trusting
        the mtime gate there would stamp 'synced' over content the store does not
        hold, and the stale copy would keep answering searches.
        """
        back = tmp_path / "restored.md"
        back.write_text("# restored from an older backup")
        sid = store.add_source(
            "restored.md",
            "local_file",
            str(back),
            # mtime far ahead of the file's, so `mtime > stored_mtime` is False.
            properties={"mtime": 1 << 40, "content_hash": _hash(back) + "-before-the-restore"},
        )
        store.db.execute("UPDATE sources SET sync_status = 'missing' WHERE id = ?", (sid,))
        store.db.commit()

        watcher = _watcher(store)
        # Raising is what proves the claim is withheld: the file was read, the
        # read failed, and nothing was stamped 'synced' on its behalf.
        watcher.pipeline.ingest_file = AsyncMock(side_effect=RuntimeError("read failed"))
        await watcher._scan()

        watcher.pipeline.ingest_file.assert_awaited_once()
        assert _status(store, sid) == "missing"

    @pytest.mark.asyncio
    async def test_a_present_file_keeps_its_status(self, store, tmp_path):
        """The recovery write fires only for a row that reads 'missing'."""
        here = tmp_path / "here.md"
        here.write_text("# here")
        sid = store.add_source(
            "here.md", "local_file", str(here), properties={"mtime": 1 << 40, "content_hash": "abc"}
        )
        store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (sid,))
        store.db.commit()

        await _watcher(store)._scan()

        assert _status(store, sid) == "error"

    @pytest.mark.asyncio
    async def test_recovery_does_not_overwrite_a_status_that_moved(self, store, tmp_path):
        """The recovery write loses to a transition that landed mid-sweep.

        'missing' comes from the snapshot taken at the top of the sweep. A manual
        sync that fails while the sweep runs writes 'error', and stamping
        'synced' over it would report content the store does not have.
        """
        back = tmp_path / "raced.md"
        back.write_text("# raced")
        sid = store.add_source(
            "raced.md",
            "local_file",
            str(back),
            # Hash matching the file: the returning-file read finds it unchanged,
            # so the recovery write is what runs and what the CAS has to lose.
            properties={"mtime": 1 << 40, "content_hash": _hash(back)},
        )
        store.db.execute("UPDATE sources SET sync_status = 'missing' WHERE id = ?", (sid,))
        store.db.commit()

        real_update = store.update_source

        def a_manual_sync_fails_first(source_id, **fields):
            if fields.get("sync_status") == "synced":
                store.db.execute(
                    "UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,)
                )
                store.db.commit()
            return real_update(source_id, **fields)

        store.update_source = a_manual_sync_fails_first  # type: ignore[method-assign]
        await _watcher(store)._scan()

        assert _status(store, sid) == "error"

    @pytest.mark.asyncio
    async def test_a_failed_reingest_is_not_recorded_as_synced(self, store, tmp_path):
        """'synced' is a claim about content, so it waits for the re-ingest.

        A returning file whose content CHANGED is re-ingested, and the pipeline
        writes the column itself. Clearing 'missing' to 'synced' before that read
        would leave the claim standing when the read fails -- the source would
        report holding content it never ingested.
        """
        back = tmp_path / "changed.md"
        back.write_text("# changed")
        sid = store.add_source(
            "changed.md", "local_file", str(back), properties={"mtime": 1, "content_hash": "stale"}
        )
        store.db.execute("UPDATE sources SET sync_status = 'missing' WHERE id = ?", (sid,))
        store.db.commit()

        watcher = _watcher(store)
        watcher.pipeline.ingest_file = AsyncMock(side_effect=RuntimeError("read failed"))
        await watcher._scan()

        watcher.pipeline.ingest_file.assert_awaited_once()
        assert _status(store, sid) == "missing"

    @pytest.mark.asyncio
    async def test_an_ingest_that_writes_no_status_still_clears_missing(self, store, tmp_path):
        """The marker comes off a file the duplicate gate accounted for.

        A returning file whose content changed to something already in the Library
        is refused by the pre-ingest duplicate gate, which writes no status at all
        -- it records a terminal job and deletes this source's superseded items.
        Nothing else clears the marker, so a present, accounted-for file would go
        on reading 'missing'.
        """
        back = tmp_path / "dupe.md"
        back.write_text("# already in the library under another source")
        sid = store.add_source(
            "dupe.md",
            "local_file",
            str(back),
            properties={"mtime": 1, "content_hash": "stale"},
        )
        store.db.execute("UPDATE sources SET sync_status = 'missing' WHERE id = ?", (sid,))
        store.db.commit()

        watcher = _watcher(store)
        # The duplicate gate's shape: returns a terminal job id, writes no status.
        watcher.pipeline.ingest_file = AsyncMock(return_value="dupe-job-id")
        await watcher._scan()

        watcher.pipeline.ingest_file.assert_awaited_once()
        assert _status(store, sid) == "synced"
        # The restored file's own mtime and hash are recorded, so a later edit is
        # detected even when the restore LOWERED the mtime this row had stored.
        assert _props(store, sid)["content_hash"] == _hash(back)

    @pytest.mark.asyncio
    async def test_the_clear_loses_to_a_status_the_ingest_wrote(self, store, tmp_path):
        """An ingestion that DID write the column keeps its own verdict.

        The clear runs after every non-raising ingest, so the CAS on 'missing' is
        the only thing keeping it off a row the pipeline just marked 'error' for a
        partial write.
        """
        back = tmp_path / "partial.md"
        back.write_text("# partially written")
        sid = store.add_source(
            "partial.md",
            "local_file",
            str(back),
            properties={"mtime": 1, "content_hash": "stale"},
        )
        store.db.execute("UPDATE sources SET sync_status = 'missing' WHERE id = ?", (sid,))
        store.db.commit()

        watcher = _watcher(store)

        async def a_partial_write(*_args, **_kwargs):
            store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (sid,))
            store.db.commit()
            return "job-id"

        watcher.pipeline.ingest_file = AsyncMock(side_effect=a_partial_write)
        await watcher._scan()

        assert _status(store, sid) == "error"
