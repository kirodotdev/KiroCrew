"""A code channel's conductor evaluates its done items through the gateway.

Its ledger read is the published-fields projection (a derived ``state``, no
``status`` and no bars), so ``accept_eval`` over that read would call open work
"already done". The gateway evaluates the stored bars and answers verdicts alone.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

from test_slack_code_channel_dispatch_exemption import (  # noqa: F401 - fixtures among them
    _channel,
    _create,
    _enabled,
    _key,
    _owner_turn,
    _read,
    _record,
    _report,
    _req,
    _state,
    ledger_routes_open,
)

from kiro_crew import mcp_core, mcp_work
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import work_ledger as routes


def _eval(state, sk: str, query: str = "") -> tuple[int, dict[str, Any]]:
    path = "/api/work-ledger/accept-eval" + (f"?{query}" if query else "")
    resp = asyncio.run(routes.api_work_ledger_accept_eval(_req(state, "GET", path, sk)))
    return resp.status, json.loads(resp.text)


def _done_item(state, tmp_path, acceptance: dict[str, Any] | None = None) -> tuple[str, str]:
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    sk = _key(slot)
    assert _record(state, sk, {"action": "goal", "goal": "Fix it", "round": 1})[0] == 200
    status, body = _record(
        state,
        sk,
        {
            "action": "create",
            "title": "Fix it",
            "acceptance": acceptance or {"kind": "pr_checks", "pr": 41, "repo": "owner/name"},
        },
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    worker = state.get_slot(_create(state, slot)["target"])
    bind = {"action": "bind", "item_id": item_id, "worker_session_key": worker.key}
    assert _record(state, sk, bind)[0] == 200
    report = {"status": "done", "summary": "pushed", "pr": 41}
    assert _report(state, slot_history_key(worker), report)[0] == 200
    return sk, item_id


def test_a_done_item_gets_a_real_verdict_without_its_bar(
    tmp_path, monkeypatch, ledger_routes_open  # noqa: F811
):
    state = _state(tmp_path, monkeypatch)
    sk, item_id = _done_item(state, tmp_path)
    argvs: list[list[str]] = []

    def _green(argv, **kwargs):
        argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "build  pass  https://ci/42", "")

    monkeypatch.setattr(subprocess, "run", _green)
    # The tool's own path: the read is the projection, so it asks the gateway.
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: sk)
    waits: list[float] = []

    def _get(path: str, **kw: Any) -> dict[str, Any]:
        if path.startswith("/api/work-ledger/accept-eval"):
            waits.append(kw.get("timeout", 10))
        return _route(state, sk, path)

    monkeypatch.setattr(mcp_work, "_get", _get)
    for args in ({}, {"item_id": item_id}):
        answer = json.loads(mcp_work._call_tool_inner("accept_eval", args))
        assert answer["results"] == [{"id": item_id, "verdict": "pass"}]
        assert answer["skipped"] == [] and answer["published_fields_only"] is True
    assert argvs[-1] == ["gh", "pr", "checks", "41", "--repo", "owner/name"]
    # GPT: the gateway runs the checks behind the GET, so it outwaits them.
    assert waits and all(wait > 600 for wait in waits)
    # Nothing of the bar or the check's own output comes back.
    status, body = _eval(state, sk)
    assert status == 200
    for private in ("owner/name", "pr_checks", "https://ci/42", "pushed"):
        assert private not in json.dumps(body), private


def _route(state, sk: str, path: str) -> dict[str, Any]:
    if path.startswith("/api/work-ledger/accept-eval"):
        return _eval(state, sk, path.partition("?")[2])[1]
    return _read(state, sk)[1]


def test_a_bar_that_needs_a_working_directory_is_skipped_not_judged(
    tmp_path, monkeypatch, ledger_routes_open  # noqa: F811
):
    """GPT note: the gateway's directory is not the channel's worktree, so a PR with
    no repo is not checked against whatever repository the gateway runs in."""
    from kiro_crew.dashboard.handlers import work_ledger_read

    state = _state(tmp_path, monkeypatch)
    sk, item_id = _done_item(state, tmp_path, {"kind": "pr_checks", "pr": 41})
    ran: list[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))
    for query in ("", f"item_id={item_id}"):
        status, body = _eval(state, sk, query)
        assert status == 200, body
        assert body["results"] == []
        assert body["skipped"] == [{"id": item_id, "why": work_ledger_read._UNPLACED_WHY}]
    assert ran == []


def test_only_a_code_channels_conductor_is_evaluated_by_the_gateway(
    tmp_path, monkeypatch, ledger_routes_open  # noqa: F811
):
    state = _state(tmp_path, monkeypatch)
    sk, _ = _done_item(state, tmp_path)
    monkeypatch.setattr(routes, "_code_channel_caller", lambda request: False)
    status, body = _eval(state, sk)
    assert status == 403 and body["code"] == "not_code_channel"


def test_a_stray_query_parameter_is_refused(
    tmp_path, monkeypatch, ledger_routes_open  # noqa: F811
):
    state = _state(tmp_path, monkeypatch)
    sk, _ = _done_item(state, tmp_path)
    assert _eval(state, sk, "items=1")[0] == 400
    assert _eval(state, sk, "item_id=" + "x" * 65)[0] == 400


def test_a_record_reply_names_the_projected_verdict(monkeypatch):
    """Sweep: the projection names the verdict ``ci``; the record reply reads it."""
    item = {"item_id": "it_00000001", "state": "open", "ci": "pass"}
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    monkeypatch.setattr(mcp_work, "_post", lambda *a, **k: {"item": item, "action": "verdict"})
    args = {"action": "verdict", "item_id": "it_00000001", "verdict": "pass"}
    line = mcp_work._call_tool_inner("work_ledger_record", args)
    assert "verdict=pass" in line
