"""Tests for GET /api/slash-commands descriptions.

Pins the contract that every dashboard slash command carries a non-empty,
human-readable description sourced from the single SLASH_COMMAND_DESCRIPTIONS
map (no blank descriptions in the autocomplete menu), and guards that the map
stays in sync with _SLASH_COMMANDS.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
)


def _fake_config(provider: str):
    return SimpleNamespace(agent=SimpleNamespace(provider=provider))


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_slash_commands

    app = web.Application()
    app.router.add_get("/api/slash-commands", api_slash_commands)
    return app


async def _get(provider: str):
    with patch(
        "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
        return_value=_fake_config(provider),
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
        (so the menu offered it) but rejected at execution time."""
        payload = await _get("kiro")
        names = {item["name"] for item in payload}
        leaked = names & _BLOCKED_SLASH_COMMANDS
        assert not leaked, f"blocked commands advertised in menu: {sorted(leaked)}"
        assert "/tangent" not in names

    @pytest.mark.asyncio
    async def test_claude_code_provider_filters_blocked_commands(self):
        """The provider-reported path applies the same gate: a harness that
        reports a blocked command must not have it forwarded to the menu."""
        provider = SimpleNamespace(_slash_commands=["compact", "tangent", "quit", "help"])
        state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: [provider]))
        with patch(
            "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
            return_value=_fake_config("claude_code"),
        ):
            app = _make_app()
            app["state"] = state
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/slash-commands")
                assert resp.status == 200
                payload = await resp.json()
        names = {item["name"] for item in payload}
        assert "/compact" in names and "/help" in names and "/side" in names
        assert "/tangent" not in names
        assert "/quit" not in names
