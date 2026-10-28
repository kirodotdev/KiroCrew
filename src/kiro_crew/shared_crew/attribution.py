"""Session creator and message author stamping.

A slot carries its :class:`SessionAttribution` on the attribute
:data:`SLOT_ATTRIBUTION_ATTR`. Three states:

* attribute absent or ``None``: a slot this process created and nobody has
  stamped yet;
* ``LEGACY_MISSING``: a slot read back from a metadata line with no
  ``attribution`` key. It stays legacy forever: it is never written back and no
  later stamp replaces it, so a legacy session never gains a fabricated
  creator;
* ``RECORDED`` or ``UNATTRIBUTED``: stamped once, written to the metadata line by
  ``metadata_codec``'s ``attribution`` row, and never replaced
  (:func:`~kiro_crew.shared_crew.contracts.keep_creator`).

The metadata line is read back verbatim on every restore, and every other save
writer carries the key unchanged because no writer lists it as owned.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections.abc import Mapping
from typing import Any

from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.contracts import (
    MESSAGE_AUTHOR_META_KEY,
    ActorContext,
    AttributionAvailability,
    Principal,
    PrincipalKind,
    SessionAttribution,
    keep_creator,
    principal_or_unknown,
)
from kiro_crew.shared_crew.trigger import SessionTrigger, trigger_of

logger = logging.getLogger(__name__)

SLOT_ATTRIBUTION_ATTR = "_attribution"


def slot_attribution(slot: Any) -> SessionAttribution | None:
    """The slot's attribution, or ``None`` when it was never stamped or read."""
    value = getattr(slot, SLOT_ATTRIBUTION_ATTR, None)
    return value if isinstance(value, SessionAttribution) else None


def _set(slot: Any, value: SessionAttribution) -> bool:
    try:
        setattr(slot, SLOT_ATTRIBUTION_ATTR, value)
    except AttributeError:
        # A slot class that does not declare the attribute cannot hold a
        # creator; the session then persists with no attribution and reads back
        # as legacy, which loses a label and grants nothing.
        logger.debug("slot %r cannot hold attribution", getattr(slot, "key", "?"))
        return False
    return True


def attribution_for(actor: ActorContext, *, now: float | None = None) -> SessionAttribution:
    """The attribution a session created by *actor* records."""
    principal = actor.principal
    availability = (
        AttributionAvailability.UNATTRIBUTED
        if principal.is_unknown
        else AttributionAvailability.RECORDED
    )
    return SessionAttribution(
        creator=principal,
        availability=availability,
        created_at=time.time() if now is None else now,
    )


def stamp_creator(
    slot: Any, actor: ActorContext, *, now: float | None = None
) -> SessionAttribution:
    """Record *actor* as the slot's creator unless the slot already has one.

    Returns the attribution the slot holds afterwards. A legacy slot keeps its
    legacy value and a stamped slot keeps its first creator.
    """
    existing = slot_attribution(slot)
    kept = keep_creator(existing, attribution_for(actor, now=now))
    if kept is not existing:
        _set(slot, kept)
    return kept


def stamp_creator_if_fresh(
    slot: Any, actor: ActorContext, *, prior_user_turns: int, now: float | None = None
) -> SessionAttribution | None:
    """Stamp the creator on the first accepted message of a never-stamped slot.

    For a slot created by a path that does not stamp at creation. The sender of
    the first user message is who started the conversation; a slot that
    already holds user turns was started by someone this process did not see,
    so it is left unstamped and reads back as legacy.
    """
    existing = slot_attribution(slot)
    if existing is not None:
        return existing
    if prior_user_turns > 0:
        return None
    return stamp_creator(slot, actor, now=now)


def restore_from_metadata(slot: Any, meta: Mapping[str, Any]) -> SessionAttribution:
    """Read the stored attribution onto a slot hydrated from *meta*."""
    value = SessionAttribution.from_metadata(meta)
    _set(slot, value)
    return value


def metadata_value(slot: Any) -> dict[str, Any] | None:
    """The ``attribution`` value a save writes, or ``None`` to write nothing.

    A legacy slot writes nothing, so the line keeps its absence and no marker
    is invented for it.
    """
    value = slot_attribution(slot)
    if value is None or value.availability is AttributionAvailability.LEGACY_MISSING:
        return None
    return value.to_dict()


def public_attribution(
    value: SessionAttribution, trigger: SessionTrigger | None = None
) -> dict[str, Any]:
    """The browser-facing attribution: the creator without issuer or subject.

    ``trigger`` is present only for a session a schedule, a webhook or the Crew
    started (:mod:`kiro_crew.shared_crew.trigger`); such a session names its
    trigger and never a person.
    """
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "availability": value.availability.value,
        "creator": value.creator.to_public_dict(),
        "created_at": value.created_at,
    }
    if trigger is not None and not is_person(value.creator):
        body["trigger"] = trigger.to_public_dict()
    return body


def public_attribution_of_slot(slot: Any) -> dict[str, Any]:
    value = slot_attribution(slot)
    trigger = trigger_of(
        key=str(getattr(slot, "key", "") or ""),
        created_by=str(getattr(slot, "_created_by", "") or ""),
        origin=str(getattr(slot, "_origin", "") or ""),
    )
    return public_attribution(value if value is not None else SessionAttribution.legacy(), trigger)


def public_attribution_from_meta(
    meta: Mapping[str, Any] | None, *, key: str = ""
) -> dict[str, Any]:
    """The public attribution of a persisted session's metadata line (and its *key*)."""
    if not isinstance(meta, Mapping):
        return public_attribution(SessionAttribution.legacy(), trigger_of(key=key))
    created_by = meta.get("created_by")
    trigger = trigger_of(key=key, created_by=created_by if isinstance(created_by, str) else "")
    return public_attribution(SessionAttribution.from_metadata(meta), trigger)


def trigger_of_public(raw: object) -> Mapping[str, Any] | None:
    """The ``trigger`` a public attribution value carries, or ``None``."""
    if not isinstance(raw, Mapping):
        return None
    trigger = raw.get("trigger")
    return trigger if isinstance(trigger, Mapping) else None


def creator_of_public(raw: object) -> Principal:
    """The creator principal named by a public attribution value."""
    if not isinstance(raw, Mapping):
        return Principal.unknown()
    return principal_or_unknown(raw.get("creator"))


def author_meta(actor: ActorContext) -> dict[str, Any]:
    """The value stored under ``meta[MESSAGE_AUTHOR_META_KEY]`` for a message.

    The public shape: a message row reaches every collaborator's browser, so it
    carries the id and label only.
    """
    return actor.principal.to_public_dict()


def stamp_message_author(meta: dict[str, Any], actor: ActorContext) -> dict[str, Any]:
    """Set the author on a message ``meta`` dict the server is about to accept.

    Overwrites any value already present: a client never names the author, so
    a client-supplied ``author`` in the incoming meta is discarded here.
    """
    meta[MESSAGE_AUTHOR_META_KEY] = author_meta(actor)
    return meta


def message_author(meta: Mapping[str, Any] | None) -> Principal:
    """The author of a stored message; a message written before authors is Unknown."""
    if not isinstance(meta, Mapping):
        return Principal.unknown()
    return principal_or_unknown(meta.get(MESSAGE_AUTHOR_META_KEY))


#: The authorized request whose handler is running in this task: ``(task, author)``.
_REQUEST_AUTHOR: contextvars.ContextVar[tuple[object, dict[str, Any]] | None] = (
    contextvars.ContextVar("shared_crew_request_author", default=None)
)


def bind_request_author(actor: ActorContext) -> None:
    """Record *actor* as the author of user rows its request's handler appends.

    Called by ``identity.publish_actor`` in the request's own task. Bound to
    that task, so a turn or drain the handler starts (a task that copies this
    context) never inherits it: those rows carry their own server-stamped author.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        _REQUEST_AUTHOR.set((task, author_meta(actor)))


def with_request_author(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """*meta* with the running request's author when it names none.

    A row that already carries an author keeps it. Outside an authorized
    request's own task this returns *meta* unchanged.
    """
    if isinstance(meta, dict) and MESSAGE_AUTHOR_META_KEY in meta:
        return meta
    bound = _REQUEST_AUTHOR.get()
    if bound is None:
        return meta
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    if current is None or current is not bound[0]:
        return meta
    out = dict(meta or {})
    out[MESSAGE_AUTHOR_META_KEY] = dict(bound[1])
    return out


def stamp_principal_author(meta: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """Set *principal* as the author of a row no request carries (channels, automation).

    A channel sender has no Shared Crew principal and is stamped Unknown; Crew
    automation is stamped with its bounded system principal. Never a client claim.
    """
    meta[MESSAGE_AUTHOR_META_KEY] = principal.to_public_dict()
    return meta


def is_person(principal: Principal) -> bool:
    return principal.kind is PrincipalKind.HUMAN


_SENDER_LABEL_MAX = 64


def _label(text: str) -> str:
    """*text* as one plain line that cannot close the ``[from: ...]`` frame."""
    kept = "".join(c if c.isprintable() else " " for c in text if c not in "[]()")
    return " ".join(kept.split())[:_SENDER_LABEL_MAX]


def _owner_ids(owner_id: str) -> frozenset[str]:
    """Every principal id this machine's owner writes under, local token or tailnet."""
    import kiro_crew.shared_crew.access_set as access_set
    from kiro_crew.shared_crew.crew_mode import LOCAL_BOOTSTRAP_SUBJECTS
    from kiro_crew.shared_crew.identity import local_operator_principal

    subjects = set(LOCAL_BOOTSTRAP_SUBJECTS) | ({owner_id} if owner_id else set())
    ids = {local_operator_principal(s).id for s in subjects}
    return frozenset(ids | access_set.principal_ids_for(access_set.owner_login(), frozenset()))


def sender_line(slot: Any, row: Any, *, owner_id: str = "") -> str:
    """The line put before a user row's text in the agent's prompt, or ``""``.

    Present when the row's server-stamped author is a person other than this
    machine's owner, or when more than one person has written in the session,
    so the agent knows who sent each message. Only the prompt carries it; the
    stored row keeps the text as typed.
    """
    if not isinstance(row, Mapping) or row.get("role") != "user":
        return ""
    author = message_author(row.get("meta"))
    if not is_person(author):
        return ""
    owners = _owner_ids(owner_id)
    people = {
        "owner" if p.id in owners else p.id
        for m in getattr(slot, "messages", None) or ()
        if isinstance(m, Mapping)
        and m.get("role") == "user"
        and is_person(p := message_author(m.get("meta")))
    }
    if author.id in owners:
        if len(people) <= 1:
            return ""
        import kiro_crew.shared_crew.access_set as access_set

        name, login = "owner", _label(access_set.owner_login())
    else:
        login = _label(author.display.login)
        name = _label(author.display.name) or login or "unknown"
    shown = f"{name} ({login})" if login and login != name else name
    return f"[from: {shown}]\n"
