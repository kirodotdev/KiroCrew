"""Slack senders as Shared Crew principals: attribution, never access.

A Slack message reaches the Crew because the Slack allowlist admitted it, not
through a Shared Crew membership boundary. This module only names who sent it,
so a transcript row, a Slack-born session's creator and a decision answered in
Slack can say "Alice, via Slack". It grants nothing:

* the provenance is :attr:`~kiro_crew.shared_crew.contracts.Provenance.SLACK_EVENT`,
  which :mod:`~kiro_crew.shared_crew.collaborators` never admits;
* the provider is a channel provider
  (:data:`~kiro_crew.shared_crew.contracts.CHANNEL_PROVIDERS`), so the principal
  owns no admission budget (``turn_gate.queued_initiator``) and is no evidence
  of a team path (``crew_mode.others_observed``);
* a bot or app sender is ``machine``, never ``human``.

The ``slack_sender`` contract
-----------------------------

Every Slack path that appends a person's user row puts the sender's raw Slack
ids on it, ``meta["slack_sender"] = {"team_id": "T…", "enterprise_id": "E…" or
"", "user_id": "U…"}`` (:func:`sender_meta`). No name, no token. The two
append doors -- ``ConversationLog.append`` (a channel session's own transcript)
and ``_ChatSlot.append`` (a dashboard session's window, which a linked thread
and a code channel route into) -- both call :func:`stamp_sender_author`, the ONE
reader that turns the key into ``meta["author"]``. A missing or malformed key
leaves the row as it was (unattributed); nothing is guessed.

The id is :func:`~kiro_crew.shared_crew.contracts.principal_id_for` over
``("slack", <enterprise id, else team id>, <user id>)``. The same person on
Slack and on the tailnet stays two principals: nothing links them by email or
name (the contract's no-email-linking rule).
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from kiro_crew.shared_crew.contracts import (
    MESSAGE_AUTHOR_META_KEY,
    SLACK_SENDER_META_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
    principal_or_unknown,
)

PROVIDER_SLACK = "slack"
#: Slack's own system user. It posts on its own behalf, never a person's.
SLACKBOT_USER_ID = "USLACKBOT"
_DISPLAY_MAX = 128

#: The id shapes Slack issues. A user is ``U…``/``W…`` (Enterprise Grid), so a
#: bot id (``B…``) never matches; a workspace is ``T…``; an org is ``E…``.
_TEAM_RE = re.compile(r"\AT[A-Z0-9]{1,31}\Z")
_ENTERPRISE_RE = re.compile(r"\AE[A-Z0-9]{1,31}\Z")
_USER_RE = re.compile(r"\A[UW][A-Z0-9]{1,31}\Z")

# -- display names ---------------------------------------------------------

#: Where a sender's display name is read: the gateway's ``users.info`` cache
#: (``ChannelHistory.user_name``), registered at boot. Never a network call.
_name_source: Callable[[str], str] | None = None
_name_lock = threading.Lock()


def set_display_name_source(source: Callable[[str], str] | None) -> None:
    """Register (or clear) the ``user id -> display name`` cache reader."""
    global _name_source
    with _name_lock:
        _name_source = source


def _display_name(user_id: str) -> str:
    source = _name_source
    if source is None:
        return ""
    try:
        name = source(user_id)
    except Exception:  # noqa: BLE001 -- a label only
        return ""
    return name if isinstance(name, str) else ""


def _clean_label(label: object) -> str:
    """A display label without control or format characters, bounded."""
    if not isinstance(label, str):
        return ""
    kept = "".join(ch for ch in label if unicodedata.category(ch) not in ("Cc", "Cf"))
    return " ".join(kept.split())[:_DISPLAY_MAX]


# -- the principal ---------------------------------------------------------


def _self_user_id() -> str:
    try:
        from kiro_crew.slack.enterprise import validated_self_user_id

        return validated_self_user_id()
    except Exception:  # noqa: BLE001
        return ""


def _is_machine(user_id: str) -> bool:
    """A Slack id that never belongs to a person: Slackbot or this app's own user."""
    if user_id == SLACKBOT_USER_ID:
        return True
    own = _self_user_id()
    return bool(own) and user_id == own


def slack_principal(
    user_id: str,
    *,
    team_id: str,
    enterprise_id: str = "",
    display_name: str | None = None,
    now: float | None = None,
) -> Principal:
    """The principal for a Slack sender named by well-formed raw ids.

    *display_name* defaults to the registered ``users.info`` cache. It is a
    label only and never part of the id, and is cleaned of control and
    format characters (bidi overrides could otherwise disguise a byline). With
    no name, or a "name" that is just the raw user id (what a failed
    ``users.info`` lookup leaves cached), it is empty -- the raw user id is
    never shown -- and a surface
    labels the person by their channel. Ids that do not have Slack's shapes
    give Unknown.
    """
    if not (_USER_RE.match(user_id or "") and _TEAM_RE.match(team_id or "")):
        return Principal.unknown()
    if enterprise_id and not _ENTERPRISE_RE.match(enterprise_id):
        return Principal.unknown()
    issuer = enterprise_id or team_id
    label = _display_name(user_id) if display_name is None else display_name
    name = _clean_label(label)
    if name == user_id:
        # A failed ``users.info`` lookup can leave the raw id cached as the
        # name. That is no name: the surface labels the person by channel.
        name = ""
    return Principal(
        id=principal_id_for(PROVIDER_SLACK, issuer, user_id),
        kind=PrincipalKind.MACHINE if _is_machine(user_id) else PrincipalKind.HUMAN,
        provider=PROVIDER_SLACK,
        issuer=issuer,
        subject=user_id,
        provenance=Provenance.SLACK_EVENT,
        display=DisplaySnapshot(
            name=name, login="", captured_at=time.time() if now is None else now
        ),
    )


def slack_actor(principal: Principal) -> ActorContext:
    """The actor of one Slack event, for the attribution helpers that take one."""
    return ActorContext(
        principal=principal, request_id=uuid.uuid4().hex, transport=Transport.CHANNEL
    )


# -- the slack_sender contract ---------------------------------------------


def sender_meta(*, team_id: str, user_id: str, enterprise_id: str = "") -> dict[str, str] | None:
    """The ``meta["slack_sender"]`` value for a person's Slack message, else ``None``.

    ``None`` when an id does not have Slack's shape, so a writer stamps nothing
    rather than something a reader would refuse. A caller passes ``None`` for a
    bot's message: only a person's row carries the key.
    """
    value = {
        "team_id": team_id or "",
        "enterprise_id": enterprise_id or "",
        "user_id": user_id or "",
    }
    return value if read_sender(value) is not None else None


def gateway_sender_meta(user_id: str, team_id: str = "") -> dict[str, str] | None:
    """:func:`sender_meta` scoped by the gateway's validated install.

    *team_id* is the event's workspace (the gateway's own when empty); the
    enterprise id is the validated install's org, so one person in an
    Enterprise Grid org is one principal in every workspace of it. When startup
    validation has not verified the install (no validated workspace), whether
    it is an Enterprise Grid org is unknown, so no sender is written: a guessed
    scope would give the same person a different id once validation succeeds.
    """
    try:
        from kiro_crew.slack.enterprise import validated_workspace

        validated_team, enterprise = validated_workspace()
    except Exception:  # noqa: BLE001 -- no Slack module means no workspace scope
        validated_team, enterprise = "", ""
    if not validated_team:
        return None
    return sender_meta(team_id=team_id or validated_team, user_id=user_id, enterprise_id=enterprise)


def gateway_principal(user_id: str, team_id: str = "") -> Principal:
    """The principal of a Slack person acting on this gateway (a button click), else Unknown."""
    principal = sender_principal(gateway_sender_meta(user_id, team_id))
    return principal if principal is not None else Principal.unknown()


def read_sender(raw: object) -> tuple[str, str, str] | None:
    """``(team_id, enterprise_id, user_id)`` from a well-formed value, else ``None``."""
    if not isinstance(raw, Mapping):
        return None
    team, enterprise, user = raw.get("team_id"), raw.get("enterprise_id", ""), raw.get("user_id")
    if not (isinstance(team, str) and isinstance(user, str) and isinstance(enterprise, str)):
        return None
    if not (_TEAM_RE.match(team) and _USER_RE.match(user)):
        return None
    if enterprise and not _ENTERPRISE_RE.match(enterprise):
        return None
    return team, enterprise, user


def sender_principal(raw: object, *, now: float | None = None) -> Principal | None:
    """The principal a ``slack_sender`` value names, or ``None`` when it names none."""
    ids = read_sender(raw)
    if ids is None:
        return None
    team, enterprise, user = ids
    return slack_principal(user, team_id=team, enterprise_id=enterprise, now=now)


def stamp_sender_author(meta: dict[str, Any] | None) -> Principal | None:
    """The one reader: turn ``meta["slack_sender"]`` into ``meta["author"]``.

    Called by both append doors for a user row. The key is CONSUMED: it is
    removed whatever it holds, so the raw Slack ids never reach the stored row
    or a browser (the public author carries none). A row that already names a
    known author keeps it; a well-formed key replaces an absent or Unknown
    author. Returns the stamped principal, or ``None`` when nothing was stamped
    (no key, a malformed key, or a known author already there).
    """
    if not isinstance(meta, dict) or SLACK_SENDER_META_KEY not in meta:
        return None
    raw = meta.pop(SLACK_SENDER_META_KEY)
    existing = meta.get(MESSAGE_AUTHOR_META_KEY)
    if existing is not None and not principal_or_unknown(existing).is_unknown:
        return None
    principal = sender_principal(raw)
    if principal is None or principal.is_unknown:
        return None
    meta[MESSAGE_AUTHOR_META_KEY] = principal.to_public_dict()
    return principal


def creator_attribution(principal: Principal, *, now: float | None = None) -> dict[str, Any] | None:
    """The ``attribution`` metadata value of a session this sender started.

    ``None`` for a sender that is not a person: such a session records no
    creator and reads back as legacy, as before this module existed.
    """
    from kiro_crew.shared_crew.attribution import attribution_for

    if principal.kind is not PrincipalKind.HUMAN:
        return None
    return attribution_for(slack_actor(principal), now=now).to_dict()


__all__ = [
    "PROVIDER_SLACK",
    "SLACKBOT_USER_ID",
    "creator_attribution",
    "gateway_principal",
    "gateway_sender_meta",
    "read_sender",
    "sender_meta",
    "sender_principal",
    "set_display_name_source",
    "slack_actor",
    "slack_principal",
    "stamp_sender_author",
]
