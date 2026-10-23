"""The spawn receipt says when a set model or effort starts a run on its own runtime.

A model or effort in a run's sharing plan -- per call, the ``subagent`` role
pins in Settings, or a routed model -- sends it down the dedicated-process path
(``_sharing_plan_impl``), which costs one kiro-cli process plus its MCP servers
per run. ``POST /api/spawn`` reads that same plan for an additive
``runtime_note`` key, and ``spawn_run`` renders it once per batch.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import PINNED_RUNTIME_NOTE
from kiro_crew.subagent_parts.start_pricing import _SharingPlan

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _api_spawn(
    body: dict,
    plan: Any,
    *,
    would_share: Any = True,
    keep: bool = False,
    selection_kind: str = "template",
) -> tuple[dict, MagicMock]:
    from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
    from kiro_crew.dashboard.handlers.messaging import api_spawn
    from kiro_crew.dashboard.state import _ChatSlot

    mgr = MagicMock()
    info = SimpleNamespace(
        id="a1",
        done=False,
        error="",
        model="sonnet-test-model",
        routed_model="r",
        keep=keep,
        agent="",
        execution_context=SimpleNamespace(selection_kind=selection_kind),
    )
    mgr.spawn.return_value = info
    if isinstance(plan, Exception):
        mgr._sharing_plan.side_effect = plan
    else:
        mgr._sharing_plan.return_value = plan
    mgr._should_use_session_sharing.return_value = would_share
    state = SimpleNamespace(
        _slots={"1": _ChatSlot("1", memory_mode="persistent")},
        _restricted_keys=set(),
        subagents=mgr,
        sessions=SimpleNamespace(
            get_agent=lambda key: "",
            get_agent_selection=MagicMock(return_value=("template", "")),
        ),
        conversation_log=SimpleNamespace(get_metadata_status=lambda key: ({}, True)),
    )
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": body.get("parent_session", "")}
    request.json = AsyncMock(return_value=body)
    cfg = KiroCrewConfig(agent=AgentConfig())
    with (
        patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        patch(
            "kiro_crew.dashboard.handlers.messaging.warm_project_agents_for_spawn",
            AsyncMock(),
        ),
    ):
        resp = asyncio.run(api_spawn(request))
    assert resp.status == 200
    mgr.spawn_info = info
    return json.loads(resp.body), mgr


_BODY = {"task": "x", "parent_session": "dashboard:1"}


class TestApiSpawnRuntimeNote:
    @pytest.mark.parametrize(
        "plan",
        [
            _SharingPlan("sonnet-test-model", "", False),
            _SharingPlan("", "high", False),
            _SharingPlan("routed-model", "high", False),
        ],
    )
    def test_a_model_or_effort_in_the_plan_adds_the_key(self, plan):
        data, _ = _api_spawn(_BODY, plan)
        assert data["runtime_note"] == PINNED_RUNTIME_NOTE
        assert data["status"] == "spawned"

    @pytest.mark.parametrize("plan", [_SharingPlan("", "", True), _SharingPlan("", "", False)])
    def test_no_model_or_effort_omits_the_key(self, plan):
        """A dedicated run with no pin (keep, a member, an AWS profile) is not
        credited to a pin it does not have."""
        data, _ = _api_spawn(_BODY, plan)
        assert "runtime_note" not in data
        assert data["id"] == "a1"

    def test_the_note_reads_the_runs_own_plan(self):
        _, mgr = _api_spawn(_BODY, _SharingPlan("m", "", False))
        args, _kwargs = mgr._sharing_plan.call_args
        assert args == (mgr.spawn_info,)

    @pytest.mark.parametrize("would_share", [False, None, MagicMock()])
    def test_no_note_where_the_run_could_never_share(self, would_share):
        """A backend outside session sharing, ``session_sharing`` off or an
        ineligible parent: the pin took nothing away, so nothing is said."""
        data, _ = _api_spawn(_BODY, _SharingPlan("m", "high", False), would_share=would_share)
        assert "runtime_note" not in data

    def test_no_note_for_a_member_run(self):
        """A member run never takes the shared arm, even one whose context
        carries no member id (a v1-store member)."""
        data, mgr = _api_spawn(_BODY, _SharingPlan("m", "", False), selection_kind="member")
        assert "runtime_note" not in data
        mgr._should_use_session_sharing.assert_not_called()

    def test_no_note_for_a_kept_run(self):
        """A kept run owns its process whatever its model."""
        data, _ = _api_spawn(_BODY, _SharingPlan("m", "", False), keep=True)
        assert "runtime_note" not in data

    def test_the_share_question_is_asked_without_the_runs_own_model(self):
        """The run's own model is what turns sharing off, so the 'would it
        share otherwise' question drops it -- on a copy, never the run's info."""
        _, mgr = _api_spawn(_BODY, _SharingPlan("sonnet-test-model", "", False))
        (asked,), _ = mgr._should_use_session_sharing.call_args
        assert asked is not mgr.spawn_info
        assert asked.model == "" and asked.routed_model == ""
        assert mgr.spawn_info.model == "sonnet-test-model"
        assert mgr.spawn_info.routed_model == "r"

    @pytest.mark.parametrize("plan", [RuntimeError("plan failed"), MagicMock()])
    def test_an_unreadable_plan_never_fails_the_spawn(self, plan):
        data, _ = _api_spawn(_BODY, plan)
        assert "runtime_note" not in data
        assert data["status"] == "spawned"

    def test_note_names_the_cost(self):
        assert "own runtime" in PINNED_RUNTIME_NOTE
        assert "0.55 GB" in PINNED_RUNTIME_NOTE


def _receipt(responses: list[dict[str, Any]], tasks: list[str]) -> str:
    from kiro_crew import mcp_core

    replies = iter(responses)

    def _fake_post(path: str, body: dict) -> dict:
        return next(replies) if path == "/api/spawn" else {}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        return mcp_core._call_tool("spawn_run", {"tasks": tasks})


class TestReceipt:
    def test_note_is_rendered_once_for_the_batch(self):
        result = _receipt(
            [{"id": f"a{i}", "runtime_note": "pinned-note"} for i in range(3)],
            ["t0", "t1", "t2"],
        )
        assert "ℹ a0, a1, a2: pinned-note" in result
        assert result.count("pinned-note") == 1

    def test_no_note_without_the_key(self):
        result = _receipt([{"id": "a0"}, {"id": "a1"}], ["t0", "t1"])
        assert "own runtime" not in result
        assert result.startswith("Spawned 2 subagent(s).")

    def test_note_never_shadows_the_error_prefix(self):
        result = _receipt(
            [{"error": "nope", "counted": True}, {"error": "nope", "counted": True}],
            ["t0", "t1"],
        )
        assert result.startswith("Error:")
