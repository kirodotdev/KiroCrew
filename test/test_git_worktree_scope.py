"""Tests for :mod:`kiro_crew.git_worktree_scope`.

The shared classification all four filter-driver guards consult AFTER a
``--worktree`` config probe fails — never to gate whether the probe runs.
Only a genuinely ABSENT ``config.worktree`` reads as the empty scope git
creates the file lazily for; every present entry — regular or not — and every
other stat outcome fails closed, keeping the caller's refusal.
"""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew.git_worktree_scope import worktree_probe_failure_is_empty_scope


def _gitdir(tmp_path):
    d = tmp_path / "repo" / ".git"
    d.mkdir(parents=True)
    return d


def test_absent_file_is_the_empty_scope(tmp_path):
    d = _gitdir(tmp_path)
    assert worktree_probe_failure_is_empty_scope(str(d), str(tmp_path)) is True


@pytest.mark.skipif(
    os.name == "nt" or sys.platform == "darwin",
    reason="non-UTF-8 bytes are not legal NTFS or APFS/HFS+ name units",
)
def test_surrogate_decoded_non_utf8_gitdir_finds_the_existing_config(tmp_path):
    """A gitdir whose path holds a byte that is not valid UTF-8 must still
    fail closed when ``config.worktree`` EXISTS.

    git prints the path byte-for-byte. Decoded with ``surrogateescape``
    (the callers' contract), ``os.fsencode`` inside the ``lstat`` restores
    the original bytes and the file is found. Decoded with ``"replace"``,
    the byte becomes U+FFFD, the ``lstat`` inspects a path that names
    nothing, and the guard clears a scope git still reads -- the bypass this
    pin exists to keep closed.
    """
    gitdir_bytes = os.fsencode(str(tmp_path)) + b"/git-\xff"
    os.mkdir(gitdir_bytes)
    with open(gitdir_bytes + b"/config.worktree", "wb"):
        pass
    surrogate = gitdir_bytes.decode("utf-8", "surrogateescape") + "\n"
    assert worktree_probe_failure_is_empty_scope(surrogate, str(tmp_path)) is False
    # The defect class, pinned as a contrast: the display decode does not
    # round-trip, so the same on-disk state reads as the empty scope.
    replaced = gitdir_bytes.decode("utf-8", "replace") + "\n"
    assert worktree_probe_failure_is_empty_scope(replaced, str(tmp_path)) is True


def test_trailing_newline_is_gits_terminator_not_the_path(tmp_path):
    """Raw ``rev-parse`` stdout ends in one newline; the classifier removes
    exactly that terminator and inspects the real path."""
    d = _gitdir(tmp_path)
    (d / "config.worktree").write_text("garbage [[[ not config\n")
    assert worktree_probe_failure_is_empty_scope(f"{d}\n", str(tmp_path)) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX path semantics")
def test_cr_before_the_newline_is_path_content_on_posix(tmp_path):
    """On POSIX git terminates with a bare LF, so a CR ahead of it belongs to
    the real directory name; removing it would lstat a different path and
    wrongly clear a scope whose config file exists."""
    d = tmp_path / "repo" / ".git\r"
    d.mkdir(parents=True)
    (d / "config.worktree").write_text("garbage [[[ not config\n")
    assert worktree_probe_failure_is_empty_scope(f"{d}\n", str(tmp_path)) is False


def test_windows_crlf_terminator_is_removed(tmp_path, monkeypatch):
    """On Windows a text-mode pipe delivers git's terminator as CRLF; the
    classifier removes exactly that pair and inspects the real path. The
    branch reads ``platform_compat.IS_POSIX`` at call time, so this exercises
    the Windows terminator handling on every OS."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    d = _gitdir(tmp_path)
    (d / "config.worktree").write_text("garbage [[[ not config\n")
    assert worktree_probe_failure_is_empty_scope(f"{d}\r\n", str(tmp_path)) is False


def test_windows_crlf_absent_file_is_the_empty_scope(tmp_path, monkeypatch):
    """The CRLF trim must land on the REAL path: with the file absent the
    classifier reports the empty scope, proving the CR did not survive into
    the lstat'ed path (a leftover CR would miss the directory and still
    return True only by accident of the same FileNotFoundError — so this
    pins the pair with :func:`test_windows_crlf_terminator_is_removed`,
    where a surviving CR would flip the present-file verdict)."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    d = _gitdir(tmp_path)
    assert worktree_probe_failure_is_empty_scope(f"{d}\r\n", str(tmp_path)) is True


@pytest.mark.skipif(os.name == "nt", reason="trailing-space dirs are POSIX-only")
def test_whitespace_bearing_git_dir_is_not_rewritten(tmp_path):
    """A git dir whose real name ends in a space must be lstat'ed AS IS: a
    ``.strip()`` would inspect a different, nonexistent path and clear a
    scope whose config file exists."""
    d = tmp_path / "repo" / ".git "
    d.mkdir(parents=True)
    (d / "config.worktree").write_text("garbage [[[ not config\n")
    assert worktree_probe_failure_is_empty_scope(f"{d}\n", str(tmp_path)) is False


def test_present_file_keeps_the_refusal(tmp_path):
    """A probe that failed while the file EXISTS is a garbled/unreadable scope,
    never the empty one — the guard must keep refusing."""
    d = _gitdir(tmp_path)
    (d / "config.worktree").write_text("garbage [[[ not config\n")
    assert worktree_probe_failure_is_empty_scope(str(d), str(tmp_path)) is False


def test_relative_gitdir_joins_onto_base(tmp_path):
    d = _gitdir(tmp_path)
    (d / "config.worktree").write_text("")
    rel = os.path.join("repo", ".git")
    assert worktree_probe_failure_is_empty_scope(rel, str(tmp_path)) is False


def test_empty_gitdir_fails_closed(tmp_path):
    """An unlocatable git dir cannot confirm absence, so the failure is not
    classified as the empty scope and the caller's refusal stands."""
    assert worktree_probe_failure_is_empty_scope("", str(tmp_path)) is False


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is POSIX-only")
def test_fifo_keeps_the_refusal(tmp_path):
    """A present-but-non-regular entry must NOT read as the empty scope: git
    still loads the path, so clearing the failure would skip the refusal on
    exactly the entry an evader would plant. ``os.path.isfile`` answers False
    for a FIFO; the lstat-based check fails closed."""
    d = _gitdir(tmp_path)
    os.mkfifo(d / "config.worktree")
    assert worktree_probe_failure_is_empty_scope(str(d), str(tmp_path)) is False


def test_broken_symlink_keeps_the_refusal(tmp_path):
    d = _gitdir(tmp_path)
    try:
        (d / "config.worktree").symlink_to(d / "nowhere")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert worktree_probe_failure_is_empty_scope(str(d), str(tmp_path)) is False
