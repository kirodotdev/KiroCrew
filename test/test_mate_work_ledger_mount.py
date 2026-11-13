# SPDX-License-Identifier: Apache-2.0
"""A crewmate's conductor and worker both mount the work ledger.

``build_agent_config`` skips every ``opt_in`` server, so the two mate service
specs derived from it carried no ``kirocrew-work`` and the crewmate prompt's
``work_ledger_*`` references were dead. Both installers now mount it through
``managed_mcp._managed_opt_in_entry``: the conductor with the ledger
conductor's grants, the worker with the worker's. These tests pin the spec on
disk, the merge with a crewmate's own file, the governance ceiling, and the
real path a crewmate's thread walks: goal, create, ``session_create`` (forced
onto ``kirocrew-mate-worker``), bind, and the worker's ``work_report``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

# The crewmate fixture the hand-off tests drive the real ``create_session`` with.
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401

from kiro_crew import agent, mate_worker
from kiro_crew import work_ledger as wl
from kiro_crew.agent_files import MATE_CONDUCTOR_AGENT_FILENAME, MATE_WORKER_AGENT_FILENAME
from kiro_crew.agent_materialization import auto_approve, managed_mcp, service_agents
from kiro_crew.config import paths as paths_mod
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.mate_worker import MATE_WORKER_AGENT

_SPECS = {
    "conductor": (
        service_agents._install_mate_conductor_agent,
        MATE_CONDUCTOR_AGENT_FILENAME,
        agent._LEDGER_CONDUCTOR_WORK_GRANTS,
    ),
    "worker": (
        service_agents._install_mate_worker_agent,
        MATE_WORKER_AGENT_FILENAME,
        agent._WORKER_WORK_GRANTS,
    ),
}


def _install(which: str, tmp_path, monkeypatch) -> dict:
    installer, filename, _grants = _SPECS[which]
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    installer()
    return json.loads((paths_mod.mate_agents_dir() / filename).read_text(encoding="utf-8"))


def _served(tool: str) -> bool:
    return tool in {t["name"] for t in importlib.import_module("kiro_crew.mcp_work")._list_tools()}


def _reachable(spec: dict, tool: str) -> bool:
    """Configured, launching ``mcp-work``, serving *tool*, and declared in ``tools``."""
    entry = (spec.get("mcpServers") or {}).get("kirocrew-work")
    if not isinstance(entry, dict) or not entry.get("command"):
        return False
    if "mcp-work" not in (entry.get("args") or []):
        return False
    return _served(tool) and "@kirocrew-work" in (spec.get("tools") or [])


# ── the installed specs ──


@pytest.mark.parametrize("which", sorted(_SPECS))
def test_the_entry_is_the_managed_opt_in_entry(which, tmp_path, monkeypatch):
    spec = _install(which, tmp_path, monkeypatch)
    assert spec["mcpServers"]["kirocrew-work"] == managed_mcp._managed_opt_in_entry("mcp-work")
    assert "@kirocrew-work" in spec["tools"]


@pytest.mark.parametrize("which", sorted(_SPECS))
def test_the_entry_carries_the_registry_type_and_home_pin(which, tmp_path, monkeypatch):
    """The two fields that fail silently, forced on so their presence is observable.
    They match the ``kirocrew-conductor`` spec's entry built under the same modes."""
    from kiro_crew.agent_materialization import conductor_agents

    monkeypatch.setattr(managed_mcp, "_mcp_registry_mode", lambda: True)
    monkeypatch.setattr(managed_mcp, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/pinned"})
    spec = _install(which, tmp_path, monkeypatch)
    entry = spec["mcpServers"]["kirocrew-work"]
    assert entry["type"] == managed_mcp._MCP_REGISTRY_TYPE
    assert entry["env"]["KIROCREW_HOME"] == "/pinned"
    assert entry == conductor_agents._conductor_mcp_servers({})["kirocrew-work"]


def test_the_conductor_gets_the_ledger_conductors_grants(tmp_path, monkeypatch):
    spec = _install("conductor", tmp_path, monkeypatch)
    added = {r for r in spec["allowedTools"] if r.startswith("@kirocrew-work/")}
    assert added == set(agent._LEDGER_CONDUCTOR_WORK_GRANTS)
    for tool in ("work_ledger_read", "work_ledger_record"):
        assert f"@kirocrew-work/{tool}" in spec["allowedTools"]
        assert _reachable(spec, tool)


def test_the_worker_gets_the_worker_grants_only(tmp_path, monkeypatch):
    spec = _install("worker", tmp_path, monkeypatch)
    added = {r for r in spec["allowedTools"] if r.startswith("@kirocrew-work/")}
    assert added == set(agent._WORKER_WORK_GRANTS)
    for tool in ("work_brief", "work_report"):
        assert _reachable(spec, tool)
    assert "@kirocrew-work/work_ledger_record" not in spec["allowedTools"]


def test_the_probe_sees_a_dead_grant(tmp_path, monkeypatch):
    """Positive control: a spec without the entry is unreachable."""
    spec = _install("worker", tmp_path, monkeypatch)
    spec["mcpServers"].pop("kirocrew-work")
    assert not _reachable(spec, "work_report")


@pytest.mark.parametrize(
    ("which", "withheld", "kept"),
    [
        ("conductor", "@kirocrew-work/work_ledger_record", "@kirocrew-work/work_ledger_read"),
        ("worker", "@kirocrew-work/work_report", "@kirocrew-work/work_brief"),
    ],
)
def test_a_ceiling_that_withholds_a_verb_leaves_it_mounted_not_granted(
    which, withheld, kept, tmp_path, monkeypatch
):
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: ref != withheld)
    spec = _install(which, tmp_path, monkeypatch)
    assert withheld not in spec["allowedTools"]
    assert kept in spec["allowedTools"]
    assert _reachable(spec, withheld.rsplit("/", 1)[1])


@pytest.mark.parametrize(
    ("which", "verb"),
    [("conductor", "work_ledger_record"), ("worker", "work_report")],
)
def test_with_the_ceiling_off_every_verb_is_granted(which, verb, tmp_path, monkeypatch):
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: True)
    spec = _install(which, tmp_path, monkeypatch)
    assert f"@kirocrew-work/{verb}" in spec["allowedTools"]


@pytest.mark.parametrize("which", sorted(_SPECS))
@pytest.mark.parametrize("muted", [True, False])
def test_an_inherited_switch_off_survives_the_mount(which, muted, tmp_path, monkeypatch):
    """An operator's ``disabled`` on ``kirocrew-work`` in the default template is
    kept on the managed entry, so the KAS projection still withholds it."""
    from kiro_crew.acp.kas_agents import _project_mcp_servers

    real = agent.build_agent_config

    def with_switch():
        config = real()
        config.setdefault("mcpServers", {})["kirocrew-work"] = {"disabled": muted}
        return config

    monkeypatch.setattr(agent, "build_agent_config", with_switch)
    spec = _install(which, tmp_path, monkeypatch)
    entry = spec["mcpServers"]["kirocrew-work"]
    assert "mcp-work" in entry["args"]
    assert entry.get("disabled", False) is muted
    projected = _project_mcp_servers(spec, spec["name"], frozenset())
    assert ("kirocrew-work" in projected) is (not muted)


@pytest.mark.parametrize(
    ("which", "limited", "kept"),
    [
        ("conductor", "work_ledger_record", "work_ledger_read"),
        ("worker", "work_report", "work_brief"),
    ],
)
def test_an_inherited_per_tool_limit_survives_the_mount(
    which, limited, kept, tmp_path, monkeypatch
):
    """An operator's ``disabledTools`` on ``kirocrew-work`` stays on the managed
    entry and its verb is not granted. On the worker, the merge then excludes it."""
    real = agent.build_agent_config

    def with_limit():
        config = real()
        config.setdefault("mcpServers", {})["kirocrew-work"] = {"disabledTools": [limited]}
        return config

    monkeypatch.setattr(agent, "build_agent_config", with_limit)
    spec = _install(which, tmp_path, monkeypatch)
    assert spec["mcpServers"]["kirocrew-work"]["disabledTools"] == [limited]
    assert "mcp-work" in spec["mcpServers"]["kirocrew-work"]["args"]
    assert f"@kirocrew-work/{limited}" not in spec["allowedTools"]
    assert f"@kirocrew-work/{kept}" in spec["allowedTools"]
    if which == "worker":
        monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
        merged = mate_worker.resolve_worker_spec({**spec, "prompt": ""}, None)
        assert f"@kirocrew-work/{limited}" in merged["excludedTools"]
        assert f"@kirocrew-work/{limited}" not in merged["allowedTools"]


# ── on KAS: the projection a session's spec goes through ──


@pytest.mark.parametrize("which", sorted(_SPECS))
def test_kas_declares_the_mount_when_no_stub_serves_it(which, tmp_path, monkeypatch):
    """The unstubbed path: no gateway overlay holds stubs for the mate service specs,
    so ``injection_server_names`` answers empty and the spec's own entry is the one
    KAS launches. Crew's managed entry keeps its ``KIROCREW_HOME`` pin there."""
    from kiro_crew.acp.kas_agents import _project_mcp_servers

    monkeypatch.setattr(managed_mcp, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/pinned"})
    spec = _install(which, tmp_path, monkeypatch)
    projected = _project_mcp_servers(spec, spec["name"], frozenset())
    entry = projected["kirocrew-work"]
    assert "mcp-work" in entry["args"]
    assert entry["env"]["KIROCREW_HOME"] == "/pinned"


@pytest.mark.parametrize("which", sorted(_SPECS))
def test_kas_leaves_the_mount_to_a_stub_that_serves_it(which, tmp_path, monkeypatch):
    """When a gateway stub is injected for it, the spec copy is withheld so the
    server is not registered twice; ``mcp-work`` is a managed stub subcommand."""
    from kiro_crew.acp.kas_agents import _project_mcp_servers
    from kiro_crew.mcp_gateway import stub_register

    assert "mcp-work" in stub_register._KIROCREW_MCP_SUBCOMMANDS
    spec = _install(which, tmp_path, monkeypatch)
    projected = _project_mcp_servers(spec, spec["name"], frozenset({"kirocrew-work"}))
    assert "kirocrew-work" not in projected
    assert "kirocrew-core" in projected


@pytest.mark.parametrize(
    ("which", "ledger_verbs"),
    [
        ("conductor", ("work_ledger_read", "work_ledger_record")),
        ("worker", ("work_brief", "work_report")),
    ],
)
def test_the_live_member_projection_keeps_the_ledger_beside_the_dashboard(
    which, ledger_verbs, tmp_path, monkeypatch
):
    """A member session's KAS agent is the spec plus the member projection, which
    ADDS ``@kirocrew-dashboard`` and its grants. It appends to the spec's tool and
    grant lists rather than rebuilding them, so the spec's ``kirocrew-work`` mount
    and grants reach the session too."""
    from kiro_crew.acp.kas_agents import to_client_custom_agent

    spec = _install(which, tmp_path, monkeypatch)
    out = to_client_custom_agent(spec["name"], spec, "p", member_dispatch=True)
    assert "@kirocrew-work" in out["tools"]
    assert "@kirocrew-dashboard" in out["tools"]
    assert "mcp-work" in out["mcpServers"]["kirocrew-work"]["args"]
    rendered = json.dumps(out.get("permissions") or {})
    for verb in ledger_verbs:
        assert verb in rendered, verb
    assert "session_read_message" in rendered
    # The negative: without the member flag the dashboard is not added, the ledger is.
    plain = to_client_custom_agent(spec["name"], spec, "p")
    assert "@kirocrew-work" in plain["tools"]
    assert "@kirocrew-dashboard" not in plain["tools"]


# ── the worker merge with a crewmate's own file ──


def _worker_base(tmp_path, monkeypatch) -> dict:
    base = _install("worker", tmp_path, monkeypatch)
    return {**base, "prompt": ""}


def test_no_crewmate_file_keeps_the_mount(tmp_path, monkeypatch):
    base = _worker_base(tmp_path, monkeypatch)
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    spec = mate_worker.resolve_worker_spec(base, None)
    assert _reachable(spec, "work_report")
    assert "@kirocrew-work/work_report" in spec["allowedTools"]


def test_a_crewmate_file_cannot_replace_the_mount(tmp_path, monkeypatch):
    """A file that declares its own ``kirocrew-work`` entry, or none, adds nothing
    on that name: the base entry wins (``merge_worker_spec``)."""
    base = _worker_base(tmp_path, monkeypatch)
    mine = {
        "name": "pixel",
        "tools": ["@jira", "@kirocrew-work"],
        "mcpServers": {
            "jira": {"command": "j"},
            "kirocrew-work": {"command": "/tmp/evil", "args": []},
        },
    }
    additions = mate_worker.worker_additions(base, mine)
    assert "kirocrew-work" not in additions["mcpServers"]
    spec = mate_worker.merge_worker_spec(base, additions)
    assert spec["mcpServers"]["kirocrew-work"] == base["mcpServers"]["kirocrew-work"]
    assert "jira" in spec["mcpServers"]
    assert _reachable(spec, "work_report")
    assert "@kirocrew-work/work_brief" in spec["allowedTools"]


def test_a_crewmate_file_on_disk_keeps_the_mount_end_to_end(tmp_path, monkeypatch):
    """Through ``resolve_worker_spec`` with a real file in the crewmate folder."""
    base = _worker_base(tmp_path, monkeypatch)
    mates = paths_mod.mate_agents_dir()
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "tools": ["@jira"], "mcpServers": {"jira": {"command": "j"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    spec = mate_worker.resolve_worker_spec(base, "pixel")
    assert "jira" in spec["mcpServers"]
    assert _reachable(spec, "work_report")


# ── the route: a crewmate's thread records, opens its worker, binds, and is reported to ──


def _req(path: str, body: dict[str, Any], sk: str, state: Any) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def test_a_crewmates_worker_report_lands_on_its_threads_item(crewmate, monkeypatch):  # noqa: F811
    state, caller, _execution = crewmate
    conductor = slot_history_key(caller)

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    routes._BOARD_LOCKS._locks.clear()
    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)

    async def record(body: dict[str, Any]) -> dict[str, Any]:
        resp = await routes.api_work_ledger_record(
            _req("/api/work-ledger/record", body, conductor, state)
        )
        assert resp.status == 200, resp.text
        return json.loads(resp.text or "")

    async def goal_and_create() -> str:
        await record({"action": "goal", "goal": "triage the inbox", "round": 1})
        created = await record(
            {
                "action": "create",
                "title": "sort the inbox",
                "acceptance": {"kind": "human_approval"},
            }
        )
        return created["item"]["item_id"]

    item_id = asyncio.run(goal_and_create())
    # The thread's hand-off: the gateway forces it onto the crewmate's worker.
    worker = state.get_slot(_create(state, caller)["target"])
    assert worker.agent == MATE_WORKER_AGENT
    worker_key = slot_history_key(worker)

    async def bind_and_report() -> web.Response:
        bound = await record(
            {"action": "bind", "item_id": item_id, "worker_session_key": worker_key}
        )
        assert bound["item"]["item_id"] == item_id
        return await routes.api_work_report(
            _req(
                "/api/work-ledger/report",
                {"status": "done", "summary": "sorted", "artifacts": {"path": "/x/INBOX.md"}},
                worker_key,
                state,
            )
        )

    resp = asyncio.run(bind_and_report())
    assert resp.status == 200, resp.text
    # The ledger keys on the slot key, not the history key's ``dashboard:`` form.
    conductor_slot = caller.key
    assert conductor_slot == "member-pixel"
    assert wl.read_binding(worker.key) == (conductor_slot, item_id)
    item = wl.read_work_item(conductor_slot, item_id)
    assert item is not None and item.status == "done"
