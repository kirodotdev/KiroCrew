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
from datetime import datetime, timedelta
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
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import session_control as send_routes
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
    routes._BOARD_LOCKS._locks.clear()

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

    # The report is recorded twice: the worker's copy and the board's copy.
    assert [d["action"] for d in recorded] == ["goal", "create", "bind", "report", "report"]
    recorded = recorded[:-1]
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


# -- a session_send-only answer still hands a waiting item back ------------------

ANSWER = "use the staging token and retry"


def _ledger_request(body: dict[str, Any], sk: str, slots: dict[str, _Slot]) -> web.Request:
    return _req("/api/work-ledger/record", body, sk, slots)


async def _board_with_blocked_item(
    tmp_path, monkeypatch, recorded: list[dict[str, Any]], *, reason: str | None = None
):
    """goal + create + bind + a blocked report, through the real routes."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    routes._BOARD_LOCKS._locks.clear()

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")

    def _capture(unit: str, data: dict[str, Any]) -> bool:
        recorded.append(data)
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _capture)
    slots: dict[str, _Slot] = {}

    async def record(body: dict[str, Any]) -> dict[str, Any]:
        resp = await routes.api_work_ledger_record(_ledger_request(body, PIPELINE, slots))
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
            {"status": "blocked", "summary": "no token", **({"reason": reason} if reason else {})},
            WORKER,
            slots,
        )
    )
    assert resp.status == 200, resp.text
    return item_id, slots


async def _session_send(
    monkeypatch,
    slots: dict[str, _Slot],
    *,
    caller: str,
    target: str,
    message: str,
    during_delivery: Any = None,
) -> web.Response:
    """Drive the real send route with the delivery itself stubbed out.

    *during_delivery*, when given, is awaited while the message is "in flight"."""

    async def _deliver(state: Any, **kwargs: Any) -> dict[str, Any]:
        if during_delivery is not None:
            await during_delivery()
        return {"ok": True, "target": kwargs["target"], "started": True, "steered": False}

    monkeypatch.setattr(sc, "send_to_target", _deliver)

    async def _scope(request: Any, operation: str) -> tuple[Any, Any]:
        return None, None

    monkeypatch.setattr(send_routes, "internal_memory_scope", _scope)
    return await send_routes.api_session_control_send(
        _req("/api/session-control/send", {"target": target, "message": message}, caller, slots)
    )


def _stale_now(item: Any) -> datetime:
    """Past the stale window, counted from the item's own newest stamp.

    Read from the stamps the product wrote, never the host clock, so the result
    does not depend on the zone or the moment the test runs."""
    stamp = wl.parse_stamp(item.decided_at or item.last_report_at)
    assert stamp is not None
    return stamp + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS + 5)


@pytest.mark.asyncio
async def test_a_session_send_only_answer_hands_a_waiting_item_back(tmp_path, monkeypatch):
    """The charter's second call, made by the SERVER: a conductor that answers a
    ``blocked`` item with ``session_send`` alone gets the ``decided_at`` stamp a
    manual ``work_ledger_record action=decide`` would have written, and the item's
    silence then ends in the stale flag instead of an unwatched stall."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)

    resp = await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message=ANSWER)
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.decided_at is not None
    assert item.decision == ANSWER
    # The worker owns the next move again, so the window runs from the decision:
    # a worker that never resumes is now reachable by the stale flag.
    assert (
        wl.is_stale(
            item,
            worker_running=False,
            worker_closed=False,
            worker_looping=False,
            now=_stale_now(item),
        )
        is True
    )
    # The stamp is recorded, not just cached: the crew-log entry carries the fields
    # the fold applies, so a rebuild reads the same hand-back the cache does.
    decide = [d for d in recorded if d.get("action") == "decide"]
    assert len(decide) == 1
    assert decide[0]["item_id"] == item_id
    assert decide[0]["decided_at"] == item.decided_at
    assert decide[0]["decision"] == ANSWER
    assert decide[0]["actor"] == "conductor"


@pytest.mark.asyncio
async def test_a_session_send_from_another_session_does_not_hand_back(tmp_path, monkeypatch):
    """Only the item's own conductor's answer hands the item back; a bystander's
    message to the bound worker moves nothing."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)

    resp = await _session_send(monkeypatch, slots, caller=WORKER, target=WORKER, message=ANSWER)
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.decided_at is None
    assert not item.decision
    assert [d for d in recorded if d.get("action") == "decide"] == []
    assert (
        wl.is_stale(
            item,
            worker_running=False,
            worker_closed=False,
            worker_looping=False,
            now=_stale_now(item),
        )
        is False
    )


@pytest.mark.asyncio
async def test_a_session_send_to_an_unbound_session_does_not_stamp(tmp_path, monkeypatch):
    """A send to a session no binding names is not a work dispatch; no item moves."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)

    resp = await _session_send(
        monkeypatch, slots, caller=PIPELINE, target="chat-999-bystander", message=ANSWER
    )
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.decided_at is None
    assert [d for d in recorded if d.get("action") == "decide"] == []


@pytest.mark.asyncio
async def test_a_session_send_to_a_non_waiting_item_does_not_stamp(tmp_path, monkeypatch):
    """The hand-back answers a ``blocked`` / ``question`` report. A ``progress``
    item's next move is already the worker's, so a message about it writes nothing."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)
    resp = await routes.api_work_report(
        _req(
            "/api/work-ledger/report",
            {"status": "progress", "summary": "unblocked, working"},
            WORKER,
            slots,
        )
    )
    assert resp.status == 200, resp.text

    resp = await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message=ANSWER)
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.decided_at is None
    assert not item.decision
    assert [d for d in recorded if d.get("action") == "decide"] == []


async def _report(slots: dict[str, _Slot], body: dict[str, Any]) -> None:
    resp = await routes.api_work_report(_req("/api/work-ledger/report", body, WORKER, slots))
    assert resp.status == 200, resp.text


@pytest.mark.asyncio
async def test_a_session_send_hand_back_redacts_the_decision(tmp_path, monkeypatch):
    """The stamp redacts before it commits, like the record route: the cache and
    its crew-log entry hold the same redacted text, never the raw secret."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)
    secret = "AKIAIOSFODNN7EXAMPLE"

    resp = await _session_send(
        monkeypatch, slots, caller=PIPELINE, target=WORKER, message=f"use key {secret}"
    )
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None and item.decided_at is not None
    assert secret not in (item.decision or "")
    assert "[REDACTED:" in (item.decision or "")
    decide = [d for d in recorded if d.get("action") == "decide"]
    assert len(decide) == 1
    assert decide[0]["decision"] == item.decision


@pytest.mark.asyncio
async def test_a_long_session_send_is_clipped_to_the_decision_cap(tmp_path, monkeypatch):
    """A message over the cap still hands the item back, clipped with a marker."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)
    message = "retry " * wl.MAX_DECISION_CHARS

    resp = await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message=message)
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None and item.decided_at is not None
    assert len(item.decision or "") == wl.MAX_DECISION_CHARS
    assert (item.decision or "").endswith("\u2026")


@pytest.mark.asyncio
async def test_a_second_session_send_does_not_replace_the_first_answer(tmp_path, monkeypatch):
    """One answer per report: a second send to an item already handed back keeps
    the recorded decision and its stamp, so the stale window is not restarted."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)

    await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message=ANSWER)
    first = wl.read_work_item(PIPELINE, item_id)
    assert first is not None and first.decided_at is not None

    await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message="any update?")
    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.decision == ANSWER
    assert item.decided_at == first.decided_at
    assert len([d for d in recorded if d.get("action") == "decide"]) == 1


@pytest.mark.asyncio
async def test_a_new_report_filed_during_delivery_is_not_marked_answered(tmp_path, monkeypatch):
    """The stamp answers the report the conductor saw. A new ``question`` the
    worker files while the message is in flight stays the conductor's move."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)

    async def _new_question() -> None:
        await _report(slots, {"status": "question", "summary": "which branch should I use?"})

    resp = await _session_send(
        monkeypatch,
        slots,
        caller=PIPELINE,
        target=WORKER,
        message=ANSWER,
        during_delivery=_new_question,
    )
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.status == "question"
    assert item.decided_at is None
    assert [d for d in recorded if d.get("action") == "decide"] == []


@pytest.mark.asyncio
async def test_a_new_report_after_a_hand_back_is_answered_again(tmp_path, monkeypatch):
    """Each report gets its own answer: once the worker reports again, the next
    conductor send hands that new report back."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)
    await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message=ANSWER)
    first = wl.read_work_item(PIPELINE, item_id)
    assert first is not None and first.decided_at is not None
    # The second report must land in a later second than the first decision,
    # or ``decided_at`` (seconds precision) would still read as answering it.
    item_file = next((tmp_path / "home").rglob(f"{item_id}.json"))
    data = json.loads(item_file.read_text())
    data["decided_at"] = (wl.parse_stamp(first.decided_at) - timedelta(seconds=5)).isoformat()
    item_file.write_text(json.dumps(data))
    await _report(slots, {"status": "question", "summary": "which branch should I use?"})

    await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message="use main")
    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.decision == "use main"
    assert len([d for d in recorded if d.get("action") == "decide"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["approval", "needs_human"])
async def test_a_session_send_to_an_item_waiting_on_a_person_does_not_stamp(
    tmp_path, monkeypatch, reason
):
    """A ``blocked`` report with a ``reason`` waits on a person. The conductor's
    message ("hold on, I asked the operator") is not the answer, so nothing moves."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded, reason=reason)

    resp = await _session_send(monkeypatch, slots, caller=PIPELINE, target=WORKER, message=ANSWER)
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.reason == reason
    assert item.decided_at is None
    assert [d for d in recorded if d.get("action") == "decide"] == []


@pytest.mark.asyncio
async def test_a_same_text_report_with_new_artifacts_during_delivery_is_not_answered(
    tmp_path, monkeypatch
):
    """A report that repeats the status and summary but changes the artifacts is
    still a new report, even in the same second; the in-flight answer does not
    stamp it."""
    recorded: list[dict[str, Any]] = []
    item_id, slots = await _board_with_blocked_item(tmp_path, monkeypatch, recorded)

    async def _same_text_new_artifacts() -> None:
        await _report(
            slots,
            {"status": "blocked", "summary": "no token", "artifacts": {"log": "/x/run.log"}},
        )

    resp = await _session_send(
        monkeypatch,
        slots,
        caller=PIPELINE,
        target=WORKER,
        message=ANSWER,
        during_delivery=_same_text_new_artifacts,
    )
    assert resp.status == 200, resp.text

    item = wl.read_work_item(PIPELINE, item_id)
    assert item is not None
    assert item.artifacts == {"log": "/x/run.log"}
    assert item.decided_at is None
    assert [d for d in recorded if d.get("action") == "decide"] == []
