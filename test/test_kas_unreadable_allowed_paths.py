"""An ``allowedPaths`` entry Kiro Crew cannot read, named on its own on KAS.

Kiro Crew answers a KAS read or write from the spec's ``allowedPaths`` itself, with
every entry it can read (``kas_permissions.crew_answered_paths``). One entry outside
that subset covers nothing, but the others still apply, so the session-start notice
names that entry and its reason, never the whole list as having no effect. The list
stays named whole when it is not a list, when the session answers none of it,
or when it belongs to a tool other than read or write, whose list Kiro Crew never answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import crew_answered_paths, crew_answered_read_paths
from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice
from kiro_crew.platform import allowed_paths
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _lost(settings: dict[str, Any], **extra: Any) -> list[str]:
    spec = {"name": "probe", "prompt": "p", "toolsSettings": settings, **extra}
    return kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)


@pytest.mark.parametrize(
    ("tool", "answered"), [("write", crew_answered_paths), ("read", crew_answered_read_paths)]
)
def test_one_unreadable_entry_is_named_and_the_others_still_apply(tool: str, answered) -> None:
    settings = {tool: {"allowedPaths": ["/work/a/**", "/work/[b]/**", "/work/c/**"]}}
    assert answered(settings) == ["/work/a/**", "/work/c/**"]
    reason = allowed_paths.refusal_reason("/work/[b]/**")
    assert reason == "the [ character"
    lost = _lost(settings)
    assert lost == [f"toolsSettings.{tool}.allowedPaths: /work/[b]/** ({reason})"]
    notice = _spec_keys_notice("probe", lost)
    assert "/work/[b]/** (the [ character)" in notice
    assert "no effect" not in notice and "other entries still apply" in notice


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("/w/{a,b}", "the { character"),
        ("~bob/x", "another account's home"),
        ("$HOME/x", "the $ character"),
        (7, "not a string"),
    ],
)
def test_each_entry_is_named_with_why_it_is_unreadable(entry: object, reason: str) -> None:
    settings = {"write": {"allowedPaths": ["/work/a/**", entry]}}
    shown = entry if isinstance(entry, str) else "7"
    assert _lost(settings) == [f"toolsSettings.write.allowedPaths: {shown} ({reason})"]


def test_a_list_with_no_readable_entry_stays_named_whole() -> None:
    lost = _lost({"write": {"allowedPaths": ["/work/[b]/**"]}})
    assert lost == ["toolsSettings.write.allowedPaths"]
    assert "other entries still apply" not in _spec_keys_notice("probe", lost)


def test_a_value_that_is_not_a_list_stays_named_whole() -> None:
    assert _lost({"write": {"allowedPaths": "/work/a/**"}}) == ["toolsSettings.write.allowedPaths"]


def test_a_list_the_session_answers_none_of_is_never_said_to_keep_its_other_entries() -> None:
    ask = {"rules": [{"capability": "fs_write", "effect": "ask"}]}
    lost = _lost({"write": {"allowedPaths": ["/work/a/**", "/work/[b]/**"]}}, permissions=ask)
    assert "toolsSettings.write.allowedPaths" in lost
    notice = _spec_keys_notice("probe", lost)
    assert "/work/[b]/** (the [ character)" in notice
    assert "other entries still apply" not in notice


def test_a_readable_list_names_nothing() -> None:
    assert _lost({"write": {"allowedPaths": ["/work/a/**", "/work/c/**"]}}) == []


@pytest.mark.parametrize("tool", ["grep", "glob", "some_mcp_tool"])
def test_a_list_kiro_crew_never_answers_stays_named_whole(tool: str) -> None:
    lost = _lost({tool: {"allowedPaths": ["/work/a/**", "/work/[b]/**"]}})
    assert f"toolsSettings.{tool}.allowedPaths" in lost
    assert not any(".allowedPaths: " in key for key in lost)
    notice = _spec_keys_notice("probe", lost)
    assert "no effect" in notice and "other entries still apply" not in notice
