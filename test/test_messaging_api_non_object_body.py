"""A messaging route answers a JSON body that is not an object with a 400.

Each of these handlers parses with ``await request.json()`` and then reads its
fields with ``body.get()``. A list, string or number is valid JSON, so the parse
succeeds and the ``.get()`` raised ``AttributeError``: a 500 for a client mistake.
Each now returns ``body_not_object_error``'s 400 right after the parse.

Every case also runs the route's malformed-JSON body and an empty object, so the
request double is shown to reach the parse, and an object body is shown to pass
the guard to the route's own first field check, exactly as before.
"""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from handlers_messaging_coverage_helpers import _BAD_JSON, _payload, _Req, _run, _state

import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew.dashboard.handlers._shared import body_not_object_error


def _mgr() -> Any:
    mgr = MagicMock()
    mgr.max_concurrent = 4
    mgr.all_agents = []
    mgr._agents = {}
    mgr._tasks = {}
    mgr.get.return_value = None
    return mgr


def _plain(body: Any) -> _Req:
    return _Req(_state(), body)


def _with_slack(body: Any) -> _Req:
    return _Req(_state(slack_client=MagicMock()), body)


def _with_manager(body: Any) -> _Req:
    return _Req(_state(subagents=_mgr()), body)


def _with_manager_for(agent_id: str) -> Callable[[Any], _Req]:
    def _build(body: Any) -> _Req:
        return _Req(_state(subagents=_mgr()), body, match_info={"agent_id": agent_id})

    return _build


#: (route, request builder, the error the route gives an empty object body). The
#: last column is the route's own first field check, which an object body reaches.
_ROUTES = [
    ("api_notification_delete", _plain, "ts is required"),
    ("api_notification_ack", _plain, "ts is required"),
    ("api_notification_unack", _plain, "ts is required"),
    ("api_delete_message", _plain, "channel and ts required"),
    ("api_update_message", _plain, "invalid channel ID format"),
    ("api_send_message", _plain, "text required"),
    ("api_spawn_steer", _with_manager_for("a1"), "message is required"),
    ("api_spawn_lost", _with_manager, "valid batch_id required"),
    ("api_spawn_mark_collected", _plain, "'ids' array required"),
    ("api_slack_pins", _with_slack, "action must be 'add', 'remove', or 'list'"),
    ("api_slack_reactions", _with_slack, "action must be 'add' or 'remove'"),
    ("api_slack_profile", _with_slack, "user required"),
    ("api_spawn", _with_manager, "task is required"),
    ("api_spawn_continue", _with_manager_for("conv1"), "task is required"),
]

_IDS = [name for name, _, _ in _ROUTES]

#: Valid JSON that is not an object.
_NOT_OBJECTS = [[], [{"ts": "1"}], "ts", 7, None]


@pytest.mark.parametrize(("name", "build", "_empty_error"), _ROUTES, ids=_IDS)
@pytest.mark.parametrize("body", _NOT_OBJECTS, ids=repr)
def test_a_body_that_is_not_an_object_is_a_400(
    name: str, build: Callable[[Any], _Req], _empty_error: str, body: Any
) -> None:
    resp = _run(getattr(mod, name), build(body))
    assert resp.status == 400
    assert _payload(resp) == {"error": "body must be a JSON object", "code": "body_not_object"}


@pytest.mark.parametrize(("name", "build", "_empty_error"), _ROUTES, ids=_IDS)
def test_the_request_reaches_the_parse(
    name: str, build: Callable[[Any], _Req], _empty_error: str
) -> None:
    resp = _run(getattr(mod, name), build(_BAD_JSON))
    assert resp.status == 400
    assert _payload(resp)["error"] == "invalid JSON"


@pytest.mark.parametrize(("name", "build", "empty_error"), _ROUTES, ids=_IDS)
def test_an_object_body_passes_the_guard(
    name: str, build: Callable[[Any], _Req], empty_error: str
) -> None:
    resp = _run(getattr(mod, name), build({}))
    assert resp.status == 400
    assert _payload(resp)["error"] == empty_error


def test_the_helper_passes_an_object_and_refuses_the_rest() -> None:
    assert body_not_object_error({}) is None
    assert body_not_object_error({"a": 1}) is None
    for body in _NOT_OBJECTS:
        refusal = body_not_object_error(body)
        assert refusal is not None and refusal.status == 400
