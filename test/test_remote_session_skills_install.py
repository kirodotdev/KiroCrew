"""The guest installer (``remote_session/skills_install.py``) run in-process.

``test_remote_session_skills_copy.py`` runs it the way ``coder ssh`` does, as a
child process, which the coverage gate cannot see; these call it directly.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import skills_install

# The installer runs in a POSIX guest (read-only copies, owner-only modes), as
# ``test_remote_session_skills_copy.py`` also says.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest installer")

DIGEST = "a" * 32


@pytest.fixture
def base(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    monkeypatch.setattr(skills_install, "BASE", str(root))
    return root


def _run(monkeypatch, capsys, argv, header=None, body=b""):
    stdin = b"" if header is None else json.dumps(header).encode() + b"\n" + body
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    skills_install.main(argv)
    return json.loads(capsys.readouterr().out)


def test_install_then_check_reuses_a_read_only_copy(base, monkeypatch, capsys):
    script, doc = b"def broken(:\n", b"# tool\n"
    header = [["s/scripts/x.py", len(script), 0o755], ["s/SKILL.md", len(doc), 0o644]]
    report = _run(monkeypatch, capsys, ["install", DIGEST], header, script + doc)
    assert report["present"] is True and report["root"] == str(base / DIGEST)
    assert [p for p, _r in report["incompatible"]] == [os.path.join("s", "scripts", "x.py")]
    copied = base / DIGEST / "s" / "scripts" / "x.py"
    assert copied.read_bytes() == script and os.access(copied, os.X_OK)
    assert not os.access(base / DIGEST / "s" / "SKILL.md", os.W_OK)
    # Present now: a check (or another install) reads it and sends nothing.
    assert _run(monkeypatch, capsys, ["check", DIGEST])["present"] is True
    assert _run(monkeypatch, capsys, ["check", "b" * 32])["present"] is False
    skills_install.remove_tree(str(base / DIGEST))
    assert not (base / DIGEST).exists()


@pytest.mark.parametrize(
    ("argv", "header", "body", "message"),
    [
        (["copy", DIGEST], None, b"", "usage"),
        (["install", "../x"], None, b"", "usage"),
        (["install", DIGEST], "raw", b"", "not JSON"),
        (["install", DIGEST], {"a": 1}, b"", "bounded list"),
        (["install", DIGEST], [["../e", 1, 0]], b"x", "unsafe skill file path"),
        (["install", DIGEST], [["s/a", -1, 0]], b"", "size or mode"),
        (["install", DIGEST], [["s/a", (32 << 20) + 1, 0]], b"", "size bound"),
        (["install", DIGEST], [["s/a", 5, 0]], b"ab", "truncated"),
    ],
)
def test_the_installer_refuses_bad_input(base, monkeypatch, capsys, argv, header, body, message):
    if header == "raw":
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{not json\n")))
        header = None
    with pytest.raises(SystemExit) as exc:
        if header is None and argv[0] == "install" and message != "usage":
            skills_install.main(argv)
        else:
            _run(monkeypatch, capsys, argv, header, body)
    assert exc.value.code == 2 and message in capsys.readouterr().err
    # Nothing half-written is left behind as a copy.
    assert not (base / DIGEST).exists()
    assert not [p for p in base.glob(".tmp-*")] if base.exists() else True


def test_the_copy_is_renamed_into_place_before_it_is_locked(base, monkeypatch, capsys):
    """macOS refuses to rename a folder its owner cannot write; Linux allows it."""
    real_rename = os.rename

    def strict_rename(src, dst):
        if os.path.isdir(src) and not os.access(src, os.W_OK):
            raise PermissionError(13, "Permission denied", src)
        return real_rename(src, dst)

    monkeypatch.setattr(skills_install.os, "rename", strict_rename)
    report = _run(monkeypatch, capsys, ["install", DIGEST], [["s/SKILL.md", 2, 0o644]], b"x\n")
    assert report["present"] is True
    assert stat.S_IMODE((base / DIGEST).stat().st_mode) == 0o555
    assert stat.S_IMODE((base / DIGEST / "s").stat().st_mode) == 0o555


def test_a_copy_another_session_finished_first_is_kept(base, monkeypatch, capsys):
    header = [["s/SKILL.md", 2, 0o644]]
    _run(monkeypatch, capsys, ["install", DIGEST], header, b"x\n")
    stdin = json.dumps(header).encode() + b"\ny\n"
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    skills_install.install(DIGEST)  # the rename fails: the finished copy stands
    assert (base / DIGEST / "s" / "SKILL.md").read_bytes() == b"x\n"
    assert not list(base.glob(".tmp-*"))


def test_prune_removes_only_abandoned_partial_installs(base, monkeypatch):
    base.mkdir()
    old, fresh, done = base / ".tmp-old", base / ".tmp-new", base / DIGEST
    for path in (old, fresh, done):
        path.mkdir()
    (base / ".tmp-file").write_text("x")
    stale = time.time() - skills_install.STALE_SECONDS - 60
    os.utime(old, (stale, stale))
    os.utime(done, (stale, stale))
    skills_install.prune()
    assert not old.exists() and fresh.exists() and done.exists()
    monkeypatch.setattr(skills_install, "BASE", str(base / "missing"))
    skills_install.prune()  # no folder yet: nothing to do


def test_the_report_of_incompatible_scripts_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_install, "MAX_REPORTED", 2)
    for n in range(4):
        (tmp_path / f"b{n}.py").write_text("def (:\n")
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("def (:\n")
    found, total = skills_install.incompatible(str(tmp_path))
    # The examples are bounded; the count is not, so the total is said out loud.
    assert len(found) == 2 and total == 4


def test_safe_rel_refuses_every_escape():
    for rel in ("", "/abs", "a/../b", "a/.env", "a\\b", "a\x00b", None, "a//b"):
        assert not skills_install.safe_rel(rel)
    assert skills_install.safe_rel("a/b/SKILL.md")
