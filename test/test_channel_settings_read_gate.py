"""The messaging channels' settings reads answer only the owner and admitted teammates.

``GET /api/<channel>/config`` for every messaging channel, the Slack manifest, its
status and the Slack Code agent list, and the WhatsApp pairing status and group
list. They name who may talk to the Crew (each channel's allow-list), how the
owner's Slack app is set up and which groups the owner's account is in. Their
writes were already direct-local only; the reads answered any signed-in dashboard
session, including a messaging allow-list link whose subject is neither the owner
nor a teammate.

Each route is walked from the registered route table, so a read added to these
families later is checked too: the owner, an SSM invite guest, a relay teammate and
a Tailscale collaborator get the handler's own answer, any other dashboard session
is refused 403 ``owner_only``, and an internal or app caller stays under the
controls that already govern it.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.dashboard.handlers.weixin_qr import setup_weixin_routes
from kiro_crew.dashboard.handlers.whatsapp_setup import setup_whatsapp_routes
from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
from kiro_crew.dashboard.routes import messaging
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

_CHANNELS = "slack|discord|telegram|webex|imessage|wecom|feishu|teams|whatsapp|weixin"
_FAMILY = re.compile(rf"\A/api/(?:(?:{_CHANNELS})/|channels/(?:whatsapp|weixin)/)")

#: Reads in the family that already had a gate of their own: the Slack owner claim
#: and the Weixin pairing status answer only a session on the host's own browser
#: (``is_direct_local_request``), and Slack shared access asks the owner gate itself.
_ALREADY_GATED = frozenset(
    {"/api/slack/owner-claim", "/api/channels/weixin/qr/status", "/api/slack/shared-access"}
)

#: Every read this change gates. The walk must find exactly these.
EXPECTED = frozenset(
    {
        "/api/slack/config",
        "/api/slack/manifest",
        "/api/slack/manifest/status",
        "/api/slack/code-channel-agents",
        "/api/discord/config",
        "/api/telegram/config",
        "/api/webex/config",
        "/api/imessage/config",
        "/api/wecom/config",
        "/api/feishu/config",
        "/api/teams/config",
        "/api/whatsapp/config",
        "/api/whatsapp/groups",
        "/api/channels/whatsapp/qr/status",
        "/api/weixin/config",
    }
)


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


def _app() -> web.Application:
    app = web.Application()
    # No channel is connected: every admitted read answers its own "not set up" view.
    app["state"] = SimpleNamespace(owner_id=OWNER, slack_client=None, channel_transports={})
    return app


def _gated_reads() -> list[tuple[str, Any]]:
    """Every registered GET of the channel settings families, but the already-gated ones."""
    app = web.Application()
    messaging.register(app)
    setup_whatsapp_routes(app)
    setup_weixin_routes(app)
    picked = []
    for route in app.router.routes():
        canonical = route.resource.canonical if route.resource else ""
        if route.method != "GET" or not _FAMILY.match(canonical):
            continue
        if canonical in _ALREADY_GATED:
            continue
        picked.append((canonical, route.handler))
    return picked


READS = _gated_reads()
IDS = [path for path, _ in READS]


def _request(who: str, path: str) -> Any:
    request = make_mocked_request("GET", path, app=_app())
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


def test_the_walk_finds_every_channel_settings_read() -> None:
    assert set(IDS) == EXPECTED
    assert len(IDS) == len(EXPECTED)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "handler"), READS, ids=IDS)
async def test_a_session_that_is_neither_owner_nor_teammate_is_refused(path, handler) -> None:
    assert _is_owner_refusal(await _answer(handler, _request("other", path)))


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "handler"), READS, ids=IDS)
@pytest.mark.parametrize("who", ("owner", *TEAMMATES))
async def test_the_owner_and_every_teammate_kind_get_the_answer(who, path, handler) -> None:
    resp = await _answer(handler, _request(who, path))
    assert resp.status == 200, (who, path, resp.status)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "handler"), READS, ids=IDS)
async def test_an_internal_caller_is_left_to_the_internal_transport(path, handler) -> None:
    # ``token_auth`` leaves ``app`` absent for an X-Internal-Secret caller.
    request = make_mocked_request("GET", path, app=_app())
    request["internal_auth"] = True
    request["user"] = OWNER
    resp = await _answer(handler, request)
    assert resp.status == 200, (path, resp.status)
