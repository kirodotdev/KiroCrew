"""Session non-portability reporting and the plan-side adapter.

Task 3.5 / Req 5.5-5.6: when a session is migrated, non-portable references
must be DROPPED and REPORTED, never silently swallowed. This is the pure
classification layer — it takes a session's metadata and returns the portable
subset plus one advisory Finding per dropped reference. It does not touch the
dashboard runtime (that reuse is a later circle).

Side-effect discipline: pure dict-in / (dict, findings)-out. No dashboard, no
event loop, no disk.
"""

from __future__ import annotations

import pytest

from kiro_crew.migration.session_adapter import (
    SESSION_NONPORTABLE,
    SessionMigrationAdapter,
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


# ------------------------------------ circle 5: SessionMigrationAdapter (3.1)


def _adapter(meta=None):
    return SessionMigrationAdapter(
        session_id="sess-1",
        bundle_builder=lambda sid: {
            "transcript": ["hi"],
            "layer_b": {"sid": sid},
            **(meta or {"project": "/mac/wt", "goal": "g"}),
        },
    )


@pytest.mark.asyncio
async def test_adapter_conforms_to_the_plan_side_seam():
    a = _adapter()
    assert a.bundle_kind == "session"
    for m in ("describe", "requirements", "serialize"):
        assert hasattr(a, m)


@pytest.mark.asyncio
async def test_the_transfer_steps_are_absent_from_the_plan_adapter():
    """Pin the subtraction: a transfer step back without wiring is a regression.

    quiesce / unquiesce / materialize / tombstone were removed with the
    coordinator that called them. Re-adding one here would make the adapter
    advertise a step nothing drives, which is what made the "single owner"
    claim untestable end to end.
    """
    a = _adapter()
    for absent in ("quiesce", "unquiesce", "materialize", "tombstone"):
        assert not hasattr(a, absent), f"{absent} is back without the transmit wiring"


@pytest.mark.asyncio
async def test_adapter_serialize_uses_builder_and_reports_nonportable():
    a = _adapter(
        meta={"transcript": ["hi"], "project": "/mac/wt", "model": "sonnet", "goal": "ship it"}
    )
    payload = await a.serialize("sess-1")
    # builder output carried; non-portable refs stripped + reported
    assert payload["goal"] == "ship it"
    assert "project" not in payload and "model" not in payload
    assert any(f.detail_key == "project" for f in a.last_findings)


# ------------------- 3.3: session requirements are actually derived (Req 5.6)


def _sess_adapter(meta):
    return SessionMigrationAdapter(
        session_id="sess-1",
        bundle_builder=lambda sid: meta,
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


# ------------------------------- the adapter's own construction contract


@pytest.mark.asyncio
async def test_the_adapter_routes_serialize_through_the_injected_builder():
    built = {"builder": 0}

    def bundle_builder(sid):
        built["builder"] += 1
        return {"transcript": ["hi"], "project": "/mac/wt", "goal": "g"}

    a = SessionMigrationAdapter(session_id="sess-1", bundle_builder=bundle_builder)

    assert isinstance(a, SessionMigrationAdapter)
    assert a.bundle_kind == "session"
    payload = await a.serialize("sess-1")
    assert built["builder"] == 1
    assert "project" not in payload and payload["goal"] == "g"


def test_the_adapter_requires_a_builder():
    """The builder is the only REQUIRED callable now.

    ``importer`` moved to the transfer half, so demanding it here would refuse a
    perfectly valid plan-only construction. A None builder is still a wiring
    error rather than a degraded mode: without it there is nothing to plan from.
    """
    with pytest.raises((TypeError, ValueError)):
        SessionMigrationAdapter(session_id="s", bundle_builder=None)


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
    a = SessionMigrationAdapter(
        session_id="sess-1",
        # no layer_b in the built bundle -> degraded fidelity must be reported
        bundle_builder=lambda sid: {
            "transcript": ["hi"],
            "project": "/mac/wt",
            "ledger": _ledger(),
        },
    )
    payload = await a.serialize("sess-1")
    # ledger working state travelled
    assert payload["ledger"]["goal"] == "ship the migration feature"
    keys = {f.detail_key for f in a.last_findings}
    assert "project" in keys  # non-portable reference (3.5)
    assert "layer_b" in keys  # degraded fidelity (3.6)
    assert "worktree" in keys  # host-local ledger artifact (3.3)
