"""Tests for GET /api/slash-commands descriptions.

Pins the contract that every dashboard slash command carries a non-empty,
human-readable description sourced from the single SLASH_COMMAND_DESCRIPTIONS
map (no blank descriptions in the autocomplete menu), and guards that the map
stays in sync with _SLASH_COMMANDS.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp.client import AcpClient
from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
)
from kiro_crew.providers.acp import AcpProvider


@pytest.fixture(autouse=True)
def prompts_dir(tmp_path, _floor_monkeypatch):
    """A private ``~/.kiro/prompts``: the menu lists the user's opted-in prompts."""
    home = tmp_path / "home"
    _floor_monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    path = home / ".kiro" / "prompts"
    path.mkdir(parents=True)
    return path


def _fake_config(provider: str, acp_backend: str = ""):
    return SimpleNamespace(agent=SimpleNamespace(provider=provider, acp_backend=acp_backend))


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_slash_commands

    app = web.Application()
    app.router.add_get("/api/slash-commands", api_slash_commands)
    return app


async def _get(provider: str, acp_backend: str = ""):
    with patch(
        "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
        return_value=_fake_config(provider, acp_backend),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/slash-commands")
            assert resp.status == 200
            return await resp.json()


def test_description_map_covers_slash_commands():
    """DRY guard: a command added to _SLASH_COMMANDS without a description
    (or vice-versa) fails the build here rather than shipping a blank menu row."""
    missing = set(_SLASH_COMMANDS) - set(SLASH_COMMAND_DESCRIPTIONS)
    assert not missing, f"commands missing a description: {sorted(missing)}"
    for name, desc in SLASH_COMMAND_DESCRIPTIONS.items():
        assert desc.strip(), f"empty description for {name}"


class TestApiSlashCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", ["", "claude"])
    async def test_user_quick_prompt_is_listed_on_every_harness(self, prompts_dir, backend):
        (prompts_dir / "cr.md").write_text(
            "---\ndescription: Review a change\nslash_command: true\n---\nReview {arg}.\n",
            encoding="utf-8",
        )

        payload = await _get("acp", backend)

        assert {"name": "/cr", "description": "Review a change"} in payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend,supported", [("", False), ("claude", True)])
    async def test_acp_todos_suggestion_uses_default_backend_capability(self, backend, supported):
        payload = await _get("acp", backend)
        names = {item["name"] for item in payload}

        assert ("/todos" in names) is supported
        assert not names & (_BLOCKED_SLASH_COMMANDS - {"/todos"})

    @pytest.mark.asyncio
    async def test_default_provider_returns_described_commands(self):
        payload = await _get("kiro")
        by_name = {item["name"]: item["description"] for item in payload}

        # Default path returns the _SLASH_COMMANDS set minus the blocked
        # commands: a blocked command only ever produces a rejection message,
        # so advertising it in the autocomplete would be an inert suggestion.
        assert set(by_name) == set(_SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS)
        # Every command has a non-empty description matching the shared map.
        for name, desc in by_name.items():
            assert desc, f"blank description for {name}"
            assert desc == SLASH_COMMAND_DESCRIPTIONS[name]
        # KiroCrew-local commands read meaningfully.
        assert "side" in by_name["/side"].lower()

    @pytest.mark.asyncio
    async def test_blocked_commands_absent_from_suggestions(self):
        """Regression guard: no blocked command may appear in the suggestion
        payload. /tangent regressed this way once — present in _SLASH_COMMANDS
        (so the menu offered it) but rejected at execution time. /todos is also
        blocked because the kiro-cli ACP harness does not implement it."""
        payload = await _get("kiro")
        names = {item["name"] for item in payload}
        leaked = names & _BLOCKED_SLASH_COMMANDS
        assert not leaked, f"blocked commands advertised in menu: {sorted(leaked)}"
        assert "/tangent" not in names
        assert "/todos" not in names

    @pytest.mark.asyncio
    async def test_kiro_issue_is_not_advertised(self):
        payload = await _get("kiro")
        names = {item["name"] for item in payload}
        assert "/issue" not in names

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend,supported", [("", False), ("claude", True)])
    async def test_claude_code_provider_filters_blocked_commands(self, backend, supported):
        """The claude list applies the same gate: /todos only where it is served."""
        payload = await _get("claude_code", backend)
        names = {item["name"] for item in payload}
        assert "/compact" in names and "/help" in names and "/side" in names
        assert not names & (_BLOCKED_SLASH_COMMANDS - {"/todos"})
        assert ("/todos" in names) is supported

    @pytest.mark.asyncio
    async def test_claude_backend_lists_claude_commands_not_kiro_cli_ones(self):
        """acp_backend claude runs its own commands, so the menu offers them.

        The public schema only admits provider "acp"; the backend decides.
        """
        payload = await _get("acp", "claude")
        by_name = {item["name"]: item["description"] for item in payload}

        assert {"/init", "/review", "/security-review"} <= set(by_name)
        assert not set(by_name) & {
            "/hooks",
            "/code",
            "/experiment",
            "/logdump",
            "/changelog",
            "/tools",
        }
        # The commands the dashboard answers itself stay offered on claude.
        assert {"/agent", "/goal", "/prompts", "/side", "/workflow"} <= set(by_name)
        assert not set(by_name) & (_BLOCKED_SLASH_COMMANDS - {"/todos"})
        for name, desc in by_name.items():
            assert desc == SLASH_COMMAND_DESCRIPTIONS[name], name
        assert len(payload) == len(by_name)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", ["", "kiro", "kas", "codex"])
    async def test_non_member_backends_keep_the_kiro_cli_menu(self, backend):
        """Only a backend that runs its own commands gets the claude list."""
        names = {item["name"] for item in await _get("acp", backend)}
        assert not names & {"/init", "/review", "/security-review"}

    @pytest.mark.asyncio
    async def test_codex_lists_only_the_commands_that_work_there(self):
        """codex-acp runs /compact and /mcp from a prompt and sends every other
        kiro-cli word to the model, so the menu offers those two, /clear and the
        commands the dashboard answers itself."""
        payload = await _get("acp", "codex")
        by_name = {item["name"]: item["description"] for item in payload}

        assert {"/compact", "/clear", "/mcp"} <= set(by_name)
        assert {"/agent", "/goal", "/prompts", "/side", "/workflow"} <= set(by_name)
        assert not set(by_name) & {
            "/tools",
            "/hooks",
            "/code",
            "/experiment",
            "/logdump",
            "/changelog",
            "/help",
            "/context",
            "/usage",
            "/model",
        }
        for name, desc in by_name.items():
            assert desc == SLASH_COMMAND_DESCRIPTIONS[name], name


# The frame claude-agent-acp 0.94.0 sends right after ``session/new`` (a live capture
# in a throwaway project holding one command and one skill), cut to five of its rows:
# the project command and skill, and three built-ins.
_ADVERTISED = {
    "jsonrpc": "2.0",
    "method": "session/update",
    "params": {
        "sessionId": "claude-session-1",
        "update": {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [
                {
                    "name": "release-notes-draft",
                    "description": "Draft a two-line release note from a change summary (project)",
                    "input": None,
                },
                {
                    "name": "greet-team",
                    "description": "Say hello to the team in one line (project)",
                    "input": {"hint": "team name"},
                },
                {
                    "name": "compact",
                    "description": "Free up context by summarizing the conversation so far",
                    "input": {"hint": "<optional custom summarization instructions>"},
                },
                {"name": "goal", "description": "Set a goal", "input": None},
                {"name": "mcp", "description": "Show the MCP servers", "input": None},
            ],
        },
    },
}


async def _provider_after_frame(backend: str = "claude", frame: dict = _ADVERTISED) -> AcpProvider:
    """An ``AcpProvider`` whose client read *frame* off its stdout."""
    client = AcpClient(acp_backend=backend)
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(frame) + "\n").encode())
    client._process = SimpleNamespace(stdout=reader, returncode=None)
    client._session_id = "claude-session-1"
    assert await client._read_message() is not None
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client
    return provider


async def _get_for_slot(
    provider: AcpProvider, query: str = "?slot=s1", app: str = "", default: str = "claude"
):
    """GET the menu under a *default* backend, with one live slot ``s1`` whose session
    runs *provider*."""
    asked: list[str] = []

    def get_provider(key: str):
        asked.append(key)
        return provider

    async def is_provider_alive(key: str) -> bool:
        return True

    @web.middleware
    async def as_app(request, handler):
        if app:
            request["app"] = app
        return await handler(request)

    application = _make_app()
    application.middlewares.append(as_app)
    application["state"] = SimpleNamespace(
        _slots={"s1": SimpleNamespace(key="s1", linked_session_key="", _app="")},
        sessions=SimpleNamespace(get_provider=get_provider, is_provider_alive=is_provider_alive),
    )
    with patch(
        "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
        return_value=_fake_config("acp", default),
    ):
        async with TestClient(TestServer(application)) as client:
            resp = await client.get("/api/slash-commands" + query)
            assert resp.status == 200
            return await resp.json(), asked


class TestSessionAdvertisedCommands:
    @pytest.mark.asyncio
    async def test_a_claude_chat_lists_what_its_session_advertised(self):
        payload, asked = await _get_for_slot(await _provider_after_frame())
        by_name = {item["name"]: item["description"] for item in payload}

        assert set(asked) == {"dashboard:s1"}
        assert by_name["/greet-team"] == "Say hello to the team in one line (project)"
        assert "/release-notes-draft" in by_name
        # A name the dashboard lists or answers first keeps the dashboard's row.
        assert by_name["/compact"] == SLASH_COMMAND_DESCRIPTIONS["/compact"]
        assert by_name["/goal"] == SLASH_COMMAND_DESCRIPTIONS["/goal"]
        assert "/mcp" not in by_name
        assert len(payload) == len(by_name)

    @pytest.mark.asyncio
    async def test_an_advertised_description_reaches_the_menu_scrubbed(self):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        frame = copy.deepcopy(_ADVERTISED)
        frame["params"]["update"]["availableCommands"][1]["description"] = f"Post with {token}"
        payload, _ = await _get_for_slot(await _provider_after_frame(frame=frame))
        description = {item["name"]: item["description"] for item in payload}["/greet-team"]

        assert token not in description
        assert description.startswith("Post with ")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["no slot", "replaced session", "foreign app", "kiro"])
    async def test_the_list_stays_with_its_own_session(self, case):
        provider = await _provider_after_frame("" if case == "kiro" else "claude")
        if case == "replaced session":
            provider._client._session_id = "claude-session-2"
        payload, _ = await _get_for_slot(
            provider,
            query="" if case == "no slot" else "?slot=s1",
            app="other-app" if case == "foreign app" else "",
        )

        assert not {item["name"] for item in payload} & {"/greet-team", "/release-notes-draft"}


class TestMenuFollowsTheChatsBackend:
    """The base list is the one for the backend the slash gate resolves for the chat."""

    _KAS_ONLY = {"/help", "/tools", "/context"}

    @pytest.mark.asyncio
    async def test_a_codex_chat_under_a_kas_default_lists_the_codex_menu(self):
        payload, _ = await _get_for_slot(await _provider_after_frame("codex"), default="kas")
        names = {item["name"] for item in payload}

        assert {"/compact", "/mcp", "/clear"} <= names
        assert not names & self._KAS_ONLY

    @pytest.mark.asyncio
    async def test_a_kas_chat_under_a_codex_default_keeps_the_kas_menu(self):
        payload, _ = await _get_for_slot(await _provider_after_frame("kas"), default="codex")

        assert self._KAS_ONLY <= {item["name"] for item in payload}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["no slot", "foreign app"])
    async def test_a_chat_the_caller_cannot_read_gets_the_default_menu(self, case):
        payload, asked = await _get_for_slot(
            await _provider_after_frame("kas"),
            query="" if case == "no slot" else "?slot=s1",
            app="other-app" if case == "foreign app" else "",
            default="codex",
        )

        assert not {item["name"] for item in payload} & self._KAS_ONLY
        assert asked == []
