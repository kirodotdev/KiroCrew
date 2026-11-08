"""Snapshot and restore guards, each asserted on its consequence.

Each test checks what state would be destroyed, or what would escape, without the guard.
"""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from conftest import make_dir_link
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

        A hardlink alias can point at a file outside the component tree, so the walk skips
        it. A pass that rebuilds databases from the SOURCE walk alone would reinstate it.
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

    A root that goes bad AFTER the pre-flight must not be silently dropped from
    ``mem_roots``, neither saved nor restored, while the run prints "Replace complete."

    The swap is injected inside a wrapper around the pre-flight; planting it beforehand
    would only test the pre-flight. The fire count is asserted first, or every assertion
    after it is vacuous.
    """

    def test_it_refuses_instead_of_reporting_a_complete_replace(
        self, tmp_path, monkeypatch, capsys
    ):
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
            # A directory link: a junction on Windows, where os.symlink needs a
            # privilege an ordinary shell lacks (WinError 1314 inside this hook
            # meant the swap never fired and the test failed for the wrong reason).
            # realpath resolves a junction outside the home exactly as a symlink.
            make_dir_link(home / "workspace", outside / "victim")
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

    `installed` records a core file BEFORE the save, so a crash mid-write still knows the
    name was reached. A DIRECTORY at that path is neither saved branch (symlink or regular
    file), so "recorded but nothing saved" does not prove this run created it. Deleting it
    would be data loss reported as "Previous state restored."
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

    Per-file `O_NOFOLLOW` does not check ancestors, so a `workspace` swapped for an external
    link after the pre-flight would take the whole tree outside the data home. Only a
    use-time check on the ROOT catches it.

    The swap is injected inside a wrapper around the pre-flight; planting it beforehand
    would only test the pre-flight. The fire count is asserted first.
    """

    def test_it_refuses_instead_of_merging_outside_the_data_home(
        self, tmp_path, monkeypatch, capsys
    ):
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
            # A directory link: a junction on Windows, where os.symlink needs a
            # privilege an ordinary shell lacks (WinError 1314 inside this hook
            # meant the swap never fired and the test failed for the wrong reason).
            # realpath resolves a junction outside the home exactly as a symlink.
            make_dir_link(home / "workspace", outside / "victim")
            fired.append(True)

        monkeypatch.setattr(snap_mod, "_refuse_unsafe_destination_roots", _preflight_then_swap)
        capsys.readouterr()

        # Without the flag, a platform with no directory descriptors refuses for the PINNING
        # reason and `rc != 0` below passes for the wrong reason.
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

    `_backup_and_copy` skips a core file the bundle lacks, so a live `memory_index.db` would
    outlive the `memory.db` it indexes. Every redacted bundle omits the index, and a stale
    one also hides the restore's missing-index warning. Same rule as the memory-tree loop.
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

    `KeyboardInterrupt` is a `BaseException`, outside the handler's
    `(OSError, DatabaseCopyFailed, PinnedPathRefusal)`, yet it can fire MID-mutation like
    `PinnedPathRefusal`. Both halves are asserted: rollback runs AND the interrupt propagates.
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

    Replace would clear the live state with nothing to put back. The product writes such a
    bundle (a home with no memory payload still declares `memory`), and the damage is a
    PARTIAL erasure: trees and index cleared, `memory.db` kept. Run end to end because the
    guard sits at the decision point, which `_do_replace_mutations` bypasses.
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
        """The mirror case: `--components memory` on a hollow bundle also refuses.

        Being declared is not the same as carrying a payload. The return code alone is not
        enough: a later memory.db integrity failure also gives `rc == 1`, after the notes are
        gone. So the note must SURVIVE, and the destination gets a real database.
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
