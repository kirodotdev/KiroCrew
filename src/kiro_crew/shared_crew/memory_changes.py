"""Who wrote a Crew lesson, and telling everyone when a new one replaced an old one.

Crew memory is one shared store: a lesson or preference any admitted person
teaches applies to every session, the owner's included, and the owner gets no
priority. When two lessons conflict the store keeps the newest one (the lesson
store's own supersede rules decide what conflicts). This module covers the
three things around that write:

* :func:`writer_principal` names the person a write is attributed to. It is
  derived by the server, never read from the request body: the verified actor
  of a person's own dashboard request, else the author of the latest user turn
  in the session that called the lessons route (the turn whose agent called
  ``learn_add``), else Unknown.
* :func:`writer_stamp` is the reduced form stored on the lesson row
  (``written_by``): id, kind and display name. No issuer, subject or login.
* :func:`announce_replacement` runs when a write replaced stored lessons. It
  always writes one ``memory_change`` audit record. In a shared Crew it also
  appends a visible system notice to the session where the change happened and
  sends a ``shared_crew.memory_change`` frame to the owner and every connected
  collaborator for shared stores. Owner-only stores notify only owner sockets
  and add no shared transcript row.
  A personal Crew has one person, so it gets the replacement and the audit
  record but no "changed by" notice.

Summaries are redacted by the caller and clipped here (:data:`SUMMARY_MAX`), so
neither the notice nor the audit record carries a full lesson body.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from kiro_crew.shared_crew.collab import FRAME_MEMORY_CHANGE
from kiro_crew.shared_crew.contracts import (
    ActionOutcome,
    ActionType,
    ActorContext,
    Principal,
    PrincipalKind,
    Transport,
)

logger = logging.getLogger(__name__)

#: The system notice kind (``meta.kind``) the session row carries.
NOTICE_KIND = "memory_change"
#: The meta key holding the notice's structured payload.
NOTICE_META_KEY = "memory_change"
#: The websocket frame collaborators receive.
WS_FRAME = FRAME_MEMORY_CHANGE
#: The stored lesson field naming its writer.
WRITTEN_BY_KEY = "written_by"
#: Longest summary of a lesson a notice or audit record carries.
SUMMARY_MAX = 120
#: Most replaced lessons one notice or record names.
OLD_MAX = 5
_NAME_MAX = 80
_WS_RE = re.compile(r"\s+")


def summarize(text: object) -> str:
    """One line of *text*, clipped to :data:`SUMMARY_MAX` characters."""
    if not isinstance(text, str):
        return ""
    line = _WS_RE.sub(" ", text).strip()
    if len(line) <= SUMMARY_MAX:
        return line
    return line[: SUMMARY_MAX - 1].rstrip() + "…"


def display_name(principal: Principal) -> str:
    """The label people know *principal* by; empty for Unknown."""
    if principal.is_unknown:
        return ""
    name = principal.display.name or principal.display.login
    return name.strip()[:_NAME_MAX]


def writer_stamp(principal: Principal) -> dict[str, str] | None:
    """The ``written_by`` value stored on a lesson, or ``None`` for Unknown."""
    if principal.is_unknown:
        return None
    return {"id": principal.id, "kind": principal.kind.value, "name": display_name(principal)}


def stamp_of(raw: object) -> dict[str, str] | None:
    """A stored ``written_by`` value read back, or ``None`` when absent or damaged.

    A lesson written before writers were recorded has none and reads as ``None``.
    """
    if not isinstance(raw, Mapping):
        return None
    pid, kind, name = raw.get("id"), raw.get("kind"), raw.get("name", "")
    if not isinstance(pid, str) or not pid or not isinstance(kind, str):
        return None
    return {
        "id": pid[:256],
        "kind": kind[:32],
        "name": name[:_NAME_MAX] if isinstance(name, str) else "",
    }


def slot_for_session(state: Any, session_key: str) -> Any | None:
    """The dashboard slot holding *session_key*, by slot key or effective key, else ``None``."""
    if not session_key or state is None:
        return None
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, Mapping):
        return None
    slot = slots.get(session_key)
    if slot is not None:
        return slot
    try:
        from kiro_crew.dashboard.chat_utils import effective_session_key
    except Exception:  # noqa: BLE001 -- no dashboard, no slot
        return None
    for candidate in list(slots.values()):
        try:
            if getattr(candidate, "key", None) == session_key or (
                effective_session_key(candidate) == session_key
            ):
                return candidate
        except Exception:  # noqa: BLE001 -- a broken slot names nobody
            continue
    return None


def writer_principal(request: Any, state: Any, session_key: str) -> Principal:
    """The person a lesson write on *request* is attributed to.

    A person's own dashboard request names its verified actor. A write the
    agent makes (the ``learn_add`` tool posts through the internal caller)
    names the author of the latest user turn in its session. Anything else is
    Unknown.
    """
    from kiro_crew.shared_crew.decisions import turn_initiator
    from kiro_crew.shared_crew.identity import actor_of

    try:
        internal = request.get("internal_auth") is True
    except Exception:  # noqa: BLE001
        internal = False
    if not internal:
        principal = actor_of(request).principal
        if principal.kind is PrincipalKind.HUMAN:
            return principal
    slot = slot_for_session(state, session_key)
    if slot is None:
        return Principal.unknown()
    return turn_initiator(getattr(slot, "messages", None))


def crew_is_shared(state: Any, writer: Principal) -> bool:
    """Whether more than one person uses this Crew, so a change needs a notice.

    True when the writer reached the Crew over a network path, a network
    sharing path is on, or anyone other than the operator has been seen. A
    personal Crew answers False.
    """
    from kiro_crew.shared_crew.contracts import Provenance

    if writer.kind is PrincipalKind.HUMAN and writer.provenance is not Provenance.LOCAL_OPERATOR:
        return True
    try:
        from kiro_crew.shared_crew.access import setup_paths_dict
        from kiro_crew.shared_crew.crew_mode import network_configured, others_observed
        from kiro_crew.shared_crew.people import registry as people_registry

        if network_configured(setup_paths_dict()):
            return True
        observed = [obs.principal for obs in people_registry().snapshot()]
        owner_id = str(getattr(state, "owner_id", "") or "")
        return others_observed(writer, observed, owner_id=owner_id)
    except Exception:  # noqa: BLE001 -- unreadable mode posts no notice
        logger.debug("shared crew mode unreadable for memory notice", exc_info=True)
        return False


@dataclass(frozen=True)
class MemoryChange:
    """One write that replaced stored lessons."""

    actor: Principal
    session_key: str
    category: str
    new_summary: str
    old_summaries: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        """The notice meta, the websocket frame and the audit ``extra`` share this."""
        return {
            "actor": {
                "id": self.actor.id,
                "kind": self.actor.kind.value,
                "name": display_name(self.actor),
            },
            "session_key": self.session_key,
            "category": self.category,
            "new": self.new_summary,
            "old": list(self.old_summaries),
        }

    def notice_text(self) -> str:
        """The English fallback line; the dashboard renders the localized one."""
        who = display_name(self.actor) or "Someone"
        return f"{who} changed a Crew {self.category or 'lesson'}: {self.new_summary}"


def build_change(
    actor: Principal,
    *,
    session_key: str,
    category: str,
    new_rule: str,
    replaced: Iterable[str],
) -> MemoryChange | None:
    """A :class:`MemoryChange`, or ``None`` when nothing was replaced."""
    old = tuple(s for s in (summarize(r) for r in replaced) if s)[:OLD_MAX]
    if not old:
        return None
    return MemoryChange(
        actor=actor,
        session_key=session_key[:256],
        category=category if isinstance(category, str) else "",
        new_summary=summarize(new_rule),
        old_summaries=old,
    )


def audit_change(change: MemoryChange) -> Any:
    """Schedule the ``memory_change`` audit record. Never raises."""
    try:
        from kiro_crew.shared_crew.audit import EXTRA_MEMORY_CHANGE, schedule_action_audit

        payload = change.payload()
        return schedule_action_audit(
            ActionType.MEMORY_CHANGE,
            session_key=change.session_key,
            initiator=ActorContext(principal=change.actor, request_id="", transport=Transport.HTTP),
            outcome=ActionOutcome.APPLIED,
            target=f"lesson:{change.category}",
            extra={
                EXTRA_MEMORY_CHANGE: {
                    "category": payload["category"],
                    "old": payload["old"],
                    "new": payload["new"],
                }
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("memory change audit failed", exc_info=True)
        return None


def post_notice(state: Any, change: MemoryChange, *, memory_store: str = "") -> bool:
    """Send the notice only to readers authorized for its store.

    Owner-only lessons never enter a shared session transcript. Returns whether
    a session row was appended (the frame is sent either way).
    """
    from kiro_crew.dashboard.handlers._shared import lesson_store_owner_only

    owner_only = lesson_store_owner_only(memory_store)
    payload = change.payload()
    appended = False
    slot = None if owner_only else slot_for_session(state, change.session_key)
    if slot is not None:
        try:
            slot.append(
                "assistant",
                change.notice_text(),
                "msg msg-a",
                meta={"kind": NOTICE_KIND, NOTICE_META_KEY: payload},
            )
            appended = True
        except Exception:  # noqa: BLE001 -- a notice must not fail the write
            logger.debug("memory change notice append failed", exc_info=True)
    try:
        from kiro_crew.shared_crew.codec import SCHEMA_VERSION
        from kiro_crew.shared_crew.collaborators import broadcast_to_collaborators

        frame = {"type": WS_FRAME, "schema_version": SCHEMA_VERSION, "change": payload}
        if owner_only:
            state.broadcast_ws_owners(WS_FRAME, frame)
        else:
            broadcast_to_collaborators(state, WS_FRAME, frame)
    except Exception:  # noqa: BLE001
        logger.debug("memory change broadcast failed", exc_info=True)
    return appended


def announce_replacement(
    state: Any,
    actor: Principal,
    *,
    session_key: str,
    category: str,
    new_rule: str,
    replaced: Iterable[str],
    memory_store: str = "",
    shared: bool | None = None,
) -> MemoryChange | None:
    """Audit a write that replaced lessons, and in a shared Crew tell everyone.

    *shared* overrides :func:`crew_is_shared` (tests). Returns the change, or
    ``None`` when nothing was replaced.
    """
    change = build_change(
        actor, session_key=session_key, category=category, new_rule=new_rule, replaced=replaced
    )
    if change is None:
        return None
    audit_change(change)
    if crew_is_shared(state, actor) if shared is None else shared:
        post_notice(state, change, memory_store=memory_store)
    return change


__all__ = [
    "NOTICE_KIND",
    "NOTICE_META_KEY",
    "WS_FRAME",
    "WRITTEN_BY_KEY",
    "SUMMARY_MAX",
    "MemoryChange",
    "announce_replacement",
    "audit_change",
    "build_change",
    "crew_is_shared",
    "display_name",
    "post_notice",
    "stamp_of",
    "summarize",
    "writer_principal",
    "writer_stamp",
]
