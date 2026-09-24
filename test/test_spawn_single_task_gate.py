"""A one-task spawn is steered by the prompt, not refused by a runtime gate.

A gate that checks reason SHAPE always leaves the model a passing token, so
one-task spawns are steered by prompt defaults instead. These tests pin that:

* the tool descriptions carry the concrete "one task: do it yourself" default
  and advertise no reason vocabulary;
* a one-task call spawns without any reason, and carries no gate marker;
* the legacy ``solo_reason`` / ``solo_details`` fields are still ACCEPTED (so a
  skill or workflow written against the old schema is not refused as an
  unknown field) but are ignored and never forwarded;
* the gateway neither refuses nor audits a lone spawn, and still reports
  whether the parent may do bounded work of its own.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers.messaging import parent_work_supported
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.validation import SPAWN_RUN_SCHEMA, SPAWN_SUB_AGENTS_SCHEMA, validate_tool_args


def _tools() -> dict[str, dict]:
    roster = [types.SimpleNamespace(name="kirocrew")]
    with patch.object(spawn_tools.mcp_core, "list_agents", return_value=roster):
        return {t["name"]: t for t in spawn_tools.schemas()}


# ── advertisement ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["spawn_run", "spawn_sub_agents"])
def test_schema_no_longer_advertises_a_reason_vocabulary(tool: str) -> None:
    t = _tools()[tool]
    props = t["inputSchema"]["properties"]
    assert "solo_reason" not in props and "solo_details" not in props
    desc = t["description"]
    assert "ENFORCED" not in desc
    for token in ("solo_reason", "parent_parallel", "specialist value"):
        assert token not in desc, (tool, token)


def test_spawn_run_description_states_the_single_task_default() -> None:
    desc = _tools()["spawn_run"]["description"]
    assert "One task is almost always faster done yourself" in desc
    assert "two or more independent tasks" in desc
    assert "Returns immediately" in desc
    assert "[Subagent completion event]" in desc
    assert "capacity is a ceiling, not a target" in desc
    assert "Keep dependent tasks for a later batch" in desc


def test_single_task_contract_bans_only_unjustified_transfer() -> None:
    task = _tools()["spawn_run"]["inputSchema"]["properties"]["task"]["description"]
    assert "bounded assignment" in task
    assert "ready inputs, ownership, verifiable outputs and stop conditions" in task
    assert "equivalent worker merely to wait and relay" in task


# ── legacy fields ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "schema, base",
    [(SPAWN_RUN_SCHEMA, {"task": "x"}), (SPAWN_SUB_AGENTS_SCHEMA, {"agents": [{"prompt": "x"}]})],
)
@pytest.mark.parametrize("reason", ["bulk_data", "parent_parallel", "anything-at-all"])
def test_legacy_solo_fields_are_still_accepted(schema, base, reason) -> None:
    cleaned = validate_tool_args({**base, "solo_reason": reason, "solo_details": "d"}, schema)
    assert cleaned["solo_reason"] == reason


# ── tool side ────────────────────────────────────────────────────────────────


def _run(tool: str, args: dict[str, Any], answer: dict | None = None):
    """Run a spawn tool; return (POSTed bodies, result text, sel mock)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []
    sel = MagicMock()

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            return dict(answer or {"id": "a1"})
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock(return_value=sel)),
    ):
        result = mcp_core._call_tool_inner(tool, args)
    return bodies, result, sel


def test_lone_task_spawns_without_a_reason() -> None:
    bodies, result, sel = _run("spawn_run", {"task": "read the log and fix it"})
    assert len(bodies) == 1
    assert "Spawned 1 subagent(s)" in result
    assert "Solo spawn" not in result
    for key in ("solo", "solo_reason", "solo_details"):
        assert key not in bodies[0]
    # The gate is gone, but the one-line allowed-only audit survives it:
    # a one-task spawn_run call still logs spawn.solo so the rate stays
    # measurable without reviving the removed refusal.
    solo_logs = [
        c for c in sel.log_api_access.call_args_list if c.kwargs.get("operation") == "spawn.solo"
    ]
    assert len(solo_logs) == 1
    assert solo_logs[0].kwargs["outcome"] == "allowed"


def test_legacy_reason_is_ignored_not_forwarded() -> None:
    bodies, result, _ = _run(
        "spawn_run",
        {"task": "x", "solo_reason": "parent_parallel", "solo_details": "parent owns backend"},
    )
    assert len(bodies) == 1 and not result.startswith("Error")
    assert "solo_reason" not in bodies[0] and "solo_details" not in bodies[0]


@pytest.mark.parametrize("supported", [True, False, None])
def test_receipt_still_controls_the_parent_work_boundary(supported) -> None:
    _, result, _ = _run(
        "spawn_run", {"task": "x"}, {"id": "a1", "parent_work_supported": supported}
    )
    assert "END YOUR TURN" in result
    assert ("at most one minute" in result) is (supported is True)
    assert ("no confirmed parent-work" in result) is (supported is not True)


# ── gateway side ─────────────────────────────────────────────────────────────


class TestApiSpawn:
    PARENT = "dashboard:1"

    async def _call(self, body: dict):
        from kiro_crew.dashboard.handlers import messaging
        from kiro_crew.dashboard.state import _ChatSlot

        body = {"parent_session": self.PARENT, **body}
        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        mgr.max_concurrent = 4
        sessions = MagicMock()
        sessions.get_agent.return_value = "kirocrew"
        sessions.get_agent_selection.return_value = ("template", "kirocrew")
        state = SimpleNamespace(
            _slots={"1": _ChatSlot("1", memory_mode="persistent")},
            _restricted_keys=set(),
            subagents=mgr,
            sessions=sessions,
            conversation_log=SimpleNamespace(get_metadata_status=lambda key: ({}, True)),
        )
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": self.PARENT}

        async def _json() -> dict:
            return body

        request.json = _json
        sel = MagicMock()
        with (
            patch.object(messaging, "_sel", return_value=sel),
            patch.object(messaging, "warm_project_agents_for_spawn", AsyncMock()),
        ):
            resp = await messaging.api_spawn(request)
        return resp, mgr, sel

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra",
        [
            {"solo": True},
            {"solo": True, "agent": "kirocrew"},
            {"solo": True, "solo_reason": "user_requested"},
        ],
    )
    async def test_lone_spawn_is_never_refused_or_audited(self, extra) -> None:
        resp, mgr, sel = await self._call({"task": "x", **extra})
        assert resp.status == 200
        mgr.spawn.assert_called_once()
        assert "delegation" not in mgr.spawn.call_args.kwargs
        assert not [
            c
            for c in sel.log_api_access.call_args_list
            if c.kwargs.get("operation") == "spawn.solo"
        ]

    @pytest.mark.asyncio
    async def test_dashboard_parent_receipt_supports_parent_work(self) -> None:
        resp, _, _ = await self._call({"task": "x"})
        assert '"parent_work_supported": true' in resp.text


@pytest.mark.parametrize("parent", ["", "cron:a", "subagent:a", "hook:a", "slack:unlinked"])
def test_unknown_or_background_parent_cannot_claim_concurrent_work(parent) -> None:
    assert not parent_work_supported(SimpleNamespace(_slots={}), parent)
