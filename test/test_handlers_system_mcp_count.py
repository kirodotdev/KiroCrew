"""Tests for MCP process counting in handlers_system.py (Linux + macOS)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


def _run_collect() -> dict:
    """Run _collect_system_metrics and return the result dict.

    Mocks _get_static_system_info to avoid subprocess calls for static info.
    The remaining system metrics (memory, CPU, network) are all wrapped in
    try/except so they degrade gracefully — only the MCP block matters here.
    """
    from kiro_crew.dashboard import handlers_system

    with patch.object(handlers_system, "_get_static_system_info", return_value={}):
        # Reset both caches so each test gets a fresh call — the process scan
        # cache has a 15s TTL that survives across xdist workers.
        handlers_system._metrics_cache.clear()
        handlers_system._metrics_cache_ts = 0.0
        handlers_system._proc_scan_cache = {}
        handlers_system._proc_scan_cache_ts = 0.0
        return handlers_system._collect_system_metrics()


class TestMcpProcessCountLinux:
    """Linux path: scans /proc/*/cmdline."""

    def test_counts_all_signatures(self) -> None:
        fake_procs = {
            "10": b"python3\x00/tmp/kirocrew_sandbox_abc.py",
            "20": b"kiro-cli\x00acp\x00--agent\x00kirocrew",
            "40": b"postgres\x00-D\x00/var/lib/pg",
        }
        orig_listdir = os.listdir

        def fake_listdir(path: str) -> list[str]:
            if path == "/proc":
                return list(fake_procs.keys()) + ["self", "99999"]
            return orig_listdir(path)

        orig_read_bytes = Path.read_bytes

        def fake_read_bytes(self_path: Path) -> bytes:
            parts = str(self_path).split("/")
            if len(parts) >= 4 and parts[1] == "proc" and parts[3] == "cmdline":
                pid = parts[2]
                if pid in fake_procs:
                    return fake_procs[pid]
            return orig_read_bytes(self_path)

        with (
            patch("kiro_crew.dashboard.handlers_system.sys") as mock_sys,
            patch("kiro_crew.dashboard.handlers_system.os.getpid", return_value=99999),
            patch("kiro_crew.dashboard.handlers_system.os.listdir", side_effect=fake_listdir),
            patch.object(Path, "read_bytes", fake_read_bytes),
            # Linux MCP counting uses /proc (os.listdir + read_bytes), NOT
            # subprocess. Stub check_output so the unrelated CPU `ps -A -o %cpu`
            # block never spawns a real process — under xdist CPU saturation its
            # 2s timeout SIGKILLs the child and the Popen-cleanup waitpid reap
            # hangs past pytest-timeout (a flaky failure under load).
            patch(
                "kiro_crew.dashboard.handlers_system.subprocess.check_output",
                return_value="%CPU\n0.0\n",
            ),
        ):
            mock_sys.platform = "linux"
            data = _run_collect()

        assert data["mcp_processes"]["sandbox"] == 1
        assert data["mcp_processes"]["kiro_cli"] == 1
        assert data["mcp_total"] == 2

    def test_excludes_self(self) -> None:
        orig_listdir = os.listdir

        def fake_listdir(path: str) -> list[str]:
            if path == "/proc":
                return ["10"]
            return orig_listdir(path)

        def fake_read_bytes(self_path: Path) -> bytes:
            return b"kiro-cli\x00acp"

        with (
            patch("kiro_crew.dashboard.handlers_system.sys") as mock_sys,
            patch("kiro_crew.dashboard.handlers_system.os.getpid", return_value=10),
            patch("kiro_crew.dashboard.handlers_system.os.listdir", side_effect=fake_listdir),
            patch.object(Path, "read_bytes", fake_read_bytes),
            # See test_counts_all_signatures: stub the CPU `ps` subprocess so a
            # 2s-timeout SIGKILL + waitpid hang can't make this flaky under load.
            patch(
                "kiro_crew.dashboard.handlers_system.subprocess.check_output",
                return_value="%CPU\n0.0\n",
            ),
        ):
            mock_sys.platform = "linux"
            data = _run_collect()

        assert data["mcp_total"] == 0


class TestMcpProcessCountMacOS:
    """macOS path: uses ps -eo pid,command."""

    def test_counts_all_signatures(self) -> None:
        ps_output = (
            "  PID COMMAND\n"
            "   10 python3 /tmp/kirocrew_sandbox_abc.py\n"
            "   20 kiro-cli acp --agent kirocrew\n"
            "   40 postgres -D /var/lib/pg\n"
        )

        with (
            patch("kiro_crew.dashboard.handlers_system.sys") as mock_sys,
            patch("kiro_crew.dashboard.handlers_system.os.getpid", return_value=99999),
            patch(
                "kiro_crew.dashboard.handlers_system.subprocess.check_output",
                return_value=ps_output,
            ),
        ):
            mock_sys.platform = "darwin"
            data = _run_collect()

        assert data["mcp_processes"]["sandbox"] == 1
        assert data["mcp_processes"]["kiro_cli"] == 1
        assert data["mcp_total"] == 2

    def test_excludes_self(self) -> None:
        ps_output = "  PID COMMAND\n   42 kiro-cli acp\n"

        with (
            patch("kiro_crew.dashboard.handlers_system.sys") as mock_sys,
            patch("kiro_crew.dashboard.handlers_system.os.getpid", return_value=42),
            patch(
                "kiro_crew.dashboard.handlers_system.subprocess.check_output",
                return_value=ps_output,
            ),
        ):
            mock_sys.platform = "darwin"
            data = _run_collect()

        assert data["mcp_total"] == 0

    def test_ps_failure_returns_zeros(self) -> None:
        with (
            patch("kiro_crew.dashboard.handlers_system.sys") as mock_sys,
            patch("kiro_crew.dashboard.handlers_system.os.getpid", return_value=1),
            patch(
                "kiro_crew.dashboard.handlers_system.subprocess.check_output",
                side_effect=OSError("ps not found"),
            ),
        ):
            mock_sys.platform = "darwin"
            data = _run_collect()

        assert data["mcp_processes"] == {"sandbox": 0, "kiro_cli": 0}
        assert data["mcp_total"] == 0
