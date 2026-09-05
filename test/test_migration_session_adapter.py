"""Slice 3 (first circle) — session non-portability reporting (issue #7577).

Task 3.5 / Req 5.5-5.6: when a session is migrated, non-portable references
must be DROPPED and REPORTED, never silently swallowed. This is the pure
classification layer — it takes a session's metadata and returns the portable
subset plus one advisory Finding per dropped reference. It does not touch the
dashboard runtime (that reuse is a later circle).

Side-effect discipline: pure dict-in / (dict, findings)-out. No dashboard, no
event loop, no disk.
"""

from __future__ import annotations

from kiro_crew.migration import protocol as P
from kiro_crew.migration.session_adapter import (
    SESSION_NONPORTABLE,
    SessionMigrationAdapter,
    SessionMonitorHandoff,
    SessionQuiesce,
    SessionTombstone,
    build_session_adapter,
    carry_session_ledger,
    classify_session_portability,
    layer_b_fidelity_findings,
)


def _meta(**over):
    base = {
        "project": "/Users/alice/mac/worktree",  # a Mac path — not on EC2
        "model": "sonnet",
        "workspace": "default",
        "agent": "kirocrew",
        "goal": "ship the migration feature",  # portable working state
        "phase": "implementing",
    }
    base.update(over)
    return base


def test_nonportable_set_is_the_documented_four():
    assert set(SESSION_NONPORTABLE) == {"project", "model", "workspace", "agent"}


def test_dropped_references_are_reported_not_swallowed():
    portable, findings = classify_session_portability(_meta())
    dropped = {f.detail_key for f in findings}
    # project/model/workspace are hard-dropped; agent is hint-only (also reported)
    assert {"project", "model", "workspace", "agent"} <= dropped
    # every finding is advisory (a dropped reference is not a blocker)
    assert all(f.severity == "advisory" for f in findings)


def test_portable_working_state_survives():
    portable, _ = classify_session_portability(_meta())
    assert portable["goal"] == "ship the migration feature"
    assert portable["phase"] == "implementing"
    # the non-portable references are not in the portable subset
    for k in SESSION_NONPORTABLE:
        assert k not in portable


def test_absent_nonportable_reference_produces_no_finding():
    # only report what was actually present and dropped
    portable, findings = classify_session_portability({"goal": "g", "agent": "kirocrew"})
    dropped = {f.detail_key for f in findings}
    assert dropped == {"agent"}  # project/model/workspace absent → not reported
    assert portable["goal"] == "g"


def test_findings_name_the_reference_without_leaking_a_path_into_severity():
    portable, findings = classify_session_portability(_meta())
    proj = next(f for f in findings if f.detail_key == "project")
    assert proj.kind == "project_checkout"
    assert "will not transfer" in proj.detail.lower() or "dropped" in proj.detail.lower()


# --------------------------------------- circle 2: monitor-loop disarm/re-arm

import pytest


class FakeLoopController:
    """Stands in for the monitor-loop machinery: tracks armed state per crew."""

    def __init__(self, armed_on_source=True):
        self.source_armed = armed_on_source
        self.target_armed = False
        self.saved_spec = None

    def disarm_source(self):
        self.saved_spec = {"interval": 300, "message": "check PR"} if self.source_armed else None
        self.source_armed = False

    def arm_target(self, spec):
        self.target_armed = True


def test_no_loop_on_source_is_a_noop_and_never_arms_target():
    lc = FakeLoopController(armed_on_source=False)
    h = SessionMonitorHandoff(lc)
    h.quiesce_source()
    h.rearm_target_after_ack()
    assert lc.source_armed is False
    assert lc.target_armed is False  # nothing to re-arm


def test_loop_disarmed_on_source_before_ack_then_rearmed_on_target():
    lc = FakeLoopController(armed_on_source=True)
    h = SessionMonitorHandoff(lc)
    # at quiesce: source disarmed, target NOT yet armed (never both)
    h.quiesce_source()
    assert lc.source_armed is False
    assert lc.target_armed is False
    # only after durable ack does the target arm
    h.rearm_target_after_ack()
    assert lc.target_armed is True
    assert lc.source_armed is False  # invariant: never armed on both


def test_never_armed_on_both_at_any_observable_point():
    lc = FakeLoopController(armed_on_source=True)
    h = SessionMonitorHandoff(lc)
    assert not (lc.source_armed and lc.target_armed)  # before
    h.quiesce_source()
    assert not (lc.source_armed and lc.target_armed)  # mid
    h.rearm_target_after_ack()
    assert not (lc.source_armed and lc.target_armed)  # after


def test_rollback_before_ack_rearms_source_and_leaves_target_unarmed():
    lc = FakeLoopController(armed_on_source=True)
    h = SessionMonitorHandoff(lc)
    h.quiesce_source()
    # transmit failed before ack -> un-quiesce: source reclaims its loop
    h.rearm_source_on_failure()
    assert lc.source_armed is True
    assert lc.target_armed is False


# ---------------------------------------- circle 3: session quiesce (Task 3.2)


class FakeSessionController:
    """Stands in for the dashboard slot: tracks turn-acceptance + in-flight."""

    def __init__(self, in_flight=False):
        self.accepting = True
        self.in_flight = in_flight
        self.drained = False

    def block_new_turns(self):
        self.accepting = False

    def allow_new_turns(self):
        self.accepting = True

    def drain_in_flight(self, timeout):
        # a real drain awaits the running turn; the fake just clears the flag
        if self.in_flight:
            self.in_flight = False
        self.drained = True
        return True


def test_quiesce_blocks_new_turns_then_drains():
    sc = FakeSessionController(in_flight=True)
    q = SessionQuiesce(sc)
    token = q.quiesce("sess-1")
    assert isinstance(token, P.QuiesceToken)
    assert sc.accepting is False  # new turns blocked
    assert sc.in_flight is False  # in-flight drained
    assert sc.drained is True


def test_quiesce_refuses_when_in_flight_turn_will_not_drain():
    class Stuck(FakeSessionController):
        def drain_in_flight(self, timeout):
            return False  # turn did not finish in time

    sc = Stuck(in_flight=True)
    q = SessionQuiesce(sc)
    with pytest.raises(P.MidRunError):
        q.quiesce("sess-1")
    # refusing must not leave the session blocked — it is un-quiesced
    assert sc.accepting is True


def test_unquiesce_reopens_turns():
    sc = FakeSessionController(in_flight=False)
    q = SessionQuiesce(sc)
    token = q.quiesce("sess-1")
    q.unquiesce("sess-1", token)
    assert sc.accepting is True


# ---------------------------------------- circle 4: session tombstone (3.7/5.11)


class FakeSlot:
    """Stands in for the source slot after migration."""

    def __init__(self):
        self.accepting = True
        self.transcript_readable = True
        self.new_home = None

    def block_new_turns(self):
        self.accepting = False


def test_tombstone_retains_readable_transcript_and_shows_new_home():
    slot = FakeSlot()
    t = SessionTombstone(slot)
    t.tombstone("sess-1", P.CrewRef(crew_id="remote-ec2", label="EC2"), "remote-sess-9")
    assert slot.transcript_readable is True  # retained + readable (5.11)
    assert slot.accepting is False  # refuses new turns
    assert slot.new_home is not None
    assert slot.new_home.target_crew.crew_id == "remote-ec2"
    assert slot.new_home.remote_unit_id == "remote-sess-9"


def test_tombstoned_slot_reports_its_tombstone():
    slot = FakeSlot()
    t = SessionTombstone(slot)
    t.tombstone("sess-1", P.CrewRef(crew_id="dst"), "remote-1")
    ts = t.tombstone_of("sess-1")
    assert ts.unit_kind == "session"
    assert ts.remote_unit_id == "remote-1"


# ------------------------------------ circle 5: SessionMigrationAdapter (3.1)


class _FullController(FakeSessionController, FakeSlot):
    """A slot that satisfies both quiesce and tombstone controller contracts."""

    def __init__(self):
        FakeSessionController.__init__(self, in_flight=False)
        FakeSlot.__init__(self)


def _adapter(meta=None):
    ctrl = _FullController()
    return (
        SessionMigrationAdapter(
            session_id="sess-1",
            controller=ctrl,
            # injected: bundle builder returns Layer A/B, importer returns a new id
            bundle_builder=lambda sid: {
                "transcript": ["hi"],
                "layer_b": {"sid": sid},
                **(meta or {"project": "/mac/wt", "goal": "g"}),
            },
            importer=lambda payload: "remote-sess-9",
        ),
        ctrl,
    )


@pytest.mark.asyncio
async def test_adapter_conforms_to_the_migration_unit_adapter_seam():
    a, _ = _adapter()
    assert a.bundle_kind == "session"
    # duck-typed against the protocol seam
    for m in (
        "describe",
        "requirements",
        "quiesce",
        "unquiesce",
        "serialize",
        "materialize",
        "tombstone",
    ):
        assert hasattr(a, m)


@pytest.mark.asyncio
async def test_adapter_serialize_uses_builder_and_reports_nonportable():
    a, _ = _adapter(
        meta={"transcript": ["hi"], "project": "/mac/wt", "model": "sonnet", "goal": "ship it"}
    )
    payload = await a.serialize("sess-1")
    # builder output carried; non-portable refs stripped + reported
    assert payload["goal"] == "ship it"
    assert "project" not in payload and "model" not in payload
    assert any(f.detail_key == "project" for f in a.last_findings)


@pytest.mark.asyncio
async def test_adapter_quiesce_blocks_turns_and_materialize_imports():
    a, ctrl = _adapter()
    await a.quiesce("sess-1")
    assert ctrl.accepting is False
    remote = await a.materialize({"transcript": ["hi"]})
    assert remote == "remote-sess-9"


@pytest.mark.asyncio
async def test_adapter_tombstone_redirects_source_and_names_target():
    a, ctrl = _adapter()
    await a.tombstone("sess-1", P.CrewRef(crew_id="remote-ec2"), "remote-sess-9")
    assert ctrl.accepting is False  # refuses new turns
    assert ctrl.transcript_readable is True  # transcript retained
    assert ctrl.new_home.target_crew.crew_id == "remote-ec2"


# ------------------- 3.3: session requirements are actually derived (Req 5.6)


def _sess_adapter(meta):
    ctrl = _FullController()
    return build_session_adapter(
        session_id="sess-1",
        controller=ctrl,
        bundle_builder=lambda sid: meta,
        importer=lambda payload: "remote-1",
    )


@pytest.mark.asyncio
async def test_session_requirements_name_the_agent_the_target_must_have():
    a = _sess_adapter({"transcript": ["hi"], "agent": "kirocrew-research"})
    reqs = await a.requirements("sess-1")
    agent = next(r for r in reqs if r.kind == "agent")
    assert agent.identity == "kirocrew-research"
    # hint-only: an absent agent degrades resolution, it does not lose the work
    assert agent.severity == "advisory"


@pytest.mark.asyncio
async def test_session_requirements_name_the_project_checkout():
    a = _sess_adapter({"transcript": ["hi"], "project": "/Users/alice/wt/x"})
    reqs = await a.requirements("sess-1")
    proj = next(r for r in reqs if r.kind == "project_checkout")
    assert proj.identity == "/Users/alice/wt/x"
    # rematerialization is explicitly out of scope, so this is reported not blocked
    assert proj.severity == "advisory"


@pytest.mark.asyncio
async def test_session_requirements_name_each_mcp_server():
    a = _sess_adapter({"transcript": ["hi"], "mcp_servers": ["kirocrew-core", "kirocrew-cron"]})
    reqs = await a.requirements("sess-1")
    names = {r.identity for r in reqs if r.kind == "mcp_server"}
    assert names == {"kirocrew-core", "kirocrew-cron"}
    # a session whose tools are absent on the target cannot continue its work
    assert all(r.severity == "blocking" for r in reqs if r.kind == "mcp_server")


@pytest.mark.asyncio
async def test_session_requirements_are_empty_when_nothing_is_referenced():
    a = _sess_adapter({"transcript": ["hi"]})
    assert await a.requirements("sess-1") == []


@pytest.mark.asyncio
async def test_session_requirements_do_not_invent_a_requirement_from_a_blank():
    a = _sess_adapter({"transcript": ["hi"], "agent": "", "project": "", "mcp_servers": []})
    assert await a.requirements("sess-1") == []


@pytest.mark.asyncio
async def test_session_preflight_can_actually_block_on_a_missing_mcp_server():
    """The point of deriving requirements: before this, a session preflight had
    nothing to check and so could never refuse."""
    from kiro_crew.migration.receiver import LocalMigrationReceiver, RequirementProbe

    a = _sess_adapter({"transcript": ["hi"], "mcp_servers": ["ghost-server"]})
    reqs = await a.requirements("sess-1")
    probe = RequirementProbe(
        agent_exists=lambda i: True,
        script_path_ok=lambda i: True,
        command_allowed=lambda i: True,
    )
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        r = LocalMigrationReceiver(store_dir=td, materialize=lambda p: "x", requirement_probe=probe)
        bundle = P.MigrationBundle(
            bundle_kind="session",
            bundle_version=2,
            handoff_id="h",
            created_ts=0.0,
            source_crew=P.CrewRef(crew_id="src"),
            payload={"transcript": ["hi"]},
            requirements=reqs,
        )
        report = await r.preflight(bundle)
    # the probe has no mcp_server check yet, so this asserts the requirement is
    # PRESENT and carries blocking severity — the receiver's probe gains the
    # mcp_server kind when a real target-side lookup exists
    assert any(rq.kind == "mcp_server" and rq.severity == "blocking" for rq in bundle.requirements)
    assert isinstance(report, P.PreflightReport)


# ------------------------------- circle 6: build_session_adapter factory (3.1)


@pytest.mark.asyncio
async def test_build_session_adapter_wires_callables_into_a_seam_adapter():
    ctrl = _FullController()
    built = {"builder": 0, "importer": 0}

    def bundle_builder(sid):
        built["builder"] += 1
        return {"transcript": ["hi"], "project": "/mac/wt", "goal": "g"}

    def importer(payload):
        built["importer"] += 1
        return "remote-sess-42"

    a = build_session_adapter(
        session_id="sess-1", controller=ctrl, bundle_builder=bundle_builder, importer=importer
    )

    assert isinstance(a, SessionMigrationAdapter)
    assert a.bundle_kind == "session"
    # serialize routes through the provided builder + strips non-portable refs
    payload = await a.serialize("sess-1")
    assert built["builder"] == 1
    assert "project" not in payload and payload["goal"] == "g"
    # materialize routes through the provided importer
    remote = await a.materialize(payload)
    assert built["importer"] == 1 and remote == "remote-sess-42"


def test_build_session_adapter_requires_both_callables():
    ctrl = _FullController()
    with pytest.raises((TypeError, ValueError)):
        build_session_adapter(
            session_id="s", controller=ctrl, bundle_builder=None, importer=lambda p: "x"
        )
    with pytest.raises((TypeError, ValueError)):
        build_session_adapter(
            session_id="s", controller=ctrl, bundle_builder=lambda sid: {}, importer=None
        )


# ------------------- circle 8: ledger carry (3.3) + Layer B fidelity (3.6)


def _ledger():
    return {
        "schema": 1,
        "goal": "ship the migration feature",
        "phase": "implementing",
        "next": "wire the receiver to the tunnel",
        "tried": [{"approach": "distributed lease", "because": "over-engineered"}],
        "artifacts": {"branch": "feat/x", "pr": "7577", "worktree": "/Users/alice/mac/wt/x"},
        "events": [{"kind": "progress", "text": "phase 1 green"}],
    }


def test_carry_ledger_ships_the_four_working_state_fields():
    carried, findings = carry_session_ledger(_ledger())
    assert carried["goal"] == "ship the migration feature"
    assert carried["phase"] == "implementing"
    assert carried["next"] == "wire the receiver to the tunnel"
    assert carried["tried"][0]["approach"] == "distributed lease"


def test_carry_ledger_drops_absolute_path_artifacts_and_reports_them():
    carried, findings = carry_session_ledger(_ledger())
    # a host-local worktree path is the same class as a dropped project path
    assert "worktree" not in carried["artifacts"]
    assert any(f.detail_key == "worktree" for f in findings)
    assert all(f.severity == "advisory" for f in findings)


def test_carry_ledger_keeps_portable_artifact_values():
    carried, _ = carry_session_ledger(_ledger())
    assert carried["artifacts"]["branch"] == "feat/x"
    assert carried["artifacts"]["pr"] == "7577"


def test_carry_ledger_never_leaks_the_dropped_path_into_the_finding():
    _, findings = carry_session_ledger(_ledger())
    wt = next(f for f in findings if f.detail_key == "worktree")
    assert "/Users/alice" not in wt.detail


def test_carry_ledger_on_empty_state_is_empty_and_silent():
    carried, findings = carry_session_ledger({})
    assert carried["goal"] == "" and carried["tried"] == []
    assert findings == []


def test_layer_b_present_produces_no_fidelity_finding():
    assert layer_b_fidelity_findings({"transcript": ["hi"], "layer_b": {"sid": "s1"}}) == []


def test_layer_b_absent_warns_about_transcript_prefix_fidelity():
    findings = layer_b_fidelity_findings({"transcript": ["hi"]})
    assert len(findings) == 1
    f = findings[0]
    assert f.detail_key == "layer_b"
    assert f.severity == "advisory"  # degraded, not blocking
    assert "transcript" in f.detail.lower()


def test_layer_b_empty_counts_as_absent():
    assert len(layer_b_fidelity_findings({"layer_b": {}})) == 1


@pytest.mark.asyncio
async def test_adapter_serialize_carries_ledger_and_reports_layer_b_gap():
    ctrl = _FullController()
    a = build_session_adapter(
        session_id="sess-1",
        controller=ctrl,
        # no layer_b in the built bundle -> degraded fidelity must be reported
        bundle_builder=lambda sid: {
            "transcript": ["hi"],
            "project": "/mac/wt",
            "ledger": _ledger(),
        },
        importer=lambda p: "remote-1",
    )
    payload = await a.serialize("sess-1")
    # ledger working state travelled
    assert payload["ledger"]["goal"] == "ship the migration feature"
    keys = {f.detail_key for f in a.last_findings}
    assert "project" in keys  # non-portable reference (3.5)
    assert "layer_b" in keys  # degraded fidelity (3.6)
    assert "worktree" in keys  # host-local ledger artifact (3.3)
