"""Session storage: what one session is, and how it is measured.

A kiro-cli half and its mapped transcript count as one session, with the transcript's
archive segments and the crew logs whose header names its slot; ``TestCrewLogsAreTheSessions``
also pins that those crew logs are staged, restored, emptied and held from the retention
sweep with it. A mapped session protects both halves, one session may own several
transcript names, reads may reuse one filesystem pass (the scan cache), ages fall into
the documented buckets, and staged bytes are reported apart from reclaimable ones.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _DAY,
    _NOW,
    _archive_segment,
    _cli_half,
    _fresh_scan_cache,
    _index,
    _transcript,
    stores_fixture,
)

from conftest import requires_symlinks
from kiro_crew import session_storage
from kiro_crew.history import transcript_stem
from kiro_crew.session_storage import SessionIndex, SessionStorageError


def _multi_index(stem_to_sid: dict[str, str], active: set[str] | None = None) -> SessionIndex:
    """Build an index directly, for the many-stems-one-session cases."""
    return SessionIndex(stem_to_sid=stem_to_sid, active_sids=frozenset(active or set()))


class TestPairing:
    def test_a_paired_session_is_counted_once_with_one_total(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        cli = _cli_half(kiro_home, "aaaa1111", log_bytes=4096, age_days=40)
        crew = _transcript(crew_home, "dashboard_chat-1", size=500, age_days=40)

        report = session_storage.measure(_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW)

        assert report.total_sessions == 1
        assert report.total_bytes == cli + crew
        assert report.reclaimable_sessions == 1

    def test_transcript_stem_is_derived_from_the_history_module(self) -> None:
        """The pairing rule has exactly one source; a local copy would drift."""
        assert transcript_stem("dashboard:chat-1") == "dashboard_chat-1"
        assert transcript_stem("telegram:crew:direct:87431") == "telegram_crew_direct_87431"

    def test_archive_segments_belong_to_their_session(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        cli = _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        crew = _transcript(crew_home, "dashboard_chat-1", size=100, age_days=40)
        seg = _archive_segment(
            crew_home, "dashboard_chat-1", "20260730-211852", size=900, age_days=40
        )

        report = session_storage.measure(_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW)

        assert report.total_sessions == 1
        assert report.total_bytes == cli + crew + seg

    def test_a_segment_is_not_mistaken_for_a_session_of_its_own(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A stem that prefixes another must not absorb the other's segments."""
        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=10, age_days=40)
        _transcript(crew_home, "dashboard_chat-14", size=10, age_days=40)
        _archive_segment(crew_home, "dashboard_chat-14", "20260730-211852", size=700, age_days=40)

        units = {u.uid: u.bytes for u in session_storage.select_reclaimable(_index(), 0, now=_NOW)}

        assert units == {"dashboard_chat-1": 10, "dashboard_chat-14": 710}

    def test_unpaired_halves_are_each_their_own_session(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        _transcript(crew_home, "cron_74173071", size=32, age_days=40)

        report = session_storage.measure(_index(), now=_NOW)

        assert report.total_sessions == 2
        assert report.reclaimable_sessions == 2

    def test_only_a_transcript_with_a_kiro_cli_half_carries_a_sid(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A transcript's unit takes its sid from the kiro-cli half it pairs with.

        The map names ``bbbb2222`` for the second transcript, but no kiro-cli half
        of that id exists, so that transcript is a unit of its own with no sid, the
        same as one the map does not name at all.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=40)
        _transcript(crew_home, "dashboard_chat-2", size=32, age_days=40)
        _transcript(crew_home, "cron_74173071", size=32, age_days=40)
        index = _index({"aaaa1111": "dashboard_chat-1", "bbbb2222": "dashboard_chat-2"})

        units = session_storage.select_reclaimable(index, 0, now=_NOW)

        assert {u.uid: u.sid for u in units} == {
            "aaaa1111": "aaaa1111",
            "dashboard_chat-2": "",
            "cron_74173071": "",
        }

    @requires_symlinks
    def test_a_transcript_symlink_is_not_owned_by_the_session(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A symlink planted at the transcript's name is not the session's file.

        _unit_paths feeds the move/restore/delete paths, so following a link here
        would relocate the link (or read through it) instead of the real file. A
        link at the transcript name is excluded exactly as a regular file is
        included; the regular transcript below proves the exclusion is the link's
        doing, not an empty result.
        """
        crew_home, _ = stores
        sessions = crew_home / "sessions"
        # An ordinary transcript IS owned.
        _transcript(crew_home, "dashboard_chat-real", size=10, age_days=40)
        owned = session_storage._unit_paths("", ("dashboard_chat-real",), archives={}, cli_files={})
        assert any(p.name == "dashboard_chat-real.jsonl" for p, _rel in owned)
        # A symlink at the transcript name is NOT.
        outside = crew_home / "outside.jsonl"
        outside.write_bytes(b"x" * 10)
        (sessions / "dashboard_chat-link.jsonl").symlink_to(outside)
        linked = session_storage._unit_paths(
            "", ("dashboard_chat-link",), archives={}, cli_files={}
        )
        assert not any(p.name == "dashboard_chat-link.jsonl" for p, _rel in linked)


def _crew_log(unit_id: str, slot: str, *, closed: bool = True) -> int:
    """One session crew-log unit whose header names *slot*; its size on disk.

    Closed ``destroyed``, the one close the retention sweep collects, so a test that runs
    the sweep sees a unit it would take unless something holds it.
    """
    from crew_log_type_helpers import minimal_data

    from kiro_crew.crew_log import CrewLog
    from kiro_crew.crew_log.schema import KIND_SESSION

    log = CrewLog.create(KIND_SESSION, unit_id, owner="default", agent="kirocrew", slot=slot)
    log.append("session/opened", minimal_data(KIND_SESSION, "session/opened"), src="gateway")
    if closed:
        log.append("session/closed", {"reason": "destroyed"}, src="gateway")
    path = log.path
    del log
    old = _NOW - 40 * _DAY
    for child in path.parent.iterdir():
        os.utime(child, (old, old))
    return sum(child.stat().st_size for child in path.parent.iterdir() if child.is_file())


def _crew_log_exists(unit_id: str) -> bool:
    from kiro_crew.crew_log import CrewLog
    from kiro_crew.crew_log.schema import KIND_SESSION

    return CrewLog.exists(KIND_SESSION, unit_id)


class TestCrewLogsAreTheSessions:
    """A session's crew logs are measured, reclaimed and restored with it.

    One unit per ACP id the conversation ran under, so a reset leaves two; both
    belong to the transcript their header's slot names.
    """

    def test_the_report_counts_every_crew_log_unit(self, stores: tuple[Path, Path]) -> None:
        crew_home, _ = stores
        crew = _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        before_reset = _crew_log("acp-before-reset", "chat-1")
        after_reset = _crew_log("acp-after-reset", "chat-1")

        report = session_storage.measure(_index(), now=_NOW)

        assert report.total_sessions == 1
        assert report.total_bytes == crew + before_reset + after_reset

    def test_a_mapped_crew_log_unit_keeps_its_session_active(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-mapped", "chat-1")

        index = _index(active={"acp-mapped"})
        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(
                ["dashboard_chat-1"], reason="manual", index=index, now=_NOW
            )

        assert _crew_log_exists("acp-mapped")

    def test_trash_stages_them_outside_the_batch_and_restore_puts_them_back(
        self, stores: tuple[Path, Path]
    ) -> None:
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        size = _crew_log("acp-before-reset", "chat-1") + _crew_log("acp-after-reset", "chat-1")

        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )

        assert not _crew_log_exists("acp-before-reset")
        assert not _crew_log_exists("acp-after-reset")
        staged = session_storage.trash_root() / batch.batch_id
        assert not any(
            "log.jsonl" == path.name for path in staged.rglob("*")
        ), "a crew log was staged in the agent-writable batch"
        assert (crew_log_trash_root() / batch.batch_id / "dashboard_chat-1").is_dir()
        assert batch.bytes == 50 + size
        assert session_storage.list_trash()[0].bytes == 50 + size

        assert session_storage.restore(batch.batch_id) == 1

        assert _crew_log_exists("acp-before-reset")
        assert _crew_log_exists("acp-after-reset")
        assert not (crew_log_trash_root() / batch.batch_id).exists()

    def test_emptying_the_trash_removes_the_staged_crew_logs(
        self, stores: tuple[Path, Path]
    ) -> None:
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )

        freed = session_storage.empty_trash([batch.batch_id])

        # The manifest is freed too, so the total is at least what was staged.
        assert freed >= batch.bytes
        assert not (crew_log_trash_root() / batch.batch_id).exists()
        assert not _crew_log_exists("acp-before-reset")

    def test_a_crew_log_a_writer_holds_leaves_the_whole_session_in_place(
        self, stores: tuple[Path, Path]
    ) -> None:
        from crew_log_type_helpers import minimal_data

        from kiro_crew.crew_log import CrewLog
        from kiro_crew.crew_log.schema import KIND_SESSION

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-idle", "chat-1")
        held = CrewLog.create(KIND_SESSION, "acp-held", owner="default", agent="k", slot="chat-1")
        held.append("session/opened", minimal_data(KIND_SESSION, "session/opened"), src="gateway")
        old = _NOW - 40 * _DAY
        for child in held.path.parent.iterdir():
            os.utime(child, (old, old))

        with pytest.raises(SessionStorageError, match="resumed"):
            session_storage.move_to_trash(
                ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
            )

        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
        assert _crew_log_exists("acp-idle"), "a unit staged before the refusal was not put back"
        assert _crew_log_exists("acp-held")
        del held

    def test_an_unreadable_crew_log_staging_keeps_the_whole_session_staged(
        self, stores: tuple[Path, Path], monkeypatch
    ) -> None:
        """A staging that cannot be listed is not an empty one.

        Read as empty, the restore would put the transcript back, drop the manifest
        entry, and leave the staged units with nothing that lists or restores them.
        """
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        live = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        _crew_log("acp-after-reset", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        staging = crew_log_trash_root() / batch.batch_id / "dashboard_chat-1"
        staged_before = sorted(child.name for child in staging.iterdir())
        real_iterdir = Path.iterdir

        def _unreadable(self: Path):
            if self == staging:
                raise PermissionError(errno.EACCES, "injected listing failure", str(self))
            return real_iterdir(self)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "iterdir", _unreadable)
            assert session_storage.restore(batch.batch_id) == 0

        assert not live.exists(), "the transcript went back without its crew logs"
        assert session_storage.list_trash()[0].sessions == 1
        assert sorted(child.name for child in staging.iterdir()) == staged_before
        assert not _crew_log_exists("acp-before-reset")
        assert not _crew_log_exists("acp-after-reset")

        assert session_storage.restore(batch.batch_id) == 1
        assert live.is_file()
        assert _crew_log_exists("acp-before-reset")
        assert _crew_log_exists("acp-after-reset")

    @pytest.mark.parametrize("failure", ["listing", "walk"])
    def test_a_crew_log_scan_that_cannot_complete_moves_nothing(
        self, stores: tuple[Path, Path], monkeypatch, failure: str
    ) -> None:
        """A reclaim stages what the scan lists, so a short scan would split a session.

        The transcript would go to the trash and its crew logs would stay behind,
        attached to no row. So an incomplete crew-log scan refuses the whole move; the
        storage report, a read, still answers with what it could list.
        """
        from kiro_crew.crew_log import store as crew_store

        crew_home, _ = stores
        live = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")

        def _unlistable(**_kwargs):
            raise OSError(errno.EIO, "injected crew-log listing failure")

        def _unwalkable(_directory: Path):
            raise OSError(errno.EIO, "injected crew-log walk failure")

        with monkeypatch.context() as patch:
            if failure == "listing":
                patch.setattr(crew_store, "session_units_by_slot", _unlistable)
            else:
                patch.setattr(session_storage, "_tree_files", _unwalkable)
            with pytest.raises(SessionStorageError, match="no session was moved"):
                session_storage.move_to_trash(
                    ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
                )
            assert session_storage.measure(_index(), now=_NOW).total_sessions == 1

        assert live.is_file()
        assert _crew_log_exists("acp-before-reset")
        assert session_storage.list_trash() == []

    def test_a_staging_directory_that_cannot_be_created_puts_the_crew_logs_back(
        self, stores: tuple[Path, Path], monkeypatch
    ) -> None:
        """The crew logs move first, so a later failure must return them with the rest."""
        crew_home, _ = stores
        live = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        real_mkdir = Path.mkdir
        batches = session_storage.trash_root()

        def _mkdir(self: Path, *args, **kwargs):
            if batches in self.parents and self.name == "crew":
                raise OSError(errno.ENOSPC, "injected mkdir failure", str(self))
            return real_mkdir(self, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "mkdir", _mkdir)
            try:
                session_storage.move_to_trash(
                    ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
                )
            except SessionStorageError:
                pass

        assert live.is_file()
        assert _crew_log_exists("acp-before-reset")
        assert not self._held("acp-before-reset")
        assert all(batch.sessions == 0 for batch in session_storage.list_trash())

    def test_a_resume_after_the_last_refresh_keeps_the_crew_logs_and_the_session(
        self, stores: tuple[Path, Path], monkeypatch
    ) -> None:
        """A read-only resume maps the session after *refresh* last looked.

        Its mapping is in the live map, not yet in the file *refresh* reads, and it
        writes nothing the mtime guard could see. The liveness read and the staging share
        the lock the resume maps under, so the resume is seen and nothing moves.
        """
        import threading

        from kiro_crew.crew_log import store as crew_store

        crew_home, _ = stores
        live = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        lock = threading.RLock()
        live_map: dict[str, str] = {}
        real_purge = session_storage._purge_search_index
        real_stage = crew_store.stage_unit
        staged_under_lock: list[bool] = []

        def _resume_then_purge(stems):
            with lock:
                live_map["acp-resumed"] = "dashboard_chat-1"
            return real_purge(stems)

        def _stage(*args, **kwargs):
            staged_under_lock.append(lock._is_owned())  # type: ignore[attr-defined]
            return real_stage(*args, **kwargs)

        def _live_index() -> SessionIndex:
            return _index(dict(live_map), active=set(live_map))

        monkeypatch.setattr(session_storage, "_purge_search_index", _resume_then_purge)
        monkeypatch.setattr(crew_store, "stage_unit", _stage)

        try:
            session_storage.move_to_trash(
                ["dashboard_chat-1"],
                reason="manual",
                index=_index(),
                now=_NOW,
                refresh=_index,
                activation=(lock, _live_index),
            )
        except SessionStorageError:
            pass

        assert live.is_file()
        assert _crew_log_exists("acp-before-reset")
        assert staged_under_lock == []
        assert all(batch.sessions == 0 for batch in session_storage.list_trash())

    def test_crew_logs_are_staged_inside_the_activation_lock(
        self, stores: tuple[Path, Path], monkeypatch
    ) -> None:
        import threading

        from kiro_crew.crew_log import store as crew_store

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        lock = threading.RLock()
        real_stage = crew_store.stage_unit
        staged_under_lock: list[bool] = []

        def _stage(*args, **kwargs):
            staged_under_lock.append(lock._is_owned())  # type: ignore[attr-defined]
            return real_stage(*args, **kwargs)

        monkeypatch.setattr(crew_store, "stage_unit", _stage)

        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"],
            reason="manual",
            index=_index(),
            now=_NOW,
            activation=(lock, _index),
        )

        assert batch.sessions == 1
        assert staged_under_lock == [True]

    def test_a_unit_put_back_by_a_refused_move_carries_no_hold(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The staged unit goes back live with its session, so nothing keeps it held.

        Named to sort first, so it is staged before the unit a writer holds refuses the move.
        """
        from crew_log_type_helpers import minimal_data

        from kiro_crew.crew_log import CrewLog
        from kiro_crew.crew_log.schema import KIND_SESSION

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-a-idle", "chat-1")
        held = CrewLog.create(KIND_SESSION, "acp-z-held", owner="default", agent="k", slot="chat-1")
        held.append("session/opened", minimal_data(KIND_SESSION, "session/opened"), src="gateway")
        old = _NOW - 40 * _DAY
        for child in held.path.parent.iterdir():
            os.utime(child, (old, old))

        with pytest.raises(SessionStorageError, match="resumed"):
            session_storage.move_to_trash(
                ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
            )

        assert _crew_log_exists("acp-a-idle")
        assert _crew_log_exists("acp-z-held")
        assert not self._held("acp-a-idle")
        del held

    def test_an_occupied_crew_log_name_keeps_the_whole_session_staged(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        _crew_log("acp-after-reset", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        _crew_log("acp-after-reset", "chat-2")  # the name is taken again

        assert session_storage.restore(batch.batch_id) == 0

        assert not (crew_home / "sessions" / "dashboard_chat-1.jsonl").exists()
        assert not _crew_log_exists("acp-before-reset"), "restored without its sibling"
        assert session_storage.list_trash()[0].sessions == 1

    @staticmethod
    def _fail_the_restore_after_the_crew_logs_went_back(monkeypatch, mode: str) -> list:
        """Make the transcript's restore fail, then make staging a unit back fail too.

        ``fsync``: every crew-log directory sync fails from then on, so neither unit can
        be staged again. ``owned``: the session is resumed mid-restore, so the unit it
        writes is leased and stays live. Returns the handles a resume keeps open.
        """
        from crew_log_type_helpers import minimal_data

        from kiro_crew.crew_log import CrewLog
        from kiro_crew.crew_log import store as crew_store
        from kiro_crew.crew_log.schema import KIND_SESSION

        failing = {"on": False}
        handles: list = []
        real_sync = crew_store._sync_down

        def _sync_down(top: Path, leaf: Path) -> None:
            if failing["on"]:
                raise OSError(errno.EIO, "injected fsync failure")
            real_sync(top, leaf)

        def _move(src: Path, dst: Path, **_kwargs) -> bool:
            if mode == "fsync":
                failing["on"] = True
                raise OSError(errno.EIO, "injected transcript move failure")
            resumed = CrewLog.open(KIND_SESSION, "acp-before-reset")
            resumed.append(
                "session/opened", minimal_data(KIND_SESSION, "session/opened"), src="gateway"
            )
            handles.append(resumed)
            return False

        monkeypatch.setattr(crew_store, "_sync_down", _sync_down)
        monkeypatch.setattr(session_storage, "_move_file_exclusive", _move)
        return handles

    @staticmethod
    def _held(unit_id: str) -> bool:
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store import TRASH_HOLD_FILE, unit_dir_for

        directory = unit_dir_for(KIND_SESSION, unit_id)
        return directory is not None and (directory / TRASH_HOLD_FILE).exists()

    @pytest.mark.parametrize("mode", ["fsync", "owned"])
    def test_a_unit_a_failed_restore_cannot_stage_again_is_held_from_the_sweep(
        self, stores: tuple[Path, Path], monkeypatch, mode: str
    ) -> None:
        """A failed rollback leaves a unit live beside a staged transcript: it is held.

        The session stays listed in the trash, the sweep does not expire the live half,
        and restoring the session once the failure has passed makes it whole and lets
        the unit age out again.
        """
        from kiro_crew.crew_log import store as crew_store

        crew_home, _ = stores
        live = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        _crew_log("acp-after-reset", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        with monkeypatch.context() as patch:
            handles = self._fail_the_restore_after_the_crew_logs_went_back(patch, mode)
            assert session_storage.restore(batch.batch_id) == 0

        assert not live.exists(), "the transcript left the trash without its session"
        assert session_storage.list_trash()[0].sessions == 1
        assert _crew_log_exists("acp-before-reset")
        assert self._held("acp-before-reset")
        # Far past every retention window: a held unit is still not collected.
        # flake-ok: pre-existing wall-clock read anchoring a sweep 400 days out; moved verbatim
        assert crew_store.sweep_expired(0, now=time.time() + 400 * 86400)[0] == 0
        assert _crew_log_exists("acp-before-reset")

        handles.clear()
        assert session_storage.restore(batch.batch_id) == 1

        assert live.is_file()
        assert not self._held("acp-before-reset")
        assert not self._held("acp-after-reset")
        if mode == "fsync":
            # flake-ok: pre-existing wall-clock read anchoring a sweep 400 days out; moved verbatim
            assert crew_store.sweep_expired(0, now=time.time() + 400 * 86400) == (2, 0)

    def test_a_sweep_between_publishing_the_units_and_the_commit_takes_nothing(
        self, stores: tuple[Path, Path], monkeypatch
    ) -> None:
        """The units go back before the transcript; a sweep in that gap must not take them.

        Both units are closed and far past the window by the time the sweep runs, so an
        unheld unit is collectable. Once the restore commits the holds are released and
        the same sweep collects them, so the hold lasts exactly as long as the restore.
        """
        from kiro_crew.crew_log import store as crew_store

        crew_home, _ = stores
        live = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        _crew_log("acp-after-reset", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        # flake-ok: pre-existing wall-clock read anchoring a sweep 400 days out; moved verbatim
        later = time.time() + 400 * 86400
        swept: list[tuple[int, int]] = []
        real_move = session_storage._move_file_exclusive

        def _sweep_then_move(src: Path, dst: Path, **kwargs) -> bool:
            if not swept:
                swept.append(crew_store.sweep_expired(0, now=later))
            return real_move(src, dst, **kwargs)

        monkeypatch.setattr(session_storage, "_move_file_exclusive", _sweep_then_move)

        assert session_storage.restore(batch.batch_id) == 1

        assert swept == [(0, 0)], "the sweep collected a unit the restore had published"
        assert live.is_file()
        assert _crew_log_exists("acp-before-reset")
        assert _crew_log_exists("acp-after-reset")
        assert not self._held("acp-before-reset")
        assert not self._held("acp-after-reset")
        assert crew_store.sweep_expired(0, now=later) == (2, 0)

    def test_a_unit_whose_hold_cannot_be_made_durable_is_not_published(
        self, stores: tuple[Path, Path], monkeypatch
    ) -> None:
        """The hold is synced before the unit goes back; a failed sync keeps it staged.

        Published with a hold that is not on disk, a power loss would bring the unit back
        unheld and the sweep could expire it while its transcript is still in the trash.
        """
        from kiro_crew.crew_log import store as crew_store
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        live = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )

        def _fail(_directory: Path) -> None:
            raise OSError(errno.EIO, "injected hold sync failure")

        with monkeypatch.context() as patch:
            patch.setattr(crew_store, "_sync_hold_dir", _fail)
            assert session_storage.restore(batch.batch_id) == 0

        assert not _crew_log_exists("acp-before-reset")
        assert not live.exists()
        assert (crew_log_trash_root() / batch.batch_id / "dashboard_chat-1").is_dir()
        assert session_storage.list_trash()[0].sessions == 1

        assert session_storage.restore(batch.batch_id) == 1
        assert _crew_log_exists("acp-before-reset")
        assert not self._held("acp-before-reset")

    def test_emptying_the_batch_releases_a_held_unit(
        self, stores: tuple[Path, Path], monkeypatch
    ) -> None:
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-before-reset", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        with monkeypatch.context() as patch:
            self._fail_the_restore_after_the_crew_logs_went_back(patch, "fsync")
            assert session_storage.restore(batch.batch_id) == 0
        assert self._held("acp-before-reset")

        session_storage.empty_trash([batch.batch_id])

        assert not (crew_log_trash_root() / batch.batch_id).exists()
        assert _crew_log_exists("acp-before-reset")
        assert not self._held("acp-before-reset")

    def test_a_manifest_uid_cannot_step_out_of_the_crew_log_staging(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The manifest is agent-writable; its uid must never become a ``..`` path."""
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store import crew_log_root, crew_log_trash_root

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-forge", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        # The planted staging: a real unit moved outside the hidden tree, and a manifest
        # entry whose uid walks back out to it.
        planted = session_storage.trash_root() / batch.batch_id / "forge"
        planted.mkdir()
        staged_uid = crew_log_trash_root() / batch.batch_id / "dashboard_chat-1"
        for unit_dir in staged_uid.iterdir():
            unit_dir.rename(planted / unit_dir.name)
        escape = os.path.relpath(planted, crew_log_trash_root() / batch.batch_id)
        manifest = session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME
        # First after the header, while the batch's crew-log staging still exists for
        # the ``..`` to walk back out of.
        header, *entries = manifest.read_text(encoding="utf-8").splitlines()
        planted_entry = json.dumps({"uid": escape, "files": []})
        manifest.write_text("\n".join([header, planted_entry, *entries]) + "\n", encoding="utf-8")

        session_storage.restore(batch.batch_id)

        assert not any((crew_log_root(KIND_SESSION)).glob("*")), "a planted unit was published"

    def test_crew_logs_that_cannot_go_back_are_recorded_not_dropped(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed move puts the crew logs back; one it cannot is listed, never lost."""
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _crew_log("acp-recreated", "chat-1")

        def _unstatable(_path):
            raise OSError("stat failed")

        monkeypatch.setattr(session_storage, "_file_stamp", _unstatable)
        monkeypatch.setattr(session_storage, "_restore_crew_logs", lambda *_a: None)

        with pytest.raises(SessionStorageError, match="can be restored"):
            session_storage.move_to_trash(
                ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
            )

        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
        (batch,) = session_storage.list_trash()
        manifest = session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME
        entries = [json.loads(line) for line in manifest.read_text().splitlines()[1:]]
        assert entries == [{"uid": "dashboard_chat-1", "files": []}]
        assert any((crew_log_trash_root() / batch.batch_id / "dashboard_chat-1").iterdir())

    def test_a_linked_threads_dir_keeps_the_crew_logs_staged_too(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The restore that refuses a sidecar link takes nothing of the session back."""
        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        threads = crew_home / "sessions" / ".threads"
        threads.mkdir()
        sidecar = threads / "dashboard_chat-1.json"
        sidecar.write_text("{}", encoding="utf-8")
        old = _NOW - 40 * _DAY
        os.utime(sidecar, (old, old))
        _crew_log("acp-with-threads", "chat-1")
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        # Linked to a directory INSIDE the session store, so the origin checks pass and
        # the restore reaches the link refusal itself.
        threads.rmdir()
        threads.symlink_to(crew_home / "sessions" / "archive", target_is_directory=True)

        assert session_storage.restore(batch.batch_id) == 0

        assert not _crew_log_exists("acp-with-threads"), "half the session came back"

    def test_empty_refuses_a_batch_holding_crew_logs_its_manifest_does_not_list(
        self, stores: tuple[Path, Path]
    ) -> None:
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        # What a put-back that could not record its entry leaves behind.
        stray = crew_log_trash_root() / batch.batch_id / "dashboard_chat-9" / "unit"
        stray.mkdir(parents=True)
        (stray / "log.jsonl").write_text("{}\n", encoding="utf-8")
        skips: list[str] = []

        assert session_storage.empty_trash([batch.batch_id], on_skip=skips.append) == 0

        assert skips == [session_storage.SKIP_UNLISTED_FILES]
        assert (stray / "log.jsonl").is_file()

    def test_a_batch_removed_behind_the_trash_does_not_authorize_deleting_its_crew_logs(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The batch directory is agent-writable; its absence approves nothing."""
        from kiro_crew.crew_log.store import crew_log_trash_root

        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _transcript(crew_home, "dashboard_chat-2", size=50, age_days=40)
        _crew_log("acp-kept", "chat-1")
        victim = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )
        other = session_storage.move_to_trash(
            ["dashboard_chat-2"], reason="manual", index=_index(), now=_NOW
        )
        shutil.rmtree(session_storage.trash_root() / victim.batch_id)

        session_storage.empty_trash([other.batch_id])

        assert any((crew_log_trash_root() / victim.batch_id / "dashboard_chat-1").iterdir())

    def test_a_session_made_only_of_crew_logs_round_trips(self, stores: tuple[Path, Path]) -> None:
        _crew_log("acp-orphan", "chat-gone")

        units = {u.uid: u for u in session_storage.select_reclaimable(_index(), 0, now=_NOW)}
        assert set(units) == {"acp-orphan"}

        batch = session_storage.move_to_trash(
            ["acp-orphan"], reason="manual", index=_index(), now=_NOW
        )
        assert batch.sessions == 1
        assert not _crew_log_exists("acp-orphan")

        assert session_storage.restore(batch.batch_id) == 1
        assert _crew_log_exists("acp-orphan")


class TestActiveExclusion:
    def test_a_mapped_session_protects_both_halves(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=400)

        index = _index({"aaaa1111": "dashboard_chat-1"}, active={"aaaa1111"})
        report = session_storage.measure(index, now=_NOW)

        assert report.active_sessions == 1
        assert report.reclaimable_sessions == 0
        assert session_storage.select_reclaimable(index, 0, now=_NOW) == []


class TestScanCache:
    """The cache must save disk without ever answering a refusal from stale state."""

    def test_a_read_reuses_one_pass_for_the_rows_and_the_totals(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The screen needs both, and re-enumerating cost seconds per open."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        calls = 0
        real = session_storage._scan_raw_uncached

        def counted(sid_for_stem, **kwargs):
            nonlocal calls
            calls += 1
            return real(sid_for_stem, **kwargs)

        monkeypatch.setattr(session_storage, "_scan_raw_uncached", counted)

        index = _index()
        units = session_storage.list_units(index)
        session_storage.measure(index, units=units, now=_NOW)
        session_storage.list_units(index)

        assert calls == 1, "three reads of one snapshot must enumerate the stores once"

    def test_a_resumed_session_is_never_reported_retired_from_a_cached_pass(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The in-use flags come from the caller's index on every call.

        This is what makes caching safe at all: the filesystem half is reused, but
        "may this be reclaimed" is recomputed, so a session that became active
        after the pass cannot be offered.
        """
        crew_home, kiro_home = stores
        stem = transcript_stem("dashboard:chat-1")
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        _transcript(crew_home, stem, size=64, age_days=40)

        idle = _index({"aaaa1111": stem})
        assert session_storage.measure(idle, now=_NOW).reclaimable_sessions == 1

        # Same stores and same pairing, so the cached filesystem pass is eligible
        # for reuse — only the active set changed.
        resumed = _index({"aaaa1111": stem}, active={"aaaa1111"})
        report = session_storage.measure(resumed, now=_NOW)
        assert report.reclaimable_sessions == 0
        assert report.active_sessions == 1

    def test_a_new_pairing_is_not_answered_from_an_older_pass(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Pairing decides which unit a transcript belongs to, so it keys the cache."""
        crew_home, kiro_home = stores
        stem = transcript_stem("dashboard:chat-1")
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        _transcript(crew_home, stem, size=64, age_days=40)

        unpaired = session_storage.list_units(_index())
        assert len(unpaired) == 2, "unpaired, the two halves are separate units"

        paired = session_storage.list_units(_index({"aaaa1111": stem}))
        assert len(paired) == 1, "the pairing change must not be served from the old pass"

    def test_a_different_store_is_not_answered_from_an_older_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Store locations resolve per call, so they are part of the cache key.

        A pod overrides the data home and an unmigrated install resolves the legacy
        one, so one process can enumerate different stores over its lifetime. Keyed
        only on the pairing, the second home was answered with the first's contents.
        """
        first = tmp_path / "home-a"
        (first / "sessions").mkdir(parents=True)
        (first / "kiro" / "sessions" / "cli").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(first))
        monkeypatch.setenv("KIRO_HOME", str(first / "kiro"))
        _cli_half(first / "kiro", "aaaa1111", log_bytes=32, age_days=40)
        assert len(session_storage.list_units(_index())) == 1

        second = tmp_path / "home-b"
        (second / "sessions").mkdir(parents=True)
        (second / "kiro" / "sessions" / "cli").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(second))
        monkeypatch.setenv("KIRO_HOME", str(second / "kiro"))

        assert session_storage.list_units(_index()) == [], "an empty store must read as empty"

    def test_a_reclaim_does_not_select_against_a_cached_pass(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mutation re-enumerates: a snapshot is for reporting, never for moving."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        session_storage.list_units(_index())  # prime the cache

        calls = 0
        real = session_storage._scan_raw_uncached

        def counted(sid_for_stem, **kwargs):
            nonlocal calls
            calls += 1
            return real(sid_for_stem, **kwargs)

        monkeypatch.setattr(session_storage, "_scan_raw_uncached", counted)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        assert calls >= 1, "the move must enumerate the stores itself"

    def test_a_move_drops_the_cache_so_a_refetch_cannot_list_it(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The screen refetches straight after a move; it must not show the row."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        assert len(session_storage.list_units(_index())) == 1

        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        assert session_storage.list_units(_index()) == []

    def test_a_hit_is_served_for_an_equal_but_distinct_pairing_dict(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The key compares the pairing by value, so a fresh dict of equal
        contents must reuse the pass — callers rebuild the mapping per call."""
        crew_home, kiro_home = stores
        stem1 = transcript_stem("dashboard:chat-1")
        stem2 = transcript_stem("dashboard:chat-2")
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=32, age_days=40)
        _transcript(crew_home, stem1, size=64, age_days=40)
        _transcript(crew_home, stem2, size=64, age_days=40)

        session_storage.list_units(_multi_index({stem1: "aaaa1111", stem2: "bbbb2222"}))  # prime

        calls = 0
        real = session_storage._scan_raw_uncached

        def counted(sid_for_stem, **kwargs):
            nonlocal calls
            calls += 1
            return real(sid_for_stem, **kwargs)

        monkeypatch.setattr(session_storage, "_scan_raw_uncached", counted)
        # A distinct dict object with equal contents built in the opposite
        # insertion order, so an order-sensitive comparison would also miss.
        session_storage.list_units(_multi_index({stem2: "bbbb2222", stem1: "aaaa1111"}))

        assert calls == 0, "an equal-by-value pairing must be served from the cached pass"

    def test_a_repointed_stem_with_unchanged_length_is_a_miss(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One stem repointed to a different sid, same mapping length, must
        re-enumerate.

        This is the behavioural pin for the failure an ``id()``/``len()``
        memoisation of the pairing would permit: a length-preserving repoint
        served from a pass built under the old pairing. The deterministic
        key-level half lives in
        :meth:`test_scan_key_distinguishes_same_length_mappings` — this test
        alone cannot force CPython to recycle the first dict's ``id()``.
        """
        crew_home, kiro_home = stores
        stem1 = transcript_stem("dashboard:chat-1")
        stem2 = transcript_stem("dashboard:chat-2")
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=32, age_days=40)
        _transcript(crew_home, stem1, size=64, age_days=40)
        _transcript(crew_home, stem2, size=64, age_days=40)

        session_storage.list_units(_multi_index({stem1: "aaaa1111", stem2: "bbbb2222"}))  # prime

        calls = 0
        real = session_storage._scan_raw_uncached

        def counted(sid_for_stem, **kwargs):
            nonlocal calls
            calls += 1
            return real(sid_for_stem, **kwargs)

        monkeypatch.setattr(session_storage, "_scan_raw_uncached", counted)
        # Same keys, same length — only one value repointed.
        session_storage.list_units(_multi_index({stem1: "aaaa1111", stem2: "aaaa1111"}))

        assert calls == 1, "a repointed pairing must not be answered from the old pass"

    def test_a_callers_in_place_edit_after_priming_is_a_miss(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stored key must be a snapshot, not an alias of the caller's dict.

        ``dict(sid_for_stem)`` is the only thing making it one — passing the
        mapping straight through still satisfies the type annotation. Aliased,
        a caller's in-place repoint would mutate the stored key in lockstep,
        so the stale pass would compare EQUAL to the new pairing and be served
        as a hit — the exact failure the key exists to prevent.
        """
        crew_home, kiro_home = stores
        stem1 = transcript_stem("dashboard:chat-1")
        stem2 = transcript_stem("dashboard:chat-2")
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=32, age_days=40)
        _transcript(crew_home, stem1, size=64, age_days=40)
        _transcript(crew_home, stem2, size=64, age_days=40)

        pairing = {stem1: "aaaa1111", stem2: "bbbb2222"}
        session_storage.list_units(SessionIndex(stem_to_sid=pairing))  # prime

        calls = 0
        real = session_storage._scan_raw_uncached

        def counted(sid_for_stem, **kwargs):
            nonlocal calls
            calls += 1
            return real(sid_for_stem, **kwargs)

        monkeypatch.setattr(session_storage, "_scan_raw_uncached", counted)
        pairing[stem2] = "aaaa1111"  # the caller's own dict, edited in place
        session_storage.list_units(SessionIndex(stem_to_sid=pairing))

        assert calls == 1, "an in-place repoint must miss; the stored key may not alias it"

    def test_scan_key_distinguishes_same_length_mappings(self) -> None:
        """Deterministic key-level guard against a length-based pairing key.

        The pairing element must compare unequal for same-length mappings that
        differ in one value — this is what rules out memoising on
        ``id()``+``len()`` (CPython reuses ``id()`` after garbage collection,
        so identity plus length cannot stand in for contents).
        """
        a = session_storage._scan_key({"s1": "aaaa", "s2": "bbbb"})
        b = session_storage._scan_key({"s1": "aaaa", "s2": "aaaa"})
        assert a[2] != b[2], "same-length repointed mappings must produce unequal keys"

    def test_scan_key_is_unhashable_so_it_can_never_be_a_hash_key(self) -> None:
        """The docstring's "never hashed" is enforced by the interpreter.

        A future refactor that hashes the key (a dict-keyed cache, a set of
        keys) must fail loudly at runtime, not silently collide.
        """
        with pytest.raises(TypeError):
            hash(session_storage._scan_key({"a": "1"}))

    def test_scan_key_pairing_equality_matches_the_sorted_tuple_form(self) -> None:
        """``dict(a) == dict(b)`` iff ``tuple(sorted(a.items())) == tuple(sorted(b.items()))``.

        This pins the equivalence the key relies on: the dict snapshot answers the
        equality question identically to the sorted-tuple form it replaced, across
        empty, single-entry, reordered, repointed and disjoint mappings. Compares
        the pairing element directly so the assertion is about the pairing alone —
        a store-path difference cannot mask a pairing disagreement.
        """
        mappings: list[dict[str, str]] = [
            {},
            {"a": "1"},
            {"b": "1"},  # key differs, value identical
            {"a": "1", "b": "2"},
            {"b": "2", "a": "1"},  # same contents, different insertion order
            {"a": "1", "b": "3"},  # one value repointed, same length
            {"a": "2", "b": "1"},  # values swapped
            {"c": "9"},
        ]
        for a in mappings:
            for b in mappings:
                expected = tuple(sorted(a.items())) == tuple(sorted(b.items()))
                got = session_storage._scan_key(a)[2] == session_storage._scan_key(b)[2]
                assert got == expected, f"disagreement on {a!r} vs {b!r}"


class TestBuckets:
    def test_split_on_the_documented_edges(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=10, age_days=1)
        _cli_half(kiro_home, "bbbb2222", log_bytes=20, age_days=20)
        _cli_half(kiro_home, "cccc3333", log_bytes=30, age_days=60)
        _cli_half(kiro_home, "dddd4444", log_bytes=40, age_days=400)

        report = session_storage.measure(_index(), now=_NOW)

        assert {b.label: b.sessions for b in report.buckets} == {
            "under_7d": 1,
            "7_30d": 1,
            "30_90d": 1,
            "over_90d": 1,
        }

    def test_age_uses_the_newest_file_across_both_halves(self, stores: tuple[Path, Path]) -> None:
        """A stale replay log must not age out a session whose transcript is fresh."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=1)

        selected = session_storage.select_reclaimable(
            _index({"aaaa1111": "dashboard_chat-1"}), 30, now=_NOW
        )

        assert selected == []

    def test_threshold_is_inclusive(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "old00000", log_bytes=10, age_days=30)
        _cli_half(kiro_home, "young000", log_bytes=10, age_days=29)

        selected = session_storage.select_reclaimable(_index(), 30, now=_NOW)

        assert [u.uid for u in selected] == ["old00000"]

    def test_negative_threshold_is_refused(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(SessionStorageError):
            session_storage.select_reclaimable(_index(), -1, now=_NOW)


class TestTrashAccounting:
    def test_missing_stores_report_zero(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "absent-crew"))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "absent-kiro"))

        report = session_storage.measure(_index(), now=_NOW)

        assert report.total_bytes == 0
        assert report.total_sessions == 0

    def test_staged_bytes_are_reported_separately_from_reclaimable(
        self, stores: tuple[Path, Path]
    ) -> None:
        _, kiro_home = stores
        size = _cli_half(kiro_home, "aaaa1111", log_bytes=1024, age_days=40)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        report = session_storage.measure(_index(), now=_NOW)

        assert report.reclaimable_sessions == 0
        assert report.trash_batches == 1
        assert report.trash_bytes == size

    def test_a_batch_without_a_manifest_is_not_offered(self, stores: tuple[Path, Path]) -> None:
        orphan = session_storage.trash_root() / "20240101T000000-deadbeef"
        orphan.mkdir(parents=True)
        (orphan / "stray.jsonl").write_bytes(b"x")

        assert session_storage.list_trash() == []

    def test_a_truncated_final_line_does_not_lose_the_batch(
        self, stores: tuple[Path, Path]
    ) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"], reason="manual", index=_index(), now=_NOW
        )
        manifest = session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + '{"uid": "cccc333', encoding="utf-8"
        )

        listed = session_storage.list_trash()

        assert len(listed) == 1
        assert listed[0].sessions == 2

    def test_newest_batch_first(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        first = session_storage.move_to_trash(
            ["aaaa1111"], reason="a", index=_index(), now=_NOW - 10 * _DAY
        )
        second = session_storage.move_to_trash(["bbbb2222"], reason="b", index=_index(), now=_NOW)

        assert [b.batch_id for b in session_storage.list_trash()] == [
            second.batch_id,
            first.batch_id,
        ]


class TestLegacyStems:
    """One session can own more than one transcript filename."""

    def test_a_legacy_stem_keeps_its_session_active(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        # The transcript sits under the pre-migration bare name, not the canonical.
        _transcript(crew_home, "1785861252.833429", size=16, age_days=400)

        index = _multi_index(
            {"slack_1785861252.833429": "aaaa1111", "1785861252.833429": "aaaa1111"},
            active={"aaaa1111"},
        )
        report = session_storage.measure(index, now=_NOW)

        assert report.active_sessions == 1
        assert report.reclaimable_sessions == 0

    def test_both_stems_move_with_their_session(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        _transcript(crew_home, "slack_123.456", size=8, age_days=40)
        _transcript(crew_home, "123.456", size=8, age_days=40)

        index = _multi_index({"slack_123.456": "aaaa1111", "123.456": "aaaa1111"})
        batch = session_storage.move_to_trash(["aaaa1111"], reason="manual", index=index, now=_NOW)

        staged = session_storage.trash_root() / batch.batch_id / "crew"
        assert (staged / "slack_123.456.jsonl").is_file()
        assert (staged / "123.456.jsonl").is_file()
        assert batch.sessions == 1
