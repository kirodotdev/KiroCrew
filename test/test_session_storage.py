"""Tests for session storage measurement and the trash/restore cycle.

Both stores are addressed through their real resolvers by pointing
``KIROCREW_HOME`` and ``KIRO_HOME`` at temp directories, rather than patching the
module's bound names, so the tests exercise the same path resolution the product
uses and a change to where either store lives fails here.

The invariant these tests exist to protect: a session's halves are always
reclaimed and restored TOGETHER. Half a session is worse than either extreme — it
either lists without resuming, or resumes without history.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

from kiro_crew import session_storage
from kiro_crew.config import paths
from kiro_crew.history import transcript_stem
from kiro_crew.session_storage import SessionIndex, SessionStorageError

_NOW = 1_700_000_000.0
_DAY = 86400.0


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point both stores at temp dirs; return (crew data home, kiro home).

    The kiro home is nested INSIDE the data home because that is what
    :func:`reclaim_block_reason` requires of an isolated instance: a store outside
    the data home may be shared with an instance whose map this one cannot read, so
    reclaiming from a sibling layout is refused by design.
    """
    crew_home = tmp_path / "crew"
    kiro_home = crew_home / "kiro"
    (crew_home / "sessions" / "archive").mkdir(parents=True)
    (kiro_home / "sessions" / "cli").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    return crew_home, kiro_home


def _cli_half(kiro_home: Path, sid: str, *, log_bytes: int, age_days: float) -> int:
    root = kiro_home / "sessions" / "cli"
    mtime = _NOW - age_days * _DAY
    total = 0
    for suffix, payload in ((".json", b"{}"), (".jsonl", b"c" * log_bytes)):
        path = root / f"{sid}{suffix}"
        path.write_bytes(payload)
        os.utime(path, (mtime, mtime))
        total += len(payload)
    return total


def _transcript(crew_home: Path, stem: str, *, size: int, age_days: float) -> int:
    path = crew_home / "sessions" / f"{stem}.jsonl"
    path.write_bytes(b"t" * size)
    mtime = _NOW - age_days * _DAY
    os.utime(path, (mtime, mtime))
    return size


def _archive_segment(crew_home: Path, stem: str, stamp: str, *, size: int, age_days: float) -> int:
    path = crew_home / "sessions" / "archive" / f"{stem}__{stamp}.jsonl"
    path.write_bytes(b"a" * size)
    mtime = _NOW - age_days * _DAY
    os.utime(path, (mtime, mtime))
    return size


def _index(pairs: dict[str, str] | None = None, active: set[str] | None = None) -> SessionIndex:
    """Build an index from a readable {sid: stem} mapping.

    The production shape is stem -> sid (one session can own several stems), but
    tests read better keyed on the session, so this inverts for them.
    """
    stem_to_sid = {stem: sid for sid, stem in (pairs or {}).items()}
    return SessionIndex(stem_to_sid=stem_to_sid, active_sids=frozenset(active or set()))


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


class TestRestoreIsAllOrNothing:
    def test_restores_every_half(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=64, age_days=40)
        _archive_segment(crew_home, "dashboard_chat-1", "20260730-211852", size=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        restored = session_storage.restore(batch.batch_id)

        assert restored == 1
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").read_bytes() == b"c" * 32
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").read_bytes() == b"t" * 64
        seg = crew_home / "sessions" / "archive" / "dashboard_chat-1__20260730-211852.jsonl"
        assert seg.read_bytes() == b"a" * 16
        assert session_storage.list_trash() == []

    def test_an_occupied_half_leaves_the_whole_session_staged(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Restoring only the free half would recreate a half-session."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        # The transcript came back on its own while the session sat in the trash.
        (crew_home / "sessions" / "dashboard_chat-1.jsonl").write_bytes(b"NEWER")

        restored = session_storage.restore(batch.batch_id)

        assert restored == 0
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").read_bytes() == b"NEWER"
        # The replay half must NOT have been put back on its own.
        assert not (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").exists()
        assert session_storage.list_trash()[0].sessions == 1

    def test_partial_restore_by_uid_keeps_the_rest_staged(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"], reason="manual", index=_index(), now=_NOW
        )

        restored = session_storage.restore(batch.batch_id, ["aaaa1111"])

        assert restored == 1
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert not (kiro_home / "sessions" / "cli" / "bbbb2222.jsonl").exists()
        assert session_storage.list_trash()[0].sessions == 1

    def test_an_origin_naming_another_session_is_refused(self, stores: tuple[Path, Path]) -> None:
        """Both paths are inside a session store, so containment cannot catch it."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        victim = kiro_home / "sessions" / "cli" / "bbbb2222.jsonl"
        batch_dir = session_storage.trash_root() / batch.batch_id
        manifest = batch_dir / session_storage.MANIFEST_NAME
        lines = manifest.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["files"][0]["origin"] = str(victim)
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n")

        restored = session_storage.restore(batch.batch_id)

        # The harm this prevents: the other session's path is never written to.
        # Deriving the origin from the staged path is what guarantees it; the
        # agreement check then turns a disagreeing manifest into a refusal rather
        # than a silently ignored field.
        assert not victim.exists()
        assert restored == 0

    def test_an_exclusive_move_never_replaces_an_occupied_destination(self, tmp_path: Path) -> None:
        """The no-clobber guarantee itself, independent of any restore path."""
        src = tmp_path / "src.jsonl"
        src.write_bytes(b"staged")
        taken = tmp_path / "taken.jsonl"
        taken.write_bytes(b"newer generation")
        free = tmp_path / "free.jsonl"

        assert session_storage._move_file_exclusive(src, taken) is False
        assert taken.read_bytes() == b"newer generation"
        assert src.is_file(), "a refused move must not consume the staged file"

        assert session_storage._move_file_exclusive(src, free) is True
        assert free.read_bytes() == b"staged"
        assert not src.exists()

    def test_losing_the_origin_race_retains_the_entry(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused move must leave the session staged and still restorable."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        calls: list[tuple[Path, Path]] = []

        def occupied(src: Path, dst: Path) -> bool:
            # Stand in for the gateway recreating the session after the preflight.
            calls.append((src, dst))
            return False

        monkeypatch.setattr(session_storage, "_move_file_exclusive", occupied)

        assert session_storage.restore(batch.batch_id) == 0
        # Proves the move loop was reached, so this pins the lost-race branch and
        # not an earlier refusal that would return 0 for a different reason.
        assert calls, "restore never attempted the move"
        # The batch is intact, so the user can retry once the origin frees up.
        assert [b.batch_id for b in session_storage.list_trash()] == [batch.batch_id]
        staged = session_storage.trash_root() / batch.batch_id / "cli" / "aaaa1111.jsonl"
        assert staged.is_file()

    def test_a_failed_restore_stays_retryable(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-restored session would be split AND wedged against every retry."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        real_move = session_storage._move_file_exclusive
        calls = {"n": 0}

        def flaky(src: Path, dst: Path) -> bool:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated failure mid-restore")
            return real_move(src, dst)

        monkeypatch.setattr(session_storage, "_move_file_exclusive", flaky)
        assert session_storage.restore(batch.batch_id) == 0

        # The staged files are all back in the batch, so a later retry succeeds.
        monkeypatch.setattr(session_storage, "_move_file_exclusive", real_move)
        assert session_storage.restore(batch.batch_id) == 1
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
        assert session_storage.list_trash() == []

    def test_restore_never_deletes_a_file_the_manifest_omits(
        self, stores: tuple[Path, Path]
    ) -> None:
        """An interrupted move leaves a staged file nothing lists — the only copy."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        # Simulate a crash between moving a file in and appending its manifest line.
        orphan = staged / "cli" / "cccc3333.jsonl"
        orphan.write_bytes(b"ONLY COPY")

        assert session_storage.restore(batch.batch_id) == 1

        assert orphan.read_bytes() == b"ONLY COPY"
        assert staged.is_dir()

    def test_an_unstattable_file_aborts_the_whole_session(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that cannot be sized cannot be recorded, so it cannot be skipped.

        Staging the rest would commit a manifest that omits it — exactly the split
        the rollback exists to prevent.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        target = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        real_size = session_storage._file_size

        def flaky_size(path: Path) -> int:
            if path == target:
                raise OSError("simulated transient stat failure")
            return real_size(path)

        monkeypatch.setattr(session_storage, "_file_size", flaky_size)

        with pytest.raises(SessionStorageError, match="none of the selected"):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert target.is_file()
        assert session_storage.list_trash() == []

    def test_a_malformed_manifest_record_blocks_its_session(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Restoring the readable files would leave the rest staged and unreferenced."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"].append("not-an-object")
        manifest.write_text(f"{lines[0]}\n{json.dumps(entry)}\n", encoding="utf-8")

        restored = session_storage.restore(batch.batch_id)

        assert restored == 0
        # Nothing was put back, so nothing is split.
        assert not (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").exists()
        assert not (crew_home / "sessions" / "dashboard_chat-1.jsonl").exists()
        assert session_storage.list_trash()[0].sessions == 1

    def test_unknown_batch_is_refused(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(SessionStorageError, match="no restorable batch"):
            session_storage.restore("20240101T000000-deadbeef")


class TestManifestIsUntrusted:
    """The manifest lives in an agent-writable tree, so restore must not trust it."""

    def _staged(self, kiro_home: Path) -> tuple[str, Path]:
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        return batch.batch_id, session_storage.trash_root() / batch.batch_id

    def test_an_absolute_rel_cannot_pick_up_a_file_outside_the_batch(
        self, stores: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """`Path(batch) / "/etc/x"` is `/etc/x` — joining an absolute discards the base."""
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        secret = tmp_path / "credentials"
        secret.write_bytes(b"SECRET")
        manifest = batch / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        header = lines[0]
        tampered = json.dumps(
            {
                "uid": "aaaa1111",
                "files": [
                    {
                        "rel": str(secret),
                        "origin": str(kiro_home / "sessions" / "cli" / "stolen.jsonl"),
                        "bytes": 6,
                    }
                ],
            }
        )
        manifest.write_text(f"{header}\n{tampered}\n", encoding="utf-8")

        restored = session_storage.restore(batch_id)

        assert restored == 0
        assert secret.read_bytes() == b"SECRET"
        assert not (kiro_home / "sessions" / "cli" / "stolen.jsonl").exists()

    def test_a_traversing_rel_is_refused(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        manifest = batch / session_storage.MANIFEST_NAME
        header = manifest.read_text(encoding="utf-8").splitlines()[0]
        tampered = json.dumps(
            {
                "uid": "aaaa1111",
                "files": [
                    {
                        "rel": "../../../etc/hosts",
                        "origin": str(kiro_home / "sessions" / "cli" / "x.jsonl"),
                        "bytes": 1,
                    }
                ],
            }
        )
        manifest.write_text(f"{header}\n{tampered}\n", encoding="utf-8")

        assert session_storage.restore(batch_id) == 0

    def test_an_origin_outside_the_session_stores_is_refused(
        self, stores: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Restore writes to origin, so an unconstrained origin is arbitrary write."""
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        target = tmp_path / "planted.jsonl"
        manifest = batch / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"] = [
            {"rel": record["rel"], "origin": str(target), "bytes": record["bytes"]}
            for record in entry["files"]
        ]
        manifest.write_text(f"{lines[0]}\n{json.dumps(entry)}\n", encoding="utf-8")

        restored = session_storage.restore(batch_id)

        assert restored == 0
        assert not target.exists()


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


class TestSharedStoreRefusal:
    """An isolated data home over a shared replay store cannot see who is live."""

    def test_reclaim_is_refused_when_only_the_data_home_is_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew_home = tmp_path / "crew"
        (crew_home / "sessions").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.delenv("KIRO_HOME", raising=False)

        assert session_storage.reclaim_block_reason() != ""
        with pytest.raises(SessionStorageError, match="sits outside it"):
            session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

    def test_isolating_both_homes_is_allowed(self, stores: tuple[Path, Path]) -> None:
        """The fixture sets both, which is the safe configuration."""
        assert session_storage.reclaim_block_reason() == ""

    def test_isolating_neither_home_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        # The co-tenant check reads the pod root, which is real host state — point it
        # at an empty dir so this asserts the guard and not the developer's machine.
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        # Pin the default home rather than clearing the memo. Clearing it makes the
        # next data_home() RE-RESOLVE, which on a real machine initializes or
        # migrates the operator's actual data home — and leaves that resolution
        # memoized for every later test in the same worker.
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        assert session_storage.reclaim_block_reason() == ""

    def test_a_pre_migration_legacy_home_is_not_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An install that has not migrated yet must still be able to reclaim.

        The legacy home is a DEFAULT, not an isolated instance; treating it as one
        refused every pre-migration install — including the machine this feature
        was measured on.
        """
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setattr(paths, "_resolved_home", paths.legacy_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        assert session_storage.reclaim_block_reason() == ""

    def test_a_custom_store_shared_by_two_isolated_instances_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two pods pointed at one custom KIRO_HOME see neither map."""
        crew_home = tmp_path / "pod-a"
        crew_home.mkdir()
        shared_store = tmp_path / "shared-kiro"
        shared_store.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.setenv("KIRO_HOME", str(shared_store))

        # Not the DEFAULT store, so a default-location test would pass it — and
        # that is the arrangement where sharing is least visible.
        assert session_storage.reclaim_block_reason() != ""

    def test_a_store_inside_the_isolated_data_home_may_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dedicated private store is genuinely isolated."""
        crew_home = tmp_path / "pod-b"
        crew_home.mkdir()
        own_store = crew_home / "kiro"
        own_store.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.setenv("KIRO_HOME", str(own_store))

        assert session_storage.reclaim_block_reason() == ""

    def test_an_unreadable_batch_is_not_emptied(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scan that gives up early must not read as "nothing unaccounted for"."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        def failing_walk(top, onerror=None, **kwargs):
            if onerror is not None:
                onerror(OSError(5, "simulated read failure"))
            return iter(())

        monkeypatch.setattr(session_storage.os, "walk", failing_walk)

        assert session_storage.empty_trash([batch.batch_id]) == 0
        # The batch survives, so nothing was destroyed on an unverifiable scan.
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_a_staged_symlink_is_not_restored(self, stores: tuple[Path, Path]) -> None:
        """is_file() follows links, so a link would be put back as session data.

        The link points INSIDE the batch on purpose: `_staged_path` resolves the
        manifest's `rel` and already refuses one that escapes, so only a link
        resolving within the batch reaches this check.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        batch_dir = session_storage.trash_root() / batch.batch_id
        staged = next(
            p
            for p in batch_dir.rglob("*")
            if p.is_file() and p.name != session_storage.MANIFEST_NAME
        )
        staged.unlink()
        try:
            staged.symlink_to(batch_dir / session_storage.MANIFEST_NAME)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        assert session_storage.restore(batch.batch_id) == 0
        # The origin is still absent rather than occupied by a dangling link.
        assert not (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").exists()

    def test_a_pod_sharing_the_default_store_blocks_the_default_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mirror case: a pod reads THIS store while keeping its own map."""
        pod_root = tmp_path / "pods"
        (pod_root / "wt-feature").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        reason = session_storage.reclaim_block_reason()
        assert "share this kiro-cli session store" in reason
        assert "wt-feature" in reason

    def test_no_pods_leaves_the_default_instance_able_to_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A normal single install must not be refused by the co-tenant check."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "no-pods-here"))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        assert session_storage.reclaim_block_reason() == ""

    def test_a_rollback_does_not_overwrite_a_recreated_origin(self, tmp_path: Path) -> None:
        """A rollback runs after a failure, so the origin may be back and newer."""
        landed = tmp_path / "landed.jsonl"
        landed.write_bytes(b"staged copy")
        origin = tmp_path / "origin.jsonl"
        origin.write_bytes(b"newer generation")

        session_storage._rollback([(landed, origin)])

        assert origin.read_bytes() == b"newer generation"
        assert landed.is_file(), "the staged copy must stay recoverable"

    def test_a_trash_root_linked_inside_the_home_reaches_nothing(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The anchor alone permits this: the link points INSIDE the data home.

        Pointing the root at the live archive tree satisfies both the data-home
        anchor and per-batch containment, so `empty` would delete real session data.
        """
        crew_home, _ = stores
        archive = crew_home / "sessions" / "archive"
        victim = archive / "20260101T000000-victim01"
        victim.mkdir(parents=True)
        (victim / "dashboard_chat-1__20260101-000000.jsonl").write_bytes(b"history")

        root = session_storage.trash_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            root.symlink_to(archive, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        assert session_storage.list_trash() == []
        with pytest.raises(SessionStorageError, match="trash root is a link"):
            session_storage.empty_trash(["20260101T000000-victim01"])
        assert session_storage.empty_trash() == 0
        assert (victim / "dashboard_chat-1__20260101-000000.jsonl").is_file()

    def test_a_relocated_trash_root_reaches_nothing(self, stores: tuple[Path, Path]) -> None:
        """A linked ANCESTOR escapes the home, which the link test cannot see.

        `is_link_or_junction(root)` inspects only the final component, so a link one
        level up leaves the root a real directory while relocating it. Resolving and
        anchoring to the data home is what catches that.
        """
        crew_home, _ = stores
        outside = crew_home.parent / "not-ours"
        victim = outside / "sessions" / "20260101T000000-victim01"
        victim.mkdir(parents=True)
        (victim / "keep.txt").write_bytes(b"precious")
        # A SELF-CONSISTENT batch: every file it holds is listed, so the
        # unmanifested-file guard does not block the delete. Without a manifest this
        # test would pass for the wrong reason — the delete would be refused by that
        # other guard rather than by the containment anchor under test.
        header = {
            "schema": session_storage.MANIFEST_SCHEMA,
            "batch_id": "20260101T000000-victim01",
            "created_at": _NOW,
            "reason": "manual",
        }
        entry = {
            "uid": "aaaa1111",
            "sid": "aaaa1111",
            "files": [{"rel": "keep.txt", "origin": "/nowhere.jsonl", "bytes": 8}],
        }
        (victim / session_storage.MANIFEST_NAME).write_text(
            json.dumps(header) + "\n" + json.dumps(entry) + "\n"
        )

        root = session_storage.trash_root()
        # Link the PARENT, so the root itself is a real directory that only escapes
        # once resolved. is_link_or_junction(root) inspects the final component and
        # cannot see this; only anchoring the RESOLVED root to the data home can.
        try:
            root.parent.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # Nothing outside the data home is offered as a batch...
        assert session_storage.list_trash() == []
        # ...and naming one explicitly is refused rather than deleted.
        with pytest.raises(SessionStorageError, match="does not live under the data"):
            session_storage.empty_trash(["20260101T000000-victim01"])
        assert session_storage.empty_trash() == 0
        assert (victim / "keep.txt").is_file()

    def test_the_batch_link_test_covers_junctions(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_symlink() is False for an NTFS junction, so the guard must not use it.

        A junction cannot be created on Linux, so this asserts the guard routes
        through the junction-aware resolver: with that resolver reporting a link,
        the batch must be refused and must not be listed. A guard testing
        ``is_symlink()`` directly would ignore it — and on Windows a junction named
        as a valid batch id reads as a real directory, so the delete would resolve
        through it into the batch it points at.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        # Only the BATCH reads as a link. Reporting one for the root too would trip
        # the separate root check and prove nothing about this guard.
        monkeypatch.setattr(
            session_storage.platform_compat,
            "is_link_or_junction",
            lambda p: Path(p).name == batch.batch_id,
        )

        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash([batch.batch_id])
        assert session_storage.list_trash() == []
        # Nothing was destroyed by the refusal.
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_a_batch_link_pointing_at_another_batch_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Containment alone permits this: the link resolves INSIDE the root.

        Without the link check, emptying the alias would destroy the batch it
        points at — a real batch the user never named.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        root = session_storage.trash_root()
        alias = root / "20260101T000000-aliased1"
        try:
            alias.symlink_to(root / batch.batch_id, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash(["20260101T000000-aliased1"])

        # The real batch survives and is still restorable.
        assert [b.batch_id for b in session_storage.list_trash()] == [batch.batch_id]

    def test_a_symlinked_batch_is_not_restorable(self, stores: tuple[Path, Path]) -> None:
        """A link's NAME passes the id check; following it would escape the trash."""
        outside = stores[0].parent / "outside"
        outside.mkdir(exist_ok=True)
        root = session_storage.trash_root()
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "20260101T000000-deadbeef").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.restore("20260101T000000-deadbeef")

        # The link's target is untouched — refusing is not a partial operation.
        assert outside.is_dir()

    def test_a_rejected_kiro_home_does_not_look_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unsafe override is silently rejected, so presence proves nothing."""
        crew_home = tmp_path / "crew3"
        (crew_home / "sessions").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        # A filesystem/drive root is refused on EVERY platform (a root is its own
        # parent). A POSIX system directory like /etc is not portable: on Windows it
        # resolves to C:\etc, which the validator accepts, so the override would be
        # honoured and the store would not be shared.
        monkeypatch.setenv("KIRO_HOME", "/")

        assert session_storage.reclaim_block_reason() != ""

    def test_the_refresh_authority_check_runs_after_the_scan(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The scan is the slow part; a check before it is stale by move time."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        order: list[str] = []
        real_scan = session_storage._scan_units

        def watched_scan(index: SessionIndex):
            order.append("scan")
            return real_scan(index)

        def refresh() -> SessionIndex:
            order.append("refresh")
            return _index({"aaaa1111": "dashboard_chat-1"}, active={"aaaa1111"})

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(session_storage, "_scan_units", watched_scan)
            with pytest.raises(SessionStorageError, match="still in use"):
                session_storage.move_to_trash(
                    ["aaaa1111"],
                    reason="manual",
                    index=_index(),
                    now=_NOW,
                    refresh=refresh,
                )

        # The refresh must be the LAST thing before the move, i.e. after the scan.
        assert order == ["scan", "refresh"]
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_freshly_mapped_session_is_protected_at_move_time(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The scan can take minutes; a session mapped meanwhile must not move."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        stale = _index()  # nothing mapped when the selection was computed

        def refresh() -> SessionIndex:
            # By the time the lock is held, the session has been resumed.
            return _index({"aaaa1111": "dashboard_chat-1"}, active={"aaaa1111"})

        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(
                ["aaaa1111"], reason="manual", index=stale, now=_NOW, refresh=refresh
            )

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_failed_re_read_refuses_rather_than_proceeding(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Unable to confirm who is live is not permission to guess."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)

        def broken() -> SessionIndex:
            raise RuntimeError("session map unreadable")

        with pytest.raises(SessionStorageError, match="could not confirm"):
            session_storage.move_to_trash(
                ["aaaa1111"], reason="manual", index=_index(), now=_NOW, refresh=broken
            )

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_the_report_names_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client must be able to explain instead of offering a doomed button."""
        crew_home = tmp_path / "crew2"
        (crew_home / "sessions").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
        monkeypatch.delenv("KIRO_HOME", raising=False)

        report = session_storage.measure(_index(), now=_NOW)

        assert "sits outside it" in report.reclaim_blocked_reason


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


class TestEmptyTrash:
    def test_frees_the_staged_bytes(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        size = _cli_half(kiro_home, "aaaa1111", log_bytes=4096, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        freed = session_storage.empty_trash()

        assert freed >= size
        assert session_storage.list_trash() == []
        assert not (session_storage.trash_root() / batch.batch_id).exists()

    def test_targets_one_batch_and_leaves_the_others(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        keep = session_storage.move_to_trash(
            ["aaaa1111"], reason="a", index=_index(), now=_NOW - _DAY
        )
        drop = session_storage.move_to_trash(["bbbb2222"], reason="b", index=_index(), now=_NOW)

        session_storage.empty_trash([drop.batch_id])

        assert [b.batch_id for b in session_storage.list_trash()] == [keep.batch_id]

    def test_refuses_a_batch_holding_files_nothing_listed(self, stores: tuple[Path, Path]) -> None:
        """A header-only batch shows zero sessions, so emptying it is not consent."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        # Simulate a crash between the move and the manifest append.
        orphan = staged / "cli" / "cccc3333.jsonl"
        orphan.write_bytes(b"ONLY COPY")

        freed = session_storage.empty_trash([batch.batch_id])

        assert freed == 0
        assert orphan.read_bytes() == b"ONLY COPY"
        assert staged.is_dir()

    def test_refuses_a_batch_id_outside_the_trash_root(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash(["../../sessions"])

    def test_a_symlinked_batch_is_not_followed_out_of_the_root(
        self, stores: tuple[Path, Path], tmp_path: Path
    ) -> None:
        outside = tmp_path / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"do not delete")
        root = session_storage.trash_root()
        root.mkdir(parents=True, exist_ok=True)
        link = root / "20240101T000000-deadbeef"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # Naming it explicitly is refused: the caller's intent cannot be honoured,
        # and succeeding silently would hide that something planted a link here.
        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash(["20240101T000000-deadbeef"])

        # But it must not WEDGE the sweep-everything path, or one planted link
        # would make the trash permanently un-emptyable.
        session_storage.empty_trash()

        assert (outside / "keep.txt").is_file()
        assert link.is_symlink()


class TestTrashLocation:
    def test_trash_lives_under_the_data_home(self, stores: tuple[Path, Path]) -> None:
        crew_home, _ = stores
        assert session_storage.trash_root() == crew_home / "trash" / "sessions"

    def test_same_filesystem_is_reported_for_a_default_layout(
        self, stores: tuple[Path, Path]
    ) -> None:
        report = session_storage.measure(_index(), now=_NOW)
        assert report.trash_same_filesystem is True
