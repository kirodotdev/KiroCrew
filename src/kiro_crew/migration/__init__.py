"""Crew-to-crew work migration — plan surface.

This slice ships the part a user can actually reach: given a live cron job,
session or task run, describe what moving it to another crew WOULD involve —
which fields ship, which are dropped, and what the target must satisfy first.
Nothing here transfers ownership.

The transfer itself (quiesce -> transmit -> await durable ack -> tombstone +
release, and the coordinator that orders those steps so any failure before the
durable ack leaves the source still owning executable work) lands in the change
that wires it to the crew tunnel. It is deliberately NOT here: shipped without a
caller it would be a library no user can reach, and the double-execution harm
the issue names is removed only by the real transfer, never by a description of
one.

What the plan half needs from the protocol is the data model, the allow-list
serialization that decides which fields would travel, and the credential scan.
The ``MigrationUnitAdapter`` seam stays so one plan implementation serves every
unit kind and every surface.
"""

from __future__ import annotations

from kiro_crew.migration.protocol import (
    CrewRef,
    Finding,
    HostRequirement,
    MidRunError,
    MigrationBundle,
    MigrationUnitAdapter,
    allow_list_serialize,
)

__all__ = [
    "CrewRef",
    "Finding",
    "HostRequirement",
    "MidRunError",
    "MigrationBundle",
    "MigrationUnitAdapter",
    "allow_list_serialize",
]
