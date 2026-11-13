"""A code-channel tool acts for the session that called it, under ITS Slack policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    SESSION,
    _bind,
    _calls,
    _run,
    _tool_rows,
    env_fixture,
)

from kiro_crew.platform import governance_profiles
from kiro_crew.slack.code_channels import tools


@pytest.fixture(name="denied")
def denied_fixture(monkeypatch: Any) -> list[tuple[str, str, str]]:
    """Profiles where only the calling session (SESSION) is denied Slack."""
    asked: list[tuple[str, str, str]] = []

    def permits(scope: str, item: str, *, session_key: str = "", **_k: Any) -> Any:
        asked.append((scope, item, session_key))
        deny = scope == "channels" and item == "slack" and session_key == SESSION
        return SimpleNamespace(permitted=not deny, layer="profile", rule="r", reason="test")

    monkeypatch.setattr(governance_profiles, "governance_permits", permits)
    return asked


@pytest.mark.parametrize("tool", sorted(tools.TOOL_NAMES))
def test_a_caller_whose_profile_denies_slack_is_refused_every_tool(
    env: Any, denied: list[tuple[str, str, str]], tool: str
) -> None:
    _bind(env)
    out = _run(env, tool, {})
    assert out.code == "channels_policy"
    assert env.api.calls == []
    assert ("channels", "slack", SESSION) in denied
    rows = _tool_rows(env, "denied")
    assert rows and "calling session" in rows[-1]["error"]


def test_a_caller_whose_profile_allows_slack_still_works(env: Any, monkeypatch: Any) -> None:
    asked: list[str] = []

    def permits(scope: str, item: str, *, session_key: str = "", **_k: Any) -> Any:
        asked.append(session_key)
        return SimpleNamespace(permitted=True, layer="profile", rule="", reason="")

    monkeypatch.setattr(governance_profiles, "governance_permits", permits)
    _bind(env)
    out = _run(env, tools.RENAME, {"title": "Fix the login"})
    assert out.ok, out.text
    assert SESSION in asked
    assert _calls(env, "agents.sessions.rename")


def test_an_unreadable_caller_policy_refuses(env: Any, monkeypatch: Any) -> None:
    def permits(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("profile store unreadable")

    monkeypatch.setattr(governance_profiles, "governance_permits", permits)
    _bind(env)
    assert _run(env, tools.RENAME, {"title": "x"}).code == "channels_policy"
    assert env.api.calls == []
