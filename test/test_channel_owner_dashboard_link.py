"""A Telegram, Teams or Webex dashboard link is the owner's session only for the owner.

The owner names their own id on each channel (``telegram.owner_user_id``,
``teams.owner_id``, ``webex.owner_email``), the way ``KIROCREW_OWNER_ID`` names them
on Slack. A link that channel mints for that sender carries the dashboard owner
subject with the owner-mint claim, so chat keeps working. Any other allow-listed
sender's link, including a sole allow-listed id with no owner named, stays under
the sender's own id and is refused the owner's chat. Each row mints through the
channel's real ``/dashboard`` handler and spends the link on the real chat routes,
behind the real token auth and the per-slot checkpoint.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from _hot_reload_helpers import prime_live_sections
from shared_crew_host_isolation import host_isolation  # noqa: F401
from test_chat_route_owner_gate import _link_client, _owner_only
from test_owner_or_teammate_route_gates import OWNER, _fresh  # noqa: F401

_OWNER_TG, _OTHER_TG = 1001, 1002
_OWNER_MAIL, _OTHER_MAIL = "owner@example.com", "other@example.com"
_URL = "http://localhost:8765"
#: The dashboard the gateway attaches to each channel's dispatcher
#: (``register_channel_transport``); its configured owner is the subject the gate admits.
_STATE = SimpleNamespace(owner_id=OWNER)


def _token(reply: str) -> str:
    match = re.search(r"[?&]token=([A-Za-z0-9._~-]+)", reply)
    assert match, reply
    return match.group(1)


async def _telegram(sender: int, owner: int, allowed: set[int]) -> str:
    from telegram_helpers import _dispatcher, _dm

    d, client, _sess = _dispatcher(allowed)
    d.cfg.dashboard = SimpleNamespace(url=_URL)
    d.cfg.telegram.owner_user_id = owner
    d.dashboard_state = _STATE
    prime_live_sections(d.cfg, "telegram", "messaging", "agent")
    await d.handle_message(_dm("/kirocrew dashboard", uid=str(sender)))
    return _token(client.sent[-1][0])


async def _teams(sender: str, owner: str, aad: str = "") -> str:
    from test_teams_commands_dispatch import _Client, _dispatcher

    from kiro_crew.teams.client import TeamsInbound

    client = _Client()
    d = _dispatcher(client)
    d.cfg.teams.owner_id = owner
    d.dashboard_state = _STATE
    prime_live_sections(d.cfg, "teams", "messaging", "agent")
    await d.handle_message(
        TeamsInbound(
            conversation_id="CONV",
            conversation_type="personal",
            service_url="https://smba.trafficmanager.net/teams",
            text="/dashboard",
            user_email=sender,
            aad_object_id=aad,
            activity_id="act-1",
        )
    )
    return _token(client.sent[-1])


async def _webex(sender: str, owner: str) -> str:
    from webex_dispatch_helpers import FakeClient, FakeCtx, FakeProvider, FakeSessions
    from webex_dispatch_helpers import _cfg as webex_cfg
    from webex_dispatch_helpers import _dispatcher, _inbound

    cfg = webex_cfg()
    cfg.dashboard = SimpleNamespace(url=_URL)
    cfg.webex.allowed_emails = [_OWNER_MAIL, _OTHER_MAIL]
    cfg.webex.owner_email = owner
    d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient(), cfg=cfg)
    d.dashboard_state = _STATE
    await d.handle_message(_inbound("/kirocrew dashboard", email=sender))
    return _token(d.client.sent[-1][1])


_CASES: dict[str, Any] = {
    # The owner's own link: the sender is the id the owner named for the channel.
    "telegram owner": (lambda: _telegram(_OWNER_TG, _OWNER_TG, {_OWNER_TG, _OTHER_TG}), False),
    "teams owner": (lambda: _teams(_OWNER_MAIL, "Owner@Example.com"), False),
    # Named by AAD object id while the activity also carries the email, which the
    # allow-list resolves first: the owner is still matched on the attested id.
    "teams owner by object id": (lambda: _teams(_OWNER_MAIL, "aad-owner", "AAD-OWNER"), False),
    "webex owner": (lambda: _webex("Owner@Example.com", _OWNER_MAIL), False),
    # Another allow-listed sender, with the owner named.
    "telegram other": (lambda: _telegram(_OTHER_TG, _OWNER_TG, {_OWNER_TG, _OTHER_TG}), True),
    "teams other": (lambda: _teams(_OTHER_MAIL, _OWNER_MAIL), True),
    "webex other": (lambda: _webex(_OTHER_MAIL, _OWNER_MAIL), True),
    # The sole allow-listed id with no owner named is not proof of ownership.
    "telegram sole id, no owner": (lambda: _telegram(_OWNER_TG, 0, {_OWNER_TG}), True),
    "teams no owner": (lambda: _teams(_OWNER_MAIL, ""), True),
    "webex no owner": (lambda: _webex(_OWNER_MAIL, ""), True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_CASES))
async def test_a_channel_link_reaches_chat_only_for_the_named_owner(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.shared_crew import owner

    monkeypatch.delenv("KIROCREW_PORT", raising=False)
    # Without Shared Crew preview nothing records the owner subject process-wide,
    # so the mint must read the owner from the attached dashboard, not from there.
    owner.reset_for_tests()
    mint, refused = _CASES[case]
    token = await mint()
    async with _link_client() as client:
        send = await client.post(
            "/api/chat",
            params={"token": token},
            json={"slot": "main", "message": "/context clear"},
        )
        assert await _owner_only(send) is refused, case
        slot = await client.delete("/api/chat/slots/main", params={"token": token})
        assert await _owner_only(slot) is refused, case
