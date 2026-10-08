"""``spawn_run(backend=...)``: a sub-agent starts on the harness its caller names.

One path, pinned end to end: the tool's schema and wire body, the spawn
endpoint's refusal of a backend this gateway cannot serve, the manager carrying
the value through the queue, the run handing it to the provider factory (and
off the shared runtime, which is the parent's process), the factory's selection
gate, the warm pool, and the follow-ups -- a continuation and a retry run where
the original ran.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.execution_context import execution_for_store
from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


@pytest.fixture
def selectable(monkeypatch):
    """Pin the selectable set to Kiro, Claude, Codex and Pi, and this machine's
    installs: Pi is allowed but missing, Codex's probe could not decide."""
    from kiro_crew import acp_backends
    from kiro_crew.agent_sdk import backend_install
    from kiro_crew.agent_sdk.backend_install import (
        INSTALLED,
        MISSING,
        UNKNOWN,
        BackendInstallState,
    )

    values = ["", "claude", "codex", "pi", "kas"]
    installs = {
        "": INSTALLED,
        "claude": INSTALLED,
        "codex": UNKNOWN,
        "pi": MISSING,
        "kas": INSTALLED,
    }
    monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: sorted(values))
    monkeypatch.setattr(
        backend_install,
        "probe_backend",
        lambda b: BackendInstallState(
            b,
            b or "kiro",
            installs[b],
            missing_components=("pi-acp",) if installs[b] == MISSING else (),
            install_command="npm i -g pi-acp" if installs[b] == MISSING else "",
            # KAS is on disk but was installed after the gateway started.
            restart_required=b == "kas",
        ),
    )
    return values


# ── Schema ────────────────────────────────────────────────────────────────────


class TestSchema:
    @pytest.mark.parametrize("name", ["kiro", "claude", "codex", "open-code_2"])
    def test_a_backend_name_is_accepted(self, name):
        cleaned = validate_tool_args({"task": "x", "backend": name}, SPAWN_RUN_SCHEMA)
        assert cleaned["backend"] == name

    @pytest.mark.parametrize("bad", ["Claude", "co dex", "../kiro", "-x", 3, True])
    def test_a_malformed_name_is_refused(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "backend": bad}, SPAWN_RUN_SCHEMA)

    def test_a_task_entry_may_name_its_own_backend(self):
        cleaned = validate_tool_args(
            {"tasks": [{"task": "x", "backend": "codex"}]}, SPAWN_RUN_SCHEMA
        )
        assert cleaned["tasks"][0]["backend"] == "codex"


# ── The tool's wire body ──────────────────────────────────────────────────────


def _run_tool(args: dict[str, Any]) -> list[dict]:
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": f"a{len(bodies)}"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        mcp_core._call_tool_inner("spawn_run", args)
    return bodies


class TestToolForwarding:
    def test_the_backend_is_sent_batch_wide(self):
        bodies = _run_tool({"tasks": ["t1", "t2"], "backend": "claude"})
        assert [b["backend"] for b in bodies] == ["claude", "claude"]

    def test_a_task_entry_overrides_the_batch_backend(self):
        bodies = _run_tool(
            {"tasks": [{"task": "t1", "backend": "codex"}, "t2"], "backend": "claude"}
        )
        assert [b["backend"] for b in bodies] == ["codex", "claude"]

    def test_no_backend_sends_none(self):
        bodies = _run_tool({"tasks": ["t1", "t2"]})
        assert all("backend" not in b for b in bodies)

    def test_the_tool_advertises_the_field(self):
        from kiro_crew.mcp_tools.spawn import schemas

        spec = next(t for t in schemas() if t["name"] == "spawn_run")
        props = spec["inputSchema"]["properties"]
        assert "backend" in props
        assert "backend" in props["tasks"]["items"]["properties"]

    def test_a_not_installed_refusal_shows_its_install_command(self):
        from kiro_crew import mcp_core

        refusal = {
            "error": "backend 'codex' is not installed on this machine",
            "code": "backend_not_installed",
            "install_command": "npm i -g @agentclientprotocol/codex-acp",
        }
        with (
            patch.object(mcp_core, "_post", return_value=refusal),
            patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            out = mcp_core._call_tool_inner("spawn_run", {"task": "t1", "backend": "codex"})
        assert "npm i -g @agentclientprotocol/codex-acp" in str(out)


# ── The spawn endpoint ────────────────────────────────────────────────────────


def _request(body: dict) -> tuple[Any, MagicMock]:
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


class TestSpawnEndpoint:
    @pytest.mark.asyncio
    async def test_a_selectable_backend_reaches_spawn(self, selectable):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x", "backend": "claude"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["backend"] == "claude"

    @pytest.mark.asyncio
    async def test_kiro_is_spelled_by_name_and_reaches_spawn_as_its_id(self, selectable):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x", "backend": "kiro"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["backend"] == ""

    @pytest.mark.asyncio
    async def test_no_backend_reaches_spawn_as_none(self, selectable):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["backend"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["goose", "nonesuch"])
    async def test_an_unavailable_backend_is_refused_with_the_list(self, selectable, name):
        # "goose" is a backend this build knows but this gateway does not allow;
        # "nonesuch" is not a backend at all. Neither may degrade to the default.
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x", "backend": name})
        resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()
        import json

        payload = json.loads(resp.body)
        assert payload["code"] == "unknown_backend"
        assert payload["backends"] == ["claude", "codex", "kiro"]

    @pytest.mark.asyncio
    async def test_an_allowed_backend_that_is_not_installed_is_refused_with_its_install(
        self, selectable
    ):
        # Allowed by governance is not startable: refused up front, with the
        # command that installs it, instead of failing at cold start.
        import json

        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x", "backend": "pi"})
        resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()
        payload = json.loads(resp.body)
        assert payload["code"] == "backend_not_installed"
        assert payload["install_command"] == "npm i -g pi-acp"
        assert "pi" not in payload["backends"]

    @pytest.mark.asyncio
    async def test_a_backend_installed_after_the_gateway_started_is_refused(self, selectable):
        # The gateway still holds the cached absence, so the start would fail.
        import json

        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x", "backend": "kas"})
        resp = await api_spawn(request)
        assert resp.status == 400
        mgr.spawn.assert_not_called()
        payload = json.loads(resp.body)
        assert payload["code"] == "backend_restart_required"
        assert "kas" not in payload["backends"]

    @pytest.mark.asyncio
    async def test_a_probe_that_could_not_decide_does_not_block(self, selectable):
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        request, mgr = _request({"task": "x", "backend": "codex"})
        await api_spawn(request)
        assert mgr.spawn.call_args.kwargs["backend"] == "codex"


# ── The manager ───────────────────────────────────────────────────────────────


def _mgr():
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return SubagentManager(sessions=sessions, ctx_builder=ctx)


class TestManager:
    @pytest.mark.asyncio
    async def test_spawn_records_the_backend_on_the_run(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing", backend="codex")
        assert info is not None and info.backend == "codex"

    @pytest.mark.asyncio
    async def test_no_backend_is_none(self):
        mgr = _mgr()
        mgr._run = AsyncMock()  # type: ignore[method-assign]
        info = mgr.spawn("do the thing")
        assert info is not None and info.backend is None

    def test_a_queued_spawn_starts_on_its_backend(self):
        mgr = _mgr()
        mgr._should_stagger_queue = MagicMock(return_value=(True, False))  # type: ignore[method-assign]
        mgr.spawn("validate this finding", backend="claude")
        assert mgr._queue[0]["backend"] == "claude"
        captured: dict[str, object] = {}
        mgr.spawn = lambda **kw: captured.update(kw)  # type: ignore[method-assign]
        mgr._max_concurrent = 4
        mgr._running_count = 0
        mgr._spawn_stagger_secs = 0.0
        mgr._drain_queue()
        assert captured["backend"] == "claude"


# ── The run hands it to the factory ───────────────────────────────────────────


def _run(backend: str | None) -> tuple[dict, AsyncMock]:
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
    client = MagicMock()

    async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
        captured.update(kwargs)
        return client, True, False

    async def fake_stream(msg):
        yield LLMEvent(kind=EVENT_COMPLETE)

    sessions.get_or_create = fake_get_or_create
    client.stream = fake_stream
    cfg = KiroCrewConfig(agent=AgentConfig(session_sharing=True))
    runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
    shared = AsyncMock(side_effect=AssertionError("shared runtime used for a per-spawn backend"))
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="sub1",
        task="test",
        parent_session_key="parent-key",
        backend=backend,
    )
    runner._log_spawned(info)
    sessions.is_session_sharing_eligible = MagicMock(return_value=True)
    with (
        patch.object(runner, "_create_shared_session", shared),
        patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
    ):
        asyncio.run(runner._run_inner(info, "subagent:sub1"))
    return captured, shared


class TestRun:
    @pytest.mark.parametrize("backend", ["claude", ""])
    def test_the_backend_reaches_the_factory_on_a_process_of_its_own(self, backend):
        # ``""`` (Kiro) is a request too: under a non-Kiro default it must reach
        # the factory, or the run starts on the default harness.
        captured, shared = _run(backend)
        shared.assert_not_called()
        assert captured["backend_override"] == backend

    def test_no_backend_passes_nothing(self):
        captured, _ = _run(None)
        assert "backend_override" not in captured

    def test_sharing_is_ruled_out_by_a_backend(self):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
        from kiro_crew.subagent import SubagentInfo

        mgr = _mgr()
        mgr._sessions.is_session_sharing_eligible = MagicMock(return_value=True)
        cfg = KiroCrewConfig(agent=AgentConfig(session_sharing=True))
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
            plain = SubagentInfo(id="s1", task="t", parent_session_key="p")
            picked = SubagentInfo(id="s2", task="t", parent_session_key="p", backend="codex")
            assert mgr._should_use_session_sharing(plain) is True
            assert mgr._should_use_session_sharing(picked) is False


# ── The factory's selection gate ──────────────────────────────────────────────


class TestSelectionGate:
    def test_the_override_beats_the_member_route_and_the_default(self, monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.members import select_provider_backend

        monkeypatch.setattr(backends, "selectable_backends", lambda: {"", "claude", "kas"})
        assert select_provider_backend("dashboard:dm-x", "kas", "", backend_override="claude") == (
            "claude"
        )

    def test_kiro_is_an_override_not_unset(self, monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.members import select_provider_backend

        monkeypatch.setattr(backends, "selectable_backends", lambda: {"", "claude"})
        assert select_provider_backend("subagent:a", "", "claude", backend_override="") == ""

    def test_no_override_leaves_selection_as_it_was(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend("subagent:a", "", "claude") == "claude"

    def test_an_override_that_stopped_being_selectable_degrades_to_kiro(self, monkeypatch):
        from kiro_crew.agent_sdk import backends
        from kiro_crew.members import select_provider_backend

        monkeypatch.setattr(backends, "selectable_backends", lambda: {""})
        assert select_provider_backend("subagent:a", "", "", backend_override="codex") == ""

    def test_the_factory_names_the_kwarg(self):
        # Named, not left to ``**_kwargs``: swallowed there, the session would
        # start on the default backend with no error.
        from kiro_crew.config.loader import KiroCrewConfig

        src = inspect.getsource(KiroCrewConfig.create_provider_factory)
        assert "backend_override: str | None = None" in src
        assert "backend_override=backend_override" in src


class TestWarmPool:
    def test_a_backend_override_skips_the_warm_pool(self):
        from kiro_crew import session_allocation

        src = inspect.getsource(session_allocation)
        assert 'pool_decision = "bypass_backend_override"' in src
        assert 'extra_factory_kwargs.get("backend_override") is not None' in src


class TestSuccessorsKeepTheBackend:
    """A reset successor and a compaction restart rebuild from ``allocation_identity``:
    a session started on a per-spawn backend must come back on the same one."""

    def _session(self, backend):
        from kiro_crew.session import _Session

        session = _Session.__new__(_Session)
        session.agent = "kirocrew"
        session.approval_policy = ""
        session.provider = SimpleNamespace(cwd="/w")
        session.requested_model = ""
        session.capability_member = ""
        session.backend_override = backend
        return session

    @pytest.mark.parametrize("backend", ["claude", ""])
    def test_the_identity_carries_the_backend(self, backend):
        from kiro_crew.session_lifecycle import allocation_identity

        owner = SimpleNamespace(get_channel=lambda key: "")
        ident = allocation_identity(owner, "subagent:a", self._session(backend))
        assert ident["backend_override"] == backend

    def test_a_session_without_one_rebuilds_with_the_old_kwargs(self):
        from kiro_crew.session_lifecycle import allocation_identity

        owner = SimpleNamespace(get_channel=lambda key: "")
        ident = allocation_identity(owner, "subagent:a", self._session(None))
        assert "backend_override" not in ident

    def test_registration_stamps_the_backend_the_session_started_on(self):
        from kiro_crew import session_allocation

        src = inspect.getsource(session_allocation)
        assert 'session.backend_override = extra_factory_kwargs.get("backend_override")' in src


# ── The effort verdict is judged on the requested backend ────────────────────


class TestEffortVerdict:
    def test_a_backend_that_advertises_its_effort_gets_no_verdict(self):
        # Pi reports its levels at session/new; the registry cannot judge them,
        # so neither line may be printed -- a "dropped" there was a false report.
        from kiro_crew.subagent import effort_applied_note, effort_drop_reason

        assert effort_drop_reason("pi-model", "high", backend="pi") == ""
        assert effort_applied_note("pi-model", "high", backend="pi") == ""

    def test_the_applied_note_names_the_channel_the_backend_uses(self, monkeypatch):
        # kiro-cli reads the level from cli.json; an adapter harness gets it as a
        # live config option -- the receipt must name the one that delivers it.
        from kiro_crew import subagent
        from kiro_crew.acp_backends import effort_config_option_id

        monkeypatch.setattr(subagent, "_spawn_effective_model", lambda *a, **kw: "gpt-5")
        monkeypatch.setattr(subagent, "model_supports_effort", lambda m: True)
        codex = subagent.effort_applied_note("gpt-5", "high", backend="codex")
        assert codex == f"gpt-5 → config option {effort_config_option_id('codex')}"
        kiro = subagent.effort_applied_note("gpt-5", "high", backend="")
        assert kiro.endswith(".effort") and "config option" not in kiro
        assert subagent.effort_applied_note("gpt-5", "high") == kiro

    @pytest.mark.parametrize("backend", ["opencode", "goose"])
    def test_a_backend_that_takes_no_effort_is_reported_dropped(self, monkeypatch, backend):
        # Neither cli.json nor a config option reaches these harnesses: the level
        # is never sent, so the receipt must say dropped, not applied.
        from kiro_crew import subagent

        monkeypatch.setattr(subagent, "_spawn_effective_model", lambda *a, **kw: "claude-sonnet")
        monkeypatch.setattr(subagent, "model_supports_effort", lambda m: True)
        assert subagent.effort_applied_note("claude-sonnet", "high", backend=backend) == ""
        assert backend in subagent.effort_drop_reason("claude-sonnet", "high", backend=backend)

    def test_the_model_is_translated_into_the_requested_backends_namespace(self, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.subagent import _spawn_effective_model

        seen: dict = {}

        def _effective(self, agent, override, global_model=None, namespace=None):
            seen["namespace"] = namespace
            return "m"

        monkeypatch.setattr(KiroCrewConfig, "acp_effective_model", _effective)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda c: KiroCrewConfig()))
        from kiro_crew.agent_sdk.capabilities import capabilities_for

        assert _spawn_effective_model("m", "", crew_agent="", backend="claude") == "m"
        assert seen["namespace"] == capabilities_for("claude").model_id_namespace
        _spawn_effective_model("m", "", crew_agent="")
        assert seen["namespace"] is None

    @pytest.mark.asyncio
    async def test_the_endpoint_hands_the_backend_to_the_verdict(self, selectable, monkeypatch):
        from kiro_crew.dashboard.handlers import messaging

        calls: list = []
        monkeypatch.setattr(
            messaging, "effort_drop_reason", lambda *a, **kw: calls.append(kw) or ""
        )
        monkeypatch.setattr(messaging, "effort_applied_note", lambda *a, **kw: "")
        request, _mgr = _request({"task": "x", "backend": "claude", "reasoning_effort": "high"})
        request.app["state"].sessions = MagicMock()
        await messaging.api_spawn(request)
        assert calls and calls[0]["backend"] == "claude"


# ── Follow-ups run where the original ran ─────────────────────────────────────


class TestFollowUps:
    def _continue(self, mgr, monkeypatch, conv_id: str) -> dict:
        monkeypatch.setattr(mgr, "_conversation_busy", lambda _k: None)
        monkeypatch.setattr(mgr._sessions, "resumable_sid", lambda _k: "sid-1")
        monkeypatch.setattr(mgr, "_promote_conversation", lambda *_a: None)
        captured: dict[str, object] = {}
        monkeypatch.setattr(mgr, "spawn", lambda *_a, **kw: captured.update(kw))
        mgr.continue_conversation(conv_id, "follow up")
        return captured

    def test_a_continuation_inherits_the_live_runs_backend(self, monkeypatch):
        from kiro_crew.subagent import SubagentInfo

        mgr = _mgr()
        mgr._agents["conv1"] = SubagentInfo(id="conv1", task="t", backend="codex")
        assert self._continue(mgr, monkeypatch, "conv1")["backend"] == "codex"

    def test_a_continuation_after_a_restart_reads_the_recorded_backend(self, monkeypatch):
        mgr = _mgr()
        monkeypatch.setattr("kiro_crew.subagent.read_state", lambda _id: {"backend": "claude"})
        monkeypatch.setattr(mgr, "_inherited_memory_store", lambda _id: "")
        monkeypatch.setattr(
            mgr._continuation._persistence, "read_run_app", lambda _id: "", raising=True
        )
        assert self._continue(mgr, monkeypatch, "gone")["backend"] == "claude"

    def test_a_run_predating_the_field_continues_on_the_default(self, monkeypatch):
        from kiro_crew.subagent import SubagentInfo

        mgr = _mgr()
        mgr._agents["conv2"] = SubagentInfo(id="conv2", task="t")
        assert self._continue(mgr, monkeypatch, "conv2")["backend"] is None

    def test_the_spawn_writes_the_backend_into_the_run_record(self, tmp_path, monkeypatch):
        from kiro_crew import subagent_persistence
        from kiro_crew.subagent import SubagentInfo

        monkeypatch.setattr(subagent_persistence, "_subagents_dir", lambda: tmp_path)
        mgr = _mgr()
        info = SubagentInfo(
            execution_context=execution_for_store(""), id="r3", task="t", backend="codex"
        )
        mgr._log_spawned(info)
        assert subagent_persistence.read_state("r3")["backend"] == "codex"

    def test_the_run_record_carries_the_backend(self, tmp_path, monkeypatch):
        from kiro_crew import subagent_persistence

        monkeypatch.setattr(subagent_persistence, "_subagents_dir", lambda: tmp_path)
        subagent_persistence.create_agent_folder("r1", task="t", backend="codex")
        assert subagent_persistence.read_state("r1")["backend"] == "codex"
        subagent_persistence.create_agent_folder("r2", task="t")
        assert "backend" not in subagent_persistence.read_state("r2")

    @pytest.mark.asyncio
    async def test_a_retry_runs_on_the_original_backend(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        old = SimpleNamespace(
            id="a1",
            task="t",
            _raw_task="t",
            parent_session_key="dash:1",
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="",
            backend="claude",
            approval_mode="",
            silent=False,
            delegation={},
            include_memory=True,
            include_lessons=True,
            include_project=True,
            memory_store="",
            crew="",
            done=True,
            outcome="failed",
            execution_context=ExecutionContext(None, MemoryStoreRef("default"), "template", "k"),
        )
        mgr = MagicMock()
        mgr.get.return_value = old
        mgr.spawn.return_value = SimpleNamespace(id="a2", done=False, error="")
        request = MagicMock()
        request.app = {"state": SimpleNamespace(subagents=mgr)}
        request.headers = {}
        request.match_info = {"agent_id": "a1"}
        await api_spawn_retry(request)
        assert mgr.spawn.call_args.kwargs["backend"] == "claude"
