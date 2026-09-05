"""Slice 5 (circle 1) — reversibility (issue #7577, Task 5.1 / Req 7.4).

A migrated unit must be migratable AGAIN from its new owner — including back
to the crew it started on. This exercises the slice-1 coordinator with fake
adapters/receivers standing in for two crews, asserting a round trip
(A -> B -> A) preserves the single-owner invariant at every hop.

Side-effect discipline: in-memory fakes only.
"""

from __future__ import annotations

import pytest

from kiro_crew.migration import protocol as P


class FakeAdapter:
    bundle_kind = "fake"
    bundle_version = 1

    def __init__(self, unit_id="u1", executable=True):
        self.unit_id = unit_id
        self.executable = executable
        self.tombstoned = False

    async def describe(self, unit_id):
        return {}

    async def requirements(self, unit_id):
        return []

    async def quiesce(self, unit_id):
        self.executable = False
        return P.QuiesceToken(unit_id=unit_id, token="t")

    async def unquiesce(self, unit_id, token):
        self.executable = True

    async def serialize(self, unit_id):
        return {"unit_id": unit_id, "name": "fake"}

    async def materialize(self, payload):
        return "mat-" + payload["unit_id"]

    async def tombstone(self, unit_id, target, remote_id):
        self.tombstoned = True
        self.executable = False


class FakeReceiver(P.MigrationReceiver):
    def __init__(self, adapter_after_accept: FakeAdapter):
        self._accepted = {}
        self._adapter = adapter_after_accept  # the target's live adapter

    async def preflight(self, bundle):
        return P.PreflightReport(findings=[])

    async def accept(self, bundle):
        if bundle.handoff_id in self._accepted:
            return P.AcceptAck(unit_id=self._accepted[bundle.handoff_id])
        uid = "remote-" + bundle.payload["unit_id"]
        self._accepted[bundle.handoff_id] = uid
        self._adapter.unit_id = uid
        self._adapter.executable = True  # target can now run it
        return P.AcceptAck(unit_id=uid)

    async def lookup(self, handoff_id):
        uid = self._accepted.get(handoff_id)
        return P.AcceptAck(unit_id=uid) if uid else None


def _coord(adapter, receiver, src, dst):
    return P.MigrationCoordinator(
        adapter=adapter,
        receiver=receiver,
        source_crew=P.CrewRef(crew_id=src),
        target_crew=P.CrewRef(crew_id=dst),
    )


@pytest.mark.asyncio
async def test_unit_can_be_migrated_back_to_its_origin():
    crew_a = FakeAdapter(unit_id="u1")  # starts owning u1
    crew_b = FakeAdapter(unit_id="u1", executable=False)

    # A -> B
    res1 = await _coord(crew_a, FakeReceiver(crew_b), "A", "B").migrate("u1")
    assert res1.outcome == "migrated"
    assert crew_a.executable is False and crew_a.tombstoned is True
    assert crew_b.executable is True  # B now owns

    # B -> A (reverse): the unit is migratable again from its new owner
    res2 = await _coord(crew_b, FakeReceiver(crew_a), "B", "A").migrate(crew_b.unit_id)
    assert res2.outcome == "migrated"
    assert crew_b.executable is False and crew_b.tombstoned is True
    assert crew_a.executable is True  # A owns again — full round trip


@pytest.mark.asyncio
async def test_single_owner_holds_at_every_hop_of_the_round_trip():
    crew_a = FakeAdapter(unit_id="u1")
    crew_b = FakeAdapter(unit_id="u1", executable=False)

    # before: exactly one owner (A)
    assert crew_a.executable is True and crew_b.executable is False

    await _coord(crew_a, FakeReceiver(crew_b), "A", "B").migrate("u1")
    # after hop 1: exactly one owner (B)
    assert (crew_a.executable, crew_b.executable) == (False, True)
    # ...and A's stand-down is RELEASED, not merely quiesced. Without this the
    # assertion above cannot tell a completed handoff from the crash window
    # between ack and tombstone, where the source is stopped but still owns the
    # work and has no record of where it went. A mutation sweep that removed the
    # coordinator's tombstone call survived until this line existed.
    assert crew_a.tombstoned is True

    await _coord(crew_b, FakeReceiver(crew_a), "B", "A").migrate(crew_b.unit_id)
    # after hop 2: exactly one owner (A) again, and B has released in turn
    assert (crew_a.executable, crew_b.executable) == (True, False)
    assert crew_b.tombstoned is True
