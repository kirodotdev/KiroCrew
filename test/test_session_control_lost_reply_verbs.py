"""Every session tool that opens or delivers something survives a lost reply.

``test_session_control_lost_reply.py`` pins the replay for ``session_send`` and
``session_create``. The same lost reply (a slow gateway outlasting the client's
wait) repeats a fork, a broadcast, a continue, a note, and a crew create or send
when the caller retries. Pinned here, for each of those tools:

* a lost reply is resent under the same id, and the reply is reported;
* replies lost on every try say the work may have happened and must not be repeated;
* a resend the gateway did not replay is reported as a possible double;

and, for each of their routes, that a resend of the same id does the work once.
A last test lists every session-control route and fails when a new one is neither
replayed nor named with the reason it cannot do its work twice.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew import mcp_session_once, remote_dispatch
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_control_replay
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_dashboard_session_runs import (
    BROADCAST_RESPONSE_MARGIN_SECS,
    BROADCAST_TARGET_ALLOWANCE_SECS,
    MAX_BROADCAST_TARGETS,
)
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_LOST = {"error": "timed out", "transport_error": True}
_CALLER = "dashboard:chat-1-100"


@pytest.fixture(autouse=True)
def _fresh_replays(_floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setattr(mcp_session_once, "LOST_REPLY_PAUSE_SECS", 0.0)
    # The crew tools are reachable only with the remote-dispatch preview on.
    _floor_monkeypatch.setattr(remote_dispatch, "remote_dispatch_enabled", lambda: True)
    session_control_replay.reset_for_tests()
    yield
    session_control_replay.reset_for_tests()


# ── The tools ────────────────────────────────────────────────────────────────

#: tool, args, route, the route's answer, a phrase of the success reply, the
#: "Do NOT ..." phrase of the all-lost reply, and the possible-double phrase.
_TOOLS = [
    pytest.param(
        "session_fork",
        {"source": "chat-2"},
        "POST /api/session-control/fork",
        {"ok": True, "source": "chat-2", "target": "chat-9", "title": "t", "messages": 3},
        "Forked `chat-2` into `chat-9`",
        "Do NOT fork it again",
        "a second fork may have been opened",
        id="fork",
    ),
    pytest.param(
        "session_broadcast",
        {"message": "rebase", "mode": "queue", "all": True},
        "POST /api/session-control/broadcast",
        {"ok": True, "mode": "queue", "requested": 1, "delivered": 1, "results": []},
        "Queued for 1/1 session(s)",
        "Do NOT broadcast it again",
        "the message may have been delivered twice",
        id="broadcast",
    ),
    pytest.param(
        "session_continue",
        {"target": "chat-2"},
        "POST /api/session-control/continue",
        {"ok": True, "target": "chat-2", "body": "continued"},
        "Continue sent to `chat-2`",
        "Do NOT continue it again",
        "it may have been told to continue twice",
        id="continue",
    ),
    pytest.param(
        "session_set_note",
        {"target": "chat-2", "note": "green"},
        "POST /api/session-control/set-note",
        {"ok": True, "visibleDeferred": False, "contextSkipped": False},
        "Left the note in `chat-2`",
        "Do NOT leave it again",
        "the note may have been left twice",
        id="set_note",
    ),
    pytest.param(
        "session_create",
        {"crew": "lab", "title": "w"},
        "POST /api/session-control/create",
        {"target": "chat-77", "crew_name": "lab", "title": "w"},
        "Opened `chat-77` (w) on crew `lab`",
        "Do NOT create it again",
        "a second crew session may have been opened",
        id="crew_create",
    ),
    pytest.param(
        "session_send",
        {"crew": "lab", "target": "chat-77", "message": "go"},
        "POST /api/session-control/send",
        {"target": "chat-77", "crew_name": "lab", "delivered": True},
        "delivered (queued if busy) to `chat-77` on crew `lab`",
        "Do NOT send it again",
        "the message may have been delivered twice",
        id="crew_send",
    ),
]


def _lost_then(reply: dict[str, Any], *, echo: bool = True):
    """A route that loses its first reply, then answers *reply*."""
    calls = {"n": 0}

    def _answer(req: DashboardRequest) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _LOST
        if not echo:
            return dict(reply)
        stamp = {"request_id": req.body["request_id"], "replay": "replayed", "replay_attempt": 0}
        return {**reply, **stamp}

    return _answer


def _call(tool: str, args: dict[str, Any], routes: dict[str, Any]):
    dash = InMemoryDashboardClient(routes)
    out = TABLE.call(tool, args, ToolContext(dash, Caller.strict(_CALLER)))
    return out, dash.requests


@pytest.mark.parametrize(("tool", "args", "route", "ok", "done", "again", "double"), _TOOLS)
def test_a_lost_reply_is_resent_under_one_id_and_the_reply_reported(
    tool, args, route, ok, done, again, double
) -> None:
    out, (first, second) = _call(tool, args, {route: _lost_then(ok)})
    assert (first.body.pop("request_attempt"), second.body.pop("request_attempt")) == (0, 1)
    assert first.body == second.body and len(first.body["request_id"]) == 32
    assert done in out
    assert double not in out


@pytest.mark.parametrize(("tool", "args", "route", "ok", "done", "again", "double"), _TOOLS)
def test_a_reply_lost_on_every_try_says_not_to_repeat_it(
    tool, args, route, ok, done, again, double
) -> None:
    out, sent = _call(tool, args, {route: _LOST})
    assert len(sent) == mcp_session_once.LOST_REPLY_ATTEMPTS
    assert len({r.body["request_id"] for r in sent}) == 1
    assert out.startswith("Error: no reply from the gateway")
    assert again in out


@pytest.mark.parametrize(("tool", "args", "route", "ok", "done", "again", "double"), _TOOLS)
def test_a_resend_the_gateway_ran_again_is_reported_as_a_possible_double(
    tool, args, route, ok, done, again, double
) -> None:
    out, _ = _call(tool, args, {route: _lost_then(ok, echo=False)})
    assert done in out
    assert double in out


_CREW_CALLS = [
    pytest.param(
        "session_create",
        {"crew": "lab", "title": "w"},
        "POST /api/session-control/create",
        "crew session may still have been opened",
        "Do NOT create it again",
        id="crew_create",
    ),
    pytest.param(
        "session_send",
        {"crew": "lab", "target": "chat-77", "message": "go"},
        "POST /api/session-control/send",
        "message may still have been delivered",
        "Do NOT send it again",
        id="crew_send",
    ),
]


@pytest.mark.parametrize("code", ["crew_peer_interrupted", "proxy_peer_unreachable"])
@pytest.mark.parametrize(("tool", "args", "route", "outcome", "again"), _CREW_CALLS)
def test_a_crew_call_the_crew_never_answered_says_not_to_repeat_it(
    tool, args, route, outcome, again, code
) -> None:
    """The hub's call to the crew went out and no answer came back: the crew may have acted."""
    cut = {"error": "that crew stopped answering partway through", "code": code}
    out, sent = _call(tool, args, {route: cut})
    assert len(sent) == 1
    assert outcome in out and again in out


@pytest.mark.parametrize(("tool", "args", "route", "outcome", "again"), _CREW_CALLS)
def test_a_crew_refusal_stays_a_refusal(tool, args, route, outcome, again) -> None:
    refused = {"error": "the crew answered 400", "code": "crew_peer_refused"}
    out, _ = _call(tool, args, {route: refused})
    assert out.startswith("Error: could not") and again not in out


def test_every_broadcast_post_keeps_the_broadcast_wait() -> None:
    ok = {"ok": True, "mode": "queue", "requested": 0, "delivered": 0, "results": []}
    _, sent = _call(
        "session_broadcast",
        {"message": "rebase", "mode": "queue", "all": True},
        {"POST /api/session-control/broadcast": _lost_then(ok)},
    )
    wait = MAX_BROADCAST_TARGETS * BROADCAST_TARGET_ALLOWANCE_SECS + BROADCAST_RESPONSE_MARGIN_SECS
    assert [r.timeout for r in sent] == [wait, wait]


# ── The routes ───────────────────────────────────────────────────────────────

_ID = "0123456789abcdef0123456789abcdef"


def _request(state, caller, path: str, body: dict[str, Any]) -> MagicMock:
    request = MagicMock()
    request.app = {"state": state}
    request.path = path
    request.method = "POST"
    request.query = {}
    request.headers = {"X-Session-Key": _key(caller)}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def _twice(state, caller, route, path: str, body: dict[str, Any]):
    """*route* answering *body*, then a resend of it; both replies as ``(status, body)``."""

    async def _go():
        first = await route(_request(state, caller, path, dict(body)))
        resent = await route(_request(state, caller, path, dict(body)))
        return first, resent

    return [(r.status, json.loads(r.body.decode())) for r in asyncio.run(_go())]


#: route, the session-control verb it runs, its path, and a body for it.
_ROUTES = [
    pytest.param(
        handlers_sc.api_session_control_fork,
        "fork_session",
        "/api/session-control/fork",
        {"source": "chat-2"},
        id="fork",
    ),
    pytest.param(
        handlers_sc.api_session_control_continue,
        "continue_target",
        "/api/session-control/continue",
        {"target": "chat-2"},
        id="continue",
    ),
    pytest.param(
        handlers_sc.api_session_control_broadcast,
        "broadcast_to_targets",
        "/api/session-control/broadcast",
        {"message": "rebase", "mode": "queue", "targets": ["chat-2"]},
        id="broadcast",
    ),
]


@pytest.mark.parametrize(("route", "verb", "path", "body"), _ROUTES)
def test_a_resend_of_the_same_id_runs_the_verb_once(
    tmp_path, monkeypatch, route, verb, path, body
) -> None:
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    runs: list[dict[str, Any]] = []

    async def _verb(_state, **kwargs):
        runs.append(kwargs)
        return {"ok": True, "target": "chat-9"}

    monkeypatch.setattr(sc, verb, _verb)
    (status, first), (_, resent) = _twice(
        state, caller, route, path, {**body, "request_id": _ID, "request_attempt": 0}
    )
    assert status == 200, first
    assert len(runs) == 1
    assert (first["replay"], resent["replay"], resent["replay_attempt"]) == (
        "first",
        "replayed",
        0,
    )


def test_an_empty_audience_resend_keeps_the_possible_double_warning() -> None:
    empty = {"ok": True, "mode": "queue", "audience_empty": True, "results": []}
    out, _ = _call(
        "session_broadcast",
        {"message": "rebase", "mode": "queue", "all": True},
        {"POST /api/session-control/broadcast": _lost_then(empty, echo=False)},
    )
    assert "Nothing to broadcast to" in out
    assert "the message may have been delivered twice" in out


def test_a_note_resent_after_its_reply_was_lost_is_left_once(tmp_path) -> None:
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    child = _slot(state, "chat-2")
    child._created_by = "chat-1"
    body = {"target": "chat-2", "note": "The build is green.", "request_id": _ID}
    (status, first), (_, resent) = _twice(
        state,
        caller,
        handlers_sc.api_session_control_set_note,
        "/api/session-control/set-note",
        body,
    )
    assert status == 200, first
    assert [m["content"] for m in child.messages if m.get("role") == "inject"] == [
        "The build is green."
    ]
    assert resent["replay"] == "replayed"


# ── Every route is either replayed or says why it need not be ────────────────

#: The session-control routes a resend cannot make do their work twice, and why.
#: A new route that opens or delivers something belongs in the replayed set instead.
_NOT_REPLAYED = {
    "stop": "a repeat stops a turn already stopping, which the reply says",
    "retry": "a repeat is refused once the retried turn runs or ends (turn_not_failed)",
    "end_wait": "a repeat finds no wait to end",
    "set_model": "sets the same value again",
    "reload": "relaunches the agent process, keeping the conversation; opens or delivers nothing",
    "rename": "sets the same value again",
    "generate_title": "a repeat is refused by the one-per-window title budget",
    "set_color": "sets the same value again",
    "close": "a repeat finds the session already closed",
    "revive": "a repeat is refused: the session is already open",
    "history": "a read",
    "status": "a read",
    "adopt": "sets the same parent again",
    "handoff": "a repeat is refused already_manager or not_manager",
    "release": "a repeat finds the session already released",
    "read": "a read",
    "summary": "a read",
    "queue": "lists, or moves an entry to an absolute position, which a repeat leaves as it is",
    "run_queued": "a repeat finds the stop already pending (noop) or the entry gone",
}

_REPLAYED = {"create", "send", "fork", "broadcast", "continue", "set_note"}


def _routes() -> dict[str, Any]:
    return {
        name.removeprefix("api_session_control_"): inspect.unwrap(fn)
        for name, fn in vars(handlers_sc).items()
        if name.startswith("api_session_control_") and inspect.iscoroutinefunction(fn)
    }


def test_every_route_is_replayed_or_named_with_its_reason() -> None:
    routes = _routes()
    replayed = {
        op
        for op, fn in routes.items()
        if f'session_control_replay.once(request, "{op}"' in inspect.getsource(fn)
    }
    assert replayed == _REPLAYED
    assert set(routes) - replayed == set(_NOT_REPLAYED)
