"""Migration protocol — plan-side surface.

Covers what this slice actually ships: the data model a plan is built from, the
allow-list serialization that decides which fields would travel, and the
credential scan that refuses a payload carrying a secret.

The five-step handoff state machine and its single-owner invariant are NOT here.
They were removed from this slice with the coordinator itself: shipped without a
production caller they were a library no user could reach, so they return in the
change that wires transmit to the crew tunnel, together with the tests that pin
the ordering, the crash window and the reconciliation contract.

Side-effect discipline (writing-tests skill): everything is in-memory. No writes
to the real data home, no tunnel, no threads, no cron.
"""

from __future__ import annotations

import dataclasses

from kiro_crew.migration import protocol as P

# ---------------------------------------------------------------- 1.1 data model


def test_dataclasses_exist_and_are_frozen_dataclasses():
    for name in (
        "MigrationBundle",
        "HostRequirement",
        "Finding",
        "CrewRef",
    ):
        cls = getattr(P, name)
        assert dataclasses.is_dataclass(cls), f"{name} must be a dataclass"


def test_the_transfer_half_is_absent_from_this_slice():
    """Pin the subtraction so the un-wired half cannot drift back unnoticed.

    The coordinator, the receiver base and the tombstone/journal types were
    removed because nothing in production called them, which made the badge and
    the redirect line unreachable and the "single owner" claim untestable end to
    end. Re-adding any of these names without a production caller reintroduces
    exactly that, so this test fails until the wiring change brings back both
    halves together.
    """
    for absent in (
        "MigrationCoordinator",
        "MigrationReceiver",
        "MigrationResult",
        "ReconcileResult",
        "Tombstone",
        "AcceptAck",
        "PreflightReport",
    ):
        assert not hasattr(P, absent), (
            f"{absent} is back in the protocol without the transmit wiring; "
            "land it in the PR that gives it a production caller"
        )


def test_allow_list_serialize_drops_unnamed_fields():
    raw = {"keep": 1, "drop": 2}
    assert P.allow_list_serialize(raw, allowed=("keep",)) == {"keep": 1}


def test_allow_list_serialize_missing_allowed_field_is_omitted_not_none():
    assert P.allow_list_serialize({}, allowed=("absent",)) == {}
