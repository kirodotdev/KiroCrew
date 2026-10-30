"""Snapshot's creation-side SQLite staging: read-only, descriptor-verified, WAL-complete.

Staging must never write to the live database. A read-write open of a database whose
`-wal` a killed writer left unreplayed recovers the log into the main file and unlinks
it, so a backup would rewrite what it was asked to read. The backup API already reads
rows that live only in the log, so there is no WAL check to race; these tests also stop
anyone replacing `backup()` with a checkpoint and a byte copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
from contextlib import closing
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import pinned_fs
from kiro_crew import snapshot as snap

pinned_only = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="requires O_DIRECTORY, O_NOFOLLOW, dir_fd and fd-listdir support (POSIX)",
)

#: Staging refuses by default where a directory cannot be pinned, so a test driving a real
#: snapshot has to say which platform contract it asserts. These tests assert product
#: behaviour true on both, and exercise the declared by-name path on Windows.
UNPINNED_OK = {"allow_unpinned": True}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    _setup_fake_kirocrew(d)
    return d


def _fingerprint(path: Path) -> tuple[int, str] | None:
    """Size and content hash, or None when absent. Content, not mtime: the point is
    whether the BYTES changed, and an mtime comparison would also fire on a touch."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return len(data), hashlib.sha256(data).hexdigest()


#: Writes committed rows into the -wal and then dies WITHOUT closing the connection, so
#: the log survives on disk with frames not yet folded into the main file. That is the
#: state a killed gateway leaves behind, and it is the state in which a read-write open
#: performs recovery -- which is a WRITE to the live database.
_DIRTY_WRITER = textwrap.dedent("""
    import os, sqlite3, sys
    c = sqlite3.connect(sys.argv[1], isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA wal_autocheckpoint=0;")
    for i in range(200):
        c.execute(
            "INSERT INTO semantic_memory"
            " (key, value_json, confidence, source, created_at, updated_at)"
            " VALUES (?, '\\"wal\\"', 0.5, 'wal', '2026-01-01', '2026-01-01')",
            (f"wal.only.{i}",),
        )
    os._exit(0)
    """).strip()


def _leave_unreplayed_wal(db: Path, tmp_path: Path) -> None:
    """Leave *db* with a non-empty -wal and no live connection holding it.

    Done in a subprocess that hard-exits, because an in-process connection closing
    normally is the LAST connection and SQLite checkpoints and unlinks the log on the way
    out -- which is the very mutation these tests measure, applied by the fixture instead
    of by the code under test.
    """
    with closing(sqlite3.connect(str(db))) as c:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    script = tmp_path / "dirty_writer.py"
    script.write_text(_DIRTY_WRITER, encoding="utf-8")
    # `cwd=tmp_path`: otherwise the child inherits the checkout as its cwd and any
    # relative artifact it created would land in the working tree.
    subprocess.run([sys.executable, str(script), str(db)], check=True, cwd=tmp_path)
    assert (_fingerprint(Path(str(db) + "-wal")) or (0, ""))[
        0
    ] > 0, "fixture did not leave an unreplayed -wal; the test below would prove nothing"


def _staged_member(archive: Path, name: str) -> bytes:
    with tarfile.open(archive) as tf:
        root = os.path.commonprefix(tf.getnames()).rstrip("/")
        extracted = tf.extractfile(f"{root}/{name}")
        assert extracted is not None
        return extracted.read()


def _manifest(archive: Path) -> dict:
    with tarfile.open(archive) as tf:
        root = os.path.commonprefix(tf.getnames()).rstrip("/")
        member = tf.extractfile(f"{root}/MANIFEST.json")
        assert member is not None
        return json.load(member)


class TestStagingDoesNotWriteToTheLiveDatabase:
    """Snapshot staging does not write to the live database."""

    def test_a_snapshot_leaves_the_live_database_byte_identical(
        self, home: Path, tmp_path: Path
    ) -> None:
        """A backup must not modify what it backs up.

        A read-write open of a database whose -wal holds unreplayed frames recovers them
        into the main file and unlinks the log, changing both fingerprints below. A
        read-only open cannot, so both survive untouched.
        """
        db = home / "memory.db"
        _leave_unreplayed_wal(db, tmp_path)
        before_main = _fingerprint(db)
        before_wal = _fingerprint(Path(str(db) + "-wal"))

        snap._build_snapshot(
            home, tmp_path / "out", "ro-archive", selected=["memory"], **UNPINNED_OK
        )

        assert _fingerprint(db) == before_main, (
            "staging rewrote the live memory.db -- a read-write open recovered the -wal "
            "into the main file. Staging only ever needs to read; open it mode=ro."
        )
        assert _fingerprint(Path(str(db) + "-wal")) == before_wal, (
            "staging checkpointed and/or unlinked the live -wal. That is the operator's "
            "in-flight transaction state, destroyed as a side effect of a backup."
        )

    def test_the_staged_copy_still_holds_the_rows_that_lived_only_in_the_wal(
        self, home: Path, tmp_path: Path
    ) -> None:
        """Read-only costs nothing in completeness.

        The pairing matters: without this, the test above could be satisfied by not
        reading the database at all. 200 rows exist ONLY in the -wal when staging runs,
        and every one of them has to reach the archive.
        """
        db = home / "memory.db"
        _leave_unreplayed_wal(db, tmp_path)

        archive = snap._build_snapshot(
            home, tmp_path / "out", "complete-archive", selected=["memory"], **UNPINNED_OK
        )

        staged = tmp_path / "staged.db"
        staged.write_bytes(_staged_member(archive, "memory.db"))
        with closing(sqlite3.connect(str(staged))) as c:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            wal_rows = c.execute(
                "SELECT count(*) FROM semantic_memory WHERE key LIKE 'wal.only.%'"
            ).fetchone()[0]
        assert wal_rows == 200, (
            f"only {wal_rows}/200 WAL-resident rows reached the archive -- the copy is "
            "not reading the log, so the bundle is silently short of committed rows"
        )

    def test_the_command_never_writes_to_the_live_database(
        self, home: Path, tmp_path: Path
    ) -> None:
        """The whole COMMAND is read-only, not just the staging copy.

        Driven through `snapshot_main`, not `_build_snapshot`: a pre-staging WAL
        checkpoint would live in the command, and a checkpoint cannot run read-only, so
        only a command-level test sees it.
        """
        db = home / "memory.db"
        _leave_unreplayed_wal(db, tmp_path)
        before_main = _fingerprint(db)
        before_wal = _fingerprint(Path(str(db) + "-wal"))

        rc = snap.snapshot_main([str(tmp_path / "out"), "--allow-unpinned-staging"])

        assert rc == 0, f"the snapshot did not succeed (rc={rc})"
        assert _fingerprint(db) == before_main, "the command rewrote the live memory.db"
        assert _fingerprint(Path(str(db) + "-wal")) == before_wal, (
            "the command checkpointed or unlinked the live -wal -- that is the operator's "
            "in-flight transaction state, destroyed as a side effect of a backup"
        )

    def test_a_symlinked_data_home_does_not_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A symlinked data home reaches a decision instead of dying on a traceback.

        `PinnedPathRefusal` from the root open is left for `snapshot_main` to handle and
        audit. A `KIROCREW_HOME` override is `.resolve()`d and can never present a
        symlink; the default `_default_home()` is not, so the test patches `_mc_dir`.
        """
        if not pinned_fs.supports_pinned_walk():
            pytest.skip("the refusal only arises where descriptor pinning is available")
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        _setup_fake_kirocrew(real_home)
        linked = tmp_path / "linked-home"
        try:
            linked.symlink_to(real_home)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("creating a symlink needs privilege on this host")
        monkeypatch.setattr(snap, "_mc_dir", lambda: linked)

        rc = snap.snapshot_main([str(tmp_path / "out"), "--allow-unpinned-staging"])
        assert rc in (0, 1), f"expected a clean exit code, got {rc!r}"


class TestACorruptCoreDatabaseFailsInsteadOfPruningAGoodBackup:
    def test_a_corrupt_core_database_fails_the_snapshot(self, home: Path, tmp_path: Path) -> None:
        """A declared component file that is not SQLite fails the snapshot, rather than
        riding as bytes the way an incidental tree file does (see the sibling below)."""
        (home / "memory.db").write_bytes(b"this is not a database\n")
        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(
                home, tmp_path / "out", "corrupt", selected=["memory"], **UNPINNED_OK
            )
        assert "memory.db" in str(excinfo.value)

    def test_the_failure_happens_before_any_backup_is_pruned(
        self, home: Path, tmp_path: Path
    ) -> None:
        """Why it raises: `--keep` pruning must never run for an unrestorable archive.

        A succeeding snapshot would let `--keep N` prune a real backup in favour of an
        archive restore refuses. Asserted through the command, because the ORDERING is
        what protects the operator's data.
        """
        out = tmp_path / "out"
        out.mkdir()
        previous = out / "kirocrew-snapshot-20260101-000000.tgz"
        previous.write_bytes(b"the operator's only good backup\n")
        (home / "memory.db").write_bytes(b"this is not a database\n")

        rc = snap.snapshot_main([str(out), "--keep", "1", "--allow-unpinned-staging"])

        assert rc != 0, "a snapshot over a corrupt core database reported success"
        assert previous.exists(), (
            "the pre-existing backup was pruned in favour of an archive that restore "
            "would refuse -- the data-loss chain the raise exists to prevent"
        )

    def test_a_tree_database_that_is_not_sqlite_still_rides_as_bytes(
        self, home: Path, tmp_path: Path, capsys
    ) -> None:
        """The other half of the asymmetry, asserted so nobody "fixes" it into symmetry.

        A `.db` under a tree that is NOT in `PRODUCT_TREE_DATABASES` is incidental, and
        refusing the whole snapshot over it would be an outage. `_setup_fake_kirocrew`
        leaves a stub `kb.sqlite3` that must ride as bytes; a corrupt `knowledge.db` (in
        the set) fails instead, pinned below.
        """
        archive = snap._build_snapshot(
            home, tmp_path / "out", "tree-degrade", selected=["memory"], **UNPINNED_OK
        )
        assert (
            _staged_member(archive, "workspace/knowledge/kb.sqlite3") == b"SQLite format 3\x00stub"
        ), "an incidental non-database in a tree was not carried through as bytes"
        assert "not a readable SQLite database" in capsys.readouterr().out

    def test_a_database_vanishing_after_the_probe_is_named_not_a_traceback(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A source lost between the probe and the copy is reported by name.

        Both `sqlite3.connect` calls must sit inside the try that translates errors into
        `DatabaseCopyFailed`; `snapshot_main` does not handle a bare
        `sqlite3.OperationalError`. The file is really unlinked between the two connects,
        so the exception is the one SQLite produces.
        """
        calls = {"n": 0}
        real_connect = sqlite3.connect

        class _VanishOnSecondConnect:
            def __getattr__(self, item: str) -> object:
                return getattr(sqlite3, item)

            @staticmethod
            def connect(*args: object, **kwargs: object) -> object:
                target = str(args[0]) if args else ""
                if "mode=ro" in target:
                    calls["n"] += 1
                    if calls["n"] == 2:
                        (home / "memory.db").unlink()
                return real_connect(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(snap, "sqlite3", _VanishOnSecondConnect())
        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(
                home, tmp_path / "out", "vanished", selected=["memory"], **UNPINNED_OK
            )
        assert calls["n"] >= 2, "the second (copy) connect never happened; nothing was proven"
        assert "memory.db" in str(excinfo.value)

    @pinned_only
    def test_a_source_root_that_disappears_is_named_not_a_traceback(
        self, home: Path, tmp_path: Path
    ) -> None:
        """A component root removed under the copy reaches a decision, not a traceback.

        `open_dir_pinned` re-raises every `OSError` but `ELOOP`/`ENOTDIR`, and
        `snapshot_main`'s handlers around `_build_snapshot` do not catch `OSError`, so a
        root lost to a concurrent removal must not escape as `FileNotFoundError`. The root
        is removed for real, so the exception is the one the OS produces.

        Both outcomes must stay OPPOSITE: a DECLARED database is a hard failure, while one
        the tree walk merely found is recorded and rides as bytes.
        """
        gone = tmp_path / "gone"
        gone.mkdir()
        src = gone / "memory.db"
        with closing(sqlite3.connect(src)) as db:
            db.execute("CREATE TABLE t (x)")
        gone_src = src

        shutil.rmtree(gone)
        assert not gone.exists(), "the root still exists; nothing would be proven"

        # A DECLARED component database: the snapshot must fail, by name.
        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._copy_database_consistently(
                gone_src,
                tmp_path / "dst.db",
                root=gone,
                rel_parts=("memory.db",),
                require_database=True,
            )
        assert "memory.db" in str(excinfo.value)

        # The same loss on the incidental tree path is a recorded refusal, not a failure.
        assert (
            snap._copy_database_consistently(
                gone_src,
                tmp_path / "dst2.db",
                root=gone,
                rel_parts=("memory.db",),
            )
            == snap.DB_UNSAFE_SOURCE
        )

    def test_a_corrupt_product_tree_database_fails_the_snapshot(
        self, home: Path, tmp_path: Path
    ) -> None:
        """A corrupt `knowledge.db` fails the snapshot instead of riding as bytes.

        Restore validates every `PRODUCT_TREE_DATABASES` path as strictly as `memory.db`,
        so creation must too: an archive restore is guaranteed to refuse would otherwise
        let `--keep N` prune the last restorable backup. Set membership, not tree-vs-core,
        draws the line; an incidental `kb.sqlite3` still rides as bytes (sibling above).
        """
        kdir = home / "workspace" / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        (kdir / "knowledge.db").write_bytes(b"torn, not a database")

        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(
                home, tmp_path / "out", "corrupt-product-tree", selected=["memory"], **UNPINNED_OK
            )
        assert "knowledge.db" in str(excinfo.value)

    def test_a_page_corrupt_required_database_fails_the_snapshot(
        self, home: Path, tmp_path: Path
    ) -> None:
        """A database that PARSES but is page-corrupt fails, instead of shipping.

        `PRAGMA schema_version` only proves the file parses: with interior pages
        overwritten it still answers and `backup()` succeeds, while `integrity_check`
        reports the image malformed. The check runs on the STAGED COPY with restore's own
        predicate, so creation and restore agree on what is sound.
        """
        live = home / "memory.db"
        with closing(sqlite3.connect(live)) as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
            db.executemany("INSERT INTO t (blob) VALUES (?)", [("x" * 400,) for _ in range(400)])
            db.commit()

        raw = bytearray(live.read_bytes())
        page = 4096
        assert len(raw) > 8 * page, "fixture too small to corrupt interior pages"
        for pageno in range(3, 8):
            start = pageno * page
            raw[start : start + 200] = b"\xde\xad\xbe\xef" * 50
        live.write_bytes(bytes(raw))

        # Precondition: the parse-only probe still passes, so only integrity_check
        # can be what refuses.
        with closing(sqlite3.connect(f"{live.absolute().as_uri()}?mode=ro", uri=True)) as probe:
            assert probe.execute("PRAGMA schema_version").fetchone() is not None

        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(
                home, tmp_path / "out", "page-corrupt", selected=["memory"], **UNPINNED_OK
            )
        assert "memory.db" in str(excinfo.value)

    def test_a_zero_byte_required_database_fails_the_snapshot(
        self, home: Path, tmp_path: Path
    ) -> None:
        """Zero bytes is the unsoundness neither `integrity_check` nor restore can see.

        SQLite opens a zero-byte file as a valid EMPTY database and `backup()` stages a
        4096-byte copy that `integrity_check` calls `ok`, so restore would accept it and
        install an empty database over live data. Only the source shows the condition,
        which is why the check sits there.
        """
        (home / "memory.db").write_bytes(b"")

        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(
                home, tmp_path / "out", "zero-byte", selected=["memory"], **UNPINNED_OK
            )
        assert "memory.db" in str(excinfo.value)

    def test_an_unreadable_core_database_still_fails_the_snapshot_loudly(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A readable database whose COPY fails must raise too.

        Distinct from the corrupt case: this file IS a valid database, so the probe
        succeeds and only `backup()` fails. Degrading it would ship a database without the
        `-wal` this module strips -- torn, inside an archive reporting success.

        Injected by shimming the module's own `sqlite3` reference rather than patching
        `sqlite3.Connection.backup`, which is an immutable type.
        """

        class _FailingBackup:
            def __init__(self, wrapped: sqlite3.Connection) -> None:
                self._wrapped = wrapped

            def __getattr__(self, item: str) -> object:
                return getattr(self._wrapped, item)

            def backup(self, *_a: object, **_k: object) -> None:
                raise sqlite3.OperationalError("database is locked")

        class _Shim:
            def __getattr__(self, item: str) -> object:
                return getattr(sqlite3, item)

            @staticmethod
            def connect(*args: object, **kwargs: object) -> object:
                conn = sqlite3.connect(*args, **kwargs)  # type: ignore[arg-type]
                return _FailingBackup(conn)

        monkeypatch.setattr(snap, "sqlite3", _Shim())
        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(home, tmp_path / "out", "loud", selected=["memory"], **UNPINNED_OK)
        assert "memory.db" in str(excinfo.value)


@pinned_only
class TestASourceThatCannotBeVerifiedIsRefusedAndRecorded:
    def test_a_core_name_swapped_for_a_link_after_the_screen_is_not_archived(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check-to-use window on the core path, made deterministic.

        The staging loop screens `memory.db` through its held descriptor and then hands the
        NAME to SQLite, which resolves it again. The swap is injected between those two
        resolutions -- immediately after the screen returns -- which is precisely the
        window an attacker with write access to the data home has. Without the helper's own
        chain check the second resolution lands on the victim and its rows ride the bundle
        under the name `memory.db`.
        """
        victim = tmp_path / "someone-elses.db"
        with closing(sqlite3.connect(str(victim))) as c:
            c.execute("CREATE TABLE secrets (v TEXT)")
            c.execute("INSERT INTO secrets VALUES ('not-the-operators-row')")

        real_stat_at = pinned_fs.stat_at
        swapped = {"done": False}

        def stat_at_then_swap(dir_fd: int, name: str):  # type: ignore[no-untyped-def]
            result = real_stat_at(dir_fd, name)
            if name == "memory.db" and not swapped["done"]:
                swapped["done"] = True
                (home / "memory.db").unlink()
                (home / "memory.db").symlink_to(victim)
            return result

        # Probed on a name of its own: `home/memory.db` already exists, and the
        # FileExistsError would be caught here and skip the test silently.
        probe = tmp_path / "symlink-support-probe"
        try:
            probe.symlink_to(victim)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("creating a symlink needs privilege on this host")
        probe.unlink()

        monkeypatch.setattr(snap.pinned_fs, "stat_at", stat_at_then_swap)
        with pytest.raises(snap.DatabaseCopyFailed) as excinfo:
            snap._build_snapshot(home, tmp_path / "out", "swapped", selected=["memory"])
        assert swapped["done"], "the swap never fired; this test proves nothing"
        assert "memory.db" in str(excinfo.value)

    def test_a_swapped_core_name_does_not_publish_an_archive_that_prunes_a_good_one(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why the swap RAISES instead of being recorded as an omission.

        A recorded omission is recoverable only while the operator still has an older
        archive, and a successful snapshot with `--keep 1` prunes exactly that.
        """
        victim = tmp_path / "someone-elses.db"
        with closing(sqlite3.connect(str(victim))) as c:
            c.execute("CREATE TABLE secrets (v TEXT)")

        probe = tmp_path / "symlink-support-probe"
        try:
            probe.symlink_to(victim)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("creating a symlink needs privilege on this host")
        probe.unlink()

        out = tmp_path / "out"
        out.mkdir()
        previous = out / "kirocrew-snapshot-20260101-000000.tgz"
        previous.write_bytes(b"the operator's only good backup\n")

        real_stat_at = pinned_fs.stat_at
        swapped = {"done": False}

        def stat_at_then_swap(dir_fd: int, name: str):  # type: ignore[no-untyped-def]
            result = real_stat_at(dir_fd, name)
            if name == "memory.db" and not swapped["done"]:
                swapped["done"] = True
                (home / "memory.db").unlink()
                (home / "memory.db").symlink_to(victim)
            return result

        monkeypatch.setattr(snap.pinned_fs, "stat_at", stat_at_then_swap)
        rc = snap.snapshot_main([str(out), "--keep", "1"])

        assert swapped["done"], "the swap never fired; this test proves nothing"
        assert rc != 0, "a snapshot whose required database was omitted reported success"
        assert previous.exists(), (
            "the operator's previous backup was pruned in favour of an archive missing the "
            "database it claims to contain"
        )

    def test_a_tree_database_that_cannot_be_verified_is_recorded_in_the_manifest(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tree pass keeps its byte copy, but the archive stops claiming consistency.

        Deleting the byte copy would be data loss, so it stays, and the degradation has
        to be on the record.
        """
        # Scoped to the TREE entry: a blanket `lambda: False` also trips the core path,
        # which raises on `memory.db` before the tree pass runs.
        real_chain_is_link_free = snap._chain_is_link_free

        def only_the_tree_db_is_unverifiable(root: Path, rel_parts: tuple[str, ...]) -> bool:
            if rel_parts and rel_parts[-1] == "kb.sqlite3":
                return False
            return real_chain_is_link_free(root, rel_parts)

        monkeypatch.setattr(snap, "_chain_is_link_free", only_the_tree_db_is_unverifiable)
        archive = snap._build_snapshot(
            home, tmp_path / "out", "unverified-tree", selected=["memory"], **UNPINNED_OK
        )
        skipped = _manifest(archive)["skipped"]
        assert any(
            e["reason"] == snap.SKIP_DB_UNPINNED_SOURCE and e["path"].endswith("kb.sqlite3")
            for e in skipped
        ), f"the tree pass's degradation is absent from MANIFEST.json: {skipped}"
