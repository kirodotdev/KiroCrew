"""Every cron-create path captures a MEMBER selection for a member-named agent.

The CLI, the MCP ``cron_add`` tool and a scripted dashboard POST send a member NAME
in ``agent``. ``split_cron_agent_member`` records it as a member selection
(``member_id`` set, ``agent_id`` cleared); otherwise the cron chat looks the name up
as a template, finds none, and fails to bind the agent.

The App Kit SDK (``ctx.cron.add_job``) is not promoted: an installed app must not
reach a crewmate's private V2 memory by naming it.

A schedule whose create did not classify the name still carries the template
capture; the gateway's memory worker moves an owner's such schedule, and its
chat's session record, onto the member at start
(``migrate_member_named_template_schedules``).
"""

from __future__ import annotations

import argparse
import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    config_dir,
    resolve_agent_bindings,
)
from kiro_crew.cron import CronService
from kiro_crew.cron_service.identity import split_cron_agent_member
from kiro_crew.dashboard.handlers.cron import api_crons_create
from kiro_crew.execution_context import (
    bind_session_execution,
    execution_from_record,
    read_session_execution,
    resolve_member_execution,
)
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.session_agent_selection import resolve_session_agent_bindings


def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        cron_action="add",
        name="daily",
        message="task",
        every=60,
        cron_expr=None,
        at=None,
        timezone="",
        channel=None,
        agent="",
        script="",
        shell_command="",
        timeout=None,
        timeout_secs=None,
        model="",
        persistent_session=True,
        minimal_context=False,
        hide_in_chat=False,
        silent=False,
        folder="",
        approval_mode="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def member_with_distinct_template(tmp_path, monkeypatch):
    """A crew member whose display name differs from its kiro template.

    Lives under a real config home so the CLI's ``config_dir()``-rooted
    ``CronService`` and ``KiroCrewConfig.load()`` see the same member.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (config_dir() / "crons").mkdir(parents=True)
    cfg = KiroCrewConfig.load()
    cfg.agents["SampleMemberAgent"] = KiroCrewAgentConfig(
        kiro_agent="sample-template-agent", triggers="sample"
    )
    store = provision_member_memory(cfg, "SampleMemberAgent")
    cfg.save()
    return store


@pytest.fixture
def member_also_a_materialized_template(tmp_path, monkeypatch):
    """A crew member whose NAME also has a materialized ``~/.kiro/agents`` spec.

    A member like ``kirocrew-conductor`` ships both a ``config.agents`` entry and a
    ``kirocrew-conductor.json`` spec; the member alias must win. The snapshot is
    seeded warm so the test is deterministic regardless of process.
    """
    import kiro_crew.config.loader as loader

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (config_dir() / "crons").mkdir(parents=True)
    cfg = KiroCrewConfig.load()
    cfg.agents["kirocrew-conductor"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", triggers="conduct"
    )
    provision_member_memory(cfg, "kirocrew-conductor")
    cfg.save()
    # Materialize a kiro agent spec of the SAME name (warm snapshot). Under the
    # pre-fix precedence this would have forced a template; alias-first keeps it
    # a member. Restore the snapshot after the test so it does not leak.
    saved = (loader._MATERIALIZED_AGENTS, loader._MATERIALIZED_AGENTS_READY)
    loader._MATERIALIZED_AGENTS = frozenset({"kirocrew-conductor"})
    loader._MATERIALIZED_AGENTS_READY = True
    try:
        yield KiroCrewConfig.load().agents["kirocrew-conductor"].member_id
    finally:
        loader._MATERIALIZED_AGENTS, loader._MATERIALIZED_AGENTS_READY = saved


def _only_job() -> object:
    return CronService(base_dir=config_dir()).list_jobs(include_disabled=True)[0]


def _capture(job):
    return execution_from_record({"execution_context": job.execution_context})


def test_cli_agent_naming_a_member_captures_a_member_selection(member_with_distinct_template):
    _cron(_ns(agent="SampleMemberAgent"))

    job = _only_job()
    assert job.member_id == "samplememberagent"
    # The record keeps ``agent_id`` a pure template field: the member is carried
    # in ``member_id``, not conflated into ``agent_id``.
    assert job.agent_id == ""
    capture = _capture(job)
    assert capture.selection_kind == "member"
    assert capture.member_id == KiroCrewConfig.load().agents["SampleMemberAgent"].member_id


def test_cli_member_reply_resolves_without_unknown_store(member_with_distinct_template):
    """The reply path on the cron session resolves the member, never raises."""
    _cron(_ns(agent="SampleMemberAgent"))
    job = _only_job()

    # A run publishes the captured execution under the stable cron session key.
    session_key = f"cron:{job.id}"
    bind_session_execution(session_key, _capture(job))

    cfg = KiroCrewConfig.load()
    # This is what the reply path does on every turn of the cron chat; on the
    # bug it raised UnknownMemoryStore resolving the member name as a template.
    bindings = resolve_session_agent_bindings(resolve_agent_bindings, cfg, session_key, None)
    assert bindings.selection_kind == "member"
    assert bindings.kiro_agent == "sample-template-agent"
    assert read_session_execution  # import check


def test_cli_member_capture_matches_the_plain_resolver(member_with_distinct_template):
    """The cron capture agrees with the ordinary member resolution path."""
    _cron(_ns(agent="SampleMemberAgent"))
    job = _only_job()

    cfg = KiroCrewConfig.load()
    plain = resolve_agent_bindings(cfg, "SampleMemberAgent", validate_memory_files=False)
    capture = _capture(job)
    assert capture.selection_kind == plain.selection_kind == "member"
    assert capture.store.store_id == plain.memory_store_name


# ── Shared resolver (used by all three create paths) ─────────────────────────


def test_split_promotes_a_member_name_and_leaves_a_template_alone(member_with_distinct_template):
    cfg = KiroCrewConfig.load()
    cfg.agents["plain-template"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()

    # A member name with no explicit member_id -> promoted to a member selection.
    assert split_cron_agent_member("SampleMemberAgent", "") == ("", "SampleMemberAgent")
    # A name that is NOT a member stays a template.
    assert split_cron_agent_member("plain-template", "") == ("plain-template", "")
    # An explicit member_id is authoritative and untouched.
    assert split_cron_agent_member("some-template", "writer") == ("some-template", "writer")
    # A blank agent is a no-op.
    assert split_cron_agent_member("", "") == ("", "")


def test_split_prefers_the_member_alias_over_a_materialized_template(
    member_also_a_materialized_template,
):
    """Alias-first: a member whose name also has a materialized spec is a member.

    The pre-fix classifier required ``not _materialized_kiro_agent(agent_id)``,
    which reversed the live resolver and recorded such a member as a template.
    """
    assert split_cron_agent_member("kirocrew-conductor", "") == ("", "kirocrew-conductor")


def _create_via_cli() -> None:
    _cron(_ns(agent="kirocrew-conductor"))


def _create_via_mcp() -> None:
    from unittest.mock import patch

    from kiro_crew.mcp_cron import _call_tool_locally

    with patch("kiro_crew.mcp_cron._authz_session_key", return_value="dashboard:tester"):
        result = _call_tool_locally(
            "cron_add",
            {"name": "daily", "message": "task", "every": 60, "agent": "kirocrew-conductor"},
        )
    assert "Error" not in result


async def _create_via_dashboard() -> None:
    svc = CronService(base_dir=config_dir())
    app = _dashboard_app(svc.add_job_async)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "daily", "message": "task", "every": 60, "agent": "kirocrew-conductor"},
        )
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cli", "mcp", "dashboard"])
async def test_member_with_materialized_spec_records_a_member_on_every_path(
    member_also_a_materialized_template, path
):
    """A member whose name also has a materialized agent spec records a member.

    Covers the CLI, MCP and dashboard paths. The App Kit SDK is excluded on purpose
    (``test_sdk_add_job_naming_a_member_stays_a_template``).
    """
    if path == "cli":
        _create_via_cli()
    elif path == "mcp":
        _create_via_mcp()
    else:
        await _create_via_dashboard()

    job = _only_job()
    assert job.member_id == "kirocrew-conductor"
    assert job.agent_id == ""
    capture = _capture(job)
    assert capture.selection_kind == "member"
    assert capture.member_id == member_also_a_materialized_template


# ── MCP cron_add tool path ───────────────────────────────────────────────────


def test_mcp_cron_add_naming_a_member_captures_a_member_selection(
    member_with_distinct_template,
):
    from unittest.mock import patch

    from kiro_crew.mcp_cron import _call_tool_locally

    # The tool refuses an unidentified caller; a dashboard/agent caller has a
    # verified session key, so supply one for the test.
    with patch("kiro_crew.mcp_cron._authz_session_key", return_value="dashboard:tester"):
        result = _call_tool_locally(
            "cron_add",
            {"name": "daily", "message": "task", "every": 60, "agent": "SampleMemberAgent"},
        )
    assert "Error" not in result

    job = _only_job()
    assert job.member_id == "samplememberagent"
    assert job.agent_id == ""
    capture = _capture(job)
    assert capture.selection_kind == "member"
    assert capture.member_id == KiroCrewConfig.load().agents["SampleMemberAgent"].member_id


# ── Dashboard POST /api/crons path ───────────────────────────────────────────


def _dashboard_app(add_job_async) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(add_job_async=add_job_async),
        push_refresh=MagicMock(),
    )
    app.router.add_route("*", "/api/crons", api_crons_create)
    return as_owner(app)


@pytest.mark.asyncio
async def test_dashboard_create_naming_a_member_captures_a_member_selection(
    member_with_distinct_template,
):
    """A scripted POST sending a member NAME in ``agent`` records a member run."""
    svc = CronService(base_dir=config_dir())
    app = _dashboard_app(svc.add_job_async)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/crons",
            json={"name": "daily", "message": "task", "every": 60, "agent": "SampleMemberAgent"},
        )
    assert resp.status == 200

    job = _only_job()
    # The handler resolved the member name before the store call: member in,
    # agent_id cleared.
    assert job.member_id == "samplememberagent"
    assert job.agent_id == ""
    capture = _capture(job)
    assert capture.selection_kind == "member"
    assert capture.member_id == KiroCrewConfig.load().agents["SampleMemberAgent"].member_id


# ── App Kit SDK (ctx.cron.add_job) path ──────────────────────────────────────


def test_sdk_add_job_naming_a_member_stays_a_template(
    member_with_distinct_template,
):
    """An app naming a member in ``agent`` gets a template and global V1 memory only.

    Apps are confined, so ``CronSDK._add_job_kwargs`` passes the name through as
    ``agent_id``: knowing a crewmate's name must not reach its private V2 memory.
    """
    from kiro_crew.apps.cron_sdk import CronSDK

    sdk = CronSDK("testapp", CronService(base_dir=config_dir()))
    sdk.add_job("daily", "task", every_secs=60, agent="SampleMemberAgent")

    job = _only_job()
    assert job.member_id == ""
    assert job.agent_id == "SampleMemberAgent"
    capture = _capture(job)
    assert capture.selection_kind == "template"
    assert not capture.member_id
    # The isolation guarantee: the turn reaches the global V1 store, not the
    # member's private V2 store, and carries no app attribution leak.
    assert capture.store.store_id == "default"


# ── A schedule whose create captured the member name as a template ───────────


def _template_capture(
    agent: str = "SampleMemberAgent", *, created_by: str = "", store: str = ""
) -> object:
    """A schedule as a create that did not classify *agent* wrote it, fired once.

    ``CronService.add_job`` records ``agent_id`` as a template (classifying is the
    create callers' step), which is the capture such a create made. A *store*
    puts that capture on another store. Its fire published the capture under the
    stable ``cron:<id>`` key, so the session record agrees with the schedule.
    """
    job = CronService(base_dir=config_dir()).add_job(
        "daily", "task", every_secs=60, agent_id=agent, created_by=created_by
    )
    if store:
        path = config_dir() / "crons.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][0]["execution_context"]["store"]["store_id"] = store
        data["jobs"][0]["memory_store"] = store
        path.write_text(json.dumps(data), encoding="utf-8")
        job = _only_job()
    bind_session_execution(f"cron:{job.id}", _capture(job))
    return job


def _crons_bytes() -> bytes:
    return (config_dir() / "crons.json").read_bytes()


def test_start_moves_a_member_named_template_schedule_onto_the_member(
    member_with_distinct_template,
):
    """The schedule and its chat run as the member on its own store, as a create
    naming the member makes them today."""
    from kiro_crew.cron_service.identity import migrate_member_named_template_schedules

    legacy = _template_capture()
    assert _capture(legacy).selection_kind == "template"
    assert _capture(legacy).store.store_id == "default"

    assert migrate_member_named_template_schedules(config_dir()) == [legacy.id]

    cfg = KiroCrewConfig.load()
    member = resolve_member_execution(cfg, "SampleMemberAgent")
    job = _only_job()
    assert (job.agent_id, job.member_id) == ("", "samplememberagent")
    assert job.memory_store == member_with_distinct_template
    assert _capture(job) == member
    session_key = f"cron:{job.id}"
    assert read_session_execution(session_key) == member
    # What every reply in the job's chat binds.
    bindings = resolve_session_agent_bindings(resolve_agent_bindings, cfg, session_key, None)
    assert bindings.selection_kind == "member"
    assert bindings.memory_store_name == member_with_distinct_template
    # The next fire's plain publication agrees with the moved record.
    bind_session_execution(session_key, _capture(job))

    before = _crons_bytes()
    assert migrate_member_named_template_schedules(config_dir()) == []
    assert _crons_bytes() == before


def test_a_session_already_moved_does_not_hold_the_schedule_back(member_with_distinct_template):
    """A start whose schedule write failed after the session moved: the next one
    finishes the move."""
    from kiro_crew.cron_service.identity import migrate_member_named_template_schedules

    legacy = _template_capture()
    member = resolve_member_execution(KiroCrewConfig.load(), "SampleMemberAgent")
    bind_session_execution(f"cron:{legacy.id}", member, replace_existing=True)

    assert migrate_member_named_template_schedules(config_dir()) == [legacy.id]
    assert _capture(_only_job()) == member


def test_a_schedule_whose_chat_runs_another_template_stays_as_it_is(
    member_with_distinct_template, caplog
):
    """Dispatch would refuse a capture its session record disagrees with, so the
    schedule is left, and said, rather than moved under that record."""
    from kiro_crew.cron_service.identity import migrate_member_named_template_schedules

    legacy = _template_capture()
    session_key = f"cron:{legacy.id}"
    picked = _capture(legacy).with_template("other-template", "other-template")
    bind_session_execution(session_key, picked, replace_existing=True)
    before = _crons_bytes()

    assert migrate_member_named_template_schedules(config_dir()) == []
    assert _crons_bytes() == before
    assert read_session_execution(session_key) == picked
    assert "recreate the schedule" in caplog.text


@pytest.mark.parametrize(
    ("agent", "created_by", "store"),
    [
        # The member's id, not its alias: a member target would resolve it, but a
        # create classifies only an alias as a member, so it names a template.
        ("samplememberagent", "", ""),
        ("SampleMemberAgent", "app:testapp", ""),
        ("SampleMemberAgent", "", "team-notes"),
    ],
    ids=["provider-template", "app-owned", "named-store"],
)
def test_start_leaves_every_other_template_schedule_alone(
    member_with_distinct_template, agent, created_by, store
):
    """A provider template name, an app's schedule (an app may not reach a member's
    store by naming it) and a capture on a store other than Global keep their record,
    and so does their chat's session record, which agrees with it."""
    from kiro_crew.cron_service.identity import migrate_member_named_template_schedules

    job = _template_capture(agent, created_by=created_by, store=store)
    session_key = f"cron:{job.id}"
    before = _crons_bytes()

    assert migrate_member_named_template_schedules(config_dir()) == []
    assert _crons_bytes() == before
    assert read_session_execution(session_key) == _capture(job)


def test_the_gateway_memory_worker_moves_them_before_the_scheduler_arms():
    """The scheduler waits on this worker, so no fire publishes the template capture
    again once the gateway has started."""
    from kiro_crew.slack import gateway as slack_gateway

    source = inspect.getsource(slack_gateway.GatewayOrchestrator._initialize_memory_worker)
    assert (
        source.index("migrate_legacy_member_schedules(data_home())")
        < source.index("migrate_member_named_template_schedules(data_home())")
        < source.index("apply_pending_member_restores(")
    )
