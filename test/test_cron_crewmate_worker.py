"""A crewmate's scheduled agent turn runs on its worker, not on its conductor.

A cron job bound to a crewmate captures a ``member`` selection, and a member
selection chats on the conductor, which mounts none of the crewmate's own
servers. The fire runs the turn on the crewmate's worker instead
(``mate_worker.cron_worker_execution``): the same record shape a hand-off gives
the worker, under the job's own ``cron:`` key, so the worker projection reads
that crewmate's kit. A job captured under 1.0 (``template_id`` ``kirocrew`` with
a member id) takes the same path. A non-crewmate job is unchanged.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from member_memory_helpers import write_member_home

from kiro_crew import mate_worker
from kiro_crew.config import loader
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron import CronJob
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    read_session_execution,
)
from kiro_crew.mate_worker import MATE_WORKER_AGENT

SLACK = {
    "name": "alpha",
    "prompt": "",
    "tools": ["@slack-mcp"],
    "mcpServers": {"slack-mcp": {"command": "slack-mcp"}},
}


async def _cron_callback(monkeypatch, cfg):
    from kiro_crew.slack import gateway

    monkeypatch.setattr(gateway.live, "snapshot", lambda: cfg)
    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("stop before the provider"))
    gw.sessions.get = MagicMock(return_value=None)
    gw.ctx_builder = MagicMock()
    gw.slack = gw.conv_log = gw.dashboard_state = gw.subagent_mgr = None
    gw._owner_id = "owner"
    gw._cron_injecting = {}
    gw._cron_runs_holding = set()
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


async def _fire(
    monkeypatch,
    *,
    owns_file: bool,
    prior_run: bool = False,
    file_present: bool = True,
    user_spec: str = "",
    on_record: bool = True,
    bound_agent: str = "",
    user_doc: str = "",
):
    write_member_home(config_dir(), "alpha")
    if owns_file or bound_agent:
        # A crewmate owns its file when its binding names it (``mate_file_for_member``).
        path = config_dir() / "config.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["agents"]["alpha"]["kiro_agent"] = "alpha" if owns_file else bound_agent
        path.write_text(json.dumps(doc), encoding="utf-8")
    if bound_agent:
        # The member's shared template: an installed user-level agent.
        user = paths_mod.kiro_agents_dir()
        user.mkdir(parents=True, exist_ok=True)
        (user / f"{bound_agent}.json").write_text(
            json.dumps({"name": bound_agent, "prompt": ""}), encoding="utf-8"
        )
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    mates = paths_mod.mate_agents_dir()
    mates.mkdir(parents=True, exist_ok=True)
    if file_present:
        (mates / "alpha.json").write_text(json.dumps(SLACK), encoding="utf-8")
    if owns_file and on_record:
        # What every crewmate file write leaves behind, kept after a delete.
        from kiro_crew import mate_config

        mate_config._owners_path().write_text(
            json.dumps({"alpha": mate_config._this_home()}), encoding="utf-8"
        )
    if user_spec:
        # An installed agent: the user-level folder declares "alpha" under this file.
        user = paths_mod.kiro_agents_dir()
        user.mkdir(parents=True, exist_ok=True)
        (user / user_spec).write_text(
            user_doc or json.dumps({"name": "alpha", "prompt": ""}), encoding="utf-8"
        )
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    gw, callback = await _cron_callback(monkeypatch, cfg)
    # The 1.0 capture: a member selection on the shared template.
    captured = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "kirocrew"
    )
    job = CronJob(
        id="hourly", name="hourly", message="read slack", execution_context=captured.to_record()
    )
    if prior_run:
        # What a fire under 1.0 left under the job's stable key.
        bind_session_execution("cron:hourly", captured)
    with pytest.raises(RuntimeError, match="stop before the provider"):
        await callback(job)
    return gw.sessions.get_or_create.call_args, captured


@pytest.mark.asyncio
async def test_a_crewmates_cron_turn_runs_on_its_worker_with_its_servers(monkeypatch):
    call, captured = await _fire(monkeypatch, owns_file=True)

    assert call.args[0] == "cron:hourly"  # the job keeps its own key
    assert call.kwargs["agent"] == MATE_WORKER_AGENT
    record = read_session_execution("cron:hourly")
    assert mate_worker.is_own_worker_execution(record)
    # Same member, store and mode as the capture: only the selection moved.
    assert (record.member_id, record.store, record.memory_mode) == (
        captured.member_id,
        captured.store,
        captured.memory_mode,
    )
    base = {"name": MATE_WORKER_AGENT, "prompt": "base", "tools": [], "mcpServers": {}}
    spec = mate_worker.worker_session_spec(base, "cron:hourly")
    assert "slack-mcp" in spec["mcpServers"]


@pytest.mark.asyncio
async def test_a_cron_of_a_crewmate_without_its_own_file_is_unchanged(monkeypatch):
    call, captured = await _fire(monkeypatch, owns_file=False)

    assert call.kwargs["agent"] != MATE_WORKER_AGENT
    assert call.kwargs["crew_agent"] == "alpha"
    _same_member_run(read_session_execution("cron:hourly"), captured, call.kwargs["agent"])


@pytest.mark.asyncio
async def test_a_crewmate_whose_own_file_was_deleted_fails_closed_on_its_worker(monkeypatch):
    """The binding still claims its own file but the file is gone: the turn goes to
    the worker, which refuses, instead of the conductor running it without limits."""
    call, captured = await _fire(monkeypatch, owns_file=True, file_present=False)

    assert call.kwargs["agent"] == MATE_WORKER_AGENT
    record = read_session_execution("cron:hourly")
    assert mate_worker.is_own_worker_execution(record)
    assert record.member_id == captured.member_id
    base = {"name": MATE_WORKER_AGENT, "prompt": "base", "tools": [], "mcpServers": {}}
    with pytest.raises(mate_worker.WorkerKitUnreadable, match="missing or shadowed"):
        mate_worker.worker_session_spec(base, "cron:hourly")


def _same_member_run(record, captured, dispatched):
    """*record* is *captured*'s member run, naming the agent the fire dispatched."""
    assert (record.member_id, record.store, record.memory_mode, record.selection_kind) == (
        captured.member_id,
        captured.store,
        captured.memory_mode,
        captured.selection_kind,
    )
    assert record.template_id == dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fire",
    [
        {"owns_file": False, "bound_agent": "helper"},
        {"owns_file": True, "file_present": True, "user_spec": "alpha.json"},
    ],
    ids=["member-route", "shadowing-template-route"],
)
async def test_the_spawn_gate_reads_the_agent_a_member_cron_dispatches(monkeypatch, fire):
    """The 1.0 capture names ``kirocrew``; the fire dispatches the Bot's own
    agent. A ``spawn_run`` from the turn is vetted against the agent's allowlist,
    on the record-reading gate and on ``/api/spawn``'s scheduler capture."""
    from kiro_crew import agent_discovery
    from kiro_crew.subagent_parts.agent_policy import (
        _parent_template_for_spawn,
        _vet_parent_available_agents,
        parent_spawn_policy,
        parent_template_spawn_policy,
        with_cron_dispatched_policy,
    )

    call, captured = await _fire(monkeypatch, **fire)

    dispatched = call.kwargs["agent"]
    assert dispatched and dispatched != captured.template_id
    assert _parent_template_for_spawn("cron:hourly") == dispatched
    spec = {"name": dispatched, "prompt": ""}
    spec["toolsSettings"] = {"subagent": {"availableAgents": ["scout"]}}
    (paths_mod.kiro_agents_dir() / f"{dispatched}.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )
    agent_discovery.clear_list_agents_cache()
    scheduler = parent_template_spawn_policy("cron:hourly", captured.template_id)
    for policy in (
        parent_spawn_policy("cron:hourly"),
        with_cron_dispatched_policy("cron:hourly", captured, scheduler),
    ):
        assert _vet_parent_available_agents(policy, "other")
        assert _vet_parent_available_agents(policy, "scout") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("file_present", [True, False])
async def test_a_crewmate_shadowed_by_a_user_level_template_runs_its_cron_there(
    monkeypatch, file_present
):
    """A Bot on record whose name a user-level template also declares. Its chats
    run on that template (``member_chat_agent``), so its cron turn keeps the same
    member route instead of the worker, which refuses a shadowed file."""
    call, captured = await _fire(
        monkeypatch, owns_file=True, file_present=file_present, user_spec="alpha.json"
    )

    assert call.kwargs["agent"] != MATE_WORKER_AGENT
    assert call.kwargs["crew_agent"] == "alpha"
    _same_member_run(read_session_execution("cron:hourly"), captured, call.kwargs["agent"])


@pytest.mark.asyncio
@pytest.mark.parametrize("file_present", [True, False])
@pytest.mark.parametrize(
    "user_doc",
    [json.dumps({"name": "beta", "prompt": ""}), "{not json"],
    ids=["misnamed", "unparseable"],
)
async def test_a_same_stem_file_that_does_not_declare_the_bot_keeps_its_worker(
    monkeypatch, file_present, user_doc
):
    """A user-level ``alpha.json`` that names another agent, or does not parse, does
    not answer ``alpha``: the turn stays on the worker, which refuses a shadowed or
    missing own file rather than the conductor running it without the Bot's limits."""
    call, _ = await _fire(
        monkeypatch,
        owns_file=True,
        file_present=file_present,
        user_spec="alpha.json",
        user_doc=user_doc,
    )

    assert call.kwargs["agent"] == MATE_WORKER_AGENT
    assert mate_worker.is_own_worker_execution(read_session_execution("cron:hourly"))


@pytest.mark.asyncio
@pytest.mark.parametrize("user_spec", ["alpha.json", "someapp--alpha.json"])
async def test_an_installed_agent_named_like_its_member_keeps_its_cron(monkeypatch, user_spec):
    """``kiro_agent`` == alias with no crewmate file is a crewmate only when no other
    spec answers that name: an installed (or an app's namespaced) agent is not."""
    call, captured = await _fire(
        monkeypatch, owns_file=True, file_present=False, user_spec=user_spec, on_record=False
    )

    assert call.kwargs["agent"] != MATE_WORKER_AGENT
    assert call.kwargs["crew_agent"] == "alpha"
    _same_member_run(read_session_execution("cron:hourly"), captured, call.kwargs["agent"])


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_in_workspace", [False, True])
async def test_a_self_named_binding_that_never_had_a_crewmate_file_keeps_its_cron(
    monkeypatch, tmp_path, declared_in_workspace
):
    """No user-level spec and no crewmate file on record: a built-in mode the
    harness knows by name, or an agent only its workspace's ``.kiro/agents``
    declares. Neither was ever a crewmate file, so the cron keeps its route."""
    if declared_in_workspace:
        agents = tmp_path / "project" / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "alpha.json").write_text(json.dumps({"name": "alpha"}), encoding="utf-8")
        monkeypatch.chdir(tmp_path / "project")
    call, captured = await _fire(monkeypatch, owns_file=True, file_present=False, on_record=False)

    assert call.kwargs["agent"] != MATE_WORKER_AGENT
    _same_member_run(read_session_execution("cron:hourly"), captured, call.kwargs["agent"])


@pytest.mark.asyncio
async def test_another_instances_deleted_crewmate_does_not_refuse_this_ones_cron(
    monkeypatch, tmp_path
):
    """Two gateways share one Kiro home with separate data homes. The other one
    owned a crewmate ``alpha``; this one binds ``alpha`` to an installed agent."""
    from kiro_crew import mate_config

    path = mate_config._owners_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"alpha": str(tmp_path / "other-data-home")}), encoding="utf-8")
    call, captured = await _fire(monkeypatch, owns_file=True, file_present=False, on_record=False)

    assert call.kwargs["agent"] != MATE_WORKER_AGENT
    _same_member_run(read_session_execution("cron:hourly"), captured, call.kwargs["agent"])


@pytest.mark.asyncio
async def test_an_unreadable_owner_record_keeps_a_self_named_binding_refused(monkeypatch):
    from kiro_crew import mate_config

    path = mate_config._owners_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    call, _captured = await _fire(monkeypatch, owns_file=True, file_present=False, on_record=False)

    assert call.kwargs["agent"] == MATE_WORKER_AGENT


@pytest.mark.asyncio
async def test_a_record_a_1_0_run_left_moves_onto_the_worker_once(monkeypatch):
    call, captured = await _fire(monkeypatch, owns_file=True, prior_run=True)

    assert call.kwargs["agent"] == MATE_WORKER_AGENT
    record = read_session_execution("cron:hourly")
    assert mate_worker.is_own_worker_execution(record)
    assert (record.member_id, record.store) == (captured.member_id, captured.store)


@pytest.mark.asyncio
async def test_a_crewmate_cron_on_kiro_with_kas_members_runs_on_the_member_backend(monkeypatch):
    """``acp_backend`` kiro with ``member_acp_backend`` kas (the shipped member default):
    kiro-cli cannot find the worker spec, so the cron key takes the worker's member route."""
    from kiro_crew import members
    from kiro_crew.members_parts import member_sessions

    monkeypatch.setattr(member_sessions, "_CREWMATE_WORKER_SLOTS", set())
    assert KiroCrewConfig().agent.member_acp_backend == "kas"

    await _fire(monkeypatch, owns_file=True)

    assert members.is_crewmate_worker_session("cron:hourly")
    assert members.select_provider_backend("cron:hourly", "kas", "kiro") == "kas"


@pytest.mark.asyncio
async def test_a_non_crewmate_cron_keeps_the_configured_backend(monkeypatch):
    from kiro_crew import members
    from kiro_crew.members_parts import member_sessions

    monkeypatch.setattr(member_sessions, "_CREWMATE_WORKER_SLOTS", set())
    await _fire(monkeypatch, owns_file=False)
    assert not members.is_crewmate_worker_session("cron:hourly")
