"""The reasons a KAS session cannot start on an agent, as the user reads them.

Each reason names what to do next, and none of its lookups (config, the Member's
name, the app that declares the agent) may raise: the reason replaces an error that
is already being raised.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import members
from kiro_crew.acp.harness import kas_missing_agent as kma
from kiro_crew.apps import manager
from kiro_crew.config import KiroCrewConfig, loader_snapshots


def _config(agents: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(agents=agents or {})


def _broken_load() -> object:
    raise RuntimeError("config unreadable")


def test_a_builtin_agent_names_the_configured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_config))
    monkeypatch.setattr(loader_snapshots, "default_template", lambda _c: "crew-default")
    reason = kma._kiro_cli_builtin_reason("kiro_planner")
    assert '"kiro_planner" is built into kiro-cli' in reason
    assert '"crew-default"' in reason
    assert "`kiro-cli --agent kiro_planner`" in reason


def test_an_unreadable_config_falls_back_to_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_broken_load))
    reason = kma._kiro_cli_builtin_reason("kiro_default")
    assert f'"{loader_snapshots.DEFAULT_KIRO_TEMPLATE}"' in reason


def test_a_default_that_is_itself_builtin_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    builtin = sorted(loader_snapshots.KIRO_CLI_BUILTIN_AGENTS)[0]
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_config))
    monkeypatch.setattr(loader_snapshots, "default_template", lambda _c: builtin)
    reason = kma._kiro_cli_builtin_reason("kiro_guide")
    assert f'"{loader_snapshots.DEFAULT_KIRO_TEMPLATE}" instead' in reason


def test_a_missing_agent_in_a_member_thread_names_the_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def reason(agent: str, *, member: str = "") -> str:
        seen.update(agent=agent, member=member)
        return "the app reason"

    row = SimpleNamespace(display_name="Atlas")
    monkeypatch.setattr(members, "member_identity_for_session", lambda _k: {"name": "atlas"})
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: _config({"atlas": row})))
    monkeypatch.setattr(manager, "missing_agent_reason", reason)
    assert kma._missing_agent_reason("helper", "member-dm:atlas") == "the app reason"
    assert seen == {"agent": "helper", "member": "Atlas"}


def test_a_failed_member_lookup_still_gives_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def identity(_key: str) -> dict[str, str]:
        raise RuntimeError("bindings unreadable")

    def reason(agent: str, *, member: str = "") -> str:
        seen["member"] = member
        return "the app reason"

    monkeypatch.setattr(members, "member_identity_for_session", identity)
    monkeypatch.setattr(manager, "missing_agent_reason", reason)
    assert kma._missing_agent_reason("helper", "member-dm:atlas") == "the app reason"
    assert seen == {"member": ""}


def test_a_failed_app_lookup_falls_back_to_the_plain_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reason(_agent: str, *, member: str = "") -> str:
        raise OSError("apps unreadable")

    monkeypatch.setattr(members, "member_identity_for_session", lambda _k: None)
    monkeypatch.setattr(manager, "missing_agent_reason", reason)
    assert kma._missing_agent_reason("helper", "chat-1") == (
        'The agent "helper" can\'t start: its agent definition is missing.'
    )
