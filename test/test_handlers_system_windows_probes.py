"""Windows sources for the System page's MCP process count and network counters.

Windows has no ``ps`` and no ``/proc``. These tests run on any host: the platform
is patched to ``win32`` and the Windows readers are stubbed, and the readers'
own parsing and struct layout are checked directly.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import handlers_system, system_windows_probes
from kiro_crew.dashboard.system_windows_probes import (
    _MibIfRow2,
    _parse_process_rows,
    _sum_interface_octets,
    _wql_literal,
)


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


class TestProcessRowParsing:
    def test_reads_a_json_array(self) -> None:
        out = '[{"ProcessId":10,"CommandLine":"kiro-cli acp"},{"ProcessId":11,"CommandLine":null}]'
        assert _parse_process_rows(out) == [(10, "kiro-cli acp")]

    def test_reads_a_single_object(self) -> None:
        assert _parse_process_rows('{"ProcessId":5,"CommandLine":"x"}') == [(5, "x")]

    def test_empty_output_is_no_rows(self) -> None:
        assert _parse_process_rows("  \r\n") == []

    def test_non_json_is_unreadable(self) -> None:
        assert _parse_process_rows("Get-CimInstance : Access denied") is None

    def test_wql_literal_matches_pattern_characters_literally(self) -> None:
        assert _wql_literal("kirocrew_sandbox") == "%kirocrew[_]sandbox%"
        assert _wql_literal("a[b'c") == "%a[[]b''c%"


class TestProcessQuery:
    def test_runs_one_filtered_powershell_query(self) -> None:
        seen: dict = {}

        def fake_check_output(argv, **kwargs):
            seen["argv"] = argv
            return '[{"ProcessId":3,"CommandLine":"kiro-cli acp"}]'

        with (
            patch.object(system_windows_probes, "trusted_system_bin", return_value="PS.EXE"),
            patch.object(system_windows_probes.subprocess, "check_output", fake_check_output),
        ):
            rows = system_windows_probes.windows_process_command_lines(("kiro-cli",))

        assert rows == [(3, "kiro-cli acp")]
        assert seen["argv"][0] == "PS.EXE"
        assert "CommandLine LIKE '%kiro-cli%'" in seen["argv"][-1]
        # The query's own PowerShell holds the signature in its command line.
        assert "Where-Object { $_.ProcessId -ne $PID }" in seen["argv"][-1]

    @pytest.mark.parametrize("signature", ["x$(whoami)", 'a"b', "a`b", "a'b", "a%b", "a b", ""])
    def test_refuses_a_signature_powershell_could_read_as_syntax(self, signature: str) -> None:
        with (
            patch.object(system_windows_probes, "trusted_system_bin", return_value="PS.EXE"),
            patch.object(system_windows_probes.subprocess, "check_output") as spawn,
            pytest.raises(ValueError),
        ):
            system_windows_probes.windows_process_command_lines(("kiro-cli", signature))
        spawn.assert_not_called()

    def test_missing_powershell_is_unreadable(self) -> None:
        with patch.object(system_windows_probes, "trusted_system_bin", return_value=None):
            assert system_windows_probes.windows_process_command_lines(("kiro-cli",)) is None

    def test_failed_query_is_unreadable(self) -> None:
        with (
            patch.object(system_windows_probes, "trusted_system_bin", return_value="PS.EXE"),
            patch.object(
                system_windows_probes.subprocess,
                "check_output",
                side_effect=subprocess.TimeoutExpired("PS.EXE", 10),
            ),
        ):
            assert system_windows_probes.windows_process_command_lines(("kiro-cli",)) is None


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
