"""Crew-to-crew work migration (issue #7577).

Slice 1 ships the generic handoff *protocol* only — the data model, an
allow-list serialization helper, a credential scan, and the five-step
``MigrationCoordinator`` (preflight -> quiesce -> transmit -> await durable
ack -> tombstone + release) with its single-owner invariant. Unit-kind
adapters (cron, session, task-runner) arrive in later slices behind the
``MigrationUnitAdapter`` seam defined here.
"""

from __future__ import annotations

from kiro_crew.migration.protocol import (
    AcceptAck,
    CrewRef,
    Finding,
    HostRequirement,
    MidRunError,
    MigrationBundle,
    MigrationCoordinator,
    MigrationReceiver,
    MigrationResult,
    MigrationUnitAdapter,
    PreflightReport,
    QuiesceToken,
    ReconcileResult,
    Tombstone,
    allow_list_serialize,
    scan_for_secrets,
)

__all__ = [
    "AcceptAck",
    "CrewRef",
    "Finding",
    "HostRequirement",
    "MidRunError",
    "MigrationBundle",
    "MigrationCoordinator",
    "MigrationReceiver",
    "MigrationResult",
    "MigrationUnitAdapter",
    "PreflightReport",
    "QuiesceToken",
    "ReconcileResult",
    "Tombstone",
    "allow_list_serialize",
    "scan_for_secrets",
]
