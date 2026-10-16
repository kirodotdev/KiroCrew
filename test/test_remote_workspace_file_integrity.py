"""Guest file searches stay confined and exact edits preserve untouched bytes."""

import os

import pytest

from kiro_crew.remote_session.workspace_tools import ToolError, Workspace


def test_recursive_grep_skips_escaping_file_links(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("needle private\n")
    (root / "inside.txt").write_text("needle public\n")
    (root / "escape.txt").symlink_to(outside)
    (root / "alias.txt").symlink_to(root / "inside.txt")
    (root / "missing.txt").symlink_to(tmp_path / "missing")
    ws = Workspace(str(root))
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.read_file("escape.txt")
    assert set(ws.grep("needle", include="*.txt").splitlines()) == {
        "inside.txt:1: needle public",
        "alias.txt:1: needle public",
    }
    assert ws.grep("private") == "(no matches)"


@pytest.mark.parametrize("replace_all", [False, True])
@pytest.mark.parametrize("newline", [b"\r\n", b"\r", b"\n"])
def test_exact_edit_preserves_unmatched_newline_bytes(tmp_path, newline, replace_all):
    path = tmp_path / "text.txt"
    original = b"old" + newline + b"untouched\r\nnext\rfinal\n"
    if replace_all:
        original += b"old" + newline
    path.write_bytes(original)
    ws = Workspace(os.fspath(tmp_path))
    old = "old" + newline.decode()
    ws.edit_file(path.name, old, "new\r\n", replace_all=replace_all)
    assert path.read_bytes() == original.replace(old.encode(), b"new\r\n")


def test_exact_edit_does_not_match_a_different_newline(tmp_path):
    path = tmp_path / "text.txt"
    original = b"old\r\nuntouched\r\n"
    path.write_bytes(original)
    with pytest.raises(ToolError, match="not found"):
        Workspace(str(tmp_path)).edit_file(path.name, "old\n", "new\n")
    assert path.read_bytes() == original
