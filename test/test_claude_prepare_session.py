"""``prepare_claude_session``: the per-session work a claude session needs before ``session/new``.

It is split out of ``ClaudeLaunch.resolve_spawn`` so a driver that serves several
sessions on one process can run it once per session. These tests pin its order (the
seed before the MCP array, which reads the seed's ownership flag) and that the
one-process-per-session spawn still runs it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.acp.harness import SpawnContext
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution


class _Session:
    """Records the steps it is asked to run, and what was set before each one."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, dict[str, Any]]] = []
        self._agent_version_read = True

    def _seen(self) -> dict[str, Any]:
        return {
            "read": self._agent_version_read,
            "argv": getattr(self, "_claude_adapter_argv", None),
            "disk": getattr(self, "_claude_adapter_disk_version", None),
        }

    async def _seed_session_settings(self) -> None:
        self.steps.append(("seed", self._seen()))

    async def _prepare_claude_side_read_denies(self) -> None:
        self.steps.append(("side", self._seen()))

    async def _prepare_session_mcp(self) -> None:
        self.steps.append(("mcp", self._seen()))

    async def _prepare_claude_held_asks(self) -> None:
        self.steps.append(("held", self._seen()))

    def _refuse_claude_session_below_floor_before_spawn(self, installed_version: str) -> None:
        self.steps.append((f"floor:{installed_version}", self._seen()))


def test_the_version_is_read_then_the_seed_then_side_denies_then_the_array_then_held_asks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: "0.94.0")
    session = _Session()
    asyncio.run(claude_mod.prepare_claude_session(session, ["/opt/bin/claude-agent-acp"]))
    seen = {"read": False, "argv": ("/opt/bin/claude-agent-acp",), "disk": "0.94.0"}
    assert session.steps == [
        ("seed", seen),
        ("side", seen),
        ("mcp", seen),
        # The held asks name the array's servers, so they are read after it.
        ("held", seen),
        ("floor:0.94.0", seen),
    ]


def test_only_the_installed_version_reaches_the_pre_spawn_refusal(monkeypatch) -> None:
    """A remembered handshake fills the disk version but never refuses before spawn."""
    argv = ["/opt/bin/wrapper"]
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: "")
    monkeypatch.setitem(claude_mod._claude_adapter_handshake_versions, tuple(argv), "0.80.0")
    session = _Session()
    asyncio.run(claude_mod.prepare_claude_session(session, argv))
    seen = {"read": False, "argv": tuple(argv), "disk": "0.80.0"}
    assert session.steps[-1] == ("floor:", seen)


def test_the_one_session_spawn_still_prepares_its_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        claude_mod, "_resolve_claude_acp_bin", lambda: (["/opt/bin/claude-agent-acp"], "")
    )
    prepared: list[tuple[object, list[str]]] = []

    async def _prepare(session: object, argv: list[str]) -> None:
        prepared.append((session, argv))

    monkeypatch.setattr(claude_mod, "prepare_claude_session", _prepare)
    session = _Session()
    session._install_idle_reader = lambda: None  # type: ignore[attr-defined]
    session._model = ""  # type: ignore[attr-defined]
    ctx = SpawnContext(
        agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path, session=session
    )
    forget_cached_resolution(ACP_BACKEND_CLAUDE)
    try:
        plan = asyncio.run(process_adapter_for(ACP_BACKEND_CLAUDE).resolve_spawn(ctx))
    finally:
        forget_cached_resolution(ACP_BACKEND_CLAUDE)
    assert prepared == [(session, ["/opt/bin/claude-agent-acp"])]
    assert plan.argv == ["/opt/bin/claude-agent-acp"]
