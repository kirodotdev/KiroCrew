"""Identity, attribution, audit and capability contracts for Shared Crew.

These are value types only. Nothing here reads a request, a config file or the
network; the behaviour modules that derive a principal or persist a record
import these shapes so every lane agrees on one wire and storage format.

Trust rule: a :class:`Principal` or :class:`ActorContext` is only ever built by
server code from an authenticated request (``token_auth_middleware`` plus the
daemon-verified tailnet peer) or from a record this server wrote. ``from_dict``
exists for reading persisted records back; it is never applied to a request
body, query parameter or header, because a browser-supplied identity is a claim
and not an attribution.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from kiro_crew.shared_crew.codec import (
    SCHEMA_VERSION,
    ContractError,
    enum_or,
    merge_extra,
    opt_float,
    opt_str,
    read_schema_version,
    req_str,
    require_mapping,
    split_extra,
)

#: aiohttp request key under which the trusted actor is published to handlers.
ACTOR_REQUEST_KEY = "shared_crew_actor"

#: Session metadata key that holds the :class:`SessionAttribution` object. It
#: is deliberately not ``created_by``, which already names the creating slot key.
SESSION_ATTRIBUTION_META_KEY = "attribution"

#: Message ``meta`` key that holds the accepted message's author principal.
MESSAGE_AUTHOR_META_KEY = "author"

UNKNOWN_PRINCIPAL_ID = "p_unknown"

_PRINCIPAL_ID_RE = re.compile(r"\Ap_[a-z0-9_]{1,64}\Z")
_DISPLAY_MAX = 128


class PrincipalKind(str, enum.Enum):
    """What a principal stands for."""

    HUMAN = "human"
    #: A device with no personal identity, such as an ACL-tagged tailnet node.
    MACHINE = "machine"
    #: Automation the Crew runs on its own behalf (cron, heartbeat, recovery).
    SYSTEM = "system"
    UNKNOWN = "unknown"


class Provenance(str, enum.Enum):
    """How the server established a principal."""

    #: Tailscale daemon ``whois`` of the address behind the local serve proxy.
    TAILSCALE_SERVE = "tailscale_serve"
    #: Tailscale daemon ``whois`` of a peer connecting to a tailnet address directly.
    TAILSCALE_DIRECT = "tailscale_direct"
    #: Tailscale daemon ``whois`` of a peer the direct tailnet listener admitted
    #: through its access set (``tailnet_direct.DirectGate``): a verified member.
    TAILSCALE_LISTENER = "tailscale_listener"
    #: The single local operator: a loopback or owner-minted dashboard token.
    LOCAL_OPERATOR = "local_operator"
    #: Server-internal automation with a named owner component.
    INTERNAL = "internal"
    NONE = "none"


@dataclass(frozen=True)
class DisplaySnapshot:
    """A display label captured when the principal was seen.

    Display data never identifies anyone: a rename changes this snapshot and
    leaves :attr:`Principal.id` unchanged. No avatar URL is carried, because a
    remote URL in a shared record is an outbound fetch someone else chose.
    """

    name: str = ""
    login: str = ""
    captured_at: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    _KNOWN = frozenset({"name", "login", "captured_at"})

    def to_dict(self) -> dict[str, Any]:
        body = {"name": self.name, "login": self.login, "captured_at": self.captured_at}
        return merge_extra(body, self.extra)

    def to_public_dict(self) -> dict[str, Any]:
        """The browser-facing label: only the fields this build owns."""
        return {"name": self.name, "login": self.login, "captured_at": self.captured_at}

    @classmethod
    def from_dict(cls, raw: object) -> DisplaySnapshot:
        data = require_mapping(raw, "display")
        return cls(
            name=(opt_str(data, "name") or "")[:_DISPLAY_MAX],
            login=(opt_str(data, "login") or "")[:_DISPLAY_MAX],
            captured_at=opt_float(data, "captured_at"),
            extra=split_extra(data, cls._KNOWN),
        )


def principal_id_for(provider: str, issuer: str, subject: str) -> str:
    """Return the opaque principal id for a verified (provider, issuer, subject).

    Deterministic so the same person maps to one id on every device and tab
    without a registry, and opaque so the id never leaks the login it was cut
    from. Email or display name is never an input: two providers asserting the
    same email are two principals until an authoritative link says otherwise.
    """
    if not provider or not subject:
        raise ContractError("malformed_record", "subject", "provider and subject are required")
    digest = hashlib.sha256(f"{provider}\x1f{issuer}\x1f{subject}".encode()).hexdigest()
    return f"p_{digest[:32]}"


@dataclass(frozen=True)
class Principal:
    """A server-issued identity that work and records are attributed to."""

    id: str
    kind: PrincipalKind
    provider: str = ""
    issuer: str = ""
    subject: str = ""
    provenance: Provenance = Provenance.NONE
    display: DisplaySnapshot = field(default_factory=DisplaySnapshot)
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)
    #: The version the record was read at, written back unchanged so a rewrite
    #: by this build never relabels a newer record as this build's version.
    schema_version: int = field(default=SCHEMA_VERSION, compare=False)

    _KNOWN = frozenset(
        {"schema_version", "id", "kind", "provider", "issuer", "subject", "provenance", "display"}
    )

    def __post_init__(self) -> None:
        if not _PRINCIPAL_ID_RE.match(self.id):
            raise ContractError("malformed_record", "id", "not a principal id")

    @classmethod
    def unknown(cls) -> Principal:
        """The explicit Unknown principal for legacy or unattributable records.

        Stored as kind ``unknown``; every surface displays it as Unattributed.
        """
        return cls(id=UNKNOWN_PRINCIPAL_ID, kind=PrincipalKind.UNKNOWN)

    @classmethod
    def system(cls, component: str) -> Principal:
        """A bounded automation owner, one per named Crew component."""
        return cls(
            id=principal_id_for("system", "crew", component),
            kind=PrincipalKind.SYSTEM,
            provider="system",
            issuer="crew",
            subject=component,
            provenance=Provenance.INTERNAL,
            display=DisplaySnapshot(name=component),
        )

    @property
    def is_unknown(self) -> bool:
        return self.kind is PrincipalKind.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind.value,
            "provider": self.provider,
            "issuer": self.issuer,
            "subject": self.subject,
            "provenance": self.provenance.value,
            "display": self.display.to_dict(),
        }
        return merge_extra(body, self.extra)

    def to_public_dict(self) -> dict[str, Any]:
        """The browser-facing shape: no issuer or subject, only id and label."""
        return {"id": self.id, "kind": self.kind.value, "display": self.display.to_public_dict()}

    @classmethod
    def from_dict(cls, raw: object) -> Principal:
        data = require_mapping(raw, "principal")
        version = read_schema_version(data)
        display_raw = data.get("display")
        return cls(
            id=req_str(data, "id"),
            kind=enum_or(data, "kind", PrincipalKind, PrincipalKind.UNKNOWN),
            provider=opt_str(data, "provider") or "",
            issuer=opt_str(data, "issuer") or "",
            subject=opt_str(data, "subject") or "",
            provenance=enum_or(data, "provenance", Provenance, Provenance.NONE),
            display=(
                DisplaySnapshot() if display_raw is None else DisplaySnapshot.from_dict(display_raw)
            ),
            extra=split_extra(data, cls._KNOWN),
            schema_version=version,
        )


def principal_or_unknown(raw: object) -> Principal:
    """Decode a stored principal; absent or unreadable data is Unknown.

    For DISPLAY of historical records only. Authorization never reads a stored
    principal, so a damaged record can only lose its label, never gain access.
    """
    if raw is None:
        return Principal.unknown()
    try:
        return Principal.from_dict(raw)
    except ContractError:
        return Principal.unknown()


class Transport(str, enum.Enum):
    """The channel a request or action arrived through."""

    HTTP = "http"
    WEBSOCKET = "websocket"
    CHANNEL = "channel"
    CRON = "cron"
    SUBAGENT = "subagent"
    WORKFLOW = "workflow"
    REMOTE = "remote"
    INTERNAL = "internal"


@dataclass(frozen=True)
class AuthSnapshot:
    """What the existing auth middleware decided, copied for attribution.

    A copy, not a second decision: attribution never re-evaluates access.
    """

    token_subject: str = ""
    app: str = ""
    is_dashboard_user: bool = False
    pin_scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_subject": self.token_subject,
            "app": self.app,
            "is_dashboard_user": self.is_dashboard_user,
            "pin_scope": self.pin_scope,
        }


@dataclass(frozen=True)
class ActorContext:
    """The trusted caller of one request or action."""

    principal: Principal
    request_id: str
    transport: Transport
    correlation_id: str = ""
    auth: AuthSnapshot = field(default_factory=AuthSnapshot)
    #: The execution this action continues, for work started by other work.
    parent_execution_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "principal": self.principal.to_dict(),
            "request_id": self.request_id,
            "transport": self.transport.value,
            "correlation_id": self.correlation_id,
            "auth": self.auth.to_dict(),
            "parent_execution_id": self.parent_execution_id,
        }


class AttributionAvailability(str, enum.Enum):
    """Whether a session's creator is known."""

    RECORDED = "recorded"
    #: The session predates attribution; the creator displays as Unattributed.
    LEGACY_MISSING = "legacy_missing"
    #: Attribution was on but no principal could be established at creation.
    UNATTRIBUTED = "unattributed"


@dataclass(frozen=True)
class SessionAttribution:
    """Immutable creation attribution stored on the session metadata line."""

    creator: Principal
    availability: AttributionAvailability
    created_at: float | None = None
    schema_version: int = SCHEMA_VERSION
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    _KNOWN = frozenset({"schema_version", "creator", "availability", "created_at"})

    @classmethod
    def legacy(cls) -> SessionAttribution:
        return cls(creator=Principal.unknown(), availability=AttributionAvailability.LEGACY_MISSING)

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": self.schema_version,
            "creator": self.creator.to_dict(),
            "availability": self.availability.value,
            "created_at": self.created_at,
        }
        return merge_extra(body, self.extra)

    @classmethod
    def from_dict(cls, raw: object) -> SessionAttribution:
        data = require_mapping(raw, SESSION_ATTRIBUTION_META_KEY)
        version = read_schema_version(data)
        return cls(
            creator=principal_or_unknown(data.get("creator")),
            availability=enum_or(
                data,
                "availability",
                AttributionAvailability,
                AttributionAvailability.LEGACY_MISSING,
            ),
            created_at=opt_float(data, "created_at"),
            schema_version=version,
            extra=split_extra(data, cls._KNOWN),
        )

    @classmethod
    def from_metadata(cls, meta: Mapping[str, Any]) -> SessionAttribution:
        """Read attribution from a session metadata line; absent is legacy."""
        raw = meta.get(SESSION_ATTRIBUTION_META_KEY)
        if raw is None:
            return cls.legacy()
        try:
            return cls.from_dict(raw)
        except ContractError:
            return cls.legacy()


def keep_creator(
    existing: SessionAttribution | None, incoming: SessionAttribution
) -> SessionAttribution:
    """Return the attribution a metadata write must store.

    Write-once: once a session records a creator, no later write replaces it,
    whoever sends the next message. Only a session with no stored attribution
    takes *incoming*. A legacy session stays legacy: attribution is never
    back-filled from the current viewer.
    """
    if existing is not None:
        return existing
    return incoming


class ActionType(str, enum.Enum):
    """A consequential shared action recorded for audit."""

    SESSION_CREATE = "session_create"
    MESSAGE = "message"
    APPROVAL = "approval"
    QUESTION_ANSWER = "question_answer"
    STEER = "steer"
    CANCEL = "cancel"
    RETRY = "retry"
    #: A lesson or preference write that replaced stored ones (newest wins).
    MEMORY_CHANGE = "memory_change"


class ActionOutcome(str, enum.Enum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class ActionRecord:
    """One audited action. Carries ids and outcome, never prompt text or tokens."""

    action_id: str
    action_type: ActionType
    session_key: str
    initiator: Principal
    at: float
    outcome: ActionOutcome
    turn_id: str = ""
    responder: Principal | None = None
    runtime_ref: str = ""
    causal_action_id: str = ""
    correlation_id: str = ""
    target: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    _KNOWN = frozenset(
        {
            "schema_version",
            "action_id",
            "action_type",
            "session_key",
            "initiator",
            "at",
            "outcome",
            "turn_id",
            "responder",
            "runtime_ref",
            "causal_action_id",
            "correlation_id",
            "target",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": SCHEMA_VERSION,
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "session_key": self.session_key,
            "initiator": self.initiator.to_dict(),
            "at": self.at,
            "outcome": self.outcome.value,
            "turn_id": self.turn_id,
            "responder": None if self.responder is None else self.responder.to_dict(),
            "runtime_ref": self.runtime_ref,
            "causal_action_id": self.causal_action_id,
            "correlation_id": self.correlation_id,
            "target": self.target,
        }
        return merge_extra(body, self.extra)


class CapabilityState(str, enum.Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


class EnforcementLevel(str, enum.Enum):
    """What a containment or limit claim actually guarantees on this host."""

    HARD = "hard"
    BEST_EFFORT = "best_effort"
    NONE = "none"


#: Membership-management destinations the UI may link to. An id from this
#: closed set, never a URL taken from config or a peer.
MEMBERSHIP_LINK_TAILSCALE = "tailscale_admin"
MEMBERSHIP_LINK_CONSOLE = "console"
MEMBERSHIP_LINKS = frozenset({MEMBERSHIP_LINK_TAILSCALE, MEMBERSHIP_LINK_CONSOLE})

#: Shared Crew wire protocol versions this build speaks.
SUPPORTED_PROTOCOLS: tuple[int, ...] = (1,)


@dataclass(frozen=True)
class Capabilities:
    """What this Crew can truthfully do, as reported to the frontend.

    Every field defaults to unavailable, so a build that wires nothing reports
    nothing it cannot back.
    """

    attribution: CapabilityState = CapabilityState.UNAVAILABLE
    presence: CapabilityState = CapabilityState.UNAVAILABLE
    queueing: CapabilityState = CapabilityState.UNAVAILABLE
    containment: CapabilityState = CapabilityState.UNAVAILABLE
    containment_level: EnforcementLevel = EnforcementLevel.NONE
    authoritative_directory: CapabilityState = CapabilityState.UNAVAILABLE
    membership_link: str = ""
    remote_actions: CapabilityState = CapabilityState.UNAVAILABLE
    protocols: tuple[int, ...] = SUPPORTED_PROTOCOLS

    def __post_init__(self) -> None:
        if self.membership_link and self.membership_link not in MEMBERSHIP_LINKS:
            raise ContractError("malformed_record", "membership_link", "not a known destination")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "attribution": self.attribution.value,
            "presence": self.presence.value,
            "queueing": self.queueing.value,
            "containment": self.containment.value,
            "containment_level": self.containment_level.value,
            "authoritative_directory": self.authoritative_directory.value,
            "membership_link": self.membership_link,
            "remote_actions": self.remote_actions.value,
            "protocols": list(self.protocols),
        }


#: ``creator`` filter values besides a principal id.
CREATOR_ALL = "all"
CREATOR_MINE = "mine"
#: Sessions with no human creator and no named trigger, shown as Unattributed.
CREATOR_UNATTRIBUTED = "unattributed"
#: The older spelling of :data:`CREATOR_UNATTRIBUTED`, still accepted on input.
CREATOR_UNKNOWN = "unknown"
SESSION_PAGE_DEFAULT = 50
SESSION_PAGE_MAX = 200
_CURSOR_RE = re.compile(r"\A[A-Za-z0-9_\-=]{1,256}\Z")


@dataclass(frozen=True)
class SessionQuery:
    """Server-side session list filter, applied before pagination.

    ``creator`` is discovery, not access control: every authorized
    collaborator may list every session; the filter only narrows the view.
    ``mine`` resolves against the authenticated viewer's principal at the
    server, never against a principal id the client sends as "me".
    """

    creator: str = CREATOR_ALL
    search: str = ""
    cursor: str = ""
    limit: int = SESSION_PAGE_DEFAULT

    @classmethod
    def from_query(cls, params: Mapping[str, str]) -> SessionQuery:
        creator = (params.get("creator") or CREATOR_ALL).strip()
        if creator == CREATOR_UNKNOWN:
            creator = CREATOR_UNATTRIBUTED
        if creator not in (
            CREATOR_ALL,
            CREATOR_MINE,
            CREATOR_UNATTRIBUTED,
        ) and not _PRINCIPAL_ID_RE.match(creator):
            raise ContractError("invalid_filter", "creator", "not a creator filter")
        cursor = params.get("cursor") or ""
        if cursor and not _CURSOR_RE.match(cursor):
            raise ContractError("invalid_cursor", "cursor", "malformed cursor")
        limit_raw = params.get("limit") or str(SESSION_PAGE_DEFAULT)
        try:
            limit = int(limit_raw)
        except ValueError:
            raise ContractError("invalid_filter", "limit", "expected an integer") from None
        limit = max(1, min(limit, SESSION_PAGE_MAX))
        return cls(
            creator=creator, search=(params.get("q") or "")[:256], cursor=cursor, limit=limit
        )

    def matches(self, creator: Principal, viewer: Principal, *, triggered: bool = False) -> bool:
        """Whether a session with *creator* passes the creator filter for *viewer*.

        *triggered* is a session a schedule, a webhook or the Crew started: it
        names its trigger, so it is not Unattributed.
        """
        if self.creator == CREATOR_ALL:
            return True
        if self.creator == CREATOR_UNATTRIBUTED:
            return creator.is_unknown and not triggered
        if self.creator == CREATOR_MINE:
            return not viewer.is_unknown and creator.id == viewer.id
        return creator.id == self.creator


@dataclass(frozen=True)
class SessionPage:
    """One page of the filtered session list.

    ``total_matching`` counts every session that passes the filter, across all
    pages. ``hidden_by_filter`` counts sessions the filter excluded, so the UI
    can say how many exist beyond the current filter without loading them.
    """

    items: tuple[Mapping[str, Any], ...]
    total_matching: int
    hidden_by_filter: int
    next_cursor: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "items": [dict(i) for i in self.items],
            "total_matching": self.total_matching,
            "hidden_by_filter": self.hidden_by_filter,
            "next_cursor": self.next_cursor,
        }
