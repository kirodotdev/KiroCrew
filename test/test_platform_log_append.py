"""Confined decision-log append, including native Windows success and handle pins."""

from __future__ import annotations

import errno
import json
import os
import threading
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
