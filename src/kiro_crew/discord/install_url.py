"""Discord install-URL builder (the OAuth ``authorize`` URL).

Discord publishes no app manifest, so there is nothing to mirror Slack's
manifest endpoint. Its equivalent install surface is a single OAuth
``authorize`` URL carrying three things: the application id, the scopes the
app asks for, and a permissions bitfield.

The bitfield is the part that goes wrong when it is written by hand. A magic
number in a URL says nothing about which permissions it grants, so nobody can
tell a correct one from a typo, and a typo grants either too little (the bot
reads a thread but cannot reply) or too much (the operator hands a bot
permissions Kiro Crew never uses). The named bits below are therefore the
source of truth, and :data:`THREAD_PERMISSIONS` is derived from them rather
than typed out: it must equal ``309237711936``, the number
``src/kiro_crew/docs/discord-integration.md`` documents, and
``test/test_discord_install_url.py`` pins that equality so the doc and the
code cannot drift apart.

Two install shapes exist because Discord treats DMs and guilds differently:

* **DM-only** (:func:`build_install_url` with ``dm_only=True``) requests no
  guild permissions at all. Discord delivers DM content to a bot without any
  guild grant, so this is the recommended install and the one that leaves the
  smallest footprint in the operator's servers.
* **Thread-capable** (the default) requests exactly the permissions a turn
  running inside a server thread needs, and deliberately not ``SEND_MESSAGES``:
  the bot must not be able to post in ordinary channels. ``CREATE_PUBLIC_THREADS``
  is among them because ``discord.auto_thread`` promotes a message in an
  allow-listed channel into a new public thread and runs the turn there; without
  it that path fails at the thread creation and an allowed channel silently
  answers nothing.
"""

from __future__ import annotations

import re
import urllib.parse
from functools import reduce
from operator import or_

#: Discord's OAuth2 consent endpoint. The install URL is a GET onto it.
AUTHORIZE_ENDPOINT = "https://discord.com/oauth2/authorize"

# Named Discord permission bits (Discord's own ``permissions`` bitfield).
# Kept as individual constants so a call site can name the one it means.
PERM_ADD_REACTIONS = 1 << 6
PERM_VIEW_CHANNEL = 1 << 10
PERM_READ_MESSAGE_HISTORY = 1 << 16
PERM_CREATE_PUBLIC_THREADS = 1 << 35
PERM_SEND_MESSAGES_IN_THREADS = 1 << 38

#: Every permission a thread-capable install requests, keyed by the label the
#: Discord Developer Portal shows for it, so a diagnostic can render the grant
#: as words instead of a number.
THREAD_PERMISSION_BITS: dict[str, int] = {
    "View Channel": PERM_VIEW_CHANNEL,
    "Read Message History": PERM_READ_MESSAGE_HISTORY,
    "Add Reactions": PERM_ADD_REACTIONS,
    "Send Messages in Threads": PERM_SEND_MESSAGES_IN_THREADS,
    "Create Public Threads": PERM_CREATE_PUBLIC_THREADS,
}

#: Bitwise OR of :data:`THREAD_PERMISSION_BITS`, which is ``309237711936``.
THREAD_PERMISSIONS: int = reduce(or_, THREAD_PERMISSION_BITS.values())

#: A DM-only install asks for no guild permissions. Sent explicitly rather
#: than omitted so the consent screen states "no permissions" instead of
#: leaving the reader to infer it from a missing parameter.
DM_PERMISSIONS = 0

#: ``bot`` is what makes the install possible at all; ``applications.commands``
#: is what makes the ``/`` menu appear. Both are requested on every install:
#: the slash menu is a convenience over the ``!`` text commands, so an operator
#: who declines it still has a working bot.
INSTALL_SCOPES: tuple[str, ...] = ("bot", "applications.commands")

# A Discord application id is a snowflake: digits only. The bound is generous
# because snowflakes grow with time, and it exists only to reject a value of
# the wrong KIND.
_CLIENT_ID_RE = re.compile(r"^[0-9]{15,25}$")


def build_install_url(client_id: str, *, dm_only: bool = False) -> str:
    """Return the OAuth authorize URL that installs this Discord app.

    ``dm_only`` requests :data:`DM_PERMISSIONS` (none) instead of
    :data:`THREAD_PERMISSIONS`; everything else is identical, since the scopes
    are what the app is, not what it may do.

    Raises :class:`ValueError` when *client_id* is not a Discord snowflake.
    The result is printed to a terminal and pasted into a browser, so a value
    that could carry its own query parameters or a terminal control sequence
    is refused here rather than escaped and forwarded: an operator who follows
    a URL Kiro Crew printed must be able to trust every parameter in it.
    """
    cid = str(client_id).strip()
    if not _CLIENT_ID_RE.match(cid):
        raise ValueError("client_id must be a numeric Discord application id (snowflake)")
    query = urllib.parse.urlencode(
        {
            "client_id": cid,
            # Discord accepts a space-separated scope list; urlencode renders
            # the space as "+", which is the spelling the setup doc shows.
            "scope": " ".join(INSTALL_SCOPES),
            "permissions": DM_PERMISSIONS if dm_only else THREAD_PERMISSIONS,
        }
    )
    return f"{AUTHORIZE_ENDPOINT}?{query}"
