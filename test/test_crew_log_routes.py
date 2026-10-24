"""The crew-log read routes and the ``session_projection`` push.

Three things are pinned here that the fold tests cannot see: the owner gate on
the browser's two reads, the deliberate difference in posture between a PAGE read
and a FOLD read over the same bytes, and that the push sends a frame only for a
projection whose seq actually moved.

The agent's unit-keyed door is pinned in three siblings:
``test_crew_log_routes_dispatch_fence.py`` (its identity gate, the dispatch fence, the
post-read re-check and its offload helper), ``test_crew_log_routes_unit_door_audit.py``
(its transport, its answers, the SEL audit) and
``test_crew_log_routes_listing_workspace.py`` (the listing walk, caller classes, the
workspace boundary). Shared fixtures and builders live in ``crew_log_routes_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from crew_log_routes_helpers import _fresh_dispatch_scan  # noqa: F401
from crew_log_routes_helpers import _is_owner  # noqa: F401
from crew_log_routes_helpers import _isolated_home  # noqa: F401
from crew_log_routes_helpers import (
    GATEWAY,
    SESSION,
    _flag_on,
    _internal_request,
    _log,
    _opened,
    _Sessions,
    _Slot,
    _turn,
)
from off_loop_helpers import off_loop

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, Ref
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import crew_log as routes


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
async def test_the_session_route_refuses_a_tree_keyed_fold_rather_than_folding_one_unit():
    """A tree fold's population is not a property of the key, so this route cannot serve
    it.

    It is resolved by walking what the record says -- the root's units, every worker they
    recorded a bind for, and every board those workers conduct, to closure -- so folding
    the ONE unit this route addresses would serve a single session as the whole tree.
    That is the same part-served-as-the-whole the route already refuses for a slot fold
    its owner serves, so it gets the same refusal rather than a 200 carrying a partial
    answer.

    FAILS CLOSED on purpose: a tree over "the part this route can reach" is a
    complete-looking wrong answer, which is worse than a refusal a caller can act on.
    """
    handle = _log()
    _opened(handle)
    assert crew_log.TREE_PROJECTION_NAMES, "the tree key kind has no members to check"
    for name in crew_log.TREE_PROJECTION_NAMES:
        # Registered and bindable, so it passes ``require_name`` -- the refusal below is
        # the route's own, not an unknown-name rejection.
        assert name in crew_log._FOLDS
        assert name not in crew_log.SLOT_PROJECTION_NAMES
        assert name not in crew_log.SESSION_FOLD_NAMES
        response = await routes.api_session_crew_log_projection(
            _request_with_sessions("fold", "chat-7", {"chat-7": SESSION}, name=name)
        )
        assert response.status == 400, response.text
        assert _body(response)["code"] == "unknown_projection"
        assert "tree-keyed" in _body(response)["error"]
    # The folds this route DOES serve still answer, so the refusal is scoped to the kind.
    for name in ("ledger", "status"):
        served = await routes.api_session_crew_log_projection(
            _request_with_sessions("fold", "chat-7", {"chat-7": SESSION}, name=name)
        )
        assert served.status == 200, served.text


@pytest.mark.asyncio
async def test_the_unit_route_refuses_a_tree_keyed_fold_rather_than_folding_one_unit(monkeypatch):
    """The SECOND door, refusing the same way. Same defect, same reason as the route
    above: this one folds the one unit its path names, and a tree fold's population is
    not a property of that key.

    It is a separate case because the two routes are separate gates. This one admits an
    internal caller on a forwarded session key rather than the owner's cookie, so a
    refusal written on the per-session route does not cover it -- and a tree name went
    through here while the other door refused it.
    """
    handle = _log()
    _opened(handle)
    _flag_on(monkeypatch)
    assert crew_log.TREE_PROJECTION_NAMES, "the tree key kind has no members to check"
    for name in crew_log.TREE_PROJECTION_NAMES:
        request = _internal_request(
            f"/api/crew-log/units/{SESSION}/projection/{name}",
            slots={"chat-owner": _Slot(restricted=False)},
            match={"unit": SESSION, "name": name},
        )
        response = await routes.api_crew_log_unit_projection(request)
        assert response.status == 400, response.text
        body = json.loads(response.text)
        assert body["code"] == "unknown_projection"
        assert "tree-keyed" in body["error"]
    # A session-keyed fold through the same door still answers, so the refusal is
    # scoped to the kind and not to the route. Read off the advertised list rather than
    # named here: this door folds ONE unit, so a slot-keyed name is refused by design
    # and picking one by hand would test the wrong refusal.
    served_here = [
        name for name in crew_log.PROJECTION_NAMES if name in crew_log.SESSION_FOLD_NAMES
    ]
    assert served_here, "no advertised session-keyed fold to check the refusal against"
    for name in served_here:
        served = await routes.api_crew_log_unit_projection(
            _internal_request(
                f"/api/crew-log/units/{SESSION}/projection/{name}",
                slots={"chat-owner": _Slot(restricted=False)},
                match={"unit": SESSION, "name": name},
            )
        )
        assert served.status == 200, (name, served.text)


def test_every_route_that_takes_a_projection_name_is_one_of_the_two_that_refuse_a_tree():
    """The RULE, not the two instances: a fail-closed refusal is only fail-closed if it
    covers every door that takes a fold name.

    Read off the registered route table rather than listed by hand, so a THIRD route
    with a ``{name}`` fails here instead of shipping as a hole. The two cases above own
    the behaviour; this one owns the inventory, and it is the half that was missing when
    one of the two doors accepted a tree name.
    """
    from kiro_crew.dashboard.routes import sessions as session_routes

    app = web.Application()
    session_routes.register(app)
    named = {
        resource.canonical
        for resource in app.router.resources()
        if str(resource.canonical).endswith("/projection/{name}")
    }
    assert named == {
        "/api/sessions/{id}/crew-log/projection/{name}",
        "/api/crew-log/units/{unit}/projection/{name}",
    }


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
    # The unit the folds came from, so the panel can refuse a pushed frame from another
    # unit, and each fold's revision, so it can order a push against this baseline.
    assert body["unit"] == SESSION
    assert body["projections"]["status"]["revision"] > 0


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
async def test_the_fold_read_says_whether_the_gateway_is_recording(monkeypatch):
    """An empty fold cannot say why, so the read names the flag state beside it."""
    handle = _log()
    _opened(handle)
    monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    on = _body(
        await routes.api_session_crew_log_projections(_request_with_sessions("folds", SESSION, {}))
    )
    assert on["recording"] is True
    assert "flag_value" not in on
    assert "env_file" not in on
    monkeypatch.setenv("KIROCREW_CREW_LOG", "off")
    off = _body(
        await routes.api_session_crew_log_projections(_request_with_sessions("folds", SESSION, {}))
    )
    assert off["recording"] is False
    assert off["flag_value"] == "off"
    assert off["flag_recognised"] is True
    from kiro_crew.constants import env_file_display

    assert off["env_file"] == env_file_display()


@pytest.mark.asyncio
async def test_the_fold_read_quotes_only_the_flag_value_that_switched_recording_off(
    monkeypatch,
):
    """The panel names the typo; nothing else from the environment is sent."""
    handle = _log()
    _opened(handle)
    monkeypatch.setenv("KIROCREW_CREW_LOG", " fasle\x1b[31m" + "x" * 80 + " ")
    monkeypatch.setenv("KIROCREW_TEST_SECRET_NEIGHBOUR", "do-not-send")
    off = _body(
        await routes.api_session_crew_log_projections(_request_with_sessions("folds", SESSION, {}))
    )
    assert off["recording"] is False
    assert off["flag_value"].startswith("fasle[31m")
    assert len(off["flag_value"]) == 40
    assert "\x1b" not in off["flag_value"]
    assert off["flag_recognised"] is False
    assert "do-not-send" not in json.dumps(off)


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
    off_loop(_turn, handle, 1)
    body = _body(await routes.api_session_crew_log_projection(_projection_request(name)))
    assert body["name"] == name
    assert body["seq"] == handle.last_seq
    assert body["session_id"] == SESSION
    assert body["value"] == off_loop(crew_log.read_projection, SESSION, name).value


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


def _frames(state: _Sockets, name: str) -> list[dict]:
    return [data for frame, data in state.frames if frame == name]


async def _pass(publisher: routes.CrewLogPublisher, *sessions: str) -> None:
    """One flush pass over *sessions*, the way a coalesced growth runs."""
    publisher._dirty.update(sessions)
    await publisher._flush()


@pytest.mark.asyncio
async def test_a_growth_pushes_one_frame_per_projection():
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    assert await publisher._publish(SESSION)
    assert {frame for frame, _ in state.frames} == {routes.FRAME}
    assert {data["name"] for data in _frames(state, routes.FRAME)} == set(crew_log.PROJECTION_NAMES)
    for data in _frames(state, routes.FRAME):
        assert data["session_id"] == SESSION
        assert data["seq"] == handle.last_seq
        assert "value" in data


@pytest.mark.asyncio
async def test_a_growth_names_the_slot_to_reread_and_carries_nothing_else():
    """Slot folds are read through the route; the push only says which slot moved."""
    handle = _log(slot="dashboard:conductor")
    _opened(handle, slot="dashboard:conductor")
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await _pass(publisher, SESSION)
    assert _frames(state, routes.SLOT_FRAME) == [{"slot": "dashboard:conductor"}]
    state.frames.clear()
    _turn(handle, 1)
    with patch.object(crew_log, "slot_of_session", side_effect=AssertionError("re-read")):
        await _pass(publisher, SESSION)
    assert _frames(state, routes.SLOT_FRAME) == [{"slot": "dashboard:conductor"}]
    state.frames.clear()
    await _pass(publisher, SESSION)
    assert state.frames == []


@pytest.mark.asyncio
async def test_one_pass_sends_one_slot_frame_per_slot():
    for unit in ("s-a", "s-b"):
        _opened(_log(unit, slot="dashboard:team"), slot="dashboard:team")
    _opened(_log("s-c", slot="dashboard:other"), slot="dashboard:other")
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    await _pass(publisher, "s-a", "s-b", "s-c")
    assert sorted(data["slot"] for data in _frames(state, routes.SLOT_FRAME)) == [
        "dashboard:other",
        "dashboard:team",
    ]


@pytest.mark.asyncio
async def test_the_slot_owner_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(routes, "MAX_CACHED_SLOT_OWNERS", 2)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    for index in range(3):
        unit = f"s-owner{index}"
        _opened(_log(unit, slot=f"dashboard:{index}"), slot=f"dashboard:{index}")
        await _pass(publisher, unit)
    assert list(publisher._slot_owners) == ["s-owner1", "s-owner2"]
    assert [data["slot"] for data in _frames(state, routes.SLOT_FRAME)] == [
        "dashboard:0",
        "dashboard:1",
        "dashboard:2",
    ]


@pytest.mark.asyncio
async def test_an_unprovable_slot_sends_no_slot_frame_and_is_asked_again():
    handle = _log()
    _opened(handle)
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    with patch.object(crew_log, "slot_of_session", return_value="") as probe:
        await _pass(publisher, SESSION)
        _turn(handle, 1)
        await _pass(publisher, SESSION)
    assert probe.call_count == 2
    assert _frames(state, routes.SLOT_FRAME) == []


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
    assert len(publisher._sent) == routes.MAX_CACHED_SESSIONS


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
    assert {data["session_id"] for data in _frames(state, routes.FRAME)} == {"s-good"}


def test_notify_from_a_writer_thread_does_no_work_of_its_own():
    """The writer hands the id to the loop; the reading happens there."""
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    loop = MagicMock()
    publisher.bind(loop)
    publisher.notify(SESSION)
    loop.call_soon_threadsafe.assert_called_once_with(publisher._mark, SESSION)
    assert state.frames == []
    assert publisher._dirty == set()


def test_notify_before_the_publisher_is_bound_is_a_no_op():
    publisher = routes.CrewLogPublisher(_Sockets())
    publisher.notify(SESSION)
    assert publisher._dirty == set()
    assert publisher._scheduled is False
    # Once bound, an empty id still hands nothing over, while a real one does.
    loop = MagicMock()
    publisher.bind(loop)
    publisher.notify("")
    loop.call_soon_threadsafe.assert_not_called()
    publisher.notify(SESSION)
    loop.call_soon_threadsafe.assert_called_once_with(publisher._mark, SESSION)


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


@pytest.mark.asyncio
async def test_a_reinstall_swaps_the_bus_subscriptions_rather_than_doubling_them(monkeypatch):
    """The publisher keeps its two scoped disposers, so a re-install replaces the pair."""
    from kiro_crew.crew_log import bus as crew_log_bus
    from kiro_crew.crew_log import emit as crew_log_emit

    monkeypatch.setenv(routes.CREW_LOG_ENV, "1")
    crew_log_bus.reset_for_tests()
    try:
        with (
            patch.object(routes, "_publisher", None),
            patch.object(crew_log_emit, "_growth_listeners", []),
        ):
            first = routes.install_crew_log_publisher(_Sockets())
            assert first is not None
            assert crew_log_bus.subscriber_count(crew_log_bus.FOLD_ADVANCED) == 2
            with patch.object(routes, "_publisher", first):
                routes.install_crew_log_publisher(_Sockets())
            assert crew_log_bus.subscriber_count(crew_log_bus.FOLD_ADVANCED) == 2

            slot_calls: list = []
            session_calls: list = []
            monkeypatch.setattr(first, "on_slot_fold", slot_calls.append)
            monkeypatch.setattr(first, "on_session_fold", session_calls.append)
            first.subscribe_bus()
            slot_event = crew_log_bus.FoldAdvanced("slot", "dashboard:1", "work", 1, {}, 1)
            session_event = crew_log_bus.FoldAdvanced("session", "s-1", "status", 1, {}, 1)
            crew_log_bus.publish(crew_log_bus.FOLD_ADVANCED, slot_event)
            crew_log_bus.publish(crew_log_bus.FOLD_ADVANCED, session_event)
            assert slot_calls == [slot_event]
            assert session_calls == [session_event]

            first.unsubscribe_bus()
            first.unsubscribe_bus()
            assert crew_log_bus.subscriber_count(crew_log_bus.FOLD_ADVANCED) == 0
    finally:
        crew_log_bus.reset_for_tests()


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
    # A stand-in pass, so the positive half below hands the loop no real coroutine.
    flush = MagicMock(return_value="the-flush-pass")
    publisher._flush = flush
    publisher._flushing = True
    publisher._scheduled = True
    publisher._run()
    loop.create_task.assert_not_called()
    flush.assert_not_called()
    assert publisher._flushing is True
    assert publisher._scheduled is False
    # With no pass in flight the same call does start one.
    publisher._flushing = False
    publisher._run()
    loop.create_task.assert_called_once_with("the-flush-pass")
    loop.create_task.return_value.add_done_callback.assert_called_once_with(publisher._finished)


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
    loop.call_later.assert_called_once_with(routes.COALESCE_SECONDS, publisher._run)


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
    state.frames.clear()

    # Same session id, the warm memo now holding a bundle whose ORIGIN names a retired
    # file at the same terminal seq: the recreated-log shape without touching the
    # filesystem, so it behaves the same on every platform.
    key = (str(crew_log.data_home()), SESSION)
    held = crew_log._session_memos[key]
    crew_log._session_memos[key] = held._replace(
        bundle=crew_log.SessionProjections(
            session_id=SESSION,
            last_seq=held.bundle.last_seq,
            checkpoints=held.bundle.checkpoints,
            origin="a-retired-file",
        )
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
    those sessions did grow, and a pass for them is armed on the loop now serving.
    """
    publisher = routes.CrewLogPublisher(_Sockets())
    publisher.bind(asyncio.get_running_loop())
    publisher._scheduled = True
    publisher._flushing = True
    publisher._sessions_armed = True
    publisher._dirty.add(SESSION)

    publisher.bind(asyncio.get_running_loop(), _Sockets())

    assert publisher._flushing is False
    assert publisher._sessions_armed is False, "nothing is held, so no window is armed"
    assert publisher._scheduled is True, "the kept dirty set gets a pass on this loop"
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
    """A launch with the flag off must not pay for the subsystem it will not use.

    The installer runs on the gateway's boot path. With the crew log off it returns
    without importing the emitter and without constructing a publisher, so a
    disabled launch does no optional work and registers no listener.
    """
    monkeypatch.setenv(routes.CREW_LOG_ENV, "0")
    monkeypatch.setattr(routes, "_publisher", None)
    from kiro_crew.crew_log import emit as crew_log_emit

    with patch.object(crew_log_emit, "_growth_listeners", []):
        assert routes.install_crew_log_publisher(_Sockets()) is None
        assert crew_log_emit._growth_listeners == []
    assert routes._publisher is None


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


def test_the_publisher_is_installed_after_the_listener_binds():
    """Installing it imports and builds the publisher, so it stays off the boot path.

    ``no-new-work-on-gateway-boot-path``: everything before ``_start_site`` is paid by
    every launch, and the crew log is on by default. ``start_dashboard`` binds with
    ``await site.start()`` on its pre-reserved socket.
    """
    import inspect

    from kiro_crew.dashboard import server

    source = inspect.getsource(server.start_dashboard)
    # The boot phases it calls before the bind live in its server_runtime owners.
    owners = sorted((Path(server.__file__).parent / "server_runtime").glob("[!_]*.py"))
    assert owners, "expected the server_runtime owners beside server.py"
    phases = "".join(path.read_text(encoding="utf-8") for path in owners)
    bind = source.index("await site.start()")
    assert (source + phases).count("install_crew_log_publisher(") == 1
    assert source.index("install_crew_log_publisher(") > bind
