"""The argument hint kept from an ``available_commands_update``, and the Claude agent
left unread.

claude-agent-acp sends each command as ``{name, description, input}``, where ``input``
is ``{hint}`` (the argument syntax) or null. The transport keeps the hint beside the
name and description for the composer's ``/`` menu, scrubbed and bounded where it is
stored (``client_transport.bounded_commands``).

The same adapter advertises an ``agent`` select in ``configOptions`` (the main-thread
persona) whenever the host has a custom agent. Crew neither reads nor writes it: a
switch can carry a permission mode, MCP servers and hooks Crew would not see
(``claude-code-provider.md``, "Claude Code custom agents").
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_transport import ADVERTISED_COMMAND_HINT_MAX_CHARS, bounded_commands
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX
from kiro_crew.dashboard.chat import api_chat_slot_selection_capabilities
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider

_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _command(name: str, description: object = "Do it", command_input: object = None) -> dict:
    return {"name": name, "description": description, "input": command_input}


def _hint(command_input: object) -> str:
    return bounded_commands("s", [_command("loop", command_input=command_input)])[0].get("hint", "")


def test_a_hint_is_kept_scrubbed_and_cut():
    assert _hint({"hint": " [interval] [prompt] "}) == "[interval] [prompt]"
    assert _hint({"hint": "x" * 200}) == "x" * ADVERTISED_COMMAND_HINT_MAX_CHARS
    secret = _hint({"hint": f"--token {_TOKEN}"})
    assert _TOKEN not in secret and secret.startswith("--token ")
    for no_hint in (None, {}, {"hint": 5}, ["a"], "[x]"):
        assert _hint(no_hint) == "", no_hint


def _frame(commands: list[dict]) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "claude-session-1",
            "update": {"sessionUpdate": "available_commands_update", "availableCommands": commands},
        },
    }


async def _read_frames(backend: str, frames: list[dict]) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    reader = asyncio.StreamReader()
    reader.feed_data(b"".join((json.dumps(frame) + "\n").encode() for frame in frames))
    client._process = SimpleNamespace(stdout=reader, returncode=None)
    client._session_id = "claude-session-1"
    for _ in frames:
        assert await client._read_message() is not None
    return client


@pytest.mark.asyncio
async def test_the_read_path_keeps_the_hint():
    frame = _frame([_command("loop", command_input={"hint": "[interval] [prompt]"}), _command("x")])
    client = await _read_frames(ACP_BACKEND_CLAUDE, [frame])
    assert [(row["name"], row.get("hint")) for row in client.advertised_commands()] == [
        ("loop", "[interval] [prompt]"),
        ("x", None),
    ]
    other = await _read_frames(ACP_BACKEND_CODEX, [_frame([_command("loop", "", {"hint": "x"})])])
    assert other.advertised_commands() == []


#: Every key ``selection-capabilities`` may answer for a live session.
_CAPABILITY_KEYS = {
    "known",
    "backend",
    "effort_supported",
    "effort_levels",
    "model_effort_pair_ids",
    "models_backend",
    "ultracode",
    "fast",
    "fast_unavailable",
}
#: The ``agent`` select claude-agent-acp builds when the host has custom agents
#: (``session-config-options.js``), with two synthetic agents.
_AGENT_OPTION = {
    "id": "agent",
    "name": "Agent",
    "description": "Main-thread agent persona",
    "type": "select",
    "currentValue": "default",
    "options": [
        {"value": "default", "name": "Default", "description": "Standard Claude Code agent"},
        {"value": "reviewer", "name": "reviewer", "description": "Reviews a change"},
        {"value": "planner", "name": "planner", "description": "Plans a change"},
    ],
}


def _config_ids_crew_writes() -> set[str]:
    """Each ``set_config_option`` id in the package that is a literal or a module name."""
    root = Path(__file__).resolve().parents[1] / "src"
    ids: set[str] = set()
    for path in (root / "kiro_crew").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "set_config_option(" not in source:
            continue
        module = None
        for node in ast.walk(ast.parse(source)):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", "")) == "set_config_option"
                and node.args
            ):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                ids.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id.isupper():
                dotted = ".".join(path.relative_to(root).with_suffix("").parts)
                module = module or importlib.import_module(dotted)
                ids.add(getattr(module, arg.id))
    return ids


@pytest.mark.asyncio
async def test_crew_neither_offers_nor_selects_a_claude_main_thread_agent():
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE)
    provider._client = MagicMock(backend=ACP_BACKEND_CLAUDE, acp_config_options=[_AGENT_OPTION])
    slot = _ChatSlot("test")
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock(get_provider=MagicMock(return_value=provider))
    app = web.Application()
    app["state"] = state
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    with (
        patch.object(AcpProvider, "supports_effort", return_value=False),
        patch("kiro_crew.dashboard.chat_handlers._own_models_field", AsyncMock(return_value={})),
    ):
        async with TestClient(TestServer(app)) as client:
            caps = await (await client.get("/api/chat/slots/test/selection-capabilities")).json()
    assert caps["known"] is True
    assert set(caps) <= _CAPABILITY_KEYS
    provider._client.set_config_option.assert_not_called()

    session = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    for zero_tools in (False, True):
        session._spec_zero_tools = zero_tools
        assert "agent" not in session._claude_session_meta()["claudeCode"]["options"]
    writes = _config_ids_crew_writes()
    assert {"model", "ultracode", "fast"} <= writes
    assert "agent" not in writes
