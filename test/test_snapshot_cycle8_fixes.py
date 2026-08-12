"""Tests for the cycle-8 review findings.

What survives here is restore-side behaviour the off-host removal did not touch: replace
mode taking a complete rollback copy before it swaps any database, and the archive-name
escaping that keeps a forged newline or tab from faking listing lines.
"""

from __future__ import annotations

from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import snapshot as snap


class TestNamesCannotForgeListingLines:
    def test_a_newline_in_a_name_is_escaped(self):
        """The first version of the sanitizer excluded tab and newline as "harmless
        whitespace". A newline lets a name print FORGED lines, so the operator sees
        entries that do not exist."""
        out = snap._safe_name("backups/h/a.tar.gz\nbackups/h/fake.tar.gz")
        assert "\n" not in out
        assert "\\x0a" in out

    def test_a_tab_is_escaped(self):
        out = snap._safe_name("backups/h/a\tb.tar.gz")
        assert "\t" not in out
        assert "\\x09" in out


class TestReplaceSavesEveryTreeBeforeReplacingAny:
    def test_the_rollback_copy_is_taken_before_any_database_is_swapped(self):
        """Backing up and replacing tree-by-tree meant a failure partway through left
        some trees replaced and no rollback copy of the rest — with the databases already
        swapped. Half old, half new, and no complete copy of either.

        The current design splits this into two phases across two functions: `_do_replace`
        takes the WHOLE rollback set (every `_backup_tree_or_refuse`) before it calls
        `_do_replace_mutations`, and only then does the mutation function swap databases
        (`_backup_and_copy`) and replace trees (`rmtree` + `_copytree_safe(..., must_create=`).
        So the ordering claim spans both, and both are read.
        """
        import inspect

        setup = inspect.getsource(snap._do_replace)
        muts = inspect.getsource(snap._do_replace_mutations)
        # Phase one: every tree is saved via _backup_tree_or_refuse BEFORE the mutation
        # phase begins. The last save must still precede the handoff.
        save_at = setup.rindex("_backup_tree_or_refuse(")
        guard_at = setup.index("_do_replace_mutations(")
        assert save_at < guard_at, "a tree is saved after the mutation phase has already started"
        # Phase two: the database swap precedes the tree replacement, so an rmtree failure
        # cannot strand the databases in the new generation with no rollback of the trees.
        #
        # The locator is the function NAME, not a spelling of its argument list. Pinning the
        # arguments made this assertion fail on any signature change -- which is noise, not a
        # regression, since the claim being made here is purely about ORDER.
        swap_at = muts.index("_backup_and_copy(")
        replace_at = muts.index("_copytree_safe(")
        assert swap_at < replace_at, "the tree replace pass moved ahead of the database swap"
        # And the recovery is wired to the whole saved set, not to one tree.
        #
        # Checked by reading that call's ARGUMENTS for the whole-set names, not by matching a
        # spelling of the whole call. This assertion used to pin
        # `_restore_everything_from_rollback(backup, mc, targets, installed)` verbatim, which
        # is the exact mistake the comment above warns about -- threading the operator's
        # unpinned opt-in into recovery reflowed the call across lines and broke it, with
        # nothing about the claim having changed. What must hold is that recovery is handed
        # the declared target list and the installed set, so that is what is read.
        call_at = setup.index("_restore_everything_from_rollback(")
        args = setup[call_at : setup.index(")", call_at)]
        assert "targets" in args, f"recovery is not handed the declared target list: {args!r}"
        assert "installed" in args, f"recovery is not handed the installed set: {args!r}"

    def test_a_failed_tree_replace_still_leaves_a_complete_rollback_copy(
        self, tmp_path, monkeypatch
    ):
        """The behavioural half: if the replace pass dies, the ORIGINAL tree must be
        recoverable even though the databases have already moved.

        Built without `snapshot_main` (a manual `kirocrew-snapshot-` payload) so the
        rollback property is exercised directly, and the tree-replace failure is injected
        at `shutil.rmtree` -- the current mechanism `_do_replace_mutations` uses to clear a
        live tree before refilling it (the old `_clear_tree_root` helper is gone). Because
        phase one takes the whole rollback set before phase two mutates anything, the saved
        copy holds the state that was live before the restore even when phase two dies.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("changed since the backup")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "preferences.md").write_text("from the bundle")
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "b.tar.gz"
        with __import__("tarfile").open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        # Fail the live-tree clear that phase two performs, AFTER phase one has already
        # saved the rollback copy.
        real_rmtree = snap.shutil.rmtree

        def boom(path, *a, **k):
            if "workspace/memory" in str(path).replace("\\", "/"):
                raise OSError("disk full partway through the replace")
            return real_rmtree(path, *a, **k)

        monkeypatch.setattr(snap.shutil, "rmtree", boom)
        try:
            snap.restore_main(
                [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
                + unpinnable_argv()
            )
        except OSError:
            pass
        finally:
            monkeypatch.setattr(snap.shutil, "rmtree", real_rmtree)

        saved = list(home.glob("pre-restore-*/workspace/memory/preferences.md"))
        assert saved, "no rollback copy of the tree was taken before the swap"
        assert (
            saved[0].read_text() == "changed since the backup"
        ), "the rollback copy does not hold the state that was live before the restore"

    def test_a_normal_replace_still_works(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "preferences.md").write_text("original")
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "b.tar.gz"
        with __import__("tarfile").open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        (md / "preferences.md").write_text("changed since the backup")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
            + unpinnable_argv()
        )
        assert rc == 0
        assert (md / "preferences.md").read_text() == "original"
