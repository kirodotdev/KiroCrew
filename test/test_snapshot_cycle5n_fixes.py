"""A rollback copy must be faithful; an archive's member names must be clean too."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _tree_with_nested_link(tmp_path: Path) -> Path:
    live = tmp_path / "workspace"
    (live / "sub").mkdir(parents=True)
    (live / "sub" / "real.md").write_text("mine\n", encoding="utf-8")
    (live / "sub" / "pointer.md").symlink_to(live / "sub" / "real.md")
    return live


class TestARollbackCopyReproducesTheTree:
    """A link the copy drops is a link recovery deletes."""

    def test_a_nested_link_survives_as_a_link(self, tmp_path: Path) -> None:
        live = _tree_with_nested_link(tmp_path)
        backup = tmp_path / "backup" / "workspace"

        snap._copytree_rollback(live, backup)

        copied = backup / "sub" / "pointer.md"
        assert copied.is_symlink(), "the rollback set is missing a node the live tree has"

    def test_the_link_is_recreated_not_followed(self, tmp_path: Path) -> None:
        """Faithful must not mean dereferencing: nothing outside the tree may be read."""
        outside = tmp_path / "outside-secret"
        outside.write_text("not part of the tree\n", encoding="utf-8")
        live = tmp_path / "workspace"
        live.mkdir()
        (live / "escape").symlink_to(outside)
        backup = tmp_path / "backup" / "workspace"

        snap._copytree_rollback(live, backup)

        copied = backup / "escape"
        assert copied.is_symlink()
        assert not copied.is_file() or copied.resolve() == outside.resolve()
        assert "not part of the tree" not in (backup / "escape").readlink().name

    def test_the_egress_copy_still_drops_links(self, tmp_path: Path) -> None:
        """The rollback change must not relax the copy that feeds the uploaded bundle."""
        live = _tree_with_nested_link(tmp_path)
        out = tmp_path / "egress" / "workspace"

        snap._copytree_safe(live, out)

        assert (out / "sub" / "real.md").is_file()
        assert not (out / "sub" / "pointer.md").exists(), "a link reached the bundle"

    def test_no_rollback_path_uses_the_link_dropping_copy(self) -> None:
        """BOTH directions. The save and the restore are one round-trip.

        The first version of this guard matched only calls whose arguments mentioned
        `backup`, which is the SAVE direction -- so the recovery call, spelled
        `(saved, target)`, was invisible to it and stayed on the link-dropping copy for
        several rounds. A guard scoped to half a round-trip protects half a round-trip.
        """
        src = inspect.getsource(snap)
        rollback = inspect.getsource(snap._do_replace) + inspect.getsource(
            snap._restore_everything_from_rollback
        )
        offenders = re.findall(r"_copytree_safe\([^)]*\)", rollback)
        assert offenders == [], f"a rollback path still drops links: {offenders}"
        # The outbound copy must still exist and still drop them.
        assert "_copytree_safe(" in src

    def test_a_junction_refuses_before_anything_is_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A junction reports as a link but is not a symlink, so it cannot be recreated.

        Windows-only as a real filesystem object, so the SHAPE is expressed here instead:
        a plain directory that the platform predicate calls a link. Creating a junction on
        this host is impossible, and a test that skips is a test that never runs.
        """
        live = tmp_path / "workspace"
        (live / "junction").mkdir(parents=True)
        backup = tmp_path / "backup" / "workspace"

        monkeypatch.setattr(
            snap.platform_compat,
            "is_link_or_junction",
            lambda p: str(p).endswith("junction"),
        )

        with pytest.raises(snap.RollbackNotFaithful) as caught:
            snap._copytree_rollback(live, backup)

        assert "junction" in str(caught.value)
        assert not backup.exists(), "a partial rollback set was left behind"

    def test_the_refusal_is_an_oserror_so_restore_reports_it(self) -> None:
        """Restore's IO handling turns this into a refusal; a bare Exception escapes."""
        assert issubclass(snap.RollbackNotFaithful, OSError)


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
