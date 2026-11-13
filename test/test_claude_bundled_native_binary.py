"""The "claude not found" warning fires only when the SDK has no binary of its own.

claude-agent-acp's SDK installs its native binary as an optional dependency
(``@anthropic-ai/claude-agent-sdk-<platform>-<arch>``). With it present a session
starts and runs a turn with no ``claude`` on PATH (measured on adapter 0.100.0), so
warning that the adapter "will fail" on every spawn was false.
"""

from __future__ import annotations

import logging
import platform
import sys

import pytest

from kiro_crew.acp.harness import claude as claude_mod

_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine().lower()
)

pytestmark = pytest.mark.skipif(_ARCH is None, reason="no native SDK package for this arch")


def _adapter(tmp_path, *, bundled: bool):
    """A fake global install: the adapter's entry and, optionally, the SDK binary."""
    root = tmp_path / "lib" / "node_modules"
    entry = root / "@agentclientprotocol" / "claude-agent-acp" / "dist" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// adapter\n", encoding="utf-8")
    if bundled:
        pkg = root / "@anthropic-ai" / f"claude-agent-sdk-{sys.platform}-{_ARCH}"
        pkg.mkdir(parents=True)
        (pkg / ("claude.exe" if sys.platform == "win32" else "claude")).write_bytes(b"\0")
    return (["node", str(entry)], "")


def _spawn_env(monkeypatch, resolved) -> dict[str, str]:
    monkeypatch.setattr(claude_mod, "_claude_acp_argv_cache", resolved)
    monkeypatch.setattr(claude_mod, "_resolve_claude_code_executable", lambda: None)
    env: dict[str, str] = {}
    claude_mod.ClaudeLaunch().apply_spawn_env(env)
    return env


def test_no_warning_when_the_sdk_ships_its_binary(tmp_path, monkeypatch, caplog):
    resolved = _adapter(tmp_path, bundled=True)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        env = _spawn_env(monkeypatch, resolved)
    assert "CLAUDE_CODE_EXECUTABLE" not in env
    assert not [r for r in caplog.records if "not found on PATH" in r.getMessage()]


def test_still_warns_when_no_binary_is_anywhere(tmp_path, monkeypatch, caplog):
    resolved = _adapter(tmp_path, bundled=False)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        _spawn_env(monkeypatch, resolved)
    assert [r for r in caplog.records if "not found on PATH" in r.getMessage()]


def test_an_unresolved_adapter_still_warns(monkeypatch, caplog):
    from kiro_crew.acp import launch as launch_mod

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        _spawn_env(monkeypatch, launch_mod._UNRESOLVED)
    assert [r for r in caplog.records if "not found on PATH" in r.getMessage()]
