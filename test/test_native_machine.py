"""Regression tests for the WMI-free ``platform`` override.

CPython 3.11+ asks WMI (``platform._wmi_query``) for ``machine()``, the
``uname().processor`` string and the OS version. When WMI is slow the stdlib
abandons the query's helper thread, which then spins inside ``wbemprox.dll``
for the life of the process (python/cpython#125315). Importing ``kiro_crew``
replaces ``_wmi_query`` with one that raises ``OSError``, the failure every
stdlib call site already falls back from.

Windows is simulated on POSIX by patching ``sys.platform``; the stdlib chain
it drives is the real one. A canary ``platform._wmi`` module turns any WMI
contact into a loud failure instead of a silent fallback.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import types

import pytest

import kiro_crew

needs_wmi_hook = pytest.mark.skipif(
    not hasattr(platform, "_wmi_query"),
    reason="this stdlib has no platform._wmi_query (Python < 3.11)",
)


def _forbidden_exec_query(query: str) -> str:
    raise AssertionError(f"WMI contacted: {query!r}")


@pytest.fixture
def windows_sim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend this interpreter runs on Windows, with WMI present but lethal."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_wmi = types.ModuleType("_wmi")
    fake_wmi.exec_query = _forbidden_exec_query  # type: ignore[attr-defined]
    monkeypatch.setattr(platform, "_wmi", fake_wmi, raising=False)
    if hasattr(platform, "_wmi_query"):
        monkeypatch.setattr(platform, "_wmi_query", platform._wmi_query)


def test_install_is_noop_off_windows():
    if sys.platform == "win32":
        pytest.skip("only meaningful off Windows")
    before = getattr(platform, "_wmi_query", None)
    assert kiro_crew._install_wmi_free_platform() is False
    assert getattr(platform, "_wmi_query", None) is before


@needs_wmi_hook
@pytest.mark.parametrize(
    "env, expected",
    [
        ({"PROCESSOR_ARCHITECTURE": "AMD64"}, "AMD64"),
        ({"PROCESSOR_ARCHITECTURE": "ARM64"}, "ARM64"),
        # A 32-bit process on 64-bit Windows: the stdlib reads the native chip.
        ({"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "AMD64"}, "AMD64"),
    ],
)
def test_machine_chain_never_reaches_wmi(windows_sim, monkeypatch, env, expected):
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert kiro_crew._install_wmi_free_platform() is True
    assert platform._wmi_query is kiro_crew._wmi_query_disabled
    assert platform._get_machine_win32() == expected


@needs_wmi_hook
@pytest.mark.parametrize(
    "query", [("CPU", "Architecture"), ("CPU", "Name"), ("OS", "Caption"), ("OS",)]
)
def test_every_query_raises_oserror(windows_sim, query):
    assert kiro_crew._install_wmi_free_platform() is True
    with pytest.raises(OSError):
        platform._wmi_query(*query)


@needs_wmi_hook
def test_install_is_idempotent(windows_sim):
    assert kiro_crew._install_wmi_free_platform() is True
    assert kiro_crew._install_wmi_free_platform() is True
    assert platform._wmi_query is kiro_crew._wmi_query_disabled


def test_install_reports_false_without_stdlib_hook(windows_sim, monkeypatch):
    monkeypatch.delattr(platform, "_wmi_query", raising=False)
    assert kiro_crew._install_wmi_free_platform() is False
    assert not hasattr(platform, "_wmi_query")


@needs_wmi_hook
def test_importing_kiro_crew_installs_override_in_fresh_interpreter():
    # platform.uname() caches machine() per interpreter, so the import-time
    # wiring must be proven in a process that has not computed it yet. The
    # child pretends to be Windows BEFORE importing kiro_crew, with a lethal
    # _wmi canary. asyncio is imported first because its module body branches
    # on sys.platform; once cached, kiro_crew's ``import asyncio`` reuses the
    # POSIX one. On a real Windows child that line is a no-op.
    code = "\n".join(
        [
            "import asyncio",
            "import sys",
            "import types",
            "sys.platform = 'win32'",
            "import platform",
            "def boom(q): raise AssertionError('WMI contacted: ' + q)",
            "platform._wmi = types.SimpleNamespace(exec_query=boom)",
            "import kiro_crew",
            "assert platform._wmi_query is kiro_crew._wmi_query_disabled",
            "print(platform._get_machine_win32())",
        ]
    )
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
    assert result.stdout.strip() == "AMD64"
