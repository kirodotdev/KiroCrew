"""A rollback copy must be faithful; an archive's member names must be clean too."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _tree_with_nested_link(tmp_path: Path) -> Path:
    live = tmp_path / "workspace"
    (live / "sub").mkdir(parents=True)
    (live / "sub" / "real.md").write_text("mine\n", encoding="utf-8")
    (live / "sub" / "pointer.md").symlink_to(live / "sub" / "real.md")
    return live


class TestARollbackSaveRefusesATreeWithALink:
    """A rollback set must never hold a link — so the SAVE refuses one, up front.

    The premise changed with the descriptor-pinned staging module. The rollback copy
    used to PRESERVE links (a link the copy dropped was a link recovery deleted). Now
    the save (`_backup_tree_or_refuse`) reports a skipped entry as FATAL, so a tree
    containing a link is REFUSED before any incomplete rollback set is written — and
    because the save happens in phase one, before any live mutation, the refusal leaves
    the data home untouched. The consequence is that nothing in a rollback directory can
    contain a link, so the RESTORE no longer needs a link-preserving copy: it uses
    `_copytree_safe`, and there is nothing for it to preserve on the way back.
    """

    def test_a_nested_link_makes_the_save_refuse(self, tmp_path: Path) -> None:
        live = _tree_with_nested_link(tmp_path)
        backup = tmp_path / "backup" / "workspace"
        backup.parent.mkdir(parents=True)

        with pytest.raises(snap.pinned_fs.PinnedPathRefusal):
            snap._backup_tree_or_refuse(live, backup)

    def test_the_link_is_not_followed_when_the_save_refuses(self, tmp_path: Path) -> None:
        """Refusing must not mean dereferencing: nothing outside the tree may be read."""
        outside = tmp_path / "outside-secret"
        outside.write_text("not part of the tree\n", encoding="utf-8")
        live = tmp_path / "workspace"
        live.mkdir()
        (live / "escape").symlink_to(outside)
        backup = tmp_path / "backup" / "workspace"
        backup.parent.mkdir(parents=True)

        with pytest.raises(snap.pinned_fs.PinnedPathRefusal):
            snap._backup_tree_or_refuse(live, backup)

        # The refusal read the link's own name, never its target's contents.
        copied = backup / "escape"
        assert not copied.is_file() or "not part of the tree" not in copied.read_text(
            encoding="utf-8"
        )

    def test_the_egress_copy_still_drops_links(self, tmp_path: Path) -> None:
        """The staging change must not relax the copy that feeds the uploaded bundle.

        `_copytree_safe` now delegates to the descriptor-pinned primitive, whose
        documented contract is that the caller creates the destination's parent -- so
        `out.parent` is made first, then the copy runs and must still drop the link.
        """
        live = _tree_with_nested_link(tmp_path)
        out = tmp_path / "egress" / "workspace"
        out.parent.mkdir(parents=True)

        snap._copytree_safe(live, out, allow_unpinned=bool(unpinnable_argv()))

        assert (out / "sub" / "real.md").is_file()
        assert not (out / "sub" / "pointer.md").exists(), "a link reached the bundle"

    def test_the_rollback_restore_uses_the_ordinary_staging_copy(self) -> None:
        """BOTH directions of the round-trip route through `_copytree_safe`.

        The save refuses any tree with a link, so whatever lands in the rollback
        directory is links-free by construction and the recovery copy has nothing to
        preserve. Inverts the earlier guard, which forbade `_copytree_safe` on the
        rollback path back when a bespoke link-preserving copy lived there: the property
        now is that the recovery copy IS `_copytree_safe`, and that the save that feeds
        it is the fatal-reporter refusal rather than a silent link-dropping copy.
        """
        setup = inspect.getsource(snap._do_replace)
        recovery = inspect.getsource(snap._restore_everything_from_rollback)

        # The save that feeds the rollback set must refuse, not silently drop links.
        assert "_backup_tree_or_refuse(" in setup, (
            "the rollback save no longer refuses a tree with a link; an incomplete "
            "rollback set could be written and then rmtree'd over"
        )
        # The recovery copy is the ordinary staging copy, deliberately.
        assert "_copytree_safe(" in recovery, (
            "recovery must restore with _copytree_safe now that the save guarantees the "
            "rollback set holds no links"
        )
        # The deleted link-preserving helper must not reappear on either half.
        assert "_copytree_rollback" not in setup + recovery, (
            "the link-preserving rollback copy was removed; its premise (a saved link to "
            "recreate) no longer exists because the save refuses links"
        )

    def test_the_refusal_type_is_contained_by_restore(self) -> None:
        """restore_main catches this refusal and turns it into a clean rc=1.

        The old helper raised a bespoke OSError subclass so restore's IO handling would
        report it. The save now raises `PinnedPathRefusal`, which `restore_main` catches
        explicitly (alongside `UnsafeComponentRoot`), so an incomplete-rollback refusal
        still becomes a reported refusal rather than a traceback.
        """
        restore_src = inspect.getsource(snap.restore_main)
        assert "except pinned_fs.PinnedPathRefusal" in restore_src


class TestArchiveMemberNamesAreScanned:
    def test_a_credential_in_a_filename_refuses_the_upload(self, tmp_path: Path) -> None:
        """Contents can be spotless while the tar's member name carries the key."""
        stage = tmp_path / "bundle"
        stage.mkdir()
        (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
        bad = stage / "workspace" / f"creds-{KEY}.md"
        bad.parent.mkdir(parents=True)
        bad.write_text("contents are clean\n", encoding="utf-8")

        with pytest.raises(redact.OpaqueFilesPresent) as caught:
            redact.redact_bundle_for_egress(stage)

        assert any(KEY in p for p in caught.value.paths)
        assert bad.is_file(), "an operator's file must not be deleted for its name"

    def test_an_ordinary_name_is_untouched(self, tmp_path: Path) -> None:
        stage = tmp_path / "bundle"
        stage.mkdir()
        (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
        ok = stage / "workspace" / "notes.md"
        ok.parent.mkdir(parents=True)
        ok.write_text("nothing to see\n", encoding="utf-8")

        report = redact.redact_bundle_for_egress(stage)

        assert ok.is_file()
        assert report.dropped == []
