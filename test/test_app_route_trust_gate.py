"""App-token callers on ``/api/approvals*`` and ``/api/sessions*`` follow the app trust model.

Every request here is a real app (or dashboard) token through the real
``token_auth_middleware`` and the real route table (``register_all``). Only the
manifest lookup is patched, so each app's declared ``permissions.api`` and
``permissions.sessionApproval`` come from an ``AppManifest`` built by the real
``from_dict``.

The rules pinned:

* ``/api/approvals``: the slot approve route's rule. No ``sessionApproval``
  grant is the same 403, even on the app's own slot; with it, an app resolves an
  approval only on a slot it may control (its own, or a local user session), the
  id must name exactly one such pending request, a state-level approval never
  resolves, and the list shows an app only what it could resolve.
* ``/api/sessions``: an app lists, searches, reads and deletes only transcripts
  whose metadata records it as the owner app, and a delete never pops a live slot
  the app does not own. Summarize follows the request's app claim, so an
  internal-secret caller from an app's slot is narrowed the same way. An app
  cannot open a new slot (create, send or resume) over a transcript it does not
  own, since that slot's save would rewrite the owner marker.
* Every other app refusal is the uniform 404 a missing target also returns.
* The dashboard user is unchanged on every route.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all
from kiro_crew.dashboard.state import SlotOrigin

_NOT_FOUND = {"error": "not found", "code": "slot_not_found"}
#: Bound on awaiting a state-level approval task the test itself settles. Its
#: own timeout is ``_APPROVAL_TIMEOUT`` (hours), so a regression that leaves it
#: pending must fail here by name, well inside pytest-timeout.
_SETTLE = 5.0
_NO_GRANT = {"error": "app cannot manage session approvals", "code": "session_approval_not_granted"}

_API = [
    "/api/approvals",
    "/api/approvals/*",
    "/api/sessions",
    "/api/sessions/*",
    "/api/chat",
    "/api/chat/*",
]

#: Declares the routes but holds no session grant (the ops-mission-control shape).
_PLAIN = "plain-app"
#: Declares the routes and the user-consented session grant.
_GRANTED = "granted-app"
#: A sibling app, to own slots and transcripts nobody else may touch.
_OTHER = "other-app"


def _manifest(name: str, *, session_approval: bool = False) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "1.0.0",
            "displayName": name,
            "description": "route trust gate fixture",
            "permissions": {"api": list(_API), "sessionApproval": session_approval},
        }
    )


_MANIFESTS = {
    _PLAIN: _manifest(_PLAIN),
    _GRANTED: _manifest(_GRANTED, session_approval=True),
    _OTHER: _manifest(_OTHER),
}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"app-route-trust-gate-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.get_app_manifest", _MANIFESTS.get)
    monkeypatch.setattr("kiro_crew.apps.permissions.is_app_enabled", _MANIFESTS.__contains__)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    # No cron store: the delete path's owner sweep reads it, and a bare mock is
    # not awaitable. Cron ownership is not what these tests decide.
    st.crons = None
    return st


@contextlib.asynccontextmanager
async def _serve(state):
    """A client for the real auth middleware and the real route table."""
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    async with TestClient(TestServer(app)) as client:
        yield client


def _q(app_name: str = "") -> dict[str, str]:
    """Query params carrying an app token, or the dashboard user's when *app_name* is empty."""
    return {"token": token_auth.generate_token("local-app", app=app_name)}


def _native_approval(state, slot_key: str, request_id: str, **slot_kwargs):
    """Park a pending native tool approval on *slot_key* and return its future."""
    slot = state.get_or_create_slot(slot_key, **slot_kwargs)
    row = slot.append(
        "permission",
        "Run shell command",
        json.dumps({"request_id": request_id, "tool_input": "ls"}),
    )
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    slot.register_approval(request_id, future, row)
    return future


async def _state_approval(state, approval_id: str, slot: str = "user-tab"):
    """Raise a state-level (background) approval and wait until it is listed."""
    task = asyncio.ensure_future(
        state.request_approval(approval_id, "subagent", "spawn_run", tool_input="x", slot=slot)
    )
    for _ in range(100):
        if approval_id in state._pending_approvals:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("state-level approval was never registered")


def _user_slot(state, key: str = "user-tab"):
    return state.get_or_create_slot(key, origin=SlotOrigin.USER)


def _app_slot(state, key: str, app_name: str):
    return state.get_or_create_slot(key, app=app_name, origin=SlotOrigin.APP)


# ── /api/approvals: list ──


@pytest.mark.asyncio
async def test_app_list_omits_state_level_approvals(state, monkeypatch):
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: audit)
    _user_slot(state)
    task = await _state_approval(state, "spawn:list")
    async with _serve(state) as client:
        try:
            for app_name in (_PLAIN, _GRANTED):
                resp = await client.get("/api/approvals", params=_q(app_name))
                assert resp.status == 200
                assert await resp.read() == b"[]"
        finally:
            state.resolve_state_approval("spawn:list", False)
            await asyncio.wait_for(task, _SETTLE)
    assert [
        c.kwargs
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("source") == "app_isolation"
    ] == [
        {
            "caller": app_name,
            "operation": "approval_list",
            "outcome": "denied",
            "source": "app_isolation",
            "resources": "approvals=*",
            "error": "state-level approvals are not app-visible",
        }
        for app_name in (_PLAIN, _GRANTED)
    ]


@pytest.mark.asyncio
async def test_dashboard_user_still_lists_every_approval(state):
    _user_slot(state)
    task = await _state_approval(state, "spawn:dash")
    async with _serve(state) as client:
        try:
            resp = await client.get("/api/approvals", params=_q())
            assert resp.status == 200
            assert [a["id"] for a in await resp.json()] == ["spawn:dash"]
        finally:
            state.resolve_state_approval("spawn:dash", False)
            await asyncio.wait_for(task, _SETTLE)


# ── /api/approvals: resolve ──


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", [_GRANTED])
async def test_app_never_resolves_a_state_level_approval(state, app_name):
    _user_slot(state)
    task = await _state_approval(state, "spawn:bg")
    record = state._pending_approvals["spawn:bg"]
    async with _serve(state) as client:
        try:
            resp = await client.post("/api/approvals/spawn:bg/approve", params=_q(app_name))
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
            # The coordinator-target form, echoing the record exactly, is refused too.
            target = {
                "origin": "coordinator",
                "slot": record["slot"],
                "instance": record["instance"],
            }
            resp = await client.post(
                "/api/approvals/spawn:bg/approve", params={**_q(app_name), **target}
            )
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
            assert not task.done()
        finally:
            state.resolve_state_approval("spawn:bg", False)
            assert await asyncio.wait_for(task, _SETTLE) is False


@pytest.mark.asyncio
async def test_app_without_grant_cannot_resolve_user_session_approval(state):
    future = _native_approval(state, "user-tab", "7", origin=SlotOrigin.USER)
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/7/approve", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (403, _NO_GRANT)
        assert not future.done()


@pytest.mark.asyncio
async def test_app_with_grant_resolves_user_session_approval(state):
    future = _native_approval(state, "user-tab", "7", origin=SlotOrigin.USER)
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/7/reject_once", params=_q(_GRANTED))
        assert (resp.status, await resp.json()) == (200, {"ok": True})
        assert future.result() == "rejected_once"


@pytest.mark.asyncio
async def test_app_without_grant_is_refused_its_own_slot_exactly_like_the_slot_route(state):
    """Both approve routes demand ``sessionApproval`` before they look at the slot."""
    future = _native_approval(state, "plain-own", "8", app=_PLAIN, origin=SlotOrigin.APP)
    async with _serve(state) as client:
        by_id = await client.post("/api/approvals/8/approve", params=_q(_PLAIN))
        by_slot = await client.post(
            "/api/chat/slots/plain-own/approve",
            params=_q(_PLAIN),
            json={"action": "approved", "request_id": "8"},
        )
        assert (by_id.status, await by_id.json()) == (403, _NO_GRANT)
        assert (by_slot.status, await by_slot.json()) == (403, _NO_GRANT)
        assert not future.done()


@pytest.mark.asyncio
async def test_app_with_grant_resolves_its_own_slot_approval(state):
    future = _native_approval(state, "granted-own", "8", app=_GRANTED, origin=SlotOrigin.APP)
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/8/approve", params=_q(_GRANTED))
        assert (resp.status, await resp.json()) == (200, {"ok": True})
        assert future.result() == "approved"


@pytest.mark.asyncio
async def test_app_cannot_resolve_another_apps_slot_approval(state):
    future = _native_approval(state, "other-own", "9", app=_OTHER, origin=SlotOrigin.APP)
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/9/approve", params=_q(_GRANTED))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert not future.done()


@pytest.mark.asyncio
async def test_app_refusal_matches_a_missing_approval_byte_for_byte(state):
    _native_approval(state, "other-own", "9", app=_OTHER, origin=SlotOrigin.APP)
    async with _serve(state) as client:
        refused = await client.post("/api/approvals/9/approve", params=_q(_GRANTED))
        missing = await client.post("/api/approvals/no-such-id/approve", params=_q(_GRANTED))
        assert refused.status == missing.status == 404
        assert await refused.read() == await missing.read()


@pytest.mark.asyncio
async def test_app_resolves_its_own_slot_when_the_id_also_names_a_foreign_approval(state):
    """Request ids recur: ids the app cannot control do not make its own one ambiguous."""
    foreign = _native_approval(state, "other-own", "3", app=_OTHER, origin=SlotOrigin.APP)
    _user_slot(state)
    task = await _state_approval(state, "3")
    own = _native_approval(state, "granted-own", "3", app=_GRANTED, origin=SlotOrigin.APP)
    async with _serve(state) as client:
        try:
            resp = await client.post("/api/approvals/3/approve", params=_q(_GRANTED))
            assert (resp.status, await resp.json()) == (200, {"ok": True})
            assert own.result() == "approved"
            assert not foreign.done()
            assert not task.done()
        finally:
            state.resolve_state_approval("3", False)
            await asyncio.wait_for(task, _SETTLE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,second",
    [
        (("user-tab", {"origin": SlotOrigin.USER}), ("granted-own", {"app": _GRANTED})),
        (("granted-a", {"app": _GRANTED}), ("granted-b", {"app": _GRANTED})),
    ],
    ids=["user-session-and-own", "two-own"],
)
async def test_app_is_refused_an_id_pending_on_two_sessions_it_may_control(state, first, second):
    """An ambiguous id is refused, never resolved on whichever slot iterates first."""
    futures = []
    for key, kwargs in (first, second):
        kwargs = {"origin": SlotOrigin.APP, **kwargs}
        futures.append(_native_approval(state, key, "3", **kwargs))
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/3/approve", params=_q(_GRANTED))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    assert not any(f.done() for f in futures)


@pytest.mark.asyncio
async def test_app_decision_never_falls_through_to_a_same_id_sibling(state, monkeypatch):
    """A request settled during the awaited permission read is not replaced by a sibling."""
    from kiro_crew.dashboard import chat_handlers

    user = _native_approval(state, "user-tab", "5", origin=SlotOrigin.USER)
    own = _native_approval(state, "granted-own", "5", app=_GRANTED, origin=SlotOrigin.APP)
    judge = chat_handlers._app_may_send_to_slot

    async def _settle_user_first(request_app, slot):
        if slot.key == "user-tab" and not user.done():
            user.set_result("rejected")  # the user answered their own card meanwhile
        return await judge(request_app, slot)

    monkeypatch.setattr(chat_handlers, "_app_may_send_to_slot", _settle_user_first)
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/5/approve", params=_q(_GRANTED))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    assert user.result() == "rejected"
    assert not own.done()


@pytest.mark.asyncio
async def test_app_decision_never_lands_on_a_same_id_request_raised_on_the_same_slot(
    state, monkeypatch
):
    """Ids recur within one slot too: only the request judged is ever resolved."""
    from kiro_crew.dashboard import chat_handlers

    judged = _native_approval(state, "user-tab", "7", origin=SlotOrigin.USER)
    newer: list[asyncio.Future[str]] = []
    judge = chat_handlers._app_may_send_to_slot

    async def _recur_during_read(request_app, slot):
        if not newer:
            # The user answers the card, and the provider raises a new "7" there.
            judged.set_result("rejected")
            newer.append(_native_approval(state, "user-tab", "7", origin=SlotOrigin.USER))
        return await judge(request_app, slot)

    monkeypatch.setattr(chat_handlers, "_app_may_send_to_slot", _recur_during_read)
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/7/approve", params=_q(_GRANTED))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    assert judged.result() == "rejected"
    assert not newer[0].done()


@pytest.mark.asyncio
async def test_app_resolution_is_audited_under_the_apps_name(state, monkeypatch):
    """The id route names the app in the SEL, exactly as the slot route does."""
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: audit)
    future = _native_approval(state, "user-tab", "7", origin=SlotOrigin.USER)
    async with _serve(state) as client:
        resp = await client.post("/api/approvals/7/reject", params=_q(_GRANTED))
        assert (resp.status, await resp.json()) == (200, {"ok": True})
    assert future.result() == "rejected"
    audit.log_api_access.assert_any_call(
        caller=f"app:{_GRANTED}",
        operation="tool_approval:reject",
        outcome="rejected",
        resources="7",
    )


@pytest.mark.asyncio
async def test_dashboard_user_still_resolves_every_approval(state):
    native = _native_approval(state, "user-tab", "7", origin=SlotOrigin.USER)
    app_owned = _native_approval(state, "other-own", "9", app=_OTHER, origin=SlotOrigin.APP)
    task = await _state_approval(state, "spawn:dash")
    async with _serve(state) as client:
        for approval_id in ("7", "9", "spawn:dash"):
            resp = await client.post(f"/api/approvals/{approval_id}/approve", params=_q())
            assert (resp.status, await resp.json()) == (200, {"ok": True})
        assert native.result() == "approved"
        assert app_owned.result() == "approved"
        assert await asyncio.wait_for(task, _SETTLE) is True


# ── /api/sessions ──


def _transcript(state, key: str, text: str, app_name: str = "") -> None:
    log = state.conversation_log
    log.append(key, "user", text)
    log.append(key, "assistant", "noted")
    if app_name:
        log.update_metadata(key, {"app": app_name})


def _seed_transcripts(state) -> None:
    _transcript(state, "dashboard:user-chat", "user needle secret")
    _transcript(state, "dashboard:plain-chat", "plain needle note", _PLAIN)
    _transcript(state, "dashboard:other-chat", "other needle note", _OTHER)


def _keys(rows: list[dict]) -> set[str]:
    return {r["key"] for r in rows}


@pytest.mark.asyncio
async def test_app_session_list_holds_only_its_own_transcripts(state):
    _seed_transcripts(state)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions", params=_q(_PLAIN))
        assert resp.status == 200
        body = await resp.json()
        assert _keys(body["sessions"]) == {"dashboard_plain-chat"}
        assert (body["total"], body["has_more"]) == (1, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [0, 1])
async def test_app_list_rejudges_ownership_with_the_preview(state, monkeypatch, remaining):
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    log = state.conversation_log
    key = "dashboard_plain-chat"
    await asyncio.to_thread(_transcript, state, key, "plain note", _PLAIN)
    if remaining:
        await asyncio.to_thread(_transcript, state, "dashboard_plain-next", "next note", _PLAIN)
    real_filter = sessions_mod._app_owned_sessions
    replaced = []

    def _filter_then_replace(*args, **kwargs):
        rows = real_filter(*args, **kwargs)
        assert key in _keys(rows)
        rows.sort(key=lambda row: row["key"] != key)
        assert log.delete_session(key)
        _transcript(state, key, "foreign secret", _OTHER)
        log.append(key, "assistant", "foreign preview")
        replaced.append(key)
        return rows

    preview = MagicMock(wraps=log.last_message_preview)
    monkeypatch.setattr(sessions_mod, "_app_owned_sessions", _filter_then_replace)
    monkeypatch.setattr(log, "last_message_preview", preview)
    async with _serve(state) as client:
        resp = await client.get(
            "/api/sessions", params={**_q(_PLAIN), "preview": "1", "limit": "1"}
        )
        assert replaced == [key], "the replacement must land after ownership filtering"
        assert resp.status == 200
        assert await resp.json() == {
            "sessions": [],
            "total": remaining,
            "has_more": bool(remaining),
        }
    preview.assert_not_called()


@pytest.mark.asyncio
async def test_app_older_list_rejudges_ownership_before_testing_content(state, monkeypatch):
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    log = state.conversation_log
    key = "dashboard_plain-chat"
    await asyncio.to_thread(_transcript, state, key, "", _PLAIN)
    real_filter = sessions_mod._app_owned_sessions
    replaced = []

    def _filter_then_replace(*args, **kwargs):
        rows = real_filter(*args, **kwargs)
        assert rows[0]["title"] == key  # force the has_messages branch
        assert log.delete_session(key)
        _transcript(state, key, "foreign secret", _OTHER)
        replaced.append(key)
        return rows

    has_messages = MagicMock(wraps=log.has_messages)
    monkeypatch.setattr(sessions_mod, "_app_owned_sessions", _filter_then_replace)
    monkeypatch.setattr(log, "has_messages", has_messages)
    async with _serve(state) as client:
        resp = await client.get(
            "/api/sessions", params={**_q(_PLAIN), "user_only": "1", "exclude_open": "1"}
        )
        assert replaced == [key]
        assert resp.status == 200
        assert await resp.json() == {"sessions": [], "total": 0, "has_more": False}
    has_messages.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", [_PLAIN, ""], ids=["app", "dashboard"])
@pytest.mark.parametrize("stage", ["filter", "preview", "user_only"])
async def test_list_lock_timeout_is_restricted_only(state, monkeypatch, app_name, stage):
    from kiro_crew.dashboard.handlers import sessions as sessions_mod
    from kiro_crew.history import HistoryLockTimeout

    key = "dashboard_plain-chat"
    _transcript(state, key, "plain note", _PLAIN)
    log = state.conversation_log
    real_filter = sessions_mod._app_owned_sessions
    lock = MagicMock(side_effect=HistoryLockTimeout("transcript busy"))

    def _filter_then_contend(*args, **kwargs):
        rows = real_filter(*args, **kwargs)
        monkeypatch.setattr(log, "_locked", lock)
        return rows

    if app_name and stage != "filter":
        monkeypatch.setattr(sessions_mod, "_app_owned_sessions", _filter_then_contend)
    else:
        monkeypatch.setattr(log, "_locked", lock)
    params = _q(app_name)
    if stage != "filter":
        params[stage] = "1"
    async with _serve(state) as client:
        resp = await client.get("/api/sessions", params=params)
        assert resp.status == 200
        body = await resp.json()
    if app_name:
        assert body == {"sessions": [], "total": 0, "has_more": False}
        lock.assert_called_once_with(key)
    else:
        assert _keys(body["sessions"]) == {key}
        assert (body["total"], body["has_more"]) == (1, False)
        if stage == "preview":
            assert body["sessions"][0]["preview"] == "noted"
        lock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [{}, {"preview": "1"}, {"user_only": "1"}, {"q": "needle"}])
async def test_app_catalog_does_not_return_metadata_from_a_foreign_incarnation(
    state, monkeypatch, query
):
    log = state.conversation_log
    key = "dashboard_plain-chat"
    _transcript(state, key, "foreign needle secret " * 100, _OTHER)
    real_list = log.list_sessions
    replaced = []

    def _list_then_replace(*args, **kwargs):
        rows = real_list(*args, **kwargs)
        if not replaced:
            assert log.delete_session(key)
            _transcript(state, key, "plain needle note", _PLAIN)
            replaced.append(key)
        return rows

    monkeypatch.setattr(log, "list_sessions", _list_then_replace)
    route = "/api/sessions/search" if "q" in query else "/api/sessions"
    async with _serve(state) as client:
        resp = await client.get(route, params={**_q(_PLAIN), **query})
        assert resp.status == 200
        rows = (await resp.json())["sessions"]
    assert replaced == [key]
    assert _keys(rows) == {key}
    current = real_list()[0]
    for field in ("title", "messages", "created", "modified"):
        assert rows[0][field] == current[field]
    assert "foreign" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_app_list_metadata_and_preview_share_an_off_loop_hold(state, monkeypatch):
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    log = state.conversation_log
    key = "dashboard_plain-chat"
    await asyncio.to_thread(_transcript, state, key, "plain note", _PLAIN)
    await asyncio.to_thread(_transcript, state, "dashboard_plain-two", "second note", _PLAIN)
    real_lock = log._locked
    real_judge = sessions_mod._app_owns_transcript
    real_list = log.list_sessions
    real_preview = log.last_message_preview
    holds = []
    events = []

    @contextlib.contextmanager
    def _track_hold(candidate):
        assert not holds, "the loop must release one row before locking the next"
        with real_lock(candidate):
            holds.append(object())
            try:
                yield
            finally:
                holds.pop()

    def _record(event):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        events.append((event, holds[-1] if holds else None))

    def _judge(*args):
        _record("judge")
        return real_judge(*args)

    def _list(*args, **kwargs):
        _record("metadata")
        return real_list(*args, **kwargs)

    def _preview(*args, **kwargs):
        _record("preview")
        return real_preview(*args, **kwargs)

    monkeypatch.setattr(log, "_locked", _track_hold)
    monkeypatch.setattr(sessions_mod, "_app_owns_transcript", _judge)
    monkeypatch.setattr(log, "list_sessions", _list)
    monkeypatch.setattr(log, "last_message_preview", _preview)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions", params={**_q(_PLAIN), "preview": "1"})
        assert resp.status == 200
        rows = (await resp.json())["sessions"]
        assert _keys(rows) == {key, "dashboard_plain-two"}
        assert all(row["preview"] == "noted" for row in rows)
    assert [event for event, _hold in events[-6:]] == ["judge", "metadata", "preview"] * 2
    assert events[-1][1] is not None
    assert events[-3][1] is events[-2][1] is events[-1][1]
    assert events[-6][1] is events[-5][1] is events[-4][1]
    assert events[-1][1] is not events[-4][1]
    assert not holds


@pytest.mark.asyncio
async def test_app_session_search_holds_only_its_own_transcripts(state):
    _seed_transcripts(state)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/search", params={**_q(_PLAIN), "q": "needle"})
        assert resp.status == 200
        assert _keys((await resp.json())["sessions"]) == {"dashboard_plain-chat"}


@pytest.mark.asyncio
@pytest.mark.parametrize("title_only", [False, True], ids=["snippet", "title-only"])
async def test_app_search_rejudges_ownership_with_the_snippet(state, monkeypatch, title_only):
    """A same-key replacement after filtering must yield neither row nor snippet."""
    log = state.conversation_log
    key = "dashboard_plain-chat"
    _transcript(state, key, "plain note" if title_only else "plain needle note", _PLAIN)
    log.update_metadata(key, {"title": "needle"})
    catalog = log._catalog_projection
    real_fold = catalog._folded_for
    replaced = []

    def _fold_then_replace(candidate, rowids):
        folded = real_fold(candidate, rowids)
        assert log.delete_session(candidate)
        _transcript(state, candidate, "foreign needle secret", _OTHER)
        replaced.append(candidate)
        return folded

    snippet = MagicMock(wraps=log._content_snippet)
    monkeypatch.setattr(catalog, "_folded_for", _fold_then_replace)
    monkeypatch.setattr(log, "_content_snippet", snippet)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/search", params={**_q(_PLAIN), "q": "needle"})
        assert replaced == [key], "the replacement must land after the window filter"
        assert resp.status == 200
        assert (await resp.json())["sessions"] == []
    snippet.assert_not_called()


@pytest.mark.asyncio
async def test_app_search_checks_ownership_and_snippet_under_one_hold(state, monkeypatch):
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    _transcript(state, "dashboard:plain-chat", "plain needle note", _PLAIN)
    log = state.conversation_log
    real_lock = log._locked
    real_judge = sessions_mod._app_owns_transcript
    real_snippet = log._content_snippet
    holds = []
    events = []

    @contextlib.contextmanager
    def _track_hold(key):
        with real_lock(key):
            holds.append(object())
            try:
                yield
            finally:
                holds.pop()

    def _judge(*args):
        events.append(("judge", holds[-1] if holds else None))
        return real_judge(*args)

    def _snippet(*args):
        events.append(("snippet", holds[-1] if holds else None))
        return real_snippet(*args)

    monkeypatch.setattr(log, "_locked", _track_hold)
    monkeypatch.setattr(sessions_mod, "_app_owns_transcript", _judge)
    monkeypatch.setattr(log, "_content_snippet", _snippet)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/search", params={**_q(_PLAIN), "q": "needle"})
        assert resp.status == 200
        rows = (await resp.json())["sessions"]
        assert _keys(rows) == {"dashboard_plain-chat"}
        assert rows[0]["snippet"] == "plain needle note"
    assert [event for event, _hold in events] == ["judge", "judge", "snippet"]
    assert events[0][1] is None  # the initial window filter is still lock-free
    assert events[1][1] is not None
    assert events[1][1] is events[2][1]  # no release between verdict and read
    assert not holds


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", [_PLAIN, ""], ids=["app", "unrestricted"])
async def test_search_output_lock_timeout_is_restricted_only(state, monkeypatch, app_name):
    from kiro_crew.history import HistoryLockTimeout

    _transcript(state, "dashboard:plain-chat", "plain needle note", _PLAIN)
    lock = MagicMock(side_effect=HistoryLockTimeout("transcript busy"))
    monkeypatch.setattr(state.conversation_log, "_locked", lock)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/search", params={**_q(app_name), "q": "needle"})
        assert resp.status == 200
        rows = (await resp.json())["sessions"]
    if app_name:
        assert rows == []
        lock.assert_called_once_with("dashboard_plain-chat")
    else:
        assert _keys(rows) == {"dashboard_plain-chat"}
        assert rows[0]["snippet"] == "plain needle note"
        lock.assert_not_called()


@pytest.mark.asyncio
async def test_app_search_hits_are_not_crowded_out_by_other_sessions(state):
    """Ownership filters before the ranking's cap, not after it."""
    _transcript(state, "dashboard:plain-chat", "plain needle note", _PLAIN)
    for i in range(3):
        _transcript(state, f"dashboard:user-{i}", f"user needle needle needle {i}")
    async with _serve(state) as client:
        params = {**_q(_PLAIN), "q": "needle", "limit": "2"}
        resp = await client.get("/api/sessions/search", params=params)
        assert resp.status == 200
        assert _keys((await resp.json())["sessions"]) == {"dashboard_plain-chat"}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["dashboard:user-chat", "dashboard:other-chat", "dashboard:none"])
async def test_app_cannot_read_a_transcript_it_does_not_own(state, key):
    _seed_transcripts(state)
    async with _serve(state) as client:
        resp = await client.get(f"/api/sessions/{key}", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_app_reads_its_own_transcript(state):
    _seed_transcripts(state)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/dashboard:plain-chat", params=_q(_PLAIN))
        assert resp.status == 200
        assert [m["content"] for m in await resp.json()] == ["plain needle note", "noted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["dashboard:user-chat", "dashboard:other-chat"])
async def test_app_cannot_delete_a_transcript_it_does_not_own(state, key):
    _seed_transcripts(state)
    async with _serve(state) as client:
        resp = await client.delete(f"/api/sessions/{key}", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert state.conversation_log.has_log(key)


@pytest.mark.asyncio
async def test_app_deletes_its_own_transcript_and_slot(state):
    _app_slot(state, "plain-chat", _PLAIN)
    _transcript(state, "dashboard:plain-chat", "plain note", _PLAIN)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions/dashboard:plain-chat", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (200, {"ok": True})
        assert not state.conversation_log.has_log("dashboard:plain-chat")
        assert "plain-chat" not in state._slots


@pytest.mark.asyncio
async def test_app_delete_never_pops_a_live_slot_it_does_not_own(state):
    """The live slot is the server-side record: a transcript claiming the app does not override it."""
    _user_slot(state, "user-chat")
    _transcript(state, "dashboard:user-chat", "user note", _PLAIN)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions/dashboard:user-chat", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert "user-chat" in state._slots
        assert state.conversation_log.has_log("dashboard:user-chat")


@pytest.fixture
def replaced_after_check(state, monkeypatch):
    """Hand *key*'s transcript to another app right after the route's first ownership check.

    The stand-in for a same-key delete and recreate landing between that check
    and the protected read or unlink: the line now records ``_OTHER``.
    """
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    real = sessions_mod._app_transcript_refusal

    async def _check_then_replace(request, st, key, operation):
        refusal = await real(request, st, key, operation)
        if refusal is None:
            st.conversation_log.update_metadata(key, {"app": _OTHER})
        return refusal

    monkeypatch.setattr(sessions_mod, "_app_transcript_refusal", _check_then_replace)


@pytest.mark.asyncio
async def test_app_read_rejudges_ownership_with_the_read(state, replaced_after_check):
    _transcript(state, "dashboard:plain-chat", "plain note", _PLAIN)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions/dashboard:plain-chat", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_app_delete_rejudges_ownership_inside_the_unlink_hold(state, replaced_after_check):
    _transcript(state, "dashboard:plain-chat", "plain note", _PLAIN)
    async with _serve(state) as client:
        resp = await client.delete("/api/sessions/dashboard:plain-chat", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    assert state.conversation_log.has_log("dashboard:plain-chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,expected", [(_PLAIN, "plain summary"), (_OTHER, "")])
async def test_app_summary_rejudges_ownership_inside_each_read(state, monkeypatch, owner, expected):
    """``_summarize_one`` itself refuses a transcript the app stopped owning."""
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    prompts: list[str] = []

    async def _model(_sessions, prompt, **_kwargs):
        prompts.append(prompt)
        return "plain summary"

    monkeypatch.setattr(sessions_mod, "run_bg_oneliner", _model)
    _transcript(state, "dashboard:plain-chat", "plain note", owner)
    summary = await sessions_mod._summarize_one(state, "dashboard:plain-chat", owner_app=_PLAIN)
    assert summary == expected
    assert len(prompts) == (1 if expected else 0)


@pytest.mark.asyncio
async def test_app_cached_summary_is_not_served_for_a_transcript_it_does_not_own(
    state, monkeypatch
):
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    async def _model(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the model was asked to summarize a foreign transcript")

    monkeypatch.setattr(sessions_mod, "run_bg_oneliner", _model)
    key = "dashboard:other-chat"
    _transcript(state, key, "other note", _OTHER)
    log = state.conversation_log
    log.set_cached_summary(
        key, "other summary", log.session_mtime(key), log.rotation_generation(key)
    )
    assert await sessions_mod._summarize_one(state, key) == "other summary"
    assert await sessions_mod._summarize_one(state, key, owner_app=_PLAIN) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["cache", "rows"])
async def test_app_summary_ownership_loss_is_audited(state, monkeypatch, stage):
    """A late ownership loss records one denial even after the route's initial grant."""
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: audit)
    key = "dashboard:plain-chat"
    await asyncio.to_thread(_transcript, state, key, "plain note", _PLAIN)
    log = state.conversation_log
    if stage == "cache":
        summarize = sessions_mod._summarize_one

        async def _replace_before_summary(*args, **kwargs):
            await asyncio.to_thread(log.update_metadata, key, {"app": _OTHER})
            return await summarize(*args, **kwargs)

        monkeypatch.setattr(sessions_mod, "_summarize_one", _replace_before_summary)
    else:
        read_cache = log.get_cached_summary

        def _replace_after_cache(key):
            cached = read_cache(key)
            log.update_metadata(key, {"app": _OTHER})
            return cached

        monkeypatch.setattr(log, "get_cached_summary", _replace_after_cache)
    model = MagicMock(side_effect=AssertionError("foreign transcript reached the model"))
    monkeypatch.setattr(sessions_mod, "run_bg_oneliner", model)
    async with _serve(state) as client:
        resp = await client.post("/api/sessions/summarize", params=_q(_PLAIN), json={"keys": [key]})
        assert (resp.status, await resp.read()) == (200, b'{"summaries": {}}')
    model.assert_not_called()
    assert [
        c.kwargs
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("source") == "app_isolation" and c.kwargs.get("outcome") == "denied"
    ] == [
        {
            "caller": _PLAIN,
            "operation": "session_summarize",
            "outcome": "denied",
            "source": "app_isolation",
            "resources": f"session={key}",
            "error": "app does not own this transcript",
        }
    ]


@pytest.mark.asyncio
async def test_app_ownership_grants_are_audited(state, monkeypatch):
    """Every granted ownership decision leaves an ``allowed`` SEL record, like the refusals."""
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: audit)
    _seed_transcripts(state)
    async with _serve(state) as client:
        resp = await client.get("/api/sessions", params=_q(_PLAIN))
        assert resp.status == 200
        resp = await client.get("/api/sessions/search", params={**_q(_PLAIN), "q": "needle"})
        assert resp.status == 200
        resp = await client.get("/api/sessions/dashboard:plain-chat", params=_q(_PLAIN))
        assert resp.status == 200
        resp = await client.post("/api/chat/slots", params=_q(_PLAIN), json={"name": "fresh"})
        assert resp.status == 200
        resp = await client.delete("/api/sessions/dashboard:plain-chat", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (200, {"ok": True})
    grants = {
        (c.kwargs["operation"], c.kwargs["resources"])
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "allowed" and c.kwargs.get("source") == "app_isolation"
    }
    assert grants == {
        ("session_list", "sessions=1"),
        ("session_search", "sessions=1"),
        ("session_detail", "session=dashboard:plain-chat"),
        ("chat_slot_create", "session=dashboard:fresh"),
        ("session_delete", "session=dashboard:plain-chat"),
    }
    assert {
        c.kwargs["caller"]
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("source") == "app_isolation"
    } == {_PLAIN}


def _claim_requests(key: str):
    """Every route that can open a NEW app slot on *key*'s transcript."""
    name = key.removeprefix("dashboard:")
    return [
        ("POST", "/api/chat/slots", {"name": name}),
        ("POST", "/api/chat", {"slot": name, "message": "hello"}),
        ("POST", f"/api/chat/slots/{name}/resume", {"key": key}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["dashboard:user-chat", "dashboard:other-chat"])
@pytest.mark.parametrize("route", [0, 1, 2], ids=["create", "send", "resume"])
async def test_app_cannot_open_a_slot_over_a_transcript_it_does_not_own(state, key, route):
    """A new app slot would stamp the app onto the transcript's metadata on save."""
    _seed_transcripts(state)
    before = state.conversation_log.get_metadata(key)
    method, path, body = _claim_requests(key)[route]
    async with _serve(state) as client:
        resp = await client.request(method, path, params=_q(_PLAIN), json=body)
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
        assert key.removeprefix("dashboard:") not in state._slots
        resp = await client.get(f"/api/sessions/{key}", params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    assert state.conversation_log.get_metadata(key) == before


@pytest.mark.asyncio
async def test_app_reopens_its_own_transcript_and_names_a_fresh_one(state):
    _seed_transcripts(state)
    async with _serve(state) as client:
        for name in ("plain-chat", "brand-new"):
            resp = await client.post("/api/chat/slots", params=_q(_PLAIN), json={"name": name})
            assert resp.status == 200
            assert state._slots[name]._app == _PLAIN


@pytest.mark.asyncio
async def test_dashboard_user_still_reaches_every_transcript(state):
    _seed_transcripts(state)
    every = {"dashboard_user-chat", "dashboard_plain-chat", "dashboard_other-chat"}
    async with _serve(state) as client:
        resp = await client.get("/api/sessions", params=_q())
        assert _keys((await resp.json())["sessions"]) == every
        resp = await client.get("/api/sessions/search", params={**_q(), "q": "needle"})
        assert _keys((await resp.json())["sessions"]) == every
        resp = await client.get("/api/sessions/dashboard:other-chat", params=_q())
        assert [m["content"] for m in await resp.json()] == ["other needle note", "noted"]
        resp = await client.delete("/api/sessions/dashboard:user-chat", params=_q())
        assert (resp.status, await resp.json()) == (200, {"ok": True})
        assert not state.conversation_log.has_log("dashboard:user-chat")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path", [("DELETE", "/api/sessions"), ("GET", "/api/sessions/clearable/count")]
)
async def test_app_is_refused_the_whole_history_routes(state, method, path):
    """No app owns "every closed transcript", so the bulk clear and its count refuse an app."""
    _seed_transcripts(state)
    async with _serve(state) as client:
        resp = await client.request(method, path, params=_q(_PLAIN))
        assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    for key in ("dashboard:user-chat", "dashboard:plain-chat", "dashboard:other-chat"):
        assert state.conversation_log.has_log(key)


@pytest.fixture
def summaries(monkeypatch):
    """Stand in for the LLM summary so the test sees which transcripts were read."""
    read: list[str] = []

    async def _fake_summary(_state, key: str, **_kwargs) -> str:
        read.append(key)
        return f"summary of {key}"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions._summarize_one", _fake_summary)
    return read


@pytest.mark.asyncio
async def test_app_summarizes_only_its_own_transcripts(state, summaries, monkeypatch):
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: audit)
    _seed_transcripts(state)
    keys = ["dashboard:user-chat", "dashboard:plain-chat", "dashboard:other-chat", "dashboard:none"]
    async with _serve(state) as client:
        resp = await client.post(
            "/api/sessions/summarize", params=_q(_PLAIN), json={"keys": keys + keys}
        )
        assert resp.status == 200
        assert await resp.read() == (
            b'{"summaries": {"dashboard:plain-chat": "summary of dashboard:plain-chat"}}'
        )
    assert summaries == ["dashboard:plain-chat"]
    assert [
        c.kwargs
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("source") == "app_isolation"
    ] == [
        {
            "caller": _PLAIN,
            "operation": "session_summarize",
            "outcome": "allowed" if key == "dashboard:plain-chat" else "denied",
            "source": "app_isolation",
            "resources": f"session={key}",
            "error": "" if key == "dashboard:plain-chat" else "app does not own this transcript",
        }
        for key in keys
    ]


@pytest.mark.asyncio
async def test_internal_caller_from_an_app_slot_summarizes_only_that_apps_transcripts(
    state, summaries, monkeypatch
):
    """The production caller: the ``list_sessions`` MCP tool run by an agent in an app's slot."""
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.session_key_is_attested", lambda *_a, **_k: True
    )
    _seed_transcripts(state)
    _app_slot(state, "dashboard:plain-slot", _PLAIN)
    _user_slot(state)
    app = web.Application(
        middlewares=[
            token_auth.token_auth_middleware(
                internal_paths=frozenset({"/api/sessions/summarize"}),
                internal_secret="route-gate-secret",
            )
        ]
    )
    app["state"] = state
    register_all(app)
    keys = ["dashboard:user-chat", "dashboard:plain-chat", "dashboard:other-chat"]
    async with TestClient(TestServer(app)) as client:
        by_session = {}
        for caller in ("dashboard:plain-slot", "dashboard:user-tab"):
            headers = {"X-Internal-Secret": "route-gate-secret", "X-Session-Key": caller}
            resp = await client.post(
                "/api/sessions/summarize", json={"keys": keys}, headers=headers
            )
            assert resp.status == 200
            by_session[caller] = set((await resp.json())["summaries"])
    assert by_session == {
        "dashboard:plain-slot": {"dashboard:plain-chat"},
        "dashboard:user-tab": set(keys),
    }


@pytest.mark.asyncio
async def test_dashboard_user_still_summarizes_and_counts_every_transcript(state, summaries):
    _seed_transcripts(state)
    keys = ["dashboard:user-chat", "dashboard:plain-chat", "dashboard:other-chat"]
    async with _serve(state) as client:
        resp = await client.post("/api/sessions/summarize", params=_q(), json={"keys": keys})
        assert set((await resp.json())["summaries"]) == set(keys)
        resp = await client.get("/api/sessions/clearable/count", params=_q())
        assert (resp.status, await resp.json()) == (200, {"sessions": 3})
