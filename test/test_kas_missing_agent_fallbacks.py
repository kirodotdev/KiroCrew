"""The refusal reasons in ``kas_missing_agent`` never raise, whatever their lookups do.

Each reason replaces an error that is already being raised, so a failed config
read, member lookup or app lookup falls back to a plainer sentence instead.
"""

from __future__ import annotations

import pytest

from kiro_crew import members as members_mod
from kiro_crew.acp.harness.kas_missing_agent import _kiro_cli_builtin_reason, _missing_agent_reason
from kiro_crew.apps import manager as manager_mod
from kiro_crew.config import KiroCrewConfig, loader, loader_snapshots


def _raise(*_a, **_k):
    raise RuntimeError("boom")


def test_builtin_reason_names_the_shipped_default_when_config_cannot_load(monkeypatch):
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_raise))
    reason = _kiro_cli_builtin_reason("kiro_planner")
    assert '"kiro_planner" is built into kiro-cli' in reason
    assert f'default agent "{loader_snapshots.DEFAULT_KIRO_TEMPLATE}"' in reason
    assert "`kiro-cli --agent kiro_planner`" in reason


def test_builtin_reason_never_offers_a_builtin_as_the_pick(monkeypatch):
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: object()))
    # Through the facade, which mirrors the patch to every module that binds it.
    monkeypatch.setattr(loader, "default_template", lambda cfg: "kiro_default")
    reason = _kiro_cli_builtin_reason("kiro_guide")
    assert f'default agent "{loader_snapshots.DEFAULT_KIRO_TEMPLATE}"' in reason


def test_missing_agent_reason_drops_the_member_label_when_the_lookup_fails(monkeypatch):
    monkeypatch.setattr(members_mod, "member_identity_for_session", _raise)
    seen: dict[str, str] = {}

    def _reason(agent: str, *, member: str) -> str:
        seen["member"] = member
        return f"reason for {agent}"

    monkeypatch.setattr(manager_mod, "missing_agent_reason", _reason)
    assert _missing_agent_reason("gone", "chat-1") == "reason for gone"
    assert seen == {"member": ""}


@pytest.mark.parametrize("identity", [None, {"slug": "ada", "name": "ada"}])
def test_missing_agent_reason_falls_back_when_the_app_lookup_fails(monkeypatch, identity):
    monkeypatch.setattr(members_mod, "member_identity_for_session", lambda key: identity)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_raise))
    monkeypatch.setattr(manager_mod, "missing_agent_reason", _raise)
    assert _missing_agent_reason("gone", "chat-1") == (
        'The agent "gone" can\'t start: its agent definition is missing.'
    )
