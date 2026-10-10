"""Every cron-create path captures a MEMBER selection for a member-named agent.

``--agent`` / the ``agent`` field is the one slot every create surface offers for
naming either a provider template or a crew member. The dashboard UI resolves a
member pick client-side and sends a separate ``member_id``, but the CLI, the MCP
``cron_add`` tool and a scripted dashboard POST send the member NAME in
``agent``. Without resolution the schedule captures ``selection_kind='template'``
with the member name mistaken for a provider template, so the next reply in the
cron chat resolves that name as a template, finds none, and fails to bind the
agent.

Those three owner-driven create paths share one resolver,
``split_cron_agent_member``: when a member name is given in ``agent`` (and no
explicit ``member_id``), it is recorded as a member selection (``member_id`` set,
``agent_id`` cleared). A real provider template name, and the record-level
``add_job(agent_id=...)`` field, both stay templates -- that contract is pinned by
``test_member_memory_runtime.test_v1_provider_template_does_not_become_a_member``.

The App Kit SDK (``ctx.cron.add_job``) is DELIBERATELY NOT promoted: apps are
confined (Spawn SDK gates app agent scope), so an installed app must not reach a
crewmate's private V2 memory by naming it. An app cron behaves exactly as on
main -- the name passes through as ``agent_id`` and reaches only global V1 memory
(``test_sdk_add_job_naming_a_member_stays_a_template``).
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

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

    This is the real-install case the review named: a member like
    ``kirocrew-conductor`` ships both a ``config.agents`` entry (with a durable
    ``member_id``) AND a ``kirocrew-conductor.json`` kiro agent spec. The live
    resolver checks the member alias FIRST, so the schedule must still record a
    member -- the materialized template of the same name must not win. The
    snapshot is seeded warm so the test is deterministic regardless of process.
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

    The three create paths an owner drives (CLI, MCP, dashboard) must behave the
    same, and none may lose the member's memory to a same-named template -- the
    regression the review found. The App Kit SDK path is deliberately excluded:
    an app must not reach a member's private memory by naming it (see
    ``test_sdk_add_job_naming_a_member_stays_a_template``).
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
    return app


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
    """An app creating a cron with a member NAME in ``agent`` does NOT bind that
    member -- it stays a template and reaches only global V1 memory.

    Apps are confined (Spawn SDK gates app agent scope); an installed app must
    not reach a crewmate's private V2 memory just by knowing its name, so
    ``CronSDK._add_job_kwargs`` passes the name through as ``agent_id`` without
    promotion, exactly as on main. Reaching member memory from an app would need
    an explicit manifest grant with app attribution, which is out of scope here.
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
