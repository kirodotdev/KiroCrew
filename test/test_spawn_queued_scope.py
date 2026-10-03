"""An attested internal parent reads its own queued children, and every child
of a wave answers queued to it, never "not found" (real handlers, real manager)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request

from kiro_crew.dashboard.handlers import messaging
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent import SubagentManager

pytestmark = [
    pytest.mark.xdist_group("member_memory_api"),
    pytest.mark.usefixtures("close_subagent_managers"),
]

env = _member_env

PARENT = "dashboard:alice"


def _req(env, run_id: str, **shape):
    return make_request(env.state, f"/api/spawn/{run_id}", match_info={"agent_id": run_id}, **shape)


def _manager(*run_ids: str) -> SubagentManager:
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    for rid in run_ids:
        mgr._queue.append({"_preassigned_id": rid, "parent_session_key": PARENT, "task": rid})
    return mgr


@pytest.mark.asyncio
async def test_the_parent_reads_its_own_queued_child(env) -> None:
    env.state.subagents = _manager("q1")
    resp = await messaging.api_spawn_status(
        _req(env, "q1", internal=True, session=PARENT, attested=True)
    )
    body = json.loads(resp.text)
    assert resp.status == 200
    assert body["queued"] is True and body["status"] == "queued" and body["done"] is False
    other = await messaging.api_spawn_status(
        _req(env, "q1", internal=True, session="dashboard:bob", attested=True)
    )
    assert other.status == 404


@pytest.mark.asyncio
async def test_every_queued_child_of_a_wave_answers_queued_to_its_parent(env) -> None:
    """One child deferred at accept time, one waiting only for a slot: each
    poll the wave makes as its parent reads ``queued``, never a 404, so the
    blocking wait keeps following them instead of ending on "not found"."""
    env.state.subagents = _manager("deferred1", "stagger1")
    for run_id in ("deferred1", "stagger1"):
        resp = await messaging.api_spawn_status(
            _req(env, run_id, internal=True, session=PARENT, attested=True)
        )
        body = json.loads(resp.text)
        assert resp.status == 200 and body["queued"] is True and body["done"] is False
        assert spawn_tools._held_by_a_deferral(body) is False  # no deferral named: keep waiting


@pytest.mark.asyncio
async def test_a_child_held_by_a_paused_cap_settles_the_wait(env) -> None:
    """A cap the adaptive controller paused (effective cap 0) is a deferral the
    accept answer already called ``queued``, but such a row has no ``deferred``
    event to carry its sentence. Every poll names the pause instead, so the
    blocking wait settles on it rather than holding the parent until max_wait."""
    mgr = _manager("paused1")
    assert mgr.set_effective_cap(0) == 0
    mgr._queue_wait[PARENT] = {"reason": "concurrency_limit"}  # a sibling's stale label
    env.state.subagents = mgr
    resp = await messaging.api_spawn_status(
        _req(env, "paused1", internal=True, session=PARENT, attested=True)
    )
    body = json.loads(resp.text)
    assert resp.status == 200 and body["queued"] is True and body["done"] is False
    assert body["reason"] == "adaptive_cap_zero"
    assert "effective cap 0" in body["reason_detail"]
    assert spawn_tools._held_by_a_deferral(body) is True
