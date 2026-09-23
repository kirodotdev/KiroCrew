"""Confined decision-log append, including native Windows success and handle pins."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import make_dir_link
from kiro_crew import platform_log_append as pla
from kiro_crew.decisions import log


@pytest.fixture
def path(tmp_path):
    return tmp_path / "decisions" / "day.jsonl"


def test_regular_append_on_every_platform(path):
    pla.append_line(path, b'{"value":"first"}\n')
    pla.append_line(path, '{"value":"\u96ea"}\n'.encode())
    assert [json.loads(row)["value"] for row in path.read_bytes().splitlines()] == ["first", "雪"]


@pytest.mark.parametrize(
    "record", [b"{}", b"{}\n{}\n", b"\n{}\n"], ids=["unterminated", "two", "leading"]
)
def test_a_record_must_be_exactly_one_terminated_line(path, record):
    """The caller owns framing; a wrong frame is a bug, refused before any open."""
    with pytest.raises(ValueError, match="one newline-terminated record"):
        pla.append_line(path, record)
    assert not path.parent.exists()


def test_a_byte_ceiling_refuses_the_row_that_would_cross_it(path):
    """The ceiling is on the FILE, checked inside the lock, before any byte lands."""
    pla.append_line(path, b'{"n":1}\n', max_bytes=20)  # 8 bytes
    pla.append_line(path, b'{"n":2}\n', max_bytes=20)  # 16 bytes
    with pytest.raises(pla.LogFull):
        pla.append_line(path, b'{"n":3}\n', max_bytes=20)  # would be 24
    assert path.read_bytes() == b'{"n":1}\n{"n":2}\n'
    # Exactly at the ceiling is allowed; one over is not.
    pla.append_line(path, b"{}\n", max_bytes=19)
    assert path.read_bytes() == b'{"n":1}\n{"n":2}\n{}\n'


@pytest.mark.parametrize("location", ["parent", "leaf"])
def test_refuse_directory_links_on_every_platform(path, tmp_path, location):
    target = tmp_path / "outside"
    target.mkdir()
    path.parent.mkdir()
    link = path if location == "leaf" else path.parent
    if location == "parent":
        link.rmdir()
    make_dir_link(link, target)
    with pytest.raises(Exception):
        pla.append_line(path, b"{}\n")
    assert list(target.iterdir()) == []


def test_hardlink_refused_without_changing_target(path, tmp_path):
    target = tmp_path / "outside.jsonl"
    target.write_bytes(b'{"preserve":true}\n')
    path.parent.mkdir()
    os.link(target, path)
    with pytest.raises(OSError, match="one link"):
        pla.append_line(path, b"{}\n")
    assert target.read_bytes() == b'{"preserve":true}\n'


def test_directory_leaf_refused(path):
    path.mkdir(parents=True)
    with pytest.raises(OSError):
        pla.append_line(path, b"{}\n")
    assert list(path.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO; Windows reparse refusal tested above")
def test_fifo_does_not_block(path):
    path.parent.mkdir()
    os.mkfifo(path)
    with pytest.raises(OSError, match="regular file"):
        pla.append_line(path, b"{}\n")


def test_short_writes_and_eintr_preserve_whole_utf8_row(path, monkeypatch):
    original = os.write
    calls = 0

    def short(fd, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InterruptedError(errno.EINTR, "interrupted")
        return original(fd, data[:2])

    monkeypatch.setattr(pla.os, "write", short)
    row = '{"value":"雪"}\n'.encode()
    pla.append_line(path, row)
    assert path.read_bytes() == row
    assert calls > 2


@pytest.mark.parametrize("failure", ["zero", "error", "interrupt"])
def test_failed_partial_row_rolls_back_before_next_append(path, monkeypatch, failure):
    original = os.write
    pla.append_line(path, b'{"keep":1}\n')
    calls = 0

    def fail(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(fd, data[:3])
        if failure == "zero":
            return 0
        if failure == "interrupt":
            raise InterruptedError(errno.EINTR, "interrupted")
        raise OSError(errno.ENOSPC, "full")

    with monkeypatch.context() as patch:
        patch.setattr(pla.os, "write", fail)
        patch.setattr(pla, "_APPEND_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(OSError):
            pla.append_line(path, b'{"failed":true}\n')
    pla.append_line(path, b'{"keep":2}\n')
    assert path.read_bytes() == b'{"keep":1}\n{"keep":2}\n'


def test_a_write_that_makes_no_progress_is_refused_and_rolled_back(path, monkeypatch):
    """A zero-byte return would spin until the deadline; it is an error at once."""
    original = os.write
    pla.append_line(path, b'{"keep":1}\n')
    calls = 0

    def stall(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(fd, data[:2])
        return 0

    monkeypatch.setattr(pla.os, "write", stall)
    with pytest.raises(OSError, match="no progress"):
        pla.append_line(path, b'{"failed":true}\n')
    assert calls == 2
    assert path.read_bytes() == b'{"keep":1}\n'


def test_failed_rollback_preserves_tail_and_the_next_append_starts_a_new_line(path, monkeypatch):
    original = os.write
    pla.append_line(path, b'{"keep":1}\n')
    calls = 0

    def fail(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(fd, data[:3])
        raise OSError("write failed")

    def no_truncate(*args):
        raise OSError("truncate failed")

    with monkeypatch.context() as patch:
        patch.setattr(pla.os, "write", fail)
        patch.setattr(pla.os, "ftruncate", no_truncate)
        with pytest.raises(OSError, match="truncate failed"):
            pla.append_line(path, b'{"failed":true}\n')
    saved = path.read_bytes()
    assert saved == b'{"keep":1}\n{"f'
    pla.append_line(path, b'{"next":1}\n')
    assert path.read_bytes() == saved + b'\n{"next":1}\n'
    rows = path.read_bytes().splitlines()
    assert json.loads(rows[0]) == {"keep": 1} and json.loads(rows[2]) == {"next": 1}


def test_preexisting_tail_is_never_deleted(path):
    path.parent.mkdir()
    path.write_bytes(b'{"keep":1}\npartial')
    pla.append_line(path, b"{}\n")
    assert path.read_bytes() == b'{"keep":1}\npartial\n{}\n'


def test_concurrent_short_writers_serialize(path, monkeypatch):
    original = os.write
    barrier = threading.Barrier(4, timeout=5)

    def short(fd, data):
        return original(fd, data[:3])

    def writer(index):
        barrier.wait()
        pla.append_line(path, json.dumps({"index": index, "text": "x" * 100}).encode() + b"\n")

    monkeypatch.setattr(pla.os, "write", short)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = [executor.submit(writer, index) for index in range(4)]
        for result in results:
            result.result(timeout=5)
    assert sorted(json.loads(row)["index"] for row in path.read_bytes().splitlines()) == list(
        range(4)
    )


def test_contended_append_times_out_without_writing(path, monkeypatch):
    pla.append_line(path, b'{"keep":1}\n')
    monkeypatch.setattr(pla, "_APPEND_TIMEOUT_SECONDS", 0.05)
    with pla._open_log(path) as held:
        with pla.platform_compat.file_lock(held, exclusive=True):
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(pla.append_line, path, b"{}\n")
                with pytest.raises(OSError):
                    pending.result(timeout=5)
    assert path.read_bytes() == b'{"keep":1}\n'
    pla.append_line(path, b"{}\n")


def test_the_open_is_not_charged_to_the_append_deadline(path, monkeypatch):
    """A first append whose open outran the budget still writes its row.

    The budget spans the lock wait and the write retries, which is what the
    platform-compat contract says it spans. Start it above the open instead and a
    create-and-pin slower than the whole budget -- a loaded Windows worker, a
    scanner touching a fresh directory -- raises at the lock. The leaf is created
    BY that open, so the caller is left a day file that exists and holds nothing,
    and reading a JSONL log back then fails to parse.
    """
    original = pla._pin_and_open_leaf
    overrun = pla._APPEND_TIMEOUT_SECONDS + 0.1

    def slow(leaf, anchor, stack):
        fd = original(leaf, anchor, stack)
        time.sleep(overrun)
        return fd

    monkeypatch.setattr(pla, "_pin_and_open_leaf", slow)
    pla.append_line(path, b'{"row":1}\n')
    assert path.read_bytes() == b'{"row":1}\n'


@pytest.mark.parametrize("failure", ["write", "validate", "none"])
def test_file_and_directory_descriptors_close(path, monkeypatch, failure):
    closed = []
    original_close = os.close

    def close(fd):
        closed.append(fd)
        original_close(fd)

    def refuse(*args):
        raise OSError("injected failure")

    monkeypatch.setattr(pla.os, "close", close)
    if failure == "write":
        monkeypatch.setattr(pla.os, "write", refuse)
    elif failure == "validate":
        monkeypatch.setattr(pla, "_regular_single_link", refuse)
    if failure == "none":
        pla.append_line(path, b"{}\n")
    else:
        with pytest.raises(OSError, match="injected"):
            pla.append_line(path, b"{}\n")
    assert len(closed) >= 3
    for fd in set(closed):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_logging_boundary_survives_platform_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(log, "log_dir", lambda: tmp_path / "decisions")

    def refuse(*args):
        raise OSError("injected append failure")

    monkeypatch.setattr(log, "append_line", refuse)
    log.append({"value": 1})
    assert "could not append log row" in caplog.text
    assert not (tmp_path / "decisions").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows share-mode contract")
def test_windows_pins_prevent_ancestor_and_leaf_rename(path):
    with pla._open_log(path):
        for entry in (path.parent.parent, path.parent, path):
            with pytest.raises(OSError):
                entry.rename(entry.with_name(entry.name + "-moved"))
    # Every native handle must be closed, including all ancestor pins.
    path.rename(path.with_name("released.jsonl"))
    path.parent.rename(path.parent.with_name("released"))


@pytest.mark.skipif(os.name != "nt", reason="native Windows reparse-mutation handle contract")
def test_windows_directory_pin_denies_write_handle(path):
    import ctypes
    from ctypes import wintypes

    with pla._open_log(path):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(path.parent), 0x40000000, 7, None, 3, 0x02200000, None)
        if handle not in (None, wintypes.HANDLE(-1).value):
            kernel.CloseHandle(handle)
            pytest.fail("pinned directory accepted a reparse-mutation write handle")
        assert ctypes.get_last_error() == 32  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"\\?\C:\Users\me\decisions\day.jsonl", r"C:\Users\me\decisions\day.jsonl"),
        (r"\\?\UNC\srv\share\decisions\day.jsonl", r"\\srv\share\decisions\day.jsonl"),
        (r"C:\Users\ME\decisions\day.jsonl", r"C:\Users\me\decisions\day.jsonl"),
    ],
    ids=["device-prefix", "unc-prefix", "case"],
)
@pytest.mark.skipif(os.name != "nt", reason="Windows path normalisation")
def test_windows_final_path_normalisation(raw, expected):
    assert pla._win_normalized(raw) == pla._win_normalized(expected)


@pytest.mark.skipif(os.name != "nt", reason="native Windows final-path contract")
def test_windows_redirected_leaf_handle_is_refused(path, tmp_path, monkeypatch):
    """A leaf handle whose kernel path is not under the pinned directory is refused."""
    target = tmp_path / "outside"
    target.mkdir()
    monkeypatch.setattr(pla, "_win_final_path", lambda fd: str(target / path.name))
    with pytest.raises(OSError, match="redirected"):
        pla.append_line(path, b"{}\n")
    assert list(target.iterdir()) == []
    assert path.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="native Windows handle transfer failure")
def test_windows_crt_conversion_failure_closes_native_handle(path, monkeypatch):
    import msvcrt

    path.parent.mkdir()

    def refuse(*args):
        raise OSError("CRT conversion refused")

    monkeypatch.setattr(msvcrt, "open_osfhandle", refuse)
    with pytest.raises(OSError, match="CRT conversion"):
        pla._win_open(path, directory=False)
    path.rename(path.with_name("released.jsonl"))


def test_failed_append_does_not_truncate_unrelated_growth(path, monkeypatch):
    original = os.write
    pla.append_line(path, b'{"keep":1}\n')
    calls = 0

    def fail(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(fd, data[:3])
        original(fd, b"unrelated")
        raise OSError("uncooperative writer")

    monkeypatch.setattr(pla.os, "write", fail)
    with pytest.raises(OSError, match="uncooperative"):
        pla.append_line(path, b'{"failed":true}\n')
    assert path.read_bytes() == b'{"keep":1}\n{"funrelated'


@pytest.mark.skipif(os.name != "posix", reason="POSIX allows pinned directory renames")
def test_parent_swap_after_pin_cannot_redirect_write(path, tmp_path, monkeypatch):
    original = os.open
    target = tmp_path / "outside"
    target.mkdir()
    moved = tmp_path / "pinned"

    def swap(name, flags, mode=0o777, *, dir_fd=None):
        if name == path.name:
            path.parent.rename(moved)
            make_dir_link(path.parent, target)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", swap)
    pla.append_line(path, b"{}\n")
    assert list(target.iterdir()) == []
    assert (moved / path.name).read_bytes() == b"{}\n"


def test_link_installed_at_leaf_open_is_refused(path, tmp_path, monkeypatch):
    target = tmp_path / "outside"
    target.mkdir()
    if os.name == "posix":
        original = os.open

        def swapped_open(name, flags, mode=0o777, *, dir_fd=None):
            if name == path.name:
                make_dir_link(path, target)
            return original(name, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(pla.os, "open", swapped_open)
    else:
        original_win = pla._win_open

        def swapped_win(name, *, directory):
            if not directory:
                make_dir_link(path, target)
            return original_win(name, directory=directory)

        monkeypatch.setattr(pla, "_win_open", swapped_win)
    with pytest.raises(OSError):
        pla.append_line(path, b"{}\n")
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat leaf; the Windows leaf is by path")
def test_log_directory_removed_before_the_leaf_open_is_recreated(path, monkeypatch):
    """Creating the directory and opening the file in it are two syscalls (GH-12034).

    A removal landing between them is an interleaving no ordering can close. The
    sequence is re-run rather than reported, because the alternative is ``ENOENT``
    on the bare ``openat`` leaf -- ``'day.jsonl'``, a name with no directory
    component, which reads as a working-directory bug and is not one.
    """
    original = os.open
    removals = 0

    def removing(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal removals
        if name == path.name and removals == 0:
            removals += 1
            os.rmdir(path.parent)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", removing)
    pla.append_line(path, b'{"kept":1}\n')
    assert removals == 1  # the race really happened, so the retry is what passed
    assert path.read_bytes() == b'{"kept":1}\n'


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat leaf; the Windows leaf is by path")
def test_a_log_directory_removed_every_time_names_the_whole_path(path, monkeypatch):
    """Exhausting the attempts reports the PATH, not the leaf the syscall was given.

    The bare-leaf ``filename`` is what made this failure unreadable, so it is
    asserted as the contract rather than left to whichever ``openat`` lost.
    """
    original = os.open

    def removing(name, flags, mode=0o777, *, dir_fd=None):
        if name == path.name:
            os.rmdir(path.parent)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", removing)
    with pytest.raises(FileNotFoundError) as caught:
        pla.append_line(path, b"{}\n")
    assert caught.value.filename == str(path)
    assert str(path.parent) in str(caught.value)
    assert not path.parent.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat leaf; the Windows leaf is by path")
def test_a_losing_attempt_releases_its_descriptors(path, monkeypatch):
    """A discarded attempt closes its own pins, or it holds an inode for the process."""
    original_open, original_close = os.open, os.close
    opened: list[int] = []
    closed: list[int] = []
    removed = False

    def removing(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal removed
        if name == path.name and not removed:
            removed = True
            os.rmdir(path.parent)
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def recording_close(fd):
        closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(pla.os, "open", removing)
    monkeypatch.setattr(pla.os, "close", recording_close)
    pla.append_line(path, b"{}\n")
    assert removed
    # Opens and closes pair up exactly, across BOTH attempts: the transfer of the
    # winning attempt's pins to the caller's stack must not close them twice, and
    # the losing attempt's must not survive it.
    assert sorted(opened) == sorted(closed)


def test_an_absent_home_is_not_reported_as_a_removed_log_directory(tmp_path):
    """A home that is simply not there raises as itself, not as a removal.

    The retry's report says the log directory was removed, which is a specific
    claim about what happened. This pins that the claim is not made for the
    ordinary case of a path that never existed.
    """
    with pytest.raises(FileNotFoundError) as caught:
        pla.append_line(tmp_path / "absent" / "decisions" / "day.jsonl", b"{}\n")
    assert "was removed while this append" not in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat leaf; the Windows leaf is by path")
def test_a_retry_cannot_be_redirected_by_swapping_the_anchor(tmp_path, monkeypatch):
    """Every attempt targets the anchor resolved ONCE, before the first one.

    A retry that re-resolved the anchor would follow a link put at its name in the
    meantime and create the log directory -- and write the row -- inside whatever
    that link points at. Resolving once turns that into ``pin_parent``'s
    ``O_NOFOLLOW`` refusal, with nothing written anywhere.
    """
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = home / "decisions" / "day.jsonl"
    original = os.open
    swapped = False

    def swap_the_anchor(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if name == target.name and not swapped:
            swapped = True
            os.rmdir(target.parent)
            home.rename(tmp_path / "moved")
            make_dir_link(home, elsewhere)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", swap_the_anchor)
    with pytest.raises(Exception):
        pla.append_line(target, b"{}\n")
    assert swapped
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat leaf; the Windows leaf is by path")
def test_a_refusal_that_is_not_a_removal_is_not_re_attempted(path, tmp_path, monkeypatch):
    """Only a REMOVED directory is re-run. A link at the leaf is refused, once."""
    target = tmp_path / "outside"
    target.mkdir()
    path.parent.mkdir()
    make_dir_link(path, target)
    original = os.open
    leaf_flags = []

    def counting(name, flags, mode=0o777, *, dir_fd=None):
        if name == path.name:
            leaf_flags.append(flags)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", counting)
    with pytest.raises(OSError) as caught:
        pla.append_line(path, b"{}\n")
    assert caught.value.errno == errno.ELOOP
    assert len(leaf_flags) == 2
    assert leaf_flags[0] & os.O_EXCL
    assert not leaf_flags[1] & os.O_CREAT
    assert list(target.iterdir()) == []


def test_the_retention_sweep_still_refuses_an_absent_log_directory(path):
    """``pinned_log_dir`` is deliberately OUTSIDE the retry: it creates nothing.

    The sweep has nothing to do when the directory is not there, so re-attempting
    would only delay the same answer.
    """
    with pytest.raises(FileNotFoundError):
        with pla.pinned_log_dir(path.parent):
            pass


def test_independent_process_lock_blocks_append(path, tmp_path, monkeypatch):
    """The lock must serialize processes, not just threads in one interpreter."""
    import subprocess
    import sys
    from pathlib import Path

    pla.append_line(path, b'{"keep":1}\n')
    script = (
        "import sys; from pathlib import Path; "
        "from kiro_crew import platform_log_append as p; "
        "p._APPEND_TIMEOUT_SECONDS = 0.05; "
        "p.append_line(Path(sys.argv[1]), b'{}\\n')"
    )
    # The child must import the same checkout the test runs against.
    env = dict(os.environ)
    src = str(Path(pla.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
    with pla._open_log(path) as held:
        with pla.platform_compat.file_lock(held, exclusive=True):
            result = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                env=env,
            )
    assert result.returncode != 0
    assert "file lock" in result.stderr
    assert path.read_bytes() == b'{"keep":1}\n'


@pytest.mark.skipif(os.name != "posix", reason="POSIX openat creation contract")
def test_first_create_avoids_nonexclusive_create_race(path, monkeypatch):
    """Model Darwin's losing O_CREAT open without depending on thread scheduling."""
    original = os.open
    leaf_opens = []

    def open_leaf(name, flags, mode=0o777, *, dir_fd=None):
        if name == path.name:
            leaf_opens.append(flags)
            if flags & os.O_CREAT and not flags & os.O_EXCL:
                raise FileNotFoundError(errno.ENOENT, "concurrent first create", name)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", open_leaf)
    pla.append_line(path, b'{"first":1}\n')
    pla.append_line(path, b'{"second":2}\n')
    assert path.read_bytes() == b'{"first":1}\n{"second":2}\n'
    assert leaf_opens


@pytest.mark.skipif(os.name != "posix", reason="POSIX create/open handoff")
@pytest.mark.parametrize("entry", ["file", "symlink", "hardlink", "missing"])
def test_create_loser_opens_the_winner_once_and_redoes_only_a_vanished_leaf(
    path, tmp_path, monkeypatch, entry
):
    """A competing creator's entry is still untrusted: a link is refused, never followed.

    Only its absence -- the winner's leaf gone again before the open -- is retried,
    and then through the bounded redo's own exclusive, pinned create.
    """
    original = os.open
    target = tmp_path / "outside.jsonl"
    saved = b'{"keep":1}\n'
    target.write_bytes(saved)
    leaf_opens = []

    def compete(name, flags, mode=0o777, *, dir_fd=None):
        if name == path.name:
            leaf_opens.append((flags, dir_fd))
            if len(leaf_opens) == 1:
                if entry == "file":
                    path.write_bytes(saved)
                elif entry == "symlink":
                    path.symlink_to(target)
                elif entry == "hardlink":
                    os.link(target, path)
                # The missing case models removal after the competing create.
                raise FileExistsError(errno.EEXIST, "another creator won", name)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", compete)
    if entry in ("file", "missing"):
        pla.append_line(path, b'{"next":2}\n')
        expected = saved + b'{"next":2}\n' if entry == "file" else b'{"next":2}\n'
        assert path.read_bytes() == expected
    else:
        with pytest.raises(OSError):
            pla.append_line(path, b'{"next":2}\n')
    assert target.read_bytes() == saved
    assert not leaf_opens[1][0] & (os.O_CREAT | os.O_EXCL)
    assert leaf_opens[0][1] == leaf_opens[1][1] is not None
    if entry == "missing":
        # The lost handoff is redone once: a fresh exclusive create under a
        # fresh pin, never a plain O_CREAT that could follow a planted link.
        assert len(leaf_opens) == 3
        assert leaf_opens[2][0] & os.O_CREAT and leaf_opens[2][0] & os.O_EXCL
        assert leaf_opens[2][0] & os.O_NOFOLLOW
        assert leaf_opens[2][1] is not None
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        assert len(leaf_opens) == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX pinned-directory handoff")
def test_create_loser_keeps_the_original_directory_pin(path, tmp_path, monkeypatch):
    original = os.open
    target = tmp_path / "outside"
    target.mkdir()
    moved = tmp_path / "pinned"
    leaf_opens = 0

    def swap(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal leaf_opens
        if name == path.name:
            leaf_opens += 1
            if leaf_opens == 1:
                path.write_bytes(b'{"keep":1}\n')
                path.parent.rename(moved)
                make_dir_link(path.parent, target)
                raise FileExistsError(errno.EEXIST, "another creator won", name)
        return original(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pla.os, "open", swap)
    pla.append_line(path, b'{"next":2}\n')
    assert leaf_opens == 2
    assert list(target.iterdir()) == []
    assert (moved / path.name).read_bytes() == b'{"keep":1}\n{"next":2}\n'


@pytest.mark.parametrize("round_number", range(3))
def test_native_concurrent_first_appends_keep_every_record(tmp_path, round_number):
    """Fresh directory and file, real opens and writes; no precreated log or mocks."""
    path = tmp_path / f"round-{round_number}" / "day.jsonl"
    barrier = threading.Barrier(4, timeout=5)

    def writer(index):
        barrier.wait()
        pla.append_line(path, json.dumps({"index": index}).encode() + b"\n")

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(writer, index) for index in range(4)]
        for future in futures:
            future.result(timeout=5)
    assert sorted(json.loads(row)["index"] for row in path.read_bytes().splitlines()) == list(
        range(4)
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX existing-leaf handoff")
@pytest.mark.parametrize("remove_directory", [False, True])
def test_existing_log_removed_during_handoff_is_recreated_exclusively(
    path, monkeypatch, remove_directory
):
    """A real existing leaf deleted between EEXIST and the open still gets this row.

    The bytes another actor deleted are gone by their hand, not ours; the record
    being appended is the one thing this append can still keep. It lands through
    the same exclusive, pinned, no-follow create as a first append, never a plain
    ``O_CREAT`` retry, and the losing attempt's descriptors are released.
    """
    pla.append_line(path, b'{"keep":1}\n')
    original_open, original_close = os.open, os.close
    opened, closed, leaf_flags = [], [], []

    def removing(name, flags, mode=0o777, *, dir_fd=None):
        if name == path.name:
            leaf_flags.append(flags)
            if not flags & os.O_CREAT:
                path.unlink()
                if remove_directory:
                    path.parent.rmdir()
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def close(fd):
        closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(pla.os, "open", removing)
    monkeypatch.setattr(pla.os, "close", close)
    pla.append_line(path, b'{"next":2}\n')
    assert path.read_bytes() == b'{"next":2}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert leaf_flags[0] & os.O_EXCL
    assert not leaf_flags[1] & (os.O_CREAT | os.O_EXCL)
    assert leaf_flags[2] & os.O_CREAT and leaf_flags[2] & os.O_EXCL
    assert all(flags & os.O_NOFOLLOW for flags in leaf_flags)
    assert len(leaf_flags) == 3
    assert opened
    assert sorted(opened) == sorted(closed)


class TestASharingViolationOnTheOpenIsRetried:
    """Windows reports contention on the OPEN, so the open carries a budget of its own.

    ERROR_SHARING_VIOLATION means another handle is held with narrower sharing than
    the access asked for. On this path that is somebody else's transient handle -- a
    scanner, an indexer, a backup agent -- because our own opens share read and
    write. The writer already retried short writes; the open is the step where
    Windows contention actually shows up, and a verdict route that answers 503 makes
    a one-shot open the difference between recording an owner's thumbs-up and telling
    them it was not recorded.

    That budget is ``_APPEND_TIMEOUT_SECONDS`` long and is charged separately from
    the one the lock and the writes spend, which is why a slow open cannot arrive at
    the lock with nothing left (``test_the_open_is_not_charged_to_the_append_deadline``
    pins the other half of that split).

    Branching is on ``exc.winerror``, so these run on every platform; the Windows
    lane is what proves the real ``CreateFileW`` raises it.
    """

    def _violation(self):
        exc = OSError("in use by another process")
        exc.winerror = pla._WIN_ERROR_SHARING_VIOLATION
        return exc

    def test_a_transient_violation_is_retried_and_the_row_lands(self, tmp_path, monkeypatch):
        path = tmp_path / "decisions-20260919.jsonl"
        real = pla._open_log
        attempts = {"n": 0}

        def _flaky(target):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise self._violation()
            return real(target)

        monkeypatch.setattr(pla, "_open_log", _flaky)
        monkeypatch.setattr(pla, "_SHARING_RETRY_SECONDS", 0)

        pla.append_line(path, b'{"ts": "x"}\n')

        assert attempts["n"] == 2, "the first open was retried, not reported"
        assert path.read_text(encoding="utf-8") == '{"ts": "x"}\n'

    def test_a_holder_that_outlasts_the_open_budget_still_raises(self, tmp_path, monkeypatch):
        path = tmp_path / "decisions-20260919.jsonl"
        violation = self._violation()

        def _always(_target):
            raise violation

        monkeypatch.setattr(pla, "_open_log", _always)
        monkeypatch.setattr(pla, "_SHARING_RETRY_SECONDS", 0)
        monkeypatch.setattr(pla, "_APPEND_TIMEOUT_SECONDS", 0.02)

        with pytest.raises(OSError) as caught:
            pla.append_line(path, b'{"ts": "x"}\n')

        assert caught.value is violation, "the original error, not a timeout about it"

    def test_any_other_open_error_is_not_retried(self, tmp_path, monkeypatch):
        path = tmp_path / "decisions-20260919.jsonl"
        attempts = {"n": 0}

        def _denied(_target):
            attempts["n"] += 1
            raise PermissionError("chmod-ed")

        monkeypatch.setattr(pla, "_open_log", _denied)

        with pytest.raises(PermissionError):
            pla.append_line(path, b'{"ts": "x"}\n')

        assert attempts["n"] == 1, "only a sharing violation is contention"

    def test_the_retry_cannot_replay_a_partial_write(self, tmp_path, monkeypatch):
        """The retry covers the open alone, so a body failure is never repeated."""
        path = tmp_path / "decisions-20260919.jsonl"
        opens = {"n": 0}
        real = pla._open_log

        def _counting(target):
            opens["n"] += 1
            return real(target)

        def _boom(*_args, **_kwargs):
            raise OSError("write refused")

        monkeypatch.setattr(pla, "_open_log", _counting)
        monkeypatch.setattr(pla.os, "write", _boom)

        with pytest.raises(OSError):
            pla.append_line(path, b'{"ts": "x"}\n')

        assert opens["n"] == 1, "a write failure must not reopen and try again"
