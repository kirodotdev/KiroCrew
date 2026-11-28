"""A Bot's own KAS chat on an agent of the user's gains the goal tools.

A Bot bound to a user agent chats on that agent, which seldom declares
``kirocrew-mate``, so its chat had no ``set_goal`` and goals could not be set
anywhere. These tests pin that the KAS projection adds the server for that one
session (never to the file), leaves every other session alone, and that the
Bot's wording follows the mount.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import agent, member_mate_tools
from kiro_crew.acp import kas_member_goal_tools as goal_tools
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

MEMBER_KEY = "dashboard:member-ar-tpm-bot"
ENTRY = {"command": "kirocrew", "args": ["mcp-mate"]}


def _user_spec() -> dict:
    return {
        "name": "ar-tpm",
        "tools": ["read", "@asana"],
        "allowedTools": ["read"],
        "mcpServers": {"asana": {"command": "asana-mcp"}},
    }


@pytest.fixture(autouse=True)
def _managed_entry(_floor_monkeypatch):
    _floor_monkeypatch.setattr(agent, "managed_mcp_spec_entry", lambda name, **_k: dict(ENTRY))


def test_a_bots_own_chat_on_a_user_agent_gains_the_goal_tools() -> None:
    spec = _user_spec()
    before = json.dumps(spec, sort_keys=True)
    out = goal_tools.with_member_goal_tools(spec, "ar-tpm", MEMBER_KEY)
    assert json.dumps(spec, sort_keys=True) == before, "the caller's spec is never written"
    assert out["mcpServers"]["kirocrew-mate"] == ENTRY
    assert out["mcpServers"]["asana"] == {"command": "asana-mcp"}
    assert out["tools"] == ["read", "@asana", "@kirocrew-mate"]
    assert "@kirocrew-mate/set_goal" in out["allowedTools"]
    assert set(agent._MATE_AUTO_GRANTS) <= set(out["allowedTools"])
    projected = to_client_custom_agent("ar-tpm", out, "", session_key=MEMBER_KEY)
    assert "@kirocrew-mate" in projected["tools"]
    assert "kirocrew-mate" in json.dumps(projected.get("mcpServers"))


@pytest.mark.parametrize(
    ("spec_change", "agent_id", "key"),
    [
        ({}, "ar-tpm", "subagent:default-worker"),
        ({}, "ar-tpm", "dashboard:chat-12"),
        ({}, MATE_CONDUCTOR_AGENT, MEMBER_KEY),
        (
            {"mcpServers": {"kirocrew-mate": {**ENTRY, "disabled": True}}},
            "ar-tpm",
            MEMBER_KEY,
        ),
        (
            {"tools": ["@kirocrew-mate"], "mcpServers": {"kirocrew-mate": dict(ENTRY)}},
            "ar-tpm",
            MEMBER_KEY,
        ),
    ],
    ids=["worker", "plain-chat", "conductor", "user-muted-it", "already-mounted"],
)
def test_every_other_session_is_left_as_it_is(spec_change, agent_id, key) -> None:
    spec = {**_user_spec(), **spec_change}
    assert goal_tools.with_member_goal_tools(spec, agent_id, key) is spec


def test_a_tools_wildcard_gains_only_the_server() -> None:
    spec = {**_user_spec(), "tools": ["*"]}
    out = goal_tools.with_member_goal_tools(spec, "ar-tpm", MEMBER_KEY)
    assert out["tools"] == ["*"]
    assert out["mcpServers"]["kirocrew-mate"] == ENTRY


@pytest.mark.parametrize(
    ("backend", "expected"), [("kas", True), ("claude", True), ("codex", True), ("", False)]
)
def test_the_wording_follows_the_mount(tmp_path, monkeypatch, backend, expected) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "ar-tpm.json").write_text(json.dumps(_user_spec()), encoding="utf-8")
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents_dir)
    monkeypatch.setattr(
        "kiro_crew.mate_worker.crewmate_worker_backend", lambda _cfg, _member=None: backend
    )
    binding = SimpleNamespace(kiro_agent="ar-tpm", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(binding, "ar-tpm-bot") is expected
    # Without the crewmate's name there is no backend to read: the spec decides.
    assert member_mate_tools.binding_has_mate_tools(binding) is False


@pytest.mark.parametrize("backend", ["kas", "claude", "codex"])
def test_a_spec_that_excludes_the_server_gets_the_no_tools_wording(
    tmp_path, monkeypatch, backend
) -> None:
    """The mount withholds an excluded server, so the wording must not name its tools."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    spec = {**_user_spec(), "excludedTools": ["@kirocrew-mate"]}
    (agents_dir / "ar-tpm.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents_dir)
    monkeypatch.setattr(
        "kiro_crew.mate_worker.crewmate_worker_backend", lambda _cfg, _member=None: backend
    )
    binding = SimpleNamespace(kiro_agent="ar-tpm", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(binding, "ar-tpm-bot") is False


def test_a_spec_without_an_allowlist_keeps_its_own_permission_grants() -> None:
    """No ``allowedTools`` is written: one would make the projection drop the
    author's ``permissions`` allows (``merge_user_permissions``)."""
    spec = {k: v for k, v in _user_spec().items() if k != "allowedTools"}
    spec["permissions"] = [{"capability": "mcp", "match": ["asana/*"], "effect": "allow"}]
    out = goal_tools.with_member_goal_tools(spec, "ar-tpm", MEMBER_KEY)
    assert "allowedTools" not in out
    assert out["permissions"] == spec["permissions"]
    assert "@kirocrew-mate" in out["tools"]
