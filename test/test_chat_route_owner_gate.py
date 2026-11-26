"""Chat routes refuse a dashboard session that is neither the owner nor a teammate.

The per-slot checkpoint (``slot_ownership.slot_route_denial``) decides a dashboard
session on every ``/api/chat/slots/{slot}/...`` route and on the routes in
``PERSON_CHECKPOINT_ROUTES``: chat send, a Bot chat's reply threads and the bulk
chat writes. The owner and an admitted Shared Crew teammate pass; any other
dashboard session (a messaging-allowlist link) is refused 403 ``owner_only``; an
internal caller keeps its own controls. HEAD is decided as its GET.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from shared_crew_host_isolation import host_isolation  # noqa: F401
from test_owner_or_teammate_route_gates import OWNER, _app, _fresh, _publish  # noqa: F401

from kiro_crew.dashboard.routes import chat as chat_routes

# ── Requests through a real router: body, query and header shapes ─────────────


@web.middleware
async def _identity(request: web.Request, handler):
    who = request.headers.get("X-Test-Who", "")
    if who:
        _publish(request, who)
    else:  # an internal caller: the claim absent, the secret verified
        request["internal_auth"] = True
    return await handler(request)


def _routed_client(*registrars, middlewares=()) -> Any:
    from aiohttp.test_utils import TestClient, TestServer

    app = _app()
    app.middlewares.extend([_identity, *middlewares])
    for register in registrars:
        register(app)
    return TestClient(TestServer(app))


@pytest.mark.asyncio
async def test_a_slash_command_sent_to_the_owners_chat_is_refused() -> None:
    """``/context clear`` through ``POST /api/chat``, naming the owner's slot.

    The send names its slot in the body, so the per-slot checkpoint decides it
    (``slot_ownership.PERSON_CHECKPOINT_ROUTES``), in front of the handler.
    """
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    async with _routed_client(
        chat_routes.register, middlewares=[slot_ownership_middleware]
    ) as client:
        resp = await client.post(
            "/api/chat",
            json={"slot": "main", "message": "/context clear"},
            headers={"X-Test-Who": "other"},
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/chat/threads?slot=main", None),
        ("GET", "/api/chat/threads/m1?slot=main", None),
        ("POST", "/api/chat/threads/m1/reply", {"slot_key": "main", "text": "hi"}),
        # A named create reopens and re-titles the owner's existing chat.
        ("POST", "/api/chat/slots", {"name": "main", "title": "changed"}),
        # The bulk chat writes, which change every chat at once.
        ("POST", "/api/chat/slots/model", {"model": "m", "skip_running": False}),
        ("POST", "/api/chat/slots/cleanup", {"max_inactive_days": 1}),
        ("GET", "/api/chat/slots/follow-default", None),
        ("POST", "/api/chat/slots/follow-default", {}),
        ("POST", "/api/chat/slots/import", {}),
    ],
)
@pytest.mark.parametrize(("who", "refused"), [("other", True), ("owner", False), ("relay", False)])
async def test_the_reply_threads_and_bulk_writes_of_chats_are_the_owners(
    method: str, path: str, body: dict | None, who: str, refused: bool
) -> None:
    """A Bot chat's reply threads name the slot in the query or body, and the bulk
    writes act on every chat, so the checkpoint decides them like ``POST /api/chat``."""
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    async with _routed_client(
        chat_routes.register, middlewares=[slot_ownership_middleware]
    ) as client:
        resp = await client.request(method, path, json=body, headers={"X-Test-Who": who})
        payload = await resp.json(content_type=None) if resp.status < 500 else {}
        is_gate = resp.status == 403 and payload.get("code") == "owner_only"
        assert is_gate is refused, (resp.status, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/chat/threads?slot=main",
        "/api/chat/threads/m1?slot=main",
        "/api/chat/slots/follow-default",
        "/api/chat/slots/main",
    ],
)
@pytest.mark.parametrize(
    ("who", "refused"), [("other", True), ("owner", False), ("relay", False), ("ssm", False)]
)
async def test_a_head_request_takes_the_checkpoint_like_its_get(
    path: str, who: str, refused: bool
) -> None:
    """``add_get`` also answers HEAD with the GET handler, so HEAD is decided as GET."""
    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    async with _routed_client(
        chat_routes.register, middlewares=[slot_ownership_middleware]
    ) as client:
        resp = await client.head(path, headers={"X-Test-Who": who})
        assert (resp.status == 403) is refused, resp.status


# ── The per-slot checkpoint ───────────────────────────────────────────────────


def _slot_routes() -> list[tuple[str, str]]:
    from kiro_crew.dashboard.slot_ownership import slot_route_param

    app = web.Application()
    chat_routes.register(app)
    return sorted(
        {
            (r.method, r.resource.canonical)
            for r in app.router.routes()
            if r.method != "HEAD" and r.resource and slot_route_param(r.resource.canonical)
        }
    )


SLOT_ROUTES = _slot_routes()


def _checkpoint_client() -> Any:
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware

    async def reached(_request: web.Request) -> web.Response:
        return web.json_response({"reached": True})

    app = _app()
    app.middlewares.extend([_identity, slot_ownership_middleware])
    for method, canonical in SLOT_ROUTES:
        app.router.add_route(method, canonical, reached)
    app.router.add_get("/api/chat/slots", reached)  # outside the family
    return TestClient(TestServer(app))


def _concrete(canonical: str) -> str:
    return "/".join("slot1" if part.startswith("{") else part for part in canonical.split("/"))


def test_the_checkpoint_walk_finds_the_family() -> None:
    assert ("DELETE", "/api/chat/slots/{slot}") in SLOT_ROUTES
    assert len(SLOT_ROUTES) >= 40


@pytest.mark.asyncio
async def test_the_checkpoint_decides_every_per_slot_route() -> None:
    failures = []
    async with _checkpoint_client() as client:
        for method, canonical in SLOT_ROUTES:
            path = _concrete(canonical)
            for who, expect in (
                ("other", 403),
                ("owner", 200),
                ("ssm", 200),
                ("relay", 200),
                ("tailscale", 200),
                ("", 200),  # an internal caller, the claim absent
            ):
                headers = {"X-Test-Who": who} if who else {}
                resp = await client.request(method, path, headers=headers)
                if resp.status != expect:
                    failures.append(f"{method} {canonical} as {who or 'internal'}: {resp.status}")
        # A route outside the family is not the checkpoint's to decide.
        resp = await client.get("/api/chat/slots", headers={"X-Test-Who": "other"})
        if resp.status != 200:
            failures.append(f"GET /api/chat/slots as other: {resp.status}")
    assert failures == []


# ── Messaging-channel dashboard links ─────────────────────────────────────────
#
# A channel mints an allow-listed sender's link under the sender's channel id: being
# the sole allow-listed id does not prove the sender owns the machine. The owner's own
# link (the Slack owner link, `kirocrew token`, or a Telegram, Teams or Webex link from
# the id the owner named for that channel) carries the owner subject with the
# owner-mint claim. These rows drive both kinds of credential through the real token
# auth, the per-slot checkpoint and the real chat routes;
# test_channel_owner_dashboard_link.py mints them through each channel's handler.


def _link_client() -> Any:
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    app = _app()
    app.middlewares.extend([token_auth_middleware(local_only=True), slot_ownership_middleware])
    chat_routes.register(app)
    return TestClient(TestServer(app))


def _owner_link() -> str:
    from kiro_crew.dashboard.token_auth import generate_token
    from kiro_crew.dashboard.token_auth_tokens import owner_mint_extra

    # The owner's own link: the configured owner's subject.
    return generate_token(OWNER, 3600, extra=owner_mint_extra())


async def _owner_only(resp) -> bool:
    if resp.status != 403:
        return False
    return (await resp.json(content_type=None)).get("code") == "owner_only"


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "refused"), [("owner link", False), ("allow-listed", True)])
async def test_a_channel_link_on_chat_send_and_a_per_slot_route(label: str, refused: bool) -> None:
    from kiro_crew.dashboard.token_auth import generate_token

    # A Telegram user id, as the channel mints it for every allow-listed user who
    # is not the named owner, including a sole one.
    token = _owner_link() if label == "owner link" else generate_token("731405", 3600)
    async with _link_client() as client:
        send = await client.post(
            "/api/chat",
            params={"token": token},
            json={"slot": "main", "message": "/context clear"},
        )
        assert await _owner_only(send) is refused
        slot = await client.delete("/api/chat/slots/main", params={"token": token})
        assert await _owner_only(slot) is refused
