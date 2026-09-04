"""Tests for the four defects the review found on `181c09d4b`."""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew.snapshot import restore_main, snapshot_main


def _tagset(pairs: dict[str, str]) -> str:
    return json.dumps({"TagSet": [{"Key": k, "Value": v} for k, v in pairs.items()]})


@pytest.fixture
def src(tmp_path, monkeypatch):
    d = tmp_path / "src"
    _setup_fake_kirocrew(d)
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    return d


def _extract(tarball: Path, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(tarball)) as t:
        t.extractall(into, filter=lambda m, _d="": m)
    return next(
        d for d in into.iterdir() if d.name.startswith(("kirocrew-snapshot-", "kirocrew-partial-"))
    )


def _snapshot(out: Path, extra: list[str]) -> Path:
    # A selective bundle is now named `kirocrew-partial-*` so an older restore refuses
    # it rather than silently relocating the core files it lacks; a complete one keeps
    # the `kirocrew-snapshot-*` name. Match both so this helper works for either.
    assert snapshot_main([str(out)] + extra + unpinnable_argv()) == 0
    return sorted(out.glob("kirocrew-*.tar.gz"))[-1]


class TestSymlinkedTreeRootsAreRefused:
    """`is_dir()` follows a link, so a linked component root would export its target."""

    @pytest.mark.skipif(
        os.name == "nt",
        reason="the 'symlinked root resolves outside and is not followed into the bundle' "
        "guarantee is enforced by the descriptor-pinned staging walk; without dir_fd "
        "staging refuses up front (PinnedPathRefusal) before that guarantee can be asserted",
    )
    def test_a_linked_component_root_is_not_followed_into_the_bundle(self, src, tmp_path, capsys):
        secret_dir = tmp_path / "outside"
        secret_dir.mkdir()
        (secret_dir / "id_rsa").write_text("PRIVATE KEY\n")

        target = src / "workspace/knowledge"
        for p in sorted(target.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        target.rmdir()
        target.symlink_to(secret_dir, target_is_directory=True)

        # The snapshot now REFUSES rather than quietly omitting the tree: a manifest
        # that still declares `memory` while the tree is missing is a backup that lies
        # about its contents. The link is of course still not followed.
        out = tmp_path / "out"
        rc = snapshot_main([str(out), "--components", "memory"])
        assert rc != 0, "a symlinked component root produced a 'successful' backup"
        assert list(out.glob("kirocrew-snapshot-*.tar.gz")) == []
        printed = capsys.readouterr().out
        assert "resolves outside" in printed
        assert "PRIVATE KEY" not in printed

    def test_replace_restore_refuses_a_linked_destination_root(self, src, tmp_path, monkeypatch):
        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])

        dest = tmp_path / "dest"
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "keepme.txt").write_text("not ours to delete\n")
        (dest / "workspace").mkdir(parents=True)
        (dest / "workspace/memory").symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        # Strengthened contract. This previously asserted a SUCCESSFUL restore that
        # merely left the link alone — but the databases were replaced before the tree
        # loop reached the link, so "success" meant memory split between the old and new
        # versions with no warning. The refusal now happens before any mutation.
        rc = restore_main([str(tarball), "--components", "memory", "--mode", "replace", "--force"])
        assert rc == 1, "a linked destination root produced a 'successful' restore"
        assert (outside / "keepme.txt").read_text() == "not ours to delete\n"
        assert (dest / "workspace/memory").is_symlink()
        # Refused BEFORE mutation: no rollback directory was even created.
        assert list(dest.glob("pre-restore-*")) == []


class TestRestoreHonoursTheBundleManifest:
    """A selective bundle must not be restored as if it held everything."""

    def test_a_memory_only_bundle_does_not_displace_the_workspace(self, src, tmp_path, monkeypatch):
        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])

        dest = tmp_path / "dest"
        (dest / "workspace").mkdir(parents=True)
        unrelated = dest / "workspace/my-notes.md"
        unrelated.write_text("months of work\n")
        (dest / "skills").mkdir()
        (dest / "skills/mine").mkdir()
        (dest / "skills/mine/SKILL.md").write_text("my skill\n")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        # No --components: the bundle's manifest is the source of truth.
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0

        assert (
            unrelated.read_text() == "months of work\n"
        ), "an unrelated workspace file was displaced by a memory-only bundle"
        assert (dest / "skills/mine/SKILL.md").read_text() == "my skill\n"
        assert (dest / "workspace/memory/preferences.md").is_file()

    def test_the_defaulted_component_set_is_reported(self, src, tmp_path, monkeypatch, capsys):
        tarball = _snapshot(tmp_path / "out", ["--components", "memory"])
        dest = tmp_path / "dest2"
        dest.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        out = capsys.readouterr().out
        assert "from bundle manifest" in out
        assert "memory" in out

    def test_an_explicit_components_flag_still_wins(self, src, tmp_path, monkeypatch):
        tarball = _snapshot(tmp_path / "out", [])  # full bundle
        dest = tmp_path / "dest3"
        dest.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        assert (
            restore_main(
                [str(tarball), "--components", "crons", "--mode", "replace", "--force"]
                + unpinnable_argv()
            )
            == 0
        )
        assert (dest / "crons.json").is_file()
        # memory was in the bundle but not requested.
        assert not (dest / "memory.db").exists()

    def test_a_full_bundle_still_restores_everything(self, src, tmp_path, monkeypatch):
        tarball = _snapshot(tmp_path / "out", [])
        dest = tmp_path / "dest4"
        dest.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        assert restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()) == 0
        assert (dest / "memory.db").is_file()
        assert (dest / "crons.json").is_file()
        assert (dest / "skills/my-skill/SKILL.md").is_file()

    def test_a_manifest_naming_an_unknown_component_drops_it(self, src, tmp_path, monkeypatch):
        """A bundle from a newer build must not steer this one into an unknown name."""
        from kiro_crew.snapshot import _manifest_components

        snap = tmp_path / "fakesnap"
        snap.mkdir()
        (snap / "MANIFEST.json").write_text(
            json.dumps({"components": {"memory": "no-redaction", "quantum": "no-redaction"}})
        )
        assert _manifest_components(snap) == ["memory"]

    def test_a_pre_manifest_bundle_keeps_all_components(self, tmp_path):
        """No component map (a pre-v3 bundle) means it really did hold everything."""
        from kiro_crew.snapshot import _manifest_components

        snap = tmp_path / "old"
        snap.mkdir()
        (snap / "MANIFEST.json").write_text(json.dumps({"version": 2, "contents": {}}))
        assert _manifest_components(snap) is None
