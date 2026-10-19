"""Session storage: staging retired sessions into a trash batch.

Both halves move together or not at all; a session resumed while its batch is being staged
is left alone, or has its stranded half recorded in the manifest when the rollback cannot
finish; fresh and mapped sessions are refused; every mutation takes the storage lock; a
session its manifest cannot record is put back; and a batch is identified by its directory.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

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

from kiro_crew import session_storage
from kiro_crew.session_storage import SessionIndex, SessionStorageError


class TestMoveTakesBothHalves:
    def test_both_halves_and_segments_move_together(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=2048, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=300, age_days=40)
        _archive_segment(crew_home, "dashboard_chat-1", "20260730-211852", size=400, age_days=40)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        assert batch.sessions == 1
        cli_root = kiro_home / "sessions" / "cli"
        crew_root = crew_home / "sessions"
        assert not (cli_root / "aaaa1111.json").exists()
        assert not (cli_root / "aaaa1111.jsonl").exists()
        assert not (crew_root / "dashboard_chat-1.jsonl").exists()
        assert not (crew_root / "archive" / "dashboard_chat-1__20260730-211852.jsonl").exists()
        staged = session_storage.trash_root() / batch.batch_id
        assert (staged / "cli" / "aaaa1111.jsonl").is_file()
        assert (staged / "crew" / "dashboard_chat-1.jsonl").is_file()
        assert (staged / "crew" / "archive" / "dashboard_chat-1__20260730-211852.jsonl").is_file()

    def test_halves_with_the_same_filename_do_not_collide(self, stores: tuple[Path, Path]) -> None:
        """A flat batch dir would let one half overwrite the other."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "collide", log_bytes=11, age_days=40)
        _transcript(crew_home, "collide", size=22, age_days=40)

        batch = session_storage.move_to_trash(
            ["collide"], reason="manual", index=_index({"collide": "collide"}), now=_NOW
        )

        staged = session_storage.trash_root() / batch.batch_id
        assert (staged / "cli" / "collide.jsonl").read_bytes() == b"c" * 11
        assert (staged / "crew" / "collide.jsonl").read_bytes() == b"t" * 22

    def test_refuses_a_session_still_mapped(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=99)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=99)

        index = _index({"aaaa1111": "dashboard_chat-1"}, active={"aaaa1111"})
        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(["aaaa1111"], reason="manual", index=index, now=_NOW)

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()

    @pytest.mark.parametrize("uid", ["../escape", "a/b", "", "..", "with space", "x" * 250])
    def test_refuses_ids_that_could_address_another_path(
        self, stores: tuple[Path, Path], uid: str
    ) -> None:
        with pytest.raises(SessionStorageError, match="not a valid session id"):
            session_storage.move_to_trash([uid], reason="manual", index=_index(), now=_NOW)

    def test_manifest_records_every_file_of_the_session(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        _archive_segment(crew_home, "dashboard_chat-1", "20260730-211852", size=8, age_days=40)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="policy",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        lines = (
            (session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME)
            .read_text(encoding="utf-8")
            .splitlines()
        )
        header = json.loads(lines[0])
        entry = json.loads(lines[1])
        assert header["reason"] == "policy"
        assert entry["uid"] == "aaaa1111"
        assert len(entry["files"]) == 4
        origins = {record["origin"] for record in entry["files"]}
        assert str(kiro_home / "sessions" / "cli" / "aaaa1111.json") in origins
        assert str(crew_home / "sessions" / "dashboard_chat-1.jsonl") in origins


class TestASessionResumedWhileStagingIsLeftAlone:
    """The authority checks run before the move loop, so they describe one instant.

    A batch is not instant, and a session can be resumed between being certified
    retired and being reached by the loop. The loop catches that from the stat it
    already takes for the manifest: a source modified since the batch was
    validated cannot be the idle file that qualified.
    """

    def _touch_when(self, target: Path, *, on: Path) -> Callable[[Path, Path], None]:
        """Move normally, but write to *target* the first time *on* is moved.

        Stands in for the real event: something resumes the session and appends to
        its replay log while the batch is part-way through being staged.
        """
        real = session_storage._move_file
        state = {"done": False}

        def _move(src: Path, dst: Path) -> None:
            real(src, dst)
            if not state["done"] and src == on:
                state["done"] = True
                with target.open("ab") as fh:
                    fh.write(b"resumed")
                # flake-ok: pre-existing clock read dating a resumed file 5 s ahead; moved verbatim
                future = time.time() + 5
                os.utime(target, (future, future))

        return _move

    def test_the_revived_session_stays_in_place_and_is_named(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=400)
        _cli_half(kiro_home, "bbbb2222", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-2", size=16, age_days=400)

        # The first session's cli half moves, and that is when the SECOND session's
        # transcript is written to — the ordering a real resume would produce.
        victim = crew_home / "sessions" / "dashboard_chat-2.jsonl"
        trigger = kiro_home / "sessions" / "cli" / "aaaa1111.jsonl"
        monkeypatch.setattr(session_storage, "_move_file", self._touch_when(victim, on=trigger))

        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1", "bbbb2222": "dashboard_chat-2"}),
            now=_NOW,
        )

        assert batch.sessions == 1
        assert batch.revived == ("bbbb2222",)
        # Still resumable, both halves, exactly where they were.
        assert victim.is_file()
        assert (kiro_home / "sessions" / "cli" / "bbbb2222.jsonl").is_file()
        # And nothing of it is in the batch.
        staged = session_storage.trash_root() / batch.batch_id
        assert not (staged / "crew" / "dashboard_chat-2.jsonl").exists()
        assert not (staged / "cli" / "bbbb2222.jsonl").exists()
        # The session that was genuinely idle still moved.
        assert (staged / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_half_that_already_moved_is_put_back(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revival found on a LATER file must not leave the session split."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=400)
        transcript = crew_home / "sessions" / "dashboard_chat-1.jsonl"

        cli_log = kiro_home / "sessions" / "cli" / "aaaa1111.jsonl"
        monkeypatch.setattr(session_storage, "_move_file", self._touch_when(transcript, on=cli_log))

        with pytest.raises(SessionStorageError, match="resumed while being staged"):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        # The half that had already moved is back where it belongs.
        assert cli_log.is_file()
        assert transcript.is_file()

    def test_a_resume_between_the_refresh_and_the_loop_is_caught(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The anchor is taken before the scan, so this window is covered too.

        An anchor stamped after the authority checks would miss this: the write
        lands while ``refresh`` runs, so its mtime is older than any later stamp
        and the session would be staged while live. Driving it through *refresh*
        puts the write exactly where a resume during the scan would land.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=400)
        victim = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        mapping = {"aaaa1111": "dashboard_chat-1"}

        # The module's own wall clock, moved only by the resume below, so "newer
        # than the entry anchor" is arithmetic: two real reads milliseconds apart can
        # return the SAME float on a 15.6 ms Windows tick.
        wall = [_NOW]
        monkeypatch.setattr(
            session_storage,
            "time",
            SimpleNamespace(
                time=lambda: wall[0],
                monotonic=time.monotonic,
                gmtime=time.gmtime,
                strftime=time.strftime,
            ),
        )
        resumed: list[float] = []

        def _refresh_and_resume() -> SessionIndex:
            # A real resume writes at the current instant, NOT in the future, and
            # that is the whole point of this test: an mtime of "now" is newer than
            # an anchor taken at entry and OLDER than one taken after these checks,
            # so only the early anchor catches it. Once, on the read before the
            # loop: a second write from the loop's own re-read would land after any
            # stamp taken before the loop, and catch a late anchor on its behalf.
            if not resumed:
                wall[0] += 1
                with victim.open("ab") as fh:
                    fh.write(b"resumed")
                os.utime(victim, (wall[0], wall[0]))
                resumed.append(wall[0])
                # Time moves on: every later reading of the clock is past the write.
                wall[0] += 1
            # Deliberately reports the session as retired: this pins the mtime
            # guard, not the index re-read that already covers a MAPPED session.
            return _index(mapping)

        with pytest.raises(SessionStorageError, match="resumed while being staged"):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index(mapping),
                now=_NOW,
                refresh=_refresh_and_resume,
            )

        # The write it caught sits strictly inside the window only the early anchor
        # covers: after the entry reading, before every later one.
        assert resumed == [_NOW + 1]
        assert victim.is_file()
        assert _NOW < victim.stat().st_mtime < wall[0]
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_revival_whose_rollback_cannot_finish_is_not_claimed_as_left_alone(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting revival is a claim, so it must not outlive a failed rollback.

        Putting a file back is deliberately non-overwriting, so a resume that
        recreates an origin makes the rollback incomplete: the staged copy stays in
        the batch, unlisted by its manifest. Saying the session was "left in place"
        would then be false, and restore could not see the orphan either.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=400)
        cli_log = kiro_home / "sessions" / "cli" / "aaaa1111.jsonl"
        transcript = crew_home / "sessions" / "dashboard_chat-1.jsonl"

        real = session_storage._move_file
        state = {"done": False}

        def _resume_recreating_the_origin(src: Path, dst: Path) -> None:
            real(src, dst)
            if not state["done"] and src == cli_log:
                state["done"] = True
                # The resume recreates the half that just moved AND writes the
                # other one — the ordering that makes the rollback decline.
                cli_log.write_bytes(b"live again")
                with transcript.open("ab") as fh:
                    fh.write(b"resumed")
                # flake-ok: pre-existing clock read dating a resumed file 5 s ahead; moved verbatim
                future = time.time() + 5
                for path in (cli_log, transcript):
                    os.utime(path, (future, future))

        monkeypatch.setattr(session_storage, "_move_file", _resume_recreating_the_origin)

        with pytest.raises(SessionStorageError, match="could not be fully put back"):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        # The live session keeps what the resume wrote — the rollback must never
        # overwrite it with the staged copy.
        assert cli_log.read_bytes() == b"live again"
        assert transcript.is_file()

        # And whatever could not be put back is IN the manifest, because an
        # unlisted file in a batch is reachable by neither restore nor empty.
        batches = list(session_storage.trash_root().iterdir())
        assert len(batches) == 1
        entries = [
            json.loads(line)
            for line in (batches[0] / session_storage.MANIFEST_NAME).read_text().splitlines()
            if line.strip()
        ]
        staged_records = [e for e in entries if e.get("uid") == "aaaa1111"]
        assert staged_records, "the stranded half was left out of the manifest"
        rels = {record["rel"] for record in staged_records[0]["files"]}
        assert rels, "an entry with no files cannot be restored"
        for rel in rels:
            assert (batches[0] / rel).is_file()

    def test_an_untouched_batch_reports_nothing_revived(self, stores: tuple[Path, Path]) -> None:
        """The guard must not fire on the ordinary case, or every reclaim breaks."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=400)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        assert batch.sessions == 1
        assert batch.revived == ()


class TestAReadOnlyResumeIsCaughtByTheIndexReRead:
    """mtime sees writes, so a resume that only READS needs a second signal.

    Resuming a session reads its old transcript to rebuild history and records the
    turn that follows under a newly mapped sid, without rewriting that transcript.
    Every file therefore keeps the mtime that qualified it and passes the check
    above — so the loop consults the index too, at the same per-session cadence,
    where the resume IS visible: it is mapped.
    """

    def test_a_resume_that_only_reads_the_transcript_is_left_in_place(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case the mtime guard cannot see: nothing is written at all."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=16, age_days=400)
        _cli_half(kiro_home, "bbbb2222", log_bytes=16, age_days=400)
        _transcript(crew_home, "dashboard_chat-2", size=16, age_days=400)

        pairs = {"aaaa1111": "dashboard_chat-1", "bbbb2222": "dashboard_chat-2"}
        retired = _index(pairs)
        resumed = {"yet": False}
        real = session_storage._move_file

        def _resume_the_second_session(src: Path, dst: Path) -> None:
            # Mapped part-way through the batch, and nothing written: no utime, no
            # append, no recreated origin. This is the whole point of the case.
            real(src, dst)
            resumed["yet"] = True

        monkeypatch.setattr(session_storage, "_move_file", _resume_the_second_session)

        def _refresh() -> SessionIndex:
            return retired if not resumed["yet"] else _index(pairs, active={"bbbb2222"})

        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"],
            reason="manual",
            index=retired,
            now=_NOW,
            refresh=_refresh,
        )

        assert batch.revived == ("bbbb2222",)
        assert batch.sessions == 1
        # Left in place means every half, and nothing of it staged: the loop reaches
        # this session before touching any of its files, so there is no rollback.
        assert (kiro_home / "sessions" / "cli" / "bbbb2222.jsonl").is_file()
        assert (kiro_home / "sessions" / "cli" / "bbbb2222.json").is_file()
        assert (crew_home / "sessions" / "dashboard_chat-2.jsonl").is_file()
        staged = list((crew_home / "sessions" / "trash").rglob("*bbbb2222*"))
        assert staged == [], f"a session left in place has files in the batch: {staged}"

    def test_the_index_is_consulted_before_every_session(self, stores: tuple[Path, Path]) -> None:
        """Once before the loop is not enough — that is the gap being closed.

        Pinned as a count rather than a behaviour because the behaviour it buys is
        already covered above; what a future change could quietly drop is the
        CADENCE, and then only a batch resumed at exactly the wrong moment would
        notice.
        """
        crew_home, kiro_home = stores
        for sid, stem in (("aaaa1111", "1"), ("bbbb2222", "2"), ("cccc3333", "3")):
            _cli_half(kiro_home, sid, log_bytes=16, age_days=400)
            _transcript(crew_home, f"dashboard_chat-{stem}", size=16, age_days=400)

        retired = _index(
            {
                "aaaa1111": "dashboard_chat-1",
                "bbbb2222": "dashboard_chat-2",
                "cccc3333": "dashboard_chat-3",
            }
        )
        calls = {"n": 0}

        def _refresh() -> SessionIndex:
            calls["n"] += 1
            return retired

        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222", "cccc3333"],
            reason="manual",
            index=retired,
            now=_NOW,
            refresh=_refresh,
        )

        assert batch.sessions == 3
        # One before the authority checks, then one per session reached.
        assert calls["n"] == 4

    def test_an_unchanged_index_is_not_re_derived(self, stores: tuple[Path, Path]) -> None:
        """The per-session re-read has to be affordable at the selection cap.

        ``active_stems`` rebuilds a frozenset over the whole map every time it is
        read, so re-deriving it per session is what would make a six-figure
        selection unpayable. A caller says "nothing has moved" by returning the same
        object, and that must cost one identity comparison rather than a rebuild.
        """
        crew_home, kiro_home = stores
        for sid, stem in (("aaaa1111", "1"), ("bbbb2222", "2"), ("cccc3333", "3")):
            _cli_half(kiro_home, sid, log_bytes=16, age_days=400)
            _transcript(crew_home, f"dashboard_chat-{stem}", size=16, age_days=400)

        derived: list[str] = []

        class _CountingIndex(SessionIndex):
            """Records every read of the set the union would rebuild."""

            @property
            def active_stems(self) -> frozenset[str]:
                derived.append("active_stems")
                return super().active_stems

        retired = _CountingIndex(
            stem_to_sid={
                "dashboard_chat-1": "aaaa1111",
                "dashboard_chat-2": "bbbb2222",
                "dashboard_chat-3": "cccc3333",
            }
        )

        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222", "cccc3333"],
            reason="manual",
            index=_index(
                {
                    "aaaa1111": "dashboard_chat-1",
                    "bbbb2222": "dashboard_chat-2",
                    "cccc3333": "dashboard_chat-3",
                }
            ),
            now=_NOW,
            refresh=lambda: retired,
        )

        assert batch.sessions == 3
        # Once, for the union before the authority checks. Three more would mean
        # every session paid for a rebuild of a set that had not changed.
        assert derived == ["active_stems"]

    def test_a_re_read_that_starts_failing_keeps_the_view_it_last_read(
        self, stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A lost re-read costs extra protection, not the protection already read.

        Every re-read only widens the live sets, so failing to take one leaves the
        guard exactly as strong as the last successful read made it. Abandoning a
        batch that has already moved files would be the worse trade — and a refresh
        broken from the start still fails closed, before anything moves.
        """
        crew_home, kiro_home = stores
        for sid, stem in (("aaaa1111", "1"), ("bbbb2222", "2"), ("cccc3333", "3")):
            _cli_half(kiro_home, sid, log_bytes=16, age_days=400)
            _transcript(crew_home, f"dashboard_chat-{stem}", size=16, age_days=400)

        pairs = {
            "aaaa1111": "dashboard_chat-1",
            "bbbb2222": "dashboard_chat-2",
            "cccc3333": "dashboard_chat-3",
        }
        calls = {"n": 0}

        def _refresh() -> SessionIndex:
            calls["n"] += 1
            if calls["n"] == 1:
                # Before the authority checks: reports every session retired, so
                # the batch is allowed to start.
                return _index(pairs)
            if calls["n"] == 2:
                # Reached the first session, and the third has just been resumed.
                return _index(pairs, active={"cccc3333"})
            raise OSError("the map went away")

        with caplog.at_level(logging.WARNING, logger=session_storage.__name__):
            batch = session_storage.move_to_trash(
                ["aaaa1111", "bbbb2222", "cccc3333"],
                reason="manual",
                index=_index(pairs),
                now=_NOW,
                refresh=_refresh,
            )

        # The two the failing re-reads could not speak for still moved.
        assert batch.sessions == 2
        # And the one the last GOOD read named is still protected, which is the
        # difference between retaining the view and discarding it.
        assert batch.revived == ("cccc3333",)
        assert (kiro_home / "sessions" / "cli" / "cccc3333.jsonl").is_file()
        warnings = [r for r in caplog.records if "could not re-read" in r.getMessage()]
        # Once for the batch, not once per session: at the selection cap a
        # per-session line is six figures of identical warnings.
        assert len(warnings) == 1


class TestPartialMoveRollsBack:
    def test_a_session_that_cannot_move_wholly_is_left_alone(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-moved session is invisible: emptying would destroy the staged half."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        real_move = session_storage._move_file
        calls = {"n": 0}

        def flaky(src: Path, dst: Path) -> None:
            calls["n"] += 1
            # Fail on the transcript, after the replay half has already moved.
            if calls["n"] == 3:
                raise OSError("simulated cross-device failure")
            real_move(src, dst)

        monkeypatch.setattr(session_storage, "_move_file", flaky)

        with pytest.raises(SessionStorageError, match="none of the selected"):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        # Everything is back where it started; nothing is left staged.
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.json").is_file()
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
        assert session_storage.list_trash() == []


class TestSidecarFiles:
    def test_an_unrecognised_sidecar_moves_with_its_session(
        self, stores: tuple[Path, Path]
    ) -> None:
        """kiro-cli owns this layout; a file we do not know must not be orphaned."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        sidecar = kiro_home / "sessions" / "cli" / "aaaa1111.lock"
        sidecar.write_bytes(b"lock")
        # Age it with the rest of its session. The unit's age comes from the
        # .json/.jsonl pair alone, but the move loop's revival check stats EVERY
        # file it stages against a wall-clock instant, so a sidecar left stamped
        # "now" is a file written after this batch was certified -- the state the
        # check exists to refuse. Leaving it fresh passed only on the microseconds
        # between this write and that instant, and a backward CLOCK_REALTIME step
        # inside that window inverted it: "all 1 selected session(s) were resumed
        # while being staged". An idle session's sidecar is as old as its logs.
        aged = _NOW - 40 * _DAY
        os.utime(sidecar, (aged, aged))

        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        assert not sidecar.exists()
        staged = session_storage.trash_root() / batch.batch_id / "cli" / "aaaa1111.lock"
        assert staged.read_bytes() == b"lock"
        assert session_storage.restore(batch.batch_id) == 1
        assert sidecar.read_bytes() == b"lock"


class TestFreshSessionsAreProtected:
    """The session map is not a complete registry of live sessions.

    A subagent run creates a kiro-cli session that was never mapped, so mapping
    alone would let a threshold of 0 reclaim a conversation running right now.
    """

    def test_a_threshold_of_zero_cannot_take_a_fresh_session(
        self, stores: tuple[Path, Path]
    ) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "livenow0", log_bytes=16, age_days=0.01)
        _cli_half(kiro_home, "oldone00", log_bytes=16, age_days=40)

        selected = session_storage.select_reclaimable(_index(), 0, now=_NOW)

        assert [u.uid for u in selected] == ["oldone00"]

    def test_passing_a_fresh_id_directly_is_refused(self, stores: tuple[Path, Path]) -> None:
        """The move is the chokepoint; the selection helper can be bypassed."""
        _, kiro_home = stores
        _cli_half(kiro_home, "livenow0", log_bytes=16, age_days=0.01)

        with pytest.raises(SessionStorageError, match="touched in the last"):
            session_storage.move_to_trash(["livenow0"], reason="manual", index=_index(), now=_NOW)

        assert (kiro_home / "sessions" / "cli" / "livenow0.jsonl").is_file()

    def test_a_fresh_session_is_not_reported_as_reclaimable(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Reporting bytes no threshold can move would be a false promise."""
        _, kiro_home = stores
        size = _cli_half(kiro_home, "livenow0", log_bytes=1024, age_days=0.01)

        report = session_storage.measure(_index(), now=_NOW)

        assert report.total_sessions == 1
        assert report.total_bytes == size
        assert report.reclaimable_sessions == 0


class TestMutationsAreSerialized:
    """Interleaved reclaims can put one half of a session in each batch."""

    def _record_lock(self, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
        taken: list[bool] = []
        real = session_storage.platform_compat.file_lock

        @contextlib.contextmanager
        def spy(fd, *, exclusive=True, required=False):
            taken.append(exclusive)
            with real(fd, exclusive=exclusive, required=required):
                yield

        monkeypatch.setattr(session_storage.platform_compat, "file_lock", spy)
        return taken

    def test_move_takes_an_exclusive_lock(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        taken = self._record_lock(monkeypatch)

        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        assert taken == [True]

    def test_restore_and_empty_take_the_lock(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        taken = self._record_lock(monkeypatch)

        session_storage.restore(batch.batch_id)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)
        session_storage.empty_trash(None)

        assert taken == [True, True, True]


class TestManifestPersistenceFailure:
    def test_a_session_that_cannot_be_recorded_is_put_back(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moved-but-unrecorded is worse than never moved: gone and unrestorable."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)

        def full_disk(handle, entry):
            raise OSError("simulated ENOSPC")

        monkeypatch.setattr(session_storage, "_append_entry", full_disk)

        with pytest.raises(SessionStorageError, match="none of the selected"):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.json").is_file()
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
        assert session_storage.list_trash() == []

    def test_rewrite_fsyncs_before_publishing_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash after publish must not expose manifest bytes still only in cache."""
        from kiro_crew import atomic_write as atomic_write_module

        batch = tmp_path / "batch"
        batch.mkdir()
        events: list[str] = []
        real_fsync = atomic_write_module.os.fsync
        real_replace = atomic_write_module.replace_with_retry

        def tracking_fsync(fd: int) -> None:
            events.append("fsync")
            real_fsync(fd)

        def tracking_replace(src: str | Path, dst: str | Path) -> None:
            events.append("replace")
            real_replace(src, dst)

        monkeypatch.setattr(atomic_write_module.os, "fsync", tracking_fsync)
        monkeypatch.setattr(atomic_write_module, "replace_with_retry", tracking_replace)
        header = {"schema": session_storage.MANIFEST_SCHEMA, "batch_id": "batch"}
        entries = [{"uid": "aaaa1111", "files": []}]

        session_storage._rewrite_manifest(batch, header, entries)

        assert events.index("fsync") < events.index("replace")
        manifest = batch / session_storage.MANIFEST_NAME
        assert [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()] == [
            header,
            *entries,
        ]


class TestBatchIdentityIsTheDirectory:
    def test_a_header_naming_another_batch_is_not_offered(self, stores: tuple[Path, Path]) -> None:
        """Trusting the header would let a targeted empty delete the wrong batch."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        victim = session_storage.move_to_trash(
            ["aaaa1111"], reason="keep", index=_index(), now=_NOW - _DAY
        )
        attacker = session_storage.move_to_trash(
            ["bbbb2222"], reason="tampered", index=_index(), now=_NOW
        )
        # Point the second batch's header at the first.
        manifest = session_storage.trash_root() / attacker.batch_id / "manifest.jsonl"
        lines = manifest.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        header["batch_id"] = victim.batch_id
        manifest.write_text("\n".join([json.dumps(header)] + lines[1:]) + "\n", encoding="utf-8")

        listed = {b.batch_id for b in session_storage.list_trash()}

        # The tampered batch is withheld, and it cannot masquerade as the other.
        assert listed == {victim.batch_id}

    def test_listed_ids_always_match_their_directory(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        listed = session_storage.list_trash()

        assert [b.batch_id for b in listed] == [batch.batch_id]
        assert (session_storage.trash_root() / listed[0].batch_id).is_dir()
