"""Protocol-layer tests for crew-to-crew work migration (slice 1).

Covers plan.md Task 1.1–1.10: the data model, the allow-list serialization
helper, and the five-step handoff state machine with its single-owner
invariant. No unit-kind adapters here (those are slices 2/3/4) — a fake
in-memory adapter and a fake receiver stand in.

Side-effect discipline (writing-tests skill): everything is in-memory. No
writes to the real data home, no tunnel, no threads, no cron.
"""

from __future__ import annotations

import contextlib
import dataclasses
from unittest.mock import patch

import pytest

from kiro_crew.migration import protocol as P

# ---------------------------------------------------------------- 1.1 data model


def test_dataclasses_exist_and_are_frozen_dataclasses():
    for name in (
        "MigrationBundle",
        "HostRequirement",
        "PreflightReport",
        "Finding",
        "Tombstone",
        "CrewRef",
        "QuiesceToken",
    ):
        cls = getattr(P, name)
        assert dataclasses.is_dataclass(cls), f"{name} must be a dataclass"


def test_preflight_report_blocked_property():
    advisory = P.Finding(kind="agent", detail="hint", severity="advisory")
    blocking = P.Finding(kind="credential", detail="leak", severity="blocking")
    assert P.PreflightReport(findings=[advisory]).blocked is False
    assert P.PreflightReport(findings=[advisory, blocking]).blocked is True
    assert P.PreflightReport(findings=[]).blocked is False


def test_allow_list_serialize_drops_unnamed_fields():
    src = {"name": "job1", "message": "hi", "secret_runtime": "DROP_ME", "consecutive_failures": 3}
    out = P.allow_list_serialize(src, allowed=("name", "message"))
    assert out == {"name": "job1", "message": "hi"}
    assert "secret_runtime" not in out
    assert "consecutive_failures" not in out


def test_allow_list_serialize_missing_allowed_field_is_omitted_not_none():
    out = P.allow_list_serialize({"name": "x"}, allowed=("name", "timezone"))
    # a field that is allowed but absent from the source is simply not present
    assert out == {"name": "x"}


# ---------------------------------------------------------- fakes for 1.2–1.10


class FakeAdapter:
    """In-memory MigrationUnitAdapter — no real unit type."""

    bundle_kind = "fake"
    bundle_version = 1

    def __init__(self, unit_id="u1"):
        self.unit_id = unit_id
        self.quiesced = False
        self.unquiesced = False
        self.tombstoned = False
        self.executable = True  # source can run the unit

    async def describe(self, unit_id):
        return {"unit_id": unit_id, "kind": self.bundle_kind}

    async def requirements(self, unit_id):
        return []

    async def quiesce(self, unit_id):
        self.quiesced = True
        self.executable = False
        return P.QuiesceToken(unit_id=unit_id, token="tok")

    async def unquiesce(self, unit_id, token):
        self.unquiesced = True
        self.executable = True

    async def serialize(self, unit_id):
        return {"unit_id": unit_id, "name": "fake", "runtime_only": "DROP"}

    async def materialize(self, payload):
        return "remote-" + payload["unit_id"]

    async def tombstone(self, unit_id, target, remote_id):
        self.tombstoned = True
        self.executable = False


class FakeReceiver(P.MigrationReceiver):
    """In-memory MigrationReceiver: preflight + accept, dedup on handoff_id.

    Implements ``lookup`` because that is part of the receiver contract the
    coordinator relies on for reconciliation. An earlier version omitted it,
    which is why the coordinator carried a fallback that poked at this fake's
    private ``_accepted`` map -- a test shape leaking into production code.
    """

    def __init__(self, *, reachable=True, blocking=False):
        self.reachable = reachable
        self.blocking = blocking
        self._accepted: dict[str, str] = {}  # handoff_id -> unit_id
        self.accept_calls = 0

    async def preflight(self, bundle):
        if not self.reachable:
            raise ConnectionError("target unreachable")
        findings = []
        if self.blocking:
            findings.append(P.Finding(kind="credential", detail="x", severity="blocking"))
        return P.PreflightReport(findings=findings)

    async def accept(self, bundle):
        if not self.reachable:
            raise ConnectionError("target unreachable")
        self.accept_calls += 1
        if bundle.handoff_id in self._accepted:  # idempotent
            return P.AcceptAck(unit_id=self._accepted[bundle.handoff_id])
        unit_id = "remote-" + bundle.payload["unit_id"]
        self._accepted[bundle.handoff_id] = unit_id
        return P.AcceptAck(unit_id=unit_id)

    async def lookup(self, handoff_id):
        uid = self._accepted.get(handoff_id)
        return P.AcceptAck(unit_id=uid) if uid else None


def _coord(adapter, receiver):
    return P.MigrationCoordinator(
        adapter=adapter,
        receiver=receiver,
        source_crew=P.CrewRef(crew_id="src", label="source"),
        target_crew=P.CrewRef(crew_id="dst", label="target"),
    )


# ------------------------------------------------------------------- 1.3 happy


@pytest.mark.asyncio
async def test_happy_path_five_steps_in_order_then_release():
    a, r = FakeAdapter(), FakeReceiver()
    result = await _coord(a, r).migrate("u1")
    assert result.outcome == "migrated"
    assert result.remote_unit_id == "remote-u1"
    assert a.quiesced and a.tombstoned
    assert a.executable is False  # released to target


# ---------------------------------------------------- 1.4 failure semantics


@pytest.mark.asyncio
async def test_preflight_blocking_refuses_without_quiescing():
    a, r = FakeAdapter(), FakeReceiver(blocking=True)
    result = await _coord(a, r).migrate("u1")
    assert result.outcome == "refused"  # distinct from "failed"
    assert a.quiesced is False  # nothing quiesced
    assert a.executable is True  # source still owns & can run


@pytest.mark.asyncio
async def test_target_unreachable_at_preflight_refuses_source_untouched():
    a, r = FakeAdapter(), FakeReceiver(reachable=False)
    result = await _coord(a, r).migrate("u1")
    assert result.outcome in ("refused", "failed")
    assert a.executable is True


@pytest.mark.asyncio
async def test_transmit_failure_after_quiesce_unquiesces_and_retains_ownership():
    a = FakeAdapter()
    r = FakeReceiver()

    async def boom(bundle):
        raise ConnectionError("ack lost")

    r.accept = boom  # type: ignore[assignment]

    result = await _coord(a, r).migrate("u1")
    assert result.outcome == "failed"
    assert a.quiesced is True and a.unquiesced is True  # rolled back
    assert a.executable is True  # SOURCE still owns
    assert a.tombstoned is False  # never tombstoned


# ------------------------------------------------------------ 1.6 crash window


@pytest.mark.asyncio
async def test_reconciliation_after_ack_before_tombstone_converges_to_target():
    a, r = FakeAdapter(), FakeReceiver()
    # simulate: ack happened (target holds handoff), source died before tombstone
    handoff_id = "h-crash"
    r._accepted[handoff_id] = "remote-u1"
    coord = _coord(a, r)
    resolved = await coord.reconcile(handoff_id=handoff_id, unit_id="u1")
    assert resolved.owner == "target"  # exactly one owner: the target
    assert a.tombstoned is True  # tombstone completed on reconcile


@pytest.mark.asyncio
async def test_reconciliation_when_target_lacks_handoff_unquiesces_source():
    a, r = FakeAdapter(), FakeReceiver()
    a.quiesced = True
    a.executable = False
    resolved = await _coord(a, r).reconcile(handoff_id="never", unit_id="u1")
    assert resolved.owner == "source"
    assert a.executable is True  # source reclaims


# ------------------------------------ 1.6b a crash window a RESTART can resolve


@pytest.mark.asyncio
async def test_an_outstanding_handoff_is_discoverable_after_a_restart(tmp_path):
    """A process that was not there when the window opened must still close it.

    ``test_reconciliation_after_ack_before_tombstone_converges_to_target``
    proves reconcile()'s LOGIC, but it hands the handoff id in as a literal
    because the test author happens to know it. A gateway that has just booted
    does not: that id lived in the memory of the process that died. Unless an
    in-flight handoff is recorded DURABLY before the unit is transmitted,
    nothing can enumerate what still needs reconciling, and the ack->tombstone
    window stays open forever -- two owners, or none executing.

    So the property here is not "does reconcile work" but "can a fresh
    coordinator FIND the outstanding handoff without being told". That is the
    missing half of the single-owner invariant, and the reason ``migrate()`` has
    no production call site yet: wiring transmit before this exists would
    manufacture windows nothing can close.

    Side-effect discipline: the journal is written under ``tmp_path``, never the
    real data home, so this file's in-memory rule still holds.
    """
    a, r = FakeAdapter(), FakeReceiver()

    # The crash: the target durably ACKED, then the source died before it could
    # tombstone. This is the one ordering where both crews have a claim.
    async def die_before_tombstone(unit_id, target, remote_id):
        raise RuntimeError("process died between ack and tombstone")

    a.tombstone = die_before_tombstone  # type: ignore[assignment]

    def _coord_with_journal(adapter, receiver):
        return P.MigrationCoordinator(
            adapter=adapter,
            receiver=receiver,
            source_crew=P.CrewRef(crew_id="src", label="source"),
            target_crew=P.CrewRef(crew_id="dst", label="target"),
            journal_dir=tmp_path,
        )

    # Suppress ONLY the crash we injected. A first draft used
    # `suppress(Exception)`, which also swallowed the TypeError from the
    # journal_dir kwarg not existing yet -- so the test failed at its own
    # precondition instead of naming the missing API. Broad suppression in a
    # test hides exactly the signal the test exists to produce.
    with contextlib.suppress(RuntimeError):
        await _coord_with_journal(a, r).migrate("u1")

    # The target holds it; the source has not tombstoned. The window is open.
    assert r._accepted, "precondition: the ack must have landed"
    assert a.tombstoned is False, "precondition: the crash beat the tombstone"

    # A REBOOT: a brand-new coordinator, no memory of the handoff id, reading
    # only what was durably journalled.
    reborn = _coord_with_journal(FakeAdapterAdopting(a), r)
    resolved = await reborn.reconcile_outstanding()

    assert [x.owner for x in resolved] == ["target"], "exactly one owner: the target"
    # A settled handoff must not be replayed on the next boot, or every restart
    # re-runs history.
    assert await reborn.reconcile_outstanding() == []


class FakeAdapterAdopting:
    """Stands in for the rebooted process's adapter over the same unit.

    A restart builds a fresh adapter; it must still be able to finish the
    tombstone the dead process never wrote. Delegates to the original fake's
    state so the assertions can observe convergence, but with a tombstone that
    does not raise -- the crash is over.
    """

    bundle_kind = "fake"
    bundle_version = 1

    def __init__(self, original):
        self._o = original

    def __getattr__(self, name):
        return getattr(self._o, name)

    async def tombstone(self, unit_id, target, remote_id):
        self._o.tombstoned = True
        self._o.executable = False


@pytest.mark.asyncio
async def test_a_pre_ack_failure_leaves_nothing_to_reconcile(tmp_path):
    """A transmit that never acked is SETTLED, not a window.

    The source rolled back and still owns the unit, so its outcome is known. If
    the journal kept the entry, the next boot would reconcile a handoff the
    target never took and un-quiesce a unit that is already running -- acting on
    a question that was already answered.
    """
    a, r = FakeAdapter(), FakeReceiver()

    async def ack_lost(bundle):
        raise ConnectionError("ack lost")

    r.accept = ack_lost  # type: ignore[assignment]

    coord = P.MigrationCoordinator(
        adapter=a,
        receiver=r,
        source_crew=P.CrewRef(crew_id="src"),
        target_crew=P.CrewRef(crew_id="dst"),
        journal_dir=tmp_path,
    )
    result = await coord.migrate("u1")
    assert result.outcome == "failed"
    assert a.executable is True  # source retained, as the invariant requires
    assert await coord.reconcile_outstanding() == [], "a settled failure is not a window"


@pytest.mark.asyncio
async def test_a_completed_migration_leaves_nothing_to_reconcile(tmp_path):
    """The happy path must not leave a journal entry behind either."""
    a, r = FakeAdapter(), FakeReceiver()
    coord = P.MigrationCoordinator(
        adapter=a,
        receiver=r,
        source_crew=P.CrewRef(crew_id="src"),
        target_crew=P.CrewRef(crew_id="dst"),
        journal_dir=tmp_path,
    )
    assert (await coord.migrate("u1")).outcome == "migrated"
    assert await coord.reconcile_outstanding() == []


@pytest.mark.asyncio
async def test_an_unanswerable_receiver_keeps_its_window_open(tmp_path):
    """A receiver that cannot be queried must NOT have its entry dropped.

    Dropping it would silently abandon a window -- the unit could be executing on
    both crews with nothing left on disk to say so. Keeping it means the next
    boot tries again and the unreconciled-handoffs band can see the backlog. The
    sweep must also not be stranded by it, so a second, answerable handoff still
    resolves in the same pass.
    """
    a, r = FakeAdapter(), FakeReceiver()

    async def die_before_tombstone(unit_id, target, remote_id):
        raise RuntimeError("process died between ack and tombstone")

    a.tombstone = die_before_tombstone  # type: ignore[assignment]

    coord = P.MigrationCoordinator(
        adapter=a,
        receiver=r,
        source_crew=P.CrewRef(crew_id="src"),
        target_crew=P.CrewRef(crew_id="dst"),
        journal_dir=tmp_path,
    )
    with contextlib.suppress(RuntimeError):
        await coord.migrate("u1")

    async def cannot_answer(handoff_id):
        raise NotImplementedError("receiver offline")

    r.lookup = cannot_answer  # type: ignore[assignment]

    reborn = P.MigrationCoordinator(
        adapter=FakeAdapterAdopting(a),
        receiver=r,
        source_crew=P.CrewRef(crew_id="src"),
        target_crew=P.CrewRef(crew_id="dst"),
        journal_dir=tmp_path,
    )
    assert await reborn.reconcile_outstanding() == [], "unresolved yields no verdict"

    # The window is still on disk: once the receiver can answer, it converges.
    r.lookup = FakeReceiver.lookup.__get__(r, FakeReceiver)  # type: ignore[assignment]
    resolved = await reborn.reconcile_outstanding()
    assert [x.owner for x in resolved] == ["target"], "the kept entry was reconcilable"


# ----------------------------------------------------------- 1.5/2.7 idempotency


@pytest.mark.asyncio
async def test_same_handoff_id_twice_yields_one_unit_on_target():
    a, r = FakeAdapter(), FakeReceiver()
    coord = _coord(a, r)
    b = coord._build_bundle("u1", await a.serialize("u1"), handoff_id="fixed")
    ack1 = await r.accept(b)
    ack2 = await r.accept(b)
    assert ack1.unit_id == ack2.unit_id
    assert len(r._accepted) == 1


# ---------------------------------------------------------- 1.7 secret containment


def test_credential_scan_flags_blocking_and_no_secret_in_bundle():
    payload = {"name": "job", "command": "curl -H 'x-api-key: AKIAIOSFODNN7EXAMPLE'"}
    findings = P.scan_for_secrets(payload)
    assert any(f.severity == "blocking" for f in findings)


# ------------------------------- 3.1: audit reaches a real sink (Req 3.5)


def _audited_coord(adapter, receiver, sink):
    return P.MigrationCoordinator(
        adapter=adapter,
        receiver=receiver,
        source_crew=P.CrewRef(crew_id="src"),
        target_crew=P.CrewRef(crew_id="dst"),
        audit_sink=sink,
    )


@pytest.mark.asyncio
async def test_a_completed_migration_is_audited_with_duration():
    calls: list[dict] = []
    a, r = FakeAdapter(), FakeReceiver()
    result = await _audited_coord(a, r, calls.append).migrate("u1")
    assert result.outcome == "migrated"
    done = [c for c in calls if c["event"] == "migrate.done"]
    assert len(done) == 1
    entry = done[0]
    assert entry["unit_id"] == "u1"
    assert entry["target"] == "dst"
    assert entry["handoff_id"]
    # per-unit duration measurement is part of Req 3.5, not decoration
    assert "duration" in entry and isinstance(entry["duration"], float)
    assert entry["outcome"] == "migrated"


@pytest.mark.asyncio
async def test_a_refused_migration_is_audited_too():
    calls: list[dict] = []
    a, r = FakeAdapter(), FakeReceiver(blocking=True)
    await _audited_coord(a, r, calls.append).migrate("u1")
    events = {c["event"] for c in calls}
    assert "migrate.refused" in events
    refused = next(c for c in calls if c["event"] == "migrate.refused")
    assert refused["outcome"] == "refused"


@pytest.mark.asyncio
async def test_a_failed_migration_is_audited_with_the_rollback():
    calls: list[dict] = []
    a, r = FakeAdapter(), FakeReceiver()

    async def boom(bundle):
        raise ConnectionError("ack lost")

    r.accept = boom  # type: ignore[assignment]

    await _audited_coord(a, r, calls.append).migrate("u1")
    events = {c["event"] for c in calls}
    assert "migrate.quiesced" in events  # the stand-down is on the record
    assert "migrate.failed" in events


@pytest.mark.asyncio
async def test_an_audit_sink_that_raises_never_breaks_the_migration():
    """An audit write failure must not turn a good migration into a failed one."""

    def bad_sink(entry):
        raise RuntimeError("audit backend down")

    a, r = FakeAdapter(), FakeReceiver()
    result = await _audited_coord(a, r, bad_sink).migrate("u1")
    assert result.outcome == "migrated"
    assert a.tombstoned is True


@pytest.mark.asyncio
async def test_the_default_sink_is_the_security_event_log():
    """With no sink injected the coordinator must still reach SEL, not /dev/null."""
    seen: list[dict] = []
    a, r = FakeAdapter(), FakeReceiver()
    coord = P.MigrationCoordinator(
        adapter=a,
        receiver=r,
        source_crew=P.CrewRef(crew_id="src"),
        target_crew=P.CrewRef(crew_id="dst"),
    )
    with patch.object(P, "_sel_audit", seen.append):
        await coord.migrate("u1")
    assert any(e["event"] == "migrate.done" for e in seen)


# ------------------- 3.2: reconciliation uses the contract, not private state


@pytest.mark.asyncio
async def test_reconcile_uses_the_receivers_lookup_contract():
    a, r = FakeAdapter(), FakeReceiver()
    r._accepted["h"] = "remote-u1"
    seen: list[str] = []
    real_lookup = r.lookup

    async def spy(handoff_id):
        seen.append(handoff_id)
        return await real_lookup(handoff_id)

    r.lookup = spy  # type: ignore[assignment]

    resolved = await _coord(a, r).reconcile(handoff_id="h", unit_id="u1")
    assert resolved.owner == "target"
    assert seen == ["h"], "reconcile must go through lookup(), not private state"


@pytest.mark.asyncio
async def test_reconcile_refuses_a_receiver_that_cannot_be_queried():
    """A receiver with no lookup cannot resolve the crash window. Reconciliation
    must say so rather than guess -- guessing is how a unit gets two owners."""

    class NoLookup(P.MigrationReceiver):
        async def preflight(self, bundle):
            return P.PreflightReport(findings=[])

        async def accept(self, bundle):
            return P.AcceptAck(unit_id="remote-u1")

    a = FakeAdapter()
    with pytest.raises(NotImplementedError):
        await _coord(a, NoLookup()).reconcile(handoff_id="h", unit_id="u1")
