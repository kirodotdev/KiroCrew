"""Two more places a rule justified for one member kind was applied as if that kind were
the only one: the archive's member-name scan skipped directories, and replace's per-file
presence evidence was accepted for a component's directories.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "bundle"
    stage.mkdir()
    (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
    return stage


class TestADirectoryNameIsAMemberName:
    """The archive stores directory members, so their names leave the host too."""

    def test_an_empty_directory_named_after_a_credential_is_refused(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        (stage / "workspace" / f"key={KEY}").mkdir(parents=True)

        with pytest.raises(redact.OpaqueFilesPresent) as e:
            redact.redact_bundle_for_egress(stage)

        assert KEY in str(e.value), str(e.value)

    def test_a_clean_directory_tree_still_passes(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        (stage / "workspace" / "notes").mkdir(parents=True)

        redact.redact_bundle_for_egress(stage)


class TestReplaceWillNotClearATreeTheBundleLacks:
    """Per-component presence is `any declared path`; replace clears whole directories."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "workspace" / "memory").mkdir(parents=True)
        (home / "workspace" / "memory" / "keep.md").write_text("mine", encoding="utf-8")
        (home / "memory.db").write_bytes(b"")
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        return home

    def _partial_bundle(self, tmp_path: Path) -> Path:
        """A partial bundle carrying memory.db and NEITHER of memory's trees."""
        payload = tmp_path / "kirocrew-partial-20260101T000000Z"
        payload.mkdir(parents=True)
        conn = snap.sqlite3.connect(str(payload / "memory.db"))
        conn.execute("CREATE TABLE semantic_memory (key, value_json)")
        conn.commit()
        conn.close()
        payload.joinpath("MANIFEST.json").write_text(json.dumps({"version": 3}), encoding="utf-8")
        bundle = tmp_path / "partial.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)
        return bundle

    def test_the_live_tree_survives(self, home, tmp_path, capsys) -> None:
        """The point of the refusal: without it, replace clears this and refills nothing."""
        bundle = self._partial_bundle(tmp_path)

        snap.restore_main([str(bundle), "--mode", "replace", "--force", "--components", "memory"])

        assert (home / "workspace" / "memory" / "keep.md").is_file(), capsys.readouterr().out

    def test_replace_refuses_and_keeps_the_live_tree(self, home, tmp_path, capsys) -> None:
        bundle = self._partial_bundle(tmp_path)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "workspace/memory" in out, out
        assert (home / "workspace" / "memory" / "keep.md").is_file(), "live tree was cleared"

    def test_merge_still_accepts_it_because_merge_clears_nothing(
        self, home, tmp_path, capsys
    ) -> None:
        bundle = self._partial_bundle(tmp_path)

        rc = snap.restore_main(
            [str(bundle), "--mode", "merge", "--force", "--components", "memory"]
        )

        out = capsys.readouterr().out
        assert rc == 0, out
        assert (home / "workspace" / "memory" / "keep.md").is_file(), out
