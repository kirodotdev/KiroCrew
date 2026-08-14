"""The builtin channel roster — the ONE place that knows every channel.

Adding a builtin channel = add its descriptor to :func:`builtin_channel_descriptors`.
That is the "one required edit" the channel-plugin RFC's seam collapse promises
(plus, for now, an icon and i18n keys on the frontend — measured in PR ③'s
seam audit as irreducible until the frontend registry endpoint lands).

WHY THIS MODULE EXISTS (and why the list is not in ``messaging/registry.py``):
``messaging/`` must never import a channel package — the dependency direction
``<channel> -> messaging`` is pinned in ``messaging/dispatch.py`` and is what
keeps the shared pipeline reusable. But SOMETHING has to import all the
channels to enumerate them. This module is that something: it sits above both
sides and is imported only by hosts (the gateway, dashboard handlers), never
by ``messaging/`` or by any channel package.

Imports are at MODULE scope on purpose: channel modules pull in their vendor
clients, and this module is imported by hosts (``slack/gateway.py``) at
process import time — BEFORE any event loop starts. That preserves the
pre-registry import timing exactly (the gateway used to import the six
``maybe_start_*`` at its own module scope). Lazy in-function imports here
would instead run those six dependency graphs synchronously on the live
gateway loop the first time ``_start_channel_transports()`` enumerates the
roster, stalling the dashboard mid-boot. Executor-side callers such as
``_channel_members()`` still import this module lazily on their side, which
stays correct either way.

Slack's descriptor carries ``start=None``: it is governed like every other
member, but its socket-client lifecycle is host-managed in ``_connect_slack``
(a governance deny must drop the client, not merely skip a start call), and it
deliberately connects AFTER the other channels — same boot order as before.
"""

from __future__ import annotations

from functools import lru_cache

from kiro_crew.discord.gateway import maybe_start_discord
from kiro_crew.messaging.registry import ChannelDescriptor
from kiro_crew.teams.gateway import maybe_start_teams
from kiro_crew.telegram.gateway import maybe_start_telegram
from kiro_crew.webex.gateway import maybe_start_webex
from kiro_crew.wecom.gateway import maybe_start_wecom
from kiro_crew.weixin.gateway import maybe_start_weixin


@lru_cache(maxsize=1)
def builtin_channel_descriptors() -> tuple[ChannelDescriptor, ...]:
    """Every builtin channel, in governance-membership order."""
    return (
        ChannelDescriptor(channel_type="slack", start=None),
        ChannelDescriptor(channel_type="wecom", start=maybe_start_wecom),
        ChannelDescriptor(channel_type="telegram", start=maybe_start_telegram),
        ChannelDescriptor(channel_type="discord", start=maybe_start_discord),
        ChannelDescriptor(channel_type="webex", start=maybe_start_webex),
        ChannelDescriptor(channel_type="teams", start=maybe_start_teams),
        ChannelDescriptor(channel_type="weixin", start=maybe_start_weixin),
    )
