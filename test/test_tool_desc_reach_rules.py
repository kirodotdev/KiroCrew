"""Tool descriptions state the two reach rules an agent otherwise meets only as a refusal.

The spawn tools' ``cwd`` must sit under ``agent.subagent_cwd_allowed_roots``, so both
descriptions print the configured list. The session-control fence lets a fenced caller
reach only the sessions it manages, so the ``target`` field of session_send,
session_read_message, session_stop and session_close says so.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.config.sections_agent_fields import DEFAULT_CWD_ALLOWED_ROOTS
from kiro_crew.mcp_dashboard_session_tools import _session_tools
from kiro_crew.mcp_tools.spawn_parts import schemas as schemas_mod

_SPAWN_TOOLS = ("spawn_run", "spawn_sub_agents")


def _cwd_descriptions() -> dict[str, str]:
    by_name = {d["name"]: d for d in schemas_mod.schemas()}
    return {t: by_name[t]["inputSchema"]["properties"]["cwd"]["description"] for t in _SPAWN_TOOLS}


def _config_with_roots(monkeypatch: pytest.MonkeyPatch, roots: list[str]) -> None:
    cfg = SimpleNamespace(agent=SimpleNamespace(subagent_cwd_allowed_roots=roots))
    monkeypatch.setattr(schemas_mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg))


def test_both_spawn_cwd_descriptions_name_the_configured_roots(monkeypatch):
    _config_with_roots(monkeypatch, ["~/projects", "/srv/build"])
    for tool, desc in _cwd_descriptions().items():
        assert "~/projects" in desc and "/srv/build" in desc, (tool, desc)
        # The shipped defaults are not in force here, so they must not be printed.
        assert "~/workplaces" not in desc, (tool, desc)


def test_an_empty_roots_list_reads_as_cwd_disabled(monkeypatch):
    _config_with_roots(monkeypatch, [])
    for tool, desc in _cwd_descriptions().items():
        assert "Disabled" in desc, (tool, desc)


def test_an_unreadable_config_names_the_setting_and_no_roots(monkeypatch):
    # The admission gate refuses every cwd when the config cannot be read, so the
    # description must not promise the shipped roots then.
    def _boom(cls):
        raise OSError("config unreadable")

    monkeypatch.setattr(schemas_mod.KiroCrewConfig, "load", classmethod(_boom))
    for tool, desc in _cwd_descriptions().items():
        assert "agent.subagent_cwd_allowed_roots" in desc, (tool, desc)
        for root in DEFAULT_CWD_ALLOWED_ROOTS:
            assert root not in desc, (tool, root, desc)


@pytest.mark.parametrize(
    "tool", ["session_send", "session_read_message", "session_stop", "session_close"]
)
def test_session_target_descriptions_state_the_reach_rule(tool):
    spec = next(t for t in _session_tools() if t.name == tool)
    desc = spec.schema["properties"]["target"]["description"]
    assert "session_handoff" in desc, desc
    assert "not_creator" in desc, desc
    assert "what they manage" in desc, desc
    assert "session_manager_chain" in desc, desc
    assert "created or was handed" not in desc, desc
