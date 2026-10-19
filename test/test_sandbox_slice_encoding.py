"""Both ``systemctl --user set-property`` calls of the agents slice decode as UTF-8, leniently.

The child's stdout and stderr carry a unit name or a D-Bus error, localized on some hosts.
A text-mode spawn without ``encoding=`` decodes them with the locale's code page, and a
strict UTF-8 decode raises ``UnicodeDecodeError`` on a byte it cannot read, which would
skip the caller's own failure handling (its warning, and the ``False`` it returns). So
each call decodes as UTF-8 with ``errors="replace"``, as ``service/linux_unit.py`` does. Pinned
where it matters: the keyword arguments each caller hands ``subprocess.run``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat, sandbox


def _record_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Answer every ``subprocess.run`` with a clean exit and keep its keyword arguments."""
    calls: list[dict[str, object]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append({"argv": list(argv), **kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}")
    return calls


def _set_property_calls(calls: list[dict[str, object]]) -> list[dict[str, object]]:
    return [call for call in calls if "set-property" in call["argv"]]  # type: ignore[operator]


def test_the_memory_high_call_decodes_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_runs(monkeypatch)
    monkeypatch.setattr(sandbox, "_SLICE_MEMHIGH_DISABLED", False)
    monkeypatch.setattr(sandbox, "_SLICE_MEMHIGH_APPLIED", None)
    monkeypatch.setattr(sandbox, "_default_slice_memory_high_mb", lambda: 512)

    sandbox._ensure_agent_slice_memory_high()

    runs = _set_property_calls(calls)
    assert [call["argv"][-1] for call in runs] == ["MemoryHigh=512M"]  # type: ignore[index]
    assert runs[0]["text"] is True and runs[0]["encoding"] == "utf-8"
    assert runs[0]["errors"] == "replace"


def test_the_slice_limits_call_decodes_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_runs(monkeypatch)
    monkeypatch.setattr(sandbox, "_SLICE_LIMITS_APPLIED", False)
    monkeypatch.setattr(sandbox, "_SLICE_MEMHIGH_DISABLED", True)  # this call only
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: (True, ""))
    monkeypatch.setattr(sandbox, "_slice_limits_from_config", lambda: (2048, 512))

    assert sandbox.ensure_agents_slice_limits() is True

    runs = _set_property_calls(calls)
    assert any("MemoryMax=2048M" in call["argv"] for call in runs)  # type: ignore[operator]
    assert runs and all(call["text"] is True and call["encoding"] == "utf-8" for call in runs)
    assert all(call["errors"] == "replace" for call in runs)
