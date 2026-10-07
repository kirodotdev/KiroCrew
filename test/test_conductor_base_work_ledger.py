"""Every conductor spec gets the work ledger from the shared conductor base.

The dashboard's ``workstreams`` fold mints a board only from ``work/recorded``
entries, so a conductor that never records runs a fleet its Dashboard tab cannot
show. The base (``conductor_agents._conductor_mcp_servers``,
``_conductor_shipped`` and ``_conductor_prompt``) is what every conductor
installer builds from, so these tests pin the property on every spec the
materializer writes, and then drive the real record and report routes from a
pipeline-style conductor into the fold the Dashboard tab reads.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import agent
from kiro_crew import work_ledger as wl
from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
)
from kiro_crew.agent_materialization import conductor_agents
from kiro_crew.crew_log import projection
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.crew_log.schema import Entry
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

#: Every conductor spec the materializer writes: installer, file, and the charter
#: its prompt opens with.
_CONDUCTORS = [
    pytest.param(
        "_install_conductor_agent",
        CONDUCTOR_AGENT_FILENAME,
        "_CONDUCTOR_SYSTEM_PROMPT",
        id="conductor",
    ),
    pytest.param(
        "_install_ledger_conductor_agent",
        LEDGER_CONDUCTOR_AGENT_FILENAME,
        "_CONDUCTOR_SYSTEM_PROMPT",
        id="ledger-conductor-alias",
    ),
    pytest.param(
        "_install_pipeline_conductor_agent",
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        "_PIPELINE_CONDUCTOR_SYSTEM_PROMPT",
        id="pipeline",
    ),
    pytest.param(
        "_install_security_conductor_agent",
        SECURITY_CONDUCTOR_AGENT_FILENAME,
        "_SECURITY_CONDUCTOR_SYSTEM_PROMPT",
        id="security",
    ),
]


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installer: str, filename: str):
    """Run one conductor installer against a minimal template and read its spec."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
            },
            "tools": ["@kirocrew-core"],
            "allowedTools": [],
        },
    )
    monkeypatch.setattr(
        agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
    )
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
    assert getattr(agent, installer)() is True
    return json.loads((tmp_path / filename).read_text(encoding="utf-8"))


# -- the base, on every spec ----------------------------------------------------


@pytest.mark.parametrize(("installer", "filename", "charter"), _CONDUCTORS)
def test_every_conductor_spec_mounts_the_work_server(
    tmp_path, monkeypatch, installer, filename, charter
):
    data = _install(tmp_path, monkeypatch, installer, filename)
    assert data["mcpServers"]["kirocrew-work"]["args"] == ["mcp-work"]
    assert "@kirocrew-work" in data["tools"]


@pytest.mark.parametrize(("installer", "filename", "charter"), _CONDUCTORS)
def test_every_conductor_spec_auto_approves_the_base_work_verbs(
    tmp_path, monkeypatch, installer, filename, charter
):
    """The conductor verbs, on ``allowedTools`` and on the derived KAS rule, so an
    unattended patrol cycle can record without an approval prompt. The worker's
    ``work_report`` is on none of them: it writes into a parent's record."""
    data = _install(tmp_path, monkeypatch, installer, filename)
    allowed = data["allowedTools"]
    for verb in agent._CONDUCTOR_BASE_WORK_GRANTS:
        assert verb in allowed, verb
    assert "@kirocrew-work/work_report" not in allowed
    match = data["permissions"]["rules"][0]["match"]
    assert "kirocrew-work/work_ledger_record" in match


@pytest.mark.parametrize(("installer", "filename", "charter"), _CONDUCTORS)
def test_every_conductor_prompt_states_the_ledger_flow_once(
    tmp_path, monkeypatch, installer, filename, charter
):
    """The pipeline and security charters get the shared protocol block appended;
    the goal conductor's charter already states the flow, so it is left as is and
    carries no second copy."""
    data = _install(tmp_path, monkeypatch, installer, filename)
    prompt = data["prompt"]
    if charter == "_CONDUCTOR_SYSTEM_PROMPT":
        assert prompt == agent._CONDUCTOR_SYSTEM_PROMPT
        assert agent._CONDUCTOR_WORK_LEDGER_PROTOCOL not in prompt
        assert "`work_ledger_record` `action=create`" in prompt
    else:
        assert prompt == getattr(agent, charter) + agent._CONDUCTOR_WORK_LEDGER_PROTOCOL
        assert prompt.count(agent._CONDUCTOR_WORK_LEDGER_PROTOCOL) == 1


def test_the_protocol_names_the_four_steps() -> None:
    """Create with an acceptance, bind before seeding, the worker reports, the
    conductor gives the verdict and closes."""
    text = agent._CONDUCTOR_WORK_LEDGER_PROTOCOL
    for token in (
        "`action=create`",
        "`acceptance`",
        "`action=bind`",
        "before you seed it",
        "`work_report`",
        "`action=verdict`",
        "`action=close`",
    ):
        assert token in text, token


def test_the_mount_is_unconditional() -> None:
    """No caller can build a conductor spec without the work server."""
    assert list(inspect.signature(conductor_agents._conductor_mcp_servers).parameters) == ["config"]
    assert "kirocrew-work" in conductor_agents._conductor_mcp_servers({})


# -- the real path: record and report reach the Dashboard tab's fold ------------

PIPELINE = "chat-41-pipeline-conductor"
WORKER = "chat-42-worker"


class _Slot:
    def __init__(self, created_by: str = "") -> None:
        self._created_by = created_by
        self.workspace = "default"
        self.running = False


def _req(path: str, body: dict[str, Any], sk: str, slots: dict[str, _Slot]) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: slots.get(key))
    app["state"] = state
    req = make_mocked_request("POST", path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


@pytest.mark.asyncio
async def test_a_pipeline_conductor_item_lands_on_a_workstreams_board(tmp_path, monkeypatch):
    """create + bind from the pipeline conductor's session and a report from its
    worker, through the real routes, fold into a board the Dashboard tab reads."""
    spec = _install(
        tmp_path / "agents",
        monkeypatch,
        "_install_pipeline_conductor_agent",
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
    )
    assert "@kirocrew-work/work_ledger_record" in spec["allowedTools"]

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    routes._BOARD_LOCKS.clear()

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    recorded: list[dict[str, Any]] = []

    def _capture(unit: str, data: dict[str, Any]) -> bool:
        recorded.append(data)
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _capture)
    slots: dict[str, _Slot] = {}

    async def record(body: dict[str, Any]) -> dict[str, Any]:
        resp = await routes.api_work_ledger_record(
            _req("/api/work-ledger/record", body, PIPELINE, slots)
        )
        assert resp.status == 200, resp.text
        return json.loads(resp.text or "")

    await record({"action": "goal", "goal": "drain the pipeline queue", "round": 1})
    created = await record(
        {
            "action": "create",
            "title": "fix the flaky test",
            "acceptance": {"kind": "file_exists", "path": "/x/REPORT.md"},
        }
    )
    item_id = created["item"]["item_id"]
    slots[WORKER] = _Slot(created_by=PIPELINE)
    await record({"action": "bind", "item_id": item_id, "worker_session_key": WORKER})
    resp = await routes.api_work_report(
        _req(
            "/api/work-ledger/report",
            {"status": "done", "summary": "fixed", "pr": 7},
            WORKER,
            slots,
        )
    )
    assert resp.status == 200, resp.text

    assert [d["action"] for d in recorded] == ["goal", "create", "bind", "report"]
    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None and item.status == "done"

    entries = [
        Entry(type="session/opened", seq=1, time=1_000, src="test", data={"slot": PIPELINE})
    ] + [
        Entry(type=WORK_ENTRY_TYPE, seq=i + 2, time=2_000 + i, src="test", data=data)
        for i, data in enumerate(recorded)
    ]
    state = projection.initial("workstreams")
    bind = projection._FOLDS["workstreams"].bind_slot
    assert bind is not None
    bind(state.state, PIPELINE)
    value = projection.projection_of(projection.advance(state, entries)).value

    boards = [row for row in value["items"] if row["goal"] == "drain the pipeline queue"]
    assert len(boards) == 1, value["items"]
    tasks = boards[0]["tasks"]
    assert [t["title"] for t in tasks] == ["fix the flaky test"]
    assert tasks[0]["status"] == "done"
