"""Unit tests for the liveness oracle's process-tree readers (``acp/liveness_proc.py``).

The ``/proc`` helpers run against a fake tree under ``tmp_path`` and the darwin backend
against stubbed ``platform_compat`` probes, so every path runs on any host. Each reader
answers None, empty or False on a source that is missing or malformed, and never raises:
the oracle reads that as "no evidence", never as a negative.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.acp import liveness_proc as proc

_NET_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stat_line(*, state: str = "S", utime: int = 3, stime: int = 4, starttime: int = 900) -> str:
    fields = ["0"] * 50
    fields[0] = state
    fields[11] = str(utime)
    fields[12] = str(stime)
    fields[19] = str(starttime)
    return "42 (a (weird) comm) " + " ".join(fields) + "\n"


def _net_row(remote: str, state: str, inode: str) -> str:
    return f"   0: 0100007F:1F90 {remote} {state} 0:0 00:0 0 1000 0 {inode} 1 0 20 4 30 10 -1\n"


class TestReadPidStat:
    def test_the_fields_are_read_after_the_last_paren(self, tmp_path: Path) -> None:
        _write(tmp_path, "42/stat", _stat_line())
        assert proc.read_pid_stat(str(tmp_path), 42) == ("S", 900.0, 7)

    @pytest.mark.parametrize(
        "raw",
        [None, "", "42 no paren at all", "42 (comm) S 1 2", "42 (comm) " + " ".join(["x"] * 30)],
    )
    def test_a_gone_or_malformed_stat_reads_as_none(self, tmp_path: Path, raw: str | None) -> None:
        if raw is not None:
            _write(tmp_path, "42/stat", raw)
        assert proc.read_pid_stat(str(tmp_path), 42) is None


class TestChildLists:
    def test_the_walk_reads_every_task_once_and_skips_junk_tokens(self, tmp_path: Path) -> None:
        _write(tmp_path, "1/task/1/children", "2 x 3")
        _write(tmp_path, "1/task/7/children", "3 4")
        _write(tmp_path, "2/task/2/children", "1")  # a cycle back to the root
        (tmp_path / "3").mkdir()  # no task dir: listed, not walked
        walked = proc.iter_descendants(str(tmp_path), 1)
        assert walked[0] == 1
        assert sorted(walked) == [1, 2, 3, 4]

    def test_an_unreadable_root_walks_to_itself(self, tmp_path: Path) -> None:
        assert proc.iter_descendants(str(tmp_path), 5) == [5]

    def test_direct_children_read_every_task_and_skip_junk(self, tmp_path: Path) -> None:
        _write(tmp_path, "1/task/1/children", "2 junk 3")
        _write(tmp_path, "1/task/9/children", "4")
        assert sorted(proc.direct_children(str(tmp_path), 1)) == [2, 3, 4]
        assert proc.direct_children(str(tmp_path), 8) == []

    def test_the_children_interface_is_readable_only_when_a_file_reads(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "1/task/1/children", "")
        (tmp_path / "2" / "task" / "2").mkdir(parents=True)
        assert proc.children_interface_readable(str(tmp_path), 1) is True
        assert proc.children_interface_readable(str(tmp_path), 2) is False
        assert proc.children_interface_readable(str(tmp_path), 3) is False


class TestPerProcessFiles:
    def test_the_cmdline_is_argv_joined_by_spaces(self, tmp_path: Path) -> None:
        (tmp_path / "1").mkdir()
        (tmp_path / "1" / "cmdline").write_bytes(b"bash\0-c\0ls\0")
        assert proc.read_cmdline(str(tmp_path), 1) == "bash -c ls"
        assert proc.read_cmdline(str(tmp_path), 2) == ""

    def test_io_bytes_sum_rchar_and_wchar_only(self, tmp_path: Path) -> None:
        _write(tmp_path, "1/io", "rchar: 10\nwchar: 5\nsyscr: 99\n")
        _write(tmp_path, "2/io", "rchar: nope\nread_bytes: 4\n")
        _write(tmp_path, "3/io", "")
        assert proc.read_io_bytes(str(tmp_path), 1) == 15
        assert proc.read_io_bytes(str(tmp_path), 2) is None
        assert proc.read_io_bytes(str(tmp_path), 3) is None
        assert proc.read_io_bytes(str(tmp_path), 4) is None

    def test_the_wchan_is_stripped_and_empty_when_missing(self, tmp_path: Path) -> None:
        _write(tmp_path, "1/wchan", "n_tty_read\n")
        assert proc.read_wchan(str(tmp_path), 1) == "n_tty_read"
        assert proc.read_wchan(str(tmp_path), 2) == ""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0 0x3 0x0", 3),
            ("63 0xa 0x0", 10),
            ("1 0x3 0x0", None),
            ("0", None),
            ("x 0x3", None),
            ("0 zz", None),
            ("", None),
            (None, None),
        ],
    )
    def test_only_a_read_syscall_names_a_blocked_fd(
        self, tmp_path: Path, raw: str | None, expected: int | None
    ) -> None:
        if raw is not None:
            _write(tmp_path, "1/syscall", raw)
        assert proc.blocked_read_fd(str(tmp_path), 1) == expected


@requires_symlinks
class TestFdLinks:
    def test_fd_targets_and_socket_inodes(self, tmp_path: Path) -> None:
        fd = tmp_path / "1" / "fd"
        fd.mkdir(parents=True)
        (fd / "3").symlink_to("socket:[777]")
        (fd / "4").symlink_to("pipe:[12]")
        (fd / "5").write_text("", encoding="utf-8")  # not a link: readlink refuses it
        assert proc.fd_target(str(tmp_path), 1, 4) == "pipe:[12]"
        assert proc.fd_target(str(tmp_path), 1, 9) == ""
        assert proc.socket_inodes(str(tmp_path), 1) == {"777"}
        assert proc.socket_inodes(str(tmp_path), 2) == set()

    def test_a_pipe_writer_is_another_pid_holding_the_pipe_for_writing(
        self, tmp_path: Path
    ) -> None:
        root = str(tmp_path)
        (tmp_path / "1" / "fd").mkdir(parents=True)
        (tmp_path / "1" / "fd" / "0").symlink_to("pipe:[9]")  # the reader itself
        (tmp_path / "3" / "fd").mkdir(parents=True)
        (tmp_path / "3" / "fd" / "x").write_text("", encoding="utf-8")
        (tmp_path / "3" / "fd" / "4").symlink_to("pipe:[8]")  # another pipe
        (tmp_path / "3" / "fd" / "5").symlink_to("pipe:[9]")
        _write(tmp_path, "3/fdinfo/5", "pos:\t0\nflags:\t0100000\n")  # read-only
        (tmp_path / "4" / "fd").mkdir(parents=True)
        (tmp_path / "4" / "fd" / "6").symlink_to("pipe:[9]")
        _write(tmp_path, "4/fdinfo/6", "pos:\t0\nflags:\t01\n")  # O_WRONLY
        assert proc.pipe_writer_pid(root, [1, 2, 3, 4], "pipe:[9]", 1) == 4
        assert proc.pipe_writer_pid(root, [1, 2, 3], "pipe:[9]", 1) is None


class TestEstablishedSockets:
    def test_established_rows_and_their_non_loopback_subset(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "1/net/tcp",
            _NET_HEADER
            + _net_row("0100007F:1F90", "01", "11")
            + _net_row("0A000001:01BB", "01", "12")
            + _net_row("0A000001:01BB", "0A", "13")
            + "   1: short line\n",
        )
        _write(
            tmp_path,
            "1/net/tcp6",
            _NET_HEADER
            + _net_row("00000000000000000000000001000000:0050", "01", "14")
            + _net_row("0000000000000000FFFF00000100007F:0050", "01", "15")
            + _net_row("20010DB8000000000000000000000001:01BB", "01", "16"),
        )
        assert proc.established_inodes(str(tmp_path), 1) == {"11", "12", "14", "15", "16"}
        assert proc.remote_established_inodes(str(tmp_path), 1) == {"12", "16"}
        assert proc.established_inodes(str(tmp_path), 2) == set()
        assert proc.remote_established_inodes(str(tmp_path), 2) == set()

    @pytest.mark.parametrize(
        ("address", "loopback"),
        [
            ("0100007F:1F90", True),
            ("0A000001:01BB", False),
            ("00000000000000000000000001000000:0050", True),
            ("0000000000000000FFFF00000100007F:0050", True),
            ("0000000000000000FFFF00000A000001:0050", False),
            ("ABC:0050", False),
        ],
    )
    def test_a_loopback_peer_is_read_from_the_hex_column(
        self, address: str, loopback: bool
    ) -> None:
        assert proc._is_loopback_hex(address) is loopback


class TestLibprocBackend:
    @staticmethod
    def _stub(
        monkeypatch: pytest.MonkeyPatch,
        *,
        facts: dict[int, object],
        children: dict[int, list[int] | None],
        argv: dict[int, list[str]] | None = None,
        path: dict[int, str] | None = None,
    ) -> None:
        monkeypatch.setattr(platform_compat, "darwin_process_facts", facts.get)
        monkeypatch.setattr(platform_compat, "darwin_child_pids", lambda p: children.get(p, []))
        monkeypatch.setattr(platform_compat, "darwin_process_argv", (argv or {}).get)
        monkeypatch.setattr(platform_compat, "darwin_process_path", (path or {}).get)

    def test_the_subtree_is_every_live_descendant_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = SimpleNamespace(zombie=False, start_secs=10.0)
        self._stub(monkeypatch, facts={1: live}, children={1: [2, 3], 2: [4, 1], 3: [], 4: []})
        assert sorted(proc.LibprocBackend().descendants(1) or []) == [2, 3, 4]

    def test_an_unreadable_root_or_branch_hides_the_whole_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = SimpleNamespace(zombie=False, start_secs=10.0)
        self._stub(monkeypatch, facts={1: live}, children={1: [2], 2: None})
        backend = proc.LibprocBackend()
        assert backend.descendants(1) is None
        assert backend.descendants(9) is None

    def test_a_row_reads_argv_then_the_executable_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = SimpleNamespace(zombie=False, start_secs=10.5)
        zombie = SimpleNamespace(zombie=True, start_secs=1.0)
        self._stub(
            monkeypatch,
            facts={1: live, 2: live, 3: live, 4: zombie},
            children={},
            argv={1: ["bash", "-c", "make"]},
            path={2: "/opt/tools/make"},
        )
        backend = proc.LibprocBackend()
        assert backend.row(1) == proc.ProcessRow(pid=1, started=10.5, cmdline="bash -c make")
        assert backend.row(2) == proc.ProcessRow(pid=2, started=10.5, cmdline="/opt/tools/make")
        assert backend.row(3) == proc.ProcessRow(pid=3, started=10.5, cmdline="")
        assert backend.row(4) is None
        assert backend.row(5) is None

    def test_cpu_time_and_socket_counts_come_from_the_probes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", lambda p: p * 1000)
        monkeypatch.setattr(platform_compat, "darwin_established_tcp_count", lambda p: p + 1)
        backend = proc.LibprocBackend()
        assert (backend.cpu_nanos(7), backend.established_tcp(7)) == (7000, 8)
