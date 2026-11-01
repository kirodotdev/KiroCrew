"""Slack channel -> workspace (team) id, for an agent child's environment.

An agent spawned for a Slack conversation carries ``KIROCREW_CHANNEL_ID``; a hook
that opens the conversation (``slack://channel?team=<TEAM>&id=<CHANNEL>``) also
needs the workspace the channel lives in, so the spawn adds
``KIROCREW_SLACK_TEAM_ID`` beside it.

The answer comes from the one store that already routes outbound Slack calls:
``RealSlackClient._channel_team``, filled from ``conversations.info``'s
``context_team_id`` by ``ensure_channel_team``. The client hands that dict to
:func:`use_channel_team_store` when it is built, and this module only reads it,
so the ACP spawn path gets the answer without importing the Slack client and
there is no second copy to drift. An inbound event's ``team`` is never a source:
it names the AUTHOR's workspace, which on a Slack Connect shared channel is not
the workspace the channel lives in.

There is no fallback. A channel whose home workspace was never resolved gets no
value; guessing the bot's own workspace would name the wrong one on an org-wide
or Slack Connect install, which is the case the variable exists to get right.

The value is a public workspace identifier, not a credential. Lives in
``slack/`` but imports nothing Slack-specific.
"""

from __future__ import annotations

import re
from typing import Mapping, MutableMapping

#: The child-environment variable carrying the workspace id.
SLACK_TEAM_ID_ENV = "KIROCREW_SLACK_TEAM_ID"

# Slack conversation ids: C (public), G (private / legacy group), D (DM).
_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{2,}$")
# Workspace ``T…`` or Enterprise Grid org ``E…``.
_TEAM_ID_RE = re.compile(r"^[TE][A-Z0-9]{2,}$")

_store: Mapping[str, str] | None = None


def use_channel_team_store(store: Mapping[str, str]) -> None:
    """Read channel -> home-workspace answers from *store* (the Slack client's cache)."""
    global _store
    _store = store


def slack_team_id_for(channel_id: str | None) -> str:
    """The home workspace of a Slack *channel_id*, or ``""`` when unknown / not Slack."""
    if _store is None or not (isinstance(channel_id, str) and _CHANNEL_ID_RE.match(channel_id)):
        return ""
    team = _store.get(channel_id, "")
    return team if isinstance(team, str) and _TEAM_ID_RE.match(team) else ""


def apply_slack_team_env(env: MutableMapping[str, str], channel_id: str | None) -> None:
    """Set ``KIROCREW_SLACK_TEAM_ID`` for *channel_id*, or remove it when unknown.

    Removing matters as much as setting: the child env starts from the gateway's
    own, so a stale value there must never name a workspace for a session that
    has none.
    """
    team = slack_team_id_for(channel_id)
    if team:
        env[SLACK_TEAM_ID_ENV] = team
    else:
        env.pop(SLACK_TEAM_ID_ENV, None)


def _reset_for_tests() -> None:
    global _store
    _store = None
