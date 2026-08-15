"""``PinnedFilesService`` paths the seam tests never drive.

``test_mochi_seams.py`` exercises the change-poll wiring and ``test_mochi_routes.py``
the HTTP surface, so what is left uncovered is the whole startup path (``load``)
and every failure/guard branch around it: a corrupt or wrongly-shaped file being
backed up, an unreadable file, the add/remove guard clauses, a failed atomic write
rolling the in-memory list back, and the re-watch retry that makes
write-temp-then-rename saves survive.

They matter because each one is a place the service could silently diverge from
disk — a pin that returns success and then reappears on restart, or a watcher
started for a path that does not exist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import pinned_files_service as pfs
from kiro_crew.apps.builtins.mochi.pinned_files_service import (
    DATA_FILE_NAME,
    DEBOUNCE_MS,
    MAX_PINS,
    REWATCH_RETRY_MS,
    PinnedFilesService,
    _file_exists,
)

NOW = 1_700_000_000_000


def _svc(tmp_path: Path) -> tuple[PinnedFilesService, list[tuple[str, Any]]]:
    events: list[tuple[str, Any]] = []
    svc = PinnedFilesService(str(tmp_path), lambda channel, *args: events.append((channel, args)))
    return svc, events


def _write_pins(tmp_path: Path, pins: list[Any]) -> Path:
    p = tmp_path / DATA_FILE_NAME
    p.write_text(json.dumps({"version": 1, "pins": pins}), encoding="utf-8")
    return p


class TestFileExists:
    def test_a_non_string_or_empty_path_never_exists(self):
        assert _file_exists(None) is False
        assert _file_exists(17) is False
        assert _file_exists("") is False

    def test_an_os_error_from_the_stat_is_read_as_absent(self, monkeypatch):
        def _boom(_path):
            raise OSError("ELOOP")

        monkeypatch.setattr(pfs.os.path, "exists", _boom)
        assert _file_exists("/some/where") is False


class TestLoad:
    def test_a_valid_file_is_loaded_and_existing_paths_are_watched(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("x", encoding="utf-8")
        gone = tmp_path / "gone.txt"
        _write_pins(
            tmp_path,
            [
                {"path": str(real), "label": "real"},
                {"path": str(gone), "label": "absent"},
                {"path": str(real), "label": "dupe"},
                # Garbage entries round-trip unvalidated and start no watcher.
                42,
                {"nopath": True},
            ],
        )
        svc, _ = _svc(tmp_path)
        svc.load(NOW)

        assert len(svc.get_pins()) == 5
        # Only the path that exists on disk, and only once.
        assert svc.get_watched_paths() == {str(real)}

    def test_an_absent_file_loads_an_empty_list_without_a_backup(self, tmp_path):
        svc, _ = _svc(tmp_path)
        svc.load(NOW)
        assert svc.get_pins() == []
        assert list(tmp_path.glob("*.bak.*")) == []

    def test_an_unreadable_file_resets_without_backing_anything_up(self, tmp_path):
        # A directory where the JSON should be: read_text raises IsADirectoryError,
        # which is an OSError but not FileNotFoundError.
        (tmp_path / DATA_FILE_NAME).mkdir()
        svc, _ = _svc(tmp_path)
        svc.load(NOW)
        assert svc.get_pins() == []
        assert list(tmp_path.glob("*.bak.*")) == []

    def test_corrupt_json_is_backed_up_under_the_supplied_clock(self, tmp_path):
        (tmp_path / DATA_FILE_NAME).write_text("{not json", encoding="utf-8")
        svc, _ = _svc(tmp_path)
        svc.load(NOW)
        assert svc.get_pins() == []
        assert (tmp_path / f"{DATA_FILE_NAME}.bak.{NOW}").read_text(encoding="utf-8") == "{not json"
        assert not (tmp_path / DATA_FILE_NAME).exists()

    @pytest.mark.parametrize("payload", ["[]", '{"pins": "nope"}', '"a string"'])
    def test_a_wrongly_shaped_payload_is_backed_up_too(self, tmp_path, payload):
        (tmp_path / DATA_FILE_NAME).write_text(payload, encoding="utf-8")
        svc, _ = _svc(tmp_path)
        svc.load(NOW)
        assert svc.get_pins() == []
        assert (tmp_path / f"{DATA_FILE_NAME}.bak.{NOW}").exists()

    def test_reloading_clears_watchers_from_the_previous_load(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("x", encoding="utf-8")
        _write_pins(tmp_path, [{"path": str(real)}])
        svc, _ = _svc(tmp_path)
        svc.load(NOW)
        assert svc.get_watched_paths() == {str(real)}

        _write_pins(tmp_path, [])
        svc.load(NOW)
        assert svc.get_watched_paths() == set()

    def test_backing_up_a_missing_file_is_a_no_op_not_a_crash(self, tmp_path):
        svc, _ = _svc(tmp_path)
        svc._backup_corrupted(NOW)  # os.rename raises FileNotFoundError; swallowed
        assert list(tmp_path.glob("*.bak.*")) == []


class TestReloadFromDisk:
    def test_an_unreadable_file_leaves_the_in_memory_list_untouched(self, tmp_path):
        svc, _ = _svc(tmp_path)
        svc._pins = [{"path": "/kept"}]
        (tmp_path / DATA_FILE_NAME).mkdir()
        svc._reload_pins_from_disk()
        assert svc.get_pins() == [{"path": "/kept"}]


class TestAddPinGuards:
    def test_a_relative_path_is_refused(self, tmp_path):
        svc, events = _svc(tmp_path)
        assert svc.add_pin("relative/file.txt", now_ms=NOW) is False
        assert events == []

    def test_a_full_list_is_refused(self, tmp_path):
        _write_pins(tmp_path, [{"path": f"/x/{i}"} for i in range(MAX_PINS)])
        svc, events = _svc(tmp_path)
        target = tmp_path / "new.txt"
        target.write_text("x", encoding="utf-8")
        assert svc.add_pin(str(target), now_ms=NOW) is False
        assert events == []

    def test_a_duplicate_is_refused(self, tmp_path):
        target = tmp_path / "dupe.txt"
        target.write_text("x", encoding="utf-8")
        _write_pins(tmp_path, [{"path": str(target)}])
        svc, events = _svc(tmp_path)
        assert svc.add_pin(str(target), now_ms=NOW) is False
        assert events == []

    def test_an_empty_label_falls_back_to_the_basename(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        svc, events = _svc(tmp_path)
        assert svc.add_pin(str(target), "", now_ms=NOW) is True
        assert svc.get_pins()[0]["label"] == "notes.txt"
        assert "updatedAt" not in svc.get_pins()[0]
        assert events[0][0] == "pinned:files-changed"

    def test_a_failed_write_rolls_the_append_back(self, tmp_path, monkeypatch):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        svc, events = _svc(tmp_path)

        def _boom(*_a, **_k):
            raise OSError("ENOSPC")

        monkeypatch.setattr(pfs, "atomic_write", _boom)
        assert svc.add_pin(str(target), now_ms=NOW) is False
        assert svc.get_pins() == []
        assert events == []
        assert svc.get_watched_paths() == set()


class TestRemoveAndMarkSeen:
    def test_removing_an_unpinned_path_reports_false(self, tmp_path):
        svc, events = _svc(tmp_path)
        assert svc.remove_pin("/never/pinned") is False
        assert events == []

    def test_a_failed_write_restores_the_removed_pin(self, tmp_path, monkeypatch):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        _write_pins(tmp_path, [{"path": str(target), "label": "notes.txt"}])
        svc, events = _svc(tmp_path)
        svc.load(NOW)

        def _boom(*_a, **_k):
            raise OSError("ENOSPC")

        monkeypatch.setattr(pfs, "atomic_write", _boom)
        assert svc.remove_pin(str(target)) is False
        assert [p["path"] for p in svc.get_pins()] == [str(target)]
        # Still watched: nothing durably changed, so the watcher must stay.
        assert svc.get_watched_paths() == {str(target)}
        assert events == []

    def test_mark_seen_on_an_unpinned_path_is_a_silent_no_op(self, tmp_path):
        svc, events = _svc(tmp_path)
        svc.mark_seen("/never/pinned")
        assert events == []

    def test_mark_seen_clears_updated_at_and_broadcasts(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        _write_pins(tmp_path, [{"path": str(target), "updatedAt": NOW}])
        svc, events = _svc(tmp_path)
        svc.load(NOW)
        svc.mark_seen(str(target))
        assert "updatedAt" not in svc.get_pins()[0]
        assert [c for c, _ in events] == ["pinned:files-changed"]

    def test_a_failed_write_leaves_updated_at_in_place(self, tmp_path, monkeypatch):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        _write_pins(tmp_path, [{"path": str(target), "updatedAt": NOW}])
        svc, events = _svc(tmp_path)
        svc.load(NOW)

        def _boom(*_a, **_k):
            raise OSError("ENOSPC")

        monkeypatch.setattr(pfs, "atomic_write", _boom)
        svc.mark_seen(str(target))
        assert svc.get_pins()[0]["updatedAt"] == NOW
        assert events == []


class TestWatchEventProcessing:
    def test_an_event_for_an_unpinned_but_present_file_changes_nothing(self, tmp_path):
        stray = tmp_path / "stray.txt"
        stray.write_text("x", encoding="utf-8")
        svc, events = _svc(tmp_path)
        svc.on_watch_event(str(stray), "change", now_ms=NOW)
        svc.tick(NOW + DEBOUNCE_MS)
        assert events == []

    def test_a_failed_write_suppresses_the_updated_broadcast(self, tmp_path, monkeypatch):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        _write_pins(tmp_path, [{"path": str(target)}])
        svc, events = _svc(tmp_path)
        svc.load(NOW)

        def _boom(*_a, **_k):
            raise OSError("ENOSPC")

        monkeypatch.setattr(pfs, "atomic_write", _boom)
        svc.on_watch_event(str(target), "change", now_ms=NOW)
        svc.tick(NOW + DEBOUNCE_MS)
        assert events == []
        assert "updatedAt" not in svc.get_pins()[0]

    def test_a_reappearing_file_is_rewatched_and_reported_as_updated(self, tmp_path):
        """The atomic-save case: delete-then-recreate must not lose the watcher."""
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        _write_pins(tmp_path, [{"path": str(target)}])
        svc, events = _svc(tmp_path)
        svc.load(NOW)

        os.unlink(target)
        svc.on_watch_event(str(target), "rename", now_ms=NOW)
        svc.tick(NOW + DEBOUNCE_MS)
        assert [c for c, _ in events] == ["pinned:file-deleted"]
        assert svc.get_watched_paths() == set()

        # The retry is pending, not due yet.
        target.write_text("y", encoding="utf-8")
        svc.tick(NOW + DEBOUNCE_MS)
        assert [c for c, _ in events] == ["pinned:file-deleted"]

        svc.tick(NOW + DEBOUNCE_MS + REWATCH_RETRY_MS)
        assert [c for c, _ in events] == ["pinned:file-deleted", "pinned:file-updated"]
        assert svc.get_watched_paths() == {str(target)}
        assert svc.get_pins()[0]["updatedAt"] == NOW + DEBOUNCE_MS + REWATCH_RETRY_MS

    def test_a_retry_for_a_path_that_was_unpinned_meanwhile_does_nothing(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        svc, events = _svc(tmp_path)
        svc._timers[str(target)] = (NOW, (pfs._ACTION_RETRY,))
        svc.tick(NOW)
        assert events == []
        assert svc.get_watched_paths() == set()
