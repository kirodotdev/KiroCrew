"""``render_untracked``: the gateway's own read of an untracked file in a code
channel's worktree, outside any sandbox, confined to that worktree.

Split from ``test_slack_code_channel_workspace.py`` (the file-line cap)."""

from __future__ import annotations

import os

import pytest

from kiro_crew import pinned_fs
from kiro_crew.slack.code_channels.workspace import diff as diff_mod
from kiro_crew.slack.code_channels.workspace.diff import OMITTED, render_untracked

#: Whether this platform renders an untracked file's content (see the parent module).
UNTRACKED_RENDERED = pinned_fs.supports_pinned_walk()


def test_render_untracked_refuses_unsafe_paths(tmp_path):
    (tmp_path / "ok.txt").write_text("a\nb\n", newline="\n")
    shown = render_untracked(str(tmp_path), "ok.txt", budget=10_000)
    if UNTRACKED_RENDERED:
        assert shown.endswith(b"@@ -0,0 +1,2 @@\n+a\n+b\n")
    else:
        assert shown == OMITTED
    assert render_untracked(str(tmp_path), "../ok.txt", budget=10_000) == OMITTED
    assert render_untracked(str(tmp_path), "bad\nname", budget=10_000) == OMITTED
    assert render_untracked(str(tmp_path), "ok.txt", budget=10) == OMITTED


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no dir_fd support on this platform"
)
def test_render_untracked_never_reads_through_a_symlinked_worktree_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE\n", newline="\n")
    os.symlink(outside, tmp_path / "tree")
    assert render_untracked(str(tmp_path / "tree"), "id_rsa", budget=10_000) is None


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no dir_fd support on this platform"
)
def test_render_untracked_never_reads_a_hard_linked_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET\n", newline="\n")
    root = tmp_path / "tree"
    root.mkdir()
    os.link(secret, root / "notes.txt")
    assert render_untracked(str(root), "notes.txt", budget=10_000) == OMITTED


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no dir_fd support on this platform"
)
def test_render_untracked_closes_every_descriptor_it_opens(tmp_path):
    """Links, FIFOs and oversized files return early: none may leak a dir fd."""
    root = tmp_path / "tree"
    (root / "d").mkdir(parents=True)
    os.symlink("/etc/hostname", root / "d" / "link")
    (root / "d" / "big.txt").write_text("x" * 100, newline="\n")
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "d" / "pipe")
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_dir))
    for _ in range(50):
        for rel in ("d/link", "d/big.txt", "d/pipe", "d/missing"):
            render_untracked(str(root), rel, budget=10)
    assert len(os.listdir(fd_dir)) == before


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no dir_fd support on this platform"
)
def test_untracked_reads_walk_through_the_shared_pinned_walk(tmp_path, monkeypatch):
    """The gateway's read outside the sandbox is confined by ONE mechanism:
    :mod:`kiro_crew.pinned_fs`, never a private copy of its directory walk."""
    root = tmp_path / "tree"
    (root / "d").mkdir(parents=True)
    (root / "d" / "a.txt").write_text("hi\n", newline="\n")
    walked: list[tuple[str, tuple[str, ...]]] = []
    real = pinned_fs.open_pinned_descendant_dir

    def spy(base, parts, **kw):
        walked.append((str(base), tuple(parts)))
        return real(base, parts, **kw)

    monkeypatch.setattr(pinned_fs, "open_pinned_descendant_dir", spy)
    assert b"+hi" in (render_untracked(str(root), "d/a.txt", budget=10_000) or b"")
    assert walked == [(str(root), ("d",))]
    assert not hasattr(diff_mod, "_walk_beneath")


@pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no dir_fd support on this platform"
)
def test_render_untracked_never_reads_through_a_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("SLACK_BOT_TOKEN=xoxb-REAL\n", newline="\n")
    root = tmp_path / "tree"
    root.mkdir()
    os.symlink(outside, root / "d")
    assert render_untracked(str(root), "d/.env", budget=10_000) is None


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not pinned_fs.supports_pinned_walk(), reason="POSIX only"
)
def test_render_untracked_refuses_a_fifo_swapped_in_after_the_lstat(tmp_path, monkeypatch):
    """The open is non-blocking and the fd must be the file the lstat saw."""
    (tmp_path / "decoy.txt").write_text("x\n", newline="\n")
    os.mkfifo(tmp_path / "pipe")
    real_stat = os.stat

    def swapped(path, *a, **kw):
        if path == "pipe":  # the leaf lstat sees a regular file...
            return real_stat(tmp_path / "decoy.txt")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(diff_mod.os, "stat", swapped)
    # ...but the open lands on the FIFO: must neither block nor render.
    assert render_untracked(str(tmp_path), "pipe", budget=10_000) is None
