"""The reasons a KAS session shows when it cannot start on an agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kiro_crew.apps.manager as apps_manager
import kiro_crew.config as config_pkg
import kiro_crew.config.loader_snapshots as snapshots
import kiro_crew.members as members
from kiro_crew.acp.harness import kas
from kiro_crew.acp.harness.kas_missing_agent import (
    _kiro_cli_builtin_reason,
    _missing_agent_reason,
)


def _config(agents: dict | None = None, default_agent: str = "") -> SimpleNamespace:
    return SimpleNamespace(agents=agents or {}, agent=SimpleNamespace(default_agent=default_agent))


def _raise(*_args, **_kwargs):
    raise RuntimeError("unreadable")


def test_kas_keeps_both_names_as_its_import_path() -> None:
    assert kas._kiro_cli_builtin_reason is _kiro_cli_builtin_reason
    assert kas._missing_agent_reason is _missing_agent_reason


def test_the_builtin_reason_names_the_configured_default(monkeypatch) -> None:
    monkeypatch.setattr(
        config_pkg.KiroCrewConfig,
        "load",
        classmethod(lambda cls: _config(default_agent="my-agent")),
    )

    reason = _kiro_cli_builtin_reason("kiro_planner")

    assert 'The agent "kiro_planner" is built into kiro-cli' in reason
    assert 'default agent "my-agent"' in reason
    assert "`kiro-cli --agent kiro_planner`" in reason


def test_an_unreadable_config_names_the_shipped_default(monkeypatch) -> None:
    monkeypatch.setattr(config_pkg.KiroCrewConfig, "load", classmethod(_raise))

    reason = _kiro_cli_builtin_reason("kiro_default")

    assert f'default agent "{snapshots.DEFAULT_KIRO_TEMPLATE}"' in reason


@pytest.mark.parametrize("builtin", sorted(snapshots.KIRO_CLI_BUILTIN_AGENTS))
def test_a_builtin_default_is_never_offered_as_the_pick(monkeypatch, builtin) -> None:
    monkeypatch.setattr(
        config_pkg.KiroCrewConfig,
        "load",
        classmethod(lambda cls: _config(default_agent=builtin)),
    )

    reason = _kiro_cli_builtin_reason("kiro_guide")

    assert f'default agent "{snapshots.DEFAULT_KIRO_TEMPLATE}"' in reason


def test_a_member_session_names_the_member_by_display_name(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(members, "member_identity_for_session", lambda key: {"name": "slackbot"})
    monkeypatch.setattr(
        config_pkg.KiroCrewConfig,
        "load",
        classmethod(lambda cls: _config({"slackbot": SimpleNamespace(display_name="Slack")})),
    )

    def _reason(agent, *, member=""):
        seen["args"] = (agent, member)
        return "owner reason"

    monkeypatch.setattr(apps_manager, "missing_agent_reason", _reason)

    assert _missing_agent_reason("gone", "member-key") == "owner reason"
    assert seen["args"] == ("gone", "Slack")


def test_a_member_without_a_config_row_is_named_by_its_name(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(members, "member_identity_for_session", lambda key: {"name": "slackbot"})
    monkeypatch.setattr(config_pkg.KiroCrewConfig, "load", classmethod(lambda cls: _config()))
    monkeypatch.setattr(
        apps_manager,
        "missing_agent_reason",
        lambda agent, *, member="": seen.setdefault("member", member),
    )

    _missing_agent_reason("gone", "member-key")

    assert seen["member"] == "slackbot"


def test_a_failed_member_lookup_still_gives_the_owner_reason(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(members, "member_identity_for_session", _raise)
    monkeypatch.setattr(
        apps_manager,
        "missing_agent_reason",
        lambda agent, *, member="": seen.setdefault("member", member) or "owner reason",
    )

    assert _missing_agent_reason("gone", "plain-key") == "owner reason"
    assert seen["member"] == ""


def test_a_failed_owner_lookup_falls_back_to_the_plain_reason(monkeypatch) -> None:
    monkeypatch.setattr(members, "member_identity_for_session", lambda key: None)
    monkeypatch.setattr(apps_manager, "missing_agent_reason", _raise)

    reason = _missing_agent_reason("gone", "plain-key")

    assert reason == 'The agent "gone" can\'t start: its agent definition is missing.'
