"""Tests for the review findings on the first M1 SHA.

Each one asserts the consequence, not the mechanism: what state would have been
destroyed, or what would have escaped, if the fix were not there.
"""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import snapshot as snap_mod
from kiro_crew.snapshot import restore_main, snapshot_main


@pytest.fixture
def src(tmp_path, monkeypatch):
    d = tmp_path / "src"
    _setup_fake_kirocrew(d)
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    return d


class TestReplaceKeepsARecoverableRollback:
    """Restoring memory+workspace must not overwrite its own rollback copy."""

    def test_the_original_memory_is_recoverable_after_a_full_replace(
        self, src, tmp_path, monkeypatch
    ):
        # Bundle carries INCOMING content.
        (src / "workspace/memory/preferences.md").write_text("incoming prefs\n")
        out = tmp_path / "out"
        assert snapshot_main([str(out)] + unpinnable_argv()) == 0
        tars = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert tars

        # Destination holds DIFFERENT, original content.
        dest = tmp_path / "dest"
        (dest / "workspace/memory").mkdir(parents=True)
        (dest / "workspace/memory/preferences.md").write_text("ORIGINAL prefs\n")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        assert (
            restore_main([str(tars[-1]), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        )

        # Live state is the incoming copy.
        assert (dest / "workspace/memory/preferences.md").read_text() == "incoming prefs\n"

        # And the pre-restore original is still recoverable from the rollback dir.
        saved = list(dest.glob("pre-restore-*/workspace/memory/preferences.md"))
        assert saved, "no rollback copy was kept"
        assert (
            saved[0].read_text() == "ORIGINAL prefs\n"
        ), "the rollback copy was overwritten with incoming data"


class TestLiveDatabasesAreStagedConsistently:
    """A SQLite database inside a staged tree gets the backup API, not a byte copy."""

    def _stage(self, src: Path, out: Path) -> Path:
        assert snapshot_main([str(out), "--components", "memory"] + unpinnable_argv()) == 0
        tar = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))[-1]
        extract = out / "x"
        extract.mkdir(exist_ok=True)
        with tarfile.open(str(tar)) as t:
            t.extractall(extract, filter=lambda m, _d="": m)
        return next(
            d
            for d in extract.iterdir()
            if d.name.startswith(("kirocrew-snapshot-", "kirocrew-partial-"))
        )

    def test_a_knowledge_database_survives_with_its_rows(self, src, tmp_path):
        kb = src / "workspace/knowledge/knowledge.db"
        conn = sqlite3.connect(str(kb))
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO facts (body) VALUES (?)", [("a",), ("b",), ("c",)])
        conn.commit()
        conn.close()

        snap = self._stage(src, tmp_path / "out")
        staged = snap / "workspace/knowledge/knowledge.db"
        assert staged.is_file()
        c = sqlite3.connect(str(staged))
        assert c.execute("SELECT count(*) FROM facts").fetchone()[0] == 3
        c.close()

    def test_wal_and_shm_sidecars_are_not_staged(self, src, tmp_path):
        """They describe the source's transaction state, not the copy's.

        Asserted on a `.db` that SQLite CANNOT open, because that is the only case
        where the glob is the thing doing the work: for a real database, re-opening
        the staged copy makes SQLite discard the copied sidecars as a side effect, so
        a test using a real database passes either way and proves nothing.
        """
        kdir = src / "workspace/knowledge"
        (kdir / "notes.db").write_bytes(b"not a database")
        (kdir / "notes.db-wal").write_bytes(b"stale journal")
        (kdir / "notes.db-shm").write_bytes(b"stale shm")

        snap = self._stage(src, tmp_path / "out")
        staged = snap / "workspace/knowledge"
        assert (staged / "notes.db").is_file(), "the operator's file must still ride"
        assert not (staged / "notes.db-wal").exists(), "a WAL sidecar rode the bundle"
        assert not (staged / "notes.db-shm").exists(), "a SHM sidecar rode the bundle"

    def test_a_real_databases_sidecars_also_stay_out(self, src, tmp_path):
        kb = src / "workspace/knowledge/knowledge.db"
        conn = sqlite3.connect(str(kb))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO facts (body) VALUES ('uncheckpointed')")
        conn.commit()
        # Leave the connection OPEN so -wal / -shm exist on disk while we snapshot.
        assert (src / "workspace/knowledge/knowledge.db-wal").exists()
        try:
            snap = self._stage(src, tmp_path / "out")
            kdir = snap / "workspace/knowledge"
            assert not list(kdir.glob("knowledge.db-*"))
            # The committed row still arrives: the backup API checkpointed it.
            c = sqlite3.connect(str(kdir / "knowledge.db"))
            assert c.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
            c.close()
        finally:
            conn.close()

    def test_a_non_database_named_db_still_rides(self, src, tmp_path, capsys):
        """A .db file that is not SQLite is the operator's file — keep the byte copy."""
        decoy = src / "workspace/knowledge/notes.db"
        decoy.write_bytes(b"not a database at all")
        snap = self._stage(src, tmp_path / "out")
        assert (snap / "workspace/knowledge/notes.db").read_bytes() == b"not a database at all"

    def test_a_hardlinked_database_the_walk_skipped_is_not_rebuilt(self, tmp_path):
        """The consistency pass must not undo the walk's hardlink rejection.

        The staging walk refuses a hardlink alias because the second link can point at a
        file outside the component tree, and copying it would put content the tree does not
        own into the bundle. That refusal is recorded as a SKIP and staging continues -- so
        a second pass that rebuilds databases from the SOURCE, keyed only on the source
        walk, reinstates precisely what was refused.

        Reproduced before the guard existed: a `.db` hardlinked to a database outside the
        tree was skipped as `not_regular`, then rebuilt here, and its rows rode the bundle.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.db"
        conn = sqlite3.connect(str(secret))
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.execute("INSERT INTO t VALUES ('EXTERNAL')")
        conn.commit()
        conn.close()

        tree = tmp_path / "live" / "knowledge"
        tree.mkdir(parents=True)
        (tree / "kb.db").hardlink_to(secret)
        stage = tmp_path / "stage" / "knowledge"
        stage.parent.mkdir(parents=True)

        skips: list[str] = []
        snap_mod._copytree_safe(
            tree,
            stage,
            allow_unpinned=bool(unpinnable_argv()),
            on_skip=lambda reason, _p: skips.append(reason),
        )
        assert skips, "the walk did not refuse the hardlink alias, so this proves nothing"
        assert not (stage / "kb.db").exists()

        snap_mod._restage_databases(tree, stage, bundle_root=stage)
        assert not (
            stage / "kb.db"
        ).exists(), "the consistency pass rebuilt a database the walk deliberately skipped"

    def test_an_ordinary_database_is_still_restaged_consistently(self, tmp_path):
        """The guard must not cost the pass its actual job."""
        tree = tmp_path / "live" / "knowledge"
        tree.mkdir(parents=True)
        conn = sqlite3.connect(str(tree / "kb.db"))
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.execute("INSERT INTO t VALUES ('MINE')")
        conn.commit()
        conn.close()
        stage = tmp_path / "stage" / "knowledge"
        stage.parent.mkdir(parents=True)

        snap_mod._copytree_safe(tree, stage, allow_unpinned=bool(unpinnable_argv()))
        assert (stage / "kb.db").is_file()
        snap_mod._restage_databases(tree, stage, bundle_root=stage)

        c = sqlite3.connect(str(stage / "kb.db"))
        assert [r[0] for r in c.execute("SELECT v FROM t")] == ["MINE"]
        c.close()


class TestADestinationRootSwappedAfterPreflightRefuses:
    """A replace that loses a memory tree mid-run must refuse, not report success.

    ``_refuse_unsafe_destination_roots`` is hoisted ahead of every mutation, and its own
    docstring gives the reason: checking inside the per-tree loop was too late, because
    skipping an unsafe tree left memory split between two versions while the command still
    reported success. One site kept the old behaviour -- the ``mem_roots`` build
    ``continue``d past a root that failed the very same check -- so a root that went bad
    AFTER the pre-flight was dropped from the set entirely, neither saved nor restored,
    and the run still printed "Replace complete."

    The swap is injected from INSIDE a wrapper around the pre-flight rather than planted
    beforehand. Planting it first only proves the pre-flight works, which it does and which
    is not this test's subject. The wrapper's fire count is asserted first, because a
    monkeypatch that silently never ran would make every assertion after it vacuous.
    """

    def test_it_refuses_instead_of_reporting_a_complete_replace(
        self, tmp_path, monkeypatch, capsys
    ):
        import os
        import shutil

        outside = tmp_path / "outside"
        (outside / "victim").mkdir(parents=True)
        (outside / "victim" / "precious.txt").write_text("survives\n", encoding="utf-8")

        source = tmp_path / "source"
        _setup_fake_kirocrew(source)
        monkeypatch.setenv("KIROCREW_HOME", str(source))
        outdir = tmp_path / "snaps"
        assert snapshot_main([str(outdir), "--components", "memory"] + unpinnable_argv()) == 0
        bundle = sorted(outdir.glob("*.tar.gz"))[0]

        home = tmp_path / "home"
        _setup_fake_kirocrew(home)
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        real = snap_mod._refuse_unsafe_destination_roots
        fired: list[bool] = []

        def _preflight_then_swap(mc, components):
            real(mc, components)
            shutil.rmtree(home / "workspace")
            os.symlink(str(outside / "victim"), str(home / "workspace"))
            fired.append(True)

        monkeypatch.setattr(snap_mod, "_refuse_unsafe_destination_roots", _preflight_then_swap)
        capsys.readouterr()

        rc = restore_main([str(bundle), "--mode", "replace", "--force"] + unpinnable_argv())
        out = capsys.readouterr().out

        assert fired, "the mid-run swap never fired -- every assertion below would be vacuous"
        assert "Replace mode" in out, "never reached the phase under test"
        assert rc != 0, "a replace that lost both memory trees reported success"
        assert "stopped resolving inside the data home" in out
        assert "Replace complete" not in out
        assert (outside / "victim" / "precious.txt").read_text(encoding="utf-8") == "survives\n"


class TestRollbackWillNotDeleteWhatItNeverSaved:
    """Recovery must not delete an occupant it cannot prove this run created.

    `installed` records a core file BEFORE the save is known to have happened -- deliberately,
    so a crash mid-write still leaves the name known to have been reached. But the save only
    happens on two branches, a symlink or a regular file. A core file's path occupied by a
    DIRECTORY matches neither, so nothing is saved while the name is recorded anyway, and
    recovery's "nothing saved AND this run put it here" branch then had no evidence for the
    second half of its own claim.

    Reproduced before the fix: a directory at `crons.json` holding operator data was deleted
    with its contents, the failure list came back empty, and the run printed "Previous state
    restored." Data loss announced as a successful recovery, which is the one thing recovery
    must never say.
    """

    def test_a_pre_existing_directory_at_a_core_file_name_is_kept_and_reported(self, tmp_path):
        home = tmp_path / "home"
        _setup_fake_kirocrew(home)
        backup = tmp_path / "pre-restore"
        backup.mkdir()

        occupant = home / "crons.json"
        if occupant.exists() or occupant.is_symlink():
            occupant.unlink()
        occupant.mkdir()
        (occupant / "operator-data.txt").write_text("predates this run\n", encoding="utf-8")

        # Exactly the state the backup pass leaves: recorded as reached, nothing saved.
        assert not (backup / "crons.json").exists()

        failed = snap_mod._restore_everything_from_rollback(
            backup, home, ["crons.json"], {"crons.json"}
        )

        assert (occupant / "operator-data.txt").read_text(
            encoding="utf-8"
        ) == "predates this run\n", "recovery deleted data it never saved a copy of"
        assert occupant.is_dir()
        assert failed, "the refusal has to be reported, not silently skipped"
        assert "crons.json" in failed[0]

    def test_the_flat_core_file_set_is_derived_from_the_component_specs(self):
        """A hand-kept copy would drift exactly when a component is added."""
        expected = {f for files in snap_mod.CORE_FILES.values() for f in files}
        assert set(snap_mod.CORE_FILES_FLAT) == expected
        assert "crons.json" in snap_mod.CORE_FILES_FLAT


class TestMergeRefusesADestinationRootSwappedAfterPreflight:
    """Merge must refuse a root that stopped resolving inside the home, not write through it.

    `_do_merge` calls the same pre-flight as replace and then had NO use-time re-check before
    `dd.mkdir(parents=True)` and the copy. Reproduced: with `workspace` replaced by an
    external symlink immediately after the pre-flight, merge created the tree THROUGH the link
    and wrote four of the operator's memory files into a directory outside the data home --
    and printed "Merge complete."

    The per-file screens cannot catch this. Each final component is a fresh regular file, and
    a by-name open does not examine its ancestors, so `O_NOFOLLOW` per file is satisfied while
    the whole tree lands somewhere else. Refusing on the ROOT is what closes it.

    The swap is injected from inside a wrapper around the pre-flight; planting it beforehand
    would only prove the pre-flight works, which is not this test's subject. The wrapper's
    fire count is asserted first, or every assertion after it is vacuous.
    """

    def test_it_refuses_instead_of_merging_outside_the_data_home(
        self, tmp_path, monkeypatch, capsys
    ):
        import os
        import shutil

        outside = tmp_path / "outside"
        (outside / "victim").mkdir(parents=True)
        (outside / "victim" / "precious.txt").write_text("survives\n", encoding="utf-8")

        source = tmp_path / "source"
        _setup_fake_kirocrew(source)
        monkeypatch.setenv("KIROCREW_HOME", str(source))
        outdir = tmp_path / "snaps"
        assert snapshot_main([str(outdir), "--components", "memory"] + unpinnable_argv()) == 0
        bundle = sorted(outdir.glob("*.tar.gz"))[0]

        home = tmp_path / "home"
        _setup_fake_kirocrew(home)
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        real = snap_mod._refuse_unsafe_destination_roots
        fired: list[bool] = []

        def _preflight_then_swap(mc, components):
            real(mc, components)
            shutil.rmtree(home / "workspace")
            os.symlink(str(outside / "victim"), str(home / "workspace"))
            fired.append(True)

        monkeypatch.setattr(snap_mod, "_refuse_unsafe_destination_roots", _preflight_then_swap)
        capsys.readouterr()

        # The flag matters for the SUBJECT, not just for getting past staging: without it,
        # a platform with no directory descriptors would refuse this merge for the PINNING
        # reason and `rc != 0` below would pass for the wrong reason -- green while proving
        # nothing about the destination-root check this test exists for.
        rc = restore_main([str(bundle), "--mode", "merge", "--force"] + unpinnable_argv())
        out = capsys.readouterr().out

        assert fired, "the mid-run swap never fired -- every assertion below would be vacuous"
        assert rc != 0, "merge reported success while writing outside the data home"
        assert "stopped resolving inside the data home" in out
        assert "Merge complete" not in out

        strays = [
            p.relative_to(outside)
            for p in outside.rglob("*")
            if p.is_file() and p.name != "precious.txt"
        ]
        assert not strays, f"merge wrote outside the data home: {strays}"
        assert (outside / "victim" / "precious.txt").read_text(encoding="utf-8") == "survives\n"


class TestReplaceDoesNotKeepAnIndexTheArchiveOmits:
    """A derived index the archive does not carry must not survive a REPLACE.

    `_backup_and_copy` skips a core file the bundle lacks -- correct for its own job, since
    it has nothing to install -- so the live `memory_index.db` stayed put while `memory.db`
    was replaced underneath it. The restore then ends with new memory and an index built from
    the old, and searches answer from memory that is gone.

    Reachable by the ORDINARY route, which is what makes it worth a test: the redaction pass
    drops `memory_index.db` from an off-host bundle by design, so every restore from a
    redacted bundle takes this path. The "restore warns about a missing index" argument that
    justified the drop does not cover it -- that warning tests the file AFTER the restore, and
    a surviving stale index means it never fires.

    The memory-TREE loop already stated the rule ("a tree the archive does not have is a tree
    the destination must not keep"); this is the same rule for a file.
    """

    def test_the_stale_index_is_removed_and_kept_for_rollback(self, tmp_path):
        mc = tmp_path / "home"
        mc.mkdir()
        (mc / "memory.db").write_bytes(b"OLD-MEMORY")
        (mc / "memory_index.db").write_bytes(b"INDEX-OF-OLD-MEMORY")

        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "memory.db").write_bytes(b"NEW-MEMORY")  # no index, as redaction leaves it

        backup = tmp_path / "rollback"
        backup.mkdir()
        installed: set[str] = set()

        snap_mod._do_replace_mutations(
            snap,
            mc,
            backup,
            ["memory"],
            [],
            installed,
            allow_unpinned=bool(unpinnable_argv()),
        )

        assert (mc / "memory.db").read_bytes() == b"NEW-MEMORY"
        assert not (mc / "memory_index.db").exists(), "stale index survived the replace"
        # Removed, not destroyed -- and recorded, so recovery knows this run reached it.
        assert (backup / "memory_index.db").read_bytes() == b"INDEX-OF-OLD-MEMORY"
        assert "memory_index.db" in installed

    def test_an_index_the_archive_carries_is_installed_not_dropped(self, tmp_path):
        """The discrimination half: this must not turn into "always delete the index"."""
        mc = tmp_path / "home"
        mc.mkdir()
        (mc / "memory.db").write_bytes(b"OLD-MEMORY")
        (mc / "memory_index.db").write_bytes(b"INDEX-OF-OLD-MEMORY")

        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "memory.db").write_bytes(b"NEW-MEMORY")
        (snap / "memory_index.db").write_bytes(b"INDEX-OF-NEW-MEMORY")

        backup = tmp_path / "rollback"
        backup.mkdir()
        snap_mod._do_replace_mutations(
            snap,
            mc,
            backup,
            ["memory"],
            [],
            set(),
            allow_unpinned=bool(unpinnable_argv()),
        )
        assert (mc / "memory_index.db").read_bytes() == b"INDEX-OF-NEW-MEMORY"

    def test_the_two_derived_index_sets_agree(self):
        """The restore side cannot import the redaction side's set, so pin the agreement.

        `snapshot_redact` is loaded lazily to keep it out of the boot path, so the name is
        duplicated rather than imported. A divergence would mean the redaction pass drops an
        index that replace then leaves stale -- silently. This fails instead.
        """
        from kiro_crew import snapshot_redact

        assert snap_mod._DERIVED_INDEXES == snapshot_redact._DERIVED_INDEXES


class TestAnInterruptDuringReplaceStillRollsBack:
    """A Ctrl-C mid-replace must not leave live state half swapped.

    The handler used to catch `(OSError, DatabaseCopyFailed, PinnedPathRefusal)`.
    `KeyboardInterrupt` is a `BaseException` and matches none of them, so it propagated past
    the rollback entirely -- and its own comment already gave the reason `PinnedPathRefusal`
    had to be in that tuple: it fires MID-mutation, so omitting it left live state half
    replaced. An interrupt has exactly that property.

    Reproduced: a Ctrl-C after the memory component was replaced left `memory.db` holding the
    archive's copy and `crons.json` still live, with no rollback attempted.

    Both halves are asserted, because rolling back is only half the requirement -- the
    interrupt must still terminate the command rather than be swallowed into a success.
    """

    def _home(self, tmp_path):
        mc = tmp_path / "home"
        mc.mkdir()
        (mc / "memory.db").write_bytes(b"LIVE-MEMORY")
        (mc / "crons.json").write_text("[]")
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "memory.db").write_bytes(b"ARCHIVE-MEMORY")
        (snap / "crons.json").write_text('[{"from": "archive"}]')
        return mc, snap

    def test_the_previous_state_is_restored_and_the_interrupt_propagates(
        self, tmp_path, monkeypatch
    ):
        mc, snap = self._home(tmp_path)
        real = snap_mod._do_replace_mutations
        fired = {"n": 0}

        def interrupting(sd, home, backup, components, mem_roots, installed, **kw):
            # Replace the memory component for real, so "partially replaced" is observable,
            # then interrupt where the next component would begin.
            real(sd, home, backup, ["memory"], mem_roots, installed, **kw)
            fired["n"] += 1
            raise KeyboardInterrupt("operator pressed Ctrl-C")

        monkeypatch.setattr(snap_mod, "_do_replace_mutations", interrupting)

        with pytest.raises(KeyboardInterrupt):
            snap_mod._do_replace(snap, mc, None, allow_unpinned=bool(unpinnable_argv()))

        assert fired["n"] == 1, "the injected interrupt never fired -- test proves nothing"
        assert (mc / "memory.db").read_bytes() == b"LIVE-MEMORY", "rollback did not run"


class TestABundleDeclaringAComponentItDoesNotCarryIsRefused:
    """Declared-but-hollow: the manifest says a component rode, the bundle holds none of it.

    Replace then clears the live state for that component with nothing to put back. Measured
    end to end rather than on the mutation phase, because the guard is at the decision point --
    calling `_do_replace_mutations` directly bypasses it and would report the old behaviour.

    Two facts made this worth refusing rather than tolerating. The PRODUCT writes such a bundle:
    a snapshot of a home with no memory payload declares `memory` anyway. And the damage is a
    PARTIAL erasure, which is worse than either extreme -- the memory trees are cleared
    unconditionally and the derived index is removed, while `memory.db` survives because the
    bundle has no copy to install. The database is kept and the notes indexed against it are not.

    The explicit-selection branch already refused this exact situation, with the rationale that
    replace "would move the live files of that component out to the rollback dir and have
    nothing to put back". Only the manifest-derived branch was missing the check.
    """

    def test_a_hollow_memory_declaration_refuses_instead_of_erasing(
        self, src, tmp_path, monkeypatch
    ):
        # A home with the memory component present but no memory payload at all.
        for name in ("memory.db", "memory_index.db"):
            if (src / name).exists():
                (src / name).unlink()
        for tree in ("workspace", "plan_memory"):
            if (src / tree).is_dir():
                shutil.rmtree(src / tree)

        out = tmp_path / "out"
        assert snapshot_main([str(out)] + unpinnable_argv()) == 0
        tars = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert tars

        # A destination that DOES have memory, so a partial erasure would be observable.
        dest = tmp_path / "dest"
        (dest / "workspace" / "memory").mkdir(parents=True)
        (dest / "workspace" / "memory" / "note.md").write_text("the operator's note\n")
        (dest / "memory.db").write_bytes(b"LIVE-MEMORY")
        (dest / "memory_index.db").write_bytes(b"LIVE-INDEX")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        rc = restore_main([str(tars[-1]), "--mode", "replace", "--force"] + unpinnable_argv())
        assert rc == 1, "a bundle carrying no memory payload was accepted"

        # Nothing touched: the refusal has to happen BEFORE any clearing, not after.
        assert (dest / "workspace" / "memory" / "note.md").read_text() == "the operator's note\n"
        assert (dest / "memory.db").read_bytes() == b"LIVE-MEMORY"
        assert (dest / "memory_index.db").read_bytes() == b"LIVE-INDEX"

    def test_an_explicit_selection_of_a_hollow_component_also_refuses(
        self, src, tmp_path, monkeypatch
    ):
        """The mirror of the case above, and the half I missed when I fixed it.

        The explicit-selection branch tested `c not in declared` -- membership in the
        declaration, not whether any payload rode. So `--components memory` on a hollow bundle
        passed the guard that exists for exactly this situation: `memory` IS declared, so
        nothing looked absent, and replace went on to clear the live trees and index.

        Both branches now ask the same question. Guarding one of them with a stronger test than
        the other is what left this reachable.

        The return code alone would NOT have caught it. Reproducing this showed `rc == 1`
        already -- not from a refusal but from the memory.db integrity check failing later, by
        which point the operator's notes were gone. So the load-bearing assertion is that the
        note SURVIVES, and the destination gets a real database to keep the integrity check
        from deciding the outcome.
        """
        for name in ("memory.db", "memory_index.db"):
            if (src / name).exists():
                (src / name).unlink()
        for tree in ("workspace", "plan_memory"):
            if (src / tree).is_dir():
                shutil.rmtree(src / tree)

        out = tmp_path / "out"
        assert snapshot_main([str(out)] + unpinnable_argv()) == 0
        tars = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))

        dest = tmp_path / "dest3"
        (dest / "workspace" / "memory").mkdir(parents=True)
        (dest / "workspace" / "memory" / "note.md").write_text("the operator's note\n")
        conn = sqlite3.connect(str(dest / "memory.db"))
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        rc = restore_main(
            [str(tars[-1]), "--mode", "replace", "--force", "--components", "memory"]
            + unpinnable_argv()
        )
        # The property that matters: nothing was cleared. Checked FIRST because a non-zero rc
        # can arrive from a later failure that says nothing about whether the guard fired.
        assert (
            dest / "workspace" / "memory" / "note.md"
        ).read_text() == "the operator's note\n", "live memory was cleared before anything refused"
        assert rc == 1, "an explicit selection of a component with no payload was accepted"

    def test_a_bundle_that_does_carry_memory_still_restores(self, src, tmp_path, monkeypatch):
        """The discrimination half: this must not refuse an ordinary bundle."""
        (src / "workspace/memory/preferences.md").write_text("incoming prefs\n")
        out = tmp_path / "out"
        assert snapshot_main([str(out)] + unpinnable_argv()) == 0
        tars = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))

        dest = tmp_path / "dest2"
        (dest / "workspace" / "memory").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        assert (
            restore_main([str(tars[-1]), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        )
        assert (dest / "workspace/memory/preferences.md").read_text() == "incoming prefs\n"
