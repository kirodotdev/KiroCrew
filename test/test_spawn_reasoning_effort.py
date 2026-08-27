"""The per-call ``reasoning_effort`` parameter across every spawn_run layer.

Effort for a subagent used to resolve ONLY server-side
(``agent.role_efforts['subagent']`` -> chat default), so a parent could not
state the thinking depth its subagents run at without mutating the global
setting. ``spawn_run`` now takes a batch-wide ``reasoning_effort`` that is
plumbed along the exact path ``model`` takes: schema -> tool body ->
``POST /api/spawn`` -> ``SubagentManager.spawn`` -> the ``_run_inner``
resolution site. Each hop is a place the value can be silently dropped
(the queue and retry round-trips especially), so each hop is asserted here.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@contextmanager
def _hermetic_cfg(role_models=None, agent_pins=None):
    """A config the verdict helpers AND the provider factory resolve
    identically on any box: the global model stays the unresolved ``auto``
    sentinel (``_resolve_agent_model`` would otherwise read the installed
    ``~/.kiro/agents/kirocrew.json`` from disk) and named-agent pins come from
    *agent_pins* instead of the real agents directory."""
    from kiro_crew.config.loader import AgentConfig, KiroCrewConfig

    pins = dict(agent_pins or {})
    cfg = KiroCrewConfig(agent=AgentConfig(role_models=role_models or {}))
    with (
        patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        patch.object(KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")),
        patch.object(
            KiroCrewConfig,
            "_resolve_named_agent_model",
            staticmethod(lambda agent, agents_dir=None: pins.get(agent, "")),
        ),
    ):
        yield cfg


def _run_tool(args: dict[str, Any]) -> tuple[list[dict], str]:
    """Run spawn_run and return (POSTed bodies, returned text)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool_inner("spawn_run", args)
    return bodies, result


class TestSchema:
    """EFFORT_VALUES is the vocabulary: every level plus '' (unset)."""

    @pytest.mark.parametrize("level", EFFORT_LEVELS)
    def test_each_concrete_level_is_accepted(self, level):
        cleaned = validate_tool_args({"task": "x", "reasoning_effort": level}, SPAWN_RUN_SCHEMA)
        assert cleaned["reasoning_effort"] == level

    def test_empty_string_means_unset_and_is_accepted(self):
        cleaned = validate_tool_args({"task": "x", "reasoning_effort": ""}, SPAWN_RUN_SCHEMA)
        assert cleaned["reasoning_effort"] == ""

    def test_absent_field_cleans_to_none(self):
        cleaned = validate_tool_args({"task": "x"}, SPAWN_RUN_SCHEMA)
        assert cleaned.get("reasoning_effort") is None

    @pytest.mark.parametrize("bad", ["ultra", "LOW", "maximum", "0", "hi gh"])
    def test_unknown_level_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "reasoning_effort": bad}, SPAWN_RUN_SCHEMA)

    @pytest.mark.parametrize("bad", [1, 2.5, True, [], {}])
    def test_non_string_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "reasoning_effort": bad}, SPAWN_RUN_SCHEMA)


class TestSpawnRunToolForwarding:
    """The omit-when-unset wire contract, exactly like ``model``."""

    def test_set_value_is_sent_in_the_body(self):
        bodies, _ = _run_tool({"task": "x", "reasoning_effort": "high"})
        assert len(bodies) == 1
        assert bodies[0]["reasoning_effort"] == "high"

    def test_unset_value_is_omitted_from_the_body(self):
        bodies, _ = _run_tool({"task": "x"})
        assert "reasoning_effort" not in bodies[0]

    def test_value_is_batch_wide(self):
        bodies, _ = _run_tool({"tasks": ["t1", "t2", "t3"], "reasoning_effort": "max"})
        assert len(bodies) == 3
        assert all(b["reasoning_effort"] == "max" for b in bodies)


def _run_tool_with_server_verdicts(
    args: dict[str, Any], responses: list[dict]
) -> tuple[list[dict], str]:
    """Run spawn_run against a fake gateway answering from *responses*."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []
    answers = iter(responses)

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            return next(answers)
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool_inner("spawn_run", args)
    return bodies, result


class TestUnsupportedModelReport:
    """Effort on an incapable model is REPORTED, never a rejection. The
    verdict comes from the SERVER (which knows the resolved model), carried
    on the /api/spawn response as ``effort_dropped`` — the tool renders one
    attributed line per distinct verdict (identical verdicts collapse,
    see TestVerdictCollapse)."""

    def _run_tool_with_server_verdict(
        self, args: dict[str, Any], responses: list[dict]
    ) -> tuple[list[dict], str]:
        return _run_tool_with_server_verdicts(args, responses)

    def test_server_verdict_is_rendered_and_spawn_still_happens(self):
        bodies, result = self._run_tool_with_server_verdict(
            {"task": "x", "model": "deepseek-3.2", "reasoning_effort": "high"},
            [
                {
                    "id": "a1",
                    "effort_dropped": "model 'deepseek-3.2' does not support effort configuration",
                }
            ],
        )
        assert len(bodies) == 1  # dispatched regardless
        assert "does not support effort" in result
        assert "deepseek-3.2" in result
        assert "a1" in result

    def test_no_report_when_model_supports_effort(self):
        bodies, result = self._run_tool_with_server_verdict(
            {"task": "x", "model": "sonnet-test-model", "reasoning_effort": "high"},
            [{"id": "a1"}],
        )
        assert len(bodies) == 1
        assert "does not support effort" not in result

    def test_report_appears_without_a_per_call_model(self):
        """The default case the old client-side guess could not see: no
        per-call model, the server resolves to 'auto' and reports the drop."""
        bodies, result = self._run_tool_with_server_verdict(
            {"task": "x", "reasoning_effort": "high"},
            [
                {
                    "id": "a1",
                    "effort_dropped": "no concrete model is pinned — the model resolves to 'auto'",
                }
            ],
        )
        assert len(bodies) == 1
        assert "dropped for a1" in result
        assert "auto" in result

    def test_batch_reports_are_attributed_per_subagent(self):
        bodies, result = self._run_tool_with_server_verdict(
            {"tasks": ["x", "y"], "reasoning_effort": "max"},
            [
                {"id": "a1", "effort_dropped": "reason-one"},
                {"id": "a2", "effort_dropped": "reason-two"},
            ],
        )
        assert len(bodies) == 2
        assert "dropped for a1: reason-one" in result
        assert "dropped for a2: reason-two" in result

    def test_report_never_shadows_the_error_prefix_on_total_failure(self):
        """SEL and callers test the FIRST line for the 'Error:' prefix
        (mcp_shared logs outcome='failed' iff result.startswith('Error:')),
        so a spawn where nothing started must not lead with the ℹ line."""
        from kiro_crew import mcp_core

        def _reject_post(path: str, body: dict) -> dict:
            return {"error": "capacity reached"}

        with (
            patch.object(mcp_core, "_post", side_effect=_reject_post),
            patch.object(mcp_core, "_resolve_session_key", return_value="dash:1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            result = mcp_core._call_tool_inner(
                "spawn_run",
                {"task": "x", "model": "deepseek-3.2", "reasoning_effort": "high"},
            )
        assert result.startswith("Error:")
        assert "does not support effort" not in result


class TestVerdictCollapse:
    """Identical per-subagent verdicts collapse into ONE line on wide
    fan-outs (#6185). ``reasoning_effort`` and ``model`` are batch-wide, so
    every member of a wide batch usually gets the identical verdict — one
    line per subagent injects N copies of the same text into the calling
    agent's context. Differing verdicts keep their own attributed lines so
    mixed batches lose no attribution; a 1-wide batch renders exactly as
    before."""

    def test_identical_verdicts_collapse_to_one_line_naming_all_ids(self):
        n = 5
        bodies, result = _run_tool_with_server_verdicts(
            {"tasks": [f"t{i}" for i in range(n)], "reasoning_effort": "high"},
            [{"id": f"a{i}", "effort_dropped": "same-reason"} for i in range(n)],
        )
        assert len(bodies) == n
        ids = ", ".join(f"a{i}" for i in range(n))
        assert f"dropped for {ids}: same-reason" in result
        assert result.count("same-reason") == 1

    def test_differing_verdicts_keep_per_id_lines(self):
        """All-distinct verdicts (per-member agent pins can differ within one
        batch) render exactly as before the collapse — one attributed line
        each. Three members so this pin is not a duplicate of the two-member
        attribution test above."""
        bodies, result = _run_tool_with_server_verdicts(
            {"tasks": ["x", "y", "z"], "reasoning_effort": "max"},
            [
                {"id": "a1", "effort_dropped": "reason-one"},
                {"id": "a2", "effort_dropped": "reason-two"},
                {"id": "a3", "effort_dropped": "reason-three"},
            ],
        )
        assert len(bodies) == 3
        assert "dropped for a1: reason-one" in result
        assert "dropped for a2: reason-two" in result
        assert "dropped for a3: reason-three" in result

    def test_single_subagent_output_is_unchanged(self):
        bodies, result = _run_tool_with_server_verdicts(
            {"task": "x", "reasoning_effort": "high"},
            [{"id": "a1", "effort_dropped": "solo-reason"}],
        )
        assert len(bodies) == 1
        assert "dropped for a1: solo-reason" in result

    def test_partial_overlap_collapses_the_shared_group_and_keeps_the_rest(self):
        """2 identical + 1 different: the pair collapses, the odd one keeps
        its own line — full attribution either way."""
        _, result = _run_tool_with_server_verdicts(
            {"tasks": ["x", "y", "z"], "reasoning_effort": "low"},
            [
                {"id": "a1", "effort_dropped": "shared"},
                {"id": "a2", "effort_dropped": "unique"},
                {"id": "a3", "effort_dropped": "shared"},
            ],
        )
        assert "dropped for a1, a3: shared" in result
        assert "dropped for a2: unique" in result
        assert result.count("shared") == 1
        # First-seen group order and in-group dispatch order are documented
        # guarantees of _collapse_effort_verdicts — pin them so a dict/sorted
        # swap cannot silently break the deterministic-output promise.
        assert result.index("dropped for a1, a3: shared") < result.index("dropped for a2: unique")

    def test_identical_applied_notes_collapse_too(self):
        n = 3
        _, result = _run_tool_with_server_verdicts(
            {"tasks": [f"t{i}" for i in range(n)], "reasoning_effort": "xhigh"},
            [{"id": f"a{i}", "effort_applied": "same-note"} for i in range(n)],
        )
        ids = ", ".join(f"a{i}" for i in range(n))
        assert f"applied for {ids} (same-note)" in result
        assert result.count("same-note") == 1


class TestApiSpawnHandler:
    """POST /api/spawn must not lose the cleaned value on the way to spawn()."""

    def _request(self, body: dict) -> tuple[Any, MagicMock]:
        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        mgr.max_concurrent = 4
        state = SimpleNamespace(subagents=mgr)
        request = MagicMock()
        request.app = {"state": state}

        async def _json() -> dict:
            return body

        request.json = _json
        return request, mgr

    @pytest.mark.asyncio
    async def test_value_reaches_spawn(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": "xhigh"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_absent_value_reaches_spawn_as_empty(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == ""

    @pytest.mark.asyncio
    async def test_invalid_value_is_rejected_with_400(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "reasoning_effort": "turbo"})
        resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _mgr():
    from kiro_crew.subagent import SubagentManager

    return SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx())


class TestQueueRoundTrip:
    """A queued spawn must start at the effort its caller chose — the queue
    is where most members of a large fan-out sit, so a drop here is the most
    likely silent regression in this change."""

    def test_queue_entry_carries_the_value(self):
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        info = mgr.spawn("read these files", reasoning_effort="max")
        assert info is not None and info.queued is True
        assert len(mgr._queue) == 1
        assert mgr._queue[0]["reasoning_effort"] == "max"

    def test_drained_spawn_receives_the_value(self):
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        mgr.spawn("validate this finding", reasoning_effort="high")
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> None:
            captured.update(kwargs)

        mgr.spawn = _capture  # type: ignore[method-assign]
        mgr._max_concurrent = 4
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._drain_queue()
        assert captured["reasoning_effort"] == "high"


class TestRecordAndRetry:
    @pytest.mark.asyncio
    async def test_spawn_threads_the_value_onto_info(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing", reasoning_effort="low")
        assert info is not None
        assert info.reasoning_effort == "low"

    @pytest.mark.asyncio
    async def test_default_is_empty_meaning_defer_to_pin(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing")
        assert info is not None
        assert info.reasoning_effort == ""

    @pytest.mark.asyncio
    async def test_retry_re_spawns_at_the_same_effort(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry

        old = SimpleNamespace(
            id="a1",
            task="t",
            _raw_task="t",
            parent_session_key="dash:1",
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="xhigh",
            approval_mode="",
            silent=False,
            include_memory=True,
            include_lessons=True,
            include_project=True,
            done=True,
            outcome="failed",
        )
        mgr = MagicMock()
        mgr.get.return_value = old
        mgr.spawn.return_value = SimpleNamespace(id="a2", done=False, error="")
        state = SimpleNamespace(subagents=mgr)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"agent_id": "a1"}
        await api_spawn_retry(request)
        assert mgr.spawn.call_args.kwargs["reasoning_effort"] == "xhigh"


class TestResolutionPrecedence:
    """The behavior change itself: per-call value -> role pin -> default,
    and a per-call effort forces the dedicated (non-shared) session path
    exactly as a role pin already does."""

    def _run(self, *, info_effort: str = "", role_efforts=None):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        ctx_builder = MagicMock()
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.auto_approve_subagent_tools = False

        captured: dict = {}
        mock_client = MagicMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
            captured.update(kwargs)
            return mock_client, True, False

        sessions.get_or_create = fake_get_or_create

        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        cfg = KiroCrewConfig(agent=AgentConfig(role_efforts=role_efforts or {}))
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        shared = AsyncMock(
            side_effect=AssertionError("shared path taken despite an effort override")
        )
        info = SubagentInfo(
            id="sub1",
            task="test",
            parent_session_key="parent-key",
            reasoning_effort=info_effort,
        )
        with (
            patch.object(runner, "_create_shared_session", shared),
            patch.object(runner, "_should_use_session_sharing", return_value=True),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return captured, shared

    def test_per_call_value_beats_the_role_pin(self):
        captured, shared = self._run(info_effort="max", role_efforts={"subagent": "low"})
        shared.assert_not_called()
        assert captured.get("reasoning_effort_override") == "max"

    def test_pin_still_applies_when_per_call_value_is_absent(self):
        """The no-regression case: an absent per-call value changes nothing."""
        captured, shared = self._run(role_efforts={"subagent": "low"})
        shared.assert_not_called()
        assert captured.get("reasoning_effort_override") == "low"

    def test_per_call_value_forces_dedicated_path_with_no_pin(self):
        captured, shared = self._run(info_effort="high")
        shared.assert_not_called()
        assert captured.get("reasoning_effort_override") == "high"


class TestEffortDropReason:
    """The pure server-side verdict: non-empty exactly when a requested effort
    will be dropped by the factory gate — no false positives, no false
    negatives (design Property 1)."""

    def _reason(
        self, model: str, effort: str, *, role_models=None, agent="", agent_pins=None
    ) -> str:
        from kiro_crew.subagent import effort_drop_reason

        with _hermetic_cfg(role_models=role_models, agent_pins=agent_pins):
            return effort_drop_reason(model, effort, agent)

    @pytest.mark.parametrize("model", ["", "auto", "deepseek-3.2", "sonnet-test-model"])
    def test_no_effort_requested_means_no_reason(self, model):
        assert self._reason(model, "") == ""

    @pytest.mark.parametrize("level", EFFORT_LEVELS)
    def test_every_level_reports_on_unpinned_auto(self, level):
        reason = self._reason("", level)
        assert "auto" in reason
        assert "does not support effort" in reason

    def test_explicit_auto_is_treated_as_unpinned(self):
        assert "auto" in self._reason("auto", "high")

    def test_non_capable_explicit_model_is_named(self):
        reason = self._reason("deepseek-3.2", "high")
        assert "deepseek-3.2" in reason
        assert "does not support effort" in reason

    def test_capable_explicit_model_is_silent(self):
        assert self._reason("sonnet-test-model", "high") == ""

    def test_capable_role_pin_is_silent(self):
        assert self._reason("", "high", role_models={"subagent": "sonnet-test-model"}) == ""

    def test_non_capable_role_pin_is_named(self):
        reason = self._reason("", "high", role_models={"subagent": "deepseek-3.2"})
        assert "deepseek-3.2" in reason

    def test_explicit_model_beats_role_pin_in_the_verdict(self):
        """Resolution mirror: the per-spawn model wins over the pin, so a
        capable explicit model silences a non-capable pin and vice versa."""
        assert (
            self._reason("sonnet-test-model", "high", role_models={"subagent": "deepseek-3.2"})
            == ""
        )
        reason = self._reason("deepseek-3.2", "high", role_models={"subagent": "sonnet-test-model"})
        assert "deepseek-3.2" in reason

    def test_explicit_auto_beats_a_capable_role_pin(self):
        """Regression (round-4 GPT finding): an explicit ``model="auto"`` is a
        truthy override — ``_run_inner`` forwards it verbatim and the factory
        collapses it to unpinned, DROPPING the effort. The old verdict
        normalized it away and reported the role pin as applied — an inverted
        receipt."""
        reason = self._reason("auto", "high", role_models={"subagent": "sonnet-test-model"})
        assert "auto" in reason
        assert "does not support effort" in reason

    def test_named_agent_capable_pin_is_silent(self):
        """Regression (round-4 Opus finding): a named kiro agent whose own
        agents-JSON pins an effort-capable model DOES get the effort — the
        factory resolves that pin and keys the overlay on it. The old verdict
        stopped at the session layer's deliberate ``None`` and reported a
        drop — the inverse of the truth."""
        assert (
            self._reason(
                "", "high", agent="pinned-agent", agent_pins={"pinned-agent": "sonnet-test-model"}
            )
            == ""
        )

    def test_named_agent_non_capable_pin_is_named(self):
        reason = self._reason(
            "", "high", agent="pinned-agent", agent_pins={"pinned-agent": "deepseek-3.2"}
        )
        assert "deepseek-3.2" in reason


class TestNoSpawnSiteDropWarning:
    """The spawn path no longer emits its own drop warning (#6186): the
    provider factory's effort gate (config/loader.py) is the single warning
    authority, covering spawn, dashboard slot, and cron alike. The tool-result
    verdict (effort_dropped/effort_applied) remains the caller-facing signal;
    the operator-facing log line now comes from the gate itself."""

    def _run(
        self,
        *,
        info_effort: str = "",
        info_model: str = "",
        role_efforts=None,
        role_models=None,
    ):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        ctx_builder = MagicMock()
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.auto_approve_subagent_tools = False

        captured: dict = {}
        mock_client = MagicMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
            captured.update(kwargs)
            return mock_client, True, False

        sessions.get_or_create = fake_get_or_create

        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        cfg = KiroCrewConfig(
            agent=AgentConfig(
                role_efforts=role_efforts or {},
                role_models=role_models or {},
            )
        )
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        info = SubagentInfo(
            id="sub1",
            task="test",
            parent_session_key="parent-key",
            model=info_model,
            reasoning_effort=info_effort,
        )
        with (
            patch.object(runner, "_should_use_session_sharing", return_value=False),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return captured

    def _drop_messages(self, caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "will not be applied" in r.getMessage()
        ]

    def test_per_call_effort_on_auto_emits_no_spawn_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
            self._run(info_effort="high")
        assert self._drop_messages(caplog) == []

    def test_role_pin_drop_emits_no_spawn_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
            self._run(role_efforts={"subagent": "low"})
        assert self._drop_messages(caplog) == []

    def test_capable_model_stays_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
            self._run(info_effort="high", info_model="sonnet-test-model")
        assert self._drop_messages(caplog) == []

    def test_no_effort_anywhere_stays_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
            self._run()
        assert self._drop_messages(caplog) == []

    def test_kwarg_still_passed_without_a_spawn_warning(self, caplog):
        """Reporting never alters spawning (design Property 2): the factory
        stays the single dropping authority, so the override kwarg must reach
        session creation — and the spawn path adds no warning of its own."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent"):
            captured = self._run(info_effort="high")
        assert self._drop_messages(caplog) == []
        assert captured.get("reasoning_effort_override") == "high"


class TestApiSpawnEffortDropped:
    """POST /api/spawn success responses carry the server-side verdict as an
    additive, optional ``effort_dropped`` key — computed here because only the
    gateway knows the role pin and session-model chain behind an omitted
    per-call model."""

    def _spawn(self, body: dict, *, role_models=None, crews=None, parent_agent="") -> dict:
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        state = SimpleNamespace(
            subagents=mgr,
            sessions=SimpleNamespace(get_agent=lambda key: parent_agent),
        )
        request = MagicMock()
        request.app = {"state": state}
        request.json = AsyncMock(return_value=body)
        cfg = KiroCrewConfig(agent=AgentConfig(role_models=role_models or {}))
        if crews:
            cfg.agents.update(crews)
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
            patch(
                "kiro_crew.dashboard.handlers.messaging.warm_project_agents_for_spawn",
                AsyncMock(),
            ),
        ):
            resp = asyncio.run(api_spawn(request))
        assert resp.status == 200
        import json as _json

        return _json.loads(resp.body)

    def test_auto_resolution_carries_the_verdict(self):
        data = self._spawn({"task": "x", "reasoning_effort": "high", "parent_session": "d:1"})
        assert "effort_dropped" in data
        assert "auto" in data["effort_dropped"]

    def test_capable_explicit_model_omits_the_key(self):
        data = self._spawn(
            {
                "task": "x",
                "model": "sonnet-test-model",
                "reasoning_effort": "high",
                "parent_session": "d:1",
            }
        )
        assert "effort_dropped" not in data

    def test_non_capable_role_pin_carries_the_verdict(self):
        data = self._spawn(
            {"task": "x", "reasoning_effort": "high", "parent_session": "d:1"},
            role_models={"subagent": "deepseek-3.2"},
        )
        assert "deepseek-3.2" in data.get("effort_dropped", "")

    def test_no_effort_requested_omits_the_key(self):
        data = self._spawn({"task": "x", "parent_session": "d:1"})
        assert "effort_dropped" not in data
        assert data["id"] == "a1"


class TestEffortAppliedNote:
    """The delivery mirror: non-empty exactly when the drop reason is empty
    for a requested effort, naming the resolved model and family key."""

    def _note(self, model: str, effort: str, *, role_models=None, agent="", agent_pins=None) -> str:
        from kiro_crew.subagent import effort_applied_note

        with _hermetic_cfg(role_models=role_models, agent_pins=agent_pins):
            return effort_applied_note(model, effort, agent)

    def _reason(
        self, model: str, effort: str, *, role_models=None, agent="", agent_pins=None
    ) -> str:
        from kiro_crew.subagent import effort_drop_reason

        with _hermetic_cfg(role_models=role_models, agent_pins=agent_pins):
            return effort_drop_reason(model, effort, agent)

    def test_claude_family_names_output_config(self):
        note = self._note("sonnet-test-model", "high")
        assert "sonnet-test-model" in note
        assert "output_config.effort" in note

    def test_gpt_family_names_reasoning(self):
        note = self._note("gpt-5.6-sol", "medium")
        assert "gpt-5.6-sol" in note
        assert "reasoning.effort" in note

    def test_no_effort_requested_means_no_note(self):
        assert self._note("sonnet-test-model", "") == ""

    def test_auto_and_non_capable_stay_empty(self):
        assert self._note("", "high") == ""
        assert self._note("deepseek-3.2", "high") == ""

    def test_capable_role_pin_resolves_into_the_note(self):
        note = self._note("", "high", role_models={"subagent": "sonnet-test-model"})
        assert "sonnet-test-model" in note

    def test_named_agent_pin_resolves_into_the_note(self):
        """Regression (round-4 Opus finding): the note names the named agent's
        own pinned model — the model the factory actually keys the overlay
        on — instead of staying empty on the session layer's ``None``."""
        note = self._note(
            "", "high", agent="pinned-agent", agent_pins={"pinned-agent": "sonnet-test-model"}
        )
        assert "sonnet-test-model" in note
        assert "output_config.effort" in note

    @pytest.mark.parametrize(
        "model", ["", "auto", "deepseek-3.2", "sonnet-test-model", "gpt-5.6-sol"]
    )
    @pytest.mark.parametrize("level", EFFORT_LEVELS)
    def test_exactly_one_of_note_and_reason_for_a_requested_effort(self, model, level):
        """Complementarity (design Property 1): a requested effort is either
        reported dropped or reported applied — never both, never neither."""
        note, reason = self._note(model, level), self._reason(model, level)
        assert (note == "") != (reason == "")


class TestVerdictFactoryParity:
    """The verdict shares the factory's own selection function
    (``KiroCrewConfig.acp_effective_model``); this drives the REAL factory
    across the spawn-side case matrix and pins verdict↔gate agreement, so a
    change that desynchronizes them fails here instead of shipping a false
    ``effort_applied``/``effort_dropped`` receipt (round-4 Design finding)."""

    # (per-spawn model, subagent role pin, agent, named-agent pins)
    CASES = [
        pytest.param("", "", "", {}, id="nothing-pinned-anywhere"),
        pytest.param("auto", "sonnet-test-model", "", {}, id="explicit-auto-beats-pin"),
        pytest.param("sonnet-test-model", "", "", {}, id="capable-per-spawn-model"),
        pytest.param("deepseek-3.2", "sonnet-test-model", "", {}, id="non-capable-beats-pin"),
        pytest.param("", "sonnet-test-model", "", {}, id="role-pin-only"),
        pytest.param(
            "", "", "pinned-agent", {"pinned-agent": "sonnet-test-model"}, id="named-agent-pin"
        ),
        pytest.param(
            "", "", "pinned-agent", {"pinned-agent": "deepseek-3.2"}, id="non-capable-named-pin"
        ),
        pytest.param("", "", "plain-agent", {}, id="named-agent-without-pin"),
    ]

    @pytest.mark.parametrize("model,role_pin,agent,pins", CASES)
    def test_factory_gate_and_verdict_agree(self, model, role_pin, agent, pins, tmp_path):
        from kiro_crew.session import _session_model
        from kiro_crew.subagent import (
            _spawn_effective_model,
            _subagent_default_model,
            effort_applied_note,
            effort_drop_reason,
        )

        role_models = {"subagent": role_pin} if role_pin else {}
        with _hermetic_cfg(role_models=role_models, agent_pins=pins) as cfg:
            # The spawn caller side, exactly as ``_run_inner``/``get_or_create``
            # drive it: an explicit model (or the role pin) is passed verbatim
            # as the session model kwarg; with neither, ``get_or_create``
            # resolves the session chain and forwards the result (possibly
            # ``None``) to the factory as ``model_override``.
            eff_model = model or _subagent_default_model()
            override = eff_model or _session_model(cfg, agent or None)
            with patch("kiro_crew.providers.acp.AcpProvider") as provider_cls:
                factory = cfg.create_provider_factory()
                factory(
                    "parity-key",
                    agent=agent or None,
                    model_override=override,
                    cwd=str(tmp_path),
                    reasoning_effort_override="high",
                )
            kwargs = provider_cls.call_args.kwargs
            gate_model = kwargs["model"] or ""
            gate_applies = bool(kwargs["effort_per_model"])

            resolved = _spawn_effective_model(model, agent)
            drop = effort_drop_reason(model, "high", agent)
            note = effort_applied_note(model, "high", agent)

        assert resolved == gate_model
        assert (note != "") == gate_applies
        assert (drop == "") == gate_applies


class TestAppliedLineRendering:
    """The tool renders the server's effort_applied note per subagent."""

    def _run(self, args: dict[str, Any], responses: list[dict]) -> tuple[list[dict], str]:
        return _run_tool_with_server_verdicts(args, responses)

    def test_applied_line_renders_with_id_and_key(self):
        bodies, result = self._run(
            {"task": "x", "model": "gpt-5.6-sol", "reasoning_effort": "medium"},
            [{"id": "a1", "effort_applied": "gpt-5.6-sol → reasoning.effort"}],
        )
        assert len(bodies) == 1
        assert "applied for a1" in result
        assert "reasoning.effort" in result

    def test_batch_applied_lines_are_attributed_per_subagent(self):
        bodies, result = self._run(
            {"tasks": ["x", "y"], "model": "gpt-5.6-sol", "reasoning_effort": "medium"},
            [
                {"id": "a1", "effort_applied": "note-one"},
                {"id": "a2", "effort_applied": "note-two"},
            ],
        )
        assert len(bodies) == 2
        assert "applied for a1 (note-one)" in result
        assert "applied for a2 (note-two)" in result

    def test_no_applied_line_without_the_key(self):
        bodies, result = self._run({"task": "x", "reasoning_effort": "high"}, [{"id": "a1"}])
        assert "applied for" not in result

    def test_api_spawn_returns_applied_for_capable_model(self):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        state = SimpleNamespace(subagents=mgr, sessions=SimpleNamespace(get_agent=lambda key: ""))
        request = MagicMock()
        request.app = {"state": state}
        request.json = AsyncMock(
            return_value={
                "task": "x",
                "model": "sonnet-test-model",
                "reasoning_effort": "high",
                "parent_session": "d:1",
            }
        )
        cfg = KiroCrewConfig(agent=AgentConfig())
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
            patch(
                "kiro_crew.dashboard.handlers.messaging.warm_project_agents_for_spawn",
                AsyncMock(),
            ),
        ):
            resp = asyncio.run(api_spawn(request))
        import json as _json

        data = _json.loads(resp.body)
        assert "output_config.effort" in data.get("effort_applied", "")
        assert "effort_dropped" not in data

    def test_api_spawn_omits_applied_when_dropped(self):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        state = SimpleNamespace(subagents=mgr, sessions=SimpleNamespace(get_agent=lambda key: ""))
        request = MagicMock()
        request.app = {"state": state}
        request.json = AsyncMock(
            return_value={"task": "x", "reasoning_effort": "high", "parent_session": "d:1"}
        )
        cfg = KiroCrewConfig(agent=AgentConfig())
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
            patch(
                "kiro_crew.dashboard.handlers.messaging.warm_project_agents_for_spawn",
                AsyncMock(),
            ),
        ):
            resp = asyncio.run(api_spawn(request))
        import json as _json

        data = _json.loads(resp.body)
        assert "effort_applied" not in data
        assert "effort_dropped" in data


class TestSessionChainResolution:
    """The verdict mirrors the FULL model chain the factory's effort gate
    sees — including the session layer (crew pin, else non-sentinel global)
    behind an omitted per-call model and role pin."""

    def _reason(self, model: str, effort: str, agent: str = "", *, cfg=None) -> str:
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.subagent import effort_drop_reason

        cfg = cfg or KiroCrewConfig(agent=AgentConfig())
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
            return effort_drop_reason(model, effort, agent)

    def _note(self, model: str, effort: str, agent: str = "", *, cfg=None) -> str:
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.subagent import effort_applied_note

        cfg = cfg or KiroCrewConfig(agent=AgentConfig())
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
            return effort_applied_note(model, effort, agent)

    def _cfg_with_crew(self, crew_model: str):
        from kiro_crew.config.loader import (
            AgentConfig,
            KiroCrewAgentConfig,
            KiroCrewConfig,
        )

        cfg = KiroCrewConfig(agent=AgentConfig())
        cfg.agents["mycrew"] = KiroCrewAgentConfig(model=crew_model)
        return cfg

    def test_crew_pin_carries_the_effort(self):
        """A crew pinned to a capable model gets the level via the session
        chain, so the verdict must say applied — not falsely report a drop."""
        cfg = self._cfg_with_crew("sonnet-test-model")
        assert self._reason("", "high", "mycrew", cfg=cfg) == ""
        assert "sonnet-test-model" in self._note("", "high", "mycrew", cfg=cfg)

    def test_non_capable_crew_pin_is_named(self):
        cfg = self._cfg_with_crew("deepseek-3.2")
        assert "deepseek-3.2" in self._reason("", "high", "mycrew", cfg=cfg)

    def test_concrete_global_model_carries_the_effort(self):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig

        cfg = KiroCrewConfig(agent=AgentConfig(model="sonnet-test-model"))
        assert self._reason("", "high", cfg=cfg) == ""
        assert "sonnet-test-model" in self._note("", "high", cfg=cfg)

    def test_sentinel_global_still_drops(self):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig

        cfg = KiroCrewConfig(agent=AgentConfig(model="auto"))
        assert "auto" in self._reason("", "high", cfg=cfg)

    def test_explicit_model_beats_the_crew_pin(self):
        cfg = self._cfg_with_crew("sonnet-test-model")
        assert "deepseek-3.2" in self._reason("deepseek-3.2", "high", "mycrew", cfg=cfg)

    def test_api_spawn_uses_the_inherited_parent_agent(self):
        """No agent in the request: the verdict judges the agent the session
        will inherit from the parent, mirroring _run_inner."""
        harness = TestApiSpawnEffortDropped()
        from kiro_crew.config.loader import KiroCrewAgentConfig

        data = harness._spawn(
            {"task": "x", "reasoning_effort": "high", "parent_session": "d:1"},
            crews={"parentcrew": KiroCrewAgentConfig(model="sonnet-test-model")},
            parent_agent="parentcrew",
        )
        assert "sonnet-test-model" in data.get("effort_applied", "")
        assert "effort_dropped" not in data


class TestVerdictOffTheEventLoop:
    """api_spawn computes the verdict via asyncio.to_thread: the resolvers
    read config and glob ~/.kiro/agents, which must not block the gateway
    event loop."""

    def test_verdict_resolvers_run_off_the_loop_thread(self):
        import threading

        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.dashboard.handlers import messaging as messaging_mod
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        state = SimpleNamespace(subagents=mgr, sessions=SimpleNamespace(get_agent=lambda key: ""))
        request = MagicMock()
        request.app = {"state": state}
        request.json = AsyncMock(
            return_value={"task": "x", "reasoning_effort": "high", "parent_session": "d:1"}
        )
        cfg = KiroCrewConfig(agent=AgentConfig())
        seen: list[object] = []

        def _recording_drop(model, effort, agent=""):
            seen.append(threading.current_thread())
            return "recorded-drop"

        loop_thread: list[object] = []

        async def _drive():
            loop_thread.append(threading.current_thread())
            return await api_spawn(request)

        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
            patch.object(messaging_mod, "effort_drop_reason", _recording_drop),
            patch(
                "kiro_crew.dashboard.handlers.messaging.warm_project_agents_for_spawn",
                AsyncMock(),
            ),
        ):
            resp = asyncio.run(_drive())
        assert resp.status == 200
        assert seen, "the verdict resolver was never called"
        assert seen[0] is not loop_thread[0], "verdict ran ON the event loop thread"
