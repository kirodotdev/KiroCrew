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
pre-registry import timing exactly (the gateway used to import the
``maybe_start_*`` factories at its own module scope). Lazy in-function imports
here would instead run those dependency graphs synchronously on the live
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

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from kiro_crew.config.loader import (
    CRED_DISCORD_BOT_TOKEN,
    CRED_FEISHU_APP_ID,
    CRED_FEISHU_APP_SECRET,
    CRED_MICROSOFT_APP_ID,
    CRED_MICROSOFT_APP_PASSWORD,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_TELEGRAM_BOT_TOKEN,
    CRED_WEBEX_BOT_TOKEN,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_WEIXIN_TOKEN,
)
from kiro_crew.discord.gateway import maybe_start_discord
from kiro_crew.feishu.gateway import maybe_start_feishu
from kiro_crew.imessage.gateway import maybe_start_imessage
from kiro_crew.messaging.registry import ChannelDescriptor
from kiro_crew.teams.gateway import maybe_start_teams
from kiro_crew.telegram.gateway import maybe_start_telegram
from kiro_crew.webex.gateway import maybe_start_webex
from kiro_crew.wecom.gateway import maybe_start_wecom
from kiro_crew.weixin.gateway import maybe_start_weixin
from kiro_crew.whatsapp.gateway import maybe_start_whatsapp


@lru_cache(maxsize=1)
def builtin_channel_descriptors() -> tuple[ChannelDescriptor, ...]:
    """Every builtin channel, in governance-membership order."""
    return (
        ChannelDescriptor(
            channel_type="slack",
            start=None,
            credentials=(CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN),
        ),
        ChannelDescriptor(
            channel_type="wecom",
            start=maybe_start_wecom,
            credentials=(CRED_WECOM_BOT_ID, CRED_WECOM_SECRET),
        ),
        ChannelDescriptor(
            channel_type="telegram",
            start=maybe_start_telegram,
            credentials=(CRED_TELEGRAM_BOT_TOKEN,),
            credential_fallbacks=((CRED_TELEGRAM_BOT_TOKEN, "bot_token"),),
        ),
        ChannelDescriptor(
            channel_type="discord",
            start=maybe_start_discord,
            credentials=(CRED_DISCORD_BOT_TOKEN,),
            credential_fallbacks=((CRED_DISCORD_BOT_TOKEN, "bot_token"),),
        ),
        ChannelDescriptor(
            channel_type="webex",
            start=maybe_start_webex,
            credentials=(CRED_WEBEX_BOT_TOKEN,),
            credential_fallbacks=((CRED_WEBEX_BOT_TOKEN, "bot_token"),),
        ),
        ChannelDescriptor(
            channel_type="teams",
            start=maybe_start_teams,
            credentials=(CRED_MICROSOFT_APP_ID, CRED_MICROSOFT_APP_PASSWORD),
            # app_password is env-only by design: the loader hardcodes "" so the
            # Azure Bot secret stays out of a file the agent can read. Only the id
            # has a fallback.
            credential_fallbacks=((CRED_MICROSOFT_APP_ID, "app_id"),),
        ),
        ChannelDescriptor(
            channel_type="weixin",
            start=maybe_start_weixin,
            credentials=(CRED_WEIXIN_TOKEN,),
            credential_fallbacks=((CRED_WEIXIN_TOKEN, "token"),),
            # weixin/gateway.py refuses to start on either half missing, so a
            # readiness answer that checked only the token would report a channel
            # as ready that the gateway then silently skips.
            required_config=("account_id",),
        ),
        # iMessage needs no credential: the transport IS the operator's own
        # Messages.app, so there is nothing to store, and an empty tuple reads as
        # "nothing missing".
        ChannelDescriptor(channel_type="imessage", start=maybe_start_imessage),
        # WhatsApp needs no credential either: it pairs by QR and keeps its own
        # session store, so `whatsapp.enabled` is the whole gate.
        ChannelDescriptor(channel_type="whatsapp", start=maybe_start_whatsapp),
        ChannelDescriptor(
            channel_type="feishu",
            start=maybe_start_feishu,
            # BOTH, because `_feishu_enabled` requires both: the gateway skips the
            # channel when either is missing, so declaring one would let readiness
            # report "credentials present" for a channel that then silently does not
            # start -- the exact failure this descriptor's data exists to prevent.
            credentials=(CRED_FEISHU_APP_ID, CRED_FEISHU_APP_SECRET),
        ),
    )


@dataclass(frozen=True)
class ChannelReadiness:
    """One channel's answer to "would this start, and if not, why not".

    Derived from data on the descriptor rather than a per-channel branch, so a
    diagnostic covers every channel — including the next one — from one loop.
    """

    channel_type: str
    enabled: bool
    missing_credentials: tuple[str, ...]
    #: Non-secret config the channel also needs to start. Reported separately from
    #: the credentials so the operator is sent to config.json rather than to their
    #: credential store.
    missing_config: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Enabled with every credential AND every required config value present.

        Says nothing about CONNECTED — a live handshake is the gateway's answer, not
        a config read's.
        """
        return self.enabled and not self.missing_credentials and not self.missing_config


def _config_credential(section: object, attr: str | None) -> bool:
    """Whether *section* carries a non-empty value for *attr*.

    The credential's other legal home. ``TELEGRAM_BOT_TOKEN`` in the environment is
    the recommended one, but ``telegram.bot_token`` in ``config.json`` starts the
    channel just as well — so a readiness check that consults only the environment
    reports a missing credential for a bot that is running, which is worse than not
    reporting at all: the operator goes looking for a problem that is not there.
    """
    if section is None or not attr:
        return False
    return bool(str(getattr(section, attr, "") or "").strip())


def channel_readiness(cfg: object, creds: Mapping[str, str]) -> tuple[ChannelReadiness, ...]:
    """Every builtin channel's readiness, in roster order.

    ``cfg`` is a :class:`~kiro_crew.config.loader.KiroCrewConfig`; each channel's
    section is looked up by its own ``channel_type``, which the contract tests pin
    as the single identity used for the config section too. A channel with no
    section reads as disabled rather than raising, so a config predating a channel
    degrades instead of breaking the diagnostic that reports it.

    Slack is the one channel whose ``enabled`` is implied by its credentials: it
    has no ``slack.enabled`` flag, because configuring the tokens IS the opt-in.
    """
    out: list[ChannelReadiness] = []
    for descriptor in builtin_channel_descriptors():
        section = getattr(cfg, descriptor.channel_type, None)
        fallbacks = dict(descriptor.credential_fallbacks)
        missing = tuple(
            key
            for key in descriptor.credentials
            if not creds.get(key) and not _config_credential(section, fallbacks.get(key))
        )
        # The fallback when a section carries no ``enabled`` field: credentials
        # present IS the opt-in. Slack is the channel that works this way, and it
        # must hold whether the section is absent or merely field-less — reading an
        # absent section as a hard "disabled" would report a fully configured Slack
        # as off.
        implied = bool(descriptor.credentials) and not missing
        enabled = implied if section is None else bool(getattr(section, "enabled", implied))
        missing_config = tuple(
            attr for attr in descriptor.required_config if not _config_credential(section, attr)
        )
        out.append(ChannelReadiness(descriptor.channel_type, enabled, missing, missing_config))
    return tuple(out)
