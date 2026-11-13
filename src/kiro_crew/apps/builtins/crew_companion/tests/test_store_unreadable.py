"""A store file that cannot be used is never replaced by an empty store plus one edit.

``load`` starts empty whatever is on disk, so the reminder app always comes up.
The save that follows must not cost the user their reminders, config and stats:
a file that is not JSON is moved to ``<name>.corrupt-<utc>`` first, and a file
whose read fails is kept and every save is refused.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.crew_companion import hooks
from kiro_crew.apps.builtins.crew_companion import store as store_mod
from kiro_crew.apps.builtins.crew_companion.reminders import to_iso
from kiro_crew.apps.builtins.crew_companion.store import CompanionStore

_NAME = "crew-companion-reminders.json"
_FIXED_NOW = datetime(2026, 7, 31, 14, 0, 0)


def _now() -> datetime:
    return _FIXED_NOW


def _good_bytes(tmp_path: Path) -> bytes:
    """A store the store itself wrote: one reminder, a set interval, one session."""
    source = tmp_path / "source"
    s = CompanionStore(source, now=_now)
    s.load()
    s.add("stand up", to_iso(_FIXED_NOW + timedelta(days=1)))
    s.patch_config({"breakReminderMins": 60})
    s.note_breathing_session()
    return (source / _NAME).read_bytes()


def _asides(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"{_NAME}.corrupt-*"))


class TestBadBytesAreSetAside:
    def test_a_file_that_is_not_json_is_moved_aside_before_the_save(self, tmp_path, caplog):
        path = tmp_path / _NAME
        bad = b'{"reminders": [{"id": "r1", "text": "keep me"'
        path.write_bytes(bad)

        s = CompanionStore(tmp_path, now=_now)
        with caplog.at_level(logging.WARNING, logger=store_mod.__name__):
            s.load()
        s.add("new one", to_iso(_FIXED_NOW))

        asides = _asides(tmp_path)
        assert len(asides) == 1
        assert asides[0].read_bytes() == bad
        assert [r["text"] for r in json.loads(path.read_text("utf-8"))["reminders"]] == ["new one"]
        assert any(asides[0].name in rec.getMessage() for rec in caplog.records)

    def test_a_file_that_is_not_utf8_is_moved_aside(self, tmp_path):
        path = tmp_path / _NAME
        path.write_bytes(b"\xff\xfe{}")
        s = CompanionStore(tmp_path, now=_now)
        s.load()
        assert [p.read_bytes() for p in _asides(tmp_path)] == [b"\xff\xfe{}"]

    def test_json_that_is_not_an_object_is_moved_aside(self, tmp_path):
        path = tmp_path / _NAME
        path.write_text("[1, 2, 3]", "utf-8")
        s = CompanionStore(tmp_path, now=_now)
        s.load()
        assert [p.read_text("utf-8") for p in _asides(tmp_path)] == ["[1, 2, 3]"]
        assert s.snapshot()["reminders"] == []

    def test_an_oversized_file_is_moved_aside_before_the_save(self, tmp_path):
        path = tmp_path / _NAME
        big = json.dumps({"reminders": [], "pad": "x" * (store_mod.MAX_BYTES + 10)})
        path.write_text(big, "utf-8")
        s = CompanionStore(tmp_path, now=_now)
        s.load()
        s.add("new one", to_iso(_FIXED_NOW))
        assert [p.read_text("utf-8") for p in _asides(tmp_path)] == [big]

    def test_a_good_file_is_not_moved(self, tmp_path):
        (tmp_path / _NAME).write_bytes(_good_bytes(tmp_path))
        s = CompanionStore(tmp_path, now=_now)
        s.load()
        assert _asides(tmp_path) == []
        assert [r["text"] for r in s.snapshot()["reminders"]] == ["stand up"]


class TestByteOrderMark:
    def test_a_store_saved_with_a_bom_reads_normally(self, tmp_path):
        path = tmp_path / _NAME
        path.write_bytes(b"\xef\xbb\xbf" + _good_bytes(tmp_path))
        s = CompanionStore(tmp_path, now=_now)
        s.load()
        assert [r["text"] for r in s.snapshot()["reminders"]] == ["stand up"]
        assert s.snapshot()["breakReminderMins"] == 60
        assert s.stats_payload()["stats"]["breathingSessions"] == 1
        assert _asides(tmp_path) == []


def _fail_reads_of(mp: pytest.MonkeyPatch, target: Path) -> None:
    """Make every read of ``target`` fail the way a transient EACCES does."""
    real_bytes = Path.read_bytes
    real_text = Path.read_text

    def read_bytes(self):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_bytes(self)

    def read_text(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_text(self, *args, **kwargs)

    mp.setattr(Path, "read_bytes", read_bytes)
    mp.setattr(Path, "read_text", read_text)


class TestAFailedReadRefusesTheWrite:
    def test_every_save_is_refused_and_the_file_is_untouched(self, tmp_path, monkeypatch):
        path = tmp_path / _NAME
        original = _good_bytes(tmp_path)
        path.write_bytes(original)
        s = CompanionStore(tmp_path, now=_now)
        with monkeypatch.context() as mp:
            _fail_reads_of(mp, path)
            s.load()  # must not raise
            with pytest.raises(store_mod.StoreUnreadableError):
                s.add("new one", to_iso(_FIXED_NOW))
            with pytest.raises(OSError):
                s.patch_config({"breakReminderMins": 30})
            s.stop()  # shutdown keeps the file and does not raise

        assert path.read_bytes() == original
        assert _asides(tmp_path) == []
        # The refused add rolled memory back to what loaded.
        assert s.snapshot()["reminders"] == []

    def test_a_later_load_that_reads_the_file_lifts_the_refusal(self, tmp_path, monkeypatch):
        path = tmp_path / _NAME
        path.write_bytes(_good_bytes(tmp_path))
        s = CompanionStore(tmp_path, now=_now)
        with monkeypatch.context() as mp:
            _fail_reads_of(mp, path)
            s.load()

        s.load()
        s.add("new one", to_iso(_FIXED_NOW))
        texts = [r["text"] for r in json.loads(path.read_text("utf-8"))["reminders"]]
        assert texts == ["stand up", "new one"]

    def test_a_failed_move_aside_refuses_the_write(self, tmp_path, monkeypatch):
        path = tmp_path / _NAME
        bad = b"{not json"
        path.write_bytes(bad)

        def refuse(src, dst):
            raise PermissionError(13, "Permission denied", str(src))

        monkeypatch.setattr(store_mod, "replace_with_retry", refuse)
        s = CompanionStore(tmp_path, now=_now)
        s.load()
        with pytest.raises(store_mod.StoreUnreadableError):
            s.add("new one", to_iso(_FIXED_NOW))
        assert path.read_bytes() == bad


class TestReEnableRecovers:
    @pytest.mark.asyncio
    async def test_re_enable_reads_the_file_again_once_it_is_readable(self, tmp_path, monkeypatch):
        path = tmp_path / _NAME
        path.write_bytes(_good_bytes(tmp_path))
        ctx = SimpleNamespace(data_dir=str(tmp_path))
        hooks._reset_for_tests()
        try:
            with monkeypatch.context() as mp:
                _fail_reads_of(mp, path)
                await hooks.on_startup(ctx)
            store = hooks.get_store()
            assert store is not None and store.write_blocked
            await hooks.on_shutdown(ctx)

            await hooks.on_startup(ctx)
            assert hooks.get_store() is store
            assert not store.write_blocked
            assert [r["text"] for r in store.snapshot()["reminders"]] == ["stand up"]
            store.add("new one", to_iso(_FIXED_NOW))
            texts = [r["text"] for r in json.loads(path.read_text("utf-8"))["reminders"]]
            assert texts == ["stand up", "new one"]
        finally:
            hooks._reset_for_tests()


class TestReloadHoldsTheLock:
    def test_a_save_cannot_slip_in_while_a_reload_reads_the_file(self, tmp_path, monkeypatch):
        """A route save racing a reload waits for it instead of writing the empty state."""
        path = tmp_path / _NAME
        path.write_bytes(_good_bytes(tmp_path))
        s = CompanionStore(tmp_path, now=_now)
        real_read = store_mod.read_bytes_with_retry
        seen: list[bool] = []

        def read_while_probing(target, *args, **kwargs):
            # Another thread tries the store lock while the read is in progress.
            result: list[bool] = []

            def probe() -> None:
                got = s._lock.acquire(blocking=False)
                if got:
                    s._lock.release()
                result.append(got)

            worker = threading.Thread(target=probe)
            worker.start()
            worker.join(timeout=10)
            seen.extend(result)
            return real_read(target, *args, **kwargs)

        monkeypatch.setattr(store_mod, "read_bytes_with_retry", read_while_probing)
        s.load()
        assert seen == [False]
