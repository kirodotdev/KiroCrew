"""``work_dispatch``: create, open, bind and seed one work item in one call.

Each case runs one tools/call frame through ``mcp_dashboard.TABLE`` against an
in-memory dashboard, and reads back which requests reached it and in what order.
The exit criteria are those of ``docs/request-for-change/rfc-work-dispatch.md``.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp_dashboard_folders_helpers import _CALLER, _call

from kiro_crew.mcp_tools.dashboard_client import DashboardRequest

_ITEM = "it_0000abcd"
_WORKER = "chat-9-900"
_ARGS = {
    "title": "Fix the flaky test",
    "acceptance": {"kind": "pr_checks", "pr": "TBD", "repo": "o/r"},
    "agent": "kirocrew-worker",
    "agent_kind": "template",
    "seed": "Call work_brief first.",
}
_CONDUCTOR_ROW = {"key": "chat-1-100", "title": "Conductor", "agent": "kirocrew-conductor"}
_LOST = {"error": "timed out", "transport_error": True}
_RECORD = "POST /api/work-ledger/record"
_CREATE = "POST /api/session-control/create"
_CLOSE = "POST /api/session-control/close"
_SEND = "POST /api/session-control/send"


def _ledger(**by_action: Any) -> Any:
    """A record route that answers each ``action`` from *by_action*."""

    def _answer(req: DashboardRequest) -> Any:
        action = req.body["action"]
        reply = by_action.get(action, {"action": action, "item": {"item_id": _ITEM}})
        return reply(req) if callable(reply) else reply

    return _answer


def _routes(**over: Any) -> dict[str, Any]:
    routes: dict[str, Any] = {
        "GET /api/chat/slots": [_CONDUCTOR_ROW],
        _RECORD: _ledger(
            create={"action": "create", "item": {"item_id": _ITEM}},
            bind={"action": "bind", "item": {"item_id": _ITEM}, "patrol": "armed"},
        ),
        _CREATE: {"target": _WORKER, "title": "Fix the flaky test"},
        _CLOSE: {"target": _WORKER},
        _SEND: {"target": _WORKER, "started": True},
    }
    routes.update(over)
    return routes


def _sent(dash: Any) -> list[str]:
    """Each write, as ``route`` or ``route action`` for the ledger."""
    out = []
    for r in dash.requests:
        if r.method == "GET":
            continue
        out.append(f"{r.route} {r.body['action']}" if r.route == _RECORD else r.route)
    return out


def test_the_happy_path_returns_the_ids_the_routes_returned() -> None:
    out, dash = _call("work_dispatch", dict(_ARGS), _routes())
    assert _sent(dash) == [f"{_RECORD} create", _CREATE, f"{_RECORD} bind", _SEND]
    (bind,) = [r for r in dash.sent(_RECORD) if r.body["action"] == "bind"]
    assert bind.body == {"action": "bind", "item_id": _ITEM, "worker_session_key": _WORKER}
    (create_item,) = [r for r in dash.sent(_RECORD) if r.body["action"] == "create"]
    # Marked, so the record route refuses a caller that owns no ledger.
    assert create_item.body["dispatch"] is True
    # The ledger route decides who may dispatch; the tool reads no slot first.
    assert dash.requests[0].route == _RECORD
    (seed,) = dash.sent(_SEND)
    assert seed.body == {"target": _WORKER, "message": _ARGS["seed"], "steer": False}
    (create,) = dash.sent(_CREATE)
    assert create.body["agent"] == "kirocrew-worker"
    assert create.body["agent_kind"] == "template"
    assert create.body["title"] == _ARGS["title"]
    assert f"item_id={_ITEM} worker_session_key={_WORKER}" in out
    assert "patrol=armed" in out
    # Every write goes out as the verified caller.
    writes = [r for r in dash.requests if r.method != "GET"]
    assert {r.session_key for r in writes} == {"dashboard:chat-1-100"}


def test_the_success_reply_carries_the_worker_capabilities_lines() -> None:
    routes = _routes(
        **{
            _CREATE: {"target": _WORKER, "title": "t", "worker_capabilities": "CREATE-CAPS"},
            _SEND: {"target": _WORKER, "started": True, "worker_capabilities": "SEND-CAPS"},
        }
    )
    out, _ = _call("work_dispatch", dict(_ARGS), routes)
    assert "CREATE-CAPS" in out and "SEND-CAPS" in out


def test_a_preflight_refusal_keeps_its_code() -> None:
    """A folder path that needs creating runs the create as a dry run first; its
    refusal code reaches the dispatch reply rather than reading as code=none."""

    def _create(req: DashboardRequest) -> dict:
        return {"error": "no", "code": "memory_delegation_denied"}

    routes = _routes(
        **{
            "GET /api/chat/folders": [],
            _CREATE: _create,
        }
    )
    out, dash = _call("work_dispatch", {**_ARGS, "folder": "Goal/kirocrew-worker"}, routes)
    assert dash.sent("POST /api/chat/folders") == []
    assert "step=session_create outcome=refused code=memory_delegation_denied" in out
    assert _SEND not in _sent(dash)


def test_a_folder_write_refusal_keeps_its_code() -> None:
    limited = {"error": "slow down", "code": "create_rate_limited"}
    routes = _routes(**{"GET /api/chat/folders": [], "POST /api/chat/folders": limited})
    out, dash = _call("work_dispatch", {**_ARGS, "folder": "Goal/kirocrew-worker"}, routes)
    # The dry run passed, the folder write was refused, the create was never sent.
    assert [r.body.get("dry_run") for r in dash.sent(_CREATE)] == [True]
    assert "step=session_create outcome=refused code=create_rate_limited" in out
    assert _SEND not in _sent(dash)


def test_a_lost_folder_write_is_unknown_not_refused() -> None:
    """The folder may exist although its reply was lost; nothing was sent."""
    routes = _routes(**{"GET /api/chat/folders": [], "POST /api/chat/folders": _LOST})
    out, dash = _call("work_dispatch", {**_ARGS, "folder": "Goal/kirocrew-worker"}, routes)
    assert "step=session_create outcome=outcome_unknown" in out
    assert "Read your board" in out
    assert [r.body.get("dry_run") for r in dash.sent(_CREATE)] == [True]
    assert _SEND not in _sent(dash)


def test_session_title_overrides_the_item_title() -> None:
    _, dash = _call("work_dispatch", {**_ARGS, "session_title": "worker A"}, _routes())
    assert dash.sent(_CREATE)[0].body["title"] == "worker A"


def test_a_refused_create_opens_no_session_and_sends_nothing() -> None:
    capped = {"error": "cap reached", "code": "goal_concurrency_cap_reached"}
    out, dash = _call("work_dispatch", dict(_ARGS), _routes(**{_RECORD: _ledger(create=capped)}))
    assert _sent(dash) == [f"{_RECORD} create"]
    assert "step=create outcome=refused code=goal_concurrency_cap_reached" in out


def test_a_refused_session_create_closes_the_item_and_sends_nothing() -> None:
    denied = {"error": "no", "code": "memory_delegation_denied"}
    out, dash = _call("work_dispatch", dict(_ARGS), _routes(**{_CREATE: denied}))
    assert _sent(dash) == [f"{_RECORD} create", _CREATE, f"{_RECORD} close"]
    (close,) = [r for r in dash.sent(_RECORD) if r.body["action"] == "close"]
    assert close.body["item_id"] == _ITEM and close.body["state"] == "abandoned"
    assert "step=session_create outcome=refused code=memory_delegation_denied" in out
    assert f"cleanup: close item `{_ITEM}` as abandoned: done" in out


def test_a_refused_bind_closes_the_session_then_the_item_and_sends_nothing() -> None:
    refused = {"error": "not yours", "code": "worker_not_owned"}
    out, dash = _call("work_dispatch", dict(_ARGS), _routes(**{_RECORD: _ledger(bind=refused)}))
    assert _sent(dash) == [
        f"{_RECORD} create",
        _CREATE,
        f"{_RECORD} bind",
        _CLOSE,
        f"{_RECORD} close",
    ]
    assert dash.sent(_CLOSE)[0].body == {"target": _WORKER}
    assert "step=bind outcome=refused code=worker_not_owned" in out
    assert "seed: not sent" in out


def test_a_refused_cleanup_is_reported_with_the_surviving_id() -> None:
    refused_bind = {"error": "not yours", "code": "worker_not_owned"}
    refused_close = {"error": "busy", "code": "target_busy"}
    out, dash = _call(
        "work_dispatch",
        dict(_ARGS),
        _routes(**{_RECORD: _ledger(bind=refused_bind), _CLOSE: refused_close}),
    )
    assert _SEND not in _sent(dash)
    assert f"cleanup: close session `{_WORKER}`: refused (code=target_busy): busy" in out
    assert f"worker_session_key={_WORKER}" in out
    # The cleanup is not retried.
    assert len(dash.sent(_CLOSE)) == 1


@pytest.mark.parametrize(
    ("step", "over", "writes"),
    [
        ("create", {_RECORD: _ledger(create=_LOST)}, [f"{_RECORD} create"]),
        (
            "session_create",
            {_CREATE: _LOST},
            [f"{_RECORD} create", _CREATE, f"{_RECORD} close"],
        ),
        (
            "bind",
            {_RECORD: _ledger(bind=_LOST)},
            [f"{_RECORD} create", _CREATE, f"{_RECORD} bind", _CLOSE, f"{_RECORD} close"],
        ),
    ],
)
def test_a_lost_reply_is_unknown_and_nothing_is_sent_after_it(
    step: str, over: dict[str, Any], writes: list[str]
) -> None:
    out, dash = _call("work_dispatch", dict(_ARGS), _routes(**over))
    assert _sent(dash) == writes
    assert f"step={step} outcome=outcome_unknown" in out
    assert "outcome=refused" not in out.splitlines()[0]


def test_a_lost_seed_returns_both_ids_and_warns_against_a_blind_resend() -> None:
    out, dash = _call("work_dispatch", dict(_ARGS), _routes(**{_SEND: _LOST}))
    assert _sent(dash)[-1] == _SEND
    assert "step=seed outcome=outcome_unknown" in out
    assert f"item_id={_ITEM} worker_session_key={_WORKER}" in out
    assert "session_read_message before resending" in out


def test_a_refused_seed_returns_both_ids_and_cleans_up_nothing() -> None:
    refused = {"error": "gone", "code": "target_not_found"}
    out, dash = _call("work_dispatch", dict(_ARGS), _routes(**{_SEND: refused}))
    assert _sent(dash)[-1] == _SEND
    assert _CLOSE not in _sent(dash)
    assert "step=seed outcome=refused code=target_not_found" in out
    assert f"item_id={_ITEM} worker_session_key={_WORKER}" in out


def test_an_unverified_caller_reaches_nothing() -> None:
    from mcp_dashboard_folders_helpers import _UNVERIFIED

    out, dash = _call("work_dispatch", dict(_ARGS), _routes(), _UNVERIFIED)
    assert dash.requests == []
    assert out.startswith("Error")


def test_a_missing_seed_is_refused_by_the_schema() -> None:
    args = {k: v for k, v in _ARGS.items() if k != "seed"}
    out, dash = _call("work_dispatch", args, _routes(), _CALLER)
    assert dash.requests == []
    assert "seed" in out


def test_no_shipped_grant_auto_approves_it() -> None:
    from kiro_crew.agent_parts import spec_grants

    for name in ("_CONDUCTOR_DASHBOARD_GRANTS", "_MEMBER_DASHBOARD_GRANTS"):
        assert "@kirocrew-dashboard/work_dispatch" not in getattr(spec_grants, name), name
