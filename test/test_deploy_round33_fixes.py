"""R33 regression tests (round-33 Codex findings on c1771f1).

F1: containment must be pinned to the OPENED fd — a nested directory swapped
    for a symlink after the tree walk must not smuggle files from outside the
    approved tree (O_NOFOLLOW only guards the final component).
F2: a file exceeding the per-file read cap must surface as a structured
    staging rejection (RuntimeError -> 409), not an escaping 500.
"""
from pathlib import Path

import pytest

from kiro_crew import hooks as hooks_mod
from kiro_crew.hooks import safe_read_file_bytes_nolink

REPO = Path(__file__).resolve().parents[1]
HANDLERS = (REPO / "src" / "kiro_crew" / "deploy" / "handlers.py").read_text(encoding="utf-8")


class TestF1FdPinnedContainment:
    def test_reads_file_inside_root(self, tmp_path):
        f = tmp_path / "app" / "index.html"
        f.parent.mkdir()
        f.write_text("ok")
        assert safe_read_file_bytes_nolink(str(f), within_root=str(tmp_path / "app")) == b"ok"

    def test_rejects_file_outside_root(self, tmp_path):
        root = tmp_path / "app"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("leak")
        assert safe_read_file_bytes_nolink(str(outside), within_root=str(root)) is None

    def test_rejects_nested_symlink_escape(self, tmp_path):
        # simulate the post-walk swap: a dir component inside root is a
        # symlink pointing outside — the opened fd's real path escapes root.
        root = tmp_path / "app"
        root.mkdir()
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        (victim_dir / "secret.txt").write_text("secret")
        (root / "sub").symlink_to(victim_dir)
        assert safe_read_file_bytes_nolink(
            str(root / "sub" / "secret.txt"), within_root=str(root)
        ) is None

    def test_no_root_keeps_prior_behavior(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("y")
        assert safe_read_file_bytes_nolink(str(f)) == b"y"

    def test_staging_passes_within_root(self):
        assert "within_root=str(source)" in HANDLERS


class TestF2FileTooLargeStructured:
    def test_oversized_file_raises_runtime_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 8)
        f = tmp_path / "app" / "big.bin"
        f.parent.mkdir()
        f.write_bytes(b"0123456789ABCDEF")
        with pytest.raises(hooks_mod.FileTooLargeError):
            safe_read_file_bytes_nolink(str(f), within_root=str(f.parent))

    def test_staging_converts_to_runtime_error(self):
        # the staging loop must catch FileTooLargeError and re-raise as the
        # structured RuntimeError that the deploy path converts to a 409.
        assert "except FileTooLargeError" in HANDLERS
        assert "file-too-large:" in HANDLERS
