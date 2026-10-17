"""An archive that did not reach disk never lets a transcript rewrite drop its rows.

``_archive_lines`` is the precondition of every rewrite that removes rows: until
it is durable the transcript holds their only copy. These tests pin both halves:
the archive syncs everything that names it on every call, and each caller that
drops rows keeps them (or writes nothing) when the archive raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_transcript_file_change_bound import _loaded_slot, _plain

from kiro_crew import history as history_module
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
