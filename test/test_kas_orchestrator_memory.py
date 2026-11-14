"""Opt-in KAS memory recall: who asks for it, and what reaches the wire.

``agent.orchestrator_memory`` lets the owner's own new dashboard chats read the
memories the KAS engine already holds. These tests pin the two halves that
decide whether a host's private memories are read: which session shapes ask for
a reading mode, and what a ``session/new`` and a ``session/load`` actually carry.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import kas_memory
from kiro_crew.acp._dispatch import (
    attach_kas_custom_agents,
    build_session_new_params,
)
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KAS
from kiro_crew.config import KiroCrewConfig
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, bind_session_execution
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager
from kiro_crew.session_map import SESSION_MAP_FILENAME, SessionMap
from kiro_crew.shared_crew.contracts import (
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.shared_crew.people import ObservedPeople
from kiro_crew.testing.ids import UNALLOCATABLE_PID

OWNER_CHAT = "dashboard:chat-1-abc"


def _session_map(tmp_path, monkeypatch) -> SessionMap:
    """A SessionMap on disk under *tmp_path*.

    ``SessionMap`` resolves its own path from ``config_dir()``, so the redirect
    has to happen before construction rather than being passed in.
    """
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    return SessionMap()


def _plan(**overrides) -> kas_memory.MemoryPlan:
    """The plan for the owner's own persistent dashboard chat, with edits."""
    request = {
        "setting_on": True,
        "kas_backend": True,
        "session_key": OWNER_CHAT,
        "session_memory_mode": "persistent",
        "owning_app": "",
        "recorded_mode": "",
        "personal_session": True,
    }
    request.update(overrides)
    return kas_memory.memory_plan(**request)


def _mode(**overrides) -> str:
    """The mode the owner's own persistent dashboard chat asks for, with edits."""
    return _plan(**overrides).mode


class TestWhoAsksForRecall:
    @pytest.fixture(autouse=True)
    def personal_crew(self, _floor_monkeypatch):
        """A Crew only the local operator can reach, which is the default shape."""
        _floor_monkeypatch.setattr(kas_memory, "crew_reachable_by_others", lambda: False)

    def test_the_owners_own_dashboard_chat_asks_to_read_and_not_to_write(self):
        # Given: the setting is on for a persistent chat the owner's dashboard minted.
        # When: the session states its memory mode.
        # Then: it asks to read, which is the only mode that recalls anything.
        assert _mode() == kas_memory.MODE_READ_ONLY

    def test_an_incognito_chat_still_reads(self):
        # Given: the chat keeps no transcript but is allowed to read stored memory.
        # When: the session states its memory mode.
        # Then: it reads, matching what incognito means for Kiro Crew's own memory.
        assert _mode(session_memory_mode="incognito") == kas_memory.MODE_READ_ONLY

    def test_a_temporary_chat_is_disabled(self):
        # Given: a chat whose privacy mode neither reads nor writes memory.
        # When: the session states its memory mode.
        # Then: it states disabled rather than leaving the answer to a default.
        assert _mode(session_memory_mode="temporary") == kas_memory.MODE_DISABLED

    @pytest.mark.parametrize(
        "session_key",
        [
            "subagent:run-7",
            "taskrunner:job-2",
            "slack:1712345678.9",
            "cron:nightly",
            "dashboard:member-scout",
            "dashboard_member-scout",
        ],
    )
    def test_a_session_that_is_not_the_owners_own_chat_is_disabled(self, session_key):
        # Given: a worker, a scheduled run, a channel thread or a Bot DM thread.
        # When: each states its memory mode.
        # Then: all are disabled, so none of them reads the host's memories.
        assert _mode(session_key=session_key) == kas_memory.MODE_DISABLED

    def test_an_app_owned_session_is_disabled(self):
        # Given: a chat an app owns rather than the person at the dashboard.
        # When: the session states its memory mode.
        # Then: it is disabled.
        assert _mode(owning_app="meetings") == kas_memory.MODE_DISABLED

    def test_a_crew_anyone_else_can_reach_is_disabled(self, monkeypatch):
        # Given: a second person has been seen on this Crew.
        monkeypatch.setattr(kas_memory, "crew_reachable_by_others", lambda: True)
        # When: the owner's own chat states its memory mode.
        # Then: it is disabled, because the memories are one person's.
        assert _mode() == kas_memory.MODE_DISABLED

    @pytest.mark.parametrize("recorded", ["", "disabled"])
    def test_the_setting_off_states_no_policy_for_new_or_resumed_sessions(self, recorded):
        # Given: the setting is off, and the saved session read nothing.
        plan = _plan(setting_on=False, recorded_mode=recorded)
        # Then: neither a new nor a resumed session names a policy, so KAS keeps
        # its own defaults, and nothing rotates.
        assert (plan.mode, plan.resume_mode, plan.rotate) == ("", "", False)

    @pytest.mark.parametrize("session_key", [OWNER_CHAT, "cron:nightly"])
    def test_the_setting_off_states_no_policy_for_excluded_sessions_either(self, session_key):
        assert _plan(setting_on=False, session_key=session_key, personal_session=False).mode == ""

    def test_another_harness_states_no_mode_at_all(self):
        # Given: the operator chose a non-KAS harness, which has no such store.
        # When: the session states its memory mode.
        # Then: it names none.
        assert _mode(kas_backend=False) == ""


class TestCrewReachableByOthers:
    def test_a_person_who_arrived_over_the_network_counts(self, monkeypatch):
        # Given: a collaborator admitted on the tailnet listener has been seen.
        people = ObservedPeople()
        people.observe(
            Principal(
                id=principal_id_for("tailscale", "example.ts.net", "ada"),
                kind=PrincipalKind.HUMAN,
                provenance=Provenance.TAILSCALE_LISTENER,
                display=DisplaySnapshot(name="Ada"),
            )
        )
        monkeypatch.setattr("kiro_crew.shared_crew.people.registry", lambda: people)
        # When: the gate asks whether anyone else can reach this Crew.
        # Then: it answers yes.
        assert kas_memory.crew_reachable_by_others() is True

    def test_the_local_operator_alone_does_not_count(self, monkeypatch):
        # Given: only the local operator has been seen.
        people = ObservedPeople()
        people.observe(
            Principal(
                id=principal_id_for("local", "", "operator"),
                kind=PrincipalKind.HUMAN,
                provenance=Provenance.LOCAL_OPERATOR,
            )
        )
        monkeypatch.setattr("kiro_crew.shared_crew.people.registry", lambda: people)
        # When: the gate asks whether anyone else can reach this Crew.
        # Then: it answers no.
        assert kas_memory.crew_reachable_by_others() is False

    def test_an_unreadable_sharing_mode_counts_as_shared(self, monkeypatch):
        # Given: the sharing-path reader raises.
        def explode():
            raise RuntimeError("tailnet state unreadable")

        monkeypatch.setattr("kiro_crew.shared_crew.access.setup_paths_dict", explode)
        # When: the gate asks whether anyone else can reach this Crew.
        # Then: it fails closed.
        assert kas_memory.crew_reachable_by_others() is True


class TestWhatReachesTheWire:
    def test_a_reading_session_states_the_mode_and_mounts_the_memory_tool(self):
        # Given: a chat that may recall, with the shipped tool allowlist.
        agents = [{"id": "kirocrew", "prompt": "p", "tools": ["fs_read", "execute_bash"]}]
        # When: its session/new params are built.
        params = build_session_new_params(
            "/w", mcp_servers=[], kas_custom_agents=agents, kas_memory_mode="read_only"
        )
        kiro = params["_meta"]["kiro"]
        # Then: the request names read-only with reflection off, and the agent can
        # retrieve beyond the index it is handed.
        assert kiro["settings"]["memory"] == {"mode": "read_only", "reflection": False}
        assert kiro["customAgents"][0]["tools"] == ["fs_read", "execute_bash", "memory"]

    def test_the_memory_tool_is_named_once(self):
        # Given: an allowlist that already names the memory tool.
        agents = [{"id": "kirocrew", "prompt": "p", "tools": ["memory"]}]
        # When: its session/new params are built.
        params = build_session_new_params(
            "/w", mcp_servers=[], kas_custom_agents=agents, kas_memory_mode="read_only"
        )
        # Then: it is still named once.
        assert params["_meta"]["kiro"]["customAgents"][0]["tools"] == ["memory"]

    def test_an_all_tools_allowlist_is_left_alone(self):
        # Given: an agent granted every tool.
        agents = [{"id": "kirocrew", "prompt": "p", "tools": "*"}]
        # When: its session/new params are built.
        params = build_session_new_params(
            "/w", mcp_servers=[], kas_custom_agents=agents, kas_memory_mode="read_only"
        )
        # Then: the literal survives, because it already covers the memory tool.
        assert params["_meta"]["kiro"]["customAgents"][0]["tools"] == "*"

    def test_a_disabled_session_states_the_mode_and_mounts_nothing(self):
        # Given: a session the setting does not grant while the setting is on.
        agents = [{"id": "kirocrew", "prompt": "p", "tools": ["fs_read"]}]
        # When: its session/new params are built.
        params = build_session_new_params(
            "/w", mcp_servers=[], kas_custom_agents=agents, kas_memory_mode="disabled"
        )
        kiro = params["_meta"]["kiro"]
        # Then: it closes memory by request rather than by default, and gets no tool.
        assert kiro["settings"]["memory"] == {"mode": "disabled", "reflection": False}
        assert kiro["customAgents"][0]["tools"] == ["fs_read"]

    def test_the_setting_off_leaves_the_request_as_it_was(self):
        # Given: a session that states no memory mode.
        agents = [{"id": "kirocrew", "prompt": "p", "tools": ["fs_read"]}]
        # When: its session/new params are built.
        params = build_session_new_params("/w", mcp_servers=[], kas_custom_agents=agents)
        kiro = params["_meta"]["kiro"]
        # Then: no settings block is sent, and the allowlist is untouched.
        assert "settings" not in kiro
        assert kiro["customAgents"][0]["tools"] == ["fs_read"]

    def test_an_entry_with_no_allowlist_gains_no_tools_key(self):
        # Given: a definition that declares no tools field at all.
        agents = [{"id": "kirocrew", "prompt": "p"}]
        # When: its session/new params are built for a reading session.
        params = build_session_new_params(
            "/w", mcp_servers=[], kas_custom_agents=agents, kas_memory_mode="read_only"
        )
        # Then: none is invented, because the engine's schema has no null for it.
        assert "tools" not in params["_meta"]["kiro"]["customAgents"][0]

    def test_a_resumed_session_mounts_the_tool_and_states_no_mode(self):
        # Given: a load request for a chat that may recall.
        load_params = {"sessionId": "s-1", "cwd": "/w", "mcpServers": []}
        agents = [{"id": "kirocrew", "prompt": "p", "tools": ["fs_read"]}]
        # When: the agent definitions are re-sent with it.
        attach_kas_custom_agents(load_params, agents, memory_mode="read_only")
        kiro = load_params["_meta"]["kiro"]
        # Then: the tool is named, and no memory setting is sent: the engine
        # honours the session's stored record over anything a load states.
        assert kiro["customAgents"][0]["tools"] == ["fs_read", "memory"]
        assert "settings" not in kiro

    def test_the_settings_and_the_agent_definitions_do_not_displace_each_other(self):
        # Given: a request that carries both.
        params = build_session_new_params(
            "/w",
            mcp_servers=[],
            kas_custom_agents=[{"id": "kirocrew", "prompt": "p", "tools": []}],
            kas_memory_mode="read_only",
        )
        # When: the envelope is read.
        kiro = params["_meta"]["kiro"]
        # Then: both fields are present in the one envelope the engine reads.
        assert set(kiro) == {"settings", "customAgents"}


class TestWithdrawingRecallFromAnExistingSession:
    @pytest.fixture(autouse=True)
    def personal_crew(self, _floor_monkeypatch):
        """A Crew only the local operator can reach, which is the default shape."""
        _floor_monkeypatch.setattr(kas_memory, "crew_reachable_by_others", lambda: False)

    def test_the_setting_turned_off_withdraws_a_reading_session(self):
        # Given: a chat whose engine session was created reading memories.
        # When: the setting is off on the next cold start.
        # Then: the chat stops resuming it, because the engine keeps its own
        # record over anything a load states.
        assert _plan(setting_on=False, recorded_mode="read_only").rotate is True

    def test_switching_the_chat_to_temporary_withdraws_a_reading_session(self):
        # Given: the same chat, switched to the privacy mode that reads nothing.
        # When: the next cold start resolves its plan.
        # Then: it stops resuming the reading session.
        assert _plan(session_memory_mode="temporary", recorded_mode="read_only").rotate is True

    def test_a_second_person_on_the_crew_withdraws_a_reading_session(self, monkeypatch):
        # Given: the same chat on a Crew someone else can now reach.
        monkeypatch.setattr(kas_memory, "crew_reachable_by_others", lambda: True)
        # When: the next cold start resolves its plan.
        # Then: it stops resuming the reading session.
        assert _plan(recorded_mode="read_only").rotate is True

    def test_a_session_still_granted_recall_keeps_resuming(self):
        # Given: nothing about the chat's grant changed.
        # When: the next cold start resolves its plan.
        # Then: it keeps resuming, and still asks to read.
        plan = _plan(recorded_mode="read_only")
        assert (plan.rotate, plan.mode) == (False, kas_memory.MODE_READ_ONLY)

    @pytest.mark.parametrize("recorded", ["", "disabled"])
    def test_turning_the_setting_on_does_not_disturb_an_existing_chat(self, recorded):
        # Given: a chat whose engine session was created with no memory access,
        # or before the setting was ever on, so its saved mode is unknown.
        plan = _plan(recorded_mode=recorded)
        # When: the setting is turned on and the chat is reopened.
        # Then: it keeps resuming, so the setting reaches new chats only, and the
        # resume asks for disabled, whose acknowledgement the load then checks.
        assert (plan.rotate, plan.resume_mode) == (False, kas_memory.MODE_DISABLED)


class TestTheRecordedMode:
    def test_it_is_stored_with_the_session_id_and_read_back(self, tmp_path, monkeypatch):
        # Given: a session map on a scratch home.
        session_map = _session_map(tmp_path, monkeypatch)
        # When: a chat's engine session is recorded with the mode it asked for.
        session_map.set(OWNER_CHAT, "sid-1", provider="acp", kas_memory="read_only")
        # Then: the mode comes back for the comparison that decides a resume.
        assert session_map.get_kas_memory(OWNER_CHAT) == "read_only"

    def test_a_remap_that_names_no_mode_keeps_the_recorded_one(self, tmp_path, monkeypatch):
        # Given: a recorded reading session.
        session_map = _session_map(tmp_path, monkeypatch)
        session_map.set(OWNER_CHAT, "sid-1", provider="acp", kas_memory="read_only")
        # When: a path that re-maps the same session's new id writes no mode
        # (seed_conversation), which has not changed what that session asked for.
        session_map.set(OWNER_CHAT, "sid-2")
        # Then: the recorded mode still describes the mapped session.
        assert session_map.get_kas_memory(OWNER_CHAT) == "read_only"

    def test_an_explicit_empty_mode_clears_the_recorded_one(self, tmp_path, monkeypatch):
        session_map = _session_map(tmp_path, monkeypatch)
        session_map.set(OWNER_CHAT, "sid-1", provider="acp", kas_memory="read_only")

        session_map.set(OWNER_CHAT, "sid-2", kas_memory="")

        assert session_map.get_kas_memory(OWNER_CHAT) == ""

    def test_clearing_the_sid_clears_its_mode(self, tmp_path, monkeypatch):
        session_map = _session_map(tmp_path, monkeypatch)
        session_map.set(OWNER_CHAT, "sid-1", provider="acp", kas_memory="read_only")

        assert session_map.clear_sid(OWNER_CHAT) is True
        assert session_map.get_kas_memory(OWNER_CHAT) == ""

    def test_clearing_a_leftover_mode_alone_reports_no_dropped_conversation(
        self, tmp_path, monkeypatch
    ):
        # Given: a saved entry whose SID is already gone but whose mode is not.
        entry = {"sid": "", "slack_thread_ts": None, "slack_channel_id": None}
        (tmp_path / SESSION_MAP_FILENAME).write_text(
            json.dumps({OWNER_CHAT: {**entry, "kas_memory": "read_only"}}), encoding="utf-8"
        )
        session_map = _session_map(tmp_path, monkeypatch)
        assert session_map.get_kas_memory(OWNER_CHAT) == "read_only"

        # Then: clearing it orphans no conversation, and the mode leaves the disk too.
        assert session_map.clear_sid(OWNER_CHAT) is False
        assert _session_map(tmp_path, monkeypatch).get_kas_memory(OWNER_CHAT) == ""

    def test_a_chat_recorded_before_the_setting_existed_reads_as_no_memory(
        self, tmp_path, monkeypatch
    ):
        # Given: an entry written by a build that recorded no mode.
        session_map = _session_map(tmp_path, monkeypatch)
        session_map.set(OWNER_CHAT, "sid-1", provider="acp")
        # When: the mode is read back.
        # Then: it answers the same as a session that asked for none.
        assert session_map.get_kas_memory(OWNER_CHAT) == ""


def _memory_response(mode="read_only", reflection=False) -> dict:
    return {
        "sessionId": "sid-1",
        "modes": {"availableModes": [], "currentModeId": "kirocrew"},
        "_meta": {"memoryConfig": {"mode": mode, "reflection": reflection}},
        "configOptions": [
            {
                "id": "memoryReflection",
                "type": "select",
                "currentValue": "on" if reflection else "off",
                "options": [{"value": "on", "name": "On"}, {"value": "off", "name": "Off"}],
            }
        ],
    }


class TestTheRealSessionNewRequest:
    """The mode reaches the wire through ``AcpRuntime.create_session`` itself."""

    @pytest.fixture
    def runtime(self, monkeypatch, tmp_path):
        from kiro_crew.acp.harness import SessionExtras
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        rt = AcpRuntime(work_dir=str(tmp_path))
        rt._initialized = True
        rt._acp_backend = ACP_BACKEND_KAS
        self.sent: list[tuple[str, dict]] = []

        async def fake_send(method, params, timeout=None):
            self.sent.append((method, params))
            return _memory_response(kas_memory.sent_memory_mode(params) or "read_write")

        async def fake_agents(agent, **_kw):
            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": ["fs_read"]}])

        async def fake_finish(session_id, resp, **_kw):
            return session_id

        monkeypatch.setattr(rt, "_send_and_await", fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", fake_agents)
        monkeypatch.setattr(rt, "_finish_create_session", fake_finish)
        monkeypatch.setattr(rt, "_refuse_over_ceiling_start", AsyncMock())
        monkeypatch.setattr(rt, "_write_remote_token", AsyncMock())
        return rt

    @pytest.mark.asyncio
    async def test_a_granted_session_asks_for_recall_on_the_wire(self, runtime, tmp_path):
        # Given: a session the setting grants read-only recall.
        # When: the real create path issues session/new.
        await runtime.create_session(
            cwd=tmp_path, agent="kirocrew", mcp_servers=[], kas_memory_mode="read_only"
        )
        method, params = self.sent[0]
        kiro = params["_meta"]["kiro"]
        # Then: the request names the mode and mounts the engine's memory tool.
        assert method == "session/new"
        assert kiro["settings"]["memory"] == {"mode": "read_only", "reflection": False}
        assert kiro["customAgents"][0]["tools"] == ["fs_read", "memory"]

    @pytest.mark.asyncio
    async def test_a_worker_without_allocation_explicitly_disables_memory(
        self, runtime, tmp_path, monkeypatch
    ):
        # Given: orchestrator memory is on, and a worker requests no mode of its own.
        monkeypatch.setattr(kas_memory, "orchestrator_memory_enabled", lambda: True)
        await runtime.create_session(cwd=tmp_path, agent="kirocrew", mcp_servers=[])
        _method, params = self.sent[0]
        kiro = params["_meta"]["kiro"]
        # Then: it is excluded from recall by request, not by the engine's default.
        assert kiro["settings"]["memory"] == {"mode": "disabled", "reflection": False}
        assert kiro["customAgents"][0]["tools"] == ["fs_read"]

    @pytest.mark.asyncio
    async def test_with_the_setting_off_a_session_states_no_policy_and_checks_none(
        self, runtime, tmp_path
    ):
        # Given: orchestrator memory is off, and the engine reports its own
        # writing, reflecting default (the fake answers read_write).
        await runtime.create_session(cwd=tmp_path, agent="kirocrew", mcp_servers=[])
        _method, params = self.sent[0]
        kiro = params["_meta"]["kiro"]
        # Then: no memory setting is sent, and the session starts anyway,
        # because Crew asked for no policy to confirm.
        assert "settings" not in kiro
        assert kiro["customAgents"][0]["tools"] == ["fs_read"]

    @pytest.mark.parametrize(
        "response",
        [
            {"sessionId": "sid-1"},
            _memory_response("read_write"),
            _memory_response("read_only", reflection=True),
            {**_memory_response(), "configOptions": []},
            {
                **_memory_response(),
                "configOptions": [{"id": "memoryReflection", "currentValue": "on"}],
            },
        ],
        ids=["missing-policy", "write-access", "reflection-on", "unsupported", "control-mismatch"],
    )
    @pytest.mark.asyncio
    async def test_unconfirmed_recall_refuses_the_handle_and_tears_down(
        self, runtime, tmp_path, monkeypatch, response
    ):
        from kiro_crew.acp.session_handle import AcpRuntimeError

        finish = AsyncMock()
        teardown = AsyncMock()
        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(return_value=response))
        monkeypatch.setattr(runtime, "_finish_create_session", finish)
        monkeypatch.setattr(runtime, "terminate_session", teardown)

        with pytest.raises(AcpRuntimeError, match="memory"):
            await runtime.create_session(
                cwd=tmp_path, agent="kirocrew", mcp_servers=[], kas_memory_mode="read_only"
            )

        finish.assert_not_awaited()
        teardown.assert_awaited_once_with("sid-1")

    @pytest.mark.parametrize("requested", [None, "disabled"], ids=["unspecified", "disabled"])
    @pytest.mark.asyncio
    async def test_a_disabled_session_refuses_a_returned_writing_policy(
        self, runtime, tmp_path, monkeypatch, requested
    ):
        from kiro_crew.acp.session_handle import AcpRuntimeError

        monkeypatch.setattr(kas_memory, "orchestrator_memory_enabled", lambda: True)
        teardown = AsyncMock()
        monkeypatch.setattr(
            runtime, "_send_and_await", AsyncMock(return_value=_memory_response("read_write"))
        )
        monkeypatch.setattr(runtime, "terminate_session", teardown)
        with pytest.raises(AcpRuntimeError, match="memory policy"):
            await runtime.create_session(cwd=tmp_path, mcp_servers=[], kas_memory_mode=requested)
        teardown.assert_awaited_once_with("sid-1")

    @pytest.mark.parametrize("requested", ["", "disabled", "read_only"])
    @pytest.mark.asyncio
    async def test_cloud_requests_never_carry_the_preview_policy(
        self, runtime, tmp_path, monkeypatch, requested
    ):
        from kiro_crew.acp.harness import SessionExtras

        runtime.set_cloud_sandbox()
        runtime._agent_capabilities = {"_meta": {"kiro": {"executionTargets": ["cloud-sandbox"]}}}
        monkeypatch.setattr(runtime, "_kas_custom_agents", AsyncMock(return_value=SessionExtras()))
        send = AsyncMock(return_value={"sessionId": "sid-1"})
        monkeypatch.setattr(runtime, "_send_and_await", send)
        await runtime.create_session(cwd=tmp_path, mcp_servers=[], kas_memory_mode=requested)
        params = send.await_args.args[1]
        assert "settings" not in params.get("_meta", {}).get("kiro", {})
        assert "customAgents" not in params.get("_meta", {}).get("kiro", {})

    @pytest.mark.asyncio
    async def test_a_late_unconfirmed_recall_is_torn_down_without_adoption(
        self, runtime, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.runtime import AcpRequestTimeout, AcpSessionStartTimeout
        from kiro_crew.testing.wait import await_until

        future = asyncio.get_running_loop().create_future()
        timeout = AcpRequestTimeout("session/new timed out")
        timeout.req_id = 7
        timeout.adopted_future = future
        runtime._process = MagicMock()
        teardown = AsyncMock()
        adopter = AsyncMock(return_value=True)
        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(side_effect=timeout))
        monkeypatch.setattr(runtime, "terminate_session", teardown)

        with pytest.raises(AcpSessionStartTimeout) as error:
            await runtime.create_session(
                cwd=tmp_path, mcp_servers=[], kas_memory_mode="read_only", late_adopter=adopter
            )
        collector = error.value.collector
        assert collector is not None
        future.set_result({"sessionId": "sid-1"})
        await await_until(collector.settled)
        assert collector.outcome == "torn_down"
        adopter.assert_not_awaited()
        teardown.assert_awaited_once_with("sid-1")

    @pytest.mark.parametrize(
        "response",
        [{"modes": {}}, _memory_response("read_write"), _memory_response("read_only", True)],
    )
    @pytest.mark.asyncio
    async def test_an_unconfirmed_resume_is_evicted_without_deleting_its_record(
        self, runtime, tmp_path, monkeypatch, response
    ):
        from kiro_crew.acp.session_handle import AcpRuntimeError

        runtime._can_load_session = True
        evict = AsyncMock()
        delete = AsyncMock()
        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(return_value=response))
        monkeypatch.setattr(runtime, "evict_session", evict)
        monkeypatch.setattr(runtime, "terminate_session", delete)
        monkeypatch.setattr(
            "kiro_crew.acp.runtime._pooled_session_servers_and_ref_spec",
            lambda *_args: ([], None, ()),
        )
        monkeypatch.setattr(runtime, "_unpooled_control_planes", AsyncMock(return_value=[]))
        monkeypatch.setattr(runtime, "_own_stub_session", AsyncMock(return_value=([], "")))
        monkeypatch.setattr(runtime, "_session_label_elements", AsyncMock(return_value=[]))
        with pytest.raises(AcpRuntimeError, match="memory policy"):
            await runtime.load_session("", "saved-sid", cwd=tmp_path, kas_memory_mode="read_only")
        evict.assert_awaited_once_with("saved-sid")
        delete.assert_not_awaited()
        assert "saved-sid" not in runtime._session_queues

    def _stub_load(self, runtime, monkeypatch, response) -> AsyncMock:
        runtime._can_load_session = True
        send = AsyncMock(return_value=response)
        monkeypatch.setattr(runtime, "_send_and_await", send)
        monkeypatch.setattr(runtime, "evict_session", AsyncMock())
        monkeypatch.setattr(
            "kiro_crew.acp.runtime._pooled_session_servers_and_ref_spec",
            lambda *_args: ([], None, ()),
        )
        monkeypatch.setattr(runtime, "_unpooled_control_planes", AsyncMock(return_value=[]))
        monkeypatch.setattr(runtime, "_own_stub_session", AsyncMock(return_value=([], "")))
        monkeypatch.setattr(runtime, "_session_label_elements", AsyncMock(return_value=[]))
        return send

    @pytest.mark.asyncio
    async def test_a_resume_with_an_unknown_mode_on_an_enabled_host_is_checked(
        self, runtime, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.session_handle import AcpRuntimeError

        # Given: orchestrator memory is on, and a saved SID whose mode was never
        # recorded comes back from the engine with a writing policy.
        monkeypatch.setattr(kas_memory, "orchestrator_memory_enabled", lambda: True)
        send = self._stub_load(runtime, monkeypatch, _memory_response("read_write"))
        # When: it is resumed without a mode of its own.
        with pytest.raises(AcpRuntimeError, match="memory policy"):
            await runtime.load_session("", "saved-sid", cwd=tmp_path)
        # Then: the load asked for disabled and refused the contradicting answer.
        assert kas_memory.sent_memory_mode(send.await_args.args[1]) == "disabled"
        runtime.evict_session.assert_awaited_once_with("saved-sid")

    @pytest.mark.asyncio
    async def test_with_the_setting_off_a_resume_states_no_policy_and_checks_none(
        self, runtime, tmp_path, monkeypatch
    ):
        # Given: orchestrator memory is off, and the engine reports a writing policy.
        send = self._stub_load(runtime, monkeypatch, _memory_response("read_write"))
        # When: a saved SID is resumed without a mode of its own.
        await runtime.load_session("", "saved-sid", cwd=tmp_path)
        # Then: the load names no memory setting and the resume is kept.
        kiro = send.await_args.args[1].get("_meta", {}).get("kiro", {})
        assert "settings" not in kiro
        runtime.evict_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_resume_records_the_policy_it_stated_on_its_handle(
        self, runtime, tmp_path, monkeypatch
    ):
        # Given: orchestrator memory is on, and the engine confirms disabled.
        monkeypatch.setattr(kas_memory, "orchestrator_memory_enabled", lambda: True)
        self._stub_load(runtime, monkeypatch, _memory_response("disabled"))
        # When: a saved SID is resumed without a mode of its own.
        handle = await runtime.load_session("", "saved-sid", cwd=tmp_path)
        # Then: the handle names the resolved policy, which a live reload re-sends.
        assert handle.kas_memory_mode == "disabled"


class TestTheSavedSetting:
    """``agent.orchestrator_memory`` read from a real ``config.json``, unmocked."""

    @pytest.fixture
    def setting_on(self):
        cfg = KiroCrewConfig.load()
        cfg.agent.orchestrator_memory = True
        cfg.save()

    @pytest.mark.asyncio
    async def test_a_saved_true_disables_the_process_default_and_unspecified_sessions(
        self, setting_on, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.runtime import AcpRuntime

        runtime = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_KAS)
        send = AsyncMock(return_value={})
        monkeypatch.setattr(runtime, "_send_and_await", send)
        monkeypatch.setattr("kiro_crew.acp.runtime.agents_slice_throttling", lambda: False)

        await runtime._initialize_handshake({})

        settings = send.await_args.args[1]["clientCapabilities"]["_meta"]["kiro"]["settings"]
        assert settings["memory"] == {"mode": "disabled", "reflection": False}
        assert await kas_memory.runtime_memory_mode(ACP_BACKEND_KAS, None) == "disabled"
        assert await kas_memory.runtime_memory_mode(ACP_BACKEND_KAS, "") == ""


def _acp_provider(backend: str) -> MagicMock:
    provider = MagicMock(spec=AcpProvider)
    provider.client = MagicMock()
    provider.client.backend = backend
    provider.client.resumed = False
    provider.client._session_id = ""
    provider.client.rekey = MagicMock()
    provider.is_kas_backend = backend == ACP_BACKEND_KAS
    provider.is_process_alive.return_value = True
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.cwd = ""
    provider.spawn_identity = ""
    provider._runtime = None
    provider.defer_replay_sid_promotion = False
    provider.kas_memory_mode = ""
    provider.kas_memory_new_mode = ""
    return provider


def _manager_with_populated_pool(*, memory_enabled: bool, backend: str = ACP_BACKEND_KAS):
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = backend
    cfg.agent.orchestrator_memory = memory_enabled
    cfg.session.pool_size = 2
    pooled = _acp_provider(backend)
    cold = _acp_provider(backend)
    factory = MagicMock(return_value=cold)
    manager = SessionManager(cfg, provider_factory=factory)
    manager._schedule_replenish = MagicMock()
    manager._warm_pool.put_nowait((pooled, time.monotonic()))
    return manager, pooled, cold, factory


class TestWarmPoolMemoryMode:
    @pytest.fixture(autouse=True)
    def personal_crew(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(kas_memory, "crew_reachable_by_others", lambda: False)
        bind_session_execution(
            OWNER_CHAT, ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
        )
        _floor_monkeypatch.setattr(
            "kiro_crew.session_allocation_parts.get_or_create._session_runs_remotely",
            AsyncMock(return_value=False),
        )

    @pytest.mark.asyncio
    async def test_enabled_recall_cold_starts_instead_of_claiming_a_disabled_session(self):
        manager, _pooled, cold, factory = _manager_with_populated_pool(memory_enabled=True)
        try:
            provider, is_new, resumed = await manager.get_or_create(OWNER_CHAT)

            assert (provider, is_new, resumed) == (cold, True, False)
            assert cold.kas_memory_mode == kas_memory.MODE_READ_ONLY
            cold.start.assert_awaited_once()
            assert manager._warm_pool.qsize() == 1
            factory.assert_called_once()
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_disabled_recall_keeps_using_the_warm_pool(self):
        manager, pooled, _cold, factory = _manager_with_populated_pool(memory_enabled=False)
        try:
            provider, is_new, resumed = await manager.get_or_create(OWNER_CHAT)

            assert (provider, is_new, resumed) == (pooled, True, False)
            assert pooled.kas_memory_mode == ""
            factory.assert_not_called()
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_enabled_recall_keeps_other_harnesses_on_the_warm_pool(self):
        manager, pooled, _cold, factory = _manager_with_populated_pool(
            memory_enabled=True, backend=ACP_BACKEND_CODEX
        )
        try:
            provider, is_new, resumed = await manager.get_or_create(OWNER_CHAT)

            assert (provider, is_new, resumed) == (pooled, True, False)
            factory.assert_not_called()
        finally:
            await manager.close_all()


def _resumable_provider(created_sid: str) -> tuple[MagicMock, list[tuple[str, str]]]:
    provider = _acp_provider(ACP_BACKEND_KAS)
    started_modes: list[tuple[str, str]] = []

    def set_resume_session_id(sid: str) -> None:
        provider.client._session_id = sid

    async def start() -> None:
        started_modes.append((provider.kas_memory_mode, provider.kas_memory_new_mode))
        if provider.client._session_id:
            provider.client.resumed = True
            return
        provider.client._session_id = created_sid
        provider.client.resumed = False

    provider.client.set_resume_session_id = MagicMock(side_effect=set_resume_session_id)
    provider.start = AsyncMock(side_effect=start)
    return provider, started_modes


class TestAllocationRecordsTheActualSessionMode:
    @pytest.fixture(autouse=True)
    def personal_crew(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(kas_memory, "crew_reachable_by_others", lambda: False)
        bind_session_execution(
            OWNER_CHAT, ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
        )
        _floor_monkeypatch.setattr(
            "kiro_crew.session_allocation_parts.get_or_create._session_runs_remotely",
            AsyncMock(return_value=False),
        )

    @staticmethod
    def _config(*, enabled: bool) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        cfg.agent.orchestrator_memory = enabled
        cfg.session.pool_size = 0
        return cfg

    @pytest.mark.asyncio
    async def test_turning_recall_off_rotates_once_then_cold_resumes_normally(self):
        first, first_modes = _resumable_provider("sid-without-memory")
        manager = SessionManager(
            self._config(enabled=False), provider_factory=MagicMock(return_value=first)
        )
        manager._session_map.set(
            OWNER_CHAT,
            "sid-reading-memory",
            provider="kas",
            kas_memory=kas_memory.MODE_READ_ONLY,
        )
        try:
            _, _, resumed = await manager.get_or_create(OWNER_CHAT)
            assert resumed is False
            assert first_modes == [("", "")]
            assert manager._session_map.get_kas_memory(OWNER_CHAT) == ""
            assert manager._sessions[OWNER_CHAT].provider_switch_replay is True
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

        second, second_modes = _resumable_provider("unused")
        resumed_manager = SessionManager(
            self._config(enabled=False), provider_factory=MagicMock(return_value=second)
        )
        try:
            _, _, resumed = await resumed_manager.get_or_create(OWNER_CHAT)
            assert resumed is True
            assert second.client.set_resume_session_id.call_args.args == ("sid-without-memory",)
            assert second_modes == [("", "")]
            assert resumed_manager._session_map.get_kas_memory(OWNER_CHAT) == ""
            resumed_manager.release(OWNER_CHAT)
        finally:
            await resumed_manager.close_all()

    @pytest.mark.asyncio
    async def test_turning_recall_off_resumes_a_disabled_session_without_a_policy(self):
        # Given: a chat whose engine session was created disabled while the setting was on.
        provider, started_modes = _resumable_provider("unused")
        manager = SessionManager(
            self._config(enabled=False), provider_factory=MagicMock(return_value=provider)
        )
        manager._session_map.set(
            OWNER_CHAT, "sid-disabled-memory", provider="kas", kas_memory=kas_memory.MODE_DISABLED
        )
        try:
            # When: the setting is off and the chat is reopened.
            _, _, resumed = await manager.get_or_create(OWNER_CHAT)
            # Then: the same SID resumes, the load states no policy, and the
            # mapping records none.
            assert resumed is True
            assert provider.client.set_resume_session_id.call_args.args == ("sid-disabled-memory",)
            assert started_modes == [("", "")]
            assert manager._session_map.get_kas_memory(OWNER_CHAT) == ""
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_enabling_recall_keeps_a_disabled_session_disabled(self):
        provider, started_modes = _resumable_provider("unused")
        manager = SessionManager(
            self._config(enabled=True), provider_factory=MagicMock(return_value=provider)
        )
        manager._session_map.set(
            OWNER_CHAT,
            "sid-disabled-memory",
            provider="kas",
            kas_memory=kas_memory.MODE_DISABLED,
        )
        try:
            _, _, resumed = await manager.get_or_create(OWNER_CHAT)
            assert resumed is True
            assert started_modes == [(kas_memory.MODE_DISABLED, kas_memory.MODE_READ_ONLY)]
            assert provider.kas_memory_mode == kas_memory.MODE_DISABLED
            assert manager._session_map.get_kas_memory(OWNER_CHAT) == kas_memory.MODE_DISABLED
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.parametrize("enabled", [False, True])
    @pytest.mark.asyncio
    async def test_cloud_placement_keeps_the_sid_without_a_memory_grant(self, monkeypatch, enabled):
        from kiro_crew.coder.session_launch import SessionEnvironmentChoices

        monkeypatch.setattr(
            "kiro_crew.session_environment.session_environments_enabled", lambda: True
        )
        monkeypatch.setattr(
            SessionEnvironmentChoices,
            "resolve_choice",
            lambda _self, _key: ("cloud", OWNER_CHAT, "kiro_cloud"),
        )
        provider, started_modes = _resumable_provider("unused")
        manager = SessionManager(
            self._config(enabled=enabled), provider_factory=MagicMock(return_value=provider)
        )
        manager._session_map.set(OWNER_CHAT, "cloud-sid", provider="kas", kas_memory="read_only")
        try:
            _, _, resumed = await manager.get_or_create(OWNER_CHAT)
            assert resumed is True
            assert started_modes == [("", "")]
            assert manager._session_map.mapped_sid(OWNER_CHAT) == "cloud-sid"
            assert manager._session_map.get_kas_memory(OWNER_CHAT) == ""
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_a_remote_workspace_with_a_local_engine_can_recall(self, monkeypatch):
        from kiro_crew.coder.session_launch import SessionEnvironmentChoices

        monkeypatch.setattr(
            "kiro_crew.session_environment.session_environments_enabled", lambda: True
        )
        monkeypatch.setattr(
            SessionEnvironmentChoices,
            "resolve_choice",
            lambda _self, _key: ("workspace", OWNER_CHAT, "coder"),
        )
        provider, started_modes = _resumable_provider("local-kas-sid")
        manager = SessionManager(
            self._config(enabled=True), provider_factory=MagicMock(return_value=provider)
        )
        try:
            await manager.get_or_create(OWNER_CHAT)
            assert started_modes == [("read_only", "read_only")]
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()


class TestLoadFallbackUsesTheNewSessionMode:
    @pytest.mark.asyncio
    async def test_fallback_changes_from_the_loaded_mode_to_the_requested_mode(self, monkeypatch):
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_socket = None
        provider._client._resume_session_id = "old-sid"
        provider._client._session_key = OWNER_CHAT
        provider._client._channel_id = None
        provider._client._model = "auto"
        provider.kas_memory_mode = kas_memory.MODE_DISABLED
        provider.kas_memory_new_mode = kas_memory.MODE_READ_ONLY

        handle = MagicMock()
        handle.session_id = "new-sid"
        handle.available_models = []
        handle.memory_mode = "persistent"
        handle.kas_memory_mode = kas_memory.MODE_READ_ONLY
        handle.set_model = AsyncMock()
        runtime = MagicMock()
        runtime.pid = UNALLOCATABLE_PID
        runtime.spawn = AsyncMock()
        runtime.is_alive.return_value = True
        runtime.saw_not_logged_in.return_value = False
        runtime.saw_sandbox_init_failure.return_value = False
        runtime.saw_delegate_launch_failure.return_value = False
        runtime.settle_stderr = AsyncMock()
        runtime.kill = AsyncMock()
        runtime.load_session = AsyncMock(return_value=None)
        runtime.create_session = AsyncMock(return_value=handle)
        monkeypatch.setattr("kiro_crew.providers.acp.AcpRuntime", MagicMock(return_value=runtime))

        await provider._start_kiro_runtime()

        assert runtime.load_session.await_args.kwargs["kas_memory_mode"] == kas_memory.MODE_DISABLED
        assert (
            runtime.create_session.await_args.kwargs["kas_memory_mode"] == kas_memory.MODE_READ_ONLY
        )
        assert provider.kas_memory_mode == kas_memory.MODE_READ_ONLY


class TestNativeMemoryInitialization:
    @pytest.mark.parametrize("enabled", [True, False], ids=["setting-on", "setting-off"])
    @pytest.mark.parametrize(
        "backend,cloud",
        [(ACP_BACKEND_KAS, False), (ACP_BACKEND_KAS, True), (ACP_BACKEND_CODEX, False)],
    )
    @pytest.mark.asyncio
    async def test_safe_defaults_are_local_kas_only_and_preserve_other_settings(
        self, monkeypatch, tmp_path, backend, cloud, enabled
    ):
        from kiro_crew.acp.runtime import AcpRuntime

        monkeypatch.setattr(kas_memory, "orchestrator_memory_enabled", lambda: enabled)
        runtime = AcpRuntime(work_dir=str(tmp_path), acp_backend=backend)
        if cloud:
            runtime.set_cloud_sandbox()
        capabilities = {"_meta": {"kiro": {"settings": {"toolSearch": {"enabled": False}}}}}
        original = copy.deepcopy(capabilities)
        send = AsyncMock(return_value={})
        monkeypatch.setattr(runtime, "_send_and_await", send)
        monkeypatch.setattr("kiro_crew.acp.runtime.agents_slice_throttling", lambda: False)

        await runtime._initialize_handshake(capabilities)

        settings = send.await_args.args[1]["clientCapabilities"]["_meta"]["kiro"]["settings"]
        assert settings["toolSearch"] == {"enabled": False}
        assert capabilities == original
        if enabled and backend == ACP_BACKEND_KAS and not cloud:
            assert settings["memory"] == {"mode": "disabled", "reflection": False}
        else:
            assert "memory" not in settings

    def test_workers_built_outside_allocation_leave_the_mode_to_the_runtime(self):
        assert AcpProvider(acp_backend=ACP_BACKEND_KAS).kas_memory_mode is None
        assert AcpProvider(acp_backend=ACP_BACKEND_CODEX).kas_memory_mode is None


class TestPersonalDashboardAllocation:
    @pytest.fixture(autouse=True)
    def personal_crew(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(kas_memory, "crew_reachable_by_others", lambda: False)
        _floor_monkeypatch.setattr(
            "kiro_crew.session_allocation_parts.get_or_create._session_runs_remotely",
            AsyncMock(return_value=False),
        )

    @staticmethod
    def _manager():
        provider, started_modes = _resumable_provider("fresh-sid")
        cfg = TestAllocationRecordsTheActualSessionMode._config(enabled=True)
        manager = SessionManager(cfg, provider_factory=MagicMock(return_value=provider))
        return manager, provider, started_modes

    @pytest.mark.parametrize(
        "execution",
        [
            ExecutionContext("scout", MemoryStoreRef("scout", "scout"), "member", "kirocrew"),
            ExecutionContext("scout", MemoryStoreRef("scout", "scout"), "template", "kirocrew"),
            ExecutionContext(None, MemoryStoreRef("scout"), "member", "kirocrew"),
        ],
        ids=["member-new-tab", "member-child-template", "legacy-member"],
    )
    @pytest.mark.asyncio
    async def test_member_context_on_an_ordinary_dashboard_key_cannot_recall(self, execution):
        await asyncio.to_thread(bind_session_execution, OWNER_CHAT, execution, vouch=True)
        manager, provider, modes = self._manager()
        try:
            await manager.get_or_create(OWNER_CHAT)
            assert modes == [("disabled", "disabled")]
            assert provider.kas_memory_mode == "disabled"
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_registered_dashboard_worker_cannot_recall(self):
        from kiro_crew.mate_worker import MATE_WORKER_AGENT, note_worker_session
        from kiro_crew.members import forget_crewmate_worker_session, is_crewmate_worker_session

        worker = ExecutionContext(
            "scout", MemoryStoreRef("scout", "scout"), "template", MATE_WORKER_AGENT
        )
        await asyncio.to_thread(bind_session_execution, OWNER_CHAT, worker, vouch=True)
        assert note_worker_session(OWNER_CHAT, worker) is True
        assert is_crewmate_worker_session(OWNER_CHAT) is True
        manager, provider, modes = self._manager()
        provider.mate_worker_kit_supported = True
        provider.mate_worker_tool_limits_supported = True
        try:
            await manager.get_or_create(OWNER_CHAT)
            assert modes == [("disabled", "disabled")]
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()
            forget_crewmate_worker_session(OWNER_CHAT)

    @pytest.mark.parametrize("vouch", ["live", "durable", "corrupt-durable"])
    @pytest.mark.asyncio
    async def test_rewritten_member_record_cannot_grant_owner_recall(self, monkeypatch, vouch):
        from kiro_crew import execution_context
        from kiro_crew.history import ConversationLog

        member = ExecutionContext("scout", MemoryStoreRef("scout", "scout"), "member", "kirocrew")
        await asyncio.to_thread(bind_session_execution, OWNER_CHAT, member, vouch=True)
        owner = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
        await asyncio.to_thread(
            ConversationLog().update_metadata,
            OWNER_CHAT,
            {execution_context.EXECUTION_CONTEXT_KEY: owner.to_record()},
        )
        if vouch != "live":
            monkeypatch.setattr(execution_context, "_VOUCHED_EXECUTIONS", {})
        if vouch == "corrupt-durable":
            from kiro_crew._durable_vouch import durable_vouch_path

            durable_vouch_path(OWNER_CHAT).write_text("not json", encoding="utf-8")
        manager, _, modes = self._manager()
        manager._session_map.set(OWNER_CHAT, "reading-sid", provider="kas", kas_memory="read_only")
        try:
            _, _, resumed = await manager.get_or_create(OWNER_CHAT)
            assert resumed is False
            assert modes == [("disabled", "disabled")]
            assert manager._sessions[OWNER_CHAT].provider_switch_replay is True
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.parametrize(
        "link_kind", ["slack", "paused-slack", "inbound-mirror", "outbound-mirror"]
    )
    @pytest.mark.parametrize("resume", [False, True], ids=["new", "cold-resume"])
    @pytest.mark.asyncio
    async def test_channel_link_withholds_recall_and_rotates_a_reading_sid(self, link_kind, resume):
        from kiro_crew.messaging.link import ChannelLink

        await asyncio.to_thread(
            bind_session_execution,
            OWNER_CHAT,
            ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew"),
        )
        manager, _, modes = self._manager()
        if link_kind in {"slack", "paused-slack"}:
            manager.set_slack_link(OWNER_CHAT, "123.456", "C123")
            if link_kind == "paused-slack":
                manager.set_slack_paused(OWNER_CHAT, True)
        else:
            manager.set_mirror_link(
                OWNER_CHAT,
                ChannelLink(channel_type="telegram", channel_id="123"),
                accepts_inbound=link_kind == "inbound-mirror",
            )
        if resume:
            manager._session_map.set(
                OWNER_CHAT, "reading-sid", provider="kas", kas_memory="read_only"
            )
        try:
            _, _, resumed = await manager.get_or_create(OWNER_CHAT)
            assert resumed is False
            assert modes == [("disabled", "disabled")]
            assert manager._session_map.get_kas_memory(OWNER_CHAT) == "disabled"
            assert manager._sessions[OWNER_CHAT].provider_switch_replay is resume
            assert manager.get_mirror_link(OWNER_CHAT) is not None
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.parametrize("mode", ["persistent", "incognito"])
    @pytest.mark.asyncio
    async def test_personal_owner_new_chat_keeps_recall(self, mode):
        await asyncio.to_thread(
            bind_session_execution,
            OWNER_CHAT,
            ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew", mode),
        )
        manager, _, modes = self._manager()
        try:
            await manager.get_or_create(OWNER_CHAT)
            assert modes == [("read_only", "read_only")]
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_dashboard_key_without_execution_identity_gets_no_grant(self):
        manager, _, modes = self._manager()
        try:
            await manager.get_or_create(OWNER_CHAT)
            assert modes == [("disabled", "disabled")]
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_channel_dispatch_without_a_persisted_link_gets_no_grant(self):
        await asyncio.to_thread(
            bind_session_execution,
            OWNER_CHAT,
            ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew"),
        )
        manager, _, modes = self._manager()
        try:
            await manager.get_or_create(OWNER_CHAT, channel_id="C123")
            assert modes == [("disabled", "disabled")]
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_linking_a_live_session_does_not_claim_to_withdraw_its_policy(self):
        await asyncio.to_thread(
            bind_session_execution,
            OWNER_CHAT,
            ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew"),
        )
        manager, provider, modes = self._manager()
        try:
            await manager.get_or_create(OWNER_CHAT)
            manager.release(OWNER_CHAT)
            manager.set_slack_link(OWNER_CHAT, "123.456", "C123")
            same, _, _ = await manager.get_or_create(OWNER_CHAT)
            assert same is provider
            assert provider.kas_memory_mode == "read_only"
            assert modes == [("read_only", "read_only")]
            manager.release(OWNER_CHAT)
        finally:
            await manager.close_all()
