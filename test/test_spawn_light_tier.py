"""``spawn_run`` ``light``: a helper sub-agent on a cheaper model and a lower effort.

A parent that hands a search, lookup, run-and-report or summarize task to a
sub-agent can mark it ``light``. With ``agent.subagent_light_tier`` on, such a
run takes ``agent.role_models['subagent_light']`` and
``agent.role_efforts['subagent_light']`` for whichever of model and effort the
call left unset. Every other spawn, and every spawn while the setting is off,
picks exactly the model and effort it picks without the flag.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
from kiro_crew.config.sections_agent_fields import coerce_role_efforts, coerce_role_models
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_LIGHT_MODEL = "claude-haiku-4.5"
_LIGHT_EFFORT = "low"


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


def _cfg(
    *, tier: bool = True, model: str = _LIGHT_MODEL, effort: str = _LIGHT_EFFORT, **role: str
) -> KiroCrewConfig:
    """A config with the light pins set; *role* adds ``role_models`` entries."""
    models = {**({"subagent_light": model} if model else {}), **role}
    efforts = {"subagent_light": effort} if effort else {}
    agent = AgentConfig(role_models=models, role_efforts=efforts)
    # Set after construction so a tree without the setting still builds the config.
    agent.subagent_light_tier = tier
    return KiroCrewConfig(agent=agent)


def _sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


async def _spawn(body: dict[str, Any], cfg: KiroCrewConfig) -> tuple[dict[str, Any], Any]:
    """POST /api/spawn on a real manager whose runs never start: (response, run record)."""
    from kiro_crew.dashboard.handlers.messaging import api_spawn
    from kiro_crew.subagent import SubagentManager

    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    sessions = _sessions()
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    mgr._run = AsyncMock()  # type: ignore[method-assign]
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(subagents=mgr, sessions=sessions, conversation_log=MagicMock())
    }
    request.headers = {}
    request.json = AsyncMock(return_value=body)
    with (
        patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        # Any agent name is "installed": the roster is the host's, not this test's input.
        patch("kiro_crew.subagent._validate_agent", lambda agent, cwd="": (agent, "", "")),
    ):
        resp = await api_spawn(request)
    assert resp.status == 200, resp.text
    data = json.loads(resp.text)
    return data, mgr.get(data["id"])


async def _spawn_under_crewmate(
    cfg: KiroCrewConfig, *, crewmate_backend: Any
) -> tuple[dict[str, Any], Any]:
    """A ``light`` spawn whose parent is crewmate ``alpha``'s own worker.

    *crewmate_backend* is what the crewmate's worker resolves to start on (a
    backend id, or a callable standing in for the resolver).
    """
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    parent = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "template", MATE_WORKER_AGENT
    )
    resolver = (
        crewmate_backend if callable(crewmate_backend) else (lambda *_a, **_k: crewmate_backend)
    )
    with (
        patch("kiro_crew.execution_context.read_session_execution", lambda *a, **k: parent),
        # The worker's own spec lives in the crewmate folder this test does not write.
        patch(
            "kiro_crew.dashboard.handlers.messaging.parent_template_spawn_policy",
            lambda *a, **k: ("", (), ()),
        ),
        patch("kiro_crew.mate_worker.crewmate_worker_backend", resolver),
        patch("kiro_crew.crewmate_harness.crew_for_member_id", lambda _c, m: m),
    ):
        return await _spawn({"task": "x", "light": True, "parent_session": "subagent:p1"}, cfg)


def _tool(args: dict[str, Any], reply: dict[str, Any] | None = None) -> tuple[list[dict], str]:
    """Run spawn_run with a canned gateway reply: (POSTed bodies, returned text)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": "a1", **(reply or {})}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        text = mcp_core._call_tool("spawn_run", args)
    return bodies, text


class TestSettingAndPins:
    def test_the_tier_is_off_by_default(self):
        assert AgentConfig().subagent_light_tier is False

    def test_the_light_role_is_a_known_pin_key(self):
        assert coerce_role_models({"subagent_light": "claude-haiku-4.5"}) == {
            "subagent_light": "claude-haiku-4.5"
        }
        assert coerce_role_efforts({"subagent_light": "low"}) == {"subagent_light": "low"}

    def test_an_invalid_light_effort_is_dropped_like_any_role_effort(self):
        assert coerce_role_efforts({"subagent_light": "ultra"}) == {}


class TestSchema:
    def test_the_flag_is_accepted_batch_wide_and_per_task(self):
        cleaned = validate_tool_args(
            {"tasks": [{"task": "x", "light": True}, "y"], "light": True}, SPAWN_RUN_SCHEMA
        )
        assert cleaned["light"] is True
        assert cleaned["tasks"][0]["light"] is True

    def test_a_non_boolean_flag_is_refused_like_every_other_boolean(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "light": "yes"}, SPAWN_RUN_SCHEMA)


class TestToolForwarding:
    def test_a_call_without_the_flag_posts_no_light_key(self):
        bodies, _ = _tool({"tasks": ["a", {"task": "b", "model": "m1"}]})
        assert all("light" not in body for body in bodies)

    def test_the_batch_flag_reaches_every_member(self):
        bodies, _ = _tool({"tasks": ["a", "b"], "light": True})
        assert [body.get("light") for body in bodies] == [True, True]

    def test_a_task_object_can_mark_only_itself(self):
        bodies, _ = _tool({"tasks": ["a", {"task": "b", "light": True}]})
        assert ["light" in body for body in bodies] == [False, True]

    def test_a_task_objects_own_false_wins_over_the_batch_flag(self):
        # Every other tasks[] field overrides the batch-wide value for its task.
        bodies, _ = _tool({"tasks": ["a", {"task": "review", "light": False}], "light": True})
        assert [body.get("light") for body in bodies] == [True, None]

    def test_the_gateways_note_and_warning_reach_the_receipt(self):
        _, text = _tool(
            {"task": "x", "light": True},
            {"light": "light tier: model m on effort low", "warning": "light dropped: because"},
        )
        assert "light tier: model m on effort low" in text
        assert "light dropped: because" in text


class TestGatewayTierOff:
    @pytest.mark.asyncio
    async def test_a_light_spawn_keeps_main_model_and_effort_and_says_nothing(self):
        # Every parent is told about the flag; a user who never turned the tier on
        # is not told about a feature they did not ask for.
        data, info = await _spawn({"task": "x", "light": True}, _cfg(tier=False))
        assert (info.model, info.reasoning_effort) == ("", "")
        assert "warning" not in data and "light" not in data

    @pytest.mark.asyncio
    async def test_a_spawn_without_the_flag_is_untouched_even_with_the_tier_on(self):
        data, info = await _spawn({"task": "x"}, _cfg(tier=True))
        assert (info.model, info.reasoning_effort) == ("", "")
        assert "warning" not in data and "light" not in data

    @pytest.mark.asyncio
    async def test_the_light_pins_never_become_the_plain_subagent_pin(self):
        _, info = await _spawn({"task": "x"}, _cfg(tier=True, subagent="claude-sonnet-4.6"))
        assert (info.model, info.reasoning_effort) == ("", "")


class TestGatewayTierOn:
    @pytest.mark.asyncio
    async def test_a_light_spawn_takes_the_light_model_and_effort(self):
        data, info = await _spawn({"task": "x", "light": True}, _cfg())
        assert (info.model, info.reasoning_effort) == (_LIGHT_MODEL, _LIGHT_EFFORT)
        assert _LIGHT_MODEL in data["light"] and _LIGHT_EFFORT in data["light"]
        assert "warning" not in data

    @pytest.mark.asyncio
    async def test_a_light_spawn_beats_the_plain_subagent_pin(self):
        _, info = await _spawn({"task": "x", "light": True}, _cfg(subagent="claude-sonnet-4.6"))
        assert info.model == _LIGHT_MODEL

    @pytest.mark.asyncio
    async def test_the_parents_own_model_wins_and_the_effort_pin_still_fills_in(self):
        _, info = await _spawn({"task": "x", "light": True, "model": "claude-opus-5.5"}, _cfg())
        assert (info.model, info.reasoning_effort) == ("claude-opus-5.5", _LIGHT_EFFORT)

    @pytest.mark.asyncio
    async def test_the_parents_own_effort_wins_and_the_model_pin_still_fills_in(self):
        _, info = await _spawn({"task": "x", "light": True, "reasoning_effort": "high"}, _cfg())
        assert (info.model, info.reasoning_effort) == (_LIGHT_MODEL, "high")

    @pytest.mark.asyncio
    async def test_a_model_only_tier_leaves_the_effort_alone(self):
        _, info = await _spawn({"task": "x", "light": True}, _cfg(effort=""))
        assert (info.model, info.reasoning_effort) == (_LIGHT_MODEL, "")

    @pytest.mark.asyncio
    async def test_an_empty_tier_keeps_main_outcome_and_says_nothing_is_pinned(self):
        data, info = await _spawn({"task": "x", "light": True}, _cfg(model="", effort=""))
        assert (info.model, info.reasoning_effort) == ("", "")
        assert "agent.role_models.subagent_light" in data["warning"]
        assert "agent.role_efforts.subagent_light" in data["warning"]

    @pytest.mark.asyncio
    async def test_the_warning_names_only_the_pin_the_call_needed_and_found_empty(self):
        # The call names its model, so only the (unset) effort pin was needed.
        data, info = await _spawn(
            {"task": "x", "light": True, "model": "claude-opus-5.5"}, _cfg(effort="")
        )
        assert (info.model, info.reasoning_effort) == ("claude-opus-5.5", "")
        assert "agent.role_efforts.subagent_light" in data["warning"]
        assert "role_models" not in data["warning"]

    @pytest.mark.asyncio
    async def test_a_named_effort_with_an_empty_model_pin_names_only_the_model_pin(self):
        data, info = await _spawn(
            {"task": "x", "light": True, "reasoning_effort": "high"}, _cfg(model="")
        )
        assert (info.model, info.reasoning_effort) == ("", "high")
        assert "agent.role_models.subagent_light" in data["warning"]
        assert "role_efforts" not in data["warning"]


class TestGatewayDropsWhatCannotApply:
    @pytest.mark.asyncio
    async def test_a_run_that_names_its_own_agent_is_not_re_modelled(self):
        data, info = await _spawn({"task": "x", "light": True, "agent": "kirocrew"}, _cfg())
        assert (info.model, info.reasoning_effort) == ("", "")
        assert "light" in data["warning"]

    @pytest.mark.asyncio
    async def test_a_run_that_names_its_own_backend_is_not_re_modelled(self):
        # The named backend reads as installed here, as on a machine that has it:
        # without that, the gateway refuses the spawn before the tier is consulted,
        # and the result would depend on what this machine has installed.
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

        resolved = AsyncMock(return_value=(ACP_BACKEND_KIRO, None, None))
        with patch("kiro_crew.dashboard.handlers.messaging._requested_backend", resolved):
            data, info = await _spawn({"task": "x", "light": True, "backend": "kiro"}, _cfg())
        assert (info.model, info.reasoning_effort) == ("", "")
        assert "light" in data["warning"]

    @pytest.mark.asyncio
    async def test_a_run_inheriting_another_backend_keeps_no_model_pin_but_keeps_the_effort(self):
        cfg = _cfg()
        other = next(b for b in ("codex", "claude", "") if b != cfg.agent.acp_backend)
        with patch(
            "kiro_crew.dashboard.handlers.messaging._requested_backend",
            AsyncMock(return_value=(other, None, None)),
        ):
            data, info = await _spawn({"task": "x", "light": True}, cfg)
        assert (info.model, info.reasoning_effort) == ("", _LIGHT_EFFORT)
        assert "agent.role_models.subagent_light" in data["warning"]

    @pytest.mark.asyncio
    async def test_a_run_inheriting_the_default_backend_keeps_the_light_model(self):
        # A chat created on the gateway's own default backend hands that backend
        # to its children; the light model was written for exactly that backend.
        cfg = _cfg()
        with patch(
            "kiro_crew.dashboard.handlers.messaging._requested_backend",
            AsyncMock(return_value=(cfg.agent.acp_backend, None, None)),
        ):
            data, info = await _spawn({"task": "x", "light": True}, cfg)
        assert (info.model, info.reasoning_effort) == (_LIGHT_MODEL, _LIGHT_EFFORT)
        assert "warning" not in data

    @pytest.mark.asyncio
    async def test_a_crewmates_helper_on_another_backend_keeps_no_model_pin(self):
        # A crewmate's worker (a ``spawn_run(crew=...)`` run) names no backend, yet
        # its child starts on the crewmate's own backend; the light model was
        # written for the gateway default.
        cfg = _cfg()
        other = next(b for b in ("codex", "claude", "") if b != cfg.agent.acp_backend)
        data, info = await _spawn_under_crewmate(cfg, crewmate_backend=other)
        assert (info.model, info.reasoning_effort) == ("", _LIGHT_EFFORT)
        assert "agent.role_models.subagent_light" in data["warning"]

    @pytest.mark.asyncio
    async def test_a_crewmates_helper_on_the_default_backend_keeps_the_light_model(self):
        cfg = _cfg()
        data, info = await _spawn_under_crewmate(cfg, crewmate_backend=cfg.agent.acp_backend)
        assert (info.model, info.reasoning_effort) == (_LIGHT_MODEL, _LIGHT_EFFORT)
        assert "warning" not in data

    @pytest.mark.asyncio
    async def test_a_crewmate_backend_that_cannot_be_resolved_keeps_no_model_pin(self):
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable

        def _unavailable(*_a: object, **_k: object) -> str:
            raise CrewmateHarnessUnavailable("alpha", "claude")

        data, info = await _spawn_under_crewmate(_cfg(), crewmate_backend=_unavailable)
        assert (info.model, info.reasoning_effort) == ("", _LIGHT_EFFORT)
        assert "agent.role_models.subagent_light" in data["warning"]

    @pytest.mark.asyncio
    async def test_a_pin_that_is_not_a_model_id_is_dropped_and_not_echoed(self):
        bad = "x;rm -rf /\nsecret-token"
        data, info = await _spawn({"task": "x", "light": True}, _cfg(model=bad))
        assert info.model == ""
        assert info.reasoning_effort == _LIGHT_EFFORT
        assert "secret-token" not in json.dumps(data)
        assert "agent.role_models.subagent_light" in data["warning"]


class TestStartBackend:
    """``subagent_start_backend``: the one answer admission and allocation share."""

    @staticmethod
    def _crewmate(kind: str) -> Any:
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
        from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT

        store = MemoryStoreRef("member-alpha", "alpha")
        if kind == "worker":
            return ExecutionContext("alpha", store, "template", MATE_WORKER_AGENT)
        return ExecutionContext("alpha", store, "member", MATE_CONDUCTOR_AGENT, selection_name="a")

    def test_a_plain_run_starts_on_the_gateway_default(self):
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
        from kiro_crew.mate_worker import subagent_start_backend

        cfg = _cfg()
        plain = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
        assert subagent_start_backend(cfg, plain, None) == cfg.agent.acp_backend

    @pytest.mark.parametrize("kind", ["worker", "conductor"])
    def test_a_crewmates_run_starts_on_its_crewmates_backend(self, kind):
        from kiro_crew.mate_worker import subagent_start_backend

        with (
            patch("kiro_crew.mate_worker.crewmate_worker_backend", lambda _c, crew: f"of-{crew}"),
            patch("kiro_crew.crewmate_harness.crew_for_member_id", lambda _c, m: m),
        ):
            assert subagent_start_backend(_cfg(), self._crewmate(kind), None) == "of-alpha"

    def test_the_runs_own_backend_wins_over_the_crewmates(self):
        from kiro_crew.mate_worker import subagent_start_backend

        with (
            patch("kiro_crew.mate_worker.crewmate_worker_backend", lambda *_a: "never"),
            patch("kiro_crew.acp_backends.resolve_selected_backend", lambda b: f"resolved-{b}"),
        ):
            assert subagent_start_backend(_cfg(), self._crewmate("worker"), "x") == "resolved-x"
