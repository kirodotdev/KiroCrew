"""Privileged-intent grant probe for the Discord application.

Discord's three privileged Gateway intents (Message Content, Server Members,
Presence) are toggled in the Developer Portal, never in Kiro Crew's config, so
the two can disagree without anything on the Kiro Crew side noticing. A thread
allow-list can be perfectly configured while Message Content is off, and the
result is not an error but silence: Discord delivers thread messages with empty
content, or closes the Gateway with code 4014. The app itself is the only
witness, and it reports the state as a flags bitfield on
``GET /oauth2/applications/@me``.

Each intent occupies a PAIR of flag bits: an unlimited bit, set once the app is
verified for 100 or more servers, and a limited bit for the ordinary
unverified case. Both mean the toggle is ON, so a grant decodes to a tri-state
(:data:`INTENT_ENABLED` / :data:`INTENT_LIMITED` / :data:`INTENT_DISABLED`) and
never to a bare boolean. A boolean built from the unlimited bit alone reports
every working small install as "off", which is the opposite of the truth and
sends the operator to re-toggle a switch that is already on.

This is the Discord analogue of :mod:`kiro_crew.slack.scope_probe`, and it
carries the same two contracts:

* **Read-only.** One GET with no body. It reads what the install granted and
  changes nothing.
* **Never fatal.** Every failure path (no token, rejected token, HTTP error,
  malformed body, network fault) returns :data:`INTENT_UNKNOWN` with a short
  reason instead of raising, because its caller is ``kirocrew doctor``, and a
  diagnostic that dies on a network hiccup produces no report at all.

The probe owns its own :class:`aiohttp.ClientSession` and is driven from the
caller's own event loop (``kirocrew doctor`` gives it a throwaway
``asyncio.run``), so it never borrows the gateway's loop or the live
:class:`~kiro_crew.discord.client.DiscordClient` session, whose REST ladder
serves message traffic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

import aiohttp

logger = logging.getLogger(__name__)

#: The app's own install record. Authenticated with the bot token; returns the
#: application object, whose ``flags`` field carries the intent grants.
APPLICATION_URL = "https://discord.com/api/v10/oauth2/applications/@me"

#: Bounded so a wedged connection cannot stall the doctor run.
PROBE_TIMEOUT_SECS = 8.0

# Tri-state values. "limited" is a GRANTED state: the intent is on and capped
# at 100 servers until the app is verified.
INTENT_ENABLED = "enabled"
INTENT_LIMITED = "limited"
INTENT_DISABLED = "disabled"
INTENT_UNKNOWN = "unknown"

#: The states in which Discord actually delivers the intent's data.
GRANTED_STATES = frozenset({INTENT_ENABLED, INTENT_LIMITED})

# Discord application flag bits, unlimited/limited per intent. The pairing is
# per intent, not positional: Presence owns the low pair, Server Members the
# middle one, and Message Content the high one, with unrelated flags (16, 17)
# sitting between the last two.
FLAG_GATEWAY_PRESENCE = 1 << 12
FLAG_GATEWAY_PRESENCE_LIMITED = 1 << 13
FLAG_GATEWAY_GUILD_MEMBERS = 1 << 14
FLAG_GATEWAY_GUILD_MEMBERS_LIMITED = 1 << 15
FLAG_GATEWAY_MESSAGE_CONTENT = 1 << 18
FLAG_GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19

# An application id is a snowflake (digits only). It is remote input that ends
# up in a printed install URL, so anything else is dropped rather than shown.
_SNOWFLAKE_RE = re.compile(r"^[0-9]{15,25}$")


@dataclass(frozen=True)
class IntentGrants:
    """What Discord says the app's privileged intents are set to.

    Defaults are :data:`INTENT_UNKNOWN` so a failure path constructs a valid,
    honest result with no per-field bookkeeping, and ``known`` is False for it.
    ``error`` is a short reason (an exception type name or a status code), never
    a token and never a response body.
    """

    message_content: str = INTENT_UNKNOWN
    server_members: str = INTENT_UNKNOWN
    presence: str = INTENT_UNKNOWN
    application_id: str = ""
    error: str = ""

    @property
    def known(self) -> bool:
        """True when the probe decoded a real grant set."""
        return self.message_content != INTENT_UNKNOWN


def _tri_state(flags: int, unlimited: int, limited: int) -> str:
    """Decode one intent's bit pair. Either bit set means the toggle is on."""
    if flags & unlimited:
        return INTENT_ENABLED
    if flags & limited:
        return INTENT_LIMITED
    return INTENT_DISABLED


def decode_intent_flags(flags: object) -> IntentGrants:
    """Decode an application ``flags`` bitfield into the three tri-states.

    Anything that is not a non-negative integer (a missing key, a string, a
    bool, a float) decodes to all-unknown with a reason: a malformed bitfield
    must not be read as "every intent is off", which would tell the operator
    to enable a switch that may already be on.
    """
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        return IntentGrants(error="malformed intent flags")
    return IntentGrants(
        message_content=_tri_state(
            flags, FLAG_GATEWAY_MESSAGE_CONTENT, FLAG_GATEWAY_MESSAGE_CONTENT_LIMITED
        ),
        server_members=_tri_state(
            flags, FLAG_GATEWAY_GUILD_MEMBERS, FLAG_GATEWAY_GUILD_MEMBERS_LIMITED
        ),
        presence=_tri_state(flags, FLAG_GATEWAY_PRESENCE, FLAG_GATEWAY_PRESENCE_LIMITED),
    )


async def probe_intent_grants(token: str, *, timeout: float = PROBE_TIMEOUT_SECS) -> IntentGrants:
    """Read the app's privileged-intent grants from Discord. Never raises.

    Returns an :class:`IntentGrants` whose fields are :data:`INTENT_UNKNOWN`
    and whose ``error`` names the reason whenever the answer cannot be
    obtained. ``error`` carries only an exception type name or a status code:
    a response body can echo request material, and this call is authenticated
    with a bot token.
    """
    if not token:
        return IntentGrants(error="no bot token")
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(
                APPLICATION_URL, headers={"Authorization": f"Bot {token}"}
            ) as resp:
                status = resp.status
                if status == 401:
                    return IntentGrants(error="Discord rejected the bot token (401)")
                if not 200 <= status < 300:
                    return IntentGrants(error=f"HTTP {status}")
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must always answer
        logger.debug("Discord intent probe failed: %s", type(exc).__name__)
        return IntentGrants(error=type(exc).__name__)
    if not isinstance(data, dict):
        return IntentGrants(error="unexpected response body")
    app_id = str(data.get("id", "") or "")
    return replace(
        decode_intent_flags(data.get("flags")),
        application_id=app_id if _SNOWFLAKE_RE.match(app_id) else "",
    )
