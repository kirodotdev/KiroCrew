"""``pinned_fs.write_new_file_pinned``: the one pinned create-if-absent transaction.

The steering create, the skill create and the prompt create's by-name fallback all
call this. Each caller's own tests still pin its error taxonomy; these pin the
sequence itself, once, so the three callers cannot drift on it:

* ``O_EXCL`` refuses an occupied name and leaves the resident untouched;
* a symlink at the name is refused, never written through;
* the write lands relative to the held descriptor, whatever the name now says;
* a failed write, a failed close and a failed directory flush each roll the leaf
  back, verified against the inode this call created, so a rival's replacement
  at the name survives;
* a transient identity probe failure is re-asked through the descriptor;
* with no identity at all the name stays as an EMPTY file and nothing is unlinked.
"""

from __future__ import annotations

import errno
import os
import stat

import pytest

from kiro_crew import pinned_fs

pytestmark = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="needs descriptor-relative opens"
)


@pytest.fixture
def held(tmp_path):
    fd = os.open(str(tmp_path), pinned_fs.dir_flags())
    try:
        yield fd
    finally:
        os.close(fd)


def test_it_creates_the_file_with_the_bytes_and_mode_given(tmp_path, held):
    pinned_fs.write_new_file_pinned(held, "doc.md", b"line one\nline two\n", mode=0o600)
    target = tmp_path / "doc.md"
    assert target.read_bytes() == b"line one\nline two\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_nlink == 1


def test_an_empty_body_still_creates_the_file(tmp_path, held):
    pinned_fs.write_new_file_pinned(held, "empty.md", b"", mode=0o600)
    assert (tmp_path / "empty.md").read_bytes() == b""


def test_an_occupied_name_raises_file_exists_and_keeps_the_resident(tmp_path, held):
    (tmp_path / "doc.md").write_text("RESIDENT", encoding="utf-8")
    with pytest.raises(FileExistsError):
        pinned_fs.write_new_file_pinned(held, "doc.md", b"INTRUDER", mode=0o600)
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "RESIDENT"


def test_a_link_at_the_name_is_refused_and_its_target_untouched(tmp_path, held):
    victim = tmp_path / "victim"
    victim.write_text("SAFE", encoding="utf-8")
    (tmp_path / "doc.md").symlink_to(victim)
    with pytest.raises(OSError):
        pinned_fs.write_new_file_pinned(held, "doc.md", b"THROUGH THE LINK", mode=0o600)
    assert victim.read_text(encoding="utf-8") == "SAFE"


def test_the_write_follows_the_descriptor_not_the_name(tmp_path):
    """A directory swapped for a link after the pin cannot redirect the create."""
    pinned_dir = tmp_path / "pinned"
    pinned_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fd = os.open(str(pinned_dir), pinned_fs.dir_flags())
    try:
        pinned_dir.rename(tmp_path / "moved")
        pinned_dir.symlink_to(elsewhere, target_is_directory=True)
        pinned_fs.write_new_file_pinned(fd, "doc.md", b"BODY", mode=0o600)
    finally:
        os.close(fd)
    assert list(elsewhere.iterdir()) == []
    assert (tmp_path / "moved" / "doc.md").read_bytes() == b"BODY"


def test_a_failed_write_rolls_the_leaf_back_so_a_retry_creates(tmp_path, held, monkeypatch):
    real_write = os.write

    def fail(fd, data):
        if b"MINE" in bytes(data):
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_write(fd, data)

    with monkeypatch.context() as mp:
        mp.setattr(os, "write", fail)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"MINE", mode=0o600)
    assert not (tmp_path / "doc.md").exists(), "the partial body survived a failed write"
    pinned_fs.write_new_file_pinned(held, "doc.md", b"second try", mode=0o600)
    assert (tmp_path / "doc.md").read_bytes() == b"second try"


def test_the_rollback_spares_a_rivals_replacement(tmp_path, held, monkeypatch):
    """The rollback removes the inode it created, not whatever answers to the name."""
    target = tmp_path / "doc.md"
    real_write = os.write

    def swap_then_fail(fd, data):
        if b"MINE" in bytes(data):
            target.unlink()
            target.write_text("RIVAL", encoding="utf-8")
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_write(fd, data)

    with monkeypatch.context() as mp:
        mp.setattr(os, "write", swap_then_fail)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"MINE", mode=0o600)
    assert target.read_text(encoding="utf-8") == "RIVAL", "the rollback ate the rival's file"


def test_a_failed_close_runs_the_rollback(tmp_path, held, monkeypatch):
    """``close`` is where a deferred write error surfaces, so it is inside the guard."""
    real_open = os.open
    real_close = os.close
    leaf_fds: list[int] = []

    def remember(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if args and args[0] == "doc.md":
            leaf_fds.append(fd)
        return fd

    def fail_leaf_close(fd):
        if fd in leaf_fds:
            real_close(fd)
            raise OSError(errno.EIO, "deferred write error")
        return real_close(fd)

    with monkeypatch.context() as mp:
        mp.setattr(os, "open", remember)
        mp.setattr(os, "close", fail_leaf_close)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600)
    assert leaf_fds, "the leaf open was never exercised"
    assert not (tmp_path / "doc.md").exists(), "the body survived a failed close"


def test_fsync_flushes_the_body_then_the_entry(tmp_path, held, monkeypatch):
    order: list[str] = []
    real_fsync = os.fsync

    def note(fd):
        order.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    with monkeypatch.context() as mp:
        mp.setattr(os, "fsync", note)
        pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600, fsync=True)
    assert order == ["file", "dir"]


def test_without_fsync_nothing_is_flushed(tmp_path, held, monkeypatch):
    calls: list[int] = []
    with monkeypatch.context() as mp:
        mp.setattr(os, "fsync", lambda fd: calls.append(fd))
        pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600)
    assert calls == []


def test_a_failed_directory_flush_withdraws_the_file(tmp_path, held, monkeypatch):
    """The entry's flush is inside the guard: a caller promising durability gets a clean retry."""
    real_fsync = os.fsync

    def dir_flush_fails(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    with monkeypatch.context() as mp:
        mp.setattr(os, "fsync", dir_flush_fails)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600, fsync=True)
    assert not (tmp_path / "doc.md").exists()


def test_a_transient_identity_probe_failure_is_re_asked_through_the_descriptor(
    tmp_path, held, monkeypatch
):
    real_fstat = os.fstat
    failed_once: list[int] = []

    def fail_the_leaf_probe_once(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode) and not failed_once:
            failed_once.append(fd)
            raise OSError(errno.EIO, "identity probe failed")
        return info

    with monkeypatch.context() as mp:
        mp.setattr(os, "fstat", fail_the_leaf_probe_once)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600)
    assert failed_once, "the identity probe was never exercised"
    assert not (tmp_path / "doc.md").exists(), "the empty name survived a recovered probe"


def test_no_identity_at_all_leaves_an_empty_file_and_never_unlinks(tmp_path, held, monkeypatch):
    """With both probes failed the name stays, EMPTY, because nothing was written yet."""
    real_fstat = os.fstat
    real_unlink = os.unlink
    unlinked: list[str] = []

    def always_fail_the_leaf_probe(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            raise OSError(errno.EIO, "identity probe failed")
        return info

    def record_unlink(path, *args, **kwargs):
        unlinked.append(str(path))
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(os, "fstat", always_fail_the_leaf_probe)
        mp.setattr(os, "unlink", record_unlink)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600)
    assert unlinked == [], f"a name with no identity was unlinked: {unlinked}"
    assert (tmp_path / "doc.md").read_bytes() == b""


def test_no_identity_at_all_spares_a_rivals_replacement(tmp_path, held, monkeypatch):
    target = tmp_path / "doc.md"
    real_fstat = os.fstat

    def swap_the_leaf_then_fail_the_probe(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            target.unlink()
            target.write_text("RIVAL", encoding="utf-8")
            raise OSError(errno.EIO, "identity probe failed")
        return info

    with monkeypatch.context() as mp:
        mp.setattr(os, "fstat", swap_the_leaf_then_fail_the_probe)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600)
    assert target.read_text(encoding="utf-8") == "RIVAL", "the rollback ate the rival's file"


def test_a_failure_leaks_no_descriptor(tmp_path, held, monkeypatch):
    real_close = os.close
    real_open = os.open
    opened: list[int] = []
    closed: list[int] = []

    def remember(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if args and args[0] == "doc.md":
            opened.append(fd)
        return fd

    def record_close(fd):
        if fd in opened:
            closed.append(fd)
        return real_close(fd)

    def fail(fd, data):
        raise OSError(errno.EIO, "write failed")

    with monkeypatch.context() as mp:
        mp.setattr(os, "open", remember)
        mp.setattr(os, "close", record_close)
        mp.setattr(os, "write", fail)
        with pytest.raises(OSError):
            pinned_fs.write_new_file_pinned(held, "doc.md", b"BODY", mode=0o600)
    assert opened and closed == opened
