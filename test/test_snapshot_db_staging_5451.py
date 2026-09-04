"""Snapshot's creation-side SQLite staging: read-only, descriptor-verified, WAL-complete.

Issue #5451. Two claims were made about `snapshot_main`'s database staging, and they need
opposite treatment, which is why they are tested separately here.

The FIRST is real and these tests pin it: the core-file staging path opened the live
database by name and READ-WRITE. That is not merely more authority than a backup needs --
it mutates the operator's live data. With a `-wal` left unreplayed by a killed writer, the
read-write open recovers the log into the main file and unlinks the log, so `kirocrew
snapshot` rewrites the database it was asked to read.

The SECOND -- "a WAL check races the copy" -- is a property of a check-then-copy-BYTES
design, and the fix is not to build one. `TestTheBackupApiIsAlreadyWalConsistent` is the
evidence for that: the backup API reads a snapshot that already includes rows living only
in the log, so there is no check to race. Those are contract tests, not regression tests,
and are labelled as such -- they pass on base too. Their job is to stop someone
"fixing" the WAL race later by replacing `backup()` with a checkpoint and a byte copy.
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
    # `cwd=tmp_path`: the child inherits this process's directory otherwise, which under
    # pytest is the repository checkout, so any relative artifact it created would land in
    # the working tree. It only touches the absolute db path today; pinning the cwd keeps
    # that true if the script grows. Review's finding.
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
    """The regression tests for #5451's first failure mode."""

    def test_a_snapshot_leaves_the_live_database_byte_identical(
        self, home: Path, tmp_path: Path
    ) -> None:
        """A backup must not modify what it backs up.

        This is the test that discriminates the fix. On base the core-file path connects
        READ-WRITE, so opening a database whose -wal holds unreplayed frames recovers them
        into the main file and unlinks the log: both fingerprints below change. Read-only
        cannot perform that recovery, so both survive untouched.
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

        This replaces a narrower test of the pre-staging WAL checkpoint, which review had
        removed by then. That checkpoint was the only write `kirocrew snapshot` made to live
        data, and it could not be made safe: a checkpoint cannot run read-only, and
        verifying the name first does not help because SQLite re-resolves it, so a swap in
        that window put the WRITE into whatever the name then pointed at.

        Asserting through `snapshot_main` rather than `_build_snapshot` is the whole point
        -- the checkpoint lived in the command, so a test that called the builder directly
        could never have caught it. With it gone, both the main file and the `-wal` survive
        a full snapshot untouched.
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
        """A relocated data home reaches a decision instead of dying on a traceback.

        History worth keeping, because it is the reason this test is shaped the way it is.
        An intermediate revision verified the checkpoint's path with `_chain_is_link_free`,
        which then called `open_dir_pinned` outside its own try and resolves only the PARENT
        chain -- so a data home that is itself a symlink raised `PinnedPathRefusal` from a
        call site above `snapshot_main`'s try, and the command exited on a traceback.

        That root open is now wrapped, but it catches `OSError` only and still lets
        `PinnedPathRefusal` through on purpose, so a symlinked home remains a refusal
        `snapshot_main` handles and audits rather than something this helper swallows.
        `test_a_source_root_that_disappears_is_named_not_a_traceback` pins the `OSError` half.

        The reachability argument is what makes this test real. `_valid_override_home()`
        ends in `.resolve()`, so a `KIROCREW_HOME` override can never present a symlinked
        home -- a first version of this test used it and passed against the broken code,
        proving nothing. The DEFAULT route does not resolve: `_default_home()` returns
        `Path.home() / ".kiro" / "crew"` verbatim. Patching `_mc_dir` reproduces that shape.

        Stated plainly: with the checkpoint removed there is no longer a guard above the
        try for this to discriminate, so it is now a behavioural contract test rather than a
        regression test for that specific line. It is kept because a symlinked data home
        must not crash the command however the internals are arranged.
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
        """A declared component file that is not SQLite fails the snapshot.

        An earlier revision of this change degraded it to a byte copy, "matching" the tree
        pass. That is backwards, and review caught the consequence -- see the sibling test
        below for the chain it opens.
        """
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

        Degrading made the snapshot SUCCEED, so `--keep N` counted the new archive as the
        newest backup and pruned a real one, while restore refuses that archive at its
        strict database validation -- leaving nothing restorable after a command that
        printed success. Asserted through the command rather than on the exception type,
        because the ORDERING is what protects the operator's data.
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

        A `.db` under a tree that is NOT in `PRODUCT_TREE_DATABASES` is incidental -- a file
        the operator happens to keep in their workspace -- and refusing the whole snapshot
        over it would be an outage rather than a safeguard. `_setup_fake_kirocrew` leaves a
        stub `kb.sqlite3` under workspace/knowledge that is not a real database; it must ride
        as bytes.

        Set membership is what draws the line, not tree-versus-core: the sibling test
        `test_a_corrupt_product_tree_database_fails_the_snapshot` pins that a corrupt
        `knowledge.db` in the same directory FAILS the snapshot, because that path is one of
        ours and restore validates it as strictly as `memory.db`.
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

        The two `sqlite3.connect` calls used to sit OUTSIDE the try that translates errors
        into `DatabaseCopyFailed`. `snapshot_main` handles `PinnedPathRefusal`,
        `UnsafeComponentRoot`, `DatabaseCopyFailed` and `_ArchiveTooLarge` -- not a bare
        `sqlite3.OperationalError` -- so the command exited on a traceback instead of naming
        the database. Review's finding.

        Worth stating that `mode=ro` WIDENED this window rather than narrowing it: a
        read-write open would have silently created an empty database where a read-only one
        fails outright. The fix belongs with the change that made it matter.

        The vanish is injected for real -- the file is unlinked between the probe's connect
        and the copy's -- rather than by raising a synthetic error, so the exception is the
        one SQLite actually produces.
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

        `_chain_is_link_free` calls `open_dir_pinned` OUTSIDE its own try, and
        `open_dir_pinned` translates only `ELOOP`/`ENOTDIR` into `PinnedPathRefusal` and
        re-raises every other `OSError`. So a root lost to a concurrent rename or removal
        left `ENOENT` escaping as `FileNotFoundError`.

        The reachability argument is what makes this a regression test rather than a
        contract one. `snapshot_main` wraps `_build_snapshot` in a try whose handlers are
        `PinnedPathRefusal`, `UnsafeComponentRoot` and `DatabaseCopyFailed`; its
        `except (tarfile.TarError, OSError, EOFError)` belongs to a LATER, separate try that
        does not cover `_build_snapshot`. This change is what routes the helper at the live
        data home -- the sibling tree pass passes a staging directory the command itself
        just created -- so the operator-facing trigger arrives with this diff and the
        containment belongs with it. Review's finding.

        The root is removed for real rather than by raising a synthetic error, so the
        exception is the one the OS actually produces.

        Both outcomes are asserted because they must stay OPPOSITE: a DECLARED database is
        a hard failure, while one the tree walk merely found is recorded and rides as bytes.
        Collapsing them is what let `--keep` prune an operator's last complete archive.
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

        `PRODUCT_TREE_DATABASES` says of itself that everything in it is "validated as
        strictly as `memory.db`", and the restore side already enforces that --
        `_refuse_unless_sound(src, rel, strict=rel in PRODUCT_TREE_DATABASES)`, asserted by
        `test_an_unopenable_knowledge_database_is_refused`. The creation path staged those
        same files leniently, so a corrupt `workspace/knowledge/knowledge.db` produced an
        archive that reported success and that restore is GUARANTEED to refuse.

        That combination is worse than either half. `--keep N` counts the new archive as the
        newest backup and prunes a real one, so the operator loses their last restorable
        copy to a command that said it worked. Review's finding.

        This is the boundary of the tree/core asymmetry, not a repeal of it: the sibling
        test above pins that an incidental `kb.sqlite3` still rides as bytes. The set
        membership is what separates the two, which is why this asserts the path in the set
        rather than any `.db` under a tree.
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

        `PRAGMA schema_version` only proves the file parses. Measured with the interior
        pages overwritten and the header left intact: `schema_version` answered normally,
        `backup()` succeeded and staged all bytes, and `integrity_check` reported "database
        disk image is malformed" on both source and copy. So the archive reported success
        and restore was guaranteed to refuse it -- `--keep N` then prunes a real backup in
        favour of one that cannot be restored. Review's finding.

        The check runs on the STAGED COPY with restore's own predicate, so this asserts the
        two ends of the invariant now agree rather than merely that creation got stricter.
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

        # Precondition: the weaker probe this replaces still passes, so the test is
        # asserting the new check and not some earlier guard.
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

        Measured, and worse than "restore would refuse it": SQLite opens a zero-byte file as
        a valid EMPTY database, `PRAGMA schema_version` answers `(0,)`, and `backup()` does
        not preserve the emptiness -- it produced a 4096-byte staged copy that
        `integrity_check` called `ok`. Restore's own zero-byte guard reads the size of the
        ARCHIVED file, so it sees 4096 and ACCEPTS, then installs an empty database over
        live data and reports success.

        So this one is not a pruning hazard, it is silent data loss on restore, and the
        source is the only end where the condition is visible -- which is why the check sits
        there rather than on the copy.
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

        # Probed on a name of its own. Probing `home/memory.db` -- which the fixture
        # already created -- raised FileExistsError, was caught by the same handler, and
        # skipped the test with "needs privilege": a green run that asserted nothing.
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

        An intermediate revision recorded it in `MANIFEST.json` and let the snapshot
        succeed, on the issue's own reasoning that "an absent database with a recorded
        reason is recoverable information". Review showed the flaw: it is recoverable
        information only while the operator still HAS the archive it could be recovered
        from, and a successful snapshot with `--keep 1` prunes exactly that.
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

        Deleting the byte copy would be data loss, so it stays -- and that is exactly why
        the degradation has to be on the record. Before this, `_restage_databases` had no
        reporter at all and the outcome existed only as a `continue`.
        """
        # Scoped to the TREE entry. A blanket `lambda: False` also trips the core path,
        # which now raises for an unverifiable required database, so the snapshot would die
        # on `memory.db` before the tree pass ran and this test would assert nothing about
        # the manifest.
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
