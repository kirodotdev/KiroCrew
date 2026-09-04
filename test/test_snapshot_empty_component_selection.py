"""Tests for the cycle-4 review findings.

Two independent defects that still live in the product: terminal escape sequences
reaching stdout from an untrusted manifest, and an explicit component list naming
nothing.
"""

from __future__ import annotations

import json
import tarfile

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


class TestManifestOutputCannotDriveTheTerminal:
    def test_escape_sequences_in_manifest_fields_are_neutralised(self, tmp_path, capsys):
        snapdir = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        snapdir.mkdir()
        hostile = "\x1b[2J\x1b[Hrestored OK"
        (snapdir / "MANIFEST.json").write_text(
            json.dumps(
                {
                    "created_at": hostile,
                    "user": hostile,
                    "hostname": hostile,
                    "purpose": hostile,
                    "components": {hostile: hostile},
                }
            ),
            encoding="utf-8",
        )
        snap._print_manifest(snapdir)
        out = capsys.readouterr().out
        assert "\x1b" not in out, "an escape sequence from the manifest reached stdout"

    def test_ordinary_fields_still_render(self, tmp_path, capsys):
        snapdir = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        snapdir.mkdir()
        (snapdir / "MANIFEST.json").write_text(
            json.dumps({"created_at": "2026-01-01", "purpose": "backup"}),
            encoding="utf-8",
        )
        snap._print_manifest(snapdir)
        out = capsys.readouterr().out
        assert "2026-01-01" in out and "backup" in out


class TestAnExplicitButEmptyComponentListIsRefused:
    def test_snapshot_refuses_and_writes_nothing(self, home, tmp_path, capsys):
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", ","])
        assert rc == 1
        assert "names no components" in capsys.readouterr().out
        assert not list(out.glob("*.tar.gz")) if out.exists() else True

    def test_snapshot_refusal_precedes_any_pruning(self, home, tmp_path):
        """The damage was retention counting an empty bundle as the newest backup."""
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / "p"
            payload.mkdir()
            tf.add(str(payload), arcname="kirocrew-snapshot-20260101T000000Z")
        assert snap.snapshot_main([str(out), "--components", ",", "--keep", "1"]) == 1
        assert existing.is_file(), "a refused run must not prune a real backup"

    def test_restore_refuses_rather_than_reporting_a_no_op(self, home, tmp_path, capsys):
        bundle = tmp_path / "b.tar.gz"
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir()
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)
        rc = snap.restore_main([str(bundle), "--components", " , ", "--force"])
        assert rc == 1
        assert "names no components" in capsys.readouterr().out

    def test_a_real_selection_still_works(self, home, tmp_path):
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory", *unpinnable_argv()]) == 0
