"""R33 regression tests (round-33 Codex findings on c1771f1).

F1: containment must be pinned to the OPENED fd — a nested directory swapped
    for a symlink after the tree walk must not smuggle files from outside the
    approved tree (O_NOFOLLOW only guards the final component).
F2: a file exceeding the per-file read cap must surface as a structured
    staging rejection (RuntimeError -> 409), not an escaping 500.
"""

import ctypes
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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
        assert (
            safe_read_file_bytes_nolink(str(root / "sub" / "secret.txt"), within_root=str(root))
            is None
        )

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

    def test_explicit_read_limit_is_honored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 8)
        f = tmp_path / "large.txt"
        f.write_bytes(b"0123456789ABCDEF")
        assert safe_read_file_bytes_nolink(str(f), max_bytes=16) == b"0123456789ABCDEF"


class TestWindowsFdPinnedContainment:
    def test_windows_handle_path_accepts_in_root_and_rejects_escape(self, tmp_path, monkeypatch):
        root = tmp_path / "app"
        root.mkdir()
        inside = root / "index.html"
        inside.write_text("ok")

        class FakeGetFinalPath:
            argtypes = None
            restype = None

            def __init__(self, real_path):
                self.real_path = real_path

            def __call__(self, _handle, buffer, _size, _flags):
                buffer.value = self.real_path
                return len(self.real_path)

        class FakeKernel32:
            def __init__(self, real_path):
                self.GetFinalPathNameByHandleW = FakeGetFinalPath(real_path)

        real_path = str(inside)
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setitem(
            sys.modules,
            "msvcrt",
            SimpleNamespace(get_osfhandle=lambda _fd: 17),
        )
        monkeypatch.setattr(
            ctypes,
            "WinDLL",
            lambda _name, use_last_error=True: FakeKernel32(real_path),
            raising=False,
        )
        # Changing os.name is needed to exercise the Windows descriptor branch,
        # but it also changes pathlib.Path.home() on a non-Windows host. Stub
        # the unrelated path guards so this test remains portable.
        monkeypatch.setattr(hooks_mod, "validate_file_path", lambda raw: raw)
        monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda _path: False)

        assert safe_read_file_bytes_nolink(str(inside), within_root=str(root)) == b"ok"

        real_path = str(tmp_path / "outside.txt")
        (tmp_path / "outside.txt").write_text("secret")
        assert safe_read_file_bytes_nolink(str(inside), within_root=str(root)) is None
