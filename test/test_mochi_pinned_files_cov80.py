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


def _read_pins(tmp_path: Path) -> list[Any]:
    payload = json.loads((tmp_path / DATA_FILE_NAME).read_text(encoding="utf-8"))
    return payload["pins"]


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


class TestReadPinsForUpdate:
    """#8088: the update reader refuses a store it could not read, so the
    whole-file rewrite that follows every caller cannot destroy it."""

    def test_a_missing_file_reads_as_nothing_to_carry_forward(self, tmp_path):
        # The one case where an empty base is true: a first pin on a fresh
        # install must still land, so absence returns None rather than refusing.
        assert pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME)) is None

    def test_a_valid_file_returns_its_stored_list(self, tmp_path):
        _write_pins(tmp_path, [{"path": "/from-disk"}])
        assert pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME)) == [{"path": "/from-disk"}]

    def test_unparseable_bytes_refuse(self, tmp_path):
        (tmp_path / DATA_FILE_NAME).write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(pfs.PinsCorruptError):
            pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME))

    def test_non_utf8_bytes_outside_a_string_refuse(self, tmp_path):
        (tmp_path / DATA_FILE_NAME).write_bytes(b'{"pins": [\xff\xfe]}')
        with pytest.raises(pfs.PinsCorruptError):
            pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME))

    def test_a_non_utf8_byte_INSIDE_a_json_string_refuses(self, tmp_path):
        # The lenient decode's blind spot: U+FFFD inside a quoted value parses
        # CLEANLY, so nothing downstream could have caught it. Strict decoding is
        # what makes this a refusal rather than a silent rewrite.
        (tmp_path / DATA_FILE_NAME).write_bytes(
            b'{"version": 1, "pins": [{"path": "/a", "label": "caf\xe9"}]}'
        )
        with pytest.raises(pfs.PinsCorruptError):
            pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME))

    def test_valid_json_of_the_wrong_shape_refuses(self, tmp_path):
        # Parses fine, so nothing raises on its own -- the quietest of the losses.
        (tmp_path / DATA_FILE_NAME).write_text(json.dumps({"pins": "nope"}), encoding="utf-8")
        with pytest.raises(pfs.PinsCorruptError):
            pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME))
        (tmp_path / DATA_FILE_NAME).write_text(json.dumps([1, 2]), encoding="utf-8")
        with pytest.raises(pfs.PinsCorruptError):
            pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME))

    def test_an_unreadable_file_refuses(self, tmp_path):
        (tmp_path / DATA_FILE_NAME).mkdir()  # a directory where the file should be
        with pytest.raises(pfs.PinsCorruptError):
            pfs.read_pins_for_update(str(tmp_path / DATA_FILE_NAME))

    def test_the_service_reload_leaves_its_list_untouched_when_refusing(self, tmp_path):
        (tmp_path / DATA_FILE_NAME).write_text("{ nope", encoding="utf-8")
        svc, _ = _svc(tmp_path)
        svc._pins = [{"path": "/kept"}]
        with pytest.raises(pfs.PinsCorruptError):
            svc._reload_pins_for_update()
        assert svc.get_pins() == [{"path": "/kept"}]


class TestCorruptStoreIsNotOverwrittenOnMutation:
    """#8088: a corrupt pin store must survive a mutation, not be rewritten from
    the in-memory list -- that rewrite discarded rows another process wrote which
    this one never loaded, and the corrupt file is their only copy.

    The mutation cases deliberately do NOT assert on the exception type: what
    regressed is the FILE, so the assertion that has to fail on the buggy code is
    the one about its bytes. ``TestRefusalIsTheNamedType`` below pins the type.
    """

    @staticmethod
    def _attempt(call) -> None:
        """Run a mutation that is expected to refuse, tolerating either shape --
        so the byte assertion that follows is what decides the test."""
        try:
            call()
        except Exception:  # noqa: BLE001
            pass

    def test_add_pin_does_not_overwrite_a_corrupt_store(self, tmp_path):
        corrupt = "{ cross-process rows this process never parsed"
        pins_file = tmp_path / DATA_FILE_NAME
        pins_file.write_text(corrupt, encoding="utf-8")
        svc, events = _svc(tmp_path)
        svc._pins = [{"path": "/only-in-memory"}]  # a good-looking but partial list

        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        self._attempt(lambda: svc.add_pin(str(target), now_ms=NOW))

        # The refusal happened before _persist: the corrupt bytes are intact.
        assert pins_file.read_text(encoding="utf-8") == corrupt
        assert events == []
        assert svc.get_watched_paths() == set()

    def test_remove_pin_does_not_overwrite_a_corrupt_store(self, tmp_path):
        corrupt = "]]] not json [[["
        pins_file = tmp_path / DATA_FILE_NAME
        pins_file.write_text(corrupt, encoding="utf-8")
        svc, events = _svc(tmp_path)
        svc._pins = [{"path": "/only-in-memory"}]

        self._attempt(lambda: svc.remove_pin("/only-in-memory"))

        assert pins_file.read_text(encoding="utf-8") == corrupt
        assert events == []

    def test_mark_seen_does_not_overwrite_a_corrupt_store(self, tmp_path):
        # The sharpest case: mark_seen has no failure return, so before this fix
        # the route reported ok while the store was being replaced.
        corrupt = "<<garbage>>"
        pins_file = tmp_path / DATA_FILE_NAME
        pins_file.write_text(corrupt, encoding="utf-8")
        svc, events = _svc(tmp_path)
        svc._pins = [{"path": "/only-in-memory", "updatedAt": NOW}]

        self._attempt(lambda: svc.mark_seen("/only-in-memory"))

        assert pins_file.read_text(encoding="utf-8") == corrupt
        assert events == []

    def test_a_watch_event_skips_the_stamp_instead_of_crashing_the_tick(self, tmp_path):
        # The one path that swallows the refusal: it fires from the owner's tick
        # loop, where an escaping error would take down every other tick.
        target = tmp_path / "watched.txt"
        target.write_text("x", encoding="utf-8")
        corrupt = "{ truncated"
        pins_file = tmp_path / DATA_FILE_NAME
        pins_file.write_text(corrupt, encoding="utf-8")
        svc, events = _svc(tmp_path)
        svc._pins = [{"path": str(target)}]

        svc.on_watch_event(str(target), "change", now_ms=NOW)
        svc.tick(NOW + DEBOUNCE_MS)  # must not raise

        assert pins_file.read_text(encoding="utf-8") == corrupt
        assert events == []

    def test_the_mcp_pin_tools_do_not_overwrite_a_corrupt_store(self, tmp_path, monkeypatch):
        # The SECOND writer of the same file, and the worse half of the bug: its
        # lenient read defaulted to an EMPTY list, so a corrupt store was zeroed
        # rather than merely reverted to one process's view.
        from kiro_crew.apps.builtins.mochi import mcp_server

        corrupt = '{"pins": [{"path": "/a"}, {"path": ' + '"/truncated'
        pins_file = tmp_path / DATA_FILE_NAME
        pins_file.write_text(corrupt, encoding="utf-8")
        monkeypatch.setattr(mcp_server, "_data_dir", lambda: tmp_path)

        out = mcp_server._tool_pin_file({"path": str(tmp_path / "x.txt")})
        assert "corrupt" in out
        assert pins_file.read_text(encoding="utf-8") == corrupt

        out = mcp_server._tool_unpin_file({"path": "/a"})
        assert "corrupt" in out
        assert pins_file.read_text(encoding="utf-8") == corrupt

    def test_a_mutation_preserves_a_non_utf8_byte_in_a_label(self, tmp_path):
        # The sharpest loss of the set: valid JSON, so before the strict decode the
        # mutation succeeded and wrote U+FFFD over the operator's only copy of the
        # original byte. Nothing raised and nothing was logged.
        raw = b'{"version": 1, "pins": [{"path": "/a", "label": "caf\xe9 notes"}]}'
        pins_file = tmp_path / DATA_FILE_NAME
        pins_file.write_bytes(raw)
        svc, events = _svc(tmp_path)
        svc._pins = [{"path": "/a", "label": "caf\ufffd notes"}]  # what a lenient read yields

        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        self._attempt(lambda: svc.add_pin(str(target), now_ms=NOW))

        assert pins_file.read_bytes() == raw
        assert events == []

    def test_a_missing_store_still_lets_the_first_pin_land(self, tmp_path):
        # Regression guard: refusing on corruption must NOT refuse on absence,
        # or a fresh install could never add its first pin.
        svc, _ = _svc(tmp_path)
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")
        assert svc.add_pin(str(target), now_ms=NOW) is True
        written = json.loads((tmp_path / DATA_FILE_NAME).read_text(encoding="utf-8"))
        assert [p["path"] for p in written["pins"]] == [str(target)]

    def test_the_mcp_pin_tools_still_work_on_a_healthy_store(self, tmp_path, monkeypatch):
        # The strict read must not break the ordinary path it now guards.
        from kiro_crew.apps.builtins.mochi import mcp_server

        _write_pins(tmp_path, [{"path": "/a"}])
        monkeypatch.setattr(mcp_server, "_data_dir", lambda: tmp_path)

        mcp_server._tool_pin_file({"path": "/b"})
        assert [p["path"] for p in _read_pins(tmp_path)] == ["/a", "/b"]
        mcp_server._tool_unpin_file({"path": "/a"})
        assert [p["path"] for p in _read_pins(tmp_path)] == ["/b"]


class TestRefusalIsTheNamedType:
    """Both writers have to recognise the SAME refusal, so it is one public type
    rather than a module-private one -- see PinsCorruptError's docstring."""

    def test_each_mutator_raises_pins_corrupt_error(self, tmp_path):
        (tmp_path / DATA_FILE_NAME).write_text("{ nope", encoding="utf-8")
        svc, _ = _svc(tmp_path)
        svc._pins = [{"path": "/p"}]
        target = tmp_path / "notes.txt"
        target.write_text("x", encoding="utf-8")

        with pytest.raises(pfs.PinsCorruptError):
            svc.add_pin(str(target), now_ms=NOW)
        with pytest.raises(pfs.PinsCorruptError):
            svc.remove_pin("/p")
        with pytest.raises(pfs.PinsCorruptError):
            svc.mark_seen("/p")


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
