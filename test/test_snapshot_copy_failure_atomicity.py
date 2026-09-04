"""Tests for the cycle-5 review findings.

Two defects that shared one shape: a failure was absorbed into a success. A failed
database copy warned and continued, and a failed tree copy left the tree deleted and
exited zero.
"""

from __future__ import annotations

import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import snapshot as snap


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    _setup_fake_kirocrew(d)
    return d


def _sound_db_bytes(path):
    """A real SQLite database, since replace mode now refuses an unsound incoming one.

    These tests exercise rollback, not validation, so the bundle they feed in has to get
    past the pre-flight to reach the code under test.
    """
    with sqlite3.connect(str(path)) as c:
        c.execute("CREATE TABLE t (v TEXT)")
        c.execute("INSERT INTO t VALUES ('incoming')")
    return path.read_bytes()


class TestAFailedDatabaseCopyIsNotSilentlyDowngraded:
    def _db(self, path):
        with sqlite3.connect(str(path)) as c:
            c.execute("CREATE TABLE t (v TEXT)")
            c.execute("INSERT INTO t VALUES ('real')")
        return path

    def _staged_pair(self, tmp_path):
        """Mirror production: the tree copy already placed a byte copy at dst, so dst is
        itself a valid database before _restage_databases replaces it."""
        import shutil

        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir()
        dst.mkdir()
        self._db(src / "memory.db")
        shutil.copy2(src / "memory.db", dst / "memory.db")
        return src, dst

    def test_a_non_database_file_keeps_the_plain_copy(self, tmp_path, capsys):
        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir()
        dst.mkdir()
        (src / "notes.db").write_text("this is not a database", encoding="utf-8")
        (dst / "notes.db").write_text("this is not a database", encoding="utf-8")
        snap._restage_databases(src, dst, bundle_root=dst)
        assert "not a readable SQLite database" in capsys.readouterr().out
        assert (dst / "notes.db").read_text(encoding="utf-8") == "this is not a database"

    def test_a_real_database_is_copied_consistently(self, tmp_path):
        src, dst = self._staged_pair(tmp_path)
        snap._restage_databases(src, dst, bundle_root=dst)
        with closing(sqlite3.connect(str(dst / "memory.db"))) as c:
            assert c.execute("SELECT v FROM t").fetchone()[0] == "real"

    def _break_backup(self, monkeypatch, message):
        """`sqlite3.Connection` is immutable, so the failure is injected at the
        `connect` seam the module actually calls.

        The exception class comes from ``snap.sqlite3``, NOT this file's ``import
        sqlite3``: the package binds a different DB-API implementation, so an error
        built from the stdlib module is not caught by production's ``except
        sqlite3.Error`` at all. A first version of these tests did exactly that and
        passed against a mutant that swallowed the failure -- the error escaped for the
        wrong reason.
        """
        prod = snap.sqlite3
        real_connect = prod.connect
        exc = prod.OperationalError(message)

        class _NoBackup:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def backup(self, *a, **kw):
                raise exc

        monkeypatch.setattr(prod, "connect", lambda *a, **kw: _NoBackup(real_connect(*a, **kw)))

    def test_a_failed_backup_on_a_real_database_propagates(self, tmp_path, monkeypatch):
        """The dangerous case: readable database, failing copy. The staged file is a raw
        byte copy without its WAL sidecars, so success here would ship a torn database.
        """
        src, dst = self._staged_pair(tmp_path)
        self._break_backup(monkeypatch, "database is locked")
        with pytest.raises(snap.DatabaseCopyFailed) as e:
            snap._restage_databases(src, dst, bundle_root=dst)
        assert e.value.path.name == "memory.db", "the error must name the file"

    def test_the_probe_does_not_mask_a_backup_failure_as_a_plain_file(
        self, tmp_path, monkeypatch, capsys
    ):
        """Guards the specific regression: catching every sqlite3.Error again."""
        src, dst = self._staged_pair(tmp_path)
        self._break_backup(monkeypatch, "disk I/O error")
        with pytest.raises(snap.DatabaseCopyFailed):
            snap._restage_databases(src, dst, bundle_root=dst)
        assert "not a readable SQLite database" not in capsys.readouterr().out

    def test_the_command_reports_it_instead_of_crashing(self, home, tmp_path, monkeypatch, capsys):
        """Propagating is right, but the operator must get a message and a nonzero exit,
        not a traceback: `snapshot` is a command, and a traceback reads as a crash the
        operator would retry rather than act on."""
        db = home / "workspace" / "knowledge" / "knowledge.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(snap.sqlite3.connect(str(db))) as c:
            c.execute("CREATE TABLE t (v TEXT)")
        self._break_backup(monkeypatch, "database is locked")

        rc = snap.snapshot_main(
            [str(tmp_path / "out"), "--components", "memory", *unpinnable_argv()]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        # Core files are staged before trees, so memory.db is the one that fails first.
        # Both paths raise the same typed error -- there were two `backup()` call sites
        # and wrapping only the tree one left this path exiting on a traceback.
        assert "memory.db" in out, "the failing database must be named"
        assert "No bundle was written" in out
        assert (
            not list((tmp_path / "out").glob("*.tar.gz")) if (tmp_path / "out").exists() else True
        )

    def test_a_locked_database_raises_instead_of_being_called_a_plain_file(self, tmp_path, capsys):
        """The dangerous misclassification: "database is locked" is a DatabaseError too,
        so a broad probe reported a live, healthy database as "not a database" and
        shipped the raw byte copy without its WAL.

        Uses a real exclusive transaction rather than a stub, because the whole point is
        which error SQLite actually raises.
        """
        src, dst = self._staged_pair(tmp_path)
        holder = snap.sqlite3.connect(str(src / "memory.db"))
        try:
            holder.execute("BEGIN EXCLUSIVE")
            holder.execute("INSERT INTO t VALUES ('held')")
            with pytest.raises(snap.DatabaseCopyFailed) as e:
                snap._restage_databases(src, dst, bundle_root=dst)
            assert e.value.path.name == "memory.db"
            assert "not a readable SQLite database" not in capsys.readouterr().out
        finally:
            holder.close()

    def test_a_genuine_non_database_is_still_identified_positively(self, tmp_path, capsys):
        """The other direction: tightening must not turn a real non-database into a
        hard failure."""
        src, dst = tmp_path / "s", tmp_path / "d"
        src.mkdir()
        dst.mkdir()
        (src / "notes.db").write_text("plainly not a database", encoding="utf-8")
        (dst / "notes.db").write_text("plainly not a database", encoding="utf-8")
        snap._restage_databases(src, dst, bundle_root=dst)
        assert "not a readable SQLite database" in capsys.readouterr().out

    def test_both_database_copy_sites_raise_the_typed_error(self):
        """Structural: core files and trees are separate staging paths, and only one was
        wrapped at first -- the other exited on a traceback. The readability PROBE is a
        third raiser: anything it cannot positively classify as not-a-database must raise
        rather than fall through to the plain-file path.

        Rewritten when #5451 collapsed the two duplicated `backup()` blocks into one
        shared helper. The invariant is unchanged and now holds by construction: instead
        of counting two call sites and checking each is wrapped, there is ONE call site to
        wrap, and what needs asserting is that BOTH paths still route through it. The old
        `calls >= 2` form would now fail on the very refactor that removed the duplication
        it existed to police.
        """
        import inspect
        import re

        src = inspect.getsource(snap)
        calls = len(re.findall(r"src_conn\.backup\(dst_conn\)", src))
        wrapped = len(re.findall(r"raise DatabaseCopyFailed\(src, e\) from e", src))
        assert calls == 1, (
            f"{calls} backup call sites; the copy is meant to live in exactly one helper "
            "so the two staging paths cannot drift apart again"
        )
        assert wrapped >= calls, (
            f"{calls} backup call sites but only {wrapped} raise the typed error; an "
            "unwrapped site exits the command on a traceback"
        )
        # Both staging paths must reach that one helper. Without this, collapsing the
        # duplication would satisfy the count above while silently leaving one path
        # copying databases some other way.
        assert "_copy_database_consistently(" in inspect.getsource(
            snap._restage_databases
        ), "the tree pass no longer routes through the shared hardened copy"
        assert "_copy_database_consistently(" in inspect.getsource(
            snap._build_snapshot
        ), "the core-file pass no longer routes through the shared hardened copy"
        # The probe must fail closed. `SQLITE_BUSY` (a locked database) is a
        # DatabaseError too, so continuing on anything unclassified ships a raw copy
        # without its WAL as if it were consistent.
        probe_src = inspect.getsource(snap._copy_database_consistently)
        assert (
            "SQLITE_NOTADB" in probe_src
        ), "the probe must identify not-a-database positively, not by exception class"
        assert probe_src.index("if not not_a_database:") < probe_src.index(
            "return DB_NOT_A_DATABASE"
        ), "the raise must precede the not-a-database outcome"
        # The source is opened read-only. This is the #5451 fix and it is asserted
        # structurally as well as behaviourally, because a read-write open still passes
        # every functional test on a database with no unreplayed log.
        assert "?mode=ro" in probe_src, (
            "the staging connection must be read-only; a read-write open recovers an "
            "unreplayed -wal into the live main file and unlinks the log"
        )

    def test_the_module_under_test_and_this_file_agree_on_the_driver(self):
        """Pins the reason the tests above use snap.sqlite3: if the package ever binds
        the stdlib module, these tests keep working, and if it binds another one they
        still exercise the real except clause."""
        assert hasattr(snap.sqlite3, "Error") and hasattr(snap.sqlite3, "OperationalError")
        assert issubclass(snap.sqlite3.OperationalError, snap.sqlite3.Error)


class TestAFailedTreeReplacementPutsTheTreeBack:
    def test_the_whole_rollback_set_is_restored_not_just_the_failing_tree(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """The unit of atomicity is the restore, not one tree.

        Recovering only the tree that failed leaves every EARLIER tree replaced and the
        databases already swapped -- memory split across two restore generations, which
        is the state a rollback exists to prevent. So the assertion is on the whole set:
        the database content and the tree content both come back.
        """
        tree_root = home / "workspace"
        marker = tree_root / "memory" / "keep.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("live content", encoding="utf-8")
        live_db = home / "memory.db"
        live_db.write_bytes(b"LIVE DATABASE")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "new.md").write_text("x", encoding="utf-8")
        (payload / "memory.db").write_bytes(_sound_db_bytes(tmp_path / "in1_1.db"))
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        real = snap._copytree_safe

        def flaky(src, dst, **kw):
            # Fail the incoming copy into the live root; the rollback save and the
            # recovery copies must both go through.
            if Path(dst) == tree_root and "kirocrew-snapshot-" in str(src):
                raise OSError("No space left on device")
            return real(src, dst, **kw)

        monkeypatch.setattr(snap, "_copytree_safe", flaky)
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        assert rc == 1
        out = capsys.readouterr().out

        assert "Restoring the previous state" in out, out
        assert marker.is_file(), "the live tree was not put back"
        assert marker.read_text(encoding="utf-8") == "live content"
        assert (
            live_db.read_bytes() == b"LIVE DATABASE"
        ), "the database stayed on the incoming generation while the tree rolled back"

    def test_the_rollback_directory_is_kept_when_recovery_itself_fails(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """If recovery cannot put something back, it must name it and leave the saved
        copy in place -- by that point the operator's own data is what is at stake."""
        backup = home / "pre-restore-test"
        (backup / "workspace").mkdir(parents=True)
        (backup / "workspace" / "saved.md").write_text("saved", encoding="utf-8")

        def refuse(src, dst, **kw):
            raise OSError("Read-only file system")

        # Recovery now restores with `_copytree_safe` (the save refuses a tree with a
        # link, so the rollback set is links-free and needs no link-preserving copy), so
        # that is where a failure has to be injected to reach this path.
        monkeypatch.setattr(snap, "_copytree_safe", refuse)
        snap._restore_everything_from_rollback(
            backup, home, ["workspace"], {"workspace"}, allow_unpinned=bool(unpinnable_argv())
        )
        out = capsys.readouterr().out
        assert "Could not undo these" in out, out
        assert "workspace" in out
        assert backup.is_dir(), "the rollback directory must survive a failed recovery"

    def test_a_target_the_restore_created_is_removed_by_recovery(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """Restoring saved entries is not enough. A target that did NOT exist before the
        restore has nothing saved for it, so the copy the restore created would survive
        the rollback and leave the home carrying half of the incoming generation.

        The mutation order is workspace then skills, so workspace is the creation and
        skills is the step made to fail -- a creation must precede the failure for the
        hazard to exist at all.
        """
        import shutil as _shutil

        for name in ("workspace", "plan_memory"):
            p = home / name
            if p.exists():
                _shutil.rmtree(p)
        live_ws = home / "workspace"
        assert not live_ws.exists(), "premise: no pre-restore workspace tree"
        (home / "skills").mkdir(parents=True, exist_ok=True)

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "new.md").write_text("y", encoding="utf-8")
        (payload / "skills" / "incoming").mkdir(parents=True)
        (payload / "skills" / "incoming" / "SKILL.md").write_text("x", encoding="utf-8")
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        real = snap._copytree_safe

        def flaky(src, dst, **kw):
            if Path(dst) == home / "skills" and "kirocrew-snapshot-" in str(src):
                raise OSError("No space left on device")
            return real(src, dst, **kw)

        monkeypatch.setattr(snap, "_copytree_safe", flaky)
        rc = snap.restore_main(
            [
                str(bundle),
                "--mode",
                "replace",
                "--force",
                "--components",
                "workspace,skills",
                *unpinnable_argv(),
            ]
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "Restoring the previous state" in out, out
        assert not live_ws.exists(), (
            "recovery left behind a workspace tree the restore created; the home is now "
            "on two generations at once"
        )

    def test_a_target_that_existed_is_restored_not_removed(self, home, capsys):
        """The other direction: an entry WITH pre-restore state must come back, not be
        deleted as if it were a creation."""
        backup = home / "pre-restore-x"
        (backup / "skills" / "mine").mkdir(parents=True)
        (backup / "skills" / "mine" / "SKILL.md").write_text("saved", encoding="utf-8")
        snap._restore_everything_from_rollback(
            backup, home, ["skills"], {"skills"}, allow_unpinned=bool(unpinnable_argv())
        )
        assert (home / "skills" / "mine" / "SKILL.md").read_text(encoding="utf-8") == "saved"

    def test_memory_only_recovery_does_not_clear_sibling_workspace_data(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """Granularity is the invariant. Memory's trees are NESTED under workspace, so
        the rollback directory holds a partial `workspace/` containing only those
        subtrees. Recovering at directory granularity clears the live `workspace` whole
        and puts the partial copy back, deleting sibling data the restore never touched.
        """
        memory_tree = home / "workspace" / "memory"
        memory_tree.mkdir(parents=True, exist_ok=True)
        (memory_tree / "mine.md").write_text("memory content", encoding="utf-8")
        sibling = home / "workspace" / "unrelated"
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "notes.md").write_text("NOT PART OF MEMORY", encoding="utf-8")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "new.md").write_text("x", encoding="utf-8")
        (payload / "memory.db").write_bytes(_sound_db_bytes(tmp_path / "in2.db"))
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        real = snap._copytree_safe

        def flaky(src, dst, **kw):
            if Path(dst) == memory_tree and "kirocrew-snapshot-" in str(src):
                raise OSError("No space left on device")
            return real(src, dst, **kw)

        monkeypatch.setattr(snap, "_copytree_safe", flaky)
        rc = snap.restore_main(
            [
                str(bundle),
                "--mode",
                "replace",
                "--force",
                "--components",
                "memory",
                *unpinnable_argv(),
            ]
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "Restoring the previous state" in out, out
        assert (sibling / "notes.md").is_file(), (
            "recovery deleted unrelated workspace data by treating backup/workspace as "
            "a single target"
        )
        assert (sibling / "notes.md").read_text(encoding="utf-8") == "NOT PART OF MEMORY"
        assert (memory_tree / "mine.md").read_text(encoding="utf-8") == "memory content"

    def test_recovery_touches_only_declared_targets(self):
        """Structural: recovery must iterate the target list, not the rollback tree."""
        import inspect

        src = inspect.getsource(snap._restore_everything_from_rollback)
        assert "for rel in sorted(set(targets))" in src
        assert "backup.iterdir()" not in src, (
            "iterating the rollback directory reintroduces directory granularity, which "
            "clears a live parent when only nested subtrees were saved"
        )

    def test_a_missing_rollback_directory_is_reported_not_crashed(self, home, capsys):
        snap._restore_everything_from_rollback(
            home / "nope", home, ["workspace"], set(), allow_unpinned=bool(unpinnable_argv())
        )
        assert "No rollback directory" in capsys.readouterr().out

    def test_a_failed_rollback_SAVE_leaves_live_data_untouched(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """An INCOMPLETE rollback set is worse than none.

        The tree saves used to happen inside the mutation phase, so a save that failed
        partway raised into the recovery handler -- which then cleared the intact live
        tree and put the PARTIAL copy back, destroying data nothing had touched. Saves
        now complete before anything mutates, so a save failure aborts with the data
        home unchanged and recovery never runs.
        """
        live = home / "workspace" / "memory"
        live.mkdir(parents=True, exist_ok=True)
        (live / "precious.md").write_text("must survive", encoding="utf-8")
        live_db = home / "memory.db"
        live_db.write_bytes(b"LIVE DATABASE")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "new.md").write_text("x", encoding="utf-8")
        (payload / "memory.db").write_bytes(_sound_db_bytes(tmp_path / "in1_2.db"))
        bundle = tmp_path / "b.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        real = snap._copytree_safe

        def fail_the_save(src, dst, **kw):
            # The SAVE direction: copying the live tree INTO the rollback directory. The
            # save now routes through `_backup_tree_or_refuse` -> `_copytree_safe` (a tree
            # with a link is refused, not preserved), so failing the save means failing
            # the `_copytree_safe` call whose destination is under `pre-restore-`.
            if "pre-restore-" in str(dst):
                raise OSError("Input/output error")
            return real(src, dst, **kw)

        monkeypatch.setattr(snap, "_copytree_safe", fail_the_save)
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        assert rc == 1
        out = capsys.readouterr().out

        assert "Restoring the previous state" not in out, (
            "recovery ran on an incomplete rollback set: " + out
        )
        assert (live / "precious.md").read_text(encoding="utf-8") == "must survive"
        assert live_db.read_bytes() == b"LIVE DATABASE"

    def test_the_skills_tree_is_also_rolled_back(self, home, tmp_path, monkeypatch, capsys):
        """Each tree needs its own coverage: a mutant that deleted only the SKILLS
        rollback save survived while workspace was covered."""
        live_skill = home / "skills" / "mine" / "SKILL.md"
        live_skill.parent.mkdir(parents=True, exist_ok=True)
        live_skill.write_text("my skill", encoding="utf-8")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "skills" / "other").mkdir(parents=True)
        (payload / "skills" / "other" / "SKILL.md").write_text("x", encoding="utf-8")
        bundle = tmp_path / "sk.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        real = snap._copytree_safe

        def flaky(src, dst, **kw):
            # Fail only the incoming copy into the live skills tree.
            if Path(dst) == home / "skills" and "kirocrew-snapshot-" in str(src):
                raise OSError("No space left on device")
            return real(src, dst, **kw)

        monkeypatch.setattr(snap, "_copytree_safe", flaky)
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "skills"]
        )
        assert rc == 1
        assert live_skill.is_file(), "the live skills tree was not put back"
        assert live_skill.read_text(encoding="utf-8") == "my skill"

    def test_every_tree_save_precedes_the_mutation_phase(self):
        """Structural: a save left inside the mutation phase re-opens the data-loss
        path, and the failure only shows on an unreadable file.

        The save primitive is now `_backup_tree_or_refuse` (fatal skip reporter, so an
        incomplete rollback set is refused rather than written); it must live in
        `_do_replace`'s phase one and never in `_do_replace_mutations`.
        """
        import inspect

        muts = inspect.getsource(snap._do_replace_mutations)
        # No SAVE of a live tree into the rollback directory may happen in the mutation
        # phase, by any copier.
        for saving in ("backup / dirname)", 'backup / "skills")', "backup / tree)"):
            for copier in ("_backup_tree_or_refuse", "_copytree_safe", "_copytree_rollback"):
                assert f"{copier}(d, {saving}" not in muts
                assert f"{copier}(sk, {saving}" not in muts
        assert (
            "dirs_exist_ok=True" not in muts
        ), "a rollback save with dirs_exist_ok blends two pre-restore states"
        setup = inspect.getsource(snap._do_replace)
        assert setup.index("_backup_tree_or_refuse(d, backup / dirname") < setup.index(
            "_do_replace_mutations("
        ), "the workspace rollback save must complete before any mutation"

    def test_a_successful_replace_is_unaffected(self, home, tmp_path):
        live = home / "workspace" / "memory"
        live.mkdir(parents=True, exist_ok=True)
        (live / "old.md").write_text("old", encoding="utf-8")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "new.md").write_text("new", encoding="utf-8")
        bundle = tmp_path / "ok.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        assert (
            snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
            == 0
        )
        assert (live / "new.md").read_text(encoding="utf-8") == "new"
