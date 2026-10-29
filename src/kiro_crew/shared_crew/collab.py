"""Presence, typing and shared-decision contracts, plus the WebSocket frames.

Presence is transient telemetry: it is bound to the verified connection actor,
expires on its own, is never persisted, and never grants access or frees a
runtime slot. A shared decision (a tool approval or an agent question) is
authoritative state: the first valid response wins once, a retry with the same
``response_id`` returns the same outcome, and a competing response receives the
resolved state with ``ReasonCode.ALREADY_RESOLVED``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping

from kiro_crew.shared_crew.admission import AcceptedWork
from kiro_crew.shared_crew.codec import (
    SCHEMA_VERSION,
    ContractError,
    enum_or,
    merge_extra,
    opt_float,
    opt_str,
    read_schema_version,
    req_enum,
    req_float,
    req_int,
    req_str,
    require_mapping,
    split_extra,
)
from kiro_crew.shared_crew.contracts import Principal, principal_or_unknown

#: WebSocket frame ``type`` values the server pushes.
FRAME_PRESENCE = "shared_crew.presence"
FRAME_WORK = "shared_crew.work"
FRAME_DECISION = "shared_crew.decision"
#: A session's send revision and its last accepted send (``send_race``).
FRAME_SEND_STATE = "shared_crew.send_state"
#: Who holds a session's composer typing lease (``typing_lease``), or nobody.
FRAME_TYPING_LEASE = "shared_crew.typing_lease"
#: A lesson write replaced stored ones (``memory_changes``).
FRAME_MEMORY_CHANGE = "shared_crew.memory_change"
SERVER_FRAMES = frozenset(
    {
        FRAME_PRESENCE,
        FRAME_WORK,
        FRAME_DECISION,
        FRAME_SEND_STATE,
        FRAME_TYPING_LEASE,
        FRAME_MEMORY_CHANGE,
    }
)

#: WebSocket frame ``type`` the client sends to report its own activity. The
#: server ignores any principal in it and stamps the connection's actor.
FRAME_ACTIVITY = "shared_crew.activity"

#: Seconds a presence entry lives without a refresh.
PRESENCE_TTL_SECONDS = 30
#: Minimum seconds between activity frames the server accepts per connection.
ACTIVITY_MIN_INTERVAL_SECONDS = 2


class Activity(str, enum.Enum):
    VIEWING = "viewing"
    TYPING = "typing"
    IDLE = "idle"


@dataclass(frozen=True)
class PresenceEntry:
    """One person in one session. Many tabs of one principal fold into one."""

    principal: Principal
    session_key: str
    activity: Activity
    connections: int
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal": self.principal.to_public_dict(),
            "session_key": self.session_key,
            "activity": self.activity.value,
            "connections": self.connections,
            "expires_at": self.expires_at,
        }


def presence_frame(session_key: str, entries: list[PresenceEntry]) -> dict[str, Any]:
    """The whole-value presence frame for one session."""
    return {
        "type": FRAME_PRESENCE,
        "schema_version": SCHEMA_VERSION,
        "session_key": session_key,
        "entries": [e.to_dict() for e in entries],
    }


class DecisionKind(str, enum.Enum):
    APPROVAL = "approval"
    QUESTION = "question"


class DecisionState(str, enum.Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    #: The turn that asked ended before anyone answered.
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class SharedDecision:
    """An approval or question every collaborator sees and any one may answer."""

    decision_id: str
    kind: DecisionKind
    session_key: str
    state: DecisionState
    revision: int
    initiator: Principal
    created_at: float
    responder: Principal | None = None
    response_id: str = ""
    outcome: str = ""
    resolved_at: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    _KNOWN = frozenset(
        {
            "schema_version",
            "decision_id",
            "kind",
            "session_key",
            "state",
            "revision",
            "initiator",
            "created_at",
            "responder",
            "response_id",
            "outcome",
            "resolved_at",
        }
    )

    def __post_init__(self) -> None:
        if self.state is DecisionState.RESOLVED and (self.responder is None or not self.outcome):
            raise ContractError(
                "malformed_record", "responder", "a resolved decision names its responder"
            )

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "kind": self.kind.value,
            "session_key": self.session_key,
            "state": self.state.value,
            "revision": self.revision,
            "initiator": self.initiator.to_dict(),
            "created_at": self.created_at,
            "responder": None if self.responder is None else self.responder.to_dict(),
            "response_id": self.response_id,
            "outcome": self.outcome,
            "resolved_at": self.resolved_at,
        }
        return merge_extra(body, self.extra)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "kind": self.kind.value,
            "session_key": self.session_key,
            "state": self.state.value,
            "revision": self.revision,
            "initiator": self.initiator.to_public_dict(),
            "responder": None if self.responder is None else self.responder.to_public_dict(),
            "outcome": self.outcome,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SharedDecision:
        data = require_mapping(raw, "decision")
        read_schema_version(data)
        responder_raw = data.get("responder")
        return cls(
            decision_id=req_str(data, "decision_id"),
            kind=req_enum(data, "kind", DecisionKind),
            session_key=req_str(data, "session_key"),
            state=enum_or(data, "state", DecisionState, DecisionState.PENDING),
            revision=req_int(data, "revision"),
            initiator=principal_or_unknown(data.get("initiator")),
            created_at=req_float(data, "created_at"),
            responder=None if responder_raw is None else principal_or_unknown(responder_raw),
            response_id=opt_str(data, "response_id") or "",
            outcome=opt_str(data, "outcome") or "",
            resolved_at=opt_float(data, "resolved_at"),
            extra=split_extra(data, cls._KNOWN),
        )


class ResolveOutcome(str, enum.Enum):
    """What one response to a shared decision achieved."""

    WON = "won"
    #: Same ``response_id`` as the winning response: the same answer again.
    REPLAYED = "replayed"
    #: Another response already won; the caller receives the current state.
    ALREADY_RESOLVED = "already_resolved"
    NOT_PENDING = "not_pending"


def classify_response(decision: SharedDecision, response_id: str) -> ResolveOutcome:
    """Decide a response against the decision's current state.

    The resolver applies this and the state write under one lock, so the
    first caller to see ``PENDING`` is the only one that can ``WON``.
    """
    if decision.state is DecisionState.PENDING:
        return ResolveOutcome.WON
    if decision.state is DecisionState.RESOLVED:
        if response_id and response_id == decision.response_id:
            return ResolveOutcome.REPLAYED
        return ResolveOutcome.ALREADY_RESOLVED
    return ResolveOutcome.NOT_PENDING


def decision_frame(decision: SharedDecision) -> dict[str, Any]:
    return {
        "type": FRAME_DECISION,
        "schema_version": SCHEMA_VERSION,
        "decision": decision.to_public_dict(),
    }


def work_frame(work: AcceptedWork, *, slot_key: str = "") -> dict[str, Any]:
    """The whole-value frame for one accepted work item's current state.

    *slot_key* names the dashboard tab showing the work's session (a chat
    session's key is ``dashboard:<slot>``, the tab's is ``<slot>``), so the
    browser files the item under the key its work list reads.
    """
    frame: dict[str, Any] = {
        "type": FRAME_WORK,
        "schema_version": SCHEMA_VERSION,
        "work": work.to_public_dict(),
    }
    if slot_key:
        frame["slot_key"] = slot_key
    return frame
