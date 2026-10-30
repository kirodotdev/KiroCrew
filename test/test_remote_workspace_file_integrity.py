"""Guest file searches stay confined and exact edits preserve untouched bytes."""

import os

import pytest

from kiro_crew.remote_session.workspace_tools import Server, ToolError, Workspace


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
    assert set(ws.grep("needle", ".", include="*.txt").splitlines()) == {
        "inside.txt:1: needle public",
        "alias.txt:1: needle public",
    }
    assert ws.grep("private", ".") == "(no matches)"


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


@pytest.mark.parametrize("pattern", ["a{4294967296}", "(" * 1000 + ")" * 1000])
def test_a_regex_that_overflows_is_a_tool_error_not_a_dead_server(tmp_path, pattern):
    (tmp_path / "f.txt").write_text("a\n")
    server = Server(Workspace(str(tmp_path)))
    call = {"name": "grep", "arguments": {"pattern": pattern, "path": "."}}
    reply = server.handle({"id": 1, "method": "tools/call", "params": call})
    assert reply["result"]["isError"] is True
    # The server answers the next call: one bad pattern does not end the session.
    ok = {"name": "grep", "arguments": {"pattern": "a", "path": "."}}
    assert (
        server.handle({"id": 2, "method": "tools/call", "params": ok})["result"]["isError"] is False
    )


def test_an_unexpected_error_in_a_tool_is_returned_as_an_error(tmp_path, monkeypatch):
    server = Server(Workspace(str(tmp_path)))
    monkeypatch.setitem(server.tools, "glob", lambda **_: (_ for _ in ()).throw(MemoryError()))
    call = {"name": "glob", "arguments": {"pattern": "*", "path": "."}}
    reply = server.handle({"id": 1, "method": "tools/call", "params": call})
    assert reply["result"] == {
        "content": [{"type": "text", "text": "glob failed: MemoryError"}],
        "isError": True,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX guest root")
def test_a_root_workdir_admits_its_descendants(tmp_path):
    # ``/`` is an accepted workdir; every absolute path is inside it.
    ws = Workspace("/")
    target = tmp_path / "main.py"
    target.write_text("print(1)\n")
    assert ws.resolve(str(target)) == os.path.realpath(target)
    assert "print(1)" in ws.read_file(str(target))


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("**/*.py", ["main.py", "src/deep/nested.py", "src/top.py"]),
        ("src/*.py", ["src/top.py"]),
        ("src/**/*.py", ["src/deep/nested.py", "src/top.py"]),
        ("*.py", ["main.py", "src/deep/nested.py", "src/top.py"]),
        ("src/?op.py", ["src/top.py"]),
        ("src/[!t]*.py", ["(no matches)"]),
    ],
)
def test_glob_matches_path_segments(tmp_path, pattern, expected):
    (tmp_path / "src" / "deep").mkdir(parents=True)
    for rel in ("main.py", "src/top.py", "src/deep/nested.py", "src/notes.txt"):
        (tmp_path / rel).write_text("")
    found = Workspace(str(tmp_path)).glob(pattern, ".").splitlines()
    # Results use the host's separator; the guest that serves them is POSIX.
    assert sorted(f.replace(os.sep, "/") for f in found) == expected


def test_paging_counts_a_line_longer_than_one_bounded_read_once(tmp_path):
    from kiro_crew.remote_session.workspace_tools import MAX_READ_BYTES

    path = tmp_path / "wide.txt"
    path.write_bytes(b"x" * (MAX_READ_BYTES * 2 + 7) + b"\nsecond\nthird\n")
    assert Workspace(str(tmp_path)).read_file(path.name, offset=1, limit=1) == "second\n"
