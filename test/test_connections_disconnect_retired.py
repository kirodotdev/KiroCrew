"""Disconnect also removes the sign-in a provider left at its retired endpoint.

The startup migration moves Atlassian's card entry from ``v1/mcp/authv2`` to
``v2/mcp`` and deletes no sign-in file, so the v1 pair stays on disk until the
user presses Disconnect. These pin that Disconnect then removes it through the
same per-key sharer test as the current pair, and keeps it while any other
entry still names the retired URL.
"""

from __future__ import annotations

import contextlib
import pathlib
import types

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import mcp_discovery, mcp_grant
from kiro_crew.connections import ownership, warm
from kiro_crew.dashboard.handlers import mcp as mcp_handlers

_V1 = "https://mcp.atlassian.com/v1/mcp/authv2"
_V2 = "https://mcp.atlassian.com/v2/mcp"
_NOTION = "https://mcp.notion.com/mcp"


@contextlib.asynccontextmanager
async def _no_lock():
    yield


def _wire(monkeypatch: pytest.MonkeyPatch, raw_specs: dict, revoked: list[str]) -> None:
    """Neutralize every side effect except the revoke decision under test."""

    def _revoke(url: str) -> list[str]:
        revoked.append(url)
        return ["token", "registration"]

    def _rebuild(**kwargs: object) -> pathlib.Path:
        wrote_out = kwargs.get("_wrote_out")
        if isinstance(wrote_out, list):
            wrote_out.append(True)
        return pathlib.Path("/dev/null")

    inventory = [
        types.SimpleNamespace(name=name, url=spec["url"])
        for name, spec in raw_specs.get("kirocrew", {}).items()
    ]
    monkeypatch.setattr(mcp_grant, "revoke_local_grant", _revoke)
    monkeypatch.setattr(mcp_grant, "surviving_grant_artifacts", lambda _url: [])
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: list(inventory))
    monkeypatch.setattr(ownership, "spec_census", lambda _dirs=(): (raw_specs, ()))
    monkeypatch.setattr(mcp_handlers, "_get_mcp_lock", _no_lock)
    monkeypatch.setattr(mcp_handlers, "_purge_server_config", lambda _name, *, scopes=None: {})
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", _rebuild)
    monkeypatch.setattr(warm, "rearm_invalidated_provider", lambda _slug: None)


@pytest.mark.asyncio
async def test_disconnect_removes_the_retired_v1_sign_in_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoked: list[str] = []
    _wire(monkeypatch, {"kirocrew": {"atlassian": {"url": _V2}}}, revoked)

    scope = await ownership.remove_provider_entry("atlassian", _V2, ())

    assert sorted(revoked) == sorted([_V2, _V1])
    assert sorted(scope.attempted_urls) == sorted([_V2, _V1])
    assert scope.entry_removed is True


@pytest.mark.asyncio
async def test_disconnect_keeps_the_v1_sign_in_another_entry_still_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user's own spec on v1 still signs in with that pair; only v2 goes."""
    revoked: list[str] = []
    _wire(
        monkeypatch,
        {
            "kirocrew": {"atlassian": {"url": _V2}},
            "agent:mine.json": {"jira-old": {"url": _V1}},
        },
        revoked,
    )

    scope = await ownership.remove_provider_entry("atlassian", _V2, ())

    assert revoked == [_V2]
    assert scope.grant_shared_with == ("jira-old",)


@pytest.mark.asyncio
async def test_another_provider_disconnect_is_not_widened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoked: list[str] = []
    _wire(monkeypatch, {"kirocrew": {"notion": {"url": _NOTION}}}, revoked)

    await ownership.remove_provider_entry("notion", _NOTION, ())

    assert revoked == [_NOTION]
