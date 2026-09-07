"""Generic crew-to-crew migration protocol (slice 1, no unit-kind knowledge).

The design (``.kiro/specs/crew-work-migration/design.md``) states the central
invariant plainly:

    Every failure mode short of a durable ack leaves the SOURCE owning the work.

This module encodes exactly that. ``MigrationCoordinator`` drives the five
ordered steps and owns the failure semantics; anything unit-type-specific lives
behind ``MigrationUnitAdapter`` and never leaks into the coordinator.

Ownership is a *release-after-ack* protocol, not a distributed lock: the source
is authoritative until it durably records that the target holds the unit. That
yields at-most-one executor at every instant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class MidRunError(RuntimeError):
    """Raised by an adapter's quiesce() when the unit is mid-execution.

    Requirements 4.9 / 6.6: a unit cannot be migrated while a run is in
    flight — the coordinator surfaces this as a 'refused' outcome with a
    'mid-run' reason, and nothing is quiesced-then-lost.
    """


# --------------------------------------------------------------------- data model


@dataclass(frozen=True)
class CrewRef:
    """A reference to a crew endpoint. Identity only — no credentials."""

    crew_id: str
    label: str = ""


@dataclass(frozen=True)
class HostRequirement:
    """A NAMED requirement the target must satisfy — never a transferred value.

    Kinds mirror the design: credential, mcp_server, agent, project_checkout,
    script_path, command_policy, git_repo.
    """

    kind: str
    identity: str
    severity: str = "advisory"  # "blocking" | "advisory"


@dataclass(frozen=True)
class Finding:
    """A single observation about a planned move.

    Used by ``scan_for_secrets``: a payload carrying a credential is a blocking
    finding, so a plan cannot quietly describe shipping one.
    """

    kind: str
    detail: str
    severity: str = "advisory"  # "blocking" | "advisory"
    detail_key: str = ""  # optional: the reference this finding concerns


@dataclass(frozen=True)
class MigrationBundle:
    bundle_kind: str  # "cron" | "session" | "taskrun"
    bundle_version: int
    handoff_id: str  # idempotency key
    created_ts: float
    source_crew: CrewRef
    payload: dict
    requirements: list[HostRequirement] = field(default_factory=list)


# ------------------------------------------------------ allow-list serialization


def allow_list_serialize(source: dict, allowed: tuple[str, ...]) -> dict:
    """Return only the explicitly-allowed keys present in ``source``.

    Requirement 3.4: a field not named is DROPPED. An allowed field absent from
    the source is simply omitted (not emitted as ``None``), so the output never
    invents state the source did not have.
    """
    return {k: source[k] for k in allowed if k in source}


# ------------------------------------------------------ allow-list serialization

# ------------------------------------------------------------------ adapter seam


@runtime_checkable
class MigrationUnitAdapter(Protocol):
    """One per unit kind. Owns what the unit's durable state is and how to stop
    it safely. No unit-type knowledge leaks past this seam into the coordinator.
    """

    bundle_kind: str
    bundle_version: int

    async def describe(self, unit_id: str) -> dict: ...
    async def requirements(self, unit_id: str) -> list[HostRequirement]: ...
    async def serialize(self, unit_id: str) -> dict: ...
