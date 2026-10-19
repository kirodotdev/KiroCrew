"""Fakes and builders that the chat mirror endpoint test files share.

Holds ``_prep`` (a dashboard state with slot ``s1``), ``_fake_transport``,
``_real_caps_transport`` (a fake carrying a channel's shipped capabilities),
``_mirror_gate_capabilities`` (the shipped capabilities per channel, Slack excluded) and
``_make_mirror_app``. ``test_chat_mirror.py`` imports all five and
``test_chat_mirror_link_delivery.py`` every one but ``_mirror_gate_capabilities``;
``test_cross_surface_mirror.py`` reaches ``_real_caps_transport`` through
``test_chat_mirror``.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from chat_test_helpers import _make_state

# The shipped capability objects, imported from the file that already owns the
# eight-channel roster so there is one place a new channel has to be added.
from test_options_cap_contract import _all_channel_capabilities

from kiro_crew.messaging.link import SLACK_NAMESPACE
from kiro_crew.messaging.transport import ConfiguredChannelTarget


def _mirror_gate_capabilities() -> dict[str, Any]:
    """Real capabilities per channel, minus Slack.

    Slack is refused on channel TYPE before any capability is read — its
    dedicated ``slack-link`` endpoint owns the rich thread plus streaming mirror
    — so its proactive flag never reaches the gate under test here.
    """
    caps = dict(_all_channel_capabilities())
    caps.pop(SLACK_NAMESPACE, None)
    return caps


def _make_mirror_app(state):
    from kiro_crew.dashboard.chat_mirror import (
        api_channel_targets,
        api_chat_slot_mirror_link,
        api_chat_slot_mirror_unlink,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{name}/mirror-link", api_chat_slot_mirror_link)
    app.router.add_post("/api/chat/slots/{name}/mirror-unlink", api_chat_slot_mirror_unlink)
    app.router.add_get("/api/chat/channel-targets", api_channel_targets)
    return app


def _fake_transport(
    channel_type="telegram",
    proactive=True,
    max_message_chars=4096,
    session_resume=False,
    capabilities=None,
):
    return SimpleNamespace(
        channel_type=channel_type,
        capabilities=capabilities
        or SimpleNamespace(
            supports_proactive_send=proactive,
            # The real TransportCapabilities always carries this; the mirror
            # backfill chunks to it instead of truncating, so the fake needs it
            # to exercise that path rather than the getattr fallback.
            max_message_chars=max_message_chars,
            supports_session_resume=session_resume,
        ),
        send_message=AsyncMock(return_value="mid-1"),
        configured_targets=MagicMock(
            return_value=[ConfiguredChannelTarget("user:123", f"{channel_type.title()} DM · 123")]
        ),
        resolve_configured_target=AsyncMock(return_value=("123", None)),
        # Part of the MessagingTransport contract the send ladder consults: a
        # proactive send re-checks that the link's recipient is still on the
        # roster. Permissive here so these tests keep exercising delivery;
        # test_channel_transport_outbound_authz owns the refusal path.
        may_send_to=lambda conversation_id, thread_id=None, principal="": True,
    )


def _real_caps_transport(channel_type: str):
    """A fake transport carrying the channel's SHIPPED ``TransportCapabilities``.

    The proactive gate is a capability read, so a test that hands it a
    hand-built ``SimpleNamespace`` pins the fake and not the declaration:
    flipping ``WECOM_CAPABILITIES.supports_proactive_send`` left the refusal
    green while the endpoint began accepting a channel that cannot send. Only
    the network methods stay faked.

    Copied rather than aliased: ``TransportCapabilities`` is a MUTABLE dataclass
    and the module-level object is shared process-wide, so a test that tweaked a
    field in place would silently rewrite every later test's idea of the channel.
    """
    return _fake_transport(
        channel_type, capabilities=dataclasses.replace(_mirror_gate_capabilities()[channel_type])
    )


def _prep(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    # ``get_mirror_link`` is deliberately NOT stubbed to a constant None here. The
    # shared double is dict-backed and already answers None while nothing is bound,
    # so a constant made reads disagree with writes — the endpoint's own
    # claim-then-release logic reads this accessor, and a stub that never sees the
    # claim turned the release into a silent no-op that tests could not observe.
    # Individual tests still override it where they need a specific answer.
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.get_or_create_slot("s1")
    state.push_slots_update = MagicMock()
    return state
