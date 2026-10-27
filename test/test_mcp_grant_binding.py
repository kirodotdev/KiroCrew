"""Which dashboard requests bind a slot's session to a user for persisted MCP sign-ins."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import mcp_grant_principals as principals
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.mcp_grant_binding import (
    bind_routed_slot,
    bind_slot_to_principal,
    bind_slot_to_request_user,
    request_principal,
)
from kiro_crew.dashboard.token_auth_callers import MEMBER_CHAT_PRINCIPAL_KEY


class _Request(dict):
    """The parts of an aiohttp request the binding reads."""

    def __init__(self, owner_id: str = "", sessions=None, **claims):
        super().__init__(claims)
        self.app = {"state": SimpleNamespace(owner_id=owner_id, sessions=sessions)}


def _slot(key: str = "chat-1-1700000000", **extra):
    return SimpleNamespace(key=key, linked_session_key="", _app="", _created_by="", **extra)


def _bind_user(*args, **kwargs):
    asyncio.run(bind_slot_to_request_user(*args, **kwargs))


def _bind_routed(*args):
    asyncio.run(bind_routed_slot(*args))


@pytest.fixture(autouse=True)
def _fresh_bindings():
    principals.reset_for_tests()
    yield
    principals.reset_for_tests()


@pytest.mark.parametrize(
    ("owner_id", "claims", "expected"),
    [
        # A single-user local gateway: the machine-local bootstrap subject is the owner.
        ("", {"user": "local-app", "app": ""}, "owner"),
        ("U-OWNER", {"user": "U-OWNER", "app": ""}, "owner"),
        # Any other signed subject gets its own namespace.
        ("U-OWNER", {"user": "42", "app": ""}, "user:42"),
        ("", {"user": "someone@example.com", "app": ""}, "user:someone@example.com"),
        # App tokens, an unauthenticated caller and an empty subject bind no one.
        ("", {"user": "local-app", "app": "design-critique"}, None),
        ("", {"user": "local-app"}, None),
        ("", {"app": ""}, None),
        ("", {"user": "", "app": ""}, None),
        # A crew member's admitted request is an agent's, even on an owner subject.
        ("", {"user": "local-app", "app": "", MEMBER_CHAT_PRINCIPAL_KEY: "member:ada"}, None),
    ],
)
def test_request_principal(owner_id, claims, expected):
    assert request_principal(_Request(owner_id, **claims)) == expected


def test_two_users_on_one_slot_conflict_it():
    slot = _slot()
    key = effective_session_key(slot)  # the key the session's engine is spawned for
    _bind_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) == "owner"
    _bind_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) == "owner"
    _bind_user(_Request(user="42", app=""), slot)
    assert principals.principal_for(key) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"linked_session_key": "slack:1700000000.0001"},
        {"_app": "design-critique"},
        {"_created_by": "chat-parent"},
    ],
)
def test_channel_app_and_spawned_slots_conflict_their_key(extra):
    slot = SimpleNamespace(
        **{"key": "chat-2", "linked_session_key": "", "_app": "", "_created_by": "", **extra}
    )
    key = effective_session_key(slot)
    # Even a key an earlier person bound (a reused slot key) cannot be inherited.
    principals.bind(key, "owner")
    _bind_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) is None
    _bind_user(_Request(user="local-app", app=""), _slot("chat-2"))
    assert principals.principal_for(key) is None


@pytest.mark.parametrize(
    ("claims", "person"),
    [
        ({"user": "local-app", "app": "design-critique"}, True),
        ({"user": "local-app", "app": "", MEMBER_CHAT_PRINCIPAL_KEY: "member:ada"}, True),
        ({"user": "local-app", "app": ""}, False),  # an attested cron
    ],
)
def test_a_non_person_caller_on_a_persons_slot_conflicts_it(claims, person):
    slot = _slot()
    key = effective_session_key(slot)
    _bind_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) == "owner"
    _bind_user(_Request(**claims), slot, person=person)
    assert principals.principal_for(key) is None
    # The person returning does not undo it.
    _bind_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) is None


class _Sessions:
    """The session-map link reads the binding consults."""

    def __init__(self, slack: str = "", inbound: bool = False, broken: bool = False):
        self._slack, self._inbound, self._broken = slack, inbound, broken

    def get_slack_link(self, key):
        if self._broken:
            raise OSError("unreadable")
        return (self._slack or None, None)

    def mirror_accepts_inbound(self, key):
        return self._inbound


@pytest.mark.parametrize(
    "sessions",
    [_Sessions(slack="1700000000.0001"), _Sessions(inbound=True), _Sessions(broken=True)],
)
def test_a_channel_driven_session_conflicts_even_for_the_owner(sessions):
    """A persisted Slack thread or two-way mirror survives a restart; so does the conflict."""
    slot = _slot()
    _bind_user(_Request(user="local-app", app="", sessions=sessions), slot)
    assert principals.principal_for(effective_session_key(slot)) is None


def test_a_slack_linked_slot_conflicts():
    slot = _slot(_slack_linked=True)
    _bind_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(effective_session_key(slot)) is None


def test_an_unlinked_session_binds_the_owner():
    slot = _slot()
    _bind_user(_Request(user="local-app", app="", sessions=_Sessions()), slot)
    assert principals.principal_for(effective_session_key(slot)) == "owner"


def test_linking_a_session_to_a_channel_conflicts_its_key():
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.session_parts import links

    class _Map:
        def set_slack_link(self, *a):
            pass

        def set_mirror_link(self, *a, **k):
            pass

    manager = SimpleNamespace(_session_map=_Map())
    principals.bind("dashboard:a", "owner")
    links.set_slack_link(manager, "dashboard:a", "1700000000.0001", "C1")
    assert principals.principal_for("dashboard:a") is None

    principals.bind("dashboard:b", "owner")
    link = ChannelLink(channel_type="telegram", channel_id="42")
    links.set_mirror_link(manager, "dashboard:b", link)  # outbound only
    assert principals.principal_for("dashboard:b") == "owner"
    links.set_mirror_link(manager, "dashboard:b", link, accepts_inbound=True)
    assert principals.principal_for("dashboard:b") is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_second_user_on_any_slot_route_conflicts_it(method):
    """Continue, regenerate, edit-resend, reload ... all pass the per-slot checkpoint."""

    slot = _slot()
    key = effective_session_key(slot)
    owner = _Request(user="local-app", app="")
    owner.method = "POST"
    _bind_routed(owner, slot)
    assert principals.principal_for(key) == "owner"
    other = _Request("U-OWNER", user="42", app="")
    other.method = method
    _bind_routed(other, slot)
    assert principals.principal_for(key) is None


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_read_on_a_slot_route_binds_nothing(method):

    slot = _slot()
    reader = _Request("U-OWNER", user="42", app="")
    reader.method = method
    _bind_routed(reader, slot)
    assert principals.principal_for(effective_session_key(slot)) is None
    principals.bind(effective_session_key(slot), "owner")
    _bind_routed(reader, slot)
    assert principals.principal_for(effective_session_key(slot)) == "owner"


def test_the_owner_focus_prefetch_binds_the_owner():
    from kiro_crew.dashboard.mcp_grant_binding import bind_slot_to_principal

    slot = _slot()
    bind_slot_to_principal(slot, principals.OWNER_PRINCIPAL, _Sessions())
    assert principals.principal_for(effective_session_key(slot)) == "owner"
    linked = _slot("chat-9")
    bind_slot_to_principal(linked, principals.OWNER_PRINCIPAL, _Sessions(slack="1.2"))
    assert principals.principal_for(effective_session_key(linked)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["continue", "regenerate", "edit-resend", "reload", "agent"])
async def test_the_slot_checkpoint_binds_a_dashboard_user(route):
    """Through the real middleware: a dashboard user (app="") is bound, a second one conflicts."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    slot = _slot("chat-7")
    state = SimpleNamespace(owner_id="U-OWNER", sessions=_Sessions(), _slots={"chat-7": slot})

    @web.middleware
    async def identity(request, handler):
        request["user"] = request.headers["X-User"]
        request["app"] = ""
        return await handler(request)

    async def ok(request):
        return web.json_response({})

    app = web.Application(middlewares=[identity, slot_ownership_middleware])
    app["state"] = state
    app.router.add_post(f"/api/chat/slots/{{slot}}/{route}", ok)
    async with TestClient(TestServer(app)) as client:
        key = effective_session_key(slot)
        assert (
            await client.post(f"/api/chat/slots/chat-7/{route}", headers={"X-User": "U-OWNER"})
        ).status == 200
        assert principals.principal_for(key) == "owner"
        assert (
            await client.post(f"/api/chat/slots/chat-7/{route}", headers={"X-User": "42"})
        ).status == 200
        assert principals.principal_for(key) is None


def test_the_gateways_own_loopback_writes_leave_a_binding_alone():
    """A session's dashboard MCP tools tagging or filing its own slot must not conflict it."""
    slot = _slot()
    key = effective_session_key(slot)
    owner = _Request(user="local-app", app="")
    owner.method = "POST"
    _bind_routed(owner, slot)
    internal = _Request(internal_auth=True)  # X-Internal-Secret, no app claim
    internal.method = "PATCH"
    _bind_routed(internal, slot)
    assert principals.principal_for(key) == "owner"
    # An internal caller that does carry an app claim is an app: it conflicts.
    app_internal = _Request(internal_auth=True, app="design-critique")
    app_internal.method = "PATCH"
    _bind_routed(app_internal, slot)
    assert principals.principal_for(key) is None


class _LinkLandsMidRead(_Sessions):
    """The Slack link is written (and noted) right after the binder read it as absent."""

    def __init__(self, key):
        super().__init__()
        self._key, self._landed = key, False

    def mirror_accepts_inbound(self, key):
        if not self._landed:
            self._landed = True
            self._slack = "1700000000.0001"
            principals.note_channel_link(self._key)  # what set_slack_link does after writing
        return False


def test_a_channel_link_landing_mid_bind_is_never_lost():
    slot = _slot()
    key = effective_session_key(slot)
    bind_slot_to_principal(slot, "owner", _LinkLandsMidRead(key))
    assert principals.principal_for(key) is None


def test_a_channel_link_noted_after_the_bind_converts_it():
    slot = _slot()
    key = effective_session_key(slot)
    bind_slot_to_principal(slot, "owner", _Sessions())
    assert principals.principal_for(key) == "owner"
    principals.note_channel_link(key)
    assert principals.principal_for(key) is None


@pytest.mark.asyncio
async def test_the_request_bind_reads_links_off_the_event_loop():
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class _Recording(_Sessions):
        def get_slack_link(self, key):
            seen.append(threading.get_ident())
            return super().get_slack_link(key)

    await bind_slot_to_request_user(
        _Request(user="local-app", app="", sessions=_Recording()), _slot()
    )
    assert seen and all(t != loop_thread for t in seen)


@pytest.mark.parametrize("kind", ["peer", "app", "cron"])
def test_a_non_person_delivery_conflicts_the_target_before_any_turn(kind, monkeypatch):
    """session_send, an app or a cron delivering into a person's session stops its vault use."""
    from kiro_crew.dashboard import session_deliver

    slot = _slot()
    key = effective_session_key(slot)
    principals.bind(key, "owner")

    async def stop(*_a, **_k):
        return session_deliver.NOT_DELIVERED

    monkeypatch.setattr(session_deliver, "_deliver_peer", stop)
    monkeypatch.setattr(session_deliver, "_deliver_running", stop)
    origin = {
        "peer": session_deliver.peer(steer=False),
        "app": session_deliver.app("x"),
        "cron": session_deliver.cron("x"),
    }[kind]
    slot.running = True
    asyncio.run(session_deliver.deliver(SimpleNamespace(), slot, "hi", origin))
    assert principals.principal_for(key) is None


def test_an_automation_wake_leaves_the_binding():
    from kiro_crew.dashboard import session_deliver

    slot = _slot()
    key = effective_session_key(slot)
    principals.bind(key, "owner")
    slot.running = False
    result = asyncio.run(
        session_deliver.deliver(SimpleNamespace(), slot, "hi", session_deliver.automation(1, "l"))
    )
    assert result == session_deliver.NOT_DELIVERED
    assert principals.principal_for(key) == "owner"


def test_an_agent_retry_continue_or_reload_conflicts_another_session():
    from kiro_crew.dashboard.session_control_parts.turn_control import _conflict_unless_self

    slot = _slot()
    key = effective_session_key(slot)
    principals.bind(key, "owner")
    _conflict_unless_self(slot, key)  # a session acting on itself
    assert principals.principal_for(key) == "owner"
    _conflict_unless_self(slot, "dashboard:someone-else")
    assert principals.principal_for(key) is None


def test_a_peer_turn_on_an_unbound_slot_cannot_be_authorized_by_a_later_bind():
    """After a restart: session_send into A's unbound slot, then A pins it during cold start."""
    from kiro_crew.dashboard import session_deliver

    slot = _slot()
    key = effective_session_key(slot)
    assert principals.principal_for(key) is None
    slot.running = True

    async def stop(*_a, **_k):
        return session_deliver.NOT_DELIVERED

    import unittest.mock as um

    with um.patch.object(session_deliver, "_deliver_peer", stop):
        asyncio.run(
            session_deliver.deliver(
                SimpleNamespace(), slot, "hi", session_deliver.peer(steer=False)
            )
        )
    principals.bind(key, "owner")  # A's later metadata request
    assert principals.principal_for(key) is None
