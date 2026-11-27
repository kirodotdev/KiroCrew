"""The reasons a KAS session shows when it cannot start on an agent.

``acp.harness.kas_missing_agent`` builds the line a refused start shows: a kiro-cli
built-in agent names the agent to pick instead, and an agent with no definition names
the Bot it belongs to. Neither may raise, because the reason replaces an error that is
already being raised, so each lookup that can fail falls back to a plainer line.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import members as members_mod
from kiro_crew.acp.harness import kas_missing_agent as reasons
from kiro_crew.apps import manager as manager_mod
from kiro_crew.config import KiroCrewConfig, loader


@pytest.fixture
def config(monkeypatch):
    """A loaded config whose agent rows the test fills in."""
    loaded = SimpleNamespace(agents={})
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: loaded)
    return loaded


class TestKiroCliBuiltinReason:
    def test_names_the_configured_default_and_the_terminal_fix(self, config, monkeypatch):
        monkeypatch.setattr(loader, "default_template", lambda cfg: "my-agent")
        reason = reasons._kiro_cli_builtin_reason("kiro_planner")
        assert 'The agent "kiro_planner" is built into kiro-cli' in reason
        assert 'Pick Kiro Crew\'s default agent "my-agent" instead' in reason
        assert "`kiro-cli --agent kiro_planner`" in reason

    def test_a_default_that_is_itself_a_builtin_is_no_pick(self, config, monkeypatch):
        monkeypatch.setattr(loader, "default_template", lambda cfg: "kiro_default")
        reason = reasons._kiro_cli_builtin_reason("kiro_guide")
        assert f'default agent "{loader.DEFAULT_KIRO_TEMPLATE}"' in reason

    def test_an_unreadable_config_still_names_crews_default(self, monkeypatch):
        def unreadable():
            raise OSError("config.json cannot be read")

        monkeypatch.setattr(KiroCrewConfig, "load", unreadable)
        reason = reasons._kiro_cli_builtin_reason("kiro_default")
        assert f'default agent "{loader.DEFAULT_KIRO_TEMPLATE}"' in reason


class TestMissingAgentReason:
    @pytest.fixture
    def asked(self, monkeypatch):
        """What the app lookup was asked, answered as the member it was given."""
        calls: list[tuple[str, str]] = []

        def missing_agent_reason(agent, *, member=""):
            calls.append((agent, member))
            return f"reason for {agent} as {member or 'no member'}"

        monkeypatch.setattr(manager_mod, "missing_agent_reason", missing_agent_reason)
        return calls

    def test_a_bots_session_names_the_bot_by_its_display_name(self, config, asked, monkeypatch):
        config.agents["helper"] = SimpleNamespace(display_name="Helper Bot")
        monkeypatch.setattr(
            members_mod, "member_identity_for_session", lambda key: {"name": "helper"}
        )
        assert reasons._missing_agent_reason("gone", "dm:helper") == (
            "reason for gone as Helper Bot"
        )
        assert asked == [("gone", "Helper Bot")]

    def test_a_bot_with_no_row_is_named_by_its_identity(self, config, asked, monkeypatch):
        monkeypatch.setattr(
            members_mod, "member_identity_for_session", lambda key: {"name": "helper"}
        )
        assert reasons._missing_agent_reason("gone", "dm:helper") == "reason for gone as helper"

    def test_a_session_that_is_no_bots_names_no_member(self, config, asked, monkeypatch):
        monkeypatch.setattr(members_mod, "member_identity_for_session", lambda key: None)
        assert reasons._missing_agent_reason("gone", "dashboard:chat-1") == (
            "reason for gone as no member"
        )

    def test_a_failed_member_lookup_still_gives_the_reason(self, asked, monkeypatch):
        def lookup_fails(key):
            raise RuntimeError("member bindings unreadable")

        monkeypatch.setattr(members_mod, "member_identity_for_session", lookup_fails)
        assert reasons._missing_agent_reason("gone", "dm:helper") == (
            "reason for gone as no member"
        )

    def test_a_failed_app_lookup_falls_back_to_the_plain_reason(self, monkeypatch):
        def lookup_fails(agent, *, member=""):
            raise OSError("installed apps unreadable")

        monkeypatch.setattr(manager_mod, "missing_agent_reason", lookup_fails)
        monkeypatch.setattr(members_mod, "member_identity_for_session", lambda key: None)
        assert reasons._missing_agent_reason("gone", "dashboard:chat-1") == (
            'The agent "gone" can\'t start: its agent definition is missing.'
        )
