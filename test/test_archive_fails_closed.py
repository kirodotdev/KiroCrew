"""An archive that did not reach disk never lets a transcript rewrite drop its rows.

``_archive_lines`` is the precondition of every rewrite that removes rows: until
it is durable the transcript holds their only copy. These tests pin both halves:
the archive syncs everything that names it on every call, and each caller that
drops rows keeps them (or writes nothing) when the archive raises.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_transcript_file_change_bound import _loaded_slot, _plain

from kiro_crew import channel_transcript_migration
from kiro_crew import history as history_module
from kiro_crew import history_rewrite
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.slot_persistence import transcript_merge
from kiro_crew.dashboard.slot_persistence.transcript_merge import (
    _frozen_prefix_and_foreign_appends,
)
from kiro_crew.history import ARCHIVE_DIR_NAME, ConversationLog


def _refuse(*_a: Any, **_k: Any) -> None:
    raise OSError(5, "device did not take the write")


class TestTheArchiveSyncsItsParentOnEveryCall:
    """A directory that exists was not necessarily synced into its parent."""

    def test_a_retry_after_a_failed_parent_sync_syncs_the_parent_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_fsync_dir = history_module.fsync_dir
        synced: list[str] = []
        fail_parent_once = [True]

        def recording_fsync_dir(directory: Any, **kwargs: Any) -> None:
            directory = Path(directory)
            if directory == tmp_path and fail_parent_once[0]:
                fail_parent_once[0] = False
                raise OSError(5, "device did not take the write")
            synced.append(directory.name)
            real_fsync_dir(directory, **kwargs)

        monkeypatch.setattr(history_module, "fsync_dir", recording_fsync_dir)

        # The first archive creates archive/ and its parent sync fails: the
        # caller sees a failed archive.
        with pytest.raises(OSError):
            history_module._archive_lines("k", ["a\n"], reason="compact", base=tmp_path)
        assert (tmp_path / ARCHIVE_DIR_NAME).is_dir()

        synced.clear()
        history_module._archive_lines("k", ["b\n"], reason="compact", base=tmp_path)
        # The directory now exists, and the parent entry naming it is synced anyway.
        assert synced == [ARCHIVE_DIR_NAME, tmp_path.name]

    def test_an_existing_directory_still_syncs_its_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ARCHIVE_DIR_NAME).mkdir()
        synced: list[str] = []
        real_fsync_dir = history_module.fsync_dir

        def recording_fsync_dir(directory: Any, **kwargs: Any) -> None:
            synced.append(Path(directory).name)
            real_fsync_dir(directory, **kwargs)

        monkeypatch.setattr(history_module, "fsync_dir", recording_fsync_dir)

        history_module._archive_lines("k", ["a\n"], reason="compact", base=tmp_path)
        assert synced == [ARCHIVE_DIR_NAME, tmp_path.name]

    def test_neither_directory_sync_is_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A best-effort sync logs an EIO and returns, which would report a lost archive as durable."""
        calls: list[dict[str, Any]] = []
        real_fsync_dir = history_module.fsync_dir

        def recording_fsync_dir(directory: Any, **kwargs: Any) -> None:
            calls.append(kwargs)
            real_fsync_dir(directory, **kwargs)

        monkeypatch.setattr(history_module, "fsync_dir", recording_fsync_dir)

        history_module._archive_lines("k", ["a\n"], reason="compact", base=tmp_path)

        assert calls == [{}, {}]


class TestTheArchiveFileIsFlushedBeforeItsSync:
    """An fsync of a descriptor whose buffered text was never written syncs an empty file."""

    def test_the_file_fsync_sees_every_byte_of_the_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sizes: list[int] = []

        class _RecordingOs:
            """``history``'s ``os``, recording the file size each fsync is asked to sync."""

            def __getattr__(self, name: str) -> Any:
                return getattr(os, name)

            @staticmethod
            def fsync(fd: int) -> None:
                sizes.append(os.fstat(fd).st_size)
                os.fsync(fd)

        monkeypatch.setattr(history_module, "os", _RecordingOs())

        archive = history_module._archive_lines(
            "k", ["a" * 64 + "\n"], reason="compact", base=tmp_path
        )

        assert archive is not None
        assert sizes == [archive.stat().st_size]


class TestAFailedArchiveLeavesNoFile:
    """The caller keeps the rows and retries, so the failed file is not their only copy."""

    @pytest.mark.parametrize("fails", ["file", "archive-dir", "parent-dir"])
    def test_every_retry_under_a_lasting_fault_leaves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: str
    ) -> None:
        archive_dir = tmp_path / ARCHIVE_DIR_NAME
        real_fsync_dir = history_module.fsync_dir

        class _FailingOs:
            def __getattr__(self, name: str) -> Any:
                return getattr(os, name)

            @staticmethod
            def fsync(fd: int) -> None:
                if fails == "file":
                    raise OSError(5, "device did not take the write")
                os.fsync(fd)

        def failing_fsync_dir(directory: Any, **kwargs: Any) -> None:
            name = Path(directory).name
            if (fails, name) in {("archive-dir", ARCHIVE_DIR_NAME), ("parent-dir", tmp_path.name)}:
                raise OSError(5, "device did not take the write")
            real_fsync_dir(directory, **kwargs)

        monkeypatch.setattr(history_module, "os", _FailingOs())
        monkeypatch.setattr(history_module, "fsync_dir", failing_fsync_dir)

        for _ in range(3):
            with pytest.raises(OSError):
                history_module._archive_lines("k", ["a\n"], reason="compact", base=tmp_path)

        assert list(archive_dir.iterdir()) == []

    @pytest.mark.parametrize(
        "interruption", [KeyboardInterrupt, asyncio.CancelledError, SystemExit]
    )
    def test_an_interruption_inside_the_write_leaves_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        interruption: type[BaseException],
    ) -> None:
        """Not only ``Exception``: a cancel or Ctrl-C mid-sync removes the file too."""

        class _InterruptedOs:
            def __getattr__(self, name: str) -> Any:
                return getattr(os, name)

            @staticmethod
            def fsync(_fd: int) -> None:
                raise interruption()

        monkeypatch.setattr(history_module, "os", _InterruptedOs())

        with pytest.raises(interruption):
            history_module._archive_lines("k", ["a\n"], reason="compact", base=tmp_path)

        assert list((tmp_path / ARCHIVE_DIR_NAME).iterdir()) == []

    def test_a_removal_that_fails_still_raises_the_sync_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def refuse_sync(*_a: Any, **_k: Any) -> None:
            raise OSError(5, "device did not take the write")

        class _NoUnlinkOs:
            def __getattr__(self, name: str) -> Any:
                return getattr(os, name)

            @staticmethod
            def unlink(_path: Any) -> None:
                raise PermissionError(13, "unlink refused")

        monkeypatch.setattr(history_module, "fsync_dir", refuse_sync)
        monkeypatch.setattr(history_module, "os", _NoUnlinkOs())

        with pytest.raises(OSError) as raised:
            history_module._archive_lines("k", ["a\n"], reason="compact", base=tmp_path)

        assert raised.value.errno == 5
        assert "Could not remove the unsynced archive" in caplog.text


class TestTheTranscriptReplaceIsSynced:
    """Every rewrite that drops archived rows writes its replacement with ``fsync=True``.

    The archive holds the dropped rows; the kept rows live only in the replacement,
    so an unsynced rename that comes back empty after a power loss would lose them.
    """

    @staticmethod
    def _record(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        real = module.atomic_write

        def recording_atomic_write(*args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, "atomic_write", recording_atomic_write)
        return calls

    def test_compaction(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "a")
        log.append("k", "user", "b")
        calls = self._record(monkeypatch, history_rewrite)

        log.rewrite_session("k", log._read_messages("k")[:1])

        assert [c.get("fsync") for c in calls] == [True]

    def test_rotation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(history_module, "_SESSION_MAX_BYTES", 400)
        monkeypatch.setattr(history_module, "_SESSION_KEEP_LINES", 3)
        calls = self._record(monkeypatch, history_rewrite)
        log = ConversationLog(base_dir=tmp_path)

        for i in range(12):
            log.append("k", "user", f"message number {i:02d} with enough text to rotate")

        assert list((tmp_path / ARCHIVE_DIR_NAME).glob("*.jsonl"))
        assert calls
        assert all(c.get("fsync") is True for c in calls)

    def test_the_dashboard_save(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The one write the dashboard rewrite, file-change bound and foreign-dedup all reach."""
        rows = [_plain(i) for i in range(4)]
        state, slot, _path = _loaded_slot(tmp_path, monkeypatch, rows, window=4)
        del slot.messages[2:]
        slot._pending_rewrite = True
        calls = self._record(monkeypatch, chat_persistence)

        assert _save_slot_to_history(state, slot, rewrite=True) is True

        assert list((tmp_path / ARCHIVE_DIR_NAME).glob("*.jsonl"))
        assert [c.get("fsync") for c in calls] == [True]


class TestCompactionFailsClosed:
    """``rewrite_session`` drops rows only once their archive landed."""

    def test_an_archive_failure_raises_and_leaves_the_transcript_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "a")
        log.append("k", "user", "b")
        before = log._path("k").read_bytes()
        keep = log._read_messages("k")[:1]
        monkeypatch.setattr(history_module, "_archive_lines", _refuse)

        with pytest.raises(OSError):
            log.rewrite_session("k", keep)

        assert log._path("k").read_bytes() == before
        assert [m["content"] for m in ConversationLog(base_dir=tmp_path)._read_messages("k")] == [
            "a",
            "b",
        ]

    def test_a_landed_archive_compacts(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "a")
        log.append("k", "user", "b")
        log.rewrite_session("k", log._read_messages("k")[:1])
        assert [m["content"] for m in ConversationLog(base_dir=tmp_path)._read_messages("k")] == [
            "a"
        ]
        assert list((tmp_path / ARCHIVE_DIR_NAME).glob("*.jsonl"))


class TestTheDashboardRewriteFailsClosed:
    """A rewind/regenerate save writes nothing when its dropped tail cannot be archived."""

    def test_an_archive_failure_raises_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [_plain(i) for i in range(4)]
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=4)
        del slot.messages[2:]  # the rewind's truncated window
        slot._pending_rewrite = True
        before = path.read_bytes()
        monkeypatch.setattr(transcript_merge, "_archive_lines", _refuse)

        with pytest.raises(OSError):
            _save_slot_to_history(state, slot, rewrite=True)

        assert path.read_bytes() == before
        assert slot._pending_rewrite is True

    def test_a_landed_archive_rewrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [_plain(i) for i in range(4)]
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=4)
        del slot.messages[2:]
        slot._pending_rewrite = True

        assert _save_slot_to_history(state, slot, rewrite=True) is True

        assert len(path.read_text(encoding="utf-8").splitlines()) == 3
        assert slot._pending_rewrite is False
        assert list((tmp_path / ARCHIVE_DIR_NAME).glob("*.jsonl"))


def _dedup_fixture(tmp_path: Path) -> tuple[Any, Path, list[dict]]:
    """A transcript whose window region holds a fresh-``ts`` copy between two foreign rows."""
    path = tmp_path / "k.jsonl"
    disk = [
        {"_type": "metadata", "created": "2026-01-01T00:00:00Z"},
        {"role": "assistant", "content": "foreign-before", "ts": "T1"},
        # An append_if_absent copy of the window's "aif-copy", persisted with a fresh ts.
        {"role": "assistant", "content": "aif-copy", "ts": "TY"},
        {"role": "assistant", "content": "foreign-after", "ts": "T3"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in disk), encoding="utf-8")
    slot = SimpleNamespace(key="k", _frozen_prefix_cache=None, _foreign_reported=frozenset())
    window = [{"role": "assistant", "content": "aif-copy", "ts": "TZ"}]
    return slot, path, window


class TestForeignDedupFailsClosed:
    """A dedup drop leaves the payload only once its archive landed."""

    def test_an_archive_failure_keeps_the_line_in_disk_order(self, tmp_path: Path) -> None:
        slot, path, window = _dedup_fixture(tmp_path)

        _prefix, foreign, dropped = _frozen_prefix_and_foreign_appends(
            slot, path, 0, window, archive_dedup=_refuse
        )

        assert [json.loads(ln)["content"] for ln in foreign] == [
            "foreign-before",
            "aif-copy",
            "foreign-after",
        ]
        assert dropped == []
        # The cached fast path re-emits the kept line, so a failed replace
        # followed by a cache hit cannot drop it either.
        assert [json.loads(ln)["content"] for ln in slot._frozen_prefix_cache[4]] == [
            "foreign-before",
            "aif-copy",
            "foreign-after",
        ]

    def test_a_landed_archive_drops_the_line(self, tmp_path: Path) -> None:
        slot, path, window = _dedup_fixture(tmp_path)
        archived: list[list[str]] = []

        _prefix, foreign, dropped = _frozen_prefix_and_foreign_appends(
            slot, path, 0, window, archive_dedup=archived.append
        )

        assert [json.loads(ln)["content"] for ln in foreign] == [
            "foreign-before",
            "foreign-after",
        ]
        assert [json.loads(ln)["content"] for ln in dropped] == ["aif-copy"]
        assert [[json.loads(ln)["content"] for ln in batch] for batch in archived] == [["aif-copy"]]

    def test_the_save_archives_dedup_drops_through_the_same_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``compose_payload`` hands the scan its archive, so a failure keeps the row on disk."""
        rows = [_plain(i) for i in range(2)]
        state, slot, path = _loaded_slot(tmp_path, monkeypatch, rows, window=2)
        # A window row not yet saved, and an id-less copy of it another writer
        # persisted with a fresh ts: the scan folds that copy as a dedup drop.
        slot.messages.append(
            {"role": "user", "content": "new ask", "ts": "2099-01-01T00:00:00.000000"}
        )
        copy = {"role": "user", "content": "new ask", "ts": "2099-01-01T00:00:01.000000"}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(copy) + "\n")
        slot._frozen_prefix_cache = None
        attempts: list[str] = []

        def refuse(key: str, lines: list[str], *, reason: str, base: Any = None) -> None:
            attempts.append(reason)
            _refuse()

        monkeypatch.setattr(transcript_merge, "_archive_lines", refuse)

        assert _save_slot_to_history(state, slot) is True

        assert attempts == ["foreign-dedup"]
        on_disk = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()[1:]]
        assert [r["ts"] for r in on_disk if r.get("content") == "new ask"] == [
            "2099-01-01T00:00:00.000000",
            "2099-01-01T00:00:01.000000",
        ]


def _record_transcript_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, dict[str, Any]]]:
    """Record every write that reaches ``rewrite_transcript``'s ``atomic_write``."""
    calls: list[tuple[Path, dict[str, Any]]] = []
    real = history_rewrite.atomic_write

    def recording_atomic_write(path: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((Path(path), kwargs))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(history_rewrite, "atomic_write", recording_atomic_write)
    return calls


class TestEveryWholeTranscriptReplaceIsSynced:
    """A metadata-only rewrite still renames the whole conversation over the old file.

    Only line 0 changes, but the replacement carries every row, so an unsynced
    file that comes back empty after a power loss loses the conversation, not
    just the marker. Each site goes through ``rewrite_transcript``. The three hot
    ones (``mark_consolidated``, the consolidation sub-offset and
    ``update_metadata``) sync before taking the session lock instead; they are
    pinned by ``test_history_locking_remediation.py::TestReducedLockHold``.
    """

    @staticmethod
    def _log(tmp_path: Path) -> ConversationLog:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "a")
        log.append("k", "assistant", "b")
        return log

    def _assert_one_synced_write(
        self, calls: list[tuple[Path, dict[str, Any]]], log: ConversationLog
    ) -> None:
        assert [(path, kwargs.get("fsync")) for path, kwargs in calls] == [(log._path("k"), True)]
        # The rows are still there: the write replaced the file, it did not trim it.
        assert [m["content"] for m in ConversationLog(base_dir=log._dir)._read_messages("k")] == [
            "a",
            "b",
        ]

    def test_clear_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = self._log(tmp_path)
        log.update_metadata("k", {"closed": True, "closed_at": 1.0})
        calls = _record_transcript_writes(monkeypatch)

        log.clear_closed("k")

        self._assert_one_synced_write(calls, log)
        assert "closed" not in log.get_metadata("k")

    def test_the_agent_selection_rollback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import execution_context
        from kiro_crew.session_agent_selection import SelectionChange, restore_agent_selection

        published = {"store": {"store_id": "default"}}
        monkeypatch.setattr(execution_context, "restore_live_session_execution", lambda *_a: False)
        monkeypatch.setattr(
            ConversationLog, "__init__", _bound_init(ConversationLog.__init__, tmp_path)
        )
        log = self._log(tmp_path)
        log.update_metadata("k", {execution_context.EXECUTION_CONTEXT_KEY: published})
        calls = _record_transcript_writes(monkeypatch)

        restore_agent_selection("k", SelectionChange(None, published, None))

        self._assert_one_synced_write(calls, log)
        assert execution_context.EXECUTION_CONTEXT_KEY not in log.get_metadata("k")

    def test_the_helper_has_no_way_to_skip_the_sync(self) -> None:
        import inspect

        assert list(inspect.signature(history_rewrite.rewrite_transcript).parameters) == [
            "path",
            "content",
        ]
        # The staged half the hot metadata writes use has no switch either.
        assert list(inspect.signature(history_rewrite.stage_replacement).parameters) == [
            "path",
            "content",
        ]

    @pytest.mark.parametrize(
        "module",
        [
            "history.py",
            "history_projection.py",
            "history_rewrite.py",
            "session_agent_selection.py",
            "channel_transcript_migration.py",
        ],
    )
    def test_no_transcript_module_opts_out_of_the_sync(self, module: str) -> None:
        source = (Path(history_module.__file__).parent / module).read_text(encoding="utf-8")
        assert "fsync=False" not in source


def _bound_init(real_init: Any, base_dir: Path) -> Any:
    """``ConversationLog()`` with no arguments opens *base_dir*, as the rollback does."""

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("base_dir", base_dir)
        real_init(self, *args, **kwargs)

    return init


class TestTheChannelMigrationSyncsBeforeItDeletesTheOrphan:
    """The orphan is the only other copy of its rows, and ``delete_session`` is permanent."""

    @staticmethod
    def _two_files(tmp_path: Path) -> tuple[ConversationLog, Path, Path]:
        channel_stem = "slack_1785370133.085469"
        log = ConversationLog(base_dir=tmp_path)
        channel = tmp_path / f"{channel_stem}.jsonl"
        orphan = tmp_path / f"dashboard_{channel_stem}.jsonl"
        for path, content, ts in (
            (channel, "channel", "2026-08-01T10:00:00"),
            (orphan, "tab", "2026-08-01T11:00:00"),
        ):
            path.write_text(
                json.dumps({"_type": "metadata"})
                + "\n"
                + json.dumps({"role": "user", "content": content, "ts": ts})
                + "\n",
                encoding="utf-8",
            )
        return log, channel, orphan

    def test_the_merged_file_and_its_directory_are_synced_before_the_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log, channel, orphan = self._two_files(tmp_path)
        order: list[str] = []
        real_write = history_rewrite.atomic_write
        real_fsync_dir = channel_transcript_migration.fsync_dir
        real_delete = ConversationLog.delete_session

        def recording_write(path: Any, *args: Any, **kwargs: Any) -> Any:
            order.append(f"write:{Path(path).name}:fsync={kwargs.get('fsync')}")
            return real_write(path, *args, **kwargs)

        def recording_fsync_dir(directory: Any, **kwargs: Any) -> None:
            order.append(f"fsync_dir:{Path(directory).name}:{kwargs}")
            real_fsync_dir(directory, **kwargs)

        def recording_delete(self: Any, key: str) -> bool:
            order.append(f"delete:{key}")
            return real_delete(self, key)

        monkeypatch.setattr(history_rewrite, "atomic_write", recording_write)
        monkeypatch.setattr(channel_transcript_migration, "fsync_dir", recording_fsync_dir)
        monkeypatch.setattr(ConversationLog, "delete_session", recording_delete)

        assert channel_transcript_migration.migrate_channel_transcripts(log) == 1

        assert order == [
            f"write:{channel.name}:fsync=True",
            f"fsync_dir:{tmp_path.name}:{{}}",
            f"delete:{orphan.stem}",
        ]
        assert not orphan.exists()

    def test_a_failed_directory_sync_keeps_the_orphan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log, _channel, orphan = self._two_files(tmp_path)
        orphan_before = orphan.read_bytes()
        monkeypatch.setattr(channel_transcript_migration, "fsync_dir", _refuse)

        assert channel_transcript_migration.migrate_channel_transcripts(log) == 0

        assert orphan.read_bytes() == orphan_before

    def test_an_unchanged_retry_after_a_failed_directory_sync_keeps_the_orphan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retry's merge is byte-identical, so it skips the write, but still syncs.

        The first pass renamed the merge into place and its directory sync failed.
        A same-boot removing pass then finds nothing to write; deleting the orphan
        there without the sync would leave a power loss free to bring back the old
        channel transcript with the orphan gone.
        """
        log, channel, orphan = self._two_files(tmp_path)
        orphan_before = orphan.read_bytes()
        real_fsync_dir = channel_transcript_migration.fsync_dir
        writes: list[str] = []
        real_write = history_rewrite.atomic_write

        def recording_write(path: Any, *args: Any, **kwargs: Any) -> Any:
            writes.append(Path(path).name)
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(history_rewrite, "atomic_write", recording_write)
        monkeypatch.setattr(channel_transcript_migration, "fsync_dir", _refuse)

        assert channel_transcript_migration.migrate_channel_transcripts(log) == 0
        assert writes == [channel.name], "the first pass should have written the merge"
        merged = channel.read_bytes()

        assert channel_transcript_migration.migrate_channel_transcripts(log) == 0

        assert writes == [channel.name], "the retry's merge should have been unchanged"
        assert channel.read_bytes() == merged
        assert orphan.read_bytes() == orphan_before

        monkeypatch.setattr(channel_transcript_migration, "fsync_dir", real_fsync_dir)
        assert channel_transcript_migration.migrate_channel_transcripts(log) == 1
        assert not orphan.exists()
