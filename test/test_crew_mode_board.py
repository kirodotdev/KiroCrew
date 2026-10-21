"""Crew Mode's task board: the root session's own page and the turn block naming it.

Turning Crew Mode on is the ordinary switch to the ``kirocrew-conductor`` template. For
a ROOT session (``card_lifecycle.is_root_session``) that switch also adopts the shipped
``goal-board`` on the session's own page, which its side panel Overview draws, and each
turn of that session carries a ``[DASHBOARD]`` block naming the page. That block is
what the Conductor prompt reads to skip its inline task board, so these tests pin who
gets it: a root session running the conductor template with an adopted page, and
nobody else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import dashboard_agentic
from kiro_crew.context_assembly import turn as turn_parts
from kiro_crew.crew_log import session_tree_projection
from kiro_crew.dashboard_templates import instance

ROOT = "chat-200"
DISPATCHED = "chat-201"
ADOPTED = "chat-202"
CONDUCTOR = "kirocrew-conductor"


class _Node:
    def __init__(self, slot: str, parent_slot: str | None) -> None:
        self.slot = slot
        self.parent_slot = parent_slot
        self.cycle = False


def _slot(
    key: str,
    *,
    created_by: str = "",
    agent: str = CONDUCTOR,
    agent_kind: str = "template",
    mode: str = "",
    memory_mode: str = "",
) -> Any:
    return SimpleNamespace(
        key=key,
        _created_by=created_by,
        agent=agent,
        agent_kind=agent_kind,
        mode=mode,
        memory_mode=memory_mode,
    )


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    # The session tree, seeded: ADOPTED has a parent edge, the others none.
    nodes = {ADOPTED: _Node(ADOPTED, ROOT)}
    _floor_monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(nodes=lambda: nodes, seeded_for_current_store=True),
    )
    yield


def _template_of(slot_key: str) -> str:
    return instance.read(instance.session_instance_key(slot_key)).template_id


def test_a_session_with_no_adopted_page_gets_no_block():
    # Its Overview draws the default page, which asks nothing of the session.
    assert dashboard_agentic.session_turn_block(ROOT) == ""


def test_the_crew_mode_board_is_goal_board_and_the_block_names_it():
    assert dashboard_agentic.adopt_crew_mode_board(ROOT) is True
    assert _template_of(ROOT) == "goal-board"
    block = dashboard_agentic.session_turn_block(ROOT)
    assert block.startswith("[DASHBOARD]\n")
    # The words the Conductor prompt's skip rule reads: "your tab shows goal-board".
    assert "Your Dashboard tab shows template goal-board v" in block


def test_a_board_page_tells_the_conductor_not_to_draw_a_board():
    dashboard_agentic.adopt_crew_mode_board(ROOT)
    block = dashboard_agentic.session_turn_block(ROOT)
    assert dashboard_agentic.BOARD_IN_PANEL_LINE in block
    assert "Do not write a task board" in block


def test_a_page_that_is_not_a_board_gets_no_board_line():
    instance.adopt(instance.session_instance_key(ROOT), "project-report")
    assert dashboard_agentic.BOARD_IN_PANEL_LINE not in dashboard_agentic.session_turn_block(ROOT)


def test_a_page_already_adopted_is_kept():
    instance.adopt(instance.session_instance_key(ROOT), "project-report")
    assert dashboard_agentic.adopt_crew_mode_board(ROOT) is False
    assert _template_of(ROOT) == "project-report"


@pytest.mark.asyncio
async def test_a_root_session_gets_its_block():
    dashboard_agentic.adopt_crew_mode_board(ROOT)
    block = await dashboard_agentic.root_session_dashboard_block(_slot(ROOT))
    assert "goal-board" in block


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", [_slot(DISPATCHED, created_by=ROOT), _slot(ADOPTED)])
async def test_a_dispatched_or_adopted_session_gets_no_block(slot):
    # Even with a page on disk under its key: the gateway serves it no page.
    dashboard_agentic.adopt_crew_mode_board(slot.key)
    assert await dashboard_agentic.root_session_dashboard_block(slot) == ""


@pytest.mark.asyncio
async def test_a_crewmate_dm_slot_gets_no_session_block():
    # Its block rides in its member section, keyed by the crewmate.
    dashboard_agentic.adopt_crew_mode_board(ROOT)
    assert await dashboard_agentic.root_session_dashboard_block(_slot(ROOT, mode="member")) == ""


@pytest.mark.asyncio
async def test_a_root_session_off_crew_mode_keeps_its_page_but_gets_no_block():
    # Crew Mode turned off lands on the default template; the adopted page stays.
    dashboard_agentic.adopt_crew_mode_board(ROOT)
    slot = _slot(ROOT, agent="kirocrew")
    assert await dashboard_agentic.root_session_dashboard_block(slot) == ""
    assert _template_of(ROOT) == "goal-board"


@pytest.mark.asyncio
async def test_a_root_session_on_a_crewmate_named_like_the_conductor_gets_no_block():
    # A crewmate picked after Crew Mode was on is that crewmate, not Crew Mode.
    dashboard_agentic.adopt_crew_mode_board(ROOT)
    slot = _slot(ROOT, agent=CONDUCTOR, agent_kind="member")
    assert await dashboard_agentic.root_session_dashboard_block(slot) == ""


class _Owners:
    def __init__(self) -> None:
        self.frames: list[tuple[str, object]] = []

    def broadcast_ws_owners(self, msg_type: str, data: object) -> None:
        self.frames.append((msg_type, data))


@pytest.mark.asyncio
async def test_switching_a_root_session_to_the_conductor_adopts_the_board():
    owners = _Owners()
    await dashboard_agentic.adopt_crew_mode_board_on_switch(_slot(ROOT), CONDUCTOR, owners)
    assert _template_of(ROOT) == "goal-board"
    # The open Overview re-reads: the frame an agent's dashboard_apply sends.
    assert owners.frames == [("dashboard_instance_changed", {"slot": ROOT})]
    # A second switch keeps the page and tells nobody.
    await dashboard_agentic.adopt_crew_mode_board_on_switch(_slot(ROOT), CONDUCTOR, owners)
    assert len(owners.frames) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot", "agent"),
    [
        (_slot(DISPATCHED, created_by=ROOT), CONDUCTOR),
        (_slot(ADOPTED), CONDUCTOR),
        # A crewmate that carries the conductor's name is that crewmate, not Crew Mode.
        (_slot(ROOT, agent_kind="member"), CONDUCTOR),
        # Turning Crew Mode off lands on the default template and leaves the page alone.
        (_slot(ROOT), "kirocrew"),
    ],
)
async def test_no_board_is_adopted_off_the_crew_mode_path(slot, agent):
    await dashboard_agentic.adopt_crew_mode_board_on_switch(slot, agent)
    assert _template_of(slot.key) == ""


@pytest.mark.asyncio
async def test_a_persistent_root_session_adopts_the_board():
    await dashboard_agentic.adopt_crew_mode_board_on_switch(
        _slot(ROOT, memory_mode="persistent"), CONDUCTOR
    )
    assert _template_of(ROOT) == "goal-board"


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_a_private_memory_root_session_adopts_no_board(memory_mode):
    # Crew Mode needs a Persistent chat: a private one may not open sessions.
    owners = _Owners()
    slot = _slot(ROOT, memory_mode=memory_mode)
    await dashboard_agentic.adopt_crew_mode_board_on_switch(slot, CONDUCTOR, owners)
    assert _template_of(ROOT) == ""
    assert owners.frames == []


def _rail(**over: Any) -> str:
    kwargs: dict[str, Any] = {
        "project": None,
        "context_groups": None,
        "board_tags": None,
        "minimal_context": False,
        "folder_path": None,
        "session_key": None,
        "agent": None,
        "request_prefix_context": None,
    }
    kwargs.update(over)
    return "".join(turn_parts.rail_parts(**kwargs))


def test_the_turn_rail_carries_the_session_block_on_its_own_line():
    block = "[DASHBOARD]\nYour Dashboard tab shows template goal-board v1."
    rail = _rail(session_dashboard=block)
    assert "\n[DASHBOARD]\n" in "\n" + rail
    assert "goal-board" in rail
    assert _rail(session_dashboard=block, minimal_context=True).count("[DASHBOARD]") == 0
    assert "[DASHBOARD]" not in _rail()


def test_build_message_carries_the_session_block_into_a_follow_up_turn(tmp_path):
    from kiro_crew.context import ContextBuilder
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    dashboard_agentic.adopt_crew_mode_board(ROOT)
    block = dashboard_agentic.session_turn_block(ROOT)
    msg, _ = builder.build_message("hi", is_new_session=False, session_dashboard=block)
    # On its own line, so the block is read as a block.
    assert "\n[DASHBOARD]\nYour Dashboard tab shows template goal-board v" in "\n" + msg
    # Before the request header, so the request stays at the prompt's recency edge.
    assert msg.index("[DASHBOARD]") < msg.rindex("[CURRENT USER REQUEST")
    plain, _ = builder.build_message("hi", is_new_session=False)
    assert "[DASHBOARD]" not in plain


@pytest.mark.asyncio
async def test_the_agent_switch_route_adopts_the_board_for_the_conductor(tmp_path, monkeypatch):
    """The ordinary ``POST /api/chat/slots/{slot}/agent`` is where Crew Mode turns on."""
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes, _make_state
    from dashboard_owner_helpers import as_owner

    from kiro_crew.config.loader import KiroCrewConfig, ResolvedBindings
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    mod = "kiro_crew.dashboard.chat_handlers"
    bindings = ResolvedBindings(
        workspace_dir=tmp_path / "ws",
        effective_memory_config={},
        memory_store_name="default",
        requested_resolved=True,
        resolved_alias=CONDUCTOR,
        kiro_agent=CONDUCTOR,
        selection_kind="template",
        execution_context=ExecutionContext(
            None, MemoryStoreRef("default"), "template", CONDUCTOR, selection_name=CONDUCTOR
        ),
    )
    monkeypatch.setattr(f"{mod}.KiroCrewConfig.load", lambda: KiroCrewConfig())
    monkeypatch.setattr(f"{mod}.resolve_agent_bindings", lambda *_a, **_k: bindings)
    monkeypatch.setattr(f"{mod}._workspace_name_for_dir", lambda cfg, ws_dir: "default")
    monkeypatch.setattr(f"{mod}.warm_project_agent_names", AsyncMock())
    monkeypatch.setattr(f"{mod}.cached_project_agent_names", lambda project_dir: frozenset())
    monkeypatch.setattr(f"{mod}.default_project_dir", lambda ws: str(tmp_path / "ws"))

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")
    state.sessions.reset = AsyncMock()
    seen: list[str] = []
    monkeypatch.setattr(
        dashboard_agentic, "adopt_crew_mode_board", lambda key: seen.append(key) or False
    )

    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        resp = await client.post(
            "/api/chat/slots/s1/agent", json={"agent": CONDUCTOR, "agent_kind": "template"}
        )
        assert resp.status == 200, await resp.text()
    assert seen == ["s1"]
