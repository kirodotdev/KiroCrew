"""A spawn's `silent` flag reaches the gate, and a silent parent passes it on.

The decision (docs/decisions/, "a silent parent's sub-agent completions never
ring the bell"): `spawn_run` and `spawn_sub_agents` each take an optional call-level `silent`
boolean forwarded to the admission gate's existing parameter, a silent cron's
sub-agents — and any they spawn — are always silent, and `silent` drops ONLY
completion notifications: the bell note on every route that rings one.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import mcp_core
from kiro_crew.dashboard.messaging_api.spawn import _parent_run_is_silent
from kiro_crew.mcp_tools import spawn as spawn_mod
from kiro_crew.mcp_tools.spawn_parts import wave as wave_mod
from kiro_crew.slack import gateway as gateway_mod
from kiro_crew.slack.gateway_runtime import subagent_injection as inj_mod


def _quiet_mcp(monkeypatch: pytest.MonkeyPatch, posted: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:tab-1")
    monkeypatch.setattr(mcp_core, "sel", lambda: MagicMock())

    def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        posted.append(dict(body))
        return {"id": f"a{len(posted)}", "parent_work_supported": True}

    monkeypatch.setattr(mcp_core, "_post", _post)


class TestToolsForwardSilent:
    def test_spawn_run_forwards_silent_to_the_gateway(self, monkeypatch):
        posted: list[dict[str, Any]] = []
        _quiet_mcp(monkeypatch, posted)
        step = spawn_mod.spawn_run("spawn_run", {"task": "t", "silent": True})
        assert not isinstance(step, str), step
        step._submit_one(0)
        assert posted and posted[0]["silent"] is True

    def test_spawn_run_without_silent_sends_no_silent_key(self, monkeypatch):
        # Absent, not false: the endpoint's default must stay the endpoint's.
        posted: list[dict[str, Any]] = []
        _quiet_mcp(monkeypatch, posted)
        step = spawn_mod.spawn_run("spawn_run", {"task": "t"})
        assert not isinstance(step, str), step
        step._submit_one(0)
        assert posted and "silent" not in posted[0]

    def test_spawn_sub_agents_forwards_silent_to_the_gateway(self, monkeypatch):
        posted: list[dict[str, Any]] = []
        _quiet_mcp(monkeypatch, posted)
        step = wave_mod.spawn_sub_agents(
            "spawn_sub_agents", {"agents": [{"prompt": "p"}], "silent": True}
        )
        assert not isinstance(step, str), step
        step._submit_one()
        assert posted and posted[0]["silent"] is True

    def test_spawn_sub_agents_without_silent_sends_no_silent_key(self, monkeypatch):
        posted: list[dict[str, Any]] = []
        _quiet_mcp(monkeypatch, posted)
        step = wave_mod.spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "p"}]})
        assert not isinstance(step, str), step
        step._submit_one()
        assert posted and "silent" not in posted[0]


class TestParentRunIsSilent:
    @pytest.mark.asyncio
    async def test_a_silent_cron_parent_is_silent(self):
        state = SimpleNamespace(
            crons=SimpleNamespace(get_job=lambda job_id: SimpleNamespace(silent=True)),
            subagents=None,
        )
        assert await _parent_run_is_silent(state, "cron:job1:run-9") is True

    @pytest.mark.asyncio
    async def test_a_loud_cron_parent_is_not_silent(self):
        state = SimpleNamespace(
            crons=SimpleNamespace(get_job=lambda job_id: SimpleNamespace(silent=False)),
            subagents=None,
        )
        assert await _parent_run_is_silent(state, "cron:job1") is False

    @pytest.mark.asyncio
    async def test_a_silent_subagent_parent_carries_the_chain(self):
        # The parent's own flag was resolved by this rule at ITS admission, so
        # one hop makes a silent cron's grandchildren silent too.
        state = SimpleNamespace(
            crons=None,
            subagents=SimpleNamespace(_agents={"abc": SimpleNamespace(silent=True)}),
        )
        assert await _parent_run_is_silent(state, "subagent:abc") is True

    @pytest.mark.asyncio
    async def test_an_untracked_subagent_parent_is_not_silent(self, tmp_path, monkeypatch):
        # The durable fallback must read the TEST home, never the host's.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = SimpleNamespace(crons=None, subagents=SimpleNamespace(_agents={}))
        assert await _parent_run_is_silent(state, "subagent:gone") is False

    @pytest.mark.asyncio
    async def test_a_watched_surface_is_never_silent(self):
        state = SimpleNamespace(crons=None, subagents=None)
        assert await _parent_run_is_silent(state, "dashboard:tab-1") is False


class TestApiSpawnWiring:
    """The endpoint applies inheritance and forwards the resolved flag."""

    def _spawn_kwargs(self, body: dict[str, Any], state_kw: dict[str, Any]) -> dict[str, Any]:
        import test_handlers_messaging_coverage as cov

        mgr = cov._mgr()
        mgr.spawn.return_value = cov._info()
        state = cov._state(subagents=mgr, **state_kw)
        state.sessions.get_agent_selection.return_value = ("template", "")
        req = cov._Req(state, body)
        req.headers["X-Session-Key"] = body.get("parent_session", "")
        from unittest.mock import AsyncMock, patch

        with patch(
            "kiro_crew.dashboard.handlers._shared.resolve_session_memory_mode",
            AsyncMock(return_value="persistent"),
        ):
            resp = cov._run(cov.mod.api_spawn, req)
        assert mgr.spawn.call_args is not None, getattr(resp, "body", resp)
        return mgr.spawn.call_args.kwargs

    def test_a_silent_cron_parent_spawns_silent(self):
        from types import SimpleNamespace as NS

        kwargs = self._spawn_kwargs(
            {"task": "x", "parent_session": "cron:job1:run-9"},
            {"crons": NS(get_job=lambda jid: NS(silent=True))},
        )
        assert kwargs.get("silent") is True

    def test_a_loud_cron_parent_spawns_loud(self):
        from types import SimpleNamespace as NS

        kwargs = self._spawn_kwargs(
            {"task": "x", "parent_session": "cron:job1"},
            {"crons": NS(get_job=lambda jid: NS(silent=False))},
        )
        assert kwargs.get("silent") is False

    def test_a_silent_cron_overrides_an_explicit_loud_body(self):
        # The main rule: a silent cron's sub-agents never ring — OR, not a
        # default, so silent=false from the caller cannot opt back in.
        from types import SimpleNamespace as NS

        kwargs = self._spawn_kwargs(
            {"task": "x", "parent_session": "cron:job1", "silent": False},
            {"crons": NS(get_job=lambda jid: NS(silent=True))},
        )
        assert kwargs.get("silent") is True

    def test_an_explicit_silent_body_is_forwarded(self):
        kwargs = self._spawn_kwargs(
            {"task": "x", "parent_session": "dashboard:tab-1", "silent": True},
            {"crons": None},
        )
        assert kwargs.get("silent") is True


class TestToolDescriptorsDeclareSilent:
    """The LLM-facing descriptors must declare the field: validate_tool_args
    accepts only what the validator schema allows, but the model only OFFERS
    what the descriptor describes — dropping it there retires the feature
    silently on the next schema edit."""

    def test_both_spawn_tools_describe_silent(self):
        from kiro_crew.mcp_tools.spawn_parts.schemas import schemas

        by_name = {d["name"]: d for d in schemas()}
        for tool in ("spawn_run", "spawn_sub_agents"):
            props = by_name[tool]["inputSchema"]["properties"]
            assert "silent" in props, tool
            assert props["silent"]["type"] == "boolean", tool


class TestBellRoutesHonorSilent:
    def _self(self) -> SimpleNamespace:
        return SimpleNamespace(
            dashboard_state=MagicMock(),
            _notif_meta=lambda key: {"k": key},
        )

    def test_the_tab_gone_route_rings_for_a_loud_run(self):
        host = self._self()
        info = SimpleNamespace(id="a1", silent=False, _report_delivered=False)
        gateway_mod._notify_subagent_tab_gone(host, info, "t", "b", "dashboard:x", "x")
        host.dashboard_state.notify.assert_called_once()
        assert info._report_delivered is True

    def test_the_tab_gone_route_stays_quiet_for_a_silent_run(self):
        # The suppression IS the delivery decision: no bell, still delivered.
        host = self._self()
        info = SimpleNamespace(id="a1", silent=True, _report_delivered=False)
        gateway_mod._notify_subagent_tab_gone(host, info, "t", "b", "dashboard:x", "x")
        host.dashboard_state.notify.assert_not_called()
        assert info._report_delivered is True

    def test_every_completion_bell_note_in_the_injection_routes_is_guarded(self):
        # A ratchet, not a behaviour test: a NEW completion bell note added to
        # the injection routes without an `info.silent` guard reintroduces the
        # unconditional ring this change removes. Each notify("subagent", ...)
        # call must have `info.silent` decided within the ten lines above it.
        source = Path(inj_mod.__file__).read_text(encoding="utf-8").splitlines()
        sites = [
            i for i, line in enumerate(source) if re.search(r"dashboard_state\.notify\(", line)
        ]
        assert sites, "expected at least one completion bell note to pin"

        def _guarded(i: int) -> bool:
            window = source[max(0, i - 10) : i + 1]
            # A real conditional, not a comment that name-drops the flag; an
            # alert note that must stay loud carries `# bell-ok: <reason>`.
            if any("bell-ok:" in line for line in window):
                return True
            return any(re.search(r"^\s*(el)?if .*info\.silent", line) for line in window)

        unguarded = [i + 1 for i in sites if not _guarded(i)]
        assert unguarded == [], f"unguarded completion bell note at lines {unguarded}"


class TestSilentSurvivesTheLiveRecord:
    """The resolved flag outlives `_agents`: the durable row carries it."""

    def test_the_durable_row_records_a_silent_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.subagent_persistence import create_agent_folder, read_state

        create_agent_folder("run-silent", task="t", silent=True)
        create_agent_folder("run-loud", task="t")
        assert (read_state("run-silent") or {}).get("silent") is True
        assert "silent" not in (read_state("run-loud") or {})

    @pytest.mark.asyncio
    async def test_an_evicted_silent_parent_answers_from_its_row(self, tmp_path, monkeypatch):
        # A kept conversation's children name the FIRST run's id; after
        # eviction or a restart that run is gone from _agents, and the durable
        # row is what keeps the chain silent.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("gone-run", task="t", silent=True)
        state = SimpleNamespace(crons=None, subagents=SimpleNamespace(_agents={}))
        assert await _parent_run_is_silent(state, "subagent:gone-run") is True

    def test_run_silent_prefers_the_live_record_whole(self):
        from kiro_crew.subagent_manager.continuation import _run_silent

        assert _run_silent(SimpleNamespace(silent=True), {}) is True
        # Live loud wins over a stale silent row: the live record is whole.
        assert _run_silent(SimpleNamespace(silent=False), {"silent": True}) is False
        assert _run_silent(None, {"silent": True}) is True
        assert _run_silent(None, {}) is False
        assert _run_silent(None, None) is False

    def test_run_silent_is_bound_on_the_manager_facade(self):
        # The continuation component runs on kiro_crew.subagent's globals
        # (bind_component_globals), so the helper must be bound there or
        # every follow-up turn into a kept conversation dies with NameError.
        import kiro_crew.subagent as subagent_facade
        from kiro_crew.subagent_manager import continuation as cont_mod

        assert vars(subagent_facade).get("_run_silent") is cont_mod._run_silent

    def test_a_continuation_prelude_carries_the_runs_silence(self):
        # The prelude inherits the ORIGINAL run's resolved flag, so a silent
        # cron's kept child stays off the bell when continued; the source pin
        # keeps `silent=run_silent` from quietly leaving the spawn kwargs.
        import inspect as _inspect

        from kiro_crew.subagent_manager import continuation as cont_mod

        src = _inspect.getsource(cont_mod)
        assert "_run_silent(original, recorded_row)" in src
        assert "silent=run_silent," in src


class TestContinuationWiring:
    def test_an_active_continuations_silence_answers_for_its_conversation(self):
        import asyncio as _asyncio

        # Children of a continuation name the conversation key (the FIRST
        # run's id); the active continuation runs under its own id, and its
        # resolved flag is the conversation's current answer.
        live = SimpleNamespace(silent=True, done=False, conversation_key="subagent:first-id")
        state = SimpleNamespace(crons=None, subagents=SimpleNamespace(_agents={"cont-id": live}))
        assert _asyncio.run(_parent_run_is_silent(state, "subagent:first-id")) is True

    def test_the_active_continuation_outranks_a_retained_loud_original(self):
        import asyncio as _asyncio

        # The completed first run is retained beside the active continuation;
        # the run still executing is the conversation's current resolution.
        done_loud = SimpleNamespace(silent=False, done=True, conversation_key="")
        active_silent = SimpleNamespace(
            silent=True, done=False, conversation_key="subagent:first-id"
        )
        state = SimpleNamespace(
            crons=None,
            subagents=SimpleNamespace(_agents={"first-id": done_loud, "cont-id": active_silent}),
        )
        assert _asyncio.run(_parent_run_is_silent(state, "subagent:first-id")) is True

    def test_a_retained_completed_record_answers_when_nothing_runs(self):
        import asyncio as _asyncio

        done_silent = SimpleNamespace(silent=True, done=True, conversation_key="")
        state = SimpleNamespace(
            crons=None, subagents=SimpleNamespace(_agents={"first-id": done_silent})
        )
        assert _asyncio.run(_parent_run_is_silent(state, "subagent:first-id")) is True

    def test_api_spawn_continue_applies_the_parent_silence_rule(self):
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock, patch

        import test_handlers_messaging_coverage as cov

        mgr = cov._mgr()
        mgr.continue_conversation.return_value = cov._info()
        mgr.recorded_cwd.return_value = ""
        state = cov._state(subagents=mgr, crons=NS(get_job=lambda jid: NS(silent=True)))
        req = cov._Req(
            state,
            {"task": "follow up", "parent_session": "cron:job1"},
            match_info={"agent_id": "conv-1"},
        )
        req.headers["X-Session-Key"] = "cron:job1"
        with patch(
            "kiro_crew.dashboard.handlers._shared.resolve_session_memory_mode",
            AsyncMock(return_value="persistent"),
        ):
            resp = cov._run(cov.mod.api_spawn_continue, req)
        assert mgr.continue_conversation.call_args is not None, getattr(resp, "body", resp)
        assert mgr.continue_conversation.call_args.kwargs.get("silent") is True
