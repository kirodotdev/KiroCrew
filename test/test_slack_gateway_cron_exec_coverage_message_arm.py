"""Coverage for the ``message`` (LLM) arm of the Kiro Crew gateway's cron callback.

Covers the arm's fire-time governance denial, a crewmate goal check-in passing the same
gate, the ``_init_cron`` wiring that skips the reaper without a session manager, and a
job whose ``agent_id`` is an agent alias dispatching that alias's kiro agent mode and
workspace instead of the alias itself. The deterministic arms are in
``test_slack_gateway_cron_exec_coverage.py``; the shared harness comes from
``slack_gateway_cron_exec_coverage_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_cron_exec_coverage_helpers import (
    _blind_sel,
    _cron_cb,
    _cron_message_cb,
    _cron_service_double,
    _job,
    _make_orchestrator,
    _message_arm_sessions,
    _mock_dashboard_state,
)

from kiro_crew.config.loader import KiroCrewConfig

# ═══════════════════════════════════════════════════════════════════════════
# message-mode denial + _init_cron wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestCronMessageDenialAndWiring:
    @pytest.mark.asyncio
    async def test_message_job_fire_time_denial(self):
        """An LLM cron denied at fire time never dispatches a turn."""
        orch = _make_orchestrator()
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        job = _job()
        async with _cron_cb(orch, gate_reason="capabilities.cron off", sel_obj=_blind_sel()) as cb:
            assert await cb(job) is None
        assert job.fire_time_denied is True
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_goal_checkin_denied_at_fire_time_never_fires(self):
        """A crewmate's goal check-in passes the same gate as a message job."""
        from kiro_crew.dashboard import goal_reminder

        orch = _make_orchestrator()
        job = _job(managed_by=goal_reminder.managed_key("ava", "daily"))
        fire = AsyncMock(return_value="started")
        with patch.object(goal_reminder, "fire", fire):
            async with _cron_cb(orch, gate_reason="capabilities.cron off") as cb:
                assert await cb(job) is None
        fire.assert_not_awaited()
        assert job.fire_time_denied is True
        assert job.last_status == "error"
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_goal_checkin_allowed_by_the_gate_fires(self):
        from kiro_crew.dashboard import goal_reminder

        orch = _make_orchestrator()
        job = _job(managed_by=goal_reminder.managed_key("ava", "daily"))
        fire = AsyncMock(return_value="started")
        with patch.object(goal_reminder, "fire", fire):
            async with _cron_cb(orch, gate_reason="") as cb:
                assert await cb(job) == "started"
        fire.assert_awaited_once()
        assert job.fire_time_denied is False

    @pytest.mark.asyncio
    async def test_reaper_not_started_without_sessions(self):
        """Arming the scheduler without a session manager skips the reaper."""
        orch = _make_orchestrator(no_crons=False)
        orch.sessions = None
        svc = _cron_service_double()
        async with _cron_cb(orch, svc=svc):
            pass
        svc.start.assert_awaited_once()
        svc.start_reaper.assert_not_called()


class TestCronResolvesAgentAlias:
    """A cron whose agent is a Kiro Crew ALIAS must dispatch the alias's real
    kiro agent mode + workspace, not the alias verbatim.

    Regression: a cron created in a Slack channel carries that channel's agent
    alias (e.g. ``in-3d``) in ``job.agent_id``. kiro-cli only accepts a
    materialized agent MODE, so dispatching the alias failed every run with
    "Agent mode 'in-3d' is not available on this session … its
    ~/.kiro/agents/in-3d.json is likely missing". The Slack chat path already
    collapses the alias via resolve_agent_bindings; the cron path did not.
    """

    @staticmethod
    def _cfg_with_alias(ws_dir: str) -> Any:
        from kiro_crew.config.loader import (
            KiroCrewAgentConfig,
            WorkspaceConfig,
        )

        return KiroCrewConfig(
            agents={
                "in-3d": KiroCrewAgentConfig(
                    kiro_agent="kirocrew",
                    workspace="in-3d",
                    memory_store="default",
                ),
            },
            workspaces={"in-3d": WorkspaceConfig(dir=ws_dir)},
        )

    @pytest.mark.asyncio
    async def test_alias_agent_id_dispatches_resolved_kiro_agent_and_cwd(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.slack import gateway as _gw

        monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
        ws_dir = str(tmp_path / "in-3d-ws")
        orch = _make_orchestrator()
        orch._cfg = self._cfg_with_alias(ws_dir)
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        # agent_id is the channel alias; the fix must resolve it to kirocrew.
        job = _job(id="ja1", name="alias probe", agent_id="in-3d")

        async with _cron_message_cb(orch, result_text="ok") as callback:
            await callback(job)

        orch.sessions.get_or_create.assert_awaited()
        kw = orch.sessions.get_or_create.await_args.kwargs
        # Dispatched the alias's kiro_agent, NOT the raw alias 'in-3d'.
        assert kw.get("agent") == "kirocrew"
        # Ran in the alias's workspace, not the default cwd.
        assert kw.get("cwd") == ws_dir
        # Carried the alias as crew_agent so prepare_runtime still resolves the
        # member identity (capability gates, model/effort pins, watchdogs).
        assert kw.get("crew_agent") == "in-3d"

    @pytest.mark.asyncio
    async def test_relative_workspace_dir_is_anchored_under_data_home(self, tmp_path, monkeypatch):
        """A RELATIVE workspace dir must be anchored by the placement rule
        (config_dir()/dirname), not handed to the provider as a bare relative
        string that would resolve against the gateway process directory."""
        from kiro_crew.config import loader as _loader
        from kiro_crew.slack import gateway as _gw

        monkeypatch.setattr(_gw.live, "snapshot", lambda: None)

        orch = _make_orchestrator()
        # Shipped default is the relative "workspace"; use a relative dir here.
        orch._cfg = self._cfg_with_alias("my-rel-ws")
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(id="ja3", name="rel-ws probe", agent_id="in-3d")

        async with _cron_message_cb(orch, result_text="ok") as callback:
            await callback(job)

        kw = orch.sessions.get_or_create.await_args.kwargs
        expected = str(_loader.workspace_dir_from_entry(orch._cfg.workspaces["in-3d"]))
        # Anchored absolute path, not the raw relative "my-rel-ws".
        assert kw.get("cwd") == expected
        assert Path(kw.get("cwd")).is_absolute()
        assert kw.get("cwd") != "my-rel-ws"

    @pytest.mark.asyncio
    async def test_hot_added_alias_is_resolved_from_the_live_snapshot(self, tmp_path, monkeypatch):
        """An alias created at runtime (dashboard/CLI) lands in the live config
        snapshot but not in the boot self._cfg. Resolution must consult
        live.snapshot() so a hot-added alias resolves instead of being
        dispatched raw and failing closed on every fire."""
        from kiro_crew.slack import gateway as _gw

        ws_dir = str(tmp_path / "in-3d-ws")
        orch = _make_orchestrator()
        # Boot config knows NO agents — the alias only exists in the live snapshot.
        orch._cfg = KiroCrewConfig()
        live_cfg = self._cfg_with_alias(ws_dir)
        monkeypatch.setattr(_gw.live, "snapshot", lambda: live_cfg)
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(id="ja4", name="hot-add probe", agent_id="in-3d")

        async with _cron_message_cb(orch, result_text="ok") as callback:
            await callback(job)

        kw = orch.sessions.get_or_create.await_args.kwargs
        # Resolved from the live snapshot, not left as the raw alias.
        assert kw.get("agent") == "kirocrew"
        assert kw.get("crew_agent") == "in-3d"

    @pytest.mark.asyncio
    async def test_recreated_alias_does_not_cross_captured_member_identity(
        self, tmp_path, monkeypatch
    ):
        """A member cron captures its member_id at authoring. If the alias name
        is later deleted and recreated under a DIFFERENT member_id, resolution
        must pin to the captured identity (member_config_for_id), never bind the
        same-name-but-different-identity record — which would run the new member
        while writing into the retired member's memory silo."""
        from kiro_crew.config.loader import KiroCrewAgentConfig, WorkspaceConfig
        from kiro_crew.slack import gateway as _gw

        monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
        orch = _make_orchestrator()
        # Live cfg: alias 'in-3d' now belongs to a DIFFERENT member than the one
        # the cron captured.
        orch._cfg = KiroCrewConfig(
            agents={
                "in-3d": KiroCrewAgentConfig(
                    kiro_agent="kirocrew",
                    workspace="in-3d",
                    memory_store="default",
                    member_id="mid-recreated",
                )
            },
            workspaces={"in-3d": WorkspaceConfig(dir=str(tmp_path / "ws"))},
        )
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        # Cron captured member_id 'mid-original' (the retired member).
        job = _job(
            id="ja6",
            name="recreated-alias probe",
            agent_id="in-3d",
            member_id="mid-original",
            memory_store="default",
        )

        async with _cron_message_cb(orch, result_text="ok") as callback:
            await callback(job)

        kw = orch.sessions.get_or_create.await_args.kwargs
        # The captured identity 'mid-original' resolves to NO live agent, so the
        # resolver must NOT dispatch the recreated 'in-3d' runtime. It falls back
        # to the raw value unchanged rather than crossing the identity.
        assert kw.get("agent") == "in-3d"
        assert kw.get("crew_agent") is None
        assert kw.get("cwd") is None

    @pytest.mark.asyncio
    async def test_unmapped_workspace_anchors_to_base_dir_not_default_hop(
        self, tmp_path, monkeypatch
    ):
        """An alias whose workspace NAME is not declared in cfg.workspaces
        anchors to the BASE workspace directory (workspace_dir_from_entry(None)),
        never to cfg.default_workspace's dir and never to a bare cwd=None that
        would resolve against the gateway process directory."""
        from kiro_crew.config import loader as _loader
        from kiro_crew.config.loader import KiroCrewAgentConfig
        from kiro_crew.slack import gateway as _gw

        monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
        # Alias present, but its workspace name 'ghost' is NOT in cfg.workspaces.
        orch = _make_orchestrator()
        orch._cfg = KiroCrewConfig(
            agents={
                "in-3d": KiroCrewAgentConfig(
                    kiro_agent="kirocrew", workspace="ghost", memory_store="default"
                )
            },
            workspaces={},
        )
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(id="ja5", name="unmapped-ws probe", agent_id="in-3d")

        async with _cron_message_cb(orch, result_text="ok") as callback:
            await callback(job)

        kw = orch.sessions.get_or_create.await_args.kwargs
        base = str(_loader.workspace_dir_from_entry(None))
        assert kw.get("agent") == "kirocrew"
        assert kw.get("crew_agent") == "in-3d"
        # Anchored to the base workspace dir, not left None.
        assert kw.get("cwd") == base
        assert kw.get("cwd") is not None

    @pytest.mark.asyncio
    async def test_unknown_agent_is_passed_through_unchanged(self, tmp_path, monkeypatch):
        """A non-alias agent (already a real mode, or unmapped) is unchanged:
        the resolver misses and the raw value is dispatched with cwd=None."""
        from kiro_crew.slack import gateway as _gw

        monkeypatch.setattr(_gw.live, "snapshot", lambda: None)
        ws_dir = str(tmp_path / "in-3d-ws")
        orch = _make_orchestrator()
        orch._cfg = self._cfg_with_alias(ws_dir)
        orch.sessions = _message_arm_sessions()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(id="ja2", name="passthrough probe", agent_id="kirocrew-lite")

        async with _cron_message_cb(orch, result_text="ok") as callback:
            await callback(job)

        kw = orch.sessions.get_or_create.await_args.kwargs
        # Not an alias in config.agents → dispatched verbatim, no workspace cwd.
        assert kw.get("agent") == "kirocrew-lite"
        assert kw.get("cwd") is None
        # No alias to collapse → no crew identity is asserted.
        assert kw.get("crew_agent") is None
