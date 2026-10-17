"""``session_create`` inherits the dispatching crewmate's model and reasoning effort.

A crewmate runs on the model and effort its profile pins, and a worker it opens
through ``session_create`` should run the same unless the profile says otherwise.
An omitted ``model`` / ``reasoning_effort`` comes from the caller's member record:
its ``worker_model`` / ``worker_reasoning_effort`` when set, else its own pair;
a caller that is not a crewmate keeps the agent's or the global default.

The three contracts pinned here:

* a crewmate's child inherits (profile pair first, own pair otherwise);
* an explicit argument always wins over the inherited value;
* a caller that is not a crewmate keeps today's default, and the new
  ``reasoning_effort`` argument pins the level on its own.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from chat_test_helpers import _make_state
from member_memory_helpers import forget_declared_stores, make_request, write_member_home

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig, worker_session_defaults
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers.session_control import api_session_control_create
from kiro_crew.members import DM_SLOT_KEY_PREFIX

_MEMBER = "radar"


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _crewmate_home(tmp_path: Path, monkeypatch, **record) -> None:
    """A home with one V2 crewmate, ``radar``, whose record carries *record*."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = write_member_home(tmp_path, _MEMBER, "peer")
    config["agents"][_MEMBER].update(record)
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    forget_declared_stores(monkeypatch)


def _crewmate_caller(tmp_path: Path, monkeypatch, **record):
    """A crewmate DM slot with its execution record bound, as the thread endpoint mints it."""
    from kiro_crew.config.sections import ResolvedBindings
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.history import ConversationLog

    _crewmate_home(tmp_path, monkeypatch, **record)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    caller = state.get_or_create_slot(DM_SLOT_KEY_PREFIX + _MEMBER, mode="member")
    cfg = KiroCrewConfig.load()
    execution = resolve_member_execution(cfg, _MEMBER)
    caller.agent = _MEMBER
    caller.memory_store = execution.store.legacy_name
    bind_session_execution(slot_history_key(caller), execution, vouch=True)

    def resolve(_cfg, name, *_args, **_kwargs):
        selected = resolve_member_execution(_cfg, name or _MEMBER)
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=selected.store.legacy_name,
            effective_memory_config={},
            kiro_agent=selected.template_id,
            selection_kind="member",
            resolved_alias=name or _MEMBER,
            execution_context=selected,
        )

    monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda *_: caller.workspace)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
    return state, caller


def _create(state, caller, **kwargs):
    return asyncio.run(
        sc.create_session(state, caller_session_key=slot_history_key(caller), **kwargs)
    )


class TestCrewmateChildInherits:
    def test_child_takes_the_crewmates_own_model_and_effort(self, tmp_path, monkeypatch):
        """No worker pair set: the worker runs what its conductor runs."""
        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-sonnet-4.6", reasoning_effort="high"
        )

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == "claude-sonnet-4.6"
        assert child.reasoning_effort == "high"
        # Pinned as an explicit pick: the restore probe must not overwrite it.
        assert child._model_pick_gen == 1
        assert child._effort_pick_gen == 1
        assert created["model"] == "claude-sonnet-4.6"
        assert created["reasoning_effort"] == "high"
        assert created["inherited"] == ["model", "reasoning_effort"]
        written = state.conversation_log.get_metadata(slot_history_key(child))
        assert written.get("model") == "claude-sonnet-4.6"
        assert written.get("reasoning_effort") == "high"

    def test_the_worker_pair_outranks_the_crewmates_own(self, tmp_path, monkeypatch):
        """``worker_model`` = "auto" is a REAL pin: cheap workers under a costly conductor."""
        state, caller = _crewmate_caller(
            tmp_path,
            monkeypatch,
            model="claude-opus-4.6",
            reasoning_effort="max",
            worker_model="auto",
            worker_reasoning_effort="low",
        )

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == "auto"
        assert child.reasoning_effort == "low"
        assert created["inherited"] == ["model", "reasoning_effort"]

    def test_a_worker_model_alone_drops_the_crewmates_own_effort(self, tmp_path, monkeypatch):
        """A level picked for the crewmate's model is not carried onto a different worker model."""
        state, caller = _crewmate_caller(
            tmp_path,
            monkeypatch,
            model="claude-opus-4.6",
            reasoning_effort="max",
            worker_model="claude-haiku-4.5",
        )

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == "claude-haiku-4.5"
        assert child.reasoning_effort == ""
        assert created["inherited"] == ["model"]

    def test_an_explicit_model_drops_the_crewmates_own_effort_too(self, tmp_path, monkeypatch):
        """A model named on the call is one the crewmate's level was not chosen for."""
        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-opus-4.6", reasoning_effort="max"
        )

        created = _create(state, caller, model="claude-sonnet-4.6")

        child = state.get_slot(created["target"])
        assert child.model == "claude-sonnet-4.6"
        assert child.reasoning_effort == ""
        assert "inherited" not in created

    def test_an_explicit_model_still_takes_the_worker_effort(self, tmp_path, monkeypatch):
        state, caller = _crewmate_caller(
            tmp_path,
            monkeypatch,
            model="claude-opus-4.6",
            reasoning_effort="max",
            worker_reasoning_effort="low",
        )

        created = _create(state, caller, model="claude-sonnet-4.6")

        child = state.get_slot(created["target"])
        assert child.reasoning_effort == "low"
        assert created["inherited"] == ["reasoning_effort"]

    def test_a_worker_effort_alone_rides_the_crewmates_own_model(self, tmp_path, monkeypatch):
        state, caller = _crewmate_caller(
            tmp_path,
            monkeypatch,
            model="claude-opus-4.6",
            reasoning_effort="max",
            worker_reasoning_effort="low",
        )

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == "claude-opus-4.6"
        assert child.reasoning_effort == "low"

    def test_an_explicit_argument_wins_over_the_profile(self, tmp_path, monkeypatch):
        state, caller = _crewmate_caller(
            tmp_path,
            monkeypatch,
            model="claude-opus-4.6",
            reasoning_effort="max",
            worker_model="auto",
            worker_reasoning_effort="low",
        )

        created = _create(state, caller, model="claude-sonnet-4.6", reasoning_effort="medium")

        child = state.get_slot(created["target"])
        assert child.model == "claude-sonnet-4.6"
        assert child.reasoning_effort == "medium"
        assert "inherited" not in created

    def test_each_argument_overrides_independently(self, tmp_path, monkeypatch):
        """Naming one of the pair inherits only the other."""
        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-opus-4.6", reasoning_effort="max"
        )

        created = _create(state, caller, reasoning_effort="low")

        child = state.get_slot(created["target"])
        assert child.model == "claude-opus-4.6"
        assert child.reasoning_effort == "low"
        assert created["inherited"] == ["model"]

    def test_a_crewmate_with_nothing_set_leaves_the_default(self, tmp_path, monkeypatch):
        state, caller = _crewmate_caller(tmp_path, monkeypatch)

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert child.reasoning_effort == ""
        assert child._model_pick_gen == 0
        assert "model" not in created
        assert "reasoning_effort" not in created
        assert "inherited" not in created
        written = state.conversation_log.get_metadata(slot_history_key(child))
        assert "model" not in written
        assert "reasoning_effort" not in written

    def test_an_inherited_model_the_guard_refuses_is_dropped_not_fatal(self, tmp_path, monkeypatch):
        """A stale profile pin must not stop the crewmate from opening workers.

        The child starts on the default, the effort still inherits, the reply flags
        the drop, and the SEL record names the refused id (redacted).
        """
        import kiro_crew.dashboard.chat_handlers as ch

        monkeypatch.setattr(
            ch, "_model_rejected_reason", lambda name, provider=None: f"{name} is display-only"
        )
        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, worker_model="fable-5-1m", worker_reasoning_effort="high"
        )
        audits = []
        monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert child._model_pick_gen == 0
        assert child.reasoning_effort == "high"
        assert "model" not in created
        assert created["inherited_model_dropped"] is True
        assert created["inherited"] == ["reasoning_effort"]
        assert "model" not in state.conversation_log.get_metadata(slot_history_key(child))
        create = next(a for a in audits if a["operation"] == "create")
        assert create["detail"]["model"] == ""
        assert create["detail"]["inherited"] == "reasoning_effort"
        assert create["detail"]["inherited_model_dropped"] == "fable-5-1m"

    def test_a_pin_from_another_harness_is_dropped_for_the_worker(self, tmp_path, monkeypatch):
        """Member thread on claude, workers on kiro: the crewmate's claude id must not ride the worker."""
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        state, caller = _crewmate_caller(tmp_path, monkeypatch, model="claude-opus-4.6")
        real_load = _Cfg.load

        def load_with_split_backends(*a, **kw):
            cfg = real_load(*a, **kw)
            cfg.agent.acp_backend = ""
            cfg.agent.member_acp_backend = "claude"
            return cfg

        monkeypatch.setattr(_Cfg, "load", staticmethod(load_with_split_backends))
        audits = []
        monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert created["inherited_model_dropped"] is True
        create = next(a for a in audits if a["operation"] == "create")
        assert create["detail"]["inherited_model_dropped"] == "claude-opus-4.6"

    def test_dropping_the_crewmates_model_drops_its_own_effort_too(self, tmp_path, monkeypatch):
        """The crewmate's level rode along for its model; with the model gone it goes too."""
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-opus-4.6", reasoning_effort="max"
        )
        real_load = _Cfg.load

        def load_with_split_backends(*a, **kw):
            cfg = real_load(*a, **kw)
            cfg.agent.acp_backend = ""
            cfg.agent.member_acp_backend = "claude"
            return cfg

        monkeypatch.setattr(_Cfg, "load", staticmethod(load_with_split_backends))

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert child.reasoning_effort == ""
        assert "inherited" not in created
        assert created["inherited_model_dropped"] is True

    def test_an_explicit_worker_pin_crosses_harnesses_and_keeps_the_worker_effort(
        self, tmp_path, monkeypatch
    ):
        """`worker_model` was validated for the worker's harness, so the namespace read does not drop it."""
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        state, caller = _crewmate_caller(
            tmp_path,
            monkeypatch,
            model="claude-opus-4.6",
            reasoning_effort="max",
            worker_model="claude-haiku-4.5",
            worker_reasoning_effort="low",
        )
        real_load = _Cfg.load

        def load_with_split_backends(*a, **kw):
            cfg = real_load(*a, **kw)
            cfg.agent.acp_backend = ""
            cfg.agent.member_acp_backend = "claude"
            return cfg

        monkeypatch.setattr(_Cfg, "load", staticmethod(load_with_split_backends))

        created = _create(state, caller)

        child = state.get_slot(created["target"])
        assert child.model == "claude-haiku-4.5"
        assert child.reasoning_effort == "low"
        assert "inherited_model_dropped" not in created

    def test_auto_rides_any_harness(self, tmp_path, monkeypatch):
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        state, caller = _crewmate_caller(tmp_path, monkeypatch, worker_model="auto")
        real_load = _Cfg.load

        def load_with_split_backends(*a, **kw):
            cfg = real_load(*a, **kw)
            cfg.agent.acp_backend = ""
            cfg.agent.member_acp_backend = "claude"
            return cfg

        monkeypatch.setattr(_Cfg, "load", staticmethod(load_with_split_backends))

        created = _create(state, caller)

        assert state.get_slot(created["target"]).model == "auto"
        assert "inherited_model_dropped" not in created

    def test_an_explicit_model_the_guard_refuses_still_fails_the_create(
        self, tmp_path, monkeypatch
    ):
        """The drop is for the profile's pin only: the caller's own bad pick is its own error."""
        import kiro_crew.dashboard.chat_handlers as ch

        monkeypatch.setattr(
            ch, "_model_rejected_reason", lambda name, provider=None: f"{name} is display-only"
        )
        state, caller = _crewmate_caller(tmp_path, monkeypatch, worker_model="auto")
        before = state.live_slot_count()

        with pytest.raises(sc.SessionControlError) as exc:
            _create(state, caller, model="fable-5-1m")

        assert exc.value.code == "model_rejected"
        assert state.live_slot_count() == before

    def test_the_audit_names_what_the_profile_supplied(self, tmp_path, monkeypatch):
        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-opus-4.6", reasoning_effort="max"
        )
        audits = []
        monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

        _create(state, caller, reasoning_effort="low")

        create = next(a for a in audits if a["operation"] == "create")
        assert create["detail"]["model"] == "claude-opus-4.6"
        assert create["detail"]["reasoning_effort"] == "low"
        assert create["detail"]["inherited"] == "model"


class TestNonCrewmateCaller:
    def test_an_ordinary_session_keeps_the_default(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        created = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert child.reasoning_effort == ""
        assert "inherited" not in created

    def test_a_template_worker_does_not_re_inherit_its_dispatchers_profile(
        self, tmp_path, monkeypatch
    ):
        """A worker keeps the crewmate's store but runs as a template; its own children do not inherit."""
        from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-opus-4.6", reasoning_effort="max"
        )
        cfg = KiroCrewConfig.load()
        as_template = resolve_member_execution(cfg, _MEMBER).with_template(
            "kirocrew-worker", "kirocrew-worker"
        )
        worker = state.get_or_create_slot("chat-worker", workspace=caller.workspace)
        bind_session_execution(slot_history_key(worker), as_template, vouch=True)

        created = _create(state, worker)

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert child.reasoning_effort == ""

    def test_an_id_less_members_template_worker_does_not_re_inherit_either(
        self, tmp_path, monkeypatch
    ):
        """The record of an id-less member's worker still says ``member``; the template tells it apart."""
        from kiro_crew.execution_context import (
            ExecutionContext,
            MemoryStoreRef,
            bind_session_execution,
        )

        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-opus-4.6", reasoning_effort="max"
        )
        # The shape `create_session` mints for such a worker: member selection,
        # no id, the default store, the WORKER's template.
        as_worker = ExecutionContext(
            None,
            MemoryStoreRef("default"),
            "member",
            "kirocrew-worker",
            "persistent",
            selection_name=_MEMBER,
        )
        worker = state.get_or_create_slot("chat-worker", workspace=caller.workspace)
        bind_session_execution(slot_history_key(worker), as_worker, vouch=True)

        created = _create(state, worker)

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert child.reasoning_effort == ""

    def test_a_child_that_is_itself_a_crewmate_keeps_its_own_profile(self, tmp_path, monkeypatch):
        """Naming another crewmate as `agent` runs THAT crewmate's profile, never the dispatcher's."""
        state, caller = _crewmate_caller(
            tmp_path, monkeypatch, model="claude-opus-4.6", reasoning_effort="max"
        )

        # `caller_fenced=False`: the owner-authority form, since a member DM may
        # not bind another member's private store on its own.
        created = _create(state, caller, agent="peer", caller_fenced=False)

        child = state.get_slot(created["target"])
        # No pin copied: peer's own crew-pin tier decides at its first turn.
        assert child.model == ""
        assert child.reasoning_effort == ""
        assert "inherited" not in created

    def test_an_explicit_effort_pins_the_level_on_its_own(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        created = asyncio.run(
            sc.create_session(
                state, caller_session_key=slot_history_key(caller), reasoning_effort="xhigh"
            )
        )

        child = state.get_slot(created["target"])
        assert child.model == ""
        assert child.reasoning_effort == "xhigh"
        assert child._effort_pick_gen == 1
        assert created["reasoning_effort"] == "xhigh"
        written = state.conversation_log.get_metadata(slot_history_key(child))
        assert written.get("reasoning_effort") == "xhigh"
        assert "model" not in written

    @pytest.mark.parametrize("bad", ["", "minimal", "HIGH", "x" * 40])
    def test_a_level_outside_the_five_is_refused_before_allocation(self, tmp_path, bad):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        before = state.live_slot_count()

        if bad == "":
            # "" is the absence of a pick, not a level: it inherits like None.
            created = asyncio.run(
                sc.create_session(
                    state, caller_session_key=slot_history_key(caller), reasoning_effort=""
                )
            )
            assert state.get_slot(created["target"]).reasoning_effort == ""
            return
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(
                sc.create_session(
                    state, caller_session_key=slot_history_key(caller), reasoning_effort=bad
                )
            )
        assert exc.value.code == "effort_rejected"
        assert state.live_slot_count() == before


class TestHttpRoute:
    def test_the_route_forwards_reasoning_effort(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")

        async def go():
            return await api_session_control_create(
                make_request(
                    state,
                    "/api/session-control/create",
                    body={"title": "worker", "reasoning_effort": "high"},
                    internal=True,
                    session=slot_history_key(caller),
                )
            )

        response = asyncio.run(go())
        assert response.status == 200, response.text
        payload = json.loads(response.text)
        assert payload["reasoning_effort"] == "high"
        assert state.get_slot(payload["target"]).reasoning_effort == "high"

    def test_the_route_refuses_a_non_string_effort(self, tmp_path):
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        before = state.live_slot_count()

        async def go():
            return await api_session_control_create(
                make_request(
                    state,
                    "/api/session-control/create",
                    body={"title": "worker", "reasoning_effort": 3},
                    internal=True,
                    session=slot_history_key(caller),
                )
            )

        response = asyncio.run(go())
        assert response.status == 400
        assert json.loads(response.text)["code"] == "invalid_field_type"
        assert state.live_slot_count() == before


class TestMcpTool:
    """The ``session_create`` tool frame, against an in-memory dashboard."""

    def _call(self, args, reply):
        from kiro_crew.mcp_dashboard import TABLE
        from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
        from kiro_crew.mcp_tools.table import Caller, ToolContext

        dash = InMemoryDashboardClient(
            {
                "GET /api/chat/slots": [{"key": "chat-1-100", "title": "Caller"}],
                "GET /api/chat/folders": [],
                "POST /api/session-control/create": lambda req: reply,
            }
        )
        out = TABLE.call(
            "session_create", args, ToolContext(dash, Caller.strict("dashboard:chat-1-100"))
        )
        posted = [r for r in dash.requests if r.method == "POST"]
        return out, posted[-1].body

    def test_forwards_reasoning_effort_and_names_what_was_inherited(self):
        out, body = self._call(
            {"title": "w", "agent": "kirocrew-worker", "reasoning_effort": "low"},
            {
                "ok": True,
                "target": "chat-9",
                "title": "w",
                "model": "claude-opus-4.6",
                "reasoning_effort": "low",
                "inherited": ["model"],
            },
        )
        assert body["reasoning_effort"] == "low"
        assert "model" not in body
        assert "on model `claude-opus-4.6`" in out
        assert "reasoning effort `low`" in out
        assert "inherited from your crewmate profile: model" in out

    def test_an_omitted_effort_is_not_sent(self):
        out, body = self._call(
            {"title": "w", "agent": "kirocrew-worker"},
            {"ok": True, "target": "chat-9", "title": "w"},
        )
        assert "reasoning_effort" not in body
        assert "inherited" not in out

    def test_the_schema_bounds_the_level(self):
        from kiro_crew.validation import SESSION_CREATE_SCHEMA, ValidationError, validate_tool_args

        assert (
            validate_tool_args({"reasoning_effort": "high"}, SESSION_CREATE_SCHEMA)[
                "reasoning_effort"
            ]
            == "high"
        )
        assert "reasoning_effort" not in validate_tool_args({}, SESSION_CREATE_SCHEMA)
        with pytest.raises(ValidationError):
            validate_tool_args({"reasoning_effort": "x" * 33}, SESSION_CREATE_SCHEMA)


class TestProfileWritesTheWorkerPair:
    """``PUT /api/agents/{name}`` stores the pair under the crew pin's own validators."""

    @pytest.fixture(autouse=True)
    def _owner_caller(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.fixture()
    def seeded_agent(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["existing"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="default", memory_store="default"
        )
        cfg.save()
        return "existing"

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

        app = web.Application()
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        return app

    @pytest.mark.asyncio
    async def test_stores_the_pair_and_keeps_auto_verbatim(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"worker_model": " auto ", "worker_reasoning_effort": "low"},
            )
            assert resp.status == 200, await resp.text()

        stored = KiroCrewConfig.load().agents[seeded_agent]
        # "auto" is a pin here, where `model` would have folded it to "".
        assert stored.worker_model == "auto"
        assert stored.worker_reasoning_effort == "low"
        assert stored.model == ""

    @pytest.mark.asyncio
    async def test_the_empty_string_clears_each(self, seeded_agent):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            await client.put(
                f"/api/agents/{seeded_agent}",
                json={"worker_model": "auto", "worker_reasoning_effort": "low"},
            )
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"worker_model": "", "worker_reasoning_effort": ""},
            )
            assert resp.status == 200

        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.worker_model == ""
        assert stored.worker_reasoning_effort == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"worker_reasoning_effort": "ultra"}, "invalid_worker_reasoning_effort"),
            ({"worker_reasoning_effort": 3}, "invalid_worker_reasoning_effort"),
            ({"worker_model": 3}, "invalid_worker_model"),
        ],
    )
    async def test_a_bad_value_writes_nothing(self, seeded_agent, body, code):
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(self._app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"workspace": "other", **body}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == code

        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.worker_model == ""
        assert stored.worker_reasoning_effort == ""
        assert stored.workspace == "default"


class TestRosterShipsTheWorkerPair:
    @pytest.mark.asyncio
    async def test_members_row_carries_both(self, tmp_path, monkeypatch):
        """``GET /api/members`` is the profile card's read, so the pair rides it."""
        from types import SimpleNamespace

        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers.members import api_members

        _crewmate_home(tmp_path, monkeypatch, worker_model="auto", worker_reasoning_effort="low")
        app = web.Application()
        app["state"] = SimpleNamespace(owner_id="", _slots={}, conversation_log=None)
        request = make_mocked_request("GET", "/api/members", app=app)
        request["app"] = ""
        request["user"] = "local-app"

        response = await api_members(request)

        rows = {row["name"]: row for row in json.loads(response.body)["members"]}
        assert rows[_MEMBER]["worker_model"] == "auto"
        assert rows[_MEMBER]["worker_reasoning_effort"] == "low"
        assert rows["peer"]["worker_model"] == ""
        assert rows["peer"]["worker_reasoning_effort"] == ""


class TestWorkerSessionDefaults:
    """The resolver the create reads, on its own."""

    def test_nothing_set_pins_nothing(self):
        assert worker_session_defaults(KiroCrewAgentConfig()) == ("", "")

    def test_the_own_pair_is_the_fallback(self):
        crew = KiroCrewAgentConfig(model="claude-opus-4.6", reasoning_effort="high")
        assert worker_session_defaults(crew) == ("claude-opus-4.6", "high")

    def test_the_own_models_auto_is_inherit_but_the_worker_models_is_a_pin(self):
        assert worker_session_defaults(KiroCrewAgentConfig(model="auto")) == ("", "")
        assert worker_session_defaults(KiroCrewAgentConfig(worker_model="auto")) == ("auto", "")

    def test_the_worker_pair_wins(self):
        crew = KiroCrewAgentConfig(
            model="claude-opus-4.6",
            reasoning_effort="high",
            worker_model=" claude-haiku-4.5 ",
            worker_reasoning_effort="low",
        )
        assert worker_session_defaults(crew) == ("claude-haiku-4.5", "low")

    def test_the_own_effort_carries_only_while_workers_run_the_own_model(self):
        own = KiroCrewAgentConfig(model="claude-opus-4.6", reasoning_effort="high")
        assert worker_session_defaults(own) == ("claude-opus-4.6", "high")
        other = KiroCrewAgentConfig(
            model="claude-opus-4.6", reasoning_effort="high", worker_model="auto"
        )
        assert worker_session_defaults(other) == ("auto", "")
        effort_only = KiroCrewAgentConfig(
            model="claude-opus-4.6", reasoning_effort="high", worker_reasoning_effort="low"
        )
        assert worker_session_defaults(effort_only) == ("claude-opus-4.6", "low")

    def test_an_unknown_worker_effort_in_the_file_falls_back(self, tmp_path, monkeypatch):
        """Hand-edited config: a typo collapses to inherit at load, never reaching the child."""
        _crewmate_home(
            tmp_path,
            monkeypatch,
            reasoning_effort="high",
            worker_model="  auto ",
            worker_reasoning_effort="sometimes",
        )
        crew = KiroCrewConfig.load().agents[_MEMBER]
        assert crew.worker_model == "auto"
        assert crew.worker_reasoning_effort == ""
        # "auto" workers take no effort of their own: the typo falls to "", not to "high".
        assert worker_session_defaults(crew) == ("auto", "")
