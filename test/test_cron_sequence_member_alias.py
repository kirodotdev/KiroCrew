"""A sequence cron carrying a captured member runs each step as its OWN crew.

A schedule whose captured execution names a crew member must still dispatch each
``agent_sequence`` step under that step's own crew runtime, workspace, pinned
model and capability gates. The captured member is the run's shared memory
identity, not the agent every step runs as. Before the fix, the captured-member
pin in ``_resolve_cron_agent`` collapsed every step onto the one member, so the
other crews named in the sequence never actually ran.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from member_memory_helpers import write_member_home

from kiro_crew import agent_discovery, subagent
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron import CronJob
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    read_session_execution,
)


async def _cron_callback(monkeypatch, cfg):
    from kiro_crew.slack import gateway

    # Resolution reads ``live.snapshot() or self._cfg``; pin both to the test
    # config so the two crew aliases are visible without a live config watcher.
    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)

    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("stop before the provider"))
    gw.ctx_builder = MagicMock()
    gw.slack = gw.conv_log = gw.dashboard_state = gw.subagent_mgr = None
    gw._owner_id = "owner"
    gw._cron_injecting = {}
    gw._no_crons = False
    gw._cfg = cfg
    gw.cron_svc = None
    callbacks = []

    async def create(on_job=None, **kwargs):
        callbacks.append(on_job)
        service = MagicMock()
        service.start = AsyncMock()
        return service

    monkeypatch.setattr(gateway.CronService, "create", create)
    monkeypatch.setattr(
        gateway, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False))
    )
    await gw._init_cron()
    return gw, callbacks[0]


@pytest.mark.asyncio
async def test_each_crew_in_a_sequence_is_dispatched_as_itself(monkeypatch, tmp_path):
    write_member_home(config_dir(), "alpha", "beta")  # two crew members, alpha and beta
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()

    # Give crew beta its own kiro_agent and a mapped workspace so the step's
    # runtime (agent) and tree (cwd) are pinned, not just its crew_agent alias.
    # A regression that put the step back on the captured member's runtime or in
    # the base workspace would then red, not pass silently.
    from kiro_crew.config.sections import WorkspaceConfig

    beta_tree = tmp_path / "beta-tree"
    beta_tree.mkdir()
    cfg.workspaces["beta-ws"] = WorkspaceConfig(dir=str(beta_tree))
    cfg.agents["beta"].kiro_agent = "beta-mode"
    cfg.agents["beta"].workspace = "beta-ws"

    gw, callback = await _cron_callback(monkeypatch, cfg)

    # The schedule was created as alpha, so it carries alpha's captured execution.
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq",
        name="sequence",
        message="task",
        agent_sequence=["beta", "alpha"],  # first step is crew beta
        execution_context=captured.to_record(),
    )
    with pytest.raises(RuntimeError, match="stop before the provider"):
        await callback(job)

    first = gw.sessions.get_or_create.call_args_list[0]
    assert first.args[0] == "cron:seq:beta"  # the session key names beta
    # Each step resolves its OWN crew, not the captured member alpha: its
    # crew_agent alias, its kiro_agent mode, and its own workspace tree.
    assert first.kwargs["crew_agent"] == "beta"
    assert first.kwargs["agent"] == "beta-mode"
    assert first.kwargs["cwd"] == str(beta_tree)


@pytest.mark.asyncio
async def test_sequence_step_binds_its_spawn_policy_to_the_captured_store(monkeypatch, tmp_path):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["beta"].kiro_agent = "beta-mode"

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", agents_dir)
    for name, allowed in (("alpha", "alpha-child"), ("beta-mode", "beta-child")):
        (agents_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "toolsSettings": {"subagent": {"availableAgents": [allowed]}},
                }
            ),
            encoding="utf-8",
        )

    _gw, callback = await _cron_callback(monkeypatch, cfg)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-authz",
        name="sequence authorization",
        message="task",
        agent_sequence=["beta", "alpha"],
        execution_context=captured.to_record(),
    )

    with pytest.raises(RuntimeError, match="stop before the provider"):
        await callback(job)

    bound = read_session_execution("cron:seq-authz:beta", required=True)
    assert bound.template_id == "beta-mode"
    assert bound.selection_kind == "template"
    assert bound.selection_name == "beta"
    assert subagent.parent_spawn_allowlists(bound.template_id) == (("beta-child",),)
    assert bound.member_id == captured.member_id
    assert bound.store == captured.store
    assert bound.memory_mode == captured.memory_mode
    assert bound.app == captured.app


@pytest.mark.asyncio
async def test_second_sequence_fire_rebinds_a_prior_captured_execution(monkeypatch, tmp_path):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["beta"].kiro_agent = "beta-mode"

    _gw, callback = await _cron_callback(monkeypatch, cfg)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-refire",
        name="sequence refire",
        message="task",
        agent_sequence=["beta", "alpha"],
        execution_context=captured.to_record(),
    )
    session_key = "cron:seq-refire:beta"

    # Model the first fire under the earlier sequence binding, which published
    # the schedule's captured execution before attempting provider startup.
    with monkeypatch.context() as first_fire:
        first_fire.setattr(ExecutionContext, "with_template", lambda self, *_args: self)
        with pytest.raises(RuntimeError, match="stop before the provider"):
            await callback(job)
    assert read_session_execution(session_key, required=True) == captured

    # The next fire upgrades only the step selection and reaches the provider.
    with pytest.raises(RuntimeError, match="stop before the provider"):
        await callback(job)
    assert read_session_execution(session_key, required=True) == captured.with_template(
        "beta-mode", "beta"
    )


@pytest.mark.asyncio
async def test_first_step_agent_mismatch_defers_the_whole_sequence(monkeypatch):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["beta"].kiro_agent = "beta-mode"
    cfg.agents["alpha"].kiro_agent = "alpha-mode"

    gw, callback = await _cron_callback(monkeypatch, cfg)
    gw.sessions._get_session_agent = MagicMock(
        side_effect=lambda key: (
            "retained-beta-mode" if key == "cron:seq-first-mismatch:beta" else ""
        )
    )
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-first-mismatch",
        name="sequence first mismatch",
        message="task",
        agent_sequence=["beta", "alpha"],
        execution_context=captured.to_record(),
    )

    result = await callback(job)

    assert result is None
    gw.sessions.get_or_create.assert_not_awaited()
    assert job.run_never_started is True
    assert "sequence step 'beta'" in (job.last_error or "")
    assert "retained-beta-mode" in (job.last_error or "")
    assert "beta-mode" in (job.last_error or "")


@pytest.mark.asyncio
async def test_later_step_agent_mismatch_defers_before_first_dispatch(monkeypatch):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["beta"].kiro_agent = "beta-mode"
    cfg.agents["alpha"].kiro_agent = "alpha-mode"

    gw, callback = await _cron_callback(monkeypatch, cfg)
    gw.sessions._get_session_agent = MagicMock(
        side_effect=lambda key: (
            "retained-alpha-mode" if key == "cron:seq-later-mismatch:alpha" else ""
        )
    )
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-later-mismatch",
        name="sequence later mismatch",
        message="task",
        agent_sequence=["beta", "alpha"],
        execution_context=captured.to_record(),
    )

    result = await callback(job)

    assert result is None
    gw.sessions.get_or_create.assert_not_awaited()
    assert gw.sessions._get_session_agent.call_args_list == [
        (("cron:seq-later-mismatch:beta",),),
        (("cron:seq-later-mismatch:alpha",),),
    ]
    assert job.run_never_started is True
    assert "sequence step 'alpha'" in (job.last_error or "")
    assert "retained-alpha-mode" in (job.last_error or "")
    assert "alpha-mode" in (job.last_error or "")


@pytest.mark.asyncio
async def test_an_empty_sequence_step_aborts_a_member_captured_cron(monkeypatch):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()

    gw, callback = await _cron_callback(monkeypatch, cfg)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-empty-step",
        name="sequence empty step",
        message="task",
        agent_sequence=["", "beta"],
        execution_context=captured.to_record(),
    )

    result = await callback(job)

    assert result is None
    gw.sessions.get_or_create.assert_not_awaited()
    assert job.run_never_started is True
    assert "empty agent_sequence step cannot resolve a crew; refusing to dispatch" in (
        job.last_error or ""
    )


@pytest.mark.asyncio
async def test_an_empty_step_does_not_consume_a_delete_after_run_oneshot(monkeypatch):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()

    gw, callback = await _cron_callback(monkeypatch, cfg)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-empty-step-oneshot",
        name="sequence empty step one-shot",
        message="task",
        agent_sequence=["", "beta"],
        execution_context=captured.to_record(),
        delete_after_run=True,
    )

    result = await callback(job)

    assert result is None
    gw.sessions.get_or_create.assert_not_awaited()
    assert job.run_never_started is True
    assert "empty agent_sequence step cannot resolve a crew; refusing to dispatch" in (
        job.last_error or ""
    )


def _let_sequence_complete(monkeypatch, gw):
    """Replace the stop-before-provider seam with one complete sequence turn."""
    from kiro_crew.slack import gateway

    client = MagicMock()
    gw.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    gw.sessions.reset = AsyncMock()
    monkeypatch.setattr(gateway, "publish_turn_identity", AsyncMock())
    monkeypatch.setattr(gateway, "run_in_embed_pool", AsyncMock(return_value=("message", None)))
    monkeypatch.setattr(
        gateway,
        "_cron_stream_with_posttoken_resume",
        AsyncMock(return_value=("done", 0)),
    )
    monkeypatch.setattr(gateway, "consume_reinjection", lambda *_args: False)
    monkeypatch.setattr(gateway, "rearm_reinjection", MagicMock())
    monkeypatch.setattr(gateway, "persist_token_record_async", AsyncMock())


@pytest.mark.asyncio
async def test_a_crew_edit_after_the_sweep_does_not_change_the_dispatch(monkeypatch, tmp_path):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()

    from kiro_crew.config.sections import WorkspaceConfig

    beta_tree = tmp_path / "beta-tree"
    edited_tree = tmp_path / "edited-tree"
    beta_tree.mkdir()
    edited_tree.mkdir()
    cfg.workspaces["beta-ws"] = WorkspaceConfig(dir=str(beta_tree))
    cfg.workspaces["edited-ws"] = WorkspaceConfig(dir=str(edited_tree))
    cfg.agents["beta"].kiro_agent = "beta-mode"
    cfg.agents["beta"].workspace = "beta-ws"
    cfg.agents["alpha"].kiro_agent = "alpha-mode"

    gw, callback = await _cron_callback(monkeypatch, cfg)

    def edit_beta_after_the_sweep(key):
        if key == "cron:seq-live-edit:alpha":
            cfg.agents["beta"].kiro_agent = "edited-beta-mode"
            cfg.agents["beta"].workspace = "edited-ws"
        return ""

    gw.sessions._get_session_agent = MagicMock(side_effect=edit_beta_after_the_sweep)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-live-edit",
        name="sequence live edit",
        message="task",
        agent_sequence=["beta", "alpha"],
        execution_context=captured.to_record(),
    )

    with pytest.raises(RuntimeError, match="stop before the provider"):
        await callback(job)

    assert gw.sessions._get_session_agent.call_args_list == [
        (("cron:seq-live-edit:beta",),),
        (("cron:seq-live-edit:alpha",),),
    ]
    first = gw.sessions.get_or_create.call_args_list[0]
    assert first.kwargs["agent"] == "beta-mode"
    assert first.kwargs["cwd"] == str(beta_tree)
    assert (
        read_session_execution("cron:seq-live-edit:beta", required=True).template_id == "beta-mode"
    )


@pytest.mark.asyncio
async def test_each_sequence_step_is_resolved_exactly_once_per_fire(monkeypatch):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["beta"].kiro_agent = "beta-mode"
    cfg.agents["alpha"].kiro_agent = "alpha-mode"

    resolve_calls = 0
    resolve_agent_bindings = loader.resolve_agent_bindings

    def counting_wrapper(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return resolve_agent_bindings(*args, **kwargs)

    monkeypatch.setattr(loader, "resolve_agent_bindings", counting_wrapper)
    gw, callback = await _cron_callback(monkeypatch, cfg)
    _let_sequence_complete(monkeypatch, gw)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-one-resolve",
        name="sequence one resolve",
        message="task",
        agent_sequence=["beta", "alpha"],
        execution_context=captured.to_record(),
    )

    assert await callback(job) == "done"
    assert resolve_calls == 2


@pytest.mark.asyncio
async def test_a_sequence_repeating_one_crew_dispatches_it_twice(monkeypatch):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["beta"].kiro_agent = "beta-mode"

    gw, callback = await _cron_callback(monkeypatch, cfg)
    _let_sequence_complete(monkeypatch, gw)
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-repeat",
        name="sequence repeat",
        message="task",
        agent_sequence=["beta", "beta"],
        execution_context=captured.to_record(),
    )

    assert await callback(job) == "done"
    assert [call.args[0] for call in gw.sessions.get_or_create.call_args_list] == [
        "cron:seq-repeat:beta",
        "cron:seq-repeat:beta",
    ]


@pytest.mark.asyncio
async def test_a_config_write_between_resolve_and_dispatch_keeps_the_dispatched_template(
    monkeypatch,
):
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["beta"].kiro_agent = "stale-beta-mode"
    cfg.agents["alpha"].kiro_agent = "alpha-mode"

    gw, callback = await _cron_callback(monkeypatch, cfg)
    _let_sequence_complete(monkeypatch, gw)
    prepared_beta = MagicMock()
    prepared_beta.loaded_capability_template = "prepared-beta-mode"
    prepared_alpha = MagicMock()
    prepared_alpha.loaded_capability_template = "alpha-mode"
    gw.sessions.get_or_create = AsyncMock(
        side_effect=[
            (prepared_beta, True, False),
            (prepared_alpha, True, False),
        ]
    )
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha", "persistent", "app"
    )
    job = CronJob(
        id="seq-config-write",
        name="sequence config write",
        message="task",
        agent_sequence=["beta", "alpha"],
        execution_context=captured.to_record(),
    )

    assert await callback(job) == "done"
    bound = read_session_execution("cron:seq-config-write:beta", required=True)
    assert bound.template_id == "prepared-beta-mode"
    assert bound.template_id != "stale-beta-mode"
    assert bound.selection_name == "beta"
    assert bound.member_id == captured.member_id
    assert bound.store == captured.store
    assert bound.memory_mode == captured.memory_mode
    assert bound.app == captured.app
