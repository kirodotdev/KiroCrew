"""The private-member gate in front of the session-control HTTP routes.

``_private_caller_refusal`` admits a crew member (a ``member-*`` key, or a ``chat-*``
slot whose store is a member's V2 store) under ``member_dispatch``, so the member fence
in ``session_control.py`` decides. Every other verified V2 caller gets
``member_scope_denied``; an unverifiable one gets ``member_session_unverified``.

The gate reads the same ``sc.member_dispatch_enabled()`` ceiling, so it cannot open
wider. ``internal_memory_scope`` and ``_store_is_member_owned`` are stubbed; both have
their own tests.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from session_control_helpers import (  # noqa: F401 -- autouse, registers by import
    _fresh_stop_windows,
)

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import session_control as handler

MEMBER_SESSION = "dashboard:member-radar"
CHAT_SLOT_SESSION = "dashboard:chat-10-1789623359"
MEMBER_STORE = "member-kirocrew-conductor-deadbeef"
NON_MEMBER_STORE = "default"


def _internal_request(session_key: str) -> web.Request:
    """A strict-internal request carrying *session_key*, as the middleware marks it."""
    app = web.Application()
    request = make_mocked_request(
        "POST", "/api/session-control/create", app=app, headers={"X-Session-Key": session_key}
    )
    request["internal_auth"] = True
    return request


def _stub_scope(monkeypatch, *, scope, refusal=None):
    """Pin ``internal_memory_scope`` (as imported into the handler module)."""

    async def _scope(_request, _operation, **_kwargs):
        return scope, refusal

    monkeypatch.setattr(handler, "internal_memory_scope", _scope)


def _stub_member_store(monkeypatch, *, member_stores=()):
    """Pin ``_store_is_member_owned`` so a store counts as a member store iff listed."""
    monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: store in member_stores)


class TestPrivateCallerGate:
    @pytest.mark.asyncio
    async def test_member_under_ceiling_is_admitted(self, monkeypatch):
        _stub_scope(monkeypatch, scope="member-radar")
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        refusal = await handler._private_caller_refusal(_internal_request(MEMBER_SESSION))
        assert refusal is None

    @pytest.mark.asyncio
    async def test_chat_slot_bound_to_member_store_is_admitted(self, monkeypatch):
        # Case (b): a plain `chat-*` session key whose bound store is a crew
        # member's V2 store. The key is NOT a `member-*` slot, so admission comes
        # entirely from `_store_is_member_owned(scope)`.
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        _stub_member_store(monkeypatch, member_stores={MEMBER_STORE})
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        refusal = await handler._private_caller_refusal(_internal_request(CHAT_SLOT_SESSION))
        assert refusal is None

    @pytest.mark.asyncio
    async def test_chat_slot_bound_to_non_member_store_is_refused(self, monkeypatch):
        # A verified private V2 caller whose store is NOT a crew member's store
        # keeps the refusal, even with both switches on — the admission never
        # widens past member stores.
        _stub_scope(monkeypatch, scope=NON_MEMBER_STORE)
        _stub_member_store(monkeypatch, member_stores={MEMBER_STORE})
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        refusal = await handler._private_caller_refusal(_internal_request(CHAT_SLOT_SESSION))
        assert refusal is not None
        assert refusal.status == 403
        assert json.loads(refusal.text)["code"] == "member_scope_denied"

    @pytest.mark.asyncio
    async def test_member_admitted_under_switch_when_bypass_is_off(self, monkeypatch):
        # member_dispatch off does NOT eject a member from the surface: its own
        # bypass is withdrawn, so it falls back UNDER the global switch, and the
        # switch being on admits it exactly like any ordinary caller. Refusing it
        # here would put a member outside a surface the operator left open to all.
        _stub_scope(monkeypatch, scope="member-radar")
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: False)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        refusal = await handler._private_caller_refusal(_internal_request(MEMBER_SESSION))
        assert refusal is None

    @pytest.mark.asyncio
    async def test_member_is_refused_when_both_switches_are_off(self, monkeypatch):
        # The gate never opens wider than the two switches behind it: bypass off
        # AND the global switch off => the surface is closed for the member too.
        _stub_scope(monkeypatch, scope="member-radar")
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: False)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        refusal = await handler._private_caller_refusal(_internal_request(MEMBER_SESSION))
        assert refusal is not None
        assert refusal.status == 403
        assert json.loads(refusal.text)["code"] == "member_scope_denied"

    @pytest.mark.asyncio
    async def test_chat_slot_member_refused_when_both_switches_off(self, monkeypatch):
        # Case (b) is bounded by the same two switches: a chat-slot member with
        # both off is refused just like a DM slot.
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        _stub_member_store(monkeypatch, member_stores={MEMBER_STORE})
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: False)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        refusal = await handler._private_caller_refusal(_internal_request(CHAT_SLOT_SESSION))
        assert refusal is not None
        assert json.loads(refusal.text)["code"] == "member_scope_denied"

    @pytest.mark.asyncio
    async def test_owner_or_global_caller_falls_through(self, monkeypatch):
        # No private scope => not a private surface => nothing to refuse, exactly
        # as the route behaved before member dispatch existed.
        _stub_scope(monkeypatch, scope=None)
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        refusal = await handler._private_caller_refusal(_internal_request("dashboard:owner"))
        assert refusal is None

    @pytest.mark.asyncio
    async def test_unverified_caller_keeps_its_refusal(self, monkeypatch):
        # A verification failure is returned verbatim, member key or not.
        denial = web.json_response(
            {"error": "unverified", "code": "member_session_unverified"}, status=403
        )
        _stub_scope(monkeypatch, scope=None, refusal=denial)
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        refusal = await handler._private_caller_refusal(_internal_request(MEMBER_SESSION))
        assert refusal is denial


class TestRequireInternalWiring:
    """``_require_internal`` delegates the internal-auth branch to the gate, and
    still refuses a request with no internal secret."""

    @pytest.mark.asyncio
    async def test_internal_auth_branch_uses_the_private_gate(self, monkeypatch):
        _stub_scope(monkeypatch, scope="member-radar")
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        assert await handler._require_internal(_internal_request(MEMBER_SESSION)) is None

    @pytest.mark.asyncio
    async def test_missing_internal_secret_is_still_refused(self, monkeypatch):
        # The non-internal branch is untouched by the fix.
        monkeypatch.setattr(
            handler, "sel", lambda: type("S", (), {"log_api_access": lambda *a, **k: None})()
        )
        app = web.Application()
        request = make_mocked_request("POST", "/api/session-control/create", app=app)
        refusal = await handler._require_internal(request)
        assert refusal is not None
        assert refusal.status == 403
        assert json.loads(refusal.text)["code"] == "internal_secret_required"


class TestCarriedFenceVerdict:
    """The gate carries its member admission into the inner fence.

    Otherwise the inner fence re-derives member status from the mutable config after
    awaits, and a config write in that window would make an admitted member an unfenced
    caller. Routes pass ``_carried_fence`` as ``precomputed_ownership_fenced=True``.
    """

    @pytest.mark.asyncio
    async def test_dm_member_admission_is_carried(self, monkeypatch):
        _stub_scope(monkeypatch, scope="member-radar")
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        request = _internal_request(MEMBER_SESSION)
        assert await handler._private_caller_refusal(request) is None
        assert handler._carried_fence(request) is True

    @pytest.mark.asyncio
    async def test_chat_slot_member_admission_is_carried(self, monkeypatch):
        # Case (b) is the spelling whose admission rests on the config record, so
        # it is the one the carry exists for.
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        _stub_member_store(monkeypatch, member_stores={MEMBER_STORE})
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        request = _internal_request(CHAT_SLOT_SESSION)
        assert await handler._private_caller_refusal(request) is None
        assert handler._carried_fence(request) is True

    @pytest.mark.asyncio
    async def test_owner_caller_carries_no_verdict(self, monkeypatch):
        # An owner / Global-V1 caller is not fenced by the gate; its fence is
        # evaluated inline as before, so nothing is carried.
        _stub_scope(monkeypatch, scope=None)
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        request = _internal_request("dashboard:owner")
        assert await handler._private_caller_refusal(request) is None
        assert handler._carried_fence(request) is None

    @pytest.mark.asyncio
    async def test_refused_caller_carries_no_verdict(self, monkeypatch):
        _stub_scope(monkeypatch, scope=NON_MEMBER_STORE)
        _stub_member_store(monkeypatch, member_stores={MEMBER_STORE})
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        request = _internal_request(CHAT_SLOT_SESSION)
        assert await handler._private_caller_refusal(request) is not None
        assert handler._carried_fence(request) is None

    @pytest.mark.asyncio
    async def test_record_flip_after_admission_cannot_unfence_through_the_route(
        self, tmp_path, monkeypatch
    ):
        # End to end through the real route and a real DashboardState: the gate
        # admits a chat-slot member on its verified scope, then the member-store
        # record flips (the predicate answers False from here on) before the inner
        # authorization runs. The read of a foreign same-workspace session the
        # member did not create is refused at the creator fence, not served.
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_utils import slot_history_key

        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-10-1789623359")
        caller.memory_store = MEMBER_STORE
        foreign = state.get_or_create_slot("chat-7", workspace=caller.workspace)
        assert not foreign._created_by  # the user's own tab, created by no agent

        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        # True exactly once — at the gate — then the record has flipped.
        answers = iter([True])
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda store: next(answers, False))
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        # Switch ON: the only thing standing between this caller and the foreign
        # tab is the creator fence.
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)

        app = web.Application()
        app["state"] = state
        request = make_mocked_request(
            "GET",
            "/api/session-control/read?target=chat-7",
            app=app,
            headers={"X-Session-Key": slot_history_key(caller)},
        )
        request["internal_auth"] = True
        response = await handler.api_session_control_read(request)

        assert response.status == 403, response.text
        assert json.loads(response.text)["code"] == "not_creator", response.text
        # The window is real: with the flipped record the store does not classify
        # as a member store, so the INLINE fence does not bind this caller.
        assert not sc._caller_is_ownership_fenced(state, caller.key)


class TestEveryRouteForwardsTheCarriedVerdict:
    """The carry is held by one hand-written call site per route; pin all of them.

    A route that forgets ``_carried_fence(request)`` reopens the TOCTOU window for its
    verb. ``caller_fenced`` must be ``True`` for a member and ``None`` for an owner; for
    ``create`` it decides which store the new child may bind.
    """

    ROUTES = {
        # handler name -> (core function name, awaited?, body, query)
        "api_session_control_stop": ("stop_target", True, {"target": "chat-7"}, None),
        "api_session_control_close": ("close_target", True, {"target": "chat-7"}, None),
        "api_session_control_send": (
            "send_to_target",
            True,
            {"target": "chat-7", "message": "hello"},
            None,
        ),
        "api_session_control_set_color": (
            "set_color_target",
            True,
            {"target": "chat-7", "color": "2"},
            None,
        ),
        "api_session_control_set_note": (
            "authorize_created_target",
            True,
            {"target": "chat-7", "note": "The build is green."},
            None,
        ),
        "api_session_control_rename": (
            "rename_target",
            True,
            {"target": "chat-7", "title": "renamed"},
            None,
        ),
        "api_session_control_generate_title": (
            "generate_title_target",
            True,
            {"target": "chat-7"},
            None,
        ),
        "api_session_control_read": ("read_messages", False, None, {"target": "chat-7"}),
        # No target to name, so the body carries only the optional create fields
        # and an empty one exercises the carry on its own.
        "api_session_control_create": ("create_session", True, {}, None),
    }

    def _request(self, session_key, *, body, query):
        from urllib.parse import urlencode

        from member_memory_helpers import json_payload

        app = web.Application()
        app["state"] = object()  # never touched: the core function is a recorder
        path = "/api/session-control/x" + (f"?{urlencode(query)}" if query else "")
        headers = {"X-Session-Key": session_key}
        kwargs = {}
        if body is not None:
            raw = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
            kwargs["payload"] = json_payload(raw)
        request = make_mocked_request(
            "POST" if body is not None else "GET", path, app=app, headers=headers, **kwargs
        )
        request["internal_auth"] = True
        return request

    class _Answer(dict):
        """A recorder's return: the JSON a route answers, plus the ``key`` the slot a
        gate returns carries, which the set-note route's audit reads."""

        key = "chat-7"

    def _record(self, monkeypatch, core_name, awaited):
        seen: dict = {}
        answer = self._Answer(ok=True)

        async def _async_recorder(state, **kwargs):
            seen.update(kwargs)
            return answer

        def _sync_recorder(state, **kwargs):
            seen.update(kwargs)
            return answer

        monkeypatch.setattr(sc, core_name, _async_recorder if awaited else _sync_recorder)

        async def _no_prewarm():
            return None

        async def _no_window_load(_state, _target):
            return None

        async def _no_note_write(_state, _slot, **_kwargs):
            return web.json_response({"ok": True})

        monkeypatch.setattr(sc, "prewarm_enabled_check", _no_prewarm)
        monkeypatch.setattr(sc, "load_target_window", _no_window_load)
        # set-note writes through the note route's body once its gate returns.
        monkeypatch.setattr(chat_handlers, "write_slot_note", _no_note_write)
        return seen

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name", sorted(ROUTES))
    async def test_admitted_member_verdict_reaches_the_core(self, monkeypatch, handler_name):
        core_name, awaited, body, query = self.ROUTES[handler_name]
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        _stub_member_store(monkeypatch, member_stores={MEMBER_STORE})
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        seen = self._record(monkeypatch, core_name, awaited)
        response = await getattr(handler, handler_name)(
            self._request(CHAT_SLOT_SESSION, body=body, query=query)
        )
        assert response.status == 200, response.text
        assert "caller_fenced" in seen, f"{handler_name} does not forward the carried verdict"
        assert seen["caller_fenced"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name", sorted(ROUTES))
    async def test_owner_caller_forwards_none(self, monkeypatch, handler_name):
        core_name, awaited, body, query = self.ROUTES[handler_name]
        _stub_scope(monkeypatch, scope=None)
        _stub_member_store(monkeypatch)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
        seen = self._record(monkeypatch, core_name, awaited)
        response = await getattr(handler, handler_name)(
            self._request("dashboard:owner", body=body, query=query)
        )
        assert response.status == 200, response.text
        assert seen.get("caller_fenced", "missing") is None


class TestOwnerCallerReach:
    """A scopeless caller in a tab the person opened is deliberately not creator-fenced.

    The fence follows authority, and that tab carries the person's own, so through
    the real routes it reaches a same-workspace session it did not create. Why:
    session-control.md "The owner's own tab keeps the owner's reach".
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["read", "send", "stop", "close"])
    async def test_reaches_a_session_it_did_not_create(self, tmp_path, monkeypatch, verb):
        from chat_test_helpers import _make_state
        from member_memory_helpers import json_payload

        from kiro_crew.dashboard.chat_utils import slot_history_key

        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        target = state.get_or_create_slot("chat-7", workspace=caller.workspace)
        assert not target._created_by  # the person opened it; the caller did not
        _stub_scope(monkeypatch, scope=None)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: True)

        async def _no_turn(_state, _slot, _prompt):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _no_turn)

        app = web.Application()
        app["state"] = state
        headers = {"X-Session-Key": slot_history_key(caller)}
        if verb == "read":
            request = make_mocked_request(
                "GET", "/api/session-control/read?target=chat-7", app=app, headers=headers
            )
        else:
            body = {"target": "chat-7", **({"message": "hello"} if verb == "send" else {})}
            raw = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
            request = make_mocked_request(
                "POST",
                f"/api/session-control/{verb}",
                app=app,
                headers=headers,
                payload=json_payload(raw),
            )
        request["internal_auth"] = True
        response = await getattr(handler, f"api_session_control_{verb}")(request)

        assert response.status == 200, response.text
        assert json.loads(response.text)["target"] == "chat-7", response.text
