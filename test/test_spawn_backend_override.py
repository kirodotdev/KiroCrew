"""The per-spawn ``backend`` (ACP backend override) across every spawn_run layer.

A subagent otherwise runs on the configured default backend with no way for a
parent to run a delegated task on a different harness. ``spawn_run`` takes a batch-wide
``backend`` plumbed along the exact path ``model``/``reasoning_effort`` take:
schema -> tool body -> ``POST /api/spawn`` -> ``SubagentManager.spawn`` ->
``spawn_impl`` (admission) -> the queue round-trip -> ``_run_inner``'s
``extra_kwargs`` -> the provider factory's ``backend_override`` seam. Each hop
is a place the value can be silently dropped, so each hop is asserted here.

The provider factory's ``backend_override`` param is added by a sibling task
(the per-chat backend arm). This suite is independent of it: the pass-through
test stubs ``get_or_create`` at the SessionManager seam and asserts the kwarg
arrives there, rather than driving the real factory. Selectability is asserted
against ``selectable_backends()``; ``kas`` and ``""`` (kiro) are in the default
selectable baseline and ``deepseek`` is deliberately NOT (routing UNVERIFIED),
so it is the natural unselectable-but-well-formed refusal case.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args


@pytest.fixture(autouse=True)
def _deepseek_unselectable():
    """These suites need a KNOWN but UNSELECTABLE backend to exercise the
    degrade/refuse branches, and ``deepseek`` -- known, and outside the shipped
    selectable set until its gate plugin landed -- is the id they were written
    around. Withdraw it from the live selectable set for the test and restore the
    set afterwards, so the branch under test still has a value to take."""
    from kiro_crew.acp.types import ACP_BACKEND_DEEPSEEK
    from kiro_crew.agent_sdk import backends as _b

    before = set(_b._selectable)
    _b._selectable.discard(ACP_BACKEND_DEEPSEEK)
    yield
    _b._selectable.clear()
    _b._selectable.update(before)


# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


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
    """The wire grammar: a well-formed backend id is accepted, malformed
    rejected. Selectability is NOT a schema concern (the registry decides it,
    live) -- the schema only shape-checks the id."""

    @pytest.mark.parametrize(
        "value", ["kas", "deepseek", "acme", "kiro", "a", "x-y-z", "-lead", "trail-", "a" * 32]
    )
    def test_wellformed_ids_are_accepted(self, value):
        # Hyphen-edged ids ride along on purpose: the descriptor grammar and the
        # registrar (``_REGISTERABLE_BACKEND_ID``) accept them, so a descriptor
        # named ``acme-`` registers and is selectable -- a stricter wire pattern
        # here would make that same id unspawnable.
        cleaned = validate_tool_args({"task": "x", "backend": value}, SPAWN_RUN_SCHEMA)
        assert cleaned["backend"] == value

    def test_the_wire_grammar_is_the_registrar_grammar(self):
        """One grammar for 'can register' and 'can be named on the wire'."""
        from kiro_crew import validation
        from kiro_crew.agent_sdk import backends as b

        assert validation._BACKEND_ID_RE.pattern == b._REGISTERABLE_BACKEND_ID.pattern

    def test_empty_string_means_inherit_and_is_accepted(self):
        cleaned = validate_tool_args({"task": "x", "backend": ""}, SPAWN_RUN_SCHEMA)
        assert cleaned["backend"] == ""

    def test_absent_field_cleans_to_none(self):
        cleaned = validate_tool_args({"task": "x"}, SPAWN_RUN_SCHEMA)
        assert cleaned.get("backend") is None

    @pytest.mark.parametrize("bad", ["KAS", "has space", "under_score", "a..b", "a" * 33])
    def test_malformed_id_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "backend": bad}, SPAWN_RUN_SCHEMA)

    @pytest.mark.parametrize("bad", [1, 2.5, True, [], {}])
    def test_non_string_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "backend": bad}, SPAWN_RUN_SCHEMA)


class TestSpawnRunToolForwarding:
    """The omit-when-unset wire contract, exactly like ``model``.

    A one-task call has to get past the solo gate to post anything: a NAMED
    backend does that on its own (it is a difference from the caller's own
    session, the same class as ``model``); the unset case names nothing, so
    it carries a ``solo_reason`` -- the test is about the body, not the gate.
    """

    def test_set_value_is_sent_in_the_body(self):
        bodies, _ = _run_tool({"task": "x", "backend": "kas"})
        assert len(bodies) == 1
        assert bodies[0]["backend"] == "kas"

    def test_a_named_backend_alone_opens_the_solo_gate(self):
        """No reason, no model/agent/crew: the backend is the difference."""
        bodies, text = _run_tool({"task": "x", "backend": "kas"})
        assert len(bodies) == 1
        assert not text.startswith("Error:")
        assert bodies[0].get("solo") is True

    def test_unset_value_is_omitted_from_the_body(self):
        bodies, _ = _run_tool({"task": "x", "solo_reason": "bulk_data"})
        assert len(bodies) == 1
        assert "backend" not in bodies[0]

    def test_value_is_batch_wide(self):
        bodies, _ = _run_tool({"tasks": ["t1", "t2", "t3"], "backend": "kas"})
        assert len(bodies) == 3
        assert all(b["backend"] == "kas" for b in bodies)


class TestSoloGateRosterCheckBackend:
    """Gateway half of the solo gate, backend arm: a lone spawn that names the
    parent's OWN backend is refused (``""``), another backend is the ground
    ``"backend"``, and an un-comparable parent fails OPEN and says so.

    Three values are kept apart on purpose: ``None`` = "no pin / unknown",
    ``""`` = kiro-cli (its own id), ``"kas"`` etc. = another harness.
    """

    _NO_SLOT = object()

    @classmethod
    def _state(cls, pinned: Any) -> SimpleNamespace:
        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "kirocrew")
        sessions.get_agent.return_value = "kirocrew"
        slots: dict[str, Any] = {}
        if pinned is not cls._NO_SLOT:
            slots["chat-1"] = SimpleNamespace(acp_backend=pinned, model="", key="chat-1")
        return SimpleNamespace(sessions=sessions, _slots=slots)

    def _diff(
        self, state: SimpleNamespace, backend: str | None, *, default: str | None = None
    ) -> str:
        from kiro_crew.solo_spawn import solo_spawn_difference

        with patch(
            "kiro_crew.dashboard.chat_utils.effective_session_key", return_value="dashboard:chat-1"
        ):
            return solo_spawn_difference(
                state, "dashboard:chat-1", backend=backend, configured_default_backend=default
            )

    def test_same_backend_as_the_pinned_slot_does_not_differ(self):
        assert self._diff(self._state("kas"), "kas") == ""

    def test_another_backend_than_the_pinned_slot_differs(self):
        assert self._diff(self._state("kas"), "claude") == "backend"

    def test_unpinned_slot_compares_against_the_deployment_default(self):
        # The default is SUPPLIED by the caller (loaded off the event loop in the
        # handler); the synchronous check itself never reads config.
        assert self._diff(self._state(None), "kas", default="kas") == ""
        assert self._diff(self._state(None), "claude", default="kas") == "backend"

    def test_the_check_never_loads_config_itself(self):
        # Blocking file I/O on the loop is what the caller's to_thread avoids; a
        # config load from inside the check would put it right back.
        with patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            side_effect=AssertionError("config must not be loaded on the event loop"),
        ):
            assert self._diff(self._state(None), "kas", default="kas") == ""
            # No default supplied for an unpinned parent: unknown, fail open.
            assert self._diff(self._state(None), "kas") == "backend (parent unknown)"

    def test_kiro_parent_is_comparable(self):
        # A parent on kiro -- pinned to "" or unpinned on a kiro default -- is a
        # KNOWN backend, so a spawn naming kiro is the parent's own and a spawn
        # naming another harness differs. "" is not "unknown".
        assert self._diff(self._state(""), "") == ""
        assert self._diff(self._state(""), "kas") == "backend"
        assert self._diff(self._state(None), "", default="") == ""
        assert self._diff(self._state(None), "kas", default="") == "backend"

    def test_unknown_parent_fails_open_and_says_so(self):
        # No slot at all: nothing to compare.
        assert self._diff(self._state(self._NO_SLOT), "kas") == "backend (parent unknown)"

    def test_nothing_named_does_not_differ(self):
        # None is "no backend named"; "" would be a NAMED backend (kiro).
        assert self._diff(self._state("kas"), None) == ""
        assert self._diff(self._state("kas"), "") == "backend"


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
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


class TestAdmissionValidation:
    """A non-empty ``acp_backend`` is validated against ``selectable_backends()``
    at spawn admission -- a refusal at spawn time, naming the selectable set,
    not a silent degrade deep in provider construction. Empty inherits the
    parent's backend and is never checked."""

    def test_unselectable_backend_is_refused_with_named_set(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        # ``deepseek`` is well-formed (passes the schema) but NOT selectable
        # (routing UNVERIFIED, absent from the baseline), so admission refuses it.
        info = mgr.spawn("do the thing", acp_backend="deepseek")
        assert info is not None
        assert info.done and info.error
        assert "deepseek" in info.error
        assert "not selectable" in info.error
        # The refusal names the set the caller may choose from.
        assert "kas" in info.error
        mgr._run.assert_not_called()

    # mgr.spawn() schedules the run task, so these need a running loop — same
    # pattern as test_spawn_reasoning_effort.py:407.
    @pytest.mark.asyncio
    async def test_selectable_backend_is_admitted(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing", acp_backend="kas")
        assert info is not None
        assert not info.done  # started, not refused
        assert info.acp_backend == "kas"

    @pytest.mark.asyncio
    async def test_absent_backend_is_never_checked(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing")
        assert info is not None
        assert not info.done
        assert info.acp_backend is None


class TestQueueRoundTrip:
    """A queued spawn must start on the backend its caller chose -- the queue
    is where most members of a large fan-out sit, so a drop here is the most
    likely silent regression. The docstring on ``queue_params`` warns a field
    missing there is a scope the run silently regains."""

    def test_queue_entry_carries_the_value(self):
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        info = mgr.spawn("read these files", acp_backend="kas")
        assert info is not None and info.queued is True
        assert len(mgr._queue) == 1
        assert mgr._queue[0]["acp_backend"] == "kas"

    def test_drained_spawn_receives_the_value(self):
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        mgr.spawn("validate this finding", acp_backend="kas")
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> None:
            captured.update(kwargs)

        mgr.spawn = _capture  # type: ignore[method-assign]
        mgr._max_concurrent = 4
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._drain_queue()
        assert captured["acp_backend"] == "kas"


class TestRecordOntoInfo:
    # These call mgr.spawn(), which schedules the run task and therefore needs a
    # running loop — the sibling suite (test_spawn_reasoning_effort.py:407)
    # marks the identical shape @pytest.mark.asyncio; mirrored here.
    @pytest.mark.asyncio
    async def test_spawn_threads_the_value_onto_info(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing", acp_backend="kas")
        assert info is not None
        assert info.acp_backend == "kas"

    @pytest.mark.asyncio
    async def test_default_is_none_meaning_inherit(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing")
        assert info is not None
        assert info.acp_backend is None


class TestForcingRuleAndDisqualifier:
    """A per-spawn backend override forces the dedicated-process path, exactly
    as a model / reasoning-effort override does: the parent's shared runtime
    was started on its own backend and cannot serve a different one."""

    def _run(self, *, info_backend: str | None = None):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
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

        cfg = KiroCrewConfig(agent=AgentConfig())
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        # A shared-path attempt is a bug when a backend override is set: the
        # side effect fails loudly if the forcing rule did not disable sharing.
        shared = AsyncMock(
            side_effect=AssertionError("shared path taken despite a backend override")
        )
        _exec = ExecutionContext(None, MemoryStoreRef("default"), "template", "")
        info = SubagentInfo(
            id="sub1",
            task="test",
            parent_session_key="parent-key",
            acp_backend=info_backend,
            execution_context=_exec,
        )
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("sub1", execution_context=_exec)
        with (
            patch.object(runner, "_create_shared_session", shared),
            patch.object(runner, "_should_use_session_sharing", return_value=True),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "sub1"))
        return captured, shared

    def test_backend_override_alone_forces_dedicated_path(self):
        captured, shared = self._run(info_backend="kas")
        shared.assert_not_called()
        # And it reaches the provider factory seam as backend_override.
        assert captured.get("backend_override") == "kas"

    def test_absent_backend_leaves_shared_path_available(self):
        """No override ⇒ the forcing rule does not fire; the shared path is
        chosen (the AssertionError side effect proves it was taken) and no
        backend_override kwarg is passed."""
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
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

        cfg = KiroCrewConfig(agent=AgentConfig())
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        shared = AsyncMock(return_value=mock_client)
        _exec = ExecutionContext(None, MemoryStoreRef("default"), "template", "")
        info = SubagentInfo(
            id="sub1",
            task="test",
            parent_session_key="parent-key",
            execution_context=_exec,
        )
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("sub1", execution_context=_exec)
        with (
            patch.object(runner, "_create_shared_session", shared),
            patch.object(runner, "_should_use_session_sharing", return_value=True),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "sub1"))
        shared.assert_awaited()  # the shared path WAS taken
        assert "backend_override" not in captured

    def test_disqualifier_excludes_a_backend_override_from_sharing(self):
        """``_should_use_session_sharing`` itself must reject an info with a
        backend override, independent of the forcing rule in _run_inner (defence
        in depth: the two guards agree)."""
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.is_session_sharing_eligible = MagicMock(return_value=True)
        runner = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx())
        cfg = KiroCrewConfig(agent=AgentConfig(session_sharing=True))
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
            with_override = SubagentInfo(
                id="s1", task="t", parent_session_key="p", acp_backend="kas"
            )
            without = SubagentInfo(id="s2", task="t", parent_session_key="p")
            assert runner._should_use_session_sharing(with_override) is False
            # The control: with everything else equal, no override IS eligible,
            # so the False above is caused by the backend override specifically.
            assert runner._should_use_session_sharing(without) is True


class TestApiSpawnHandler:
    """POST /api/spawn must not lose the cleaned value on the way to spawn()."""

    def _request(self, body: dict) -> tuple[Any, MagicMock]:
        mgr = MagicMock()
        mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
        mgr.max_concurrent = 4
        state = SimpleNamespace(subagents=mgr, conversation_log=MagicMock())
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {}

        async def _json() -> dict:
            return body

        request.json = _json
        return request, mgr

    @pytest.mark.asyncio
    async def test_value_reaches_spawn_as_acp_backend(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "backend": "kas"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["acp_backend"] == "kas"

    @pytest.mark.asyncio
    async def test_absent_value_reaches_spawn_as_none(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["acp_backend"] is None

    @pytest.mark.asyncio
    async def test_malformed_backend_is_rejected_with_400(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = self._request({"task": "x", "backend": "NOT VALID"})
        resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()
