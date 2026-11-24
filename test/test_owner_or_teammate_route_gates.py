"""Routes that hold the owner's data refuse a dashboard session that is not a teammate.

Covers the Task Runner project alias (``/api/projects``), the multi-agent channels
(``/api/channels``), a session's compaction threshold, turn time limit and colour,
session storage, the log-level write, the default memory read and the import
preview. Each is walked from the registered route table, so a route added to one of
these families later is checked too: the owner and an admitted Shared Crew teammate
pass, while any other dashboard session (an empty app claim, a subject that is
neither) is refused 403 ``owner_only``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.dashboard.handlers._shared_memory_stores import resolve_requested_memory_store
from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
from kiro_crew.dashboard.routes import chat as chat_routes
from kiro_crew.dashboard.routes import connections, realtime, system, taskrunner
from kiro_crew.shared_crew import owner, people
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    SSM_GUEST_REQUEST_KEY,
    SSM_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    SsmGuest,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import KIRO_ISSUER, PROVIDER_KIRO, ssm_principal

pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "U0OWNER"
INVITE = "b0ae764d1587"
ANA = "kiro-user.teammate-ana-0002"
TEAMMATES = ("ssm", "relay", "tailscale")
OTHER_USER = "U0SOMEONEELSE"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    people.reset_for_tests()
    owner.set_owner_subject(OWNER)
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        yield
    people.reset_for_tests()


def _publish(request: Any, who: str) -> None:
    """The claims token auth, the SSM gate and the relay publish for *who*."""
    request["app"] = ""
    request["is_dashboard_user"] = True
    if who == "owner":
        request["user"] = OWNER
        return
    if who == "other":
        request["user"] = OTHER_USER
        return
    if who == "ssm":
        request["user"] = f"ssm:{INVITE}"
        request[SSM_REQUEST_KEY] = True
        request[SSM_GUEST_REQUEST_KEY] = SsmGuest(invite_id=INVITE, alias="Sam")
        principal = ssm_principal(INVITE, "Sam")
    elif who == "tailscale":
        # A collaborator the direct tailnet listener admitted by its ``whois``.
        request["user"] = "tailnet:uid:42"
        request["auth_tailnet_direct"] = True
        principal = Principal(
            id=principal_id_for("tailscale", "tailnet", "carol@example.test"),
            kind=PrincipalKind.HUMAN,
            provider="tailscale",
            issuer="tailnet",
            subject="carol@example.test",
            provenance=Provenance.TAILSCALE_LISTENER,
            display=DisplaySnapshot(name="Carol", login="carol@example.test"),
        )
    else:
        request["user"] = f"relay:{ANA}"
        request[RELAY_CALLER_KEY] = RelayCaller(kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE)
        principal = Principal(
            id=principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, ANA),
            kind=PrincipalKind.HUMAN,
            provider=PROVIDER_KIRO,
            issuer=KIRO_ISSUER,
            subject=ANA,
            provenance=Provenance.KIRO_RELAY,
            display=DisplaySnapshot(name="Ana", login=""),
        )
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=principal, request_id="req", transport=Transport.HTTP
    )


class _NoChannels:
    def list_channels(self) -> list:
        return []

    def get(self, _channel_id: str) -> None:
        return None


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        owner_id=OWNER,
        task_runner=None,
        channel_manager=_NoChannels(),
        _slots={},
        conversation_log=None,
        running_session_keys=frozenset,
    )
    return app


def _request(app: web.Application, who: str, method: str, path: str, match_info=None) -> Any:
    request = make_mocked_request(method, path, app=app, match_info=match_info or {})
    _publish(request, who)
    return request


def _is_owner_refusal(resp: web.StreamResponse) -> bool:
    if resp.status != 403:
        return False
    body = resp.text if isinstance(resp, web.HTTPException) else resp.body
    return json.loads(body or b"{}").get("code") == "owner_only"


async def _answer(handler, request) -> web.StreamResponse:
    """What *handler* answers *request*; any other exception propagates and fails the test."""
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return exc


async def _refused(handler, request) -> bool:
    """Whether *handler* refused *request* at the owner gate."""
    return _is_owner_refusal(await _answer(handler, request))


async def _admitted(handler, request) -> bool:
    """Whether *handler* got past the gate and answered without a server error.

    The stub state holds no data, so an admitted caller reaches the handler's own
    answer (a 404, a 400, an empty list), never the gate's 403 and never a 5xx.
    """
    resp = await _answer(handler, request)
    return resp.status < 500 and not _is_owner_refusal(resp)


def _in_gated_family(method: str, canonical: str) -> bool:
    if canonical.startswith(("/api/channels/whatsapp", "/api/channels/weixin")):
        return False  # channel connection routes, registered by their own modules
    if canonical.startswith(("/api/projects", "/api/channels", "/api/system/session-storage")):
        return True
    if canonical.startswith("/api/chat/slots/{slot}/"):
        return canonical.rsplit("/", 1)[-1] in ("autocompact", "turn-timeout", "color")
    if canonical == "/api/logs/level":
        return method == "POST"
    return canonical == "/api/portability/preview"


def _gated_routes() -> list[tuple[str, str, Any, dict]]:
    """Every registered route of the gated families, with a concrete path."""
    app = web.Application()
    for registrar in (connections, realtime, chat_routes, system, taskrunner):
        registrar.register(app)
    picked = []
    for route in app.router.routes():
        if route.method == "HEAD":
            continue
        canonical = route.resource.canonical if route.resource else ""
        if not _in_gated_family(route.method, canonical):
            continue
        names = [part[1:-1] for part in canonical.split("/") if part.startswith("{")]
        match_info = {name: f"{name}1" for name in names}
        path = canonical
        for name, value in match_info.items():
            path = path.replace("{" + name + "}", value)
        picked.append((route.method, path, route.handler, match_info))
    return picked


GATED = _gated_routes()


def test_the_walk_finds_every_family() -> None:
    paths = {path for _method, path, _handler, _match in GATED}
    assert {
        "/api/projects",
        "/api/projects/id1",
        "/api/channels",
        "/api/channels/presets",
        "/api/channels/id1/agents/aid1/approve",
        "/api/chat/slots/slot1/autocompact",
        "/api/chat/slots/slot1/turn-timeout",
        "/api/chat/slots/slot1/color",
        "/api/system/session-storage",
        "/api/system/session-storage/cleanup",
        "/api/system/session-storage/sessions/uid1",
        "/api/logs/level",
        "/api/portability/preview",
    } <= paths
    assert len(GATED) >= 32


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "handler", "match_info"), GATED)
async def test_a_session_that_is_neither_owner_nor_teammate_is_refused(
    method: str, path: str, handler, match_info: dict
) -> None:
    request = _request(_app(), "other", method, path, match_info)
    assert await _refused(handler, request)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "handler", "match_info"), GATED)
async def test_the_owner_passes(method: str, path: str, handler, match_info: dict) -> None:
    request = _request(_app(), "owner", method, path, match_info)
    assert await _admitted(handler, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("who", TEAMMATES)
@pytest.mark.parametrize(("method", "path", "handler", "match_info"), GATED)
async def test_a_teammate_passes(
    who: str, method: str, path: str, handler, match_info: dict
) -> None:
    request = _request(_app(), who, method, path, match_info)
    assert await _admitted(handler, request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "handler", "match_info"),
    [r for r in GATED if r[1] != "/api/portability/preview"],
)
async def test_an_app_token_keeps_its_own_controls(
    method: str, path: str, handler, match_info: dict
) -> None:
    """An app claim is judged by app isolation and its manifest, never by this gate."""
    request = _request(_app(), "owner", method, path, match_info)
    request["app"] = "notes"
    request["is_dashboard_user"] = False
    assert await _admitted(handler, request)


@pytest.mark.asyncio
async def test_the_import_preview_refuses_an_app_token_like_import_and_export() -> None:
    (route,) = [r for r in GATED if r[1] == "/api/portability/preview"]
    method, path, handler, match_info = route
    request = _request(_app(), "owner", method, path, match_info)
    request["app"] = "notes"
    request["is_dashboard_user"] = False
    assert await _refused(handler, request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "refused"), [("owner", False), ("ssm", False), ("tailscale", False), ("other", True)]
)
async def test_a_memory_route_without_a_store_param(who: str, refused: bool) -> None:
    app = _app()
    request = _request(app, who, "GET", "/api/memory/preferences")
    store, refusal = await resolve_requested_memory_store(
        request, app["state"], "memory.preferences", require_ready=False
    )
    assert store == ""
    if refused:
        assert refusal is not None and _is_owner_refusal(refusal)
    else:
        assert refusal is None


@pytest.mark.asyncio
async def test_an_internal_caller_keeps_its_captured_store() -> None:
    """The internal transport leaves the app claim absent and is routed by its execution."""
    app = _app()
    request = make_mocked_request("GET", "/api/memory/preferences", app=app)
    request["internal_auth"] = True
    store, refusal = await resolve_requested_memory_store(
        request, app["state"], "memory.preferences", require_ready=False
    )
    assert (store, refusal) == ("", None)
