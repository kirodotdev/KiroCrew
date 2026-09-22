"""The crew-log read routes and the ``session_projection`` push.

Four things are pinned here that the fold tests cannot see: the owner gate on
the browser's two reads, the one-rule identity gate on the agent's unit-keyed
reads (any session the gateway can NAME may read any unit), the deliberate
difference in posture between a PAGE read and a FOLD read over the same bytes,
and that the push sends a frame only for a projection whose seq actually moved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, Ref
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import crew_log as routes

SESSION = "s-route"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture(autouse=True)
def _is_owner():
    """Every test here calls as the dashboard owner unless it says otherwise."""
    with patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=True,
    ):
        yield


def _log(unit_id: str = SESSION, slot: str = "dashboard:1") -> CrewLog:
    # The HEADER's slot, which is what the session tree keys its fold by. Separate
    # from the opening entry's own ``slot`` field, and the two are kept equal here
    # because the emitter writes them from the same value.
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _opened(
    handle: CrewLog,
    *,
    parent_sid: str = "",
    parent_slot: str = "",
    slot: str = "dashboard:1",
    memory: str = "persistent",
    app: str = "",
    channel: bool = False,
    workspace: str = "default",
    record_class: bool = True,
) -> None:
    data = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent_slot:
        # The same shape ``crew_log.emit`` writes: ``slot`` names the creator's tab,
        # which is what a scope test reads because a slot outlives its ACP session,
        # and ``sid`` names the creator's crew log unit as an audit citation.
        data["parent"] = {"slot": parent_slot}
        if parent_sid:
            data["parent"]["sid"] = parent_sid
    if record_class:
        # ``record_class=False`` is a log written before the class was recorded, which
        # a cross-session read must refuse rather than read as "nothing applies".
        session_class: dict[str, object] = {"memory": memory}
        if app:
            session_class["app"] = app
        if channel:
            session_class["channel"] = True
        if workspace:
            session_class["workspace"] = workspace
        data["class"] = session_class
    handle.append("session/opened", data, src=GATEWAY)


def _class_moved(
    unit_id: str,
    *,
    memory: str = "persistent",
    app: str = "",
    channel: bool = False,
    workspace: str = "default",
) -> None:
    """Append the ``session/class`` the emitter writes when a class moves mid-life.

    Same payload as the opening entry's ``class`` object, which is why both are
    declared from one field tuple: a reader folds the second over the first. The log
    is OPENED rather than created, because a transition by definition arrives after
    the opener and ``create`` refuses a log that already exists.
    """
    data: dict[str, object] = {"memory": memory}
    if app:
        data["app"] = app
    if channel:
        data["channel"] = True
    if workspace:
        data["workspace"] = workspace
    CrewLog.open(lg.KIND_SESSION, unit_id).append("session/class", data, src=GATEWAY)


def _turn(handle: CrewLog, turn: int) -> None:
    handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append(
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 10,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.1,
            "tokens": {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0},
        },
        src=GATEWAY,
    )


def _page_request(session_id: str = SESSION, query: str = "") -> object:
    url = f"/api/sessions/{session_id}/crew-log" + (f"?{query}" if query else "")
    request = make_mocked_request("GET", url)
    request.match_info["id"] = session_id
    return request


def _projection_request(name: str, session_id: str = SESSION) -> object:
    request = make_mocked_request("GET", f"/api/sessions/{session_id}/crew-log/projection/{name}")
    request.match_info["id"] = session_id
    request.match_info["name"] = name
    return request


def _body(response) -> dict:
    return json.loads(response.body)


# --- the owner gate -------------------------------------------------------


@pytest.mark.asyncio
async def test_both_reads_refuse_a_caller_that_is_not_the_owner():
    """A crew log holds the session's message bodies, so a reader must be the owner."""
    _opened(_log())
    with (
        patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ),
        patch("kiro_crew.dashboard.handlers._shared.logger"),
    ):
        page = await routes.api_session_crew_log(_page_request())
        fold = await routes.api_session_crew_log_projection(_projection_request("status"))
    assert page.status in {401, 403}
    assert fold.status in {401, 403}


@pytest.mark.asyncio
async def test_the_module_stands_behind_the_private_member_guard():
    """Every ``api_`` route here is wrapped, so one added later is refused by default."""
    for handler in (
        routes.api_session_crew_log,
        routes.api_session_crew_log_projection,
        routes.api_session_crew_log_projections,
    ):
        assert getattr(handler, "__wrapped__", None) is not None


def test_every_crew_log_route_is_reachable_from_the_router():
    """Registration is where a handler that is not EXPORTED bites.

    The route table reaches these handlers through the handlers PACKAGE, so a
    handler added to this module and left out of that package's re-export raises
    at ``register`` -- the gateway then fails to boot, which the unit tests here
    cannot see because they call the functions directly.

    A SUBSET, not an equal set: the same router also mounts the unit-keyed door the
    ``kirocrew-crew-log`` MCP server reads through, and those paths belong to that
    feature's own tests. Asserting the whole set here would redden this test every
    time someone else adds a crew-log route, which teaches the next person to widen
    the assertion rather than to read it.
    """
    from aiohttp import web as _web

    from kiro_crew.dashboard.routes import sessions as sessions_routes

    app = _web.Application()
    sessions_routes.register(app)
    paths = {
        resource.canonical
        for resource in app.router.resources()
        if "crew-log" in str(resource.canonical)
    }
    assert {
        "/api/sessions/{id}/crew-log",
        "/api/sessions/{id}/crew-log/projection/{name}",
        "/api/sessions/{id}/crew-log/projections",
    } <= paths


# --- which unit a read addresses -----------------------------------------


class _Provider:
    """The one attribute ``session_id_of`` reads off a live provider."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Sessions:
    """A SessionManager stand-in: an exact key lookup, the way the resolver uses it.

    Shared by the tests below and by the unit-keyed door's ``_State``: both address a
    unit through one slot key, so one stand-in serves both and lives above the first
    of them.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def get_provider(self, key: str) -> object | None:
        found = self._mapping.get(key)
        return _Provider(found) if found else None


def _request_with_sessions(kind: str, session_id: str, mapping: dict[str, str], name: str = ""):
    """A request whose app really holds a dashboard state with *mapping*.

    A real ``web.Application`` rather than the default mocked app: the resolution
    reads ``request.app["state"]``, and a mock answers every lookup with another
    mock, so a test built on one would pass whatever the handler did.
    """
    state = MagicMock()
    state.sessions = _Sessions(mapping)
    # A REAL slot lookup, not the MagicMock's auto-attribute: the read resolves a
    # slot's effective session key, and a Mock answers that with a truthy object
    # that is not a key -- so every test here would exercise a path no gateway has.
    # Tests about a channel-linked slot override this with their own.
    state.get_slot = lambda name: None
    app = web.Application()
    app["state"] = state
    if kind == "page":
        request = make_mocked_request("GET", f"/api/sessions/{session_id}/crew-log", app=app)
        request.match_info["id"] = session_id
        return request
    if kind == "folds":
        request = make_mocked_request(
            "GET", f"/api/sessions/{session_id}/crew-log/projections", app=app
        )
        request.match_info["id"] = session_id
        return request
    request = make_mocked_request(
        "GET", f"/api/sessions/{session_id}/crew-log/projection/{name}", app=app
    )
    request.match_info["id"] = session_id
    request.match_info["name"] = name
    return request


@pytest.mark.asyncio
async def test_a_read_addressed_by_slot_key_folds_that_slot_s_unit():
    """A chat surface holds a SLOT key; the unit is keyed by the ACP session id.

    Without the resolution this answers an empty fold for a session that has
    entries, which is a panel that is always blank rather than one that is wrong
    in a visible way.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    fold = await routes.api_session_crew_log_projection(
        _request_with_sessions("fold", "chat-7", {"chat-7": SESSION}, name="status")
    )
    body = _body(fold)
    assert body["seq"] == handle.last_seq
    assert body["value"]["turns_completed"] == 1
    # The answer names what the CALLER asked about, so a client polling by slot key
    # can match the response to its request.
    assert body["session_id"] == "chat-7"

    page = _body(
        await routes.api_session_crew_log(
            _request_with_sessions("page", "chat-7", {"chat-7": SESSION})
        )
    )
    assert page["exists"] is True
    assert page["last_seq"] == handle.last_seq
    # The page read builds its payload around the unit it opened; it must still
    # answer with the id the caller sent, or a slot-addressed client is handed an
    # ACP id it never asked about.
    assert page["session_id"] == "chat-7"


@pytest.mark.asyncio
async def test_the_projection_routes_refuse_a_slot_keyed_fold_its_owner_serves(monkeypatch):
    """A slot-keyed fold its OWNER serves (the radar fold: its owner orders the slot's
    units by what the crew recorded and pins the live unit last) is refused by both
    projection routes the way an unregistered name is, so a client cannot be handed a
    part of the record as the whole. The slot-keyed fold this route DOES serve, and
    the per-unit folds, still answer."""
    handle = _log()
    _opened(handle)
    assert crew_log.OWNER_SERVED_SLOT_PROJECTION == "radar"
    assert crew_log.OWNER_SERVED_SLOT_PROJECTION in crew_log.SLOT_PROJECTION_NAMES
    assert crew_log.SLOT_PROJECTION_NAMES != (crew_log.OWNER_SERVED_SLOT_PROJECTION,)
    for name in (crew_log.OWNER_SERVED_SLOT_PROJECTION,):
        response = await routes.api_session_crew_log_projection(
            _request_with_sessions("fold", "chat-7", {"chat-7": SESSION}, name=name)
        )
        assert response.status == 400
        assert _body(response)["code"] == "unknown_projection"
        _flag_on(monkeypatch)
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/{name}",
            slots={"chat-owner": _Slot(restricted=False)},
            match={"unit": SESSION, "name": name},
        )
        unit_route = await routes.api_crew_log_unit_projection(request)
        assert unit_route.status == 400
        assert json.loads(unit_route.text)["code"] == "unknown_projection"
    # The slot-keyed fold this route serves, and a per-unit fold, still answer.
    for name in ("ledger", "status"):
        fold = await routes.api_session_crew_log_projection(
            _request_with_sessions("fold", "chat-7", {"chat-7": SESSION}, name=name)
        )
        assert fold.status == 200, name


@pytest.mark.asyncio
async def test_the_batch_read_answers_every_fold_from_one_resolution():
    """Every fold, resolved once and folded once, so they cannot disagree.

    Per-name reads each resolve the session for themselves, so a session
    replaced mid-flight can leave some answers describing the unit going away and
    some the one arriving. This route removes that window: one resolution, one pass
    over one file.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    body = _body(
        await routes.api_session_crew_log_projections(
            _request_with_sessions("folds", "chat-7", {"chat-7": SESSION})
        )
    )
    assert body["session_id"] == "chat-7"
    assert set(body["projections"]) == set(crew_log.PROJECTION_NAMES)
    assert body["projections"]["status"]["value"]["turns_completed"] == 1
    # Every fold came from the same read, so none of them can be ahead of the file
    # the others were folded from.
    assert {fold["seq"] for fold in body["projections"].values()} <= {0, handle.last_seq}


@pytest.mark.asyncio
async def test_the_batch_read_of_an_unresolvable_key_is_empty_not_an_error():
    """A slot with no unit reads back five empty folds, the same as no entries."""
    _opened(_log())
    body = _body(
        await routes.api_session_crew_log_projections(
            _request_with_sessions("folds", "chat-new", {})
        )
    )
    assert body["session_id"] == "chat-new"
    assert body["projections"]["status"]["seq"] == 0
    assert body["projections"]["status"]["value"]["lifecycle"] == "unknown"


@pytest.mark.asyncio
async def test_a_read_addressed_by_the_acp_id_still_reads_that_unit():
    """An ACP id is not a session KEY, so the registry misses and the id stands."""
    handle = _log()
    _opened(handle)
    body = _body(
        await routes.api_session_crew_log_projection(
            _request_with_sessions("fold", SESSION, {"chat-7": SESSION}, name="status")
        )
    )
    assert body["seq"] == handle.last_seq
    assert body["session_id"] == SESSION


@pytest.mark.asyncio
async def test_an_id_the_registry_recognises_is_resolved_even_if_it_looks_like_a_unit():
    """The registry WINS over the verbatim branch, and that ordering is deliberate.

    Nothing enforces that a provider's session id can never equal a live session
    key -- the two namespaces are minted by different code -- so the ordering
    decides what happens if one ever does: a recognised id is resolved, which is
    the branch a chat surface depends on for every read it makes. Pinned here so
    the precedence is a decision with a test behind it rather than a side effect
    of an `or`, and so a reader of the spec's addressing section can see which way
    a collision would fall.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    # The caller names something shaped like a unit id, and the registry happens to
    # serve it: the resolved unit is read, not the given string.
    body = _body(
        await routes.api_session_crew_log_projection(
            _request_with_sessions(
                "fold", "acp-looking-id", {"acp-looking-id": SESSION}, name="status"
            )
        )
    )
    assert body["value"]["turns_completed"] == 1
    assert body["session_id"] == "acp-looking-id"


@pytest.mark.asyncio
async def test_a_channel_born_slot_folds_the_session_its_turns_run_on():
    """A channel slot's provider is registered under its LINKED key, not its own.

    A slot born from a channel message runs its turns on the channel's session and
    carries that key in ``linked_session_key`` (``slack:<ts>``), so the ACP provider
    sits under that key. The resolver is an exact registry lookup whose only retry is
    the ``dashboard:`` form, so sending the bare slot key missed the provider and
    folded an empty record for every channel-linked session -- and never recovered,
    because the mapping is stable rather than racy.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    # The registry knows ONLY the linked channel key, which is the real arrangement.
    request = _request_with_sessions(
        "fold", "chat-9", {"slack:1789822000.42": SESSION}, name="status"
    )
    request.app["state"].get_slot = lambda name: (
        SimpleNamespace(key="chat-9", linked_session_key="slack:1789822000.42")
        if name == "chat-9"
        else None
    )
    body = _body(await routes.api_session_crew_log_projections(request))
    assert body["projections"]["status"]["value"]["turns_completed"] == 1
    assert body["resolved"] is True
    # Still the caller's own id on the wire, the rule every read here follows.
    assert body["session_id"] == "chat-9"


@pytest.mark.asyncio
async def test_an_id_naming_no_slot_reaches_the_resolver_unchanged():
    """An ACP unit id names no slot, and must not be rewritten on its way through."""
    handle = _log()
    _opened(handle)
    request = _request_with_sessions("fold", SESSION, {}, name="status")
    request.app["state"].get_slot = lambda name: None
    body = _body(await routes.api_session_crew_log_projection(request))
    assert body["seq"] == handle.last_seq
    assert body["session_id"] == SESSION


@pytest.mark.asyncio
async def test_an_unresolvable_key_reads_back_an_empty_fold():
    """A slot that never ran a turn has no unit, and an empty fold is the answer."""
    _opened(_log())
    body = _body(
        await routes.api_session_crew_log_projection(
            _request_with_sessions("fold", "chat-new", {}, name="status")
        )
    )
    assert body["seq"] == 0
    assert body["value"]["lifecycle"] == "unknown"


@pytest.mark.asyncio
async def test_an_empty_fold_says_whether_a_unit_was_addressable_at_all():
    """An empty fold has two causes and a reader must not be told the wrong one.

    A slot whose ACP session was torn down -- an idle reset -- still has its record
    on disk under the retired id, so answering its panel "nothing recorded for this
    session" is FALSE. The two cases are indistinguishable from the fold alone (both
    are seq 0), so the answer carries whether a unit was named for the given id.
    """
    _opened(_log())
    torn_down = _body(
        await routes.api_session_crew_log_projections(
            _request_with_sessions("folds", "chat-idle", {})
        )
    )
    assert torn_down["projections"]["status"]["seq"] == 0
    assert torn_down["resolved"] is False

    live = _body(
        await routes.api_session_crew_log_projections(
            _request_with_sessions("folds", "chat-7", {"chat-7": SESSION})
        )
    )
    assert live["resolved"] is True


@pytest.mark.asyncio
async def test_the_batch_read_reports_whether_the_writer_owed_anything():
    """A fold read that raced the writer must not present itself as current.

    The emitter queues an append and returns, so a turn can end with entries still
    owed -- and the refresh that turn triggers would fold a file it has not finished
    writing. The read waits briefly and then says which happened, rather than
    handing back a value that is behind the record with nothing to show it.
    """
    handle = _log()
    _opened(handle)
    body = _body(
        await routes.api_session_crew_log_projections(_request_with_sessions("folds", SESSION, {}))
    )
    assert body["writes_drained"] is True

    with patch.object(routes, "_settle_writes", return_value=False):
        raced = _body(
            await routes.api_session_crew_log_projections(
                _request_with_sessions("folds", SESSION, {})
            )
        )
    assert raced["writes_drained"] is False

    # The folds are still served: a read that could not confirm the drain is worth
    # less than one that could, and far more than no answer at all.
    assert set(raced["projections"]) == set(crew_log.PROJECTION_NAMES)


@pytest.mark.asyncio
async def test_the_settle_step_waits_on_the_emitter_s_own_flush():
    """The drain must be the emitter's, not a local guess at what quiet means.

    A batch the writer has already CLAIMED is absent from the per-session queue and
    cannot be seen there, so a predicate this module wrote for one session would
    report quiet in exactly the case that matters. ``emit.flush`` is the emitter's
    answer for a caller that must read the file it just wrote, and this pins that it
    is what gets called, with a bounded wait rather than an unbounded one.
    """
    from kiro_crew.crew_log import emit

    with patch.object(emit, "flush", return_value=True) as flush:
        assert routes._settle_writes() is True
    flush.assert_called_once_with(timeout=routes._SETTLE_SECONDS)
    assert 0 < routes._SETTLE_SECONDS <= 2.0


@pytest.mark.asyncio
async def test_a_read_without_dashboard_state_uses_the_id_it_was_given():
    """No registry to ask (a mocked or partially built app) is not a reason to fail."""
    handle = _log()
    _opened(handle)
    body = _body(await routes.api_session_crew_log_projection(_projection_request("status")))
    assert body["seq"] == handle.last_seq


# --- the page read --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_returns_the_requested_range_oldest_first():
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    _turn(handle, 2)
    body = _body(await routes.api_session_crew_log(_page_request(query="from=2&to=3")))
    assert [row["seq"] for row in body["entries"]] == [2, 3]
    assert body["from"] == 2
    assert body["to"] == 3
    assert body["last_seq"] == handle.last_seq
    assert body["exists"] is True


@pytest.mark.asyncio
async def test_a_page_hands_back_the_cursor_for_the_next_one():
    handle = _log()
    _opened(handle)
    for turn in range(1, 4):
        _turn(handle, turn)
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=2")))
    assert body["next_from"] == 3
    tail = _body(await routes.api_session_crew_log(_page_request(query="from=3&to=999")))
    assert tail["next_from"] is None


@pytest.mark.asyncio
async def test_a_span_wider_than_one_page_is_clamped_rather_than_refused():
    handle = _log()
    _opened(handle)
    body = _body(
        await routes.api_session_crew_log(
            _page_request(query=f"from=1&to={lg.MAX_PAGE_LIMIT + 500}")
        )
    )
    assert body["to"] == lg.MAX_PAGE_LIMIT
    assert handle.last_seq == 1


@pytest.mark.asyncio
async def test_from_defaults_to_the_start_and_to_defaults_to_one_page():
    _opened(_log())
    body = _body(await routes.api_session_crew_log(_page_request()))
    assert body["from"] == 1
    assert body["to"] == lg.DEFAULT_PAGE_LIMIT


@pytest.mark.asyncio
async def test_a_malformed_range_is_refused():
    _opened(_log())
    for query in ("from=0", "from=abc", "from=5&to=2", "to=-1"):
        response = await routes.api_session_crew_log(_page_request(query=query))
        assert response.status == 400, query
        assert _body(response)["code"] == "bad_range"


@pytest.mark.asyncio
async def test_a_session_with_no_crew_log_reads_as_an_empty_page():
    body = _body(await routes.api_session_crew_log(_page_request("s-none")))
    assert body["exists"] is False
    assert body["entries"] == []
    assert body["last_seq"] == 0


# --- refs on a page ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_resolves_a_ref_to_a_verdict_and_a_span():
    child = _log("s-child")
    _opened(child)
    _turn(child, 1)
    parent = _log()
    _opened(parent)
    parent.append(
        "subagent/spawned",
        {"turn": 1, "agent_id": "sub-1", "agent": "worker", "model": "opus"},
        src=GATEWAY,
        ref=Ref(unit=lg.KIND_SESSION, id="s-child", from_seq=1, to_seq=2),
    )
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    cited = [row for row in body["entries"] if "ref_resolution" in row]
    assert len(cited) == 1
    assert cited[0]["ref_resolution"]["status"] == lg.STATUS_OK
    assert cited[0]["ref_resolution"]["entries"] == 2
    # The verdict and the span, never the cited bytes.
    assert "entries" not in cited[0]["ref"]


@pytest.mark.asyncio
async def test_a_ref_into_a_session_that_has_no_log_resolves_as_gone():
    parent = _log()
    _opened(parent)
    parent.append(
        "subagent/spawned",
        {"turn": 1, "agent_id": "sub-1", "agent": "worker", "model": "opus"},
        src=GATEWAY,
        ref=Ref(unit=lg.KIND_SESSION, id="s-vanished", from_seq=1),
    )
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    cited = [row for row in body["entries"] if "ref_resolution" in row]
    assert cited[0]["ref_resolution"]["status"] == lg.STATUS_GONE


@pytest.mark.asyncio
async def test_identical_refs_on_one_page_are_resolved_once():
    child = _log("s-child")
    _opened(child)
    parent = _log()
    _opened(parent)
    pointer = Ref(unit=lg.KIND_SESSION, id="s-child", from_seq=1)
    for index in range(3):
        parent.append(
            "subagent/spawned",
            {"turn": 1, "agent_id": f"sub-{index}", "agent": "worker", "model": "opus"},
            src=GATEWAY,
            ref=pointer,
        )
    with patch.object(CrewLog, "resolve", autospec=True, side_effect=CrewLog.resolve) as spy:
        body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    assert spy.call_count == 1
    cited = [row for row in body["entries"] if "ref_resolution" in row]
    assert len(cited) == 3


@pytest.mark.asyncio
async def test_a_page_past_its_ref_budget_says_how_many_it_left():
    child = _log("s-child")
    _opened(child)
    parent = _log()
    _opened(parent)
    extra = 2
    for index in range(routes.MAX_PAGE_REFS + extra):
        # A DISTINCT ref each time, so the budget rather than the dedupe is what
        # bounds the work.
        CrewLog.create(lg.KIND_SESSION, f"s-kid{index}", owner="raymond", agent="kirocrew")
        parent.append(
            "subagent/spawned",
            {"turn": 1, "agent_id": f"sub-{index}", "agent": "worker", "model": "opus"},
            src=GATEWAY,
            ref=Ref(unit=lg.KIND_SESSION, id=f"s-kid{index}", from_seq=1),
        )
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=200")))
    resolved = [row for row in body["entries"] if "ref_resolution" in row]
    assert len(resolved) == routes.MAX_PAGE_REFS
    assert body["refs_unresolved"] == extra


# --- the projection read -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", crew_log.PROJECTION_NAMES)
async def test_each_projection_is_served_with_the_seq_it_folded_through(name):
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    body = _body(await routes.api_session_crew_log_projection(_projection_request(name)))
    assert body["name"] == name
    assert body["seq"] == handle.last_seq
    assert body["session_id"] == SESSION
    assert body["value"] == crew_log.read_projection(SESSION, name).value


@pytest.mark.asyncio
async def test_an_unknown_projection_name_is_refused():
    _opened(_log())
    response = await routes.api_session_crew_log_projection(_projection_request("board"))
    assert response.status == 400
    assert _body(response)["code"] == "unknown_projection"


@pytest.mark.asyncio
async def test_a_projection_for_a_session_with_no_log_is_the_empty_one():
    body = _body(await routes.api_session_crew_log_projection(_projection_request("usage", "s-x")))
    assert body["seq"] == 0
    assert body["value"]["turns"]["completed"] == 0


# --- the posture difference ---------------------------------------------


def _plant_unknown_required_type(handle: CrewLog) -> None:
    line = json.dumps(
        {
            "type": "turn/teleported",
            "seq": handle.last_seq + 1,
            "time": 1789000000000,
            "src": GATEWAY,
            "data": {"turn": 1},
        }
    )
    with handle.path.open("a", encoding="utf-8") as sink:
        sink.write(line + "\n")


@pytest.mark.asyncio
async def test_a_page_shows_a_line_it_cannot_interpret():
    """Paging renders history for a person: an unfamiliar line is a detail, not a fault."""
    handle = _log()
    _opened(handle)
    _plant_unknown_required_type(handle)
    body = _body(await routes.api_session_crew_log(_page_request(query="from=1&to=9")))
    assert [row["type"] for row in body["entries"]] == ["session/opened", "turn/teleported"]


@pytest.mark.asyncio
async def test_a_fold_refuses_a_line_it_cannot_interpret():
    """A fold would answer with a total the unknown line may have changed."""
    handle = _log()
    _opened(handle)
    _plant_unknown_required_type(handle)
    response = await routes.api_session_crew_log_projection(_projection_request("usage"))
    assert response.status == 409
    assert _body(response)["code"] == lg.CODE_UNKNOWN_ENTRY_TYPE


# --- the push -----------------------------------------------------------


class _Sockets:
    """A dashboard state stub that records what a push would send."""

    def __init__(self, watchers: int = 1) -> None:
        self.frames: list[tuple[str, dict]] = []
        self._watchers = watchers

    def dashboard_user_ws_count(self) -> int:
        return self._watchers

    def broadcast_ws_owners(self, frame: str, data: dict) -> None:
        self.frames.append((frame, data))


@pytest.mark.asyncio
async def test_a_growth_pushes_one_frame_per_projection():
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    assert {frame for frame, _ in state.frames} == {routes.FRAME}
    assert {data["name"] for _, data in state.frames} == set(crew_log.PROJECTION_NAMES)
    for _, data in state.frames:
        assert data["session_id"] == SESSION
        assert data["seq"] == handle.last_seq
        assert "value" in data


@pytest.mark.asyncio
async def test_a_second_pass_with_no_growth_pushes_nothing():
    handle = _log()
    _opened(handle)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    state.frames.clear()
    await publisher._publish(SESSION)
    assert state.frames == []


@pytest.mark.asyncio
async def test_a_growth_only_reads_the_entries_that_arrived():
    handle = _log()
    _opened(handle)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    first_seq = handle.last_seq
    _turn(handle, 1)
    with patch.object(CrewLog, "iter_from", autospec=True, side_effect=CrewLog.iter_from) as spy:
        await publisher._publish(SESSION)
    assert spy.call_args.args[1] == first_seq + 1


@pytest.mark.asyncio
async def test_no_watcher_means_no_fold_and_no_frame():
    handle = _log()
    _opened(handle)
    state = _Sockets(watchers=0)
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    publisher._dirty.add(SESSION)
    await publisher._flush()
    assert state.frames == []
    assert handle.last_seq == 1


@pytest.mark.asyncio
async def test_the_publisher_caches_a_bounded_number_of_sessions():
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    for index in range(routes.MAX_CACHED_SESSIONS + 3):
        unit = f"s-many{index}"
        _opened(_log(unit))
        await publisher._publish(unit)
    assert len(publisher._bundles) == routes.MAX_CACHED_SESSIONS


@pytest.mark.asyncio
async def test_a_fold_refusal_does_not_stop_the_other_sessions():
    good = _log("s-good")
    _opened(good)
    broken = _log("s-broken")
    _opened(broken)
    _plant_unknown_required_type(broken)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    publisher._dirty.update({"s-good", "s-broken"})
    await publisher._flush()
    assert {data["session_id"] for _, data in state.frames} == {"s-good"}


def test_notify_from_a_writer_thread_does_no_work_of_its_own():
    """The writer hands the id to the loop; the reading happens there."""
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    loop = MagicMock()
    publisher.bind(loop)
    publisher.notify(SESSION)
    loop.call_soon_threadsafe.assert_called_once()
    assert state.frames == []


def test_notify_before_the_publisher_is_bound_is_a_no_op():
    publisher = routes.CrewLogPublisher(_Sockets())
    publisher.notify(SESSION)
    publisher.notify("")


@pytest.mark.asyncio
async def test_installing_the_publisher_registers_exactly_one_growth_listener(monkeypatch):
    from kiro_crew.crew_log import emit as crew_log_emit

    monkeypatch.setenv(routes.CREW_LOG_ENV, "1")
    with (
        patch.object(routes, "_publisher", None),
        patch.object(crew_log_emit, "_growth_listeners", []),
    ):
        first = routes.install_crew_log_publisher(_Sockets())
        with patch.object(routes, "_publisher", first):
            again = routes.install_crew_log_publisher(_Sockets())
        assert again is first
        assert len(crew_log_emit._growth_listeners) == 1


def test_the_frame_keeps_the_name_the_rfc_gives_it():
    assert routes.FRAME == "session_projection"


def test_this_module_does_not_load_the_storage_package_at_import():
    """The crew log is optional, and this module sits on the dashboard's boot path.

    A clean interpreter is the only place it is observable: this suite has already
    imported the storage package, so an in-process check would read its own
    imports rather than the boot path's.
    """
    probe = (
        "import importlib, json, sys;"
        "importlib.import_module('kiro_crew.dashboard.handlers.crew_log');"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('kiro_crew.crew_log'))))"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [os.path.abspath(sys.executable), "-B", "-c", probe],
        # Inherit the full environment (Windows needs SYSTEMROOT and friends to
        # start the interpreter at all) and layer the probe's own values on top.
        # ``-B`` already stops the child writing bytecode into the checkout, so no
        # env var is relied on for that.
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "KIROCREW_HOME": os.environ.get("KIROCREW_HOME", ""),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []


def test_run_does_not_start_an_overlapping_flush_while_one_is_in_flight():
    """A second scheduled pass must not run concurrently with a slow flush.

    Two overlapping ``_publish`` for one session would share the same ``before``
    bundle and race the cache write, so an older seq could be broadcast last.
    """
    publisher = routes.CrewLogPublisher(_Sockets())
    loop = MagicMock()
    publisher.bind(loop)
    publisher._flushing = True
    publisher._scheduled = True
    publisher._run()
    loop.create_task.assert_not_called()


def test_finished_reschedules_when_work_arrived_mid_flush():
    """A growth marked during a flush is picked up once the pass finishes."""
    publisher = routes.CrewLogPublisher(_Sockets())
    loop = MagicMock()
    publisher.bind(loop)
    publisher._flushing = True
    publisher._dirty.add(SESSION)
    publisher._scheduled = False
    done = MagicMock()
    done.cancelled.return_value = False
    done.exception.return_value = None
    publisher._finished(done)
    assert publisher._flushing is False
    assert publisher._scheduled is True
    loop.call_later.assert_called_once()


def test_a_page_reports_the_tail_it_observed_not_a_stale_cached_one():
    """A page must not tell a client the history ends where its handle thinks.

    ``CrewLog.last_seq`` is the handle's own cached figure and its docstring says it
    is authoritative only for that handle's own appends. A reader never appends, so
    a writer growing the file after the handle opened is invisible to it. The pass
    over the file is live and walks the whole tail, so the real end is observable;
    deriving the metadata from the cached figure instead would return rows up to
    ``to`` and still report that nothing follows, and a client that believes it
    stops paging with entries left unread.
    """
    handle = _log()
    _opened(handle)
    for turn in (1, 2):
        _turn(handle, turn)

    # A handle opened NOW, then a writer that grows the file behind its back.
    stale = crew_log.open_session_log(SESSION)
    assert stale is not None
    cached = stale.last_seq
    writer = CrewLog.open(lg.KIND_SESSION, SESSION)
    for turn in (3, 4, 5):
        _turn(writer, turn)
    assert writer.last_seq > cached
    assert stale.last_seq == cached  # the reader handle never learned

    with patch.object(crew_log, "open_session_log", return_value=stale):
        page = routes._read_page(SESSION, 1, cached)

    # The page stops at the range it was asked for, but it does NOT claim the log
    # ends there: next_from points at the entries the writer added.
    assert page["last_seq"] == writer.last_seq
    assert page["next_from"] == cached + 1
    assert max(row["seq"] for row in page["entries"]) == cached


@pytest.mark.asyncio
async def test_a_recreated_log_at_the_same_seq_still_pushes_its_new_values():
    """A seq is only comparable within one file.

    ``fold_session`` refuses a bundle whose origin does not match the file and
    rebuilds from the start, so a log removed and recreated can come back at the
    same terminal seq carrying entirely different values. Comparing seqs alone
    reads that as nothing having moved and suppresses every frame, leaving each
    client holding the retired file's projection with no later growth able to
    dislodge it.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await publisher._publish(SESSION)
    before = publisher._bundles[SESSION]
    state.frames.clear()

    # Same session id, a DIFFERENT file, folded to the same terminal seq. Handing
    # the publisher that cached bundle is the recreated-log shape without touching
    # the filesystem, so it behaves the same on every platform.
    other = _log("s-recreated-src")
    _opened(other)
    _turn(other, 1)
    fresh = crew_log.fold_session("s-recreated-src", crew_log.PROJECTION_NAMES)
    assert fresh.last_seq == before.last_seq  # seq-only check would suppress
    assert fresh.origin != before.origin
    publisher._bundles[SESSION] = crew_log.SessionProjections(
        session_id=SESSION,
        last_seq=fresh.last_seq,
        checkpoints=fresh.checkpoints,
        origin="a-retired-file",
    )

    await publisher._publish(SESSION)
    assert state.frames, "a rebuilt bundle must push, not be read as unchanged"


@pytest.mark.asyncio
async def test_rebinding_the_publisher_repoints_it_at_the_state_now_serving():
    """A restart inside one process must not keep broadcasting to the retired hub.

    The publisher is a per-process singleton, so a second install returns the same
    object. Rebinding only the loop would leave it counting the old hub's sockets
    and sending every frame to a room nobody is in, which looks exactly like a
    session that quietly stopped updating.
    """
    handle = _log()
    _opened(handle)
    retired = _Sockets()
    publisher = routes.CrewLogPublisher(retired)
    publisher.bind(asyncio.get_running_loop())

    serving = _Sockets()
    publisher.bind(asyncio.get_running_loop(), serving)
    await publisher._publish(SESSION)

    assert serving.frames, "frames must reach the state now serving"
    assert retired.frames == [], "and none must reach the retired one"


@pytest.mark.asyncio
async def test_rebinding_clears_scheduling_flags_left_on_the_retired_loop():
    """A timer armed on a closed loop never fires and a flush there never ends.

    Left set, ``_scheduled`` makes a growth believe a pass is already coming and
    ``_flushing`` makes the runner yield to a pass that does not exist, so the
    publisher would go quiet permanently after a restart. The dirty set is kept:
    those sessions did grow and the next pass folds them forward.
    """
    publisher = routes.CrewLogPublisher(_Sockets())
    publisher.bind(asyncio.get_running_loop())
    publisher._scheduled = True
    publisher._flushing = True
    publisher._dirty.add(SESSION)

    publisher.bind(asyncio.get_running_loop(), _Sockets())

    assert publisher._scheduled is False
    assert publisher._flushing is False
    assert publisher._dirty == {SESSION}


def test_the_flag_name_matches_the_emitters_own_constant():
    """The boot path spells the variable itself, so a test keeps the two in step.

    Importing the emitter to ask whether the crew log is wanted is the cost the
    flag exists to avoid, so the name is spelled in the handler. That is only safe
    while something proves the spelling still matches.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    assert routes.CREW_LOG_ENV == crew_log_emit.CREW_LOG_ENV


@pytest.mark.asyncio
async def test_installing_with_the_flag_off_builds_nothing(monkeypatch):
    """A launch without the flag must not pay for the subsystem it will not use.

    The installer runs on the gateway's boot path. With the crew log off it returns
    without importing the emitter and without constructing a publisher, so a
    disabled launch does no optional work and registers no listener.
    """
    monkeypatch.delenv(routes.CREW_LOG_ENV, raising=False)
    monkeypatch.setattr(routes, "_publisher", None)
    from kiro_crew.crew_log import emit as crew_log_emit

    with patch.object(crew_log_emit, "_growth_listeners", []):
        assert routes.install_crew_log_publisher(_Sockets()) is None
        assert crew_log_emit._growth_listeners == []
    assert routes._publisher is None


# --------------------------------------------------------------------------- #
# The agent's door: the unit-keyed routes and who may walk through them
# --------------------------------------------------------------------------- #


class _Slot:
    """The little of a dashboard slot these routes read."""

    def __init__(
        self,
        *,
        app: str = "",
        restricted: bool = False,
        key: str = "",
        memory_mode: str = "persistent",
        linked_session_key: str = "",
        workspace: str = "default",
    ) -> None:
        self._app = app
        self.workspace = workspace
        self.is_restricted = restricted
        self.linked_session_key = linked_session_key
        # ``key`` is how a slot is matched back to the unit it is writing, and
        # ``memory_mode`` is the field the mirrored incognito test reads --
        # ``is_restricted`` is the owner test's own signal and is not the same field.
        self.key = key
        self.memory_mode = memory_mode


class _State:
    def __init__(self, slots: dict[str, _Slot], sessions: dict[str, str]) -> None:
        self._slots = slots
        self.sessions = _Sessions(sessions)
        self.crons = None
        self.subagents = None


OWNER_KEY = "dashboard:chat-owner"


def _internal_request(
    path: str,
    *,
    caller: str = "kirocrew-crew-log",
    session_key: str = OWNER_KEY,
    secret: bool = True,
    slots: dict[str, _Slot] | None = None,
    sessions: dict[str, str] | None = None,
    match: dict[str, str] | None = None,
) -> object:
    """A request as the MCP proxy makes it: internal secret + caller + session key.

    ``secret=False`` is the shape a COOKIE-authenticated caller arrives in. The
    middleware admits one on loopback (it falls through to cookie auth when the
    header is absent, strict bucket or not), so the handler sees it and has to
    refuse it itself.
    """
    headers = {"X-Internal-Secret": "s3cret"} if secret else {}
    if caller:
        headers["X-Internal-Caller"] = caller
    if session_key:
        headers["X-Session-Key"] = session_key
    # A REAL application, not ``make_mocked_request``'s MagicMock default: these
    # handlers read ``request.app.get("state")``, and on the mock that answers a
    # fresh MagicMock -- every attribute of which is truthy, so the app-ownership
    # check would "find" an owning app for the person and the whole gate would be
    # tested against a fiction.
    app = web.Application()
    app["state"] = _State(
        {"chat-owner": _Slot()} if slots is None else slots,
        {OWNER_KEY: SESSION} if sessions is None else sessions,
    )
    request = make_mocked_request("GET", path, headers=headers, app=app)
    for key, value in (match or {}).items():
        request.match_info[key] = value
    return request


def _flag_on(monkeypatch) -> None:
    monkeypatch.setenv(routes.CREW_LOG_ENV, "1")


def test_the_caller_name_is_pinned_to_the_mcp_servers_own(monkeypatch):
    """Two modules name one component; a rename on one side must fail here."""
    from kiro_crew.mcp_crew_log import SERVER_NAME

    assert routes.CREW_LOG_MCP_CALLER == SERVER_NAME


def test_the_enable_hint_names_the_real_flag():
    assert routes.CREW_LOG_ENV in routes.CREW_LOG_ENABLE_HINT


def test_the_enable_hint_names_the_live_data_home_not_the_legacy_one():
    """An agent is told to edit this file, so naming the wrong one wastes the turn.

    The live credentials file is ``~/.kiro/crew/.env`` (``config/loader.py``'s own
    header, and ``config_dir()`` under the default home). ``~/.kirocrew/.env`` is a
    legacy location that ``sandbox.py`` keeps only to fence a leftover copy;
    nothing reads configuration from it. A hint naming it sends the reader to an
    inert file, and the flag appears not to work.

    Not compared against ``config_dir()``: the suite's isolation fixture overrides
    the home, so that call answers a ``tmp_path`` here and would pass on either
    string.
    """
    assert ".kiro/crew/.env" in routes.CREW_LOG_ENABLE_HINT
    assert ".kirocrew/" not in routes.CREW_LOG_ENABLE_HINT


def test_the_page_reader_is_the_one_shared_implementation():
    """One implementation, so the browser door and the agent door cannot differ."""
    from kiro_crew.crew_log import read as shared

    assert routes.MAX_PAGE_REFS == shared.MAX_PAGE_REFS
    assert routes._read_page.__module__ == routes.__name__
    with patch.object(shared, "read_page", return_value={"exists": False}) as called:
        routes._read_page(SESSION, 1, 2)
    called.assert_called_once_with(SESSION, 1, 2)


def test_a_read_with_the_flag_off_says_how_to_switch_it_on(monkeypatch):
    """MUTATION-SENSITIVE: the agent learns the flag state from THIS refusal."""
    monkeypatch.delenv(routes.CREW_LOG_ENV, raising=False)
    request = _internal_request("/api/crew-log/sessions")
    response = asyncio.run(routes.api_crew_log_sessions(request))
    assert response.status == 422
    body = json.loads(response.text)
    assert body["code"] == "crew_log_disabled"
    assert routes.CREW_LOG_ENV in body["error"]


#: A conductor, its child and its grandchild, plus a session in another tree. The
#: keys are deliberately NOT ``dashboard:`` so the owner arm cannot answer for them
#: and the lineage arm is what is under test. The SLOT half of the conductor's key is
#: what the fence matches, because the recorded edge names a slot.
CONDUCTOR_KEY = "subagent:conductor"
CONDUCTOR_SLOT = "conductor"
CONDUCTOR_UNIT = "s-conductor"
CHILD_SLOT = "chat-child-1789000000"
CHILD_UNIT = "s-child"
GRANDCHILD_SLOT = "chat-grandchild-1789000001"
GRANDCHILD_UNIT = "s-grandchild"
STRANGER_SLOT = "chat-stranger-1789000002"
STRANGER_UNIT = "s-stranger"


@pytest.fixture(autouse=True)
def _fresh_dispatch_scan():
    """Drop the read side's per-unit caches between tests.

    Two of them, both module-level so they survive a request, which is what keeps a
    scope test one ``stat`` per untouched unit and one fold per grown log. Each is
    validated against the log's own identity, so a new data home would invalidate it
    anyway -- clearing them outright means these tests do not depend on that
    validation being right, which is a property with its own tests.
    """
    from kiro_crew.crew_log import read as crew_log_read

    crew_log_read._TREE._heads.clear()
    crew_log_read.reset_class_bundles()
    yield
    crew_log_read._TREE._heads.clear()
    crew_log_read.reset_class_bundles()


def _dispatch_tree(**child_class) -> None:
    """Four units: conductor -> child -> grandchild, and a stranger under nobody.

    The edges are recorded by SLOT, which is what the fence walks; each entry also
    carries the creator's unit as ``sid``, the way the emitter writes it, so a test
    that keys on the slot cannot pass merely because the sid happens to agree.
    ``child_class`` sets the child's own recorded class.
    """
    _opened(_log(CONDUCTOR_UNIT, slot=CONDUCTOR_SLOT), slot=CONDUCTOR_SLOT)
    _opened(
        _log(CHILD_UNIT, slot=CHILD_SLOT),
        slot=CHILD_SLOT,
        parent_slot=CONDUCTOR_SLOT,
        parent_sid=CONDUCTOR_UNIT,
        **child_class,
    )
    _opened(
        _log(GRANDCHILD_UNIT, slot=GRANDCHILD_SLOT),
        slot=GRANDCHILD_SLOT,
        parent_slot=CHILD_SLOT,
        parent_sid=CHILD_UNIT,
    )
    _opened(
        _log(STRANGER_UNIT, slot=STRANGER_SLOT),
        slot=STRANGER_SLOT,
        parent_slot="somebody-else",
        parent_sid="s-somebody-else",
    )


def _as_conductor(path: str, **kwargs) -> object:
    """A request from the conductor session, which is not the owner's own tab."""
    kwargs.setdefault("session_key", CONDUCTOR_KEY)
    kwargs.setdefault("slots", {"conductor": _Slot()})
    kwargs.setdefault("sessions", {CONDUCTOR_KEY: CONDUCTOR_UNIT})
    return _internal_request(path, **kwargs)


class TestTheDispatchFence:
    """A strict session identity, and then a scope: own unit, or one it dispatched.

    The entitlement is derived from the recorded ``session/opened`` creator edge, so
    a conductor reaches its child and its grandchild while a session in another tree
    reaches neither. The owner at a dashboard tab still reaches everything, which is
    the case this module shipped with.

    The caller classes ``session_control.authorize_target`` refuses are refused here
    too, and for its reasons rather than for a general distrust of headless callers:
    each one is about where a read LANDS.
    """

    def test_a_conductor_reads_the_unit_it_dispatched(self, monkeypatch):
        """MUTATION-SENSITIVE: the lineage arm itself.

        The conductor is not the owner's tab and the child is not its own unit, so
        nothing but the recorded creator edge can admit this read.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_conductor_reads_a_descendant_forty_generations_down(self, monkeypatch):
        """MUTATION-SENSITIVE: the walk is bounded by the tree, not by a number.

        The repeat check is the whole bound, and it ends the walk on every input: each
        pass either returns or adds a slot to ``seen``, and ``seen`` draws only from a
        finite node map. A fixed depth cap on top of that buys no termination safety and
        costs correctness -- it answers "no creator above this" for an honest chain
        longer than itself, refusing a grant the tree supports. This chain is 40 deep and
        the top reads the bottom, so adding any fixed cap below 40 reddens it.
        """
        _flag_on(monkeypatch)
        depth = 40
        slots = [f"chat-deep-{i}" for i in range(depth)]
        units = [f"s-deep-{i}" for i in range(depth)]
        _opened(_log(units[0], slot=slots[0]), slot=slots[0])
        for i in range(1, depth):
            _opened(
                _log(units[i], slot=slots[i]),
                slot=slots[i],
                parent_slot=slots[i - 1],
                parent_sid=units[i - 1],
            )
        request = _internal_request(
            f"/api/crew-log/units/{units[-1]}/page",
            match={"unit": units[-1]},
            session_key=f"subagent:{slots[0]}",
            slots={slots[0]: _Slot()},
            sessions={f"subagent:{slots[0]}": units[0]},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200, response.text
        assert json.loads(response.text)["session_id"] == units[-1]

    def test_a_conductor_reads_a_grandchild(self, monkeypatch):
        """MUTATION-SENSITIVE: the walk is transitive, not one generation.

        The grandchild's edge names the CHILD, so admitting this needs the chain to
        be followed past its first link.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{GRANDCHILD_UNIT}/page", match={"unit": GRANDCHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == GRANDCHILD_UNIT

    def test_a_conductor_keeps_its_children_across_a_gateway_restart(self, monkeypatch):
        """MUTATION-SENSITIVE: the fence keys on the recorded SLOT, not on the sid.

        A gateway restart gives the same tab a NEW ACP session id, so the conductor's
        work lands in a new unit while its slot is unchanged. The child's entry still
        names the OLD unit as ``sid``, which is correct -- that is the log that held
        the creating call -- and a sid-keyed fence would therefore lock the
        re-attached conductor out of a child it dispatched minutes earlier. Only the
        slot survives the restart, so admitting this read requires keying on it.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        # The same slot, a new session: a second log for the conductor's tab, whose
        # own entry cites no creator because the mint witness died with the process.
        reattached = "s-conductor-after-restart"
        _opened(_log(reattached, slot=CONDUCTOR_SLOT), slot=CONDUCTOR_SLOT)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            sessions={CONDUCTOR_KEY: reattached},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_target_whose_log_records_no_class_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the fail-closed half of the recorded-class test.

        A log opened before the class was recorded cannot say whether its session was
        app-owned, incognito or published to a channel, and a live lookup cannot
        answer for it either once that session has closed. Admitting it would put
        every pre-existing unit back inside the fence, so the absence REFUSES and the
        refusal names the missing record. The caller here really did dispatch the
        child, so nothing but this test can refuse the read.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(record_class=False)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not record what kind of session it is" in json.loads(response.text)["error"]

    def test_an_unreadable_middle_log_refuses_as_unknown_lineage_not_out_of_scope(
        self, monkeypatch, caplog
    ):
        """MUTATION-SENSITIVE: the scan's fault bit reaches the refusal SITE, not the caller.

        The conductor really did dispatch the grandchild, transitively, and the
        grandchild's own log is intact -- nothing about this request is out of scope.
        What breaks the chain is the CHILD in the middle: its announce record is
        present and cannot be read as an entry, so the scan drops that unit's record
        and the walk finds no node for its slot. ``dispatched_by`` answers False for a
        missing node whatever put it there, so the only thing that can tell a real
        absence of lineage from an edge the scan never read is the flag the reading
        carries.

        What the caller is told is DELIBERATELY the same either way. This door passes
        the requested unit as the scan's ``preferred``, and a named unit's own read
        fault feeds the scan's flag, so a caller-visible distinction would let a
        guessed id that exists-and-is-unreadable be told apart from one that does not
        exist -- an existence oracle on the boundary this fence exists to keep closed.
        So the assertion is on the OPERATOR's record: the refusal site says the scan
        was incomplete, and the wire says only "out of scope".

        Reverting ``dispatch_view`` to ``SessionTree.records()`` reddens it: that
        accessor drops the fault bit, ``view.incomplete`` is then False, and nothing
        is logged -- the refusal goes out with no account of why the tree was short.

        The damage is planted as BYTES rather than monkeypatched, because this is the
        non-transient case: a log is append-only, so a line that cannot be read as an
        entry stays unreadable for the life of the file, and the scanner caches that
        verdict. A patched read would exercise the transient case and leave the one
        that never clears unpinned.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        child_log = lg.crew_log_path(lg.KIND_SESSION, CHILD_UNIT)
        lines = child_log.read_bytes().split(b"\n")
        # Line 1 is the header and stays intact, so the unit is still PROVABLE and is
        # not simply skipped as a stray directory; line 2 is the announce record the
        # creator edge lives on. It is replaced with a well-formed JSON object that is
        # not an entry, which is what makes the head read "announced, unreadable"
        # rather than "not announced yet".
        assert len(lines) >= 2, lines
        lines[1] = b'{"not": "an entry"}'
        child_log.write_bytes(b"\n".join(lines))
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read._TREE._heads.clear()

        request = _as_conductor(
            f"/api/crew-log/units/{GRANDCHILD_UNIT}/page", match={"unit": GRANDCHILD_UNIT}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.crew_log"):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        # The wire carries the ordinary refusal and nothing that distinguishes this
        # case, which is the security half of the change.
        assert "does not fall inside that scope" in json.loads(response.text)["error"]
        # The operator's half: the fault bit reached the refusal site and was recorded
        # there. This is what fails when the view stops carrying it.
        assert any("lineage scan was INCOMPLETE" in record.message for record in caplog.records)

    def test_an_intact_store_logs_no_incomplete_scan_when_it_refuses(self, monkeypatch, caplog):
        """The allow-direction control: the operator signal discriminates.

        Without it, a refusal site that logged "INCOMPLETE" on every denial would
        satisfy the assertion above while saying nothing true -- the same shape as a
        guard verified only in its blocking direction. Nothing is damaged here, so the
        scan is complete, a session in another tree is still refused, and no
        incomplete-scan record is written.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{STRANGER_UNIT}/page", match={"unit": STRANGER_UNIT}
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.crew_log"):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not fall inside that scope" in json.loads(response.text)["error"]
        assert not any("lineage scan was INCOMPLETE" in r.message for r in caplog.records)

    def test_the_refusal_text_is_identical_whether_the_scan_was_short(self, monkeypatch):
        """MUTATION-SENSITIVE: no existence oracle at this door.

        The two cases above differ in exactly the way a caller could steer -- this
        door passes the requested unit as the scan's ``preferred``, so a guessed id
        that exists and cannot be read sets the flag while a guessed id that does not
        exist does not. Re-introducing any caller-visible difference between them
        reddens this: it compares the two bodies for equality rather than asserting a
        phrase, so it catches a new wording however it is spelled.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        intact = _as_conductor(
            f"/api/crew-log/units/{STRANGER_UNIT}/page", match={"unit": STRANGER_UNIT}
        )
        intact_body = json.loads(asyncio.run(routes.api_crew_log_unit_page(intact)).text)["error"]

        child_log = lg.crew_log_path(lg.KIND_SESSION, CHILD_UNIT)
        lines = child_log.read_bytes().split(b"\n")
        lines[1] = b'{"not": "an entry"}'
        child_log.write_bytes(b"\n".join(lines))
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read._TREE._heads.clear()

        short = _as_conductor(
            f"/api/crew-log/units/{GRANDCHILD_UNIT}/page", match={"unit": GRANDCHILD_UNIT}
        )
        short_body = json.loads(asyncio.run(routes.api_crew_log_unit_page(short)).text)["error"]
        assert short_body == intact_body

    def test_a_listing_only_lineage_fault_is_recorded_by_the_producer_once(
        self, monkeypatch, caplog
    ):
        """MUTATION-SENSITIVE: the listing consumer gets an operator record too.

        A caller that only LISTS never reaches the per-unit refusal door, so the
        warning that door writes cannot account for the ``truncated`` it is handed.
        And this fault never clears: a log is append-only, so the scanner caches the
        verdict and every later reading is short for the life of the file. With no
        record at the producer, such a caller sees ``truncated`` forever while nothing
        anywhere says why -- the same failure indistinguishable to an operator that
        this change exists to remove, one consumer over.

        So the record is asserted at the layer that knows both the cause and WHICH
        unit carries it, and asserted to fire ONCE across two scans. That second half
        is the discriminating one: the cache serves every later scan without
        re-judging, so a warning placed where the cache is READ would satisfy a
        presence check while writing one line per scan for the life of the log.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        child_log = lg.crew_log_path(lg.KIND_SESSION, CHILD_UNIT)
        lines = child_log.read_bytes().split(b"\n")
        assert len(lines) >= 2, lines
        lines[1] = b'{"not": "an entry"}'
        child_log.write_bytes(b"\n".join(lines))
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read._TREE._heads.clear()

        # The scope_slot is load-bearing: ``list_session_units`` builds its dispatch
        # view only when a slot is in scope, so an unscoped owner listing never
        # consults lineage and cannot reach this fault at all. The consumer the gap
        # belongs to is a conductor listing what it dispatched.
        with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.session_tree"):
            first = crew_log_read.list_session_units(limit=50, scope_slot=CONDUCTOR_SLOT)
            second = crew_log_read.list_session_units(limit=50, scope_slot=CONDUCTOR_SLOT)

        # The listing still tells its caller the set is short, exactly as before.
        assert first["truncated"] is True
        assert second["truncated"] is True
        # The producer accounted for it, naming the unit whose bytes carry the fault.
        judged = [r for r in caplog.records if "creator edge is unknown" in r.message]
        assert len(judged) == 1, [r.message for r in judged]
        unit_name = child_log.parent.name
        assert unit_name, "the fixture resolved no unit directory name"
        assert unit_name in judged[0].message

    @pytest.mark.parametrize(
        ("recorded", "word"),
        [
            ({"channel": True}, "published to a channel"),
            ({"app": "travel-desk"}, "app-scoped"),
            ({"memory": "incognito"}, "incognito"),
        ],
        ids=["channel", "app", "incognito"],
    )
    def test_a_closed_targets_class_is_read_from_its_own_log(self, monkeypatch, recorded, word):
        """MUTATION-SENSITIVE: the recorded class decides a target with no live slot.

        Nothing is writing the child's unit, which is the ordinary state of a
        finished run, so the live target test has nothing to exclude on. The class is
        on the child's own opening entry, which is the whole reason it is recorded
        there, and each of these three closes a boundary a live-only test would miss.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(**recorded)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert word in json.loads(response.text)["error"]

    def test_a_class_acquired_after_the_log_opened_refuses_a_closed_target(self, monkeypatch):
        """MUTATION-SENSITIVE: the whole reason the class history is folded, not read.

        The child opens as an ordinary session and is given a channel surface later,
        so the OPENING entry truthfully records no channel while the turns after it
        carry a third party's words. Reading the opener alone admits this read; the
        fold holds ``channel`` at the most restrictive value the log ever stated, so it
        refuses. Nothing else in this suite catches it: the target has no live slot,
        which is the ordinary state of a finished run.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, channel=True)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "published to a channel" in json.loads(response.text)["error"]

    def test_a_class_that_moved_back_stays_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the fold keeps the most restrictive value EVER held.

        The link is dropped again, so the newest statement is clean -- and the turns
        that ran while it was published are still in this log. A fold that took the
        LATEST value would hand those words to the dispatcher, so taking the most
        restrictive one is the honest reading rather than a conservative default.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, channel=True)
        _class_moved(CHILD_UNIT)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "published to a channel" in json.loads(response.text)["error"]

    def test_a_class_history_with_no_beginning_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: a fold that saw only moves cannot say what it moved FROM.

        Retention can take the segment carrying the opening entry, leaving transitions
        whose earliest class is unknown. The surviving part states a clean class, so a
        reader that trusted it would admit on a log whose opener may have been
        app-owned. ``complete`` is what refuses, and it doubles as the log's date: the
        opening ``class`` object and this transition were declared together, so a log
        stating the first was written by a build that records the second.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(record_class=False)
        _class_moved(CHILD_UNIT)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "not the class it started from" in json.loads(response.text)["error"]

    def test_a_closed_target_whose_class_never_moved_is_still_read(self, monkeypatch):
        """The admitting direction, which is the feature this whole fence exists for.

        An affirmatively clean history is an opening class plus no transition away
        from it. Without this the three refusal tests above are satisfied by a gate
        that refuses every closed target, which is what the ruling this implements
        deliberately did not do.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, memory="persistent")
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_class_fold_that_stopped_short_of_the_file_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: a partial fold cannot answer a question about the whole log.

        ``iter_from`` stops at an entry type it does not know, which a log written by a
        newer build can carry. Stopping is safe for a fold that accumulates totals, and
        unsafe for this one: a restrictive move recorded AFTER the unknown entry would
        simply be unseen, so the fold would report the log as more readable than it is.
        The fold is shortened here rather than a newer entry planted, because the
        reader's rule is about the seq it reached, whatever stopped it.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        from kiro_crew.crew_log import read as crew_log_read

        real = crew_log_read.projections.fold_session

        def _short(session_id, names, **kwargs):
            bundle = real(session_id, names, **kwargs)
            return type(bundle)(
                session_id=bundle.session_id,
                last_seq=max(0, bundle.last_seq - 1),
                checkpoints=bundle.checkpoints,
                origin=bundle.origin,
            )

        monkeypatch.setattr(crew_log_read.projections, "fold_session", _short)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not record what kind of session it is" in json.loads(response.text)["error"]

    def test_a_target_whose_class_moves_mid_read_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check RE-FOLDS the target's class.

        The target is published to a channel while the payload is being built, takes a
        channel turn, and is unlinked again. At the grant it was clean and at the answer
        its live slot is clean again, so both live reads pass -- and the words of that
        channel turn are in the payload. Only a fold taken AFTER the read sees the move,
        and because the fold is held at the most restrictive value the log ever recorded
        it stays refused rather than following the slot back to clean.

        This is why reusing the grant's fold is not an optimisation: the two answers
        differ exactly in the case the guard exists for.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CHILD_UNIT, channel=True)
            _class_moved(CHILD_UNIT)
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "published to a channel" in json.loads(response.text)["error"]

    def test_a_target_whose_class_holds_still_mid_read_is_still_answered(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-fold is a TEST, not a second refusal.

        The same hook runs and appends a move that changes nothing restrictive. Without
        this case a re-fold that refused whenever the log grew during the read would
        pass the test above while breaking every read of a session still working, which
        is most of them.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CHILD_UNIT, memory="persistent")
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_target_whose_log_lost_its_front_segment_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: a log that does not hold seq 1 cannot be dated.

        Retention deletes whole segments off the FRONT, so a surviving log can begin
        part-way through its own life. This log's own first opener recorded no class,
        and the survivor is a RE-ATTACHMENT's opener, which does -- so within the file
        that remains the stating opener genuinely is the first one the fold sees, and
        the projection's own first-opener rule cannot help. Only the store can say the
        file starts past seq 1, and the first seq is in each segment's NAME, so the
        check costs a directory listing rather than a read.

        The re-attachment carries the dispatch edge too, which is what keeps this in
        scope and makes the CLASS the reason it is refused -- a trim that took the edge
        as well would be refused for being out of scope and prove nothing about dating.

        The trimmed state is built on disk rather than by calling retention, because no
        writer rotates yet: the surviving segment is named for the seq it starts at and
        carries exactly the entries from that seq on, which is the layout a front trim
        leaves.
        """
        _flag_on(monkeypatch)
        _dispatch_tree(record_class=False)
        child = CrewLog.open(lg.KIND_SESSION, CHILD_UNIT)
        _turn(child, 1)
        _opened(child, slot=CHILD_SLOT, parent_slot=CONDUCTOR_SLOT, parent_sid=CONDUCTOR_UNIT)

        directory = lg.crew_log_dir(lg.KIND_SESSION, CHILD_UNIT)
        head = directory / "log.jsonl"
        lines = head.read_text(encoding="utf-8").splitlines(keepends=True)
        # Line 0 is the log HEADER, not an entry, so the openers are found by type
        # rather than by position and the RE-ATTACHMENT is the second of them.
        openers = [
            i for i, line in enumerate(lines) if json.loads(line).get("type") == "session/opened"
        ]
        assert len(openers) == 2, f"expected an original opener and a re-attachment, got {openers}"
        # Every segment carries a copy of the log HEADER as its own line 1, which is
        # what lets a reader open one without the segment before it -- and what keeps
        # the dispatch edge readable here, since the tree reads line 2 of the oldest
        # surviving segment.
        kept = [lines[0]] + lines[openers[1] :]
        first_kept = json.loads(kept[1])["seq"]
        assert first_kept > 1, "a trim that keeps seq 1 is not a trim"
        (directory / f"log.{first_kept}.jsonl").write_text("".join(kept), encoding="utf-8")
        head.unlink()
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read.reset_class_bundles()

        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        error = json.loads(response.text)["error"]
        # The retention case gets its OWN text: the remedy differs, because nothing
        # about this log will satisfy the read again, where an unrecorded class is
        # fixed by the session recording one. Asserting the generic wording here
        # would pass whether or not the two are told apart.
        assert "no longer holds its own beginning" in error, error
        assert "does not record what kind of session" not in error, error

    def test_a_bare_session_key_is_read_as_its_own_slot(self, monkeypatch):
        """MUTATION-SENSITIVE: a key with no colon is a bare SLOT name, not a broken key.

        The crew log's own resolver states the premise and acts on it -- a key carrying
        no colon cannot already be namespaced, since every namespace spelling carries
        one -- so it retries a bare key in ``dashboard:`` form. Reading its slot half by
        splitting on the colon and taking the SECOND part instead raises ``IndexError``
        on exactly that key, which reaches the caller as a 500 on an ordinary read.

        Resolving it as its own slot is not a widening: the class tests and the lineage
        walk all still run on that slot, and the own-unit resolution already handled
        this key shape, so refusing it here was the two halves disagreeing about who the
        caller is rather than a boundary.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _internal_request(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            session_key=CONDUCTOR_SLOT,
            slots={CONDUCTOR_SLOT: _Slot()},
            sessions={CONDUCTOR_SLOT: CONDUCTOR_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200, response.text
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_target_whose_log_has_a_damaged_line_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: byte damage must not raise an authorization ceiling.

        The restriction is written, then that line's bytes are destroyed while the line
        COUNT and the tail seq are left alone. The store skips an unreadable interior
        line on purpose and does not check interior seq continuity, so the fold still
        reaches the file's tail: nothing stopped early, the short-fold refusal cannot
        fire, and without the contiguity check the fold answers with the clean opening
        class and the dispatcher receives the channel turn's message bodies.

        The damage is planted rather than simulated because that is the only way to
        reach the skipped-line path: no writer produces a line the reader cannot parse.
        It is planted in the INTERIOR because the tail is a different case -- a damaged
        LAST line lowers the tail seq too, so the fold and that seq agree, and nothing
        can tell it from an append in flight. That case carries no exposure: a class is
        recorded at the START of the turn that runs under it, so a move with nothing
        after it is a move no turn has run under yet.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, channel=True)
        child = CrewLog.open(lg.KIND_SESSION, CHILD_UNIT)
        _turn(child, 1)
        head = lg.crew_log_dir(lg.KIND_SESSION, CHILD_UNIT) / "log.jsonl"
        lines = head.read_text(encoding="utf-8").splitlines(keepends=True)
        moves = [i for i, ln in enumerate(lines) if json.loads(ln).get("type") == "session/class"]
        assert len(moves) == 1, f"expected one move to damage, got {moves}"
        lines[moves[0]] = "{not json at all\n"
        head.write_text("".join(lines), encoding="utf-8")
        from kiro_crew.crew_log import read as crew_log_read

        crew_log_read.reset_class_bundles()

        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403, response.text
        error = json.loads(response.text)["error"]
        assert "does not record what kind of session it is" in error, error

    def test_an_out_of_tree_target_is_refused_without_naming_its_class(self, monkeypatch):
        """The scope refusal comes FIRST, so a class is not readable one refusal at a time.

        The stranger's log records no class here, so a gate that tested the class
        before the scope would answer with the class refusal and let any caller learn
        something about a unit it may not read at all.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _opened(_log("s-lonely", slot="chat-lonely"), slot="chat-lonely", record_class=False)
        request = _as_conductor("/api/crew-log/units/s-lonely/page", match={"unit": "s-lonely"})
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        error = json.loads(response.text)["error"]
        assert "does not fall inside that scope" in error
        assert "kind of session" not in error

    def test_a_mirrored_owner_tab_reads_only_its_own_unit(self, monkeypatch):
        """MUTATION-SENSITIVE: the owner arm is held to the caller classes too.

        An owner's own tab publishes to the person at it, but a dashboard session
        MIRRORED to a channel republishes every turn -- so the one caller entitled to
        read every unit would otherwise be the one that publishes them. It keeps its
        own unit, like every other excluded caller.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        owner_unit = "s-owner"
        _opened(_log(owner_unit, slot="chat-owner"), slot="chat-owner")
        slots = {"chat-owner": _Slot(key=OWNER_KEY, linked_session_key="slack:C500")}
        sessions = {OWNER_KEY: owner_unit}
        peer = _internal_request(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots=slots,
            sessions=sessions,
        )
        response = asyncio.run(routes.api_crew_log_unit_page(peer))
        assert response.status == 403
        assert "channel" in json.loads(response.text)["error"]
        own = _internal_request(
            f"/api/crew-log/units/{owner_unit}/page",
            match={"unit": owner_unit},
            slots=slots,
            sessions=sessions,
        )
        assert asyncio.run(routes.api_crew_log_unit_page(own)).status == 200

    def test_a_sibling_cannot_read_a_sibling(self, monkeypatch):
        """MUTATION-SENSITIVE: the fence, from the side it exists to hold.

        The stranger's tree does not contain the conductor, so a walk that ran to
        the end of the chain still must not admit it. A gate that admitted every
        identified caller would pass this read.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{STRANGER_UNIT}/page", match={"unit": STRANGER_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert "does not fall inside that scope" in body["error"]

    def test_the_lineage_walk_runs_off_the_event_loop(self, monkeypatch):
        """MUTATION-SENSITIVE: the fold is the only arm that touches the filesystem.

        It lists the store and stats every unit, so running it inline would put a
        real filesystem scan on the loop for every cross-unit request. Asserting that
        no loop is RUNNING where the fold executes is what distinguishes an offloaded
        scan from an inline one; the verdict is asserted too, so a scan that never ran
        cannot pass this.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        from kiro_crew.crew_log import read as crew_log_read

        real = crew_log_read.dispatch_view
        seen: dict[str, bool] = {}

        def _watching(preferred=()):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen["on_loop"] = False
            else:
                seen["on_loop"] = True
            return real(preferred)

        monkeypatch.setattr(crew_log_read, "dispatch_view", _watching)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert seen == {"on_loop": False}

    def test_a_malformed_unit_is_refused_rather_than_crashing(self, monkeypatch):
        """MUTATION-SENSITIVE: the lineage walk's own error containment.

        The unit id is whatever a request named, and the store rejects some spellings
        outright. A walk that let that escape would turn an unauthorized read into a
        server error -- no verdict, and an audit row that records neither outcome. The
        walk answers "no creator recorded" instead, which the fence refuses.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        bad = "bad\\id"
        request = _as_conductor(f"/api/crew-log/units/{bad}/page", match={"unit": bad})
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert json.loads(response.text)["code"] == "forbidden"

    def test_a_session_reads_its_own_unit(self, monkeypatch):
        """No slot lookup and no walk: the cheapest arm, and the one every caller has."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page", match={"unit": CONDUCTOR_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200

    def test_a_channel_linked_caller_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the mirrored caller-class exclusion.

        A linked session's own conversation is a channel thread, so a peer's crew
        log it reads is published to whoever is in that channel. This is the class
        the "sessions belong to one operator" premise does not reach, and it is
        refused on the child it really did dispatch.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={"conductor": _Slot(linked_session_key="slack:C123")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "channel" in json.loads(response.text)["error"]

    def test_a_channel_linked_caller_still_reads_its_own_unit(self, monkeypatch):
        """The exclusion is about reading PAST its own record, not about reading.

        Without this the mirrored exclusions would be indistinguishable from a
        blanket refusal of the class, which is a different and wider rule.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page",
            match={"unit": CONDUCTOR_UNIT},
            slots={"conductor": _Slot(linked_session_key="slack:C123")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200

    @pytest.mark.parametrize("prefix", ["cron", "taskrunner"], ids=["cron", "taskrunner"])
    def test_an_unattended_caller_cannot_read_a_peer(self, monkeypatch, prefix):
        """A scheduled run has no operator watching what it did with the content."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        key = f"{prefix}:nightly"
        request = _internal_request(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            session_key=key,
            slots={"nightly": _Slot()},
            sessions={key: CONDUCTOR_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "unattended" in json.loads(response.text)["error"]

    def test_an_app_owned_caller_cannot_read_a_peer(self, monkeypatch):
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={"conductor": _Slot(app="travel-desk")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "app-owned" in json.loads(response.text)["error"]

    def test_an_incognito_caller_cannot_read_a_peer(self, monkeypatch):
        """That session was created to leave and learn nothing."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={"conductor": _Slot(memory_mode="incognito")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "incognito" in json.loads(response.text)["error"]

    def test_a_caller_the_slot_table_cannot_place_reads_only_its_own(self, monkeypatch):
        """A caller with no live slot cannot be placed in a dispatch tree.

        It keeps its own unit, because that is resolved from the session manager
        rather than from the slot table.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        peer = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}, slots={}
        )
        assert asyncio.run(routes.api_crew_log_unit_page(peer)).status == 403
        own = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page",
            match={"unit": CONDUCTOR_UNIT},
            slots={},
        )
        assert asyncio.run(routes.api_crew_log_unit_page(own)).status == 200

    def test_a_live_channel_linked_target_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the mirrored TARGET-class exclusion.

        A child minted by ``session_create`` is unlinked, so this is the case where
        it was linked AFTERWARDS -- read from its live slot, which is why the slot
        is matched to the unit it is currently writing.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        child_key = "dashboard:chat-child"
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={
                "conductor": _Slot(),
                "chat-child": _Slot(key=child_key, linked_session_key="slack:C999"),
            },
            sessions={CONDUCTOR_KEY: CONDUCTOR_UNIT, child_key: CHILD_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "channel" in json.loads(response.text)["error"]

    def test_a_closed_target_is_read_by_its_dispatcher(self, monkeypatch):
        """The whole point: a FINISHED child's recorded log.

        No live slot is writing the child's unit here, which is the ordinary state
        of a completed run. ``authorize_target`` answers 404 for that, and this door
        deliberately does not.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        assert asyncio.run(routes.api_crew_log_unit_page(request)).status == 200

    def test_a_caller_with_no_session_key_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the identity requirement, ahead of every scope arm.

        An unnamed caller is refused because the audit row for a read is only worth
        keeping if it names the session that made it.
        """
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions", session_key="")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert "session identity" in body["error"]

    def test_a_request_naming_another_component_is_refused(self, monkeypatch):
        """The route serves ONE internal caller; the header is validated, not trusted."""
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/sessions", caller="kirocrew-dashboard")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert routes.CREW_LOG_MCP_CALLER in json.loads(response.text)["error"]

    def test_the_owner_at_a_dashboard_tab_is_admitted(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        assert json.loads(response.text)["kind"] == "session"


class TestTheGrantIsRecheckedAfterTheRead:
    """A verdict about the TARGET's class must still hold when the payload returns.

    Each unit route authorizes, then reads the entries off the loop. That offload is a
    suspension point, so the target can append a class move while it runs -- and the
    payload would then be a one-shot delivery of entries the refreshed class governs.
    The caller's class is NOT re-tested here; where a caller's turn lands is decided
    after this route answers, so such a test could not buy an invariant. It is tested
    at the gate, which is where it decides whether the read happens at all.
    """

    def _links_during(self, monkeypatch, slot, name: str = "_read_page"):
        """Make the offloaded read acquire a channel link as its side effect.

        The link lands while the read is in flight, which is the only moment this
        guard exists for: before it, the gate refuses; after it, the payload is
        already out.
        """
        real = getattr(routes, name)

        def _linking(*args, **kwargs):
            slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, name, _linking)

    def test_the_owner_arm_keeps_reading_a_target_the_lineage_arm_would_refuse(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check re-runs the tests the GRANT ran, no others.

        The owner at a dashboard tab reads any unit, which is this door's recorded
        product decision, so that arm runs no target test at all. This target records
        no class, which the lineage arm refuses on -- so a re-check that ran the target
        test unconditionally would withdraw a grant the gate correctly gave, and turn
        the owner's ordinary read of an old unit into a refusal.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-old", slot="chat-old"), slot="chat-old", record_class=False)
        request = _internal_request("/api/crew-log/units/s-old/page", match={"unit": "s-old"})
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == "s-old"

    def test_a_payload_carrying_no_record_of_its_grant_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check fails closed on a mark it cannot read.

        Every arm that BUILDS a payload leaves a mark saying which target test its
        grant ran. A request reaching the re-check without one is the gate and this
        route having stopped agreeing, and the live test must not be run alone there:
        for a target that HAS a live slot it would pass, and the recorded test would
        never have been applied at all. So the absence refuses rather than falling
        through to the weaker half.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        assert routes.TARGET_CLASS_KEY not in request, "the gate has not run yet"
        response = asyncio.run(
            routes._stale_grant_refusal(request, "session_crew_log.read", unit=CHILD_UNIT)
        )
        assert response is not None
        assert "cannot be checked against the grant" in json.loads(response.text)["error"]

    def test_a_caller_that_links_to_a_channel_mid_read_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the caller half of the re-check on the page route.

        This is the scenario in full: a prompt to paste a dispatched child's log, a
        mirror bound while the payload read is suspended, and a reply that would
        publish the private log to the channel. The gate cannot see it -- at the moment
        it ran, the caller was unlinked and entitled.

        What this buys is exact and no wider: the route does not hand another session's
        content to a caller that is publishing AT THE HANDOFF, which is the act the
        route performs and controls. A caller that links AFTER the payload returns
        holds it in context and can publish it on a later turn; nothing at turn
        emission inspects a turn for another session's log content, so that residual
        is real and is named in ``_stale_grant_refusal``'s own docstring rather than
        papered over. The two differ in kind: delivering INTO a published session
        versus a session becoming published while holding what it was entitled to.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        self._links_during(monkeypatch, slot)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: slot},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "lands in that channel's thread" in json.loads(response.text)["error"]

    def test_a_caller_that_links_during_the_refold_offload_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the caller verdict is re-read AFTER the refold suspends.

        A window narrower than the one above, and the reason it needs its own case: the
        caller verdict is sampled before the arms run, and the lineage arm then suspends
        AGAIN to refold the target. A link landing inside that second suspension is
        invisible to the first sample, so answering on the incoming verdict alone leaves
        this route's own smaller window open while it closes the payload-build one --
        the same stale-read class the route exists to close.

        Hooking ``recorded_class`` is what places the link after the sample rather than
        before it: the sibling case hooks the payload read, which the gate's own sample
        already sees.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        from kiro_crew.crew_log import read as crew_log_read

        real = crew_log_read.recorded_class
        calls = []

        def _linking(*args, **kwargs):
            # The gate folds the class too, and THAT call precedes the caller sample.
            # Linking on it would be caught by the sample itself and the mutation
            # would survive, so the link is placed on the re-check's own call.
            calls.append(1)
            if len(calls) >= 2:
                slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(crew_log_read, "recorded_class", _linking)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: slot},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert len(calls) >= 2, "the re-check never refolded, so the window was not exercised"
        assert response.status == 403
        assert "lands in that channel's thread" in json.loads(response.text)["error"]

    def test_a_target_that_moves_mid_read_still_lets_a_caller_read_its_own_unit(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check is CONDITIONAL, not a blanket refusal.

        The classes govern reading PAST one's own record, so the own-unit exemption
        returns before any target test. Without this case a re-check that refused
        unconditionally would pass every other case here while breaking every
        self-read, which is the shipped behaviour on base. The mid-read event is a
        class move on the unit BEING read, which is what this function now reads.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CONDUCTOR_UNIT, channel=True)
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CONDUCTOR_UNIT}/page",
            match={"unit": CONDUCTOR_UNIT},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CONDUCTOR_UNIT

    def test_a_caller_that_links_mid_projection_is_refused(self, monkeypatch):
        """The same race on the projection route, which offloads its own fold."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        real = routes._crew_log().read_projection

        def _linking(*args, **kwargs):
            slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(routes._crew_log(), "read_projection", _linking)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/projection/status",
            match={"unit": CHILD_UNIT, "name": "status"},
            slots={CONDUCTOR_SLOT: slot},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 403
        assert "lands in that channel's thread" in json.loads(response.text)["error"]

    def test_a_caller_that_links_mid_listing_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the listing's scope is re-derived and compared.

        A listing carries its scope as a FILTER, so the rows were selected under the
        wider scope the caller held at the gate. Acquiring a link narrows that scope
        to its own unit, and the rows already gathered include the units it
        dispatched -- so the comparison, not a row test, is what refuses.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        real = routes._crew_log_read().list_session_units

        def _linking(*args, **kwargs):
            slot.linked_session_key = "telegram:-100999"
            return real(*args, **kwargs)

        monkeypatch.setattr(routes._crew_log_read(), "list_session_units", _linking)
        request = _as_conductor("/api/crew-log/sessions", slots={CONDUCTOR_SLOT: slot})
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403

    def test_every_offloading_route_rechecks_before_it_answers(self):
        """MUTATION-SENSITIVE: the guard is at the choke point, not at one call site.

        This route set grows, and a route added without the re-check reads correctly
        in review -- the defect is an ABSENT line, which no assertion about the
        current routes can see. So the rule is asserted over the module's own source:
        an agent-door handler that suspends to build a payload must pass through
        ``_stale_grant_refusal`` before returning it -- the unit routes because the
        target's class can move while the entries are read, and the listing because its
        rows were gathered under a scope wider than the caller now holds.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(routes))
        gated = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            called = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if "_authorize_crew_log_read" not in called:
                continue
            body = ast.dump(node)
            if "to_thread" not in body:
                # The resolve route answers from memory, so its gate's own check
                # is already the last read of live state before it returns.
                continue
            gated.append(node.name)
            assert "_stale_grant_refusal" in called, (
                f"{node.name} suspends after authorizing but never re-checks the "
                "grant; a caller can acquire a channel link while it reads"
            )
        # A control: an empty set would satisfy the loop above silently.
        assert len(gated) == 3, f"expected three offloading routes, found {gated}"


class TestTheListingCarriesTheSameScope:
    """A listing enumerates units, so it is filtered rather than merely allowed."""

    def test_a_conductor_sees_its_own_tree_and_not_a_stranger(self, monkeypatch):
        """MUTATION-SENSITIVE: the listing filter.

        An unscoped listing would name every session on the host, which is the one
        leak a per-unit gate cannot refuse after the fact.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT, CHILD_UNIT, GRANDCHILD_UNIT}

    def test_the_owner_sees_every_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert STRANGER_UNIT in listed

    def test_an_excluded_class_sees_only_its_own_unit(self, monkeypatch):
        """Its own record, and nothing below it -- the listing form of the exclusion."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            "/api/crew-log/sessions",
            slots={"conductor": _Slot(linked_session_key="slack:C123")},
        )
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT}

    def test_a_caller_with_no_unit_of_its_own_is_refused(self, monkeypatch):
        """Nothing to scope to, so there is no honest listing to return."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor("/api/crew-log/sessions", sessions={})
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert "no live crew log unit" in json.loads(response.text)["error"]


class TestOnlyTheInternalTransportReachesThese:
    """A cookie-authenticated caller is refused here, and told where its door is.

    Strict membership is NOT what refuses it. ``token_auth_middleware`` falls
    through to ordinary cookie auth when a LOOPBACK request carries no
    ``X-Internal-Secret``, on a strict path as much as a mixed one, and calls the
    handler once the cookie validates; strict decides only the NON-loopback caller,
    and ``local_only=False`` reclassifies strict as mixed anyway. So a same-machine
    tab reaches these handlers, and the refusal below is the only thing stopping it.
    """

    @pytest.mark.parametrize(
        "handler,path,match",
        [
            ("api_crew_log_sessions", "/api/crew-log/sessions", None),
            ("api_crew_log_resolve", f"/api/crew-log/resolve?key={OWNER_KEY}", None),
            (
                "api_crew_log_unit_page",
                f"/api/crew-log/units/{SESSION}/page",
                {"unit": SESSION},
            ),
            (
                "api_crew_log_unit_projection",
                f"/api/crew-log/units/{SESSION}/projection/status",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["sessions", "resolve", "page", "projection"],
    )
    def test_a_caller_with_no_internal_secret_is_refused(self, monkeypatch, handler, path, match):
        """All four, because one unguarded route is the whole hole."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(path, secret=False, match=match)
        response = asyncio.run(getattr(routes, handler)(request))
        assert response.status == 403
        body = json.loads(response.text)
        assert body["code"] == "forbidden"
        assert "/api/sessions/" in body["error"]

    def test_a_cookie_caller_reads_nothing_through_this_door(self, monkeypatch):
        """The refusal lands BEFORE any read, not after a second authorization test.

        This door and the browser's own pair keep separate authorization models, and
        a cookie admitted here would be a second path into the same bytes with a
        different gate in front of it. Pinned by asserting the shared read
        implementation is never entered, so the refusal cannot be satisfied by a
        handler that reads first and filters after.
        """
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions", secret=False)
        with patch.object(routes, "_crew_log_read") as reader:
            response = asyncio.run(routes.api_crew_log_sessions(request))
        reader.assert_not_called()
        assert response.status == 403
        assert "/api/sessions/" in json.loads(response.text)["error"]

    def test_the_prefix_is_on_the_strict_transport(self):
        """Strict, not mixed: a forwarded browser is hard-denied rather than
        offered the cookie fall-through, because what is behind it is another live
        session's history. The browser's own pair must stay OUT of the prefix, or
        the panel could not load its own log at all."""
        from kiro_crew.dashboard.server import (
            _MIXED_INTERNAL_API_PATHS,
            _STRICT_INTERNAL_API_PATHS,
        )

        assert "/api/crew-log" in _STRICT_INTERNAL_API_PATHS
        assert "/api/crew-log" not in _MIXED_INTERNAL_API_PATHS
        assert "/api/sessions" not in _STRICT_INTERNAL_API_PATHS


class TestReadingAnotherSessionsUnit:
    """A DISPATCHED session's unit, by name. The capability the rule exists for.

    ``self`` is resolved server-side from the forwarded key, and naming another
    unit asks the gate the same question as naming your own, so these tests pin
    both forms against one rule. Every cross-unit read here is a read DOWN the
    caller's own dispatch tree; a read across to a stranger is pinned in
    ``TestTheDispatchFence``.
    """

    def test_a_subagent_may_read_its_own_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        _turn(handle, 1)
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page",
            session_key="subagent:abc",
            slots={},
            sessions={"subagent:abc": SESSION},
            match={"unit": SESSION},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["entries"]

    def test_a_subagent_may_read_a_unit_it_dispatched(self, monkeypatch):
        """MUTATION-SENSITIVE: the rule, stated as a behaviour.

        A non-owner strict identity pages a unit that is NOT the one its own work
        lands in, and reaches it because that unit's ``session/opened`` names the
        caller's unit as its creator. This is the read a conductor needs to see what
        a session it dispatched actually did.
        """
        _flag_on(monkeypatch)
        child = _log("s-dispatched", slot="chat-dispatched")
        _opened(child, parent_slot="abc", parent_sid=SESSION)
        _turn(child, 1)
        request = _internal_request(
            "/api/crew-log/units/s-dispatched/page",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-dispatched"},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        body = json.loads(response.text)
        assert body["session_id"] == "s-dispatched"
        assert body["entries"]

    def test_a_subagent_may_fold_a_unit_it_dispatched(self, monkeypatch):
        """The fold door too, because one open route is the whole capability."""
        _flag_on(monkeypatch)
        child = _log("s-dispatched", slot="chat-dispatched")
        _opened(child, parent_slot="abc", parent_sid=SESSION)
        request = _internal_request(
            "/api/crew-log/units/s-dispatched/projection/status",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-dispatched", "name": "status"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 200
        assert json.loads(response.text)["name"] == "status"

    def test_a_subagent_may_resolve_the_key_of_a_session_it_dispatched(self, monkeypatch):
        """Resolving a dispatched session's key is the step BEFORE reading its unit.

        The route resolves the named key to a unit and scopes on THAT, so the answer
        is available exactly when the read that follows it would be. A key outside
        the caller's tree is refused here rather than one call later.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-dispatched", slot="chat-dispatched"), parent_slot="abc", parent_sid=SESSION)
        child_key = "dashboard:chat-child"
        request = _internal_request(
            f"/api/crew-log/resolve?key={child_key}",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION, child_key: "s-dispatched"},
        )
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 200
        assert json.loads(response.text) == {"key": child_key, "unit": "s-dispatched"}

    def test_resolving_a_stranger_key_is_refused(self, monkeypatch):
        """A resolve is a read, so it carries the same fence.

        Without this the route would answer which unit any session is landing in,
        which is the first half of reading it.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-stranger-unit"))
        stranger_key = "dashboard:chat-stranger"
        request = _internal_request(
            f"/api/crew-log/resolve?key={stranger_key}",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION, stranger_key: "s-stranger-unit"},
        )
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 403

    def test_a_dead_key_and_a_live_stranger_key_refuse_identically(self, monkeypatch):
        """MUTATION-SENSITIVE: the resolve route must not be a liveness oracle.

        The route resolves the key the caller named BEFORE authorizing, so the two
        refusals it can reach -- nothing resolved, and something resolved that is
        outside the caller's tree -- are reached by guessing a key. If their text
        differed, a dispatched agent could guess keys and learn which slots are
        live, which is an enumeration of the host's sessions rather than a read of
        one. The bodies are compared byte for byte, so any divergence fails.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-stranger-unit"))
        stranger_key = "dashboard:chat-stranger"
        live = _internal_request(
            f"/api/crew-log/resolve?key={stranger_key}",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION, stranger_key: "s-stranger-unit"},
        )
        dead = _internal_request(
            "/api/crew-log/resolve?key=dashboard:chat-does-not-exist",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
        )
        live_response = asyncio.run(routes.api_crew_log_resolve(live))
        dead_response = asyncio.run(routes.api_crew_log_resolve(dead))
        assert live_response.status == dead_response.status == 403
        assert json.loads(live_response.text) == json.loads(dead_response.text)

    def test_an_incognito_session_may_read_its_own_unit(self, monkeypatch):
        """Restriction is about what LEAVES the session, not about reading."""
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/status",
            slots={"chat-owner": _Slot(restricted=True)},
            match={"unit": SESSION, "name": "status"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 200
        assert json.loads(response.text)["name"] == "status"

    def test_an_unnamed_caller_reads_no_unit_at_all(self, monkeypatch):
        """Not even its own: "its own unit" is derived from the key that is missing."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page",
            session_key="",
            match={"unit": SESSION},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert json.loads(response.text)["code"] == "forbidden"


class TestTheNewRoutes:
    def test_the_listing_returns_one_row_per_unit(self, monkeypatch):
        _flag_on(monkeypatch)
        first = _log()
        _opened(first)
        _turn(first, 1)
        second = _log("s-second")
        _opened(second)
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        units = {row["unit"]: row for row in body["units"]}
        assert set(units) == {SESSION, "s-second"}
        assert units[SESSION]["slot"] == "dashboard:1"
        assert units[SESSION]["agent"] == "kirocrew"
        assert units[SESSION]["model"] == "opus"
        assert units[SESSION]["open"] is True
        assert units[SESSION]["last_seq"] >= 3
        assert body["scanned"] == 2
        assert body["truncated"] is False

    def test_type_counts_are_the_histogram_read_by_hand_today(self, monkeypatch):
        _flag_on(monkeypatch)
        handle = _log()
        _opened(handle)
        _turn(handle, 1)
        _turn(handle, 2)
        request = _internal_request("/api/crew-log/sessions?with_type_counts=1")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        assert body["type_counts"]["turn/started"] == 2
        assert body["type_counts"]["turn/completed"] == 2
        assert body["units"][0]["type_counts"]["session/opened"] == 1

    def test_a_slot_filter_keeps_only_matching_units(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        other = _log("s-other-slot", slot="dashboard:99")
        other.append(
            "session/opened",
            {
                "agent": "kirocrew",
                "slot": "dashboard:99",
                "model": "opus",
                "cwd": "/w",
                "owner": "raymond",
                "resumed": False,
            },
            src=GATEWAY,
        )
        request = _internal_request("/api/crew-log/sessions?slot_contains=dashboard%3A99")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        body = json.loads(response.text)
        assert [row["unit"] for row in body["units"]] == ["s-other-slot"]

    def test_an_unrecognized_query_does_not_change_what_is_listed(self, monkeypatch):
        """``kind`` is not part of this surface, so it is an unknown query.

        The listing holds sessions, because nothing writes a unit of another kind.
        A ``kind`` argument would therefore have one legal value equal to its own
        default, and refusing the illegal values would keep a decision alive on the
        surface that the storage layer does not offer. These routes ignore an
        unknown query parameter the way every other route does. What must NOT
        happen is a refusal, which reads to a caller as "this listing holds other
        kinds, and you named one wrong".
        """
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions?kind=members")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        body = json.loads(response.text)
        assert body["kind"] == "session"
        assert [row["unit"] for row in body["units"]] == [SESSION]

    def test_resolve_answers_the_unit_a_key_is_landing_in(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request(f"/api/crew-log/resolve?key={OWNER_KEY}")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 200
        assert json.loads(response.text) == {"key": OWNER_KEY, "unit": SESSION}

    def test_resolve_refuses_a_key_with_no_live_acp_session(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/resolve?key=dashboard:chat-gone")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 404
        assert json.loads(response.text)["code"] == "unresolvable_key"

    def test_resolve_needs_a_key(self, monkeypatch):
        _flag_on(monkeypatch)
        request = _internal_request("/api/crew-log/resolve")
        response = asyncio.run(routes.api_crew_log_resolve(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "unresolvable_key"

    def test_a_unit_with_no_log_is_unknown_rather_than_an_empty_page(self, monkeypatch):
        """``exists: false`` is the browser route's shape; an agent needs a code."""
        _flag_on(monkeypatch)
        request = _internal_request(
            "/api/crew-log/units/s-nothing/page", match={"unit": "s-nothing"}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 404
        assert json.loads(response.text)["code"] == "unknown_unit"

    def test_an_unknown_projection_is_named_as_such(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/nope",
            match={"unit": SESSION, "name": "nope"},
        )
        response = asyncio.run(routes.api_crew_log_unit_projection(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "unknown_projection"

    def test_a_reversed_range_is_a_bad_range(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/page?from=9&to=2", match={"unit": SESSION}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 400
        assert json.loads(response.text)["code"] == "bad_range"


class TestAudit:
    """Every read is SEL-audited under the SAME action names the browser routes use."""

    @pytest.mark.parametrize(
        "handler,operation,match",
        [
            ("api_crew_log_sessions", "session_crew_log.list", {}),
            ("api_crew_log_unit_page", "session_crew_log.read", {"unit": SESSION}),
            (
                "api_crew_log_unit_projection",
                "session_crew_log.projection",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["list", "read", "projection"],
    )
    def test_a_granted_read_is_audited_under_its_action(
        self, monkeypatch, handler, operation, match
    ):
        _flag_on(monkeypatch)
        _opened(_log())
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        path = "/api/crew-log/x"
        request = _internal_request(path, match=match)
        with patch("kiro_crew.sel.sel", return_value=sel):
            asyncio.run(getattr(routes, handler)(request))
        rows = [row for row in recorded if row["operation"] == operation]
        assert rows, recorded
        assert rows[0]["caller"] == routes.CREW_LOG_MCP_CALLER
        assert rows[0]["source"] == "mcp"
        assert rows[0]["outcome"] == "granted"

    def test_a_denied_read_is_audited_with_its_reason(self, monkeypatch):
        """MUTATION-SENSITIVE: an unnamed caller is refused AND leaves the record.

        A ``cron:`` key is admitted now, so the denial pinned here is the one the
        identity gate still makes: a request carrying no session key at all.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", session_key="")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows and rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == routes.CREW_LOG_MCP_CALLER
        assert "session identity" in rows[0]["error"]

    def test_a_dispatched_unit_read_is_audited_as_granted(self, monkeypatch):
        """MUTATION-SENSITIVE: the widened read is the one an operator must see.

        The decision makes a non-owner read of a unit it dispatched ordinary, so the
        audit row for it is the whole of what replaces the refusal. It names the
        operation, the calling component and the granted outcome.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-dispatched", slot="chat-dispatched"), parent_slot="abc", parent_sid=SESSION)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request(
            "/api/crew-log/units/s-dispatched/page",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-dispatched"},
        )
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        rows = [row for row in recorded if row["operation"] == "session_crew_log.read"]
        assert rows, recorded
        assert rows[0]["outcome"] == "granted"
        assert rows[0]["caller"] == routes.CREW_LOG_MCP_CALLER
        assert rows[0]["source"] == "mcp"

    def test_a_read_outside_the_dispatch_tree_is_audited_as_denied(self, monkeypatch):
        """MUTATION-SENSITIVE: the refusal side of the same door.

        A fence is only auditable if the refusal it produces reaches the record, and
        this is the refusal an operator most wants: one session reaching for
        another's history.
        """
        _flag_on(monkeypatch)
        _opened(_log("s-stranger-unit"))
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request(
            "/api/crew-log/units/s-stranger-unit/page",
            session_key="subagent:abc",
            slots={"abc": _Slot()},
            sessions={"subagent:abc": SESSION},
            match={"unit": "s-stranger-unit"},
        )
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.read"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert "does not fall inside that scope" in rows[0]["error"]

    def test_a_secretless_caller_is_audited_as_the_dashboard(self, monkeypatch):
        """The denial an operator most wants: something reached an MCP-only route
        with a browser-shaped credential. It is refused BEFORE the caller identity
        is settled, so the record has to come from ``request_origin``'s no-secret
        answer -- ``("dashboard", "dashboard")`` -- rather than from a claimed
        component name, which an unauthenticated caller could set to anything.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", secret=False)
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "dashboard"
        assert rows[0]["source"] == "dashboard"
        assert "/api/sessions/" in rows[0]["error"]

    def test_a_request_naming_another_component_is_audited(self, monkeypatch):
        """The other boundary-crossing shape: an authenticated internal caller that
        is not this server. It audits under the name ``request_origin`` resolved,
        which is a KNOWN component or the clamped ``unknown-internal`` -- never the
        raw header value.
        """
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", caller="kirocrew-dashboard")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows, recorded
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "kirocrew-dashboard"
        assert rows[0]["source"] == "mcp"
        assert routes.CREW_LOG_MCP_CALLER in rows[0]["error"]

    def test_an_unrecognized_component_is_audited_as_unknown_internal(self, monkeypatch):
        """A name no release ships is clamped, so the audit log cannot be seeded
        with an arbitrary string by whoever set the header."""
        _flag_on(monkeypatch)
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/sessions", caller="not-a-real-server")
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        rows = [row for row in recorded if row["operation"] == "session_crew_log.list"]
        assert rows and rows[0]["caller"] == "unknown-internal"

    @pytest.mark.parametrize(
        "handler,operation,match",
        [
            ("api_crew_log_sessions", "session_crew_log.list", {}),
            ("api_crew_log_resolve", "session_crew_log.resolve", {}),
            ("api_crew_log_unit_page", "session_crew_log.read", {"unit": SESSION}),
            (
                "api_crew_log_unit_projection",
                "session_crew_log.projection",
                {"unit": SESSION, "name": "status"},
            ),
        ],
        ids=["list", "resolve", "read", "projection"],
    )
    def test_no_route_refuses_without_leaving_a_record(
        self, monkeypatch, handler, operation, match
    ):
        """One unaudited denial path is the whole gap, so all four are pinned."""
        _flag_on(monkeypatch)
        _opened(_log())
        recorded: list[dict] = []
        sel = MagicMock()
        sel.log_api_access.side_effect = lambda **kw: recorded.append(kw)
        request = _internal_request("/api/crew-log/x", secret=False, match=match)
        with patch("kiro_crew.sel.sel", return_value=sel):
            response = asyncio.run(getattr(routes, handler)(request))
        assert response.status == 403
        assert [row for row in recorded if row["outcome"] == "denied"], recorded

    def test_a_failing_audit_never_changes_the_outcome(self, monkeypatch):
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request("/api/crew-log/sessions")
        with patch("kiro_crew.sel.sel", side_effect=RuntimeError("sel is down")):
            response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200

    def test_the_proxys_own_call_lands_in_the_callers_crew_log(self, monkeypatch):
        """The read is recorded twice on purpose: SEL for the operator, and a
        ``tool/called`` entry in the calling session's OWN log for the record the
        session itself carries. The second is automatic; this pins that it happens.
        """
        monkeypatch.setenv(routes.CREW_LOG_ENV, "1")
        from kiro_crew.crew_log import emit

        handle = _log()
        _opened(handle)
        handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
        emit.on_tool_called(
            SESSION,
            1,
            name="crew_log_read",
            server="kirocrew-crew-log",
            kind="read",
            call_id="c-1",
            args='{"unit": "self"}',
        )
        emit.drain_for_shutdown()
        types = [entry.type for entry in CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1)]
        assert "tool/called" in types
        called = [
            entry
            for entry in CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1)
            if entry.type == "tool/called"
        ]
        assert called[0].data["server"] == "kirocrew-crew-log"
        assert called[0].data["name"] == "crew_log_read"


class TestTheBrowserRoutesAreUnchanged:
    def test_the_browser_page_route_still_refuses_the_internal_caller(self, monkeypatch):
        """The session-keyed pair is cookie-only: the agent's door is the unit-keyed
        prefix, and admitting the internal caller here would widen two routes."""
        _flag_on(monkeypatch)
        _opened(_log())
        request = _internal_request(f"/api/sessions/{SESSION}/crew-log", match={"id": SESSION})
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            response = asyncio.run(routes.api_session_crew_log(request))
        assert response.status == 403


class TestTheListingBoundsWhatItRetains:
    """The count bound is applied while walking, not by the scan cap downstream.

    A host that has run many sessions is the ordinary case, not the adversarial
    one. If the walk retained every directory before the cap, the cost of one
    listing would scale with how many sessions the host had ever run, which is
    what the bound exists to prevent.
    """

    @staticmethod
    def _dirs(count: int, *, newest_first_names: bool = True) -> list[str]:
        """*count* session unit directories, each with a distinct write time.

        The stamp goes on the LOG, not the directory, because that is what the
        reader orders by -- a real unit is a directory holding ``log.jsonl``, and a
        directory with no log is not a unit at all.
        """
        from kiro_crew.crew_log import read as reader

        root = lg.crew_log_root(lg.KIND_SESSION)
        root.mkdir(parents=True, exist_ok=True)
        names = []
        for index in range(count):
            unit = root / f"s-unit-{index:04d}"
            unit.mkdir()
            log = unit / reader.LOG_FILE
            log.write_text("", encoding="utf-8")
            # Ascending write time, so the LAST created is the newest.
            os.utime(log, (1_700_000_000 + index, 1_700_000_000 + index))
            names.append(unit.name)
        assert reader  # the module under test is importable from here
        return names if newest_first_names else list(reversed(names))

    def test_only_the_newest_candidates_are_retained(self, monkeypatch):
        from kiro_crew.crew_log import read as reader

        names = self._dirs(7)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 3)
        kept, cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert [path.name for path in kept] == names[-3:][::-1]
        assert cut is True

    def test_a_root_within_the_bound_is_not_reported_cut(self, monkeypatch):
        from kiro_crew.crew_log import read as reader

        self._dirs(3)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 3)
        kept, cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert len(kept) == 3
        assert cut is False

    def test_a_cut_candidate_set_makes_the_listing_report_truncated(self, monkeypatch):
        """A caller must not read a bounded walk as the whole tree."""
        from kiro_crew.crew_log import read as reader

        self._dirs(5)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 2)
        payload = reader.list_session_units(limit=50)
        assert payload["truncated"] is True

    def test_the_walk_never_holds_more_than_the_bound(self, monkeypatch):
        """The bound is on RETENTION: the heap is capped during iteration, so the
        peak held size cannot grow with the directory count."""
        from kiro_crew.crew_log import read as reader

        self._dirs(9)
        monkeypatch.setattr(reader, "MAX_LISTED_CANDIDATES", 2)
        peaks: list[int] = []
        real_push = reader.heapq.heappush

        def watched(heap, item):
            real_push(heap, item)
            peaks.append(len(heap))

        monkeypatch.setattr(reader.heapq, "heappush", watched)
        reader._candidate_dirs(lg.KIND_SESSION)
        assert peaks and max(peaks) <= 2

    def test_a_root_that_was_never_created_is_empty_and_not_cut(self):
        from kiro_crew.crew_log import read as reader

        assert not lg.crew_log_root(lg.KIND_SESSION).exists()
        assert reader._candidate_dirs(lg.KIND_SESSION) == ([], False)


class TestTheRetentionListingStaysOffTheLoop:
    """A directory listing must never be reached from a coroutine's own body.

    The listing only chooses between two refusal TEXTS, so blocking the loop for it
    spends every session's latency on wording. A review lane found exactly that, and a
    reading of the source is what catches it returning: the repo's sync-IO gate did not
    fire here, because the blocking call is several frames below the coroutine and the
    gate matches direct ones.

    The rule is derived rather than listed: every call site is found in the source, so a
    THIRD one added later is checked without anyone remembering to name it here.
    """

    def _calls_to(self, func_name):
        """Every call of *func_name*, with the enclosing function and whether it awaits."""
        import ast
        import inspect

        from kiro_crew.dashboard.handlers import crew_log as mod

        tree = ast.parse(inspect.getsource(mod))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                target = inner.func
                if isinstance(target, ast.Name) and target.id == func_name:
                    found.append((node.name, isinstance(node, ast.AsyncFunctionDef), "direct"))
                elif (
                    isinstance(target, ast.Attribute)
                    and target.attr == "to_thread"
                    and any(isinstance(a, ast.Name) and a.id == func_name for a in inner.args)
                ):
                    found.append((node.name, isinstance(node, ast.AsyncFunctionDef), "offloaded"))
        return found

    def test_the_listing_is_only_ever_offloaded(self):
        calls = self._calls_to("_front_of_log_is_gone")
        assert calls, "no call site found, so this pin measures nothing"
        on_loop = [(where, how) for where, is_async, how in calls if is_async and how == "direct"]
        assert not on_loop, f"reached on the event loop from {on_loop}"

    def test_the_offloaded_reader_is_the_one_that_may_call_it_directly(self):
        """The non-async helper is allowed to, because it is what gets offloaded."""
        direct = {
            where
            for where, is_async, how in self._calls_to("_front_of_log_is_gone")
            if how == "direct"
        }
        assert direct <= {"_recorded_class_and_front"}, direct


class TestTheCallerTestAgreesWithTheSharedDerivation:
    """The inline caller test must refuse every state the shared derivation publishes.

    ``_caller_class_refusal`` respells the test ``chat_runner._crew_log_class`` makes,
    and deliberately: the derivation answers with a class, while a refusal has to NAME
    which member refused and why it matters, and a caller that reads its own reason is
    the difference between a usable refusal and a bare 403. What that duplication risks
    is divergence -- a member the derivation learns to report and the refusal never
    tests would let a publishing caller read another session's log, which fails OPEN.

    So the agreement is pinned rather than the duplication removed. Each case asserts
    the shape actually moved the DERIVATION before asking the refusal about it, so a
    fixture that stopped constructing the state it names fails instead of passing
    vacuously, and the width assertion makes a NEW member fail here until someone
    decides whether the refusal owes it a test.
    """

    def _request_for(self, slot):
        return _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: slot},
        )

    def test_the_derivation_reports_exactly_the_members_the_refusal_tests(self):
        """A fourth member has to be considered here, not silently untested."""
        from kiro_crew.dashboard.chat_runner import _crew_log_class

        assert len(_crew_log_class(None, _Slot())) == 3

    @pytest.mark.parametrize(
        "shape,expected",
        [
            ({"memory_mode": "incognito"}, "leave and learn nothing"),
            ({"_app": "notes"}, "app-owned session"),
            ({"linked_session_key": "telegram:-100999"}, "lands in that channel's thread"),
        ],
        ids=["memory", "app", "channel"],
    )
    def test_a_state_the_derivation_publishes_is_refused(self, monkeypatch, shape, expected):
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        for name, value in shape.items():
            setattr(slot, name, value)
        request = self._request_for(slot)
        from kiro_crew.dashboard.chat_runner import _crew_log_class

        state = request.app.get("state")
        assert _crew_log_class(state, slot) != (
            "persistent",
            "",
            False,
        ), "the fixture no longer moves the shared derivation, so it pins nothing"
        assert expected in routes._caller_class_refusal(request, CONDUCTOR_SLOT)

    def test_a_state_the_derivation_calls_private_is_admitted(self, monkeypatch):
        """The accepting case, without which every assertion above could be a blanket no."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot()
        request = self._request_for(slot)
        from kiro_crew.dashboard.chat_runner import _crew_log_class

        assert _crew_log_class(request.app.get("state"), slot) == ("persistent", "", False)
        assert routes._caller_class_refusal(request, CONDUCTOR_SLOT) == ""


class TestRecencyComesFromTheLogNotItsDirectory:
    """A long-lived session is the case that breaks reading the directory's mtime.

    A directory's mtime moves when its entry SET changes -- a file created, renamed
    or removed -- not when a file inside it is written. A unit directory gets its
    ``log.jsonl`` once and is appended to for the rest of the session, so its own
    mtime freezes at creation. Order by it and "newest first" silently means "most
    recently STARTED first", and a recency window drops the session that has been
    open and busy for a day while keeping one created a minute ago and idle since.
    That is backwards for both callers: the listing exists to find active sessions.
    """

    @staticmethod
    def _unit(unit_id: str, *, dir_at: float, log_at: float) -> None:
        """One real unit whose directory and log carry DIFFERENT times.

        Through ``crew_log_dir``, never ``root / unit_id``: a unit directory is named
        with a readable-plus-digest fold of the id, not the id itself.
        """
        from kiro_crew.crew_log import read as reader

        _opened(_log(unit_id))
        directory = lg.crew_log_dir(lg.KIND_SESSION, unit_id)
        os.utime(directory / reader.LOG_FILE, (log_at, log_at))
        # The directory LAST, so creating the log cannot move it afterwards.
        os.utime(directory, (dir_at, dir_at))

    def test_the_busy_old_session_sorts_newer_than_the_idle_new_one(self):
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-open-all-day", dir_at=now - 86_400, log_at=now)
        self._unit("s-started-just-now", dir_at=now, log_at=now - 86_400)
        kept, _cut = reader._candidate_dirs(lg.KIND_SESSION)
        assert [path.name for path in kept] == [
            lg.crew_log_dir(lg.KIND_SESSION, "s-open-all-day").name,
            lg.crew_log_dir(lg.KIND_SESSION, "s-started-just-now").name,
        ]

    def test_a_recency_window_keeps_the_busy_session_and_drops_the_idle_one(self):
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-open-all-day", dir_at=now - 86_400, log_at=now)
        self._unit("s-started-just-now", dir_at=now, log_at=now - 86_400)
        payload = reader.list_session_units(limit=50, active_within_ms=3_600_000)
        assert [row["unit"] for row in payload["units"]] == ["s-open-all-day"]

    def test_a_unit_with_no_readable_log_sorts_oldest_rather_than_raising(self):
        """An unreadable member of the root must not fail the whole listing.

        It is refused a moment later for having no unit id, so the only question
        here is whether one bad directory can take the listing down with it.
        """
        from kiro_crew.crew_log import read as reader

        now = time.time()
        self._unit("s-real", dir_at=now, log_at=now)
        (lg.crew_log_root(lg.KIND_SESSION) / "s-no-log").mkdir()
        assert reader._written_at(lg.crew_log_root(lg.KIND_SESSION) / "s-no-log") == 0.0
        payload = reader.list_session_units(limit=50)
        assert [row["unit"] for row in payload["units"]] == ["s-real"]


class TestTheWorkspaceBoundary:
    """A dispatch grant does not cross a workspace, because a lineage outlives a switch.

    The creator edge is written on the CHILD and nothing rewrites it, so a conductor that
    dispatched a child and then moved to another workspace still names that child in its
    tree. Switching workspace is ordinary operation, so the recorded lineage alone would
    hand that conductor a log belonging to the workspace it left -- the boundary a
    workspace exists to draw.

    So the class records which workspace stated it, and the fence compares. Three states
    refuse, one fact in three shapes: the workspaces DIFFER, the log's workspace MOVED so
    its content spans two and neither owns it, or the log records NO workspace, which no
    live slot can produce.

    The arm is ordered LAST of the target arms on purpose -- it asks about ownership while
    the arms above ask about publication, and a caller refused for either deserves the
    more specific reason.
    """

    def test_a_conductor_in_the_same_workspace_still_reads_its_child(self, monkeypatch):
        """The accepting case, without which every assertion below could be a blanket no."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 200
        assert json.loads(response.text)["session_id"] == CHILD_UNIT

    def test_a_conductor_that_moved_workspace_cannot_read_its_own_child(self, monkeypatch):
        """MUTATION-SENSITIVE: the comparison itself, on the scenario the lane named.

        Every other arm admits this read. The lineage is recorded and intact, the caller
        is a persistent non-app session publishing to nobody, and the target's class is
        clean -- so the workspace comparison is the only thing standing between this
        caller and a log from the workspace it left.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: _Slot(workspace="beta")},
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "belongs to a different workspace" in json.loads(response.text)["error"]

    def test_a_log_whose_workspace_moved_is_read_by_neither(self, monkeypatch):
        """A log whose content spans two workspaces is owned by neither, so both refuse.

        Asserted from the workspace the log STARTED in, which is the caller most likely to
        be admitted by a naive first-one-wins comparison.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, workspace="beta")
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "spans more than one workspace" in json.loads(response.text)["error"]

    def test_a_log_that_records_no_workspace_is_refused(self, monkeypatch):
        """Silence is not a match. A live slot always states a workspace, so a log that
        does not is one this build cannot place -- and placing it by assumption is the
        whole thing being prevented."""
        _flag_on(monkeypatch)
        _dispatch_tree(workspace="")
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "does not record which workspace" in json.loads(response.text)["error"]

    def test_a_workspace_that_moves_mid_read_is_caught_by_the_recheck(self, monkeypatch):
        """MUTATION-SENSITIVE: the workspace arm in the STALE re-check, not just the gate.

        At the grant the log named one workspace and the caller matched it, so the gate
        admitted the read. The log then records a move while the payload is being built,
        which puts content from a second workspace in this same log. Only a fold taken
        AFTER the read sees it, which is why the arm runs in both places rather than once.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        real = routes._read_page

        def _moving(*args, **kwargs):
            _class_moved(CHILD_UNIT, workspace="beta")
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "_read_page", _moving)
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        response = asyncio.run(routes.api_crew_log_unit_page(request))
        assert response.status == 403
        assert "spans more than one workspace" in json.loads(response.text)["error"]

    def test_the_gate_refuses_before_a_payload_is_built(self, monkeypatch):
        """MUTATION-SENSITIVE: the arm in the GATE, which the route alone cannot pin.

        Every unit read also passes the stale re-check, and that runs this arm too -- so
        deleting the gate's call leaves each route test above still green, refused a moment
        later by the re-check. The difference is not the verdict but WHEN: without the gate
        arm the payload is read off disk first and refused afterwards, so the log is pulled
        into memory for a caller who must not see it.

        So the gate is asked directly, where no re-check can answer for it.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page",
            match={"unit": CHILD_UNIT},
            slots={CONDUCTOR_SLOT: _Slot(workspace="beta")},
        )
        refusal = asyncio.run(routes._read_scope_refusal(request, CONDUCTOR_KEY, CHILD_UNIT))
        assert "belongs to a different workspace" in refusal

    def test_the_gate_admits_a_caller_in_the_same_workspace(self):
        """The accepting half of the assertion above, so it is a boundary not a wall."""
        _dispatch_tree()
        request = _as_conductor(
            f"/api/crew-log/units/{CHILD_UNIT}/page", match={"unit": CHILD_UNIT}
        )
        refusal = asyncio.run(routes._read_scope_refusal(request, CONDUCTOR_KEY, CHILD_UNIT))
        assert refusal == ""


class TestTheListingCarriesTheWorkspaceBoundary:
    """A listing discloses each row's unit, slot, model and activity, so the workspace
    boundary the per-unit door draws has to reach the rows as well.

    Lineage alone does not bound which workspace a row belongs to, and the dispatch edge
    is written on the child and never rewritten -- so a conductor that dispatched in one
    workspace and then switched still names those children in its tree. Without a per-row
    test the listing becomes an enumeration oracle for exactly the units the read path
    refuses to open.

    The test is asked of LINEAGE rows only, which is what the per-unit door does: a
    caller's own record is answered before any target test, and an owner's unscoped
    listing runs none at all.
    """

    def test_a_conductor_that_moved_workspace_sees_only_its_own_row(self, monkeypatch):
        """MUTATION-SENSITIVE: the per-row filter.

        Same tree and same caller as the scope test above, which lists all three. Only
        the workspace comparison removes the two descendants here -- and the caller's own
        row staying is what proves the filter is scoped to lineage rather than blanket.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        request = _as_conductor(
            "/api/crew-log/sessions", slots={CONDUCTOR_SLOT: _Slot(workspace="beta")}
        )
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT}

    def test_a_descendant_whose_workspace_moved_drops_out_alone(self, monkeypatch):
        """One row spanning two workspaces is refused without taking its siblings."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, workspace="beta")
        request = _as_conductor("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert listed == {CONDUCTOR_UNIT, GRANDCHILD_UNIT}

    def test_the_owner_still_sees_a_unit_from_another_workspace(self, monkeypatch):
        """The owner's listing is not narrowed, so it is never asked -- the recorded
        product decision for that door, which this change must not quietly revise."""
        _flag_on(monkeypatch)
        _dispatch_tree()
        _class_moved(CHILD_UNIT, workspace="beta")
        request = _internal_request("/api/crew-log/sessions")
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 200
        listed = {row["unit"] for row in json.loads(response.text)["units"]}
        assert {CONDUCTOR_UNIT, CHILD_UNIT, GRANDCHILD_UNIT} <= listed

    def test_a_caller_that_switches_workspace_mid_listing_is_refused(self, monkeypatch):
        """MUTATION-SENSITIVE: the re-check's workspace arm, and why the gate stores a
        VALUE.

        The rows were admitted against the workspace read at the gate. The caller then
        switches while the walk runs, so the payload it is about to receive names rows
        from the workspace it just left. The scope tuple is a unit and a slot and neither
        moves on a switch, so a tuple comparison alone cannot see this.

        It also pins the storage choice: holding the slot OBJECT would make the re-check
        re-read the same mutable attribute and compare the new workspace against itself,
        which is always equal and would never refuse.
        """
        _flag_on(monkeypatch)
        _dispatch_tree()
        slot = _Slot(workspace="default")
        reader = routes._crew_log_read()
        real = reader.list_session_units

        def _switching(*args, **kwargs):
            out = real(*args, **kwargs)
            slot.workspace = "beta"
            return out

        monkeypatch.setattr(reader, "list_session_units", _switching)
        request = _as_conductor("/api/crew-log/sessions", slots={CONDUCTOR_SLOT: slot})
        response = asyncio.run(routes.api_crew_log_sessions(request))
        assert response.status == 403
        assert (
            "changed workspace while this listing was built" in json.loads(response.text)["error"]
        )


# --- the work fold on the two projection routes ------------------------------


@pytest.mark.asyncio
async def test_the_session_route_serves_the_work_fold_through_the_slot():
    """`GET .../projection/work` on a session resolves the slot its unit's header
    names and folds every unit of that slot -- the same path `ledger` takes."""
    handle = CrewLog.create(
        lg.KIND_SESSION, "s-conductor", owner="raymond", agent="kirocrew", slot="chat-9"
    )
    handle.append(
        "work/recorded",
        {
            "slot": "chat-9",
            "actor": "conductor",
            "by": "chat-9",
            "action": "goal",
            "goal": "ship it",
            "round": 1,
            "depth": 0,
            "event": "goal set",
            "event_kind": "decision",
        },
        src="gateway",
    )
    fold = await routes.api_session_crew_log_projection(
        _request_with_sessions("fold", "chat-9", {"chat-9": "s-conductor"}, name="work")
    )
    assert fold.status == 200, fold.text
    body = _body(fold)
    assert body["value"]["conductor"]["goal"] == "ship it"
    assert body["value"]["conductor"]["entries"] == 1


def test_the_unit_route_refuses_a_slot_keyed_fold(monkeypatch):
    _flag_on(monkeypatch)
    _opened(_log())
    request = _internal_request(
        f"/api/crew-log/units/{SESSION}/projection/work",
        match={"unit": SESSION, "name": "work"},
    )
    response = asyncio.run(routes.api_crew_log_unit_projection(request))
    assert response.status == 400
    assert json.loads(response.text)["code"] == "slot_projection"
