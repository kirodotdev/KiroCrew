# SPDX-License-Identifier: Apache-2.0
"""A chat whose agent, the user's own file, lists no tools says so once.

On KAS an agent with no tools after approved MCP additions runs with no tool at
all. The chat gets one notice row, worded by the dashboard from its token and drawn
with a link to the agent's page under Customize. Only an agent that page can edit
is told about.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_agents
from kiro_crew.dashboard import agent_no_tools_notice as notice
from kiro_crew.dashboard import gateway_notice as gn
from kiro_crew.dashboard.handlers import agents as agent_handlers
from kiro_crew.history import ConversationLog
from kiro_crew.members import DM_SLOT_MODE
from kiro_crew.template_mcp_additions import add_addition, merge_at_spawn


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Path]]:
    """The run's own agents directory, emptied again afterwards, and a crewmates one."""
    agents = agent_handlers.kiro_agents_dir_path()
    agents.mkdir(parents=True, exist_ok=True)
    before = set(agents.iterdir())
    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "_mate_agents_dir_override", lambda: mates)
    yield {"agents": agents, "mates": mates}
    for added in set(agents.iterdir()) - before:
        added.unlink()


def _agent(home: dict[str, Path], name: str, where: str = "agents", **keys: Any) -> None:
    spec = {"name": name, "prompt": f"I am {name}.", **keys}
    (home[where] / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


class _Slot:
    def __init__(self, key: str, agent_kind: str = "template", mode: str = ""):
        self.key = key
        self.agent_kind = agent_kind
        self.mode = mode


class _State:
    def __init__(self) -> None:
        self.conversation_log = ConversationLog()


@pytest.fixture
def surfaced(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, dict]]:
    rows: list[tuple[str, str, str, dict]] = []

    def append_and_surface(state, slot, role, content, cls="", *, meta=None, **_):
        rows.append((slot.key, role, content, dict(meta or {})))
        return {}

    monkeypatch.setattr("kiro_crew.dashboard.state.append_and_surface", append_and_surface)
    return rows


def _chat(key: str) -> None:
    """A chat with a metadata line, as a first turn leaves it."""
    ConversationLog().update_metadata(key, {"title": "probe"})


def _surface(slot: _Slot, key: str, agent: str) -> bool:
    return asyncio.run(notice.surface_agent_no_tools(_State(), slot, key, agent))


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"tools": []},
        {"tools": None},
        {"tools": "shell"},
        {"tools": [""]},
        {"tools": [3]},
        {"tools": "*"},
        {"tools": ["*"]},
        {"tools": ["fs_read"]},
        {"tools": ["@probe"]},
    ],
)
def test_no_tools_is_read_as_the_kas_projection_reads_it(spec: dict[str, Any]) -> None:
    # The notice must fire exactly when the projection sends an empty allowlist.
    assert notice.spec_lists_tools(spec) == (kas_agents._project_tools(spec, "solo") != [])


def test_the_users_own_agent_file_with_no_tools_is_the_one_told_about(
    home: dict[str, Path],
) -> None:
    _agent(home, "bare")
    _agent(home, "empty", tools=[])
    _agent(home, "open", tools=["*"])
    _agent(home, "reader", tools=["fs_read"])

    assert notice.editable_agent_without_tools("bare")
    assert notice.editable_agent_without_tools("empty")
    assert not notice.editable_agent_without_tools("open")
    assert not notice.editable_agent_without_tools("reader")
    assert not notice.editable_agent_without_tools("nobody")


def test_an_empty_tool_list_with_an_approved_mcp_addition_is_not_told_about(
    home: dict[str, Path],
) -> None:
    _agent(home, "empty", tools=[])
    add_addition(
        "empty",
        "probe",
        {"command": "mcp-probe", "args": []},
        approved_by="owner",
        card_id="test-card",
    )

    assert merge_at_spawn("empty", {"tools": []})["tools"] == ["@probe"]
    assert not notice.editable_agent_without_tools("empty")


def test_default_crew_servers_suppress_the_no_tools_notice(
    home: dict[str, Path], monkeypatch: pytest.MonkeyPatch, surfaced
) -> None:
    from kiro_crew.acp.harness.kas import resolve_projected_spec

    monkeypatch.setattr("kiro_crew.agent_state.get_capabilities", lambda agent: None)
    monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: None)
    monkeypatch.setattr("kiro_crew.acp.default_crew_servers._registry_mode", lambda: False)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda server: {"command": "test-mcp", "args": [server]},
    )
    _agent(home, "blank", tools=[""])
    key = "dashboard_chat-default-tools"
    _chat(key)

    projected, _ = resolve_projected_spec(home["agents"], "blank", None)
    assert projected["tools"] == ["", "@kirocrew-core", "@kirocrew-cron"]
    assert not notice.editable_agent_without_tools("blank")
    assert not _surface(_Slot("chat-default-tools"), key, "blank")
    assert surfaced == []
    assert notice.AGENT_NO_TOOLS_NOTICE_FIELD not in ConversationLog().get_metadata(key)


def test_an_agent_the_custom_agents_tab_cannot_edit_is_not_told_about(
    home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file the tab shows read-only, a Bot's file, and a name two files claim:
    # the linked page could change none of them, so none gets the row.
    _agent(home, "packaged")
    _agent(home, "atlas", where="mates")
    _agent(home, "twin")
    (home["agents"] / "other.json").write_text(json.dumps({"name": "twin"}), encoding="utf-8")
    real = agent_handlers.read_only_reason_for_path
    monkeypatch.setattr(
        agent_handlers,
        "read_only_reason_for_path",
        lambda path: "package" if path.name == "packaged.json" else real(path),
    )

    assert not notice.editable_agent_without_tools("packaged")
    assert not notice.editable_agent_without_tools("atlas")
    assert not notice.editable_agent_without_tools("twin")


def test_a_chat_is_told_once_and_again_for_another_such_agent(
    home: dict[str, Path], surfaced
) -> None:
    _agent(home, "bare")
    _agent(home, "second")
    key = "dashboard_chat-7"
    _chat(key)
    slot = _Slot("chat-7")

    for _ in range(2):  # a Reload, a resume or a restart starts a new session
        _surface(slot, key, "bare")

    assert len(surfaced) == 1
    _slot_key, role, content, meta = surfaced[0]
    assert role == "notice"
    assert "bare" in content and "no tools" in content
    # Plain words only: no key name or JSON for the user to type.
    assert '"tools"' not in content and "[" not in content
    assert meta == {
        "kind": notice.AGENT_NO_TOOLS_NOTICE_KIND,
        "notice": gn.GATEWAY_NOTICE_AGENT_NO_TOOLS,
        "notice_args": {"agent": "bare"},
    }
    assert ConversationLog().get_metadata(key)[notice.AGENT_NO_TOOLS_NOTICE_FIELD] == "bare"

    _surface(slot, key, "second")
    assert [row[3]["notice_args"]["agent"] for row in surfaced] == ["bare", "second"]


def test_an_agent_with_tools_a_bot_chat_and_an_unknown_chat_are_told_nothing(
    home: dict[str, Path], surfaced
) -> None:
    _agent(home, "open", tools=["*"])
    _agent(home, "bare")
    _chat("dashboard_chat-8")

    assert not _surface(_Slot("chat-8"), "dashboard_chat-8", "open")
    assert not _surface(_Slot("chat-8", agent_kind="member"), "dashboard_chat-8", "bare")
    # No transcript, so nothing to mark as told: say nothing rather than repeat.
    assert not _surface(_Slot("chat-9"), "dashboard_chat-9", "bare")
    assert not _surface(_Slot("chat-8"), "", "bare")
    assert surfaced == []
    assert notice.AGENT_NO_TOOLS_NOTICE_FIELD not in ConversationLog().get_metadata(
        "dashboard_chat-8"
    )


def test_a_bot_dm_bound_to_a_user_agent_file_is_told_nothing(
    home: dict[str, Path], surfaced
) -> None:
    # A Bot's DM slot is minted with mode=DM_SLOT_MODE and no agent_kind
    # (handlers/members_thread.py); the member projection adds
    # @kirocrew-dashboard to its tools, so it does not run with none.
    _agent(home, "bare")
    key = "dashboard_member-dm"
    _chat(key)

    assert not _surface(_Slot("member-dm", agent_kind="", mode=DM_SLOT_MODE), key, "bare")
    assert surfaced == []
    assert notice.AGENT_NO_TOOLS_NOTICE_FIELD not in ConversationLog().get_metadata(key)
