"""A cron's tab sits in the workspace its job's agent is bound to.

A cron run executes in its agent's workspace, and ``session_create`` admits a
child only in the caller tab's own workspace, checked against the agent that
would answer. A tab in ``default`` for a job whose agent is bound elsewhere makes
that check refuse every child the run asks for (``agent_workspace_mismatch``).

Driven through the real config, resolver, workspace lookup and slot table: the
defect sits in the agreement between the tab's workspace and that resolution,
which a stand-in resolver hides.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from member_memory_helpers import forget_declared_stores, write_member_manifest

from kiro_crew.config import loader
from kiro_crew.cron import CronJob
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils_slots import cron_tab_name
from kiro_crew.dashboard.cron_inject import ensure_cron_slot, inject_cron_result_to_dashboard
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.history import ConversationLog
from kiro_crew.session_surface import set_dashboard_surfaced

JOB_ID = "0e520ea8"
CRON_SLOT = f"cron-{JOB_ID}"
CRON_KEY = f"cron:{JOB_ID}"
SCOUT_ID = "m-scout-0001"


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """The create-rate window and the dashboard-surface registry are process-wide."""
    create_rate_limit.reset_for_tests()
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())
    create_rate_limit.reset_for_tests()


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A home where the job's agent and a peer share ``research`` and others do not.

    ``scout`` is a crew member whose persisted id differs from its alias, so a
    member job that names it by id resolves through the id, not the name.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    dirs = {
        "default": "workspace",
        "research": "workspace-research",
        "elsewhere": "workspace-elsewhere",
        "scouting": "workspace-scouting",
    }
    config = {
        "workspaces": {name: {"dir": directory} for name, directory in dirs.items()},
        "memory_stores": {
            "default": {},
            "member-scout": write_member_manifest(
                tmp_path / "memory_stores" / "member-scout", SCOUT_ID
            ),
        },
        "agents": {
            "researcher": {"kiro_agent": "kirocrew", "workspace": "research"},
            "helper": {"kiro_agent": "kirocrew", "workspace": "research"},
            "outsider": {"kiro_agent": "kirocrew", "workspace": "elsewhere"},
            "scout": {
                "kiro_agent": "kirocrew",
                "workspace": "scouting",
                "memory_store": "member-scout",
                "member_id": SCOUT_ID,
            },
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    for directory in dirs.values():
        (tmp_path / directory).mkdir()
    forget_declared_stores(monkeypatch)
    # The installed-template catalog, the one external boundary, seeded the way
    # the loader's own tests seed it.
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"kirocrew"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    dashboard = _make_state(tmp_path)
    dashboard.conversation_log = ConversationLog()
    # A cron caller's ownership is read from its job; an unfindable one is refused.
    dashboard.crons.list_jobs.return_value = [SimpleNamespace(id=JOB_ID, created_by="")]
    return dashboard


def _job(**over) -> CronJob:
    fields = {
        "id": JOB_ID,
        "name": "nightly research",
        "message": "research",
        "agent_id": "researcher",
    }
    fields.update(over)
    return CronJob(**fields)


async def _run_start_then_create(state, **kwargs):
    """The run-start pre-create, then the turn's own ``session_create``."""
    await ensure_cron_slot(state, _job())
    return await sc.create_session(state, caller_session_key=CRON_KEY, **kwargs)


def test_the_run_start_tab_sits_in_its_agents_workspace(state):
    asyncio.run(ensure_cron_slot(state, _job()))

    tab = state.get_slot(CRON_SLOT)
    assert tab.linked_session_key == CRON_KEY
    assert tab.workspace == "research"


@pytest.mark.parametrize("agent", ["", "helper"], ids=["inherited", "peer"])
def test_the_runs_own_session_create_opens_a_child_beside_it(state, agent):
    """Its own agent, inherited when omitted, or a peer bound to the same workspace."""
    result = asyncio.run(_run_start_then_create(state, agent=agent))

    child = state.get_slot(result["target"])
    assert child is not None
    assert child.workspace == "research"
    assert child.agent == (agent or "researcher")


def test_an_agent_bound_to_a_third_workspace_is_still_refused(state):
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(_run_start_then_create(state, agent="outsider"))

    assert exc.value.code == "agent_workspace_mismatch"


def test_a_linked_tab_in_default_follows_its_agent_at_the_next_run_start(state):
    """A tab minted before its agent moved, or restored from a line that omits ``default``."""
    tab = state.get_or_create_slot(
        CRON_SLOT, agent="researcher", linked_session_key=CRON_KEY, origin=SlotOrigin.CRON
    )
    assert tab.workspace == "default"
    tab._dirty = False

    asyncio.run(ensure_cron_slot(state, _job()))

    assert state.get_slot(CRON_SLOT) is tab
    assert tab.workspace == "research"
    assert tab._dirty, "the move must reach the metadata line"


def test_a_member_jobs_tab_sits_in_the_members_workspace(state):
    job = _job(agent_id="", member_id=SCOUT_ID, memory_store="member-scout")

    asyncio.run(ensure_cron_slot(state, job))

    assert state.get_slot(CRON_SLOT).workspace == "scouting"


def test_a_delivered_run_tab_sits_in_its_agents_workspace(state):
    """A ``new_chat_per_run`` tab has no run-start pre-create: the delivery mints it."""
    run_key = f"{CRON_KEY}:11111111"
    job = _job(persistent_session=False, new_chat_per_run=True)

    inject_cron_result_to_dashboard(state, job, "findings", history=[], session_key=run_key)

    assert state.get_slot(cron_tab_name(run_key)).workspace == "research"


def test_an_unresolved_workspace_leaves_the_tab_where_it_is(state, monkeypatch):
    tab = state.get_or_create_slot(
        CRON_SLOT,
        agent="researcher",
        workspace="research",
        linked_session_key=CRON_KEY,
        origin=SlotOrigin.CRON,
    )

    def unreadable(*_args, **_kwargs):
        raise OSError("config.json is unreadable")

    monkeypatch.setattr(loader, "resolve_agent_bindings", unreadable)
    asyncio.run(ensure_cron_slot(state, _job()))

    assert tab.workspace == "research"
