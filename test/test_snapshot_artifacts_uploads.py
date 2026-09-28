"""The artifact library and the uploads directory ride a snapshot.

`config_dir()/artifacts` -- every report, log and generated file an agent or the
operator saved -- and `config_dir()/uploads` are component-table entries like memory and
skills, so a restored host gets them back. Without a table entry a restore comes back
without them and says nothing, which is the silent half of the failure: the command
reports success. `artifact_folders.json` is deliberately NOT one of them, so restored
artifacts arrive at the root of the library rather than in their folders.

The tests are grouped by the promise each one holds:

* the archive CARRIES both, and says so in its own manifest;
* replace and merge both put them back with the semantics the sibling trees have;
* an aliased entry -- a symlink, a hardlink, a swapped tree ROOT -- is refused here
  exactly as it is for `workspace` and `skills`, including at pre-restore backup
  time, where a skipped alias would be followed by an `rmtree` of the only copy;
  these two components inherit that from the shared staging path rather than
  adding a refusal of their own;
* an absent `uploads/` is the ordinary fresh-install shape, not a failure.
"""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from conftest import requires_symlinks
from kiro_crew import snapshot as snap

FOLDERS = '[{"id": "abc123abc123", "name": "Reports", "order": 0, "parent_id": ""}]'


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _setup_fake_kirocrew(home)
    (home / "artifacts").mkdir(parents=True, exist_ok=True)
    (home / "artifacts" / "report.md").write_text("# quarterly report\n")
    (home / "artifacts" / "nested").mkdir(exist_ok=True)
    (home / "artifacts" / "nested" / "meta.json").write_text('{"kind": "doc"}')
    (home / "artifact_folders.json").write_text(FOLDERS)
    (home / "uploads").mkdir(parents=True, exist_ok=True)
    (home / "uploads" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nstub")
    return home


def _snapshot(tmp_path, *components: str) -> Path:
    out = tmp_path / "out"
    argv = [
        str(out),
        *(("--components", ",".join(components)) if components else ()),
    ]
    assert snap.snapshot_main([*argv, *unpinnable_argv()]) == 0
    return next(out.glob("kirocrew-snapshot-*.tar.gz"))


def _peek(bundle: Path, tmp_path: Path, tag: str = "peek") -> Path:
    dest = tmp_path / tag
    if not dest.is_dir():
        dest.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(dest)
    return next(p for p in dest.iterdir() if p.is_dir())


def _manifest(bundle: Path, tmp_path: Path, tag: str = "peek") -> dict:
    return json.loads((_peek(bundle, tmp_path, tag) / "MANIFEST.json").read_text())


def _restore(bundle: Path, mode: str, *components: str) -> int:
    argv = [str(bundle), "--mode", mode, "--force"]
    if components:
        argv += ["--components", ",".join(components)]
    return snap.restore_main([*argv, *unpinnable_argv()])


class TestBothComponentsAreDeclaredLikeEveryOther:
    def test_they_are_in_the_component_table(self):
        assert "artifacts" in snap.COMPONENTS
        assert "uploads" in snap.COMPONENTS

    def test_each_declares_a_share_policy(self):
        """The staging path refuses a component with no declaration, so this is not
        cosmetic: an artifact or an upload is whatever someone saved, and a pasted
        token is exactly the content nobody has certified as safe to hand over."""
        for name in ("artifacts", "uploads"):
            assert snap.COMPONENTS[name].policy is snap.SecretPolicy.UNRESOLVED

    def test_the_artifact_component_carries_the_tree_and_not_the_folder_index(self):
        """The index is a record format whose consumers read fields off each entry, so
        carrying it needs its own answer for a record they cannot use. The tree is the
        data a restored host is missing; an artifact whose folder the destination does not
        know is shown at the root by the folder store already."""
        assert snap.COMPONENTS["artifacts"].trees == ("artifacts",)
        assert snap.COMPONENTS["artifacts"].files == ()
        declared = {f for files in snap.CORE_FILES.values() for f in files}
        assert "artifact_folders.json" not in declared

    def test_the_uploads_component_claims_the_uploads_tree(self):
        assert snap.COMPONENTS["uploads"].trees == ("uploads",)

    def test_the_cli_component_list_names_them(self, capsys):
        snap._list_components()
        out = capsys.readouterr().out
        assert "artifacts" in out, out
        assert "uploads" in out, out

    def test_the_documented_table_names_them(self):
        doc = (Path(snap.__file__).parent / "docs" / "snapshot-and-restore.md").read_text(
            encoding="utf-8"
        )
        assert "| artifacts |" in doc, doc
        assert "| uploads |" in doc, doc
        # The gap an operator has to know about before they rely on the backup.
        assert "artifact_folders.json" in doc, "the doc must say the filing is not carried"
        assert "do not ride" in doc.lower(), doc


class TestTheArchiveCarriesThem:
    def test_the_trees_ride_and_the_folder_index_does_not(self, tmp_path, monkeypatch):
        _home(tmp_path, monkeypatch)
        root = _peek(_snapshot(tmp_path), tmp_path)
        assert (root / "artifacts" / "report.md").read_text() == "# quarterly report\n"
        assert (root / "artifacts" / "nested" / "meta.json").is_file()
        assert (root / "uploads" / "shot.png").is_file()
        assert not (
            root / "artifact_folders.json"
        ).exists(), "the folder index is not a declared component file"

    def test_the_manifest_declares_both(self, tmp_path, monkeypatch):
        _home(tmp_path, monkeypatch)
        comps = _manifest(_snapshot(tmp_path), tmp_path)["components"]
        assert comps["artifacts"] == snap.SecretPolicy.UNRESOLVED.value
        assert comps["uploads"] == snap.SecretPolicy.UNRESOLVED.value

    def test_they_are_selectable_independently(self, tmp_path, monkeypatch):
        _home(tmp_path, monkeypatch)
        root = _peek(_snapshot(tmp_path, "artifacts"), tmp_path)
        assert (root / "artifacts" / "report.md").is_file()
        assert not (root / "uploads").exists(), "uploads rode a bundle that did not ask for it"

    def test_an_absent_uploads_directory_is_not_a_failure(self, tmp_path, monkeypatch):
        """The ordinary fresh-install shape. Refusing here would make the whole
        command unusable on a home that has simply never uploaded anything."""
        home = _home(tmp_path, monkeypatch)
        for p in (home / "uploads").iterdir():
            p.unlink()
        (home / "uploads").rmdir()
        root = _peek(_snapshot(tmp_path), tmp_path)
        assert not (root / "uploads").exists()

    def test_an_empty_uploads_directory_is_not_a_failure(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        for p in (home / "uploads").iterdir():
            p.unlink()
        assert snap.snapshot_main([str(tmp_path / "out"), *unpinnable_argv()]) == 0


class TestReplaceRoundTripsThem:
    def test_the_trees_come_back_and_the_live_index_is_left_alone(self, tmp_path, monkeypatch):
        """Not carrying the index means not touching one either: a file the component does
        not declare must survive a replace of that component untouched."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        (home / "artifacts" / "report.md").write_text("clobbered locally\n")
        (home / "artifact_folders.json").write_text('[{"id": "local", "name": "Local"}]')
        (home / "uploads" / "shot.png").write_bytes(b"clobbered")

        assert _restore(bundle, "replace", "artifacts", "uploads") == 0
        assert (home / "artifacts" / "report.md").read_text() == "# quarterly report\n"
        assert (home / "uploads" / "shot.png").read_bytes().startswith(b"\x89PNG")
        assert "Local" in (home / "artifact_folders.json").read_text()

    def test_a_local_file_the_archive_lacks_does_not_survive(self, tmp_path, monkeypatch):
        """Replace means the destination ends up matching the archive -- the same
        promise `workspace` and `skills` already make."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        (home / "artifacts" / "stale.md").write_text("not in the bundle")
        (home / "uploads" / "stale.bin").write_bytes(b"not in the bundle")

        assert _restore(bundle, "replace", "artifacts", "uploads") == 0
        assert not (home / "artifacts" / "stale.md").exists()
        assert not (home / "uploads" / "stale.bin").exists()

    def test_the_previous_state_is_saved_before_it_is_removed(self, tmp_path, monkeypatch):
        """Clearing is only defensible because the state is recoverable, so both trees
        have to be in the pre-restore rollback set rather than only in the archive."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        (home / "artifacts" / "stale.md").write_text("only copy")
        (home / "uploads" / "stale.bin").write_bytes(b"only copy")

        assert _restore(bundle, "replace", "artifacts", "uploads") == 0
        saved_a = list(home.glob("pre-restore-*/artifacts/stale.md"))
        saved_u = list(home.glob("pre-restore-*/uploads/stale.bin"))
        assert saved_a and saved_a[0].read_text() == "only copy"
        assert saved_u and saved_u[0].read_bytes() == b"only copy"

    def test_a_fresh_home_with_neither_directory_restores(self, tmp_path, monkeypatch):
        """The scenario the component exists for: a replacement machine."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        import shutil

        shutil.rmtree(home / "artifacts")
        shutil.rmtree(home / "uploads")

        assert _restore(bundle, "replace", "artifacts", "uploads") == 0
        assert (home / "artifacts" / "report.md").read_text() == "# quarterly report\n"
        assert (home / "uploads" / "shot.png").is_file()


class TestAnAliasInEitherTreeIsRefused:
    """An alias is not "handled somehow": the answer differs by phase, and both answers
    are asserted. At STAGING time an alias is screened out and the omission is recorded
    in the bundle's own manifest. At pre-restore BACKUP time it is refused outright,
    because there the skip is followed by an `rmtree` of the only copy."""

    @requires_symlinks
    def test_a_symlink_inside_a_tree_is_screened_and_recorded(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        secret = home / "telemetry_salt"
        secret.write_text("do-not-archive")
        (home / "uploads" / "alias.txt").symlink_to(secret)

        bundle = _snapshot(tmp_path, "uploads")
        root = _peek(bundle, tmp_path)
        assert not (root / "uploads" / "alias.txt").exists()
        skipped = _manifest(bundle, tmp_path)["skipped"]
        assert any("alias.txt" in (e.get("path") or "") for e in skipped), skipped

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink semantics")
    def test_a_hardlink_inside_a_tree_is_screened(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        os.link(home / "telemetry_salt", home / "artifacts" / "alias.txt")
        root = _peek(_snapshot(tmp_path, "artifacts"), tmp_path)
        assert not (root / "artifacts" / "alias.txt").exists()

    @requires_symlinks
    def test_a_tree_root_swapped_for_a_link_refuses_the_snapshot(
        self, tmp_path, monkeypatch, capsys
    ):
        """The root itself, which per-file pinning cannot answer for: a swapped root
        would stage whatever it names under the component's name."""
        home = _home(tmp_path, monkeypatch)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secrets.txt").write_text("not ours")
        import shutil

        shutil.rmtree(home / "uploads")
        (home / "uploads").symlink_to(elsewhere, target_is_directory=True)

        out = tmp_path / "refused"
        rc = snap.snapshot_main([str(out), "--components", "uploads", *unpinnable_argv()])
        assert rc != 0, "a redirected component root produced a bundle"
        # The MESSAGE is asserted, not only the exit code: an unknown component name also
        # exits non-zero, so a bare `rc != 0` would pass for a component that does not
        # exist at all and would prove nothing about the root check.
        printed = capsys.readouterr().out
        assert "does not resolve inside the data home" in printed, printed
        assert not list(out.glob("*.tar.gz"))
        assert not (tmp_path / "refused" / "secrets.txt").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink semantics")
    def test_a_hardlink_refuses_the_replace_instead_of_deleting_the_only_copy(
        self, tmp_path, monkeypatch, capsys
    ):
        """The pre-restore backup pass is where an alias costs data rather than
        completeness: replace runs `rmtree` right after it, so a skipped alias is a
        file with no copy anywhere. Refusing before any mutation is the only ordering
        that cannot lose it."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        payload = home / "artifacts" / "kept.md"
        payload.write_text("the only copy")
        os.link(payload, home / "artifacts" / "alias.md")

        rc = _restore(bundle, "replace", "artifacts")
        assert rc != 0, "the replace ran past an alias it could not copy"
        # Same reason as above: the refusal has to be THIS one. An unknown component
        # exits non-zero too, and so does a bundle it cannot read.
        printed = capsys.readouterr().out
        assert "hardlink alias" in printed, printed
        assert payload.read_text() == "the only copy", "the only copy was deleted"
        assert (home / "artifacts" / "alias.md").exists()


class TestAnEmptyLibraryDoesNotVetoTheWholeRestore:
    """The cost of declaring two components most homes leave empty.

    `restore_main` refuses a bundle that DECLARES a component and carries no payload for
    it, because replace clears live state for it and then has nothing to put back. That
    premise is true for `memory`, whose tree loop clears unconditionally, and false for
    `artifacts` and `uploads`, which are legitimately empty on a home that never made one
    and whose restore is entered only `if sd.is_dir()`. Before the scope was narrowed the
    product's own snapshot of an ordinary home refused its own restore -- memory, config,
    crons and all -- over two components that would have touched nothing.
    """

    def test_a_bundle_from_a_home_with_neither_still_restores_everything_else(
        self, tmp_path, monkeypatch, capsys
    ):
        home = _home(tmp_path, monkeypatch)
        import shutil

        shutil.rmtree(home / "artifacts")
        shutil.rmtree(home / "uploads")
        bundle = _snapshot(tmp_path)
        assert "artifacts" in _manifest(bundle, tmp_path)["components"], "premise gone"

        (home / "workspace" / "memory" / "preferences.md").write_text("local edit\n")
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert (home / "workspace" / "memory" / "preferences.md").read_text() != "local edit\n"

    def test_the_live_library_is_untouched_by_a_bundle_that_carries_none(
        self, tmp_path, monkeypatch
    ):
        """Not refusing is only defensible because nothing is cleared."""
        home = _home(tmp_path, monkeypatch)
        import shutil

        shutil.rmtree(home / "artifacts")
        shutil.rmtree(home / "uploads")
        bundle = _snapshot(tmp_path)

        (home / "artifacts").mkdir()
        (home / "artifacts" / "local.md").write_text("made after the snapshot")
        assert (
            snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
            == 0
        )
        assert (home / "artifacts" / "local.md").read_text() == "made after the snapshot"

    def test_a_rolled_back_restore_keeps_a_tree_it_never_replaced(
        self, tmp_path, monkeypatch, capsys
    ):
        """The rollback half of the same promise, and why the save is conditional.

        Phase one saved every wanted tree that existed live, including one the archive
        does not carry -- and a mutation failing later put that saved copy back, deleting
        whatever the live tree gained in between. The dashboard writes an artifact in
        exactly that window, so the loss is of data no restore ever offered to replace.
        """
        home = _home(tmp_path, monkeypatch)
        import shutil

        shutil.rmtree(home / "artifacts")
        bundle = _snapshot(tmp_path)
        assert "artifacts" in _manifest(bundle, tmp_path)["components"], "premise gone"

        (home / "artifacts").mkdir()
        (home / "artifacts" / "kept.md").write_text("saved before the restore")

        def fail_after_a_concurrent_save(*args, **kwargs):
            (home / "artifacts" / "imported.md").write_text("saved while the restore ran")
            raise OSError("disk full")

        monkeypatch.setattr(snap, "_backup_and_copy", fail_after_a_concurrent_save)
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert (home / "artifacts" / "kept.md").is_file(), out
        assert (home / "artifacts" / "imported.md").read_text() == "saved while the restore ran"

    def test_a_hollow_memory_declaration_is_still_refused(self, tmp_path, monkeypatch, capsys):
        """The case the guard was written for, which the narrowing must not reach."""
        home = _home(tmp_path, monkeypatch)
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            '{"version": 4, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "hollow.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        live = (home / "workspace" / "memory" / "preferences.md").read_text()
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        out = capsys.readouterr().out
        assert rc != 0, out
        assert "carries no data" in out, out
        assert (home / "workspace" / "memory" / "preferences.md").read_text() == live

    def test_naming_an_empty_component_explicitly_is_still_reported(
        self, tmp_path, monkeypatch, capsys
    ):
        """The asymmetry is deliberate. Restoring from the manifest means "whatever rode",
        where an empty component is not an error; naming it means the operator asked for
        it, and answering that with a silent no-op tells them nothing."""
        home = _home(tmp_path, monkeypatch)
        import shutil

        shutil.rmtree(home / "uploads")
        bundle = _snapshot(tmp_path)
        rc = _restore(bundle, "replace", "uploads")
        out = capsys.readouterr().out
        assert rc != 0, out
        assert "does not contain" in out, out

    @pytest.mark.parametrize("comp", snap._WHOLE_TREE_COMPONENTS)
    def test_a_component_left_off_the_set_really_does_clear_nothing(
        self, comp, tmp_path, monkeypatch
    ):
        """The BEHAVIOUR the set claims, driven per component rather than asserted as
        membership. A component absent from the set is one whose replace touches nothing
        when the bundle carries no payload for it -- so each one is handed a bundle that
        declares it and holds none of it, and its live tree has to survive. A future
        component whose restore starts clearing unconditionally fails here rather than
        passing a set-membership check that says nothing about what it does."""
        home = _home(tmp_path, monkeypatch)
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            json.dumps({"version": 4, "components": {comp: "unresolved"}}), encoding="utf-8"
        )
        bundle = tmp_path / f"hollow-{comp}.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        witness = home / snap.COMPONENTS[comp].trees[0] / "witness.md"
        witness.parent.mkdir(parents=True, exist_ok=True)
        witness.write_text("the live copy")
        assert (
            snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
            == 0
        )
        assert witness.read_text() == "the live copy", f"{comp} cleared live state"

    def test_every_component_is_reachable_by_a_restore_path(self):
        """The two tuples are how restore finds a component at all. One added to
        `COMPONENTS` and to neither tuple is staged, listed by `--list-components` and
        declared in the manifest, and then restored by nothing -- a snapshot that carries
        it and a restore that silently drops it, which is the failure this whole change
        exists to remove."""
        routed = set(snap._CORE_FILE_COMPONENTS) | set(snap._WHOLE_TREE_COMPONENTS)
        assert routed == set(snap.COMPONENTS), (
            "every component must be restored by one of the two paths; unrouted: "
            f"{sorted(set(snap.COMPONENTS) - routed)}"
        )


class TestMergeDoesNotRestoreTheseComponents:
    """Both are `--mode replace` only. A merge copies file by file and never overwrites,
    and that granularity does not fit a library: an artifact is a directory whose files
    describe each other, and a slug comes from the artifact's name, so two machines can
    hold the same slug for unrelated content. Replace swaps the tree whole with the
    pre-restore backup taken first, which needs no reconciliation rule at all.

    The promise here is not just "merge does not import them" — it is that merge SAYS so,
    imports everything else, and refuses outright when they are all that was asked for. A
    run that imports nothing and prints success is the failure this component exists to
    remove."""

    def test_a_merge_of_everything_skips_them_by_name_and_imports_the_rest(
        self, tmp_path, monkeypatch, capsys
    ):
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        (home / "artifacts" / "report.md").write_text("mine")
        (home / "uploads" / "shot.png").write_bytes(b"mine")
        (home / "workspace" / "memory" / "preferences.md").unlink()

        rc = snap.restore_main([str(bundle), "--mode", "merge", "--force", *unpinnable_argv()])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "artifacts: SKIPPED" in out, out
        assert "uploads: SKIPPED" in out, out
        assert "--mode replace" in out, out
        # Skipped means untouched, not half-imported.
        assert (home / "artifacts" / "report.md").read_text() == "mine"
        assert (home / "uploads" / "shot.png").read_bytes() == b"mine"
        # And the rest of the bundle still merged.
        assert (home / "workspace" / "memory" / "preferences.md").is_file()

    def test_no_success_tick_is_printed_for_a_component_it_skipped(
        self, tmp_path, monkeypatch, capsys
    ):
        """A tick for a component that imported nothing is the silent-success shape the
        problem statement condemns, so the skip returns before the tick."""
        _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        assert (
            snap.restore_main([str(bundle), "--mode", "merge", "--force", *unpinnable_argv()]) == 0
        )
        out = capsys.readouterr().out
        assert "✅ artifacts" not in out, out
        assert "✅ uploads" not in out, out
        assert "✅ workspace" in out, out

    @pytest.mark.parametrize("selection", [("artifacts",), ("uploads",), ("artifacts", "uploads")])
    def test_a_merge_of_only_these_is_refused(self, selection, tmp_path, monkeypatch, capsys):
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        (home / "artifacts" / "report.md").write_text("mine")

        rc = _restore(bundle, "merge", *selection)
        out = capsys.readouterr().out
        assert rc != 0, out
        assert "--mode replace" in out, out
        assert (home / "artifacts" / "report.md").read_text() == "mine"

    def test_a_mixed_explicit_selection_merges_the_supported_half(
        self, tmp_path, monkeypatch, capsys
    ):
        """The refusal is for a request that could import nothing, not for naming them at
        all alongside something mergeable."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        (home / "workspace" / "memory" / "preferences.md").unlink()

        rc = _restore(bundle, "merge", "artifacts", "workspace")
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "artifacts: SKIPPED" in out, out
        assert (home / "workspace" / "memory" / "preferences.md").is_file()

    def test_replace_is_the_mode_the_refusal_points_at(self, tmp_path, monkeypatch):
        """The named alternative has to actually work, or the message sends the operator
        nowhere."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        (home / "artifacts" / "report.md").write_text("mine")
        (home / "uploads" / "shot.png").write_bytes(b"mine")

        assert _restore(bundle, "replace", "artifacts", "uploads") == 0
        assert (home / "artifacts" / "report.md").read_text() == "# quarterly report\n"
        assert (home / "uploads" / "shot.png").read_bytes().startswith(b"\x89PNG")

    def test_the_declaration_matches_what_merge_does(self):
        routed = set(snap._REPLACE_ONLY_COMPONENTS)
        assert routed == {"artifacts", "uploads"}
        assert routed <= set(snap._WHOLE_TREE_COMPONENTS), "a replace-only component is a tree"
        assert routed <= set(snap.COMPONENTS)


class TestTheMergePreflightAnswersOnlyForWhatItWrites:
    """`_refuse_unsafe_destination_roots` runs at the top of both modes. Handing it the whole
    selection made a merge refuse over a root it was about to skip: a home that keeps
    `uploads/` on another disk behind a link could not merge its memory at all. Two review
    lanes found this from opposite ends, which is the signal that the basis was wrong rather
    than that an edge case was missed."""

    def _link_root_outside(self, tmp_path: Path, home: Path, name: str) -> Path:
        import shutil

        elsewhere = tmp_path / f"other-disk-{name}"
        elsewhere.mkdir()
        shutil.rmtree(home / name)
        (home / name).symlink_to(elsewhere, target_is_directory=True)
        return elsewhere

    @requires_symlinks
    def test_a_linked_uploads_root_does_not_block_merging_everything_else(
        self, tmp_path, monkeypatch, capsys
    ):
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        self._link_root_outside(tmp_path, home, "uploads")
        (home / "workspace" / "memory" / "preferences.md").unlink()

        rc = snap.restore_main([str(bundle), "--mode", "merge", "--force", *unpinnable_argv()])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "uploads: SKIPPED" in out, out
        assert (home / "workspace" / "memory" / "preferences.md").is_file()

    @requires_symlinks
    def test_a_linked_root_on_a_component_merge_does_write_still_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        """The narrowing must not reach a component the mode actually writes."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        outside = self._link_root_outside(tmp_path, home, "skills")

        rc = _restore(bundle, "merge", "skills")
        out = capsys.readouterr().out
        assert rc != 0, out
        assert "do not resolve inside the data home" in out, out
        assert not any(outside.iterdir()), "the merge wrote through the link"

    @requires_symlinks
    def test_replace_still_refuses_the_linked_uploads_root(self, tmp_path, monkeypatch, capsys):
        """Replace DOES write these components, so its pre-flight still answers for them."""
        home = _home(tmp_path, monkeypatch)
        bundle = _snapshot(tmp_path)
        outside = self._link_root_outside(tmp_path, home, "uploads")

        rc = _restore(bundle, "replace", "uploads")
        out = capsys.readouterr().out
        assert rc != 0, out
        assert "do not resolve inside the data home" in out, out
        assert not any(outside.iterdir()), "the replace wrote through the link"
