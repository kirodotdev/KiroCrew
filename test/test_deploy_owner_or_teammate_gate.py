"""Every ``/api/deploy`` write refuses a dashboard session that is not the owner or a teammate.

The deploy routes act on the owner's cloud account with the owner's AWS profile:
publish a site, take one down, rewrite the deploy config and profiles, back-fill the
registry, confirm or dismiss a pending deploy. The routes are walked from the
registered table, so a write route added later is checked too: the owner and an
admitted Shared Crew teammate pass, while any other dashboard session (an empty app
claim, a subject that is neither) is refused 403 ``owner_only``. The GET routes are
not changed here.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from deploy_web_handlers_helpers import (  # noqa: F401 - autouse fixtures
    _force_posix_shell,
    _isolate_config,
    _mock_base_stack,
)
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
from kiro_crew.deploy import handlers
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
    # An admitted caller reaches the handler body; nothing past it may touch AWS.
    reached = (200, {"ok": True})

    async def _stub(*_a, **_kw):
        return reached

    with (
        patch("kiro_crew.sel.sel", return_value=MagicMock()),
        patch("kiro_crew.dashboard.handlers._shared.admits_cloud_deployment", return_value=True),
        patch.object(handlers, "_do_deploy", _stub),
        patch.object(handlers, "_do_recall", _stub),
        patch.object(handlers, "_do_destroy", _stub),
        patch.object(handlers.iam_mod, "reachability_check", return_value={"reachable": False}),
    ):
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


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=OWNER, _restricted_keys=set(), _slots={})
    return app


def _request(who: str, method: str, path: str, match_info: dict) -> Any:
    request = make_mocked_request(
        method,
        path,
        headers={"Content-Type": "application/json"},
        app=_app(),
        match_info=match_info,
    )
    # A body of ``{}``: every write answers its own 400/404, never a 5xx.
    request._read_bytes = b"{}"
    _publish(request, who)
    return request


def _is_owner_refusal(resp: web.StreamResponse) -> bool:
    if resp.status != 403:
        return False
    body = resp.text if isinstance(resp, web.HTTPException) else resp.body
    return json.loads(body or b"{}").get("code") == "owner_only"


async def _answer(handler, request) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return exc


async def _admitted(handler, request) -> bool:
    resp = await _answer(handler, request)
    return resp.status < 500 and not _is_owner_refusal(resp)


def _routes() -> list[tuple[str, str, Any, dict]]:
    """Every registered ``/api/deploy`` route, with a concrete path."""
    app = web.Application()
    handlers.register_routes(app)
    picked = []
    for route in app.router.routes():
        if route.method == "HEAD":
            continue
        canonical = route.resource.canonical if route.resource else ""
        if not canonical.startswith("/api/deploy/"):
            continue
        names = [part[1:-1] for part in canonical.split("/") if part.startswith("{")]
        match_info = {name: f"{name}1" for name in names}
        path = canonical
        for name, value in match_info.items():
            path = path.replace("{" + name + "}", value)
        picked.append((route.method, path, route.handler, match_info))
    return picked


ROUTES = _routes()
WRITES = [r for r in ROUTES if r[0] != "GET"]


def test_the_walk_finds_every_write() -> None:
    assert {(method, path) for method, path, _h, _m in WRITES} == {
        ("PUT", "/api/deploy/config"),
        ("POST", "/api/deploy/profiles"),
        ("PUT", "/api/deploy/profiles/name1"),
        ("DELETE", "/api/deploy/profiles/name1"),
        ("POST", "/api/deploy/verify"),
        ("POST", "/api/deploy/deploy"),
        ("POST", "/api/deploy/recall"),
        ("POST", "/api/deploy/destroy"),
        ("POST", "/api/deploy/teardown/slug1"),
        ("POST", "/api/deploy/pending/id1/confirm"),
        ("POST", "/api/deploy/pending/id1/dismiss"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "handler", "match_info"), WRITES)
async def test_a_session_that_is_neither_owner_nor_teammate_is_refused(
    method: str, path: str, handler, match_info: dict
) -> None:
    request = _request("other", method, path, match_info)
    assert _is_owner_refusal(await _answer(handler, request))


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "handler", "match_info"), WRITES)
async def test_the_owner_passes(method: str, path: str, handler, match_info: dict) -> None:
    assert await _admitted(handler, _request("owner", method, path, match_info))


@pytest.mark.asyncio
@pytest.mark.parametrize("who", TEAMMATES)
@pytest.mark.parametrize(("method", "path", "handler", "match_info"), WRITES)
async def test_a_teammate_passes(
    who: str, method: str, path: str, handler, match_info: dict
) -> None:
    assert await _admitted(handler, _request(who, method, path, match_info))


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "handler", "match_info"), WRITES)
async def test_an_internal_caller_keeps_its_own_controls(
    method: str, path: str, handler, match_info: dict
) -> None:
    """The agent transport leaves the app claim absent; ``_internal_denied`` judges it."""
    request = make_mocked_request(method, path, app=_app(), match_info=match_info)
    request._read_bytes = b"{}"
    request["internal_auth"] = True
    assert not _is_owner_refusal(await _answer(handler, request))
