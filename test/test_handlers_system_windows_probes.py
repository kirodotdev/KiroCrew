"""Windows sources for the System page's MCP process count and network counters.

Windows has no ``ps`` and no ``/proc``. These tests run on any host: the platform
is patched to ``win32`` and the Windows readers are stubbed, and the readers'
own parsing and struct layout are checked directly.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import handlers_system, system_windows_probes
from kiro_crew.dashboard.system_windows_probes import (
    _command_line_from_buffer,
    _MibIfRow2,
    _sum_interface_octets,
    _UnicodeString,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def _collect_on_windows(
    *,
    rows: list[tuple[int, str]] | None,
    octets: list[tuple[int, int] | None],
    my_pid: int = 99999,
    samples: int = 1,
) -> tuple[dict, list[tuple[str, ...]]]:
    """Run ``_collect_system_metrics`` as Windows would, *samples* times.

    ``ps`` and ``/proc/net/dev`` are made unreachable, as on Windows, so a probe
    that still reaches for them finds nothing.
    """
    calls: list[tuple[str, ...]] = []
    octet_iter = iter(octets)

    def fake_rows(signatures):
        calls.append(tuple(signatures))
        return rows

    fake_probes = SimpleNamespace(
        windows_process_command_lines=fake_rows,
        windows_network_octets=lambda: next(octet_iter),
    )
    real_open = open

    def no_proc_open(path, *args, **kwargs):
        if str(path).startswith("/proc"):
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    data: dict = {}
    with (
        patch.object(handlers_system, "sys", SimpleNamespace(platform="win32")),
        patch.object(handlers_system, "system_windows_probes", fake_probes, create=True),
        patch.object(handlers_system, "_get_static_system_info", return_value={}),
        patch.object(handlers_system, "_local_ip", return_value="127.0.0.1"),
        patch.object(handlers_system.os, "getpid", return_value=my_pid),
        patch.object(
            handlers_system.subprocess,
            "check_output",
            side_effect=FileNotFoundError("ps"),
        ),
        patch("builtins.open", no_proc_open),
        patch.dict(handlers_system._prev_net, {"rx": 0.0, "tx": 0.0, "ts": 0.0}),
        patch.dict(handlers_system._net_speed, {"rx_kbs": 0.0, "tx_kbs": 0.0}),
    ):
        for index in range(samples):
            if index:
                # Age the previous sample by two seconds so the speed is computed.
                handlers_system._prev_net["ts"] -= 2.0
            handlers_system._metrics_cache.clear()
            handlers_system._metrics_cache_ts = 0.0
            handlers_system._proc_scan_cache = {}
            handlers_system._proc_scan_cache_ts = 0.0
            data = handlers_system._collect_system_metrics()
    return data, calls


class TestWindowsMcpProcessCount:
    def test_counts_kiro_cli_and_sandbox_processes(self) -> None:
        rows = [
            (10, r"C:\Python\python.exe C:\Users\u\.kiro\crew\run\kirocrew_sandbox_a.py"),
            (20, r"C:\Users\u\.local\bin\kiro-cli.exe acp --agent kirocrew"),
            (21, r'"C:\Users\u\.local\bin\kiro-cli.exe" chat'),
            (22, r"C:\Users\u\.local\bin\Kiro-CLI.EXE acp"),
        ]
        data, calls = _collect_on_windows(rows=rows, octets=[(0, 0)])

        assert calls == [("kirocrew_sandbox", "kiro-cli")]
        assert data["mcp_processes"] == {"sandbox": 1, "kiro_cli": 3}
        assert data["mcp_total"] == 4

    def test_excludes_own_pid(self) -> None:
        data, _ = _collect_on_windows(rows=[(42, "kiro-cli.exe acp")], octets=[(0, 0)], my_pid=42)

        assert data["mcp_total"] == 0

    def test_dedups_a_process_matching_both_signatures(self) -> None:
        rows = [(7, "python.exe kirocrew_sandbox_x.py kiro-cli.exe acp")]
        data, _ = _collect_on_windows(rows=rows, octets=[(0, 0)])

        assert data["mcp_processes"] == {"sandbox": 1, "kiro_cli": 1}
        assert data["mcp_total"] == 1

    def test_unreadable_table_counts_zero(self) -> None:
        data, _ = _collect_on_windows(rows=None, octets=[(0, 0)])

        assert data["mcp_processes"] == {"sandbox": 0, "kiro_cli": 0}
        assert data["mcp_total"] == 0


class TestWindowsNetwork:
    def test_speed_below_the_megabyte_rounding_step_is_reported(self) -> None:
        mib = 1024**2
        # A frozen clock: the helper's 2.0 s aging is then the whole interval.
        frozen = SimpleNamespace(monotonic=lambda: 1000.0, time=time.time)
        with patch.object(handlers_system, "time", frozen):
            data, _ = _collect_on_windows(
                rows=[],
                octets=[(10 * mib, 4 * mib), (10 * mib + 20 * 1024, 4 * mib + 6 * 1024)],
                samples=2,
            )

        assert data.get("net_rx_mb") == 10.0
        assert data.get("net_rx_kbs") == 10.0
        assert data.get("net_tx_kbs") == 3.0

    def test_reports_totals_and_speed_from_interface_counters(self) -> None:
        mib = 1024**2
        data, _ = _collect_on_windows(
            rows=[],
            octets=[(10 * mib, 4 * mib), (12 * mib, 5 * mib)],
            samples=2,
        )

        assert data["net_rx_mb"] == 12.0
        assert data["net_tx_mb"] == 5.0
        assert data["net_rx_kbs"] > 0
        assert data["net_tx_kbs"] > 0

    def test_unreadable_counters_leave_network_keys_out(self) -> None:
        data, _ = _collect_on_windows(rows=[], octets=[None])

        for key in ("net_rx_mb", "net_tx_mb", "net_rx_kbs", "net_tx_kbs"):
            assert key not in data


class TestProcessTableRead:
    """The in-process reader: a Toolhelp snapshot of pids, then one command line per pid."""

    def _read(self, table: dict[int, str | None], signatures=("kirocrew_sandbox", "kiro-cli")):
        parent_map = {pid: 4 for pid in table}
        with (
            patch.object(
                system_windows_probes.platform_compat,
                "_windows_process_parent_map",
                return_value=parent_map,
            ),
            patch.object(system_windows_probes, "_command_line_reader", return_value=table.get),
        ):
            return system_windows_probes.windows_process_command_lines(signatures)

    def test_keeps_the_processes_holding_a_signature_in_any_case(self) -> None:
        rows = self._read(
            {
                10: r"C:\Python\python.exe C:\u\.kiro\crew\run\kirocrew_sandbox_a.py",
                20: r"C:\u\.local\bin\Kiro-CLI.EXE acp",
                40: r"C:\Windows\explorer.exe",
            }
        )
        assert rows == [
            (10, r"C:\Python\python.exe C:\u\.kiro\crew\run\kirocrew_sandbox_a.py"),
            (20, r"C:\u\.local\bin\Kiro-CLI.EXE acp"),
        ]

    def test_skips_a_process_whose_command_line_cannot_be_read(self) -> None:
        assert self._read({30: None, 20: "kiro-cli.exe chat"}) == [(20, "kiro-cli.exe chat")]

    def test_no_signatures_reads_nothing(self) -> None:
        with patch.object(system_windows_probes, "_command_line_reader") as reader:
            assert system_windows_probes.windows_process_command_lines(()) == []
        reader.assert_not_called()

    def test_an_unreadable_snapshot_is_unreadable(self) -> None:
        with (
            patch.object(
                system_windows_probes.platform_compat,
                "_windows_process_parent_map",
                side_effect=OSError("Windows process enumeration failed"),
            ),
            patch.object(system_windows_probes, "_command_line_reader", return_value=dict().get),
        ):
            assert system_windows_probes.windows_process_command_lines(("kiro-cli",)) is None

    def test_no_reader_is_unreadable_and_takes_no_snapshot(self) -> None:
        with (
            patch.object(
                system_windows_probes.platform_compat, "_windows_process_parent_map"
            ) as snapshot,
            patch.object(system_windows_probes, "_command_line_reader", return_value=None),
        ):
            assert system_windows_probes.windows_process_command_lines(("kiro-cli",)) is None
        snapshot.assert_not_called()

    def test_starts_no_child_process(self) -> None:
        with (
            patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")),
            patch.object(
                system_windows_probes.platform_compat,
                "_windows_process_parent_map",
                return_value={7: 4},
            ),
            patch.object(
                system_windows_probes, "_command_line_reader", return_value={7: "kiro-cli"}.get
            ),
        ):
            assert system_windows_probes.windows_process_command_lines(("kiro-cli",)) == [
                (7, "kiro-cli")
            ]

    @pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows answer")
    def test_the_table_is_unreadable_off_windows(self) -> None:
        assert system_windows_probes.windows_process_command_lines(("kiro-cli",)) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="reads the real Windows process table")
    def test_finds_a_real_child_by_its_command_line_on_windows(self) -> None:
        marker = f"kcprobe-{os.getpid()}-{time.monotonic_ns()}"
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()", marker],
            stdin=subprocess.PIPE,
        )
        try:
            rows = system_windows_probes.windows_process_command_lines((marker,))
        finally:
            try:
                child.communicate(input=b"", timeout=FALLBACK_TIMEOUT_SECS)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=FALLBACK_TIMEOUT_SECS)
        assert rows is not None
        # A venv interpreter is a launcher that starts the base python.exe with the same
        # argv, so the marker may appear in two processes; the child is always one.
        assert child.pid in [pid for pid, _cmd in rows]
        assert all(marker in cmd for _pid, cmd in rows)


class TestCommandLineBuffer:
    """``ProcessCommandLineInformation`` answers a ``UNICODE_STRING`` pointing into itself."""

    def _answer(self, text: str, *, offset_delta: int = 0, length_delta: int = 0):
        head = ctypes.sizeof(_UnicodeString)
        data = text.encode("utf-16-le")
        buf = ctypes.create_string_buffer(head + len(data))
        ctypes.memmove(ctypes.addressof(buf) + head, data, len(data))
        string = _UnicodeString.from_buffer(buf)
        string.Length = len(data) + length_delta
        string.MaximumLength = len(data)
        string.Buffer = ctypes.addressof(buf) + head + offset_delta
        return buf

    def test_reads_the_command_line(self) -> None:
        line = '"C:\\bin\\kiro-cli.exe" acp --agent kirocrew \u00e9'
        assert _command_line_from_buffer(self._answer(line)) == line

    def test_refuses_a_string_reaching_past_the_answer(self) -> None:
        assert _command_line_from_buffer(self._answer("kiro-cli", length_delta=2)) is None

    def test_refuses_a_pointer_before_the_answer(self) -> None:
        assert _command_line_from_buffer(self._answer("kiro-cli", offset_delta=-64)) is None

    def test_refuses_an_odd_length(self) -> None:
        assert _command_line_from_buffer(self._answer("kiro-cli", length_delta=-1)) is None


class _FakeCall:
    """A stand-in for one ctypes foreign function: takes argtypes/restype, records calls."""

    def __init__(self, impl):
        self._impl = impl
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._impl(*args)


class TestCommandLineReader:
    """``_command_line_reader`` against fake kernel32 and ntdll, so it runs on every host."""

    _BUFFER_TOO_SMALL = 0xC0000023

    def _reader(self, *, handle: int, answers: list[tuple[int, int, str | None]]):
        """Build a reader whose query returns each ``(status, needed, text)`` in turn."""
        pending = list(answers)

        def query(_handle, _klass, buf, size, needed):
            status, wanted, text = pending.pop(0)
            needed._obj.value = wanted
            if text is not None:
                head = ctypes.sizeof(_UnicodeString)
                data = text.encode("utf-16-le")
                assert head + len(data) <= size
                ctypes.memmove(ctypes.addressof(buf) + head, data, len(data))
                string = _UnicodeString.from_buffer(buf)
                string.Length = len(data)
                string.MaximumLength = len(data)
                string.Buffer = ctypes.addressof(buf) + head
            return status

        calls = {
            "OpenProcess": _FakeCall(lambda _access, _inherit, _pid: handle),
            "CloseHandle": _FakeCall(lambda _h: 1),
            "NtQueryInformationProcess": _FakeCall(query),
        }
        dll = SimpleNamespace(**calls)
        with patch.object(ctypes, "WinDLL", lambda *_a, **_k: dll, create=True):
            reader = system_windows_probes._command_line_reader()
        assert reader is not None
        return reader, calls

    def test_reads_a_command_line_and_closes_the_handle(self) -> None:
        reader, calls = self._reader(handle=5, answers=[(0, 40, "kiro-cli.exe acp")])
        assert reader(42) == "kiro-cli.exe acp"
        assert calls["OpenProcess"].calls[0][2] == 42
        assert calls["CloseHandle"].calls == [(5,)]

    def test_reads_again_at_the_size_windows_asks_for(self) -> None:
        line = "kiro-cli " + "x" * 3000
        reader, calls = self._reader(
            handle=5, answers=[(self._BUFFER_TOO_SMALL, 8192, None), (0, 8192, line)]
        )
        assert reader(42) == line
        sizes = [call[3] for call in calls["NtQueryInformationProcess"].calls]
        assert sizes == [4096, 8192]
        assert calls["CloseHandle"].calls == [(5,)]

    def test_an_answer_past_the_command_line_limit_is_refused(self) -> None:
        reader, calls = self._reader(handle=5, answers=[(self._BUFFER_TOO_SMALL, 10_000_000, None)])
        assert reader(42) is None
        assert calls["CloseHandle"].calls == [(5,)]

    def test_a_failed_query_is_unreadable(self) -> None:
        reader, calls = self._reader(handle=5, answers=[(0xC0000022, 0, None)])
        assert reader(42) is None
        assert calls["CloseHandle"].calls == [(5,)]

    def test_a_process_that_cannot_be_opened_is_skipped_without_a_query(self) -> None:
        reader, calls = self._reader(handle=0, answers=[])
        assert reader(42) is None
        assert calls["NtQueryInformationProcess"].calls == []
        assert calls["CloseHandle"].calls == []


class TestInterfaceRowLayout:
    def test_matches_the_sdk_layout(self) -> None:
        assert ctypes.sizeof(_MibIfRow2) == 1352
        assert _MibIfRow2.InterfaceAndOperStatusFlags.offset == 1152
        assert _MibIfRow2.InOctets.offset == 1208
        assert _MibIfRow2.OutOctets.offset == 1280

    def test_sums_adapters_and_skips_loopback_and_filters(self) -> None:
        def row(type_: int, flags: int, rx: int, tx: int) -> _MibIfRow2:
            r = _MibIfRow2()
            r.Type = type_
            r.InterfaceAndOperStatusFlags = flags
            r.InOctets = rx
            r.OutOctets = tx
            return r

        rows = [
            row(6, 0x01, 1000, 200),  # ethernet adapter
            row(6, 0x02, 1000, 200),  # filter stacked on it
            row(24, 0x00, 50, 50),  # software loopback
            row(131, 0x00, 400, 80),  # tunnel over the ethernet adapter
            row(71, 0x01, 30, 7),  # wifi adapter
        ]
        assert _sum_interface_octets(rows) == (1030, 207)

    @pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows answer")
    def test_counters_are_unreadable_off_windows(self) -> None:
        assert system_windows_probes.windows_network_octets() is None

    @pytest.mark.skipif(sys.platform != "win32", reason="reads the real Windows interface table")
    def test_reads_real_counters_on_windows(self) -> None:
        octets = system_windows_probes.windows_network_octets()
        assert octets is not None
        rx, tx = octets
        assert rx >= 0 and tx >= 0
