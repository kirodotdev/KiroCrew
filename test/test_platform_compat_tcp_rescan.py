"""A ``/proc/net/tcp`` pass that misses a live row is read again.

The kernel serves the table a page per read and resumes by position inside a hash
bucket, so a socket closing between two reads can make one pass skip a row that
stayed open. Each fake table here lacks the wanted row on its first read only.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from kiro_crew import platform_compat
from kiro_crew import platform_compat_ports as ports

#: How many passes each lookup reads before it answers a miss.
_SCAN_PASSES = 3

_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

pytestmark = pytest.mark.skipif(
    sys.byteorder != "little", reason="the fake rows spell little-endian address words"
)


class _PagedTable:
    """A table file whose reads return *bodies* in turn, the last one repeated."""

    def __init__(self, bodies: list[str], reads: list[str], name: str) -> None:
        self._bodies = bodies
        self._reads = reads
        self._name = name

    def read_text(self, encoding: str = "ascii", errors: str = "strict") -> str:
        self._reads.append(self._name)
        index = min(self._reads.count(self._name) - 1, len(self._bodies) - 1)
        return self._bodies[index]


def test_a_peer_pid_lookup_reads_the_table_again_when_a_pass_misses_the_row(tmp_path, monkeypatch):
    fake_proc = tmp_path / "proc"
    (fake_proc / "4242" / "fd").mkdir(parents=True)
    (fake_proc / "4242" / "fd" / "3").touch()
    row = (
        "   0: 0100007F:9C40 0100007F:1E61 01 00000000:00000000 "
        "00:00000000 00000000  1000        0 555 1 0000000000000000 20 4 30 10 -1\n"
    )
    reads: list[str] = []
    tables = {
        "tcp": _PagedTable([_HEADER, _HEADER + row], reads, "tcp"),
        "tcp6": _PagedTable([_HEADER], reads, "tcp6"),
    }

    def fake_path(raw: str):
        relative = pathlib.PurePosixPath(raw).relative_to("/proc")
        if relative.parts[:1] == ("net",):
            return tables[relative.name]
        return fake_proc / relative

    monkeypatch.setattr(platform_compat, "Path", fake_path)
    descriptor = fake_proc / "4242" / "fd" / "3"
    monkeypatch.setattr(
        ports.os,
        "readlink",
        lambda path: "socket:[555]" if pathlib.Path(path) == descriptor else "pipe:[1]",
    )

    assert ports._linux_tcp_peer_pid(("127.0.0.1", 7777), ("127.0.0.1", 40000)) == 4242
    assert reads == ["tcp", "tcp6", "tcp", "tcp6"]


def test_a_peer_pid_lookup_stops_after_its_passes_when_the_row_never_shows(tmp_path, monkeypatch):
    reads: list[str] = []
    tables = {
        "tcp": _PagedTable([_HEADER], reads, "tcp"),
        "tcp6": _PagedTable([_HEADER], reads, "tcp6"),
    }
    monkeypatch.setattr(
        platform_compat, "Path", lambda raw: tables[pathlib.PurePosixPath(raw).name]
    )

    assert ports._linux_tcp_peer_pid(("127.0.0.1", 7777), ("127.0.0.1", 40000)) is None
    assert reads.count("tcp") == _SCAN_PASSES


def _listener_proc(tmp_path) -> pathlib.Path:
    proc_root = tmp_path / "proc"
    (proc_root / "net").mkdir(parents=True)
    (proc_root / "4242" / "fd").mkdir(parents=True)
    (proc_root / "4242" / "fd" / "7").touch()
    (proc_root / "net" / "tcp").write_text(_HEADER, encoding="ascii")
    (proc_root / "net" / "tcp6").write_text(_HEADER, encoding="ascii")
    return proc_root


@pytest.mark.parametrize("other_listener", [False, True])
def test_the_listener_fallback_reads_the_table_again_when_a_pass_misses_the_row(
    tmp_path, monkeypatch, other_listener
):
    proc_root = _listener_proc(tmp_path)
    tcp = proc_root / "net" / "tcp"
    other = (
        "   1: 0100007F:1E61 00000000:0000 0A 00000000:00000000 "
        "00:00000000 00000000  1000        0 999 1\n"
    )
    first = _HEADER + (other if other_listener else "")
    later = first + (
        "   0: 0100007F:1E61 00000000:0000 0A 00000000:00000000 "
        "00:00000000 00000000  1000        0 12345 1\n"
    )
    real_read_text = pathlib.Path.read_text
    reads: list[int] = []

    def paged_read_text(self, *args, **kwargs):
        if self == tcp:
            reads.append(1)
            return first if len(reads) == 1 else later
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", paged_read_text)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(ports.os, "readlink", lambda path: "socket:[12345]")

    assert ports.process_owns_loopback_listener(4242, 7777, proc_root=proc_root) is True
    assert len(reads) == 2


def test_the_listener_fallback_answers_false_after_its_passes(tmp_path, monkeypatch):
    proc_root = _listener_proc(tmp_path)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    calls: list[int] = []
    real = ports._linux_loopback_listener_inodes

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(ports, "_linux_loopback_listener_inodes", counted)

    assert ports.process_owns_loopback_listener(4242, 7777, proc_root=proc_root) is False
    assert len(calls) == _SCAN_PASSES
