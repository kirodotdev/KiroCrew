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
import logging
import os
from pathlib import Path

import pytest

from kiro_crew import session_storage
from kiro_crew.config import paths
from kiro_crew.history import transcript_stem
from kiro_crew.session_storage import SessionIndex, SessionStorageError

_NOW = 1_700_000_000.0
_DAY = 86400.0


@pytest.fixture(autouse=True)
def _fresh_scan_cache() -> None:
    """Start every test with no cached filesystem pass.

    The cache is real process state, and its key covers the store paths and the
    pairing — not the CONTENTS of the stores. So a test that writes more files and
    re-reads within the TTL would be answered from its own earlier pass. Clearing
    here makes that isolation explicit instead of depending on each test happening
    to read only once.
    """
    session_storage.invalidate_scan_cache()


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


class TestTrashBatchNamesAreLogSafe:
    """A batch directory name is agent-controlled and can embed a newline; every
    ``list_trash`` log line that carries it must escape it, or one record forges
    additional records (refs #6315, the #6281 log-forgery class).
    """

    _FORGED = "20240101T000000-deadbeef\nWARNING forged: batch cleared by operator"

    @staticmethod
    def _make_forged_dir(name: str) -> Path:
        forged = session_storage.trash_root() / name
        try:
            forged.mkdir(parents=True)
        except OSError:
            pytest.skip("this filesystem refuses a newline in a directory name")
        return forged

    def test_missing_manifest_log_escapes_the_batch_name(
        self, stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        self._make_forged_dir(self._FORGED)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_storage"):
            assert session_storage.list_trash() == []

        messages = [
            record.getMessage()
            for record in caplog.records
            if "no readable manifest" in record.getMessage()
        ]
        assert messages, "the missing-manifest site did not log at all"
        # The rendered record stays one line: the name appears repr'd, and the
        # injected second line never starts a record of its own.
        assert all("\n" not in message for message in messages)
        assert any(repr(self._FORGED) in message for message in messages)

    def test_batch_id_disagreement_log_escapes_the_batch_name(
        self, stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        # Renaming the directory makes its manifest header disagree with it,
        # which is exactly the warning path under test.
        original = session_storage.trash_root() / batch.batch_id
        try:
            original.rename(session_storage.trash_root() / self._FORGED)
        except OSError:
            pytest.skip("this filesystem refuses a newline in a directory name")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.list_trash() == []

        messages = [
            record.getMessage()
            for record in caplog.records
            if "claiming batch id" in record.getMessage()
        ]
        assert messages, "the batch-id-disagreement site did not log at all"
        assert all("\n" not in message for message in messages)
        assert any(repr(self._FORGED) in message for message in messages)

    def test_both_sites_escape_without_touching_the_filesystem(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Windows refuses a newline in a real directory name, which would skip
        the two filesystem tests above on those shards. Injecting the forged
        name at the manifest-read seam pins both log sites on every platform.
        """
        session_storage.trash_root().mkdir(parents=True)

        class _ForgedDir:
            name = self._FORGED

            @staticmethod
            def is_dir() -> bool:
                return True

        # Shaped for `_summarize_manifest` — (header, sessions, staged_bytes) —
        # because that is the seam `list_trash` reads since #6312. Patching
        # `_read_manifest` instead lets the real `_summarize_manifest` run, and it
        # does `batch / MANIFEST_NAME`, which a name-only double cannot support.
        manifests: dict[str, tuple[dict[str, object], int, int] | None] = {
            "missing": None,
            "disagreeing": ({"batch_id": "someone-else", "created_at": _NOW}, 0, 0),
        }

        # Forge ONLY the trash root's own enumeration, via a Path subclass whose
        # `iterdir` yields the double. Patching `session_storage.Path.iterdir`
        # (i.e. `pathlib.Path.iterdir`) globally leaks across the xdist worker:
        # a sibling test in the same process that legitimately iterates a real
        # directory then reaches the unpatched `_summarize_manifest`, which does
        # `<_ForgedDir> / MANIFEST_NAME` and raises TypeError (refs #6425).
        class _ForgedRoot(type(session_storage.trash_root())):
            def iterdir(self):  # type: ignore[override]
                return iter([_ForgedDir()])

        forged_root = _ForgedRoot(session_storage.trash_root())

        for label, parsed in manifests.items():
            caplog.clear()
            monkeypatch.setattr(session_storage, "trash_root", lambda: forged_root)
            monkeypatch.setattr(
                session_storage.platform_compat, "is_link_or_junction", lambda p: False
            )
            monkeypatch.setattr(
                session_storage, "_summarize_manifest", lambda batch, parsed=parsed: parsed
            )

            with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_storage"):
                assert session_storage.list_trash() == []

            rendered = [record.getMessage() for record in caplog.records]
            carrying = [message for message in rendered if repr(self._FORGED) in message]
            assert carrying, f"the {label}-manifest site did not log the name repr'd"
            assert all("\n" not in message for message in rendered)


class TestUntrustedNamesAreLogSafeOutsideListTrash:
    """The forgery primitive of :class:`TestTrashBatchNamesAreLogSafe`, at the
    sibling sites outside ``list_trash`` (refs #6344, the #6281 class).

    Two operands carry it, and they are not equally reachable. A manifest-supplied
    ``uid`` is read off disk with no validation, so its sites take a forged value
    end to end. A batch DIRECTORY name reaches its sites only through
    :func:`_batch_dir`, whose id pattern rejects a newline, so those conversions
    are defence in depth and are pinned at the seam each helper already offers —
    which is also what keeps them covered on Windows shards, where a hostile
    directory name cannot be created at all.
    """

    _FORGED_UID = "aaaa1111\nWARNING forged: session restored by operator"
    _FORGED_NAME = "20240101T000000-deadbeef\nWARNING forged: batch cleared by operator"

    @staticmethod
    def _staged(kiro_home: Path) -> tuple[str, Path]:
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        return batch.batch_id, session_storage.trash_root() / batch.batch_id

    @classmethod
    def _forge_uid(cls, batch: Path, *, files: object | None = None) -> None:
        """Rewrite the batch's one manifest entry under a forged uid.

        The uid is JSON-escaped on the way to disk, so the manifest itself stays
        one line — the forgery only materializes if a log renders the value raw.
        """
        manifest = batch / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["uid"] = cls._FORGED_UID
        if files is not None:
            entry["files"] = files
        manifest.write_text(f"{lines[0]}\n{json.dumps(entry)}\n", encoding="utf-8")

    @staticmethod
    def _carrying(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
        return [record.getMessage() for record in caplog.records if needle in record.getMessage()]

    def _assert_escaped(self, messages: list[str], value: str, site: str) -> None:
        assert messages, f"the {site} site did not log at all"
        # One record stays one record: the value appears repr'd, so the injected
        # second line never starts a record of its own.
        assert all("\n" not in message for message in messages)
        assert any(repr(value) in message for message in messages)

    def test_a_forged_uid_is_escaped_when_a_manifest_record_is_not_an_object(
        self, stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        self._forge_uid(batch, files=["not-an-object"])

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.restore(batch_id) == 0

        self._assert_escaped(
            self._carrying(caplog, "is not an object"), self._FORGED_UID, "unreadable-record"
        )

    def test_a_forged_uid_is_escaped_when_the_session_was_recreated(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        self._forge_uid(batch)
        # The occupant-wins branch: the session came back between the preflight
        # and the move, so the batch keeps its entry.
        monkeypatch.setattr(session_storage, "_move_file_exclusive", lambda src, dst: False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.restore(batch_id) == 0

        self._assert_escaped(
            self._carrying(caplog, "was recreated while being restored"),
            self._FORGED_UID,
            "lost-race",
        )

    def test_a_forged_uid_is_escaped_when_the_restore_cannot_finish(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        self._forge_uid(batch)

        def _refuse(src: Path, dst: Path) -> bool:
            raise OSError("the store is read-only")

        monkeypatch.setattr(session_storage, "_move_file_exclusive", _refuse)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.restore(batch_id) == 0

        self._assert_escaped(
            self._carrying(caplog, "could not fully restore session"),
            self._FORGED_UID,
            "restore-failed",
        )

    def test_the_manifest_rollback_site_escapes_the_session_id(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """This uid comes from the caller's validated list, so the conversion is
        defence in depth; the test pins it rather than claiming reachability.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)

        def _refuse(handle: object, entry: dict[str, object]) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(session_storage, "_append_entry", _refuse)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            with pytest.raises(SessionStorageError):
                session_storage.move_to_trash(
                    ["aaaa1111"], reason="manual", index=_index(), now=_NOW
                )

        self._assert_escaped(
            self._carrying(caplog, "could not record session"), "aaaa1111", "manifest-rollback"
        )

    def test_the_discard_and_incomplete_sites_escape_the_batch_name(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Both keep-the-batch branches and the still-on-disk branch, at the
        ``_unlisted_files`` seam — no hostile directory has to exist on disk.
        """
        forged = session_storage.trash_root() / self._FORGED_NAME
        # Neither branch may write: the point under test is the log line.
        monkeypatch.setattr(session_storage, "_rewrite_manifest", lambda *a, **k: None)

        def _refuse(batch: Path) -> list[Path]:
            raise SessionStorageError(f"could not read all of {batch.name!r}")

        monkeypatch.setattr(session_storage, "_unlisted_files", _refuse)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._discard_restored_batch(forged, {})
        self._assert_escaped(
            self._carrying(caplog, "keeping trash batch"), self._FORGED_NAME, "unreadable-scan"
        )

        caplog.clear()
        monkeypatch.setattr(
            session_storage, "_unlisted_files", lambda batch: [forged / "orphan.jsonl"]
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._discard_restored_batch(forged, {})
        self._assert_escaped(
            self._carrying(caplog, "keeping trash batch"), self._FORGED_NAME, "leftover-files"
        )

        caplog.clear()

        class _ForgedBatch:
            name = self._FORGED_NAME

            @staticmethod
            def exists() -> bool:
                return True

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert (
                session_storage._incomplete_if_present(_ForgedBatch())
                == session_storage.SKIP_INCOMPLETE
            )
        self._assert_escaped(
            self._carrying(caplog, "did not remove it"), self._FORGED_NAME, "still-present"
        )

    def test_the_empty_trash_sites_escape_what_they_name(
        self,
        stores: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The three refusals in the sweep, at the ``_batch_dir`` seam."""
        session_storage.trash_root().mkdir(parents=True, exist_ok=True)
        forged = session_storage.trash_root() / self._FORGED_NAME

        class _Batch:
            batch_id = "20240101T000000-deadbeef"

        monkeypatch.setattr(session_storage, "list_trash", lambda: [_Batch()])
        monkeypatch.setattr(session_storage, "_batch_dir", lambda batch_id: forged)

        def _refuse(batch: Path) -> list[Path]:
            raise SessionStorageError("could not read all of it")

        monkeypatch.setattr(session_storage, "_unlisted_files", _refuse)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.empty_trash() == 0
        self._assert_escaped(
            self._carrying(caplog, "refusing to empty"), self._FORGED_NAME, "unreadable-batch"
        )

        caplog.clear()
        monkeypatch.setattr(
            session_storage, "_unlisted_files", lambda batch: [forged / "orphan.jsonl"]
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.empty_trash() == 0
        self._assert_escaped(
            self._carrying(caplog, "refusing to empty"), self._FORGED_NAME, "leftover-files"
        )

        # The outside-the-root refusal names the PATH, so its repr is the Path's.
        caplog.clear()
        outside = tmp_path / self._FORGED_NAME
        monkeypatch.setattr(session_storage, "_batch_dir", lambda batch_id: outside)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.empty_trash() == 0
        messages = self._carrying(caplog, "outside the trash root")
        assert messages, "the outside-the-root site did not log at all"
        assert all("\n" not in message for message in messages)
        assert any(repr(outside) in message for message in messages)

    def test_the_unreadable_scan_error_escapes_the_batch_name(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two refusal sites render this exception with ``%s``, so the name has
        to be escaped where the exception is BUILT or the escape at the log site is
        undone by the text it carries.
        """
        forged = session_storage.trash_root() / self._FORGED_NAME

        # The error carries a hostile .filename, which is the half the batch-name
        # escape does not cover: its string form is interpolated into the same
        # message.
        planted = OSError(13, "Permission denied", str(forged / self._FORGED_NAME))

        # ``session_storage.os`` is the global module, so this patch is also seen
        # by ``shutil.rmtree`` during fixture teardown (which passes ``topdown=``
        # on Windows).  Accept the real signature and divert only the walk of the
        # forged batch, delegating every other call to the genuine ``os.walk``.
        real_walk = os.walk

        def _walk(top, *args, onerror=None, **kwargs):  # type: ignore[no-untyped-def]
            if Path(top) == forged:
                assert callable(onerror)
                onerror(planted)
                return iter(())
            return real_walk(top, *args, onerror=onerror, **kwargs)

        monkeypatch.setattr(session_storage.os, "walk", _walk)
        monkeypatch.setattr(session_storage, "_manifest_rels", lambda batch: [])

        with pytest.raises(SessionStorageError) as raised:
            session_storage._unlisted_files(forged)

        # Both halves of the message are newline-free: the name because this call
        # site reprs it, and the interpolated OSError because ``OSError.__str__``
        # reprs its own ``.filename``. The second half is why the error operand is
        # correctly left as %s at the sites that render it.
        assert "\n" not in str(raised.value)
        assert repr(self._FORGED_NAME) in str(raised.value)
        assert "Permission denied" in str(raised.value)


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

        def counted(sid_for_stem):
            nonlocal calls
            calls += 1
            return real(sid_for_stem)

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

        def counted(sid_for_stem):
            nonlocal calls
            calls += 1
            return real(sid_for_stem)

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

    def test_a_pod_with_a_readable_map_protects_its_sessions_without_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mirror case: a pod reads THIS store while keeping its own map.

        Its mapping is a file at a known host path, so the sessions it can resume
        are knowable. Naming them is strictly better than refusing the whole
        operation: the pod's own sessions stay protected and everything else stays
        reclaimable.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-feature"
        pod.mkdir(parents=True)
        # Current pods export KIRO_HOME into the pod home, so this one reads its own
        # replay store and cannot be harmed by a reclaim here.
        (pod / "kiro" / "sessions" / "cli").mkdir(parents=True)
        (pod / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": {"sid": "podsid01"}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        assert session_storage.reclaim_block_reason() == ""
        protected, refusals = session_storage.cotenant_sids()
        assert protected == frozenset({"podsid01"})
        assert refusals == ()

    def test_a_shared_store_instance_with_mappings_refuses_the_whole_reclaim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-session protection is not enough for a genuine co-tenant.

        An instance without its own replay store can seed and resume a session at
        any moment — including part-way through a move loop that runs for six
        figures of sessions — so no ownership snapshot can cover it. Those refuse
        outright, which is the one case the blanket refusal was right about.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-legacy-shared"
        pod.mkdir(parents=True)
        # No `kiro/` directory: nothing shows this instance to be self-contained.
        (pod / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": {"sid": "sharedsid1"}}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        reason = session_storage.reclaim_block_reason()
        assert "wt-legacy-shared" in reason
        assert "shares this replay store" in reason
        protected, refusals = session_storage.cotenant_sids()
        assert protected == frozenset({"sharedsid1"}), "still named, so still protected"
        assert [name for name, _why in refusals] == ["wt-legacy-shared"]

    def test_a_pod_that_left_only_a_directory_does_not_block_reclaiming(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A torn-down pod's leftover directory owns no sessions.

        This is the state a machine that has run pods for weeks is actually in, and
        blocking on it made reclaiming permanently unavailable while naming
        directories whose gateway exited long ago.
        """
        pod_root = tmp_path / "pods"
        (pod_root / "wt-long-gone").mkdir(parents=True)
        # What an evicted pod leaves behind: its audit log, and no session map.
        (pod_root / "wt-long-gone" / "security_events.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        assert session_storage.reclaim_block_reason() == ""
        assert session_storage.cotenant_sids() == (frozenset(), ())

    def test_a_legacy_string_map_entry_still_protects_its_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain-string entry is the LEGACY format, and must not fail open.

        ``SessionMap._load`` still migrates ``{"key": "<sid>"}`` to the dict form on
        read, so a map written that way is live data, not corruption. Skipping it
        would fail open on precisely the population this protection exists for: a
        co-tenant old enough to predate the pod store split is also old enough to
        have been written in the old format.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-legacy"
        pod.mkdir(parents=True)
        # Self-contained, so this isolates the entry-format question from the
        # shared-store refusal.
        (pod / "kiro" / "sessions" / "cli").mkdir(parents=True)
        (pod / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1": "legacysid01"}), encoding="utf-8"
        )
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        protected, refusals = session_storage.cotenant_sids()
        assert protected == frozenset({"legacysid01"})
        assert refusals == ()

    def test_a_cotenant_claiming_a_session_during_the_scan_is_refused(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Co-tenant ownership is re-read in the FINAL authority check.

        The store scan is the slow part of a move, so a co-tenant view taken during
        it is stale by the time files move. This simulates a co-tenant adopting a
        pre-existing replay log in that window: the first read (during the scan)
        does not know about it, the last one does. The freshness floor cannot cover
        this — the adopted session is 40 days old.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "adopted001", log_bytes=64, age_days=40)

        calls = {"n": 0}

        def claimed_late() -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
            calls["n"] += 1
            # Empty while the scan runs; owned by the time the move is authorized.
            return (frozenset() if calls["n"] == 1 else frozenset({"adopted001"})), ()

        monkeypatch.setattr(session_storage, "cotenant_sids", claimed_late)

        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(["adopted001"], reason="manual", index=_index(), now=_NOW)
        assert (kiro_home / "sessions" / "cli" / "adopted001.jsonl").is_file()

    def test_a_cotenant_map_that_becomes_unreadable_mid_move_refuses(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ownership that stops being establishable cannot be worked around."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)

        calls = {"n": 0}

        def unreadable_late() -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
            calls["n"] += 1
            return frozenset(), (() if calls["n"] == 1 else (("wt-corrupt", "unreadable map"),))

        monkeypatch.setattr(session_storage, "cotenant_sids", unreadable_late)

        with pytest.raises(SessionStorageError, match="make reclaiming unsafe"):
            session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()

    def test_a_symlinked_cotenant_map_is_refused_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pod root is writable, so its map must go through the file gate.

        A ``session_map.json`` replaced with a symlink would otherwise make the
        gateway read whatever it points at. The read is routed through
        ``hooks.safe_read_file``, which resolves the link and re-checks the
        RESOLVED target — so a link into a protected path is refused, and the
        co-tenant is treated as ownership-unknown rather than silently skipped.
        """
        secret = tmp_path / "aws-credentials"
        secret.write_text("[default]\naws_secret_access_key = shhh\n", encoding="utf-8")
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-symlinked"
        pod.mkdir(parents=True)
        (pod / "session_map.json").symlink_to(secret)
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))

        def refuse_everything(resolved: str) -> bool:
            return Path(resolved) == secret

        monkeypatch.setattr(session_storage.hooks, "is_sensitive_path", refuse_everything)

        protected, refusals = session_storage.cotenant_sids()

        assert protected == frozenset(), "nothing may be harvested from the target"
        assert [name for name, _why in refusals] == ["wt-symlinked"]
        # The REASON is what distinguishes refused-before-reading from
        # read-then-failed-to-parse. A bare read would follow the link, fail to
        # parse the credential file, and report "could not be parsed" — which is
        # the same refusal from the caller's side but means the read happened.
        assert refusals[0][1] == "its session map could not be read"
        assert "parsed" not in refusals[0][1]

    def test_a_map_that_is_not_valid_utf8_is_refused_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Undecodable bytes are an unreadable map, not an exception to the caller.

        ``safe_read_file`` decodes as UTF-8, and ``UnicodeDecodeError`` is a
        ``ValueError`` rather than an ``OSError`` — so an undecodable map would
        travel past the refusal path and out of the storage endpoints as a 500,
        breaking the whole screen instead of protecting one co-tenant.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-binary"
        pod.mkdir(parents=True)
        (pod / "session_map.json").write_bytes(b"\xff\xfe{\x00")
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))

        protected, refusals = session_storage.cotenant_sids()

        assert protected == frozenset()
        assert [name for name, _why in refusals] == ["wt-binary"]
        # Undecodable is a failure to READ, not to parse: the bytes never became
        # text, so no parse was ever attempted.
        assert refusals[0][1] == "its session map could not be read"

    def test_a_pod_whose_map_cannot_be_read_still_blocks_and_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown ownership is the one case that must still refuse.

        A map that is present but unparseable says a co-tenant claims SOMETHING
        without saying what, which is exactly when reclaiming cannot be made safe.
        """
        pod_root = tmp_path / "pods"
        pod = pod_root / "wt-corrupt"
        pod.mkdir(parents=True)
        (pod / "session_map.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", paths._default_home())
        monkeypatch.setattr(paths, "_config_dir_memo", None)

        reason = session_storage.reclaim_block_reason()
        assert "make reclaiming unsafe" in reason
        assert "wt-corrupt" in reason
        assert "could not be parsed" in reason

    def test_a_cotenant_session_is_refused_like_a_mapped_one(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The protection has to bite at the move, not only in the report.

        Patched at :func:`cotenant_sids` rather than through the pod root, because
        the *stores* fixture isolates both homes — the arrangement in which the pod
        discovery path is correctly not consulted. What is under test here is that
        whatever that function returns is enforced by ``move_to_trash``.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "podsid01", log_bytes=64, age_days=40)
        monkeypatch.setattr(session_storage, "cotenant_sids", lambda: (frozenset({"podsid01"}), ()))

        with pytest.raises(SessionStorageError, match="still in use"):
            session_storage.move_to_trash(["podsid01"], reason="manual", index=_index(), now=_NOW)
        assert (kiro_home / "sessions" / "cli" / "podsid01.jsonl").is_file()

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

    def test_reports_progress_that_ends_at_what_it_returns(self, stores: tuple[Path, Path]) -> None:
        """Progress must be a running total of the WHOLE call, not per batch.

        A screen draws a bar against one denominator, so a figure that restarts on
        each batch would march to the end and then jump backwards. The last value
        reported is also the returned total, which is what lets a client stop
        polling on the callback rather than waiting for a separate confirmation.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=4096, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=2048, age_days=40)
        first = session_storage.move_to_trash(
            ["aaaa1111"], reason="a", index=_index(), now=_NOW - _DAY
        )
        second = session_storage.move_to_trash(["bbbb2222"], reason="b", index=_index(), now=_NOW)

        seen: list[int] = []
        freed = session_storage.empty_trash(
            [first.batch_id, second.batch_id], on_progress=seen.append
        )

        assert seen, "a delete that frees bytes must report at least once"
        assert seen == sorted(seen), "a running total cannot go down"
        assert seen[-1] == freed
        assert freed >= 4096 + 2048
        assert session_storage.list_trash() == []

    def test_a_refused_batch_reports_nothing_it_did_not_delete(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The refusal path must not report bytes: nothing was freed."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        (session_storage.trash_root() / batch.batch_id / "cli" / "cccc3333.jsonl").write_bytes(
            b"ONLY COPY"
        )

        seen: list[int] = []
        skips: list[str] = []
        freed = session_storage.empty_trash(
            [batch.batch_id], on_progress=seen.append, on_skip=skips.append
        )

        assert freed == 0
        assert seen == []
        # And it SAYS why. Reporting only "0 bytes freed" made a batch deliberately
        # kept indistinguishable from an empty one, with the reason in a log the
        # user cannot read.
        assert skips == [session_storage.SKIP_UNLISTED_FILES]

    def test_deletes_only_what_the_manifest_names(self, stores: tuple[Path, Path]) -> None:
        """Files are named by the manifest, never discovered by walking the batch.

        A walk has to decide per entry whether to descend, and on Windows a junction
        is not a symlink -- os.path.islink reports False for one, so os.walk would
        descend into it and unlink the files it points at, outside the trash. Naming
        the files means traversal never happens. Asserted here with a symlinked
        subdirectory, the portable stand-in for that shape.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id

        outside = kiro_home.parent / "precious"
        outside.mkdir()
        victim = outside / "keep.jsonl"
        victim.write_bytes(b"NOT THE TRASH")
        # A LINKED DIRECTORY inside the batch. Note it does not trip the
        # unlisted-file guard: that guard walks for files, and a link to a
        # directory is not one -- which is precisely why the delete must not be the
        # thing that decides whether to descend.
        (staged / "link").symlink_to(outside, target_is_directory=True)

        freed = session_storage.empty_trash([batch.batch_id])

        assert freed >= 64
        assert not staged.exists(), "the batch itself is still removed"
        assert victim.read_bytes() == b"NOT THE TRASH"
        assert outside.is_dir()

    @pytest.mark.parametrize(
        "rel",
        [
            "/etc/victim",  # absolute
            "//tmp/victim",  # POSIX root of TWO slashes, whose part is "//"
            "///tmp/victim",
            "../outside",
            "cli/../../outside",
            "..",
            "",
            ".",
            "C:/windows/x",  # absolute in the other flavour
            "C:\\windows\\x",
            "..\\outside",  # a parent reference only Windows parsing sees
            "\\\\server\\share\\x",
        ],
    )
    def test_a_staged_name_that_is_not_a_plain_relative_path_is_refused(self, rel: str) -> None:
        """One table, because each shape got in by a different spelling.

        `//tmp/victim` is the one that shipped: its first component is `//`, so a
        check against `/` missed it, and an absolute path handed to `os.open` ignores
        `dir_fd` and leaves the batch entirely. The Windows spellings matter because
        the coarse path joins these names with the local `Path`.
        """
        assert session_storage._plain_parts(rel) is None

    @pytest.mark.parametrize("rel", ["cli/a.jsonl", "crew/archive/b.jsonl", "c.json"])
    def test_a_plain_staged_name_is_accepted(self, rel: str) -> None:
        assert session_storage._plain_parts(rel) is not None

    def test_the_size_read_refuses_a_rel_that_escapes_the_batch(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The coarse path's measurement must not stat outside the batch either.

        Where the platform cannot delete by descriptor the batch takes rmtree and the
        bytes come from statting the manifest's names. That read went straight to the
        filesystem, so a tampered entry made it measure - and report as freed - a file
        the delete never touched.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        outside = kiro_home / "big.jsonl"
        outside.write_bytes(b"z" * 100_000)

        listed = session_storage._manifest_rels(staged)
        honest = session_storage._listed_bytes(staged, listed)
        tampered = session_storage._listed_bytes(staged, listed + ["../../big.jsonl"])

        assert tampered == honest, "an escaping rel must contribute nothing"
        assert honest >= 16

    def test_a_stale_but_well_formed_id_is_refused(self, stores: tuple[Path, Path]) -> None:
        """Naming a batch that is not staged is a refusal, not a zero-byte success.

        `_batch_dir` does not require the directory to exist, so an id that was already
        emptied passes it. Measured on the pre-snapshot path: that id reached the delete
        and returned 0 bytes with only a log line, which is the silent outcome this PR
        exists to remove - and filtering it out of the snapshot kept it silent.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        stale = "20260101T000000-deadbeef"

        with pytest.raises(SessionStorageError, match="no longer staged"):
            session_storage.staged_targets([stale])

        # Naming a live batch alongside a stale one is refused too: the request is not
        # partly honoured, because the caller cannot see which half ran.
        with pytest.raises(SessionStorageError, match="no longer staged"):
            session_storage.staged_targets([batch.batch_id, stale])

        # And the live batch alone still resolves.
        ids, total = session_storage.staged_targets([batch.batch_id])
        assert ids == [batch.batch_id] and total > 0

    def test_the_target_snapshot_is_serialized_against_staging(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A batch mid-staging must not be selectable, so it cannot grow before delete.

        `list_trash` does not take the mutation lock, so an unlocked read can see a
        batch whose directory and manifest header exist while its sessions are still
        moving in. Selecting that id makes the delete wait for staging to finish and
        then destroy the FINISHED batch - sessions the user never saw included.

        Asserted on the mechanism rather than by racing staging, which on a quiet
        machine would pass either way: while the snapshot is reading, a FRESH
        descriptor on the same lock file must not be able to take it, which is exactly
        what excludes a concurrent `move_to_trash`.
        """
        pytest.importorskip("fcntl")
        import fcntl

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        lock_path = session_storage.trash_root().parent / session_storage.MUTATION_LOCK_NAME
        observed: list[str] = []
        real_list = session_storage.list_trash

        def probe_then_list():  # type: ignore[no-untyped-def]
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                observed.append("free")
            except OSError:
                observed.append("held")
            finally:
                os.close(fd)
            return real_list()

        monkey = pytest.MonkeyPatch()
        monkey.setattr(session_storage, "list_trash", probe_then_list)
        try:
            ids, total = session_storage.staged_targets(None)
        finally:
            monkey.undo()

        assert observed == ["held"], "the snapshot must resolve inside the mutation lock"
        assert len(ids) == 1 and total > 0

    def test_a_nul_in_a_manifest_name_cannot_abort_a_half_done_delete(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A NUL is the one bad name that raised ValueError, not OSError.

        JSON can carry `\\u0000`, so a tampered or corrupted manifest can hold one, and
        every `os` call then raises ValueError - which the delete's `except OSError` did
        not catch. It escaped mid-loop, after earlier files were already unlinked, so an
        irreversible operation stopped halfway. Two guards now: the validator refuses the
        name, and the loop treats an unusable name as costing its own file only.
        """
        assert session_storage._plain_parts("a\x00b") is None
        assert session_storage._plain_parts("ok/a\x00b") is None

    def test_a_drive_relative_name_is_refused_even_though_it_is_not_absolute(
        self,
    ) -> None:
        """`C:.ssh/id_rsa` has no root, so an absoluteness check alone lets it through.

        Joining it onto the batch on Windows replaces the anchor - pathlib lets a
        right-hand side carrying a drive take over - and resolves it against that
        drive's working directory, so the size read would stat a file outside the batch
        and report that it exists and how big it is.
        """
        for rel in ("C:.ssh/id_rsa", "c:y", "C:/x", "C:\\x"):
            assert session_storage._plain_parts(rel) is None, rel
        # Collateral, and deliberate: Windows parsing reads a POSIX name spelled `a:b`
        # as a drive too. This store names its own files, so it never writes one, and
        # being wrong costs a batch kept as incomplete rather than an escape.
        assert session_storage._plain_parts("a:b/c") is None
        # A colon elsewhere in the name is untouched.
        assert session_storage._plain_parts("ab:c/d") == ("ab:c", "d")

    def test_the_coarse_path_reports_only_bytes_that_went_away(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Windows takes rmtree with `ignore_errors`, which can leave the batch standing.

        The freed figure has to be measured after the attempt, not before it, or a
        locked file is reported as space reclaimed while it is still on disk.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=4096, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            # Let rmtree fail the way a lock does: quietly.
            patched.setattr(session_storage.shutil, "rmtree", lambda *a, **k: None)
            freed = session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert staged.is_dir(), "the batch must still be there for this to mean anything"
        assert freed == 0, "nothing went away, so nothing may be reported as freed"
        assert skips == [session_storage.SKIP_INCOMPLETE]

    def test_a_batch_that_survives_a_coarse_delete_reports_a_reason(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The coarse path re-checks the directory, because rmtree tells it nothing.

        `rmtree(ignore_errors=True)` reports nothing, so a tree it could not remove left
        the batch on the user's screen while the job said the delete had succeeded.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        monkeypatch.setattr(session_storage.shutil, "rmtree", lambda *a, **k: None)
        skips: list[str] = []
        session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_a_manifest_rel_that_escapes_the_batch_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A tampered manifest cannot aim the delete out of its own batch."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        keep = session_storage.move_to_trash(
            ["aaaa1111"], reason="a", index=_index(), now=_NOW - _DAY
        )
        drop = session_storage.move_to_trash(["bbbb2222"], reason="b", index=_index(), now=_NOW)

        # Point one of drop's entries at a file inside the OTHER batch.
        target = session_storage.trash_root() / keep.batch_id / "cli" / "aaaa1111.jsonl"
        assert target.is_file()
        manifest = session_storage.trash_root() / drop.batch_id / "manifest.jsonl"
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"].append({"rel": f"../{keep.batch_id}/cli/aaaa1111.jsonl"})
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        session_storage.empty_trash([drop.batch_id])

        assert target.is_file(), "a rel outside the batch must not be deleted"

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


class TestSingleTrashPass:
    """Each payload build reads each trash manifest exactly once.

    ``list_trash`` is uncached and reads every batch manifest per call, so a
    payload builder that calls it once for the totals (inside ``measure``) and
    again for the wire array pays double I/O — and could ship totals that
    contradict the array beside them if a stage/empty lands between the calls.
    Both builders thread one list through ``measure(batches=...)`` instead,
    mirroring the ``units=`` parameter and the counting test above.
    """

    @staticmethod
    def _two_batches(kiro_home: Path) -> None:
        """Two batches with DISTINCT created_at stamps, so an ordering assertion
        on the payload array is falsifiable rather than satisfied by any order."""
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=64, age_days=40)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW - 60)
        session_storage.move_to_trash(["bbbb2222"], reason="manual", index=_index(), now=_NOW)

    @staticmethod
    def _counted_manifest_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Count via ``_summarize_manifest``: since #6281 it is ``list_trash``'s
        per-batch cost (the count-only streamed pass), and it is hit through the
        module global, so it sees every ``list_trash`` pass no matter which
        module's imported name made the call."""
        reads: list[Path] = []
        real = session_storage._summarize_manifest

        def counted(batch: Path):
            reads.append(batch)
            return real(batch)

        monkeypatch.setattr(session_storage, "_summarize_manifest", counted)
        return reads

    def test_report_payload_reads_each_manifest_exactly_once(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import session_storage as handler

        _, kiro_home = stores
        self._two_batches(kiro_home)
        reads = self._counted_manifest_reads(monkeypatch)

        payload = handler._report_payload()

        assert len(reads) == 2, "one payload build must read each batch manifest once"
        assert len(set(reads)) == 2
        # Newest first is list_trash's own contract; asserting it on the wire
        # array proves the hoisted list reaches the payload unreordered. The
        # helper gives the batches distinct stamps so this can actually fail.
        assert [b["created_at"] for b in payload["trash"]["batches"]] == [_NOW, _NOW - 60]
        assert len(payload["trash"]["batches"]) == 2
        assert payload["trash"]["bytes"] == sum(b["bytes"] for b in payload["trash"]["batches"])

    def test_inventory_payload_reads_each_manifest_exactly_once(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import session_storage as handler

        _, kiro_home = stores
        self._two_batches(kiro_home)
        reads = self._counted_manifest_reads(monkeypatch)

        # Explicit empty stubs, not a MagicMock: a bare mock answers
        # running_session_keys() and list_sessions() via magic-method defaults,
        # which silently weakens the test if either callee grows a real check.
        state = SimpleNamespace(
            running_session_keys=lambda: frozenset(),
            conversation_log=SimpleNamespace(list_sessions=lambda: []),
        )
        payload = handler._inventory_payload(state)

        assert len(reads) == 2, "one payload build must read each batch manifest once"
        assert len(set(reads)) == 2
        assert len(payload["trash"]["batches"]) == 2
        assert payload["trash"]["bytes"] == sum(b["bytes"] for b in payload["trash"]["batches"])

    def test_measure_without_batches_still_enumerates_the_trash(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default path is load-bearing: every existing ``measure(index)``
        caller relies on it enumerating the trash itself."""
        _, kiro_home = stores
        self._two_batches(kiro_home)
        reads = self._counted_manifest_reads(monkeypatch)

        report = session_storage.measure(_index(), now=_NOW)

        assert report.trash_batches == 2
        assert report.trash_bytes > 0
        assert len(reads) == 2, "measure() with no batches= must do its own pass"

    def test_measure_uses_the_handed_over_batches_without_a_second_pass(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, kiro_home = stores
        self._two_batches(kiro_home)
        batches = session_storage.list_trash()
        reads = self._counted_manifest_reads(monkeypatch)

        report = session_storage.measure(_index(), now=_NOW, batches=batches)

        assert report.trash_batches == 2
        assert report.trash_bytes == sum(b.bytes for b in batches)
        assert reads == [], "a handed-over list must not trigger another manifest pass"


class TestManifestReaders:
    """`_read_manifest` streams; `_summarize_manifest` aggregates without a list.

    The core invariant: on every manifest the summary's (header, sessions, bytes)
    are byte-identical to what the full read derives — same skipped-line
    tolerance, same schema rejection, same ``None`` on an unreadable file.
    """

    _BATCH_ID = "20240101T000000-cafe0001"

    def _batch(self, tmp_path: Path, lines: list[str], *, terminated: bool = True) -> Path:
        batch = tmp_path / self._BATCH_ID
        batch.mkdir(parents=True, exist_ok=True)
        (batch / session_storage.MANIFEST_NAME).write_text(
            "\n".join(lines) + ("\n" if terminated else ""), encoding="utf-8"
        )
        return batch

    def _header(self, **overrides: object) -> dict[str, object]:
        header: dict[str, object] = {
            "schema": session_storage.MANIFEST_SCHEMA,
            "batch_id": self._BATCH_ID,
            "created_at": _NOW,
            "reason": "manual",
        }
        header.update(overrides)
        return header

    def _entry(self, uid: str, sizes: list[int]) -> dict[str, object]:
        return {
            "uid": uid,
            "files": [
                {"rel": f"cli/{uid}-{i}.jsonl", "origin": f"/x/{uid}-{i}", "bytes": size}
                for i, size in enumerate(sizes)
            ],
        }

    def test_summary_is_byte_identical_to_the_full_read(self, tmp_path: Path) -> None:
        """Header, count and byte total agree with the entry list on a manifest
        that exercises every tolerated irregularity at once: blank lines, a
        malformed line, a non-dict line, and a truncated final line."""
        entries = [
            self._entry("aaaa1111", [10, 20]),
            self._entry("bbbb2222", [5]),
            self._entry("cccc3333", []),
        ]
        lines = [json.dumps(self._header())]
        lines.append("")
        lines.append(json.dumps(entries[0]))
        lines.append("not json at all {")
        lines.append(json.dumps(entries[1]))
        lines.append(json.dumps([1, 2, 3]))
        lines.append(json.dumps(entries[2]))
        lines.append('{"uid": "dddd444')  # crash mid-append: NO trailing newline
        batch = self._batch(tmp_path, lines, terminated=False)

        parsed = session_storage._read_manifest(batch)
        summary = session_storage._summarize_manifest(batch)

        assert parsed is not None and summary is not None
        header, read_entries = parsed
        assert summary == (
            header,
            len(read_entries),
            sum(session_storage._entry_bytes(e) for e in read_entries),
        )
        # Pin the absolute values too, so both readers cannot be wrong together.
        assert summary[1] == 3
        assert summary[2] == 35

    def test_header_only_manifest_summarizes_to_zero(self, tmp_path: Path) -> None:
        batch = self._batch(tmp_path, [json.dumps(self._header())])
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert (summary[1], summary[2]) == (0, 0)

    def test_wrong_schema_is_rejected_by_both_readers(self, tmp_path: Path) -> None:
        lines = [json.dumps(self._header(schema=99)), json.dumps(self._entry("aaaa1111", [8]))]
        batch = self._batch(tmp_path, lines)
        assert session_storage._read_manifest(batch) is None
        assert session_storage._summarize_manifest(batch) is None

    def test_unreadable_manifest_returns_none_from_both_readers(self, tmp_path: Path) -> None:
        batch = tmp_path / self._BATCH_ID
        # A directory where the file should be: open() raises an OSError subclass,
        # the same contract read_text() had.
        (batch / session_storage.MANIFEST_NAME).mkdir(parents=True)
        assert session_storage._read_manifest(batch) is None
        assert session_storage._summarize_manifest(batch) is None

    def test_batch_id_disagreement_is_tolerated_by_the_reader(self, tmp_path: Path) -> None:
        """The reader returns the header untouched; refusing a disagreeing batch id
        is list_trash's decision, exercised by TestTrashIsolation."""
        lines = [json.dumps(self._header(batch_id="somebody-else"))]
        batch = self._batch(tmp_path, lines)
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert summary[0]["batch_id"] == "somebody-else"

    def test_corrupt_lines_are_counted_and_logged_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#6292 item 3: mid-file corruption is no longer silent — one aggregated
        warning per read, counting only genuinely corrupt lines."""
        lines = [
            json.dumps(self._header()),
            "garbage {",
            json.dumps(self._entry("aaaa1111", [4])),
            "more garbage {",
        ]
        batch = self._batch(tmp_path, lines)
        with caplog.at_level("WARNING", logger="kiro_crew.session_storage"):
            summary = session_storage._summarize_manifest(batch)
        assert summary is not None and summary[1] == 1
        warnings = [r for r in caplog.records if "unparseable" in r.getMessage()]
        assert len(warnings) == 1
        assert "skipped 2 unparseable" in warnings[0].getMessage()

    def test_a_trailing_partial_line_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A crash mid-append is EXPECTED and tolerated; warning for it on every
        storage-screen open would be recurring noise about a normal state."""
        lines = [
            json.dumps(self._header()),
            json.dumps(self._entry("aaaa1111", [4])),
            '{"uid": "trunc',
        ]
        batch = self._batch(tmp_path, lines, terminated=False)
        with caplog.at_level("WARNING", logger="kiro_crew.session_storage"):
            summary = session_storage._summarize_manifest(batch)
        assert summary is not None and summary[1] == 1
        assert not [r for r in caplog.records if "unparseable" in r.getMessage()]

    def test_unicode_line_boundaries_split_records_like_splitlines_did(
        self, tmp_path: Path
    ) -> None:
        """The old reader split on str.splitlines boundaries (\\u2028, \\x1c, ...).
        Two records separated by one must still parse as two records, not be
        rejected as one malformed line."""
        entry = self._entry("aaaa1111", [7])
        blob = (
            json.dumps(self._header())
            + "\u2028"
            + json.dumps(entry)
            + "\x1c"
            + json.dumps(self._entry("bbbb2222", [3]))
        )
        batch = self._batch(tmp_path, [blob])
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert (summary[1], summary[2]) == (2, 10)

    def test_an_oversized_record_aborts_the_batch_not_materialised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest lives in an agent-writable tree: one multi-GB line with no
        newline must not be read whole. Past the cap the batch is treated as
        having no readable manifest — a crafted tail sitting exactly past a
        cap boundary cannot be smuggled in as its own forged record."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        forged_tail = json.dumps(
            {"uid": "evil0000", "files": [{"rel": "r", "origin": "o", "bytes": 999}]}
        )
        # 256 filler chars put the forged record exactly after the first
        # cap-sized read of this single (newline-free) oversized line.
        giant = "x" * 256 + forged_tail
        lines = [
            json.dumps(self._header()),
            giant,
            json.dumps(self._entry("aaaa1111", [5])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_undecodable_bytes_still_propagate(self, tmp_path: Path) -> None:
        """read_text raised UnicodeDecodeError on corrupt bytes and the streaming
        readers must keep that contract rather than silently absorbing it."""
        batch = tmp_path / self._BATCH_ID
        batch.mkdir(parents=True)
        (batch / session_storage.MANIFEST_NAME).write_bytes(b'{"schema": 1}\n\xff\xfe garbage\n')
        with pytest.raises(UnicodeDecodeError):
            session_storage._read_manifest(batch)
        with pytest.raises(UnicodeDecodeError):
            session_storage._summarize_manifest(batch)

    def test_an_oserror_mid_iteration_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """read_text failed as one whole-file operation; the streaming readers must
        map a read error DURING iteration to the same None, not leak it."""
        batch = self._batch(tmp_path, [json.dumps(self._header())])

        class _FailsMidRead:
            def __init__(self) -> None:
                self.calls = 0

            def readline(self, _cap: int) -> str:
                self.calls += 1
                if self.calls == 1:
                    return json.dumps({"schema": session_storage.MANIFEST_SCHEMA}) + "\n"
                raise OSError("device gone")

            def __enter__(self) -> "_FailsMidRead":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(Path, "open", lambda *a, **k: _FailsMidRead())
        assert session_storage._read_manifest(batch) is None
        assert session_storage._summarize_manifest(batch) is None

    def test_list_trash_never_materialises_the_entry_list(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The memory fix itself: the listing must go through the count-only
        reader. Reverting it to _read_manifest would pass every aggregate check,
        so pin the path by making the full reader unreachable from list_trash."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        def _forbidden(batch: Path) -> None:
            raise AssertionError("list_trash must not materialise manifest entries")

        monkeypatch.setattr(session_storage, "_read_manifest", _forbidden)
        listed = session_storage.list_trash()
        assert len(listed) == 1 and listed[0].sessions == 1

    def test_a_cap_boundary_read_does_not_destroy_a_record_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT round-2 finding 1: a cap-sized read that cuts a physical line
        mid-way must not condemn the bounded, individually valid records inside
        it. A 255-char record + \\u2028 + another record on one physical line,
        with the cap at 256, must yield both records."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        pad = "p" * (255 - len('{"uid": "aaaa1111", "files": [], "": ""}'))
        first = '{"uid": "aaaa1111", "files": [], "' + pad + '": ""}'
        assert len(first) == 255
        second = json.dumps(self._entry("bbbb2222", [9]))
        lines = [json.dumps(self._header()), first + "\u2028" + second]
        batch = self._batch(tmp_path, lines)
        summary = session_storage._summarize_manifest(batch)
        assert summary is not None
        assert (summary[1], summary[2]) == (2, 9)

    def test_a_final_line_ended_by_a_unicode_boundary_is_complete_corruption(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """GPT round-2 finding 2: 'garbage\\u2028' at EOF IS terminated under
        splitlines semantics — a complete corrupt record that must warn, not a
        crash-partial that hides at debug."""
        blob = json.dumps(self._header()) + "\n" + "garbage {\u2028"
        batch = tmp_path / self._BATCH_ID
        batch.mkdir(parents=True, exist_ok=True)
        (batch / session_storage.MANIFEST_NAME).write_text(blob, encoding="utf-8")
        with caplog.at_level("WARNING", logger="kiro_crew.session_storage"):
            summary = session_storage._summarize_manifest(batch)
        assert summary is not None and summary[1] == 0
        assert [r for r in caplog.records if "unparseable" in r.getMessage()]

    def test_a_record_spanning_many_cap_reads_aborts_without_materialising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record several multiples of the cap long aborts the read (the batch
        is unreadable) without its bytes ever being concatenated whole."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        lines = [
            json.dumps(self._header()),
            '{"uid": "' + "x" * 1500 + '"}',
            json.dumps(self._entry("aaaa1111", [5])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_even_a_valid_record_longer_than_the_cap_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap is a contract on parsed record size: a syntactically VALID
        record between cap and 2x cap (reachable because the carry-over buffer
        admits up to two cap-sized reads) must abort the read, NOT be silently
        skipped — restore() rewrites the manifest from parsed records, so a
        skipped record's staged files would be orphaned."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        valid_oversized = '{"uid": "bbbb2222", "pad": "' + "y" * 380 + '", "files": []}'
        assert 256 < len(valid_oversized) < 512
        lines = [
            json.dumps(self._header()),
            valid_oversized,
            json.dumps(self._entry("aaaa1111", [5])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_an_oversized_record_makes_restore_refuse_not_orphan(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The blocked data-loss seam, end to end: a manifest with an oversized
        record must make restore() refuse loudly. Skipping the record instead
        would let a partial restore REWRITE the manifest without it, permanently
        orphaning its staged files. The manifest must also be left untouched."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"], reason="manual", index=_index(), now=_NOW
        )
        manifest = session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        inflated_uid = entry["uid"]
        intact_uid = "bbbb2222" if inflated_uid == "aaaa1111" else "aaaa1111"
        entry["pad"] = "y" * 600
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        before = manifest.read_bytes()
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)

        # Restoring the INTACT session is the dangerous path: with the oversized
        # record merely skipped, this would succeed and rewrite the manifest from
        # the parsed records only — erasing the skipped record's metadata.
        with pytest.raises(SessionStorageError):
            session_storage.restore(batch.batch_id, [intact_uid])

        assert manifest.read_bytes() == before

    def test_an_unterminated_oversized_record_at_eof_still_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-cap record with NO final newline must abort like any other
        oversized record, not degrade into a silently dropped 'trailing partial'
        — silent dropping is the exact shape that lets a restore rewrite orphan
        staged files."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        valid_oversized = '{"uid": "bbbb2222", "pad": "' + "y" * 560 + '", "files": []}'
        lines = [json.dumps(self._header()), valid_oversized]
        batch = self._batch(tmp_path, lines, terminated=False)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_an_oversized_sibling_aborts_even_across_a_unicode_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'x'*cap + \\u2028 + valid record on one physical line: the oversized
        piece aborts the whole batch (fail-closed) — the record past the boundary
        is NOT salvaged, because salvaging some records while dropping others is
        exactly the shape that lets a partial restore orphan staged files."""
        monkeypatch.setattr(session_storage, "_MANIFEST_RECORD_CAP", 256)
        lines = [
            json.dumps(self._header()),
            "x" * 300 + "\u2028" + json.dumps(self._entry("aaaa1111", [6])),
        ]
        batch = self._batch(tmp_path, lines)
        assert session_storage._summarize_manifest(batch) is None
        assert session_storage._read_manifest(batch) is None

    def test_list_trash_aggregates_match_the_staged_batch(self, stores: tuple[Path, Path]) -> None:
        """End-to-end proof that the count-only path reports the same numbers the
        entry-list path did: the listing agrees with the batch returned by the move."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=100, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=300, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"], reason="manual", index=_index(), now=_NOW
        )

        listed = session_storage.list_trash()

        assert len(listed) == 1
        assert listed[0].sessions == batch.sessions == 2
        assert listed[0].bytes == batch.bytes
        assert listed[0].bytes > 0
