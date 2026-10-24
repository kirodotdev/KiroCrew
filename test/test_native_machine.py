"""Regression tests for the WMI-free platform.machine() override.

CPython 3.11+ answers ``platform.machine()`` through a WMI query
(``platform._wmi_query`` -> ``Win32_Processor.Architecture``). The query runs
on a helper thread with a short fixed timeout; when WMI is slow -- a dozen
processes asking at once at gateway start is enough -- the stdlib takes its
env-var fallback and abandons the helper thread, which then lives (and spins)
for the life of the process (python/cpython#125315; the crash half was fixed
in 3.12.8, the abandoned thread was not). Long-lived Kiro Crew processes -- the
per-session MCP servers -- burned two cores each idle
(kirodotdev/KiroCrew#21632: the hot threads are inside ``wbemprox.dll``).

``kiro_crew.native_machine`` installs a WMI-free ``platform._wmi_query`` the
moment ``kiro_crew`` is imported, so no Kiro Crew process ever touches WMI for
the machine string.

Windows-only behaviour is simulated on POSIX by patching ``sys.platform``; the
stdlib chain it drives (``_get_machine_win32`` -> the Architecture-code map)
is the real one. A canary ``platform._wmi`` module turns any WMI contact back
into a loud failure instead of a silent fallback.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import types

import pytest

from kiro_crew import native_machine


def _forbidden_exec_query(query: str) -> str:
    raise AssertionError(f"WMI contacted: {query!r}")


@pytest.fixture
def windows_sim(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Pretend this interpreter runs on Windows, with WMI present but lethal."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_wmi = types.ModuleType("_wmi")
    fake_wmi.exec_query = _forbidden_exec_query  # type: ignore[attr-defined]
    monkeypatch.setattr(platform, "_wmi", fake_wmi, raising=False)
    return fake_wmi


def test_native_machine_agrees_with_platform_machine():
    # POSIX: the plain stdlib answer, unchanged. Windows: the IsWow64Process2
    # answer, which names the chip the way WMI would.
    assert native_machine.native_machine() == platform.machine()


def test_install_platform_override_is_noop_off_windows(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("only meaningful off Windows")
    before = platform._wmi_query
    assert native_machine.install_platform_override() is False
    assert platform._wmi_query is before


def test_override_answers_machine_chain_without_wmi(windows_sim, monkeypatch):
    # The value platform._get_machine_win32 sees is dictated by the injected
    # native_machine() answer, not by any query -- proving the full stdlib
    # chain (query result -> Architecture code -> name) never reaches WMI.
    monkeypatch.setattr(native_machine, "native_machine", lambda: "ARM64")
    assert native_machine.install_platform_override() is True
    assert platform._wmi_query is native_machine._wmi_query_without_wmi
    assert platform._get_machine_win32() == "ARM64"


def test_installed_query_serves_cpu_architecture_only(windows_sim, monkeypatch):
    monkeypatch.setattr(native_machine, "native_machine", lambda: "AMD64")
    assert native_machine.install_platform_override() is True
    assert list(platform._wmi_query("CPU", "Architecture")) == ["9"]
    # Every other table/shape must raise OSError so the stdlib falls back the
    # way a failed WMI query does (win32_ver -> sys.getwindowsversion).
    with pytest.raises(OSError):
        platform._wmi_query("OS", "Caption")
    with pytest.raises(OSError):
        platform._wmi_query("CPU", "Name")
    with pytest.raises(OSError):
        platform._wmi_query("CPU", "Architecture", "Name")


def test_install_is_idempotent(windows_sim):
    assert native_machine.install_platform_override() is True
    installed = platform._wmi_query
    assert native_machine.install_platform_override() is True
    assert platform._wmi_query is installed


def test_install_reports_false_without_stdlib_hook(windows_sim, monkeypatch):
    # A stdlib without platform._wmi_query (Python < 3.11, where machine()
    # never touched WMI in the first place): report False, change nothing.
    monkeypatch.delattr(platform, "_wmi_query")
    assert native_machine.install_platform_override() is False
    assert not hasattr(platform, "_wmi_query")


#: Win32_Processor.Architecture codes the stdlib maps back to machine names.
_VALID_ARCH_CODES = ("0", "5", "9", "12")


def test_importing_kiro_crew_installs_override_in_fresh_interpreter():
    # platform.uname() caches machine() per interpreter, so the import-time
    # wiring must be proven in a process that has not computed it yet (a
    # reused interpreter passes vacuously). The child pretends to be Windows
    # BEFORE importing kiro_crew -- the exact order of the real win32 process,
    # and the only way the install is observable on POSIX at all. asyncio is
    # imported first because its module body branches on sys.platform: once
    # cached, kiro_crew's own ``import asyncio`` reuses the POSIX one instead
    # of chasing Windows-only submodules. On a real Windows child this line is
    # the same no-op it always is.
    code = "\n".join(
        [
            "import asyncio",
            "import sys",
            "sys.platform = 'win32'",
            "import platform",
            "import kiro_crew",
            "assert platform._wmi_query.__module__ == 'kiro_crew.native_machine',"
            " platform._wmi_query",
            "from kiro_crew import native_machine",
            "native_machine.native_machine = lambda: 'AMD64'",
            "print(list(platform._wmi_query('CPU', 'Architecture')))",
        ]
    )
    # Once the import-installed override is verified, the child's
    # native_machine() is stubbed to a fixed answer before the query: a real
    # Windows ARM64 child would otherwise answer from IsWow64Process2
    # (['12']) and fail the assertion despite correct behaviour. The env pin
    # keeps the child's unstubbed answer coherent on POSIX.
    env = {k: v for k, v in os.environ.items() if k != "PROCESSOR_ARCHITEW6432"}
    env["PROCESSOR_ARCHITECTURE"] = "AMD64"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['9']"
