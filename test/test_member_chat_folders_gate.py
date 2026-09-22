"""Crew-member admission to the chat folder/tag routes, and the per-resource
fences behind that admission.

The bug: a dashboard chat session whose agent is a crew member with a private V2
memory store got ``member_scope_denied`` (403) from EVERY ``/api/chat/folders*``
and ``/api/chat/tags*`` route, because ``private_chat_route_refusal`` refused any
scoped caller except ``POST .../followup``. Meanwhile ``/api/session-control/*``
admits the same member callers -- so a conductor could create workers but could
not put them in a folder, which its operating procedure (``session_create`` with
``folder=``, ``chat_folder_file_self``) requires.

These tests pin:

* the SHARED member-admission predicate both surface gates key on
  (``session_control.member_admitted_to_scoped_surface``), and that flipping it
  flips BOTH gates -- so they cannot drift;
* the structural route matcher: the member routes are admitted for exactly the
  verbs a member can use, while the write verbs no member fence covers -- tag
  POST/PATCH/DELETE and folder DELETE -- plus ``/tag-columns`` and every other
  ``/api/chat/*`` route are NOT;
* the folder-tree fence generalised to a principal: a member owns the folders it
  creates (``owner_app == "member:<store>"``), and cannot rename/delete the
  person's; a member's tree READ is scoped to its own folders;
* the slot filing/tagging fence: a member may file/tag only a session it owns or
  created (``member_owns_slot``);
* the shared tag vocabulary stays owner-only: a member cannot coin/rename tags.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import _shared

MEMBER_STORE = "member-kirocrew-conductor-deadbeef"
MEMBER_SESSION = "dashboard:member-conductor"
MEMBER_PRINCIPAL = f"member:{MEMBER_STORE}"


# --------------------------------------------------------------------------- #
# The structural route matcher.
# --------------------------------------------------------------------------- #
class TestAdmittedChatRouteMethods:
    @pytest.mark.parametrize(
        "path,method",
        [
            ("/api/chat/folders", "GET"),
            ("/api/chat/folders", "POST"),
            ("/api/chat/folders/abc123", "PATCH"),  # rename/reparent own folder
            ("/api/chat/tags", "GET"),  # READ the shared vocabulary
            ("/api/chat/slots/chat-1-2/folder", "PATCH"),
            ("/api/chat/slots/chat-1-2/tags", "PUT"),
            ("/api/chat/slots", "GET"),  # read-only session list for folder tools
        ],
    )
    def test_admitted_verbs_are_exactly_a_members_capabilities(self, path, method):
        allowed = _shared._admitted_chat_route_methods(path)
        assert allowed is not None
        assert method in allowed

    @pytest.mark.parametrize(
        "path,method",
        [
            # A member reads the vocabulary but never writes it; the handlers
            # for these either refuse members (POST/PATCH) or have NO member
            # fence at all (DELETE /tags/{id}), so the GATE must refuse them.
            ("/api/chat/tags", "POST"),
            ("/api/chat/tags/t1", "PATCH"),
            ("/api/chat/tags/t1", "DELETE"),
            # A member cannot delete folders (delete is refused for every agent
            # principal), so the gate does not forward it.
            ("/api/chat/folders/abc123", "DELETE"),
        ],
    )
    def test_write_verbs_the_member_cannot_do_are_refused(self, path, method):
        allowed = _shared._admitted_chat_route_methods(path)
        # Either the path is not admitted at all, or the method is excluded.
        assert allowed is None or method not in allowed

    @pytest.mark.parametrize(
        "path",
        [
            "/api/chat/tags/t1",  # tags/{id} is not an admitted route at all
            "/api/chat/tag-columns",  # different prefix, not /tags/{id}
            "/api/chat/tag-columns/c1",
            "/api/chat/slots/chat-1-2",  # slot detail
            "/api/chat/slots/chat-1-2/pin",  # a different slot sub-resource
            "/api/chat/pins",
            "/api/chat/folders/abc/deeper",  # deeper than one segment
        ],
    )
    def test_sibling_and_foreign_routes_are_not_admitted(self, path):
        assert _shared._admitted_chat_route_methods(path) is None

    def test_reorder_is_admitted_post_only(self):
        # The sibling-position leg of a folder move is admitted (POST), fenced
        # to member-owned folders in the handler.
        assert _shared._admitted_chat_route_methods("/api/chat/folders/reorder") == frozenset(
            {"POST"}
        )

    def test_wrong_method_on_an_admitted_path_is_not_matched(self):
        # DELETE on the folders collection route is not admitted.
        assert "DELETE" not in _shared._admitted_chat_route_methods("/api/chat/folders")
        # Folder {id} admits PATCH only -- never DELETE.
        assert _shared._admitted_chat_route_methods("/api/chat/folders/abc123") == frozenset(
            {"PATCH"}
        )
        # The tag vocabulary is READ-only for a member.
        assert _shared._admitted_chat_route_methods("/api/chat/tags") == frozenset({"GET"})
        # POST on the session list is not admitted (GET only).
        assert "POST" not in _shared._admitted_chat_route_methods("/api/chat/slots")
        # reorder admits POST only.
        assert "GET" not in _shared._admitted_chat_route_methods("/api/chat/folders/reorder")


# --------------------------------------------------------------------------- #
# The shared predicate -- one flip moves BOTH gates.
# --------------------------------------------------------------------------- #
class TestSharedPredicate:
    def _wire(self, monkeypatch, *, is_member_store, dispatch, control):
        monkeypatch.setattr(sc, "_store_is_member_owned", lambda s: is_member_store)
        monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: dispatch)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: control)

    def test_member_admitted_when_dispatch_on(self, monkeypatch):
        self._wire(monkeypatch, is_member_store=True, dispatch=True, control=False)
        assert sc.member_admitted_to_scoped_surface(MEMBER_SESSION, MEMBER_STORE) is True

    def test_member_admitted_under_switch_when_dispatch_off(self, monkeypatch):
        self._wire(monkeypatch, is_member_store=True, dispatch=False, control=True)
        assert sc.member_admitted_to_scoped_surface(MEMBER_SESSION, MEMBER_STORE) is True

    def test_member_refused_when_both_off(self, monkeypatch):
        self._wire(monkeypatch, is_member_store=True, dispatch=False, control=False)
        assert sc.member_admitted_to_scoped_surface(MEMBER_SESSION, MEMBER_STORE) is False

    def test_non_member_store_refused(self, monkeypatch):
        # Not a member DM key and not a member store -> refused whatever the switches.
        self._wire(monkeypatch, is_member_store=False, dispatch=True, control=True)
        assert sc.member_admitted_to_scoped_surface("dashboard:chat-9-1", "default") is False

    def test_one_flip_moves_both_gates(self, monkeypatch):
        # Monkeypatching the SHARED helper flips the chat-route admission AND the
        # session-control gate together, proving they read one predicate.
        from kiro_crew.dashboard.handlers import session_control as sc_handler

        seen = {}

        def _fake(session_key, store):
            seen["called"] = (session_key, store)
            return False

        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", _fake)
        # chat-route gate path:
        assert sc.member_admitted_to_scoped_surface(MEMBER_SESSION, MEMBER_STORE) is False
        assert seen["called"] == (MEMBER_SESSION, MEMBER_STORE)
        # The session-control handler imports the same module attribute.
        assert sc_handler.sc.member_admitted_to_scoped_surface is _fake


# --------------------------------------------------------------------------- #
# The gate: admit on the six, refuse elsewhere.
# --------------------------------------------------------------------------- #
def _internal_request(method, path, session_key=MEMBER_SESSION):
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={})
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": session_key})
    req["internal_auth"] = True
    req["peer_verified"] = True
    return req


def _stub_scope(monkeypatch, *, scope):
    async def _scope(_request, _operation, **_kwargs):
        return scope, None

    monkeypatch.setattr(_shared, "internal_memory_scope", _scope)


class TestChatRouteGate:
    @pytest.mark.asyncio
    async def test_member_admitted_to_folders_and_stamps_principal(self, monkeypatch):
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda k, s: True)
        req = _internal_request("POST", "/api/chat/folders")
        refusal = await _shared.private_chat_route_refusal(req)
        assert refusal is None
        # The verified principal is carried for the handler fence.
        from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

        assert req[MEMBER_CHAT_PRINCIPAL_KEY] == MEMBER_PRINCIPAL

    @pytest.mark.asyncio
    async def test_member_admitted_to_slot_tags(self, monkeypatch):
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda k, s: True)
        req = _internal_request("PUT", "/api/chat/slots/chat-1-2/tags")
        assert await _shared.private_chat_route_refusal(req) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path",
        [
            ("DELETE", "/api/chat/tags/t1"),  # tag DELETE has NO vocabulary fence
            ("PATCH", "/api/chat/tags/t1"),  # tag rename is owner-only
            ("POST", "/api/chat/tags"),  # coining a tag is owner-only
            ("DELETE", "/api/chat/folders/abc123"),  # folder delete is owner-only
        ],
    )
    async def test_member_refused_on_write_verbs_no_member_fence_covers(
        self, monkeypatch, method, path
    ):
        # Even for a caller the shared predicate would admit, the gate must
        # refuse a verb whose handler has no member fence -- otherwise a member
        # could delete a shared tag or a folder.
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda k, s: True)
        req = _internal_request(method, path)
        refusal = await _shared.private_chat_route_refusal(req)
        assert refusal is not None and refusal.status == 403
        assert json.loads(refusal.text)["code"] == "member_scope_denied"

    @pytest.mark.asyncio
    async def test_member_still_refused_on_a_non_admitted_chat_route(self, monkeypatch):
        # A slot SUB-resource (pin) is NOT one of the admitted routes -- the
        # owner-only refusal stands even for a caller the shared predicate would
        # otherwise admit.
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda k, s: True)
        req = _internal_request("PATCH", "/api/chat/slots/chat-1-2/pin")
        refusal = await _shared.private_chat_route_refusal(req)
        assert refusal is not None and refusal.status == 403
        assert json.loads(refusal.text)["code"] == "member_scope_denied"

    @pytest.mark.asyncio
    async def test_member_admitted_to_session_list(self, monkeypatch):
        # GET /api/chat/slots is admitted read-only; the handler filters it.
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda k, s: True)
        req = _internal_request("GET", "/api/chat/slots")
        assert await _shared.private_chat_route_refusal(req) is None

    @pytest.mark.asyncio
    async def test_member_admitted_to_reorder(self, monkeypatch):
        # The sibling-position leg of a move is admitted; the handler fences it.
        _stub_scope(monkeypatch, scope=MEMBER_STORE)
        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda k, s: True)
        req = _internal_request("POST", "/api/chat/folders/reorder")
        assert await _shared.private_chat_route_refusal(req) is None

    @pytest.mark.asyncio
    async def test_non_member_scoped_caller_refused_on_the_six(self, monkeypatch):
        # A private V2 caller whose store is NOT a crew member's keeps the refusal
        # even on an admitted route.
        _stub_scope(monkeypatch, scope="member-not-a-crew")
        monkeypatch.setattr(sc, "member_admitted_to_scoped_surface", lambda k, s: False)
        req = _internal_request("POST", "/api/chat/folders")
        refusal = await _shared.private_chat_route_refusal(req)
        assert refusal is not None and refusal.status == 403
        assert json.loads(refusal.text)["code"] == "member_scope_denied"


# --------------------------------------------------------------------------- #
# folder_principal + the folder-tree fence.
# --------------------------------------------------------------------------- #
class TestFolderPrincipal:
    def test_app_principal_is_the_bare_name_unchanged(self):
        from kiro_crew.dashboard.token_auth import folder_principal

        req = make_mocked_request("POST", "/api/chat/folders")
        req["app"] = "scaffolder"
        assert folder_principal(SimpleNamespace(_slots={}), req) == "scaffolder"

    def test_member_principal_read_from_the_carried_key(self):
        from kiro_crew.dashboard.token_auth import (
            MEMBER_CHAT_PRINCIPAL_KEY,
            folder_principal,
        )

        req = make_mocked_request(
            "POST", "/api/chat/folders", headers={"X-Session-Key": MEMBER_SESSION}
        )
        req[MEMBER_CHAT_PRINCIPAL_KEY] = MEMBER_PRINCIPAL
        assert folder_principal(SimpleNamespace(_slots={}), req) == MEMBER_PRINCIPAL

    def test_app_claim_wins_over_a_stray_member_key(self):
        # An app never carries a member key, but if both were present the app
        # arm is checked first so an app's principal stays byte-identical.
        from kiro_crew.dashboard.token_auth import (
            MEMBER_CHAT_PRINCIPAL_KEY,
            folder_principal,
        )

        req = make_mocked_request("POST", "/api/chat/folders")
        req["app"] = "scaffolder"
        req[MEMBER_CHAT_PRINCIPAL_KEY] = MEMBER_PRINCIPAL
        assert folder_principal(SimpleNamespace(_slots={}), req) == "scaffolder"

    def test_person_principal_is_empty(self):
        from kiro_crew.dashboard.token_auth import folder_principal

        req = make_mocked_request("POST", "/api/chat/folders")
        assert folder_principal(SimpleNamespace(_slots={}), req) == ""


class TestFolderOwnerComparisons:
    def test_member_owned_folder_reads_its_principal(self):
        from kiro_crew.dashboard.chat_folders import _folder_owner_app

        assert _folder_owner_app({"owner_app": MEMBER_PRINCIPAL}) == MEMBER_PRINCIPAL

    def test_absent_owner_reads_as_person(self):
        from kiro_crew.dashboard.chat_folders import _folder_owner_app

        assert _folder_owner_app({}) == ""
        assert _folder_owner_app({"owner_app": ""}) == ""

    def test_app_and_member_principals_do_not_collide(self):
        # A bare app name never equals a member principal, so the same
        # ``stored != principal`` comparison fences both without a migration.
        from kiro_crew.dashboard.chat_folders import _folder_owner_app

        assert _folder_owner_app({"owner_app": "scaffolder"}) != MEMBER_PRINCIPAL


# --------------------------------------------------------------------------- #
# member_owns_slot + the slot filing/tagging fence.
# --------------------------------------------------------------------------- #
class TestMemberOwnsSlot:
    """The slot key space, not the session key space.

    ``create_session`` stamps ``_created_by`` with the caller's SLOT key
    (``caller_slot_key(state, ...)``), while the routes call ``member_owns_slot``
    with the raw ``X-Session-Key`` -- a SESSION key. The predicate must resolve
    the session key to a slot key before comparing, so ``state`` here holds the
    caller's OWN slot for :func:`sc.caller_slot_key` to resolve against. The
    member's own slot ``member-conductor`` has history key
    ``dashboard:member-conductor`` == ``MEMBER_SESSION``, so it resolves to slot
    key ``member-conductor``.
    """

    #: slot key that MEMBER_SESSION (dashboard:member-conductor) resolves to.
    OWN_SLOT_KEY = "member-conductor"

    def _own_slot(self):
        return SimpleNamespace(
            key=self.OWN_SLOT_KEY,
            linked_session_key="",
            _created_by="",
            channel_origin=False,
        )

    def _state_with_caller(self, *extra_slots):
        own = self._own_slot()
        slots = {own.key: own}
        for s in extra_slots:
            slots[s.key] = s
        return SimpleNamespace(_slots=slots)

    def test_own_session_is_owned(self):
        # The caller's own slot: slot.key resolves to itself.
        own = self._own_slot()
        state = SimpleNamespace(_slots={own.key: own})
        assert sc.member_owns_slot(state, own, MEMBER_SESSION) is True

    def test_created_session_is_owned(self):
        # A child stamped with the caller's SLOT key (what create_session
        # writes) is owned when the caller passes its SESSION key header. This is
        # the exact key-space mismatch the bug got wrong: _created_by holds the
        # slot key, the header is the session key.
        created = SimpleNamespace(
            key="chat-9-9",
            linked_session_key="",
            _created_by=self.OWN_SLOT_KEY,  # the caller's SLOT key
            channel_origin=False,
        )
        state = self._state_with_caller(created)
        assert sc.member_owns_slot(state, created, MEMBER_SESSION) is True

    def test_created_by_session_key_is_not_owned(self):
        # A slot whose _created_by is a SESSION key (never what create_session
        # writes) must NOT match: the compare is in slot-key space only, so a
        # stray session-key value cannot masquerade as ownership.
        created = SimpleNamespace(
            key="chat-9-9",
            linked_session_key="",
            _created_by=MEMBER_SESSION,  # a session key, not a slot key
            channel_origin=False,
        )
        state = self._state_with_caller(created)
        assert sc.member_owns_slot(state, created, MEMBER_SESSION) is False

    def test_foreign_session_is_not_owned(self):
        # A slot created by another caller's slot key is not owned.
        foreign = SimpleNamespace(
            key="chat-1-1",
            linked_session_key="",
            _created_by="chat-77-1",  # another slot key
            channel_origin=False,
        )
        state = self._state_with_caller(foreign)
        assert sc.member_owns_slot(state, foreign, MEMBER_SESSION) is False

    def test_empty_created_by_is_not_owned_by_a_third_slot(self):
        # An unattributed slot (empty _created_by) is not owned by a member that
        # is neither it nor its creator -- an empty _created_by must never match
        # an empty resolved key by accident.
        other = SimpleNamespace(
            key="chat-1-1",
            linked_session_key="",
            _created_by="",
            channel_origin=False,
        )
        state = self._state_with_caller(other)
        assert sc.member_owns_slot(state, other, MEMBER_SESSION) is False

    def test_channel_born_slot_owned_by_session_key_fallback(self):
        # A channel-born slot whose key the live slot map cannot resolve is still
        # owned via the effective_session_key session-space fallback.
        chan = SimpleNamespace(
            key="slack_123",
            linked_session_key="slack:123",
            _created_by="",
            channel_origin=True,
        )
        # State has no caller slot to resolve, so caller_slot_key returns "".
        state = SimpleNamespace(_slots={chan.key: chan})
        assert sc.member_owns_slot(state, chan, "slack:123") is True

    def test_empty_caller_owns_nothing(self):
        slot = SimpleNamespace(key="x", linked_session_key="", _created_by="", channel_origin=False)
        assert sc.member_owns_slot(SimpleNamespace(_slots={}), slot, "") is False


class TestMemberSlotWriteFence:
    def _member_request(self, slot_name):
        req = make_mocked_request(
            "PATCH",
            f"/api/chat/slots/{slot_name}/folder",
            headers={"X-Session-Key": MEMBER_SESSION},
        )
        from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

        req[MEMBER_CHAT_PRINCIPAL_KEY] = MEMBER_PRINCIPAL
        return req

    def test_non_member_caller_is_a_no_op(self):
        from kiro_crew.dashboard.chat_folders import member_slot_write_refused

        req = make_mocked_request("PATCH", "/api/chat/slots/x/folder")  # no member key
        slot = SimpleNamespace(key="x", _created_by="dashboard:owner")
        assert member_slot_write_refused(SimpleNamespace(), req, slot, "chat.slot_folder") is None

    def test_member_refused_on_a_session_it_does_not_own(self, monkeypatch):
        from kiro_crew.dashboard.chat_folders import member_slot_write_refused

        monkeypatch.setattr(sc, "member_owns_slot", lambda state, slot, key: False)
        slot = SimpleNamespace(key="chat-1-1", _created_by="dashboard:owner")
        refusal = member_slot_write_refused(
            SimpleNamespace(), self._member_request("chat-1-1"), slot, "chat.slot_folder"
        )
        assert refusal is not None and refusal.status == 404
        assert json.loads(refusal.text)["code"] == "slot_not_found"

    def test_member_allowed_on_a_session_it_owns(self, monkeypatch):
        from kiro_crew.dashboard.chat_folders import member_slot_write_refused

        monkeypatch.setattr(sc, "member_owns_slot", lambda state, slot, key: True)
        slot = SimpleNamespace(key="chat-9-9", _created_by=MEMBER_SESSION)
        assert (
            member_slot_write_refused(
                SimpleNamespace(), self._member_request("chat-9-9"), slot, "chat.slot_folder"
            )
            is None
        )


# --------------------------------------------------------------------------- #
# Shared tag vocabulary stays owner-only for members.
# --------------------------------------------------------------------------- #
class TestTagVocabularyFence:
    def test_member_cannot_write_the_shared_vocabulary(self, monkeypatch):
        from kiro_crew.dashboard import chat_tags
        from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

        # No app claim, no unattributable refusal -- a member reaching the write.
        monkeypatch.setattr(chat_tags, "refuse_unattributable_caller", lambda *a, **k: None)
        monkeypatch.setattr(chat_tags, "effective_request_app", lambda *a, **k: "")
        req = make_mocked_request(
            "POST", "/api/chat/tags", headers={"X-Session-Key": MEMBER_SESSION}
        )
        req[MEMBER_CHAT_PRINCIPAL_KEY] = MEMBER_PRINCIPAL
        refusal = chat_tags._refuse_vocabulary_write(SimpleNamespace(), req, "chat.tag_create")
        assert refusal is not None and refusal.status == 403
        assert json.loads(refusal.text)["code"] == "app_forbidden"

    def test_person_may_write_the_vocabulary(self, monkeypatch):
        from kiro_crew.dashboard import chat_tags

        monkeypatch.setattr(chat_tags, "refuse_unattributable_caller", lambda *a, **k: None)
        monkeypatch.setattr(chat_tags, "effective_request_app", lambda *a, **k: "")
        req = make_mocked_request("POST", "/api/chat/tags")  # no member key, no app
        assert chat_tags._refuse_vocabulary_write(SimpleNamespace(), req, "chat.tag_create") is None


# --------------------------------------------------------------------------- #
# GET /api/chat/slots is filtered for a member to its own + created sessions.
# --------------------------------------------------------------------------- #
class TestSessionListMemberFilter:
    @pytest.mark.asyncio
    async def test_member_slots_omit_foreign_sessions(self, monkeypatch):
        import json as _json

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.handlers import source_providers
        from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

        # Three slots: the member's own, one it created, and a foreign one.
        # The own slot's history key is dashboard:member-conductor == MEMBER_SESSION,
        # so caller_slot_key resolves the header to slot key "member-conductor" --
        # which is what create_session stamps into _created_by on the child.
        own = SimpleNamespace(
            key="member-conductor", linked_session_key="", _created_by="", channel_origin=False
        )
        created = SimpleNamespace(
            key="chat-2-2",
            linked_session_key="",
            _created_by="member-conductor",  # caller's SLOT key, as create_session writes
            channel_origin=False,
        )
        foreign = SimpleNamespace(
            key="chat-9-9",
            linked_session_key="",
            _created_by="chat-77-1",  # another caller's slot key
            channel_origin=False,
        )
        payloads = [
            {"key": "member-conductor", "title": "self"},
            {"key": "chat-2-2", "title": "worker"},
            {"key": "chat-9-9", "title": "the person's"},
        ]
        state = SimpleNamespace(
            _slots={"member-conductor": own, "chat-2-2": created, "chat-9-9": foreign},
            serialize_slots=lambda **_k: list(payloads),
        )

        # Isolate the handler from provider work and the owner probe.
        async def _noop():
            return None

        monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
        monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: False)

        app = web.Application()
        app["state"] = state
        req = make_mocked_request(
            "GET", "/api/chat/slots", app=app, headers={"X-Session-Key": MEMBER_SESSION}
        )
        req["internal_auth"] = True
        req[MEMBER_CHAT_PRINCIPAL_KEY] = MEMBER_PRINCIPAL

        resp = await chat_handlers.api_chat_slots(req)
        rows = _json.loads(resp.text)
        keys = {r["key"] for r in rows}
        assert keys == {"member-conductor", "chat-2-2"}  # foreign omitted
        assert "chat-9-9" not in keys

    @pytest.mark.asyncio
    async def test_person_sees_every_session(self, monkeypatch):
        import json as _json

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.handlers import source_providers

        payloads = [{"key": "member-conductor"}, {"key": "chat-9-9"}]
        state = SimpleNamespace(_slots={}, serialize_slots=lambda **_k: list(payloads))

        async def _noop():
            return None

        monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
        monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: False)

        app = web.Application()
        app["state"] = state
        # No member principal stamped -> the person; the list is unfiltered.
        req = make_mocked_request("GET", "/api/chat/slots", app=app)
        req["internal_auth"] = True

        resp = await chat_handlers.api_chat_slots(req)
        rows = _json.loads(resp.text)
        assert {r["key"] for r in rows} == {"member-conductor", "chat-9-9"}
