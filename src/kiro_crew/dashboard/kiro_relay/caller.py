"""The relayed caller: who a relay-delivered request is, and whether that is the owner.

Pure values. Nothing here reads a request, a config file or the network, so the
ingress middleware, token auth and the app proxy all agree on one shape.

Provenance and transport use the shared-crew attribution contract's vocabulary
(:mod:`kiro_crew.shared_crew.contracts`). :class:`Transport` is that contract's own
enum, whose ``remote`` member names a relayed request. The contract's
``Provenance`` has no relay member yet, so :class:`Provenance` here holds the one
value it needs, ``kiro_relay``; once the contract gains that member this enum becomes
an import of it, which is the whole switch: every reader here compares members,
never raw strings.
"""

from __future__ import annotations

import enum
import hmac
import re
from dataclasses import dataclass

# The one definition of the relay-header prefix, shared with the client-sent strip
# and the app-backend proxy. Only the Crew's own relay client may write a header
# under it, and only on a request it hands over; the relay drops any a client
# sends, and the client-sent strip drops any that arrive any other way.
from kiro_crew.dashboard.relay_headers import RELAY_HEADER_PREFIX
from kiro_crew.shared_crew.contracts import Transport

#: The verified person's stable Kiro user ID, stamped from the relay's own message.
RELAY_USER_HEADER = RELAY_HEADER_PREFIX + "user"
#: The per-process handover credential that marks a request as the relay client's.
RELAY_HANDOVER_HEADER = RELAY_HEADER_PREFIX + "handover"

#: aiohttp request key under which the ingress middleware publishes the caller.
RELAY_CALLER_KEY = "kiro_relay_caller"
#: ``request.remote`` of a relayed request. Not an address, so no loopback-trusting
#: check (``origin.is_loopback``, the no-Origin CSRF branch, the internal-secret
#: branch, ``is_direct_local_request``) can read a relayed request as local.
RELAY_REMOTE = "kiro-relay"
#: Prefix of a teammate's dashboard subject. Never equal to an owner subject.
TEAMMATE_SUBJECT_PREFIX = "relay:"
#: The identity provider a relayed caller's ID belongs to.
KIRO_PROVIDER = "kiro"

#: A Kiro user ID as this Crew accepts one: visible ASCII from a closed set, no
#: whitespace or separators a header or log line could be split on, at most 256
#: characters (the app proxy's owner id bound). The relay protocol contract owns
#: the authoritative grammar; this is the Crew's own fail-closed floor under it.
_KIRO_USER_ID_RE = re.compile(r"\A[A-Za-z0-9._:@+=/-]{1,256}\Z")


class Provenance(str, enum.Enum):
    """How the server established a principal: the relay's member of the contract's set."""

    #: Named by the Kiro relay in its own message, stamped by this Crew's relay client.
    KIRO_RELAY = "kiro_relay"


class CrewKind(str, enum.Enum):
    """Whose Crew this is on the relay: one person's, or a team's."""

    PERSONAL = "personal"
    TEAM = "team"


class RelayCallerKind(str, enum.Enum):
    """What a relayed caller is to this Crew."""

    OWNER = "owner"
    #: A person with a grant to a team Crew. Never the owner, on any surface.
    TEAMMATE = "teammate"


def is_kiro_user_id(value: object) -> bool:
    """Whether *value* is a Kiro user ID this Crew will compare or record."""
    return isinstance(value, str) and _KIRO_USER_ID_RE.match(value) is not None


def published_relay_caller(request: object) -> RelayCaller | None:
    """The caller the ingress middleware published on *request*, if any.

    Accepts any request-shaped object and answers ``None`` for one that is not a
    mapping or holds anything but a :class:`RelayCaller` under the key, so a test
    double or a request the middleware never saw can never read as relayed.
    """
    getter = getattr(request, "get", None)
    if not callable(getter):
        return None
    try:
        caller = getter(RELAY_CALLER_KEY)
    except Exception:
        return None
    return caller if isinstance(caller, RelayCaller) else None


#: Set on a dashboard WebSocket a relayed teammate opened. The broadcast gate reads
#: it to withhold app frames, since the remote limits keep a teammate off every app.
RELAY_TEAMMATE_WS_FLAG = "_kiro_relay_teammate"


def is_relay_teammate(request: object) -> bool:
    """Whether *request* is relayed and its caller is a teammate, not the owner."""
    caller = published_relay_caller(request)
    return caller is not None and not caller.is_owner


@dataclass(frozen=True)
class RelayCaller:
    """The person a relay-delivered request is attributed to."""

    kiro_user_id: str
    kind: RelayCallerKind
    provider: str = KIRO_PROVIDER
    provenance: Provenance = Provenance.KIRO_RELAY
    transport: Transport = Transport.REMOTE

    def __post_init__(self) -> None:
        if not is_kiro_user_id(self.kiro_user_id):
            raise ValueError("not a Kiro user ID")

    @property
    def is_owner(self) -> bool:
        return self.kind is RelayCallerKind.OWNER

    @property
    def audit_label(self) -> str:
        """The name an audit row files this caller under."""
        return f"{TEAMMATE_SUBJECT_PREFIX}{self.kiro_user_id}"

    def dashboard_subject(self, owner_subject: str) -> str:
        """The ``request["user"]`` token auth publishes for this caller.

        The owner gets the owner's own subject, so every owner gate already in the
        dashboard decides for them exactly as it does for the owner at the machine.
        A teammate gets a ``relay:`` subject that no owner gate matches.
        """
        if self.is_owner:
            return owner_subject
        return self.audit_label


@dataclass(frozen=True)
class HostBinding:
    """What the relay client knows about the host connection it holds.

    ``owner_user_id`` is the Kiro user ID of the sign-in the personal Crew renews
    its lease with, which is the host token's subject. Each sign-in provider is a
    separate Kiro user, so a person reaches a personal Crew only from the same
    provider and the same user as the Crew's computer: the comparison is exact, on
    the stable ID, with no case folding and no email or display name involved.
    """

    crew_kind: CrewKind
    owner_user_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.crew_kind, CrewKind):
            raise ValueError("crew_kind must be a CrewKind")
        if self.crew_kind is CrewKind.PERSONAL and not is_kiro_user_id(self.owner_user_id):
            raise ValueError("a personal Crew needs its owner's Kiro user ID")
        if self.crew_kind is CrewKind.TEAM and self.owner_user_id:
            raise ValueError("a team Crew has no relayed owner")

    def caller_for(self, kiro_user_id: str) -> RelayCaller | None:
        """The caller the relay named, or ``None`` when this Crew does not honor them.

        A personal Crew honors the header only for its signed-in owner. A team Crew
        takes the person as the relay names them, because Kiro minted their token
        only after checking their grant, and makes them a teammate.
        """
        if not is_kiro_user_id(kiro_user_id):
            return None
        if self.crew_kind is CrewKind.TEAM:
            return RelayCaller(kiro_user_id=kiro_user_id, kind=RelayCallerKind.TEAMMATE)
        # Bytes, so a non-ASCII value is an ordinary mismatch, never a TypeError.
        if hmac.compare_digest(
            kiro_user_id.encode("utf-8", "surrogatepass"),
            self.owner_user_id.encode("utf-8", "surrogatepass"),
        ):
            return RelayCaller(kiro_user_id=kiro_user_id, kind=RelayCallerKind.OWNER)
        return None
