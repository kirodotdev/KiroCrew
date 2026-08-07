"""Tests for macOS absolute path resolution in handlers_system.py."""

from __future__ import annotations

import sys
from unittest.mock import patch


class TestMacOsSysctlPaths:
    """Verify _SYSCTL and _VM_STAT resolve correctly on macOS."""

    def test_sysctl_resolves_via_which(self) -> None:
        """When shutil.which finds sysctl, use that path."""
        with patch(
            "shutil.which", side_effect=lambda cmd: f"/found/{cmd}" if cmd == "sysctl" else None
        ):
            import importlib

            from kiro_crew.dashboard import handlers_system

            importlib.reload(handlers_system)
            assert handlers_system._SYSCTL == "/found/sysctl"

    def test_sysctl_falls_back_to_usr_sbin(self) -> None:
        """When shutil.which returns None, fall back to /usr/sbin/sysctl."""
        with patch("shutil.which", return_value=None):
            import importlib

            from kiro_crew.dashboard import handlers_system

            importlib.reload(handlers_system)
            assert handlers_system._SYSCTL == "/usr/sbin/sysctl"

    def test_vm_stat_falls_back_to_usr_bin(self) -> None:
        """When shutil.which returns None, fall back to /usr/bin/vm_stat."""
        with patch("shutil.which", return_value=None):
            import importlib

            from kiro_crew.dashboard import handlers_system

            importlib.reload(handlers_system)
            assert handlers_system._VM_STAT == "/usr/bin/vm_stat"

    def test_collect_metrics_returns_mem_on_darwin(self) -> None:
        """On macOS, _collect_system_metrics returns mem_used_gb when commands succeed.

        Subprocess output is mocked per-command so the test is hermetic: it must
        not spawn real ``sysctl``/``vm_stat``/``ps``. The CPU block's real
        ``ps -A -o %cpu`` (timeout=2) otherwise gets SIGKILLed under xdist CPU
        saturation, and the Popen-cleanup ``waitpid`` reap hangs past
        pytest-timeout — a flaky test failure under load.
        """
        if sys.platform != "darwin":
            return  # Skip on non-macOS

        from kiro_crew.dashboard import handlers_system

        vm_stat_out = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                          100000.\n"
            "Pages inactive:                       50000.\n"
            "Anonymous pages:                     200000.\n"
            "Pages purgeable:                      10000.\n"
            "Pages wired down:                     80000.\n"
            "Pages occupied by compressor:         60000.\n"
        )

        def fake_check_output(cmd, **kwargs):  # type: ignore[no-untyped-def]
            exe = cmd[0]
            if exe == handlers_system._SYSCTL:
                return b"34359738368"  # 32 GiB, hw.memsize
            if exe == handlers_system._VM_STAT:
                return vm_stat_out.encode()
            return b"%CPU\n0.0\n"  # ps -A -o %cpu and any other call

        with (
            patch.object(handlers_system, "_get_static_system_info", return_value={}),
            patch(
                "kiro_crew.dashboard.handlers_system.subprocess.check_output",
                side_effect=fake_check_output,
            ),
        ):
            handlers_system._metrics_cache = {}
            handlers_system._metrics_cache_ts = 0.0
            data = handlers_system._collect_system_metrics()

        assert "mem_total_gb" in data
        assert "mem_used_gb" in data
        assert data["mem_total_gb"] > 0
        assert data["mem_used_gb"] > 0

    def test_collect_metrics_legacy_vm_stat_fallback(self) -> None:
        """Legacy vm_stat without 'Anonymous pages' falls back to 'Pages active'."""
        if sys.platform != "darwin":
            return  # Skip on non-macOS

        from kiro_crew.dashboard import handlers_system

        # Legacy output: only Pages free/inactive/active — no Anonymous pages line.
        vm_stat_out = (
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            "Pages free:                          100000.\n"
            "Pages inactive:                       50000.\n"
            "Pages active:                        300000.\n"
            "Pages wired down:                     80000.\n"
        )

        def fake_check_output(cmd, **kwargs):  # type: ignore[no-untyped-def]
            exe = cmd[0]
            if exe == handlers_system._SYSCTL:
                return b"17179869184"  # 16 GiB
            if exe == handlers_system._VM_STAT:
                return vm_stat_out.encode()
            return b"%CPU\n0.0\n"

        with (
            patch.object(handlers_system, "_get_static_system_info", return_value={}),
            patch(
                "kiro_crew.dashboard.handlers_system.subprocess.check_output",
                side_effect=fake_check_output,
            ),
        ):
            handlers_system._metrics_cache = {}
            handlers_system._metrics_cache_ts = 0.0
            data = handlers_system._collect_system_metrics()

        assert "mem_used_gb" in data
        # Legacy fallback: app_pages = Pages active (300000), wired = 80000
        # used_bytes = (300000 + 80000) * 4096 = 1,556,480,000 ~ 1.4 GB > 0
        assert data["mem_used_gb"] > 0
