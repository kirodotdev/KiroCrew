"""Slice 1 (circle 2) — durable LocalMigrationReceiver (issue #7577, Task 1.5).

The target-side receiver: preflight (pure), accept (validate -> persist ->
fsync -> ack), and lookup by handoff_id for reconciliation. accept is
idempotent on handoff_id so a retransmit never creates a second unit (Req 2.7).

Side-effect discipline: everything under a tmp_path; no real data home.
"""

from __future__ import annotations

import pytest

from kiro_crew.migration import protocol as P
from kiro_crew.migration.receiver import (
    LocalMigrationReceiver,
    RequirementProbe,
    build_requirement_probe,
)


def _bundle(handoff_id="h1", unit="u1"):
    return P.MigrationBundle(
        bundle_kind="cron",
        bundle_version=1,
        handoff_id=handoff_id,
        created_ts=0.0,
        source_crew=P.CrewRef(crew_id="src"),
        payload={"unit_id": unit, "name": "j"},
        requirements=[],
    )


@pytest.mark.asyncio
async def test_accept_persists_and_returns_ack(tmp_path):
    r = LocalMigrationReceiver(
        store_dir=tmp_path, materialize=lambda payload: "mat-" + payload["unit_id"]
    )
    ack = await r.accept(_bundle())
    assert ack.unit_id == "mat-u1"
    # a record file exists on disk (durable)
    assert any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_accept_is_idempotent_on_handoff_id(tmp_path):
    calls = []

    def mat(payload):
        calls.append(payload["unit_id"])
        return "mat-" + payload["unit_id"]

    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=mat)
    a1 = await r.accept(_bundle(handoff_id="dup"))
    a2 = await r.accept(_bundle(handoff_id="dup"))
    assert a1.unit_id == a2.unit_id
    assert calls == ["u1"]  # materialize ran exactly once


@pytest.mark.asyncio
async def test_lookup_returns_the_held_unit_or_none(tmp_path):
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "mat-" + p["unit_id"])
    assert await r.lookup("absent") is None
    await r.accept(_bundle(handoff_id="present"))
    ack = await r.lookup("present")
    assert ack is not None and ack.unit_id == "mat-u1"


@pytest.mark.asyncio
async def test_lookup_survives_a_fresh_receiver_instance(tmp_path):
    # durability: a new receiver over the same dir still finds the record
    r1 = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "mat-" + p["unit_id"])
    await r1.accept(_bundle(handoff_id="persisted"))
    r2 = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "SHOULD-NOT-RUN")
    ack = await r2.lookup("persisted")
    assert ack is not None and ack.unit_id == "mat-u1"


@pytest.mark.asyncio
async def test_preflight_is_read_only_and_writes_nothing(tmp_path):
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    report = await r.preflight(_bundle())
    assert isinstance(report, P.PreflightReport)
    assert list(tmp_path.iterdir()) == []  # preflight persisted nothing


def _bundle_with_reqs(*reqs):
    b = _bundle()
    return P.MigrationBundle(
        bundle_kind=b.bundle_kind,
        bundle_version=b.bundle_version,
        handoff_id=b.handoff_id,
        created_ts=b.created_ts,
        source_crew=b.source_crew,
        payload=b.payload,
        requirements=list(reqs),
    )


@pytest.mark.asyncio
async def test_preflight_flags_unsatisfiable_requirement_as_blocking(tmp_path):
    # a probe where the agent does NOT exist on the target
    probe = RequirementProbe(
        agent_exists=lambda ident: False,
        script_path_ok=lambda ident: True,
        command_allowed=lambda ident: True,
    )
    r = LocalMigrationReceiver(
        store_dir=tmp_path, materialize=lambda p: "x", requirement_probe=probe
    )
    bundle = _bundle_with_reqs(
        P.HostRequirement(kind="agent", identity="ghost-agent", severity="blocking")
    )
    report = await r.preflight(bundle)
    assert report.blocked is True
    assert any(f.detail_key == "ghost-agent" or "ghost-agent" in f.detail for f in report.findings)


@pytest.mark.asyncio
async def test_preflight_passes_when_all_requirements_satisfiable(tmp_path):
    probe = RequirementProbe(
        agent_exists=lambda ident: True,
        script_path_ok=lambda ident: True,
        command_allowed=lambda ident: True,
    )
    r = LocalMigrationReceiver(
        store_dir=tmp_path, materialize=lambda p: "x", requirement_probe=probe
    )
    bundle = _bundle_with_reqs(
        P.HostRequirement(kind="agent", identity="kirocrew", severity="blocking"),
        P.HostRequirement(kind="script_path", identity="~/c/x.py:go", severity="blocking"),
    )
    report = await r.preflight(bundle)
    assert report.blocked is False
    assert report.findings == []


@pytest.mark.asyncio
async def test_preflight_without_probe_defaults_to_empty_report(tmp_path):
    # SUPERSEDED. This test used to hand preflight a bundle carrying a BLOCKING
    # agent requirement and assert `report.blocked is False`, calling that
    # "conservative". It was not conservative: it codified the fail-open. An
    # empty findings list is how a caller reads a green light, so this asserted
    # that an unverifiable requirement may be admitted.
    #
    # Replaced by the pair that separates the two cases the old test conflated:
    #   test_preflight_with_no_probe_refuses_a_bundle_that_has_requirements
    #   test_preflight_with_no_probe_still_admits_a_bundle_needing_nothing
    # Kept as a marker rather than silently deleted, because a future reader
    # finding the old name in git history should learn why it went.
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    report = await r.preflight(
        _bundle_with_reqs(P.HostRequirement(kind="agent", identity="x", severity="blocking"))
    )
    assert report.blocked is True, "an unverifiable requirement is refused, not admitted"


# ------------------------------- build_requirement_probe factory (real wiring)


def test_build_requirement_probe_agent_exists_uses_injected_lister(tmp_path):
    probe = build_requirement_probe(
        list_agent_names=lambda: ["kirocrew", "kirocrew-lite"],
        crons_dir=tmp_path,
        command_allowed=lambda cmd: True,
    )
    assert isinstance(probe, RequirementProbe)
    assert probe.agent_exists("kirocrew") is True
    assert probe.agent_exists("ghost") is False


def test_build_requirement_probe_script_path_must_resolve_under_crons_dir(tmp_path):
    # a script identity is "<path>:func"; the path part must be under crons_dir
    good = tmp_path / "job.py"
    good.write_text("def go(ctx): ...", encoding="utf-8")
    probe = build_requirement_probe(
        list_agent_names=lambda: [],
        crons_dir=tmp_path,
        command_allowed=lambda cmd: True,
    )
    assert probe.script_path_ok(f"{good}:go") is True
    assert probe.script_path_ok(f"{tmp_path/'missing.py'}:go") is False
    # a path outside crons_dir is rejected even if it exists
    outside = tmp_path.parent / "elsewhere.py"
    assert probe.script_path_ok(f"{outside}:go") is False


def test_build_requirement_probe_command_uses_injected_policy(tmp_path):
    probe = build_requirement_probe(
        list_agent_names=lambda: [],
        crons_dir=tmp_path,
        command_allowed=lambda cmd: cmd.startswith("echo "),
    )
    assert probe.command_allowed("echo hi") is True
    assert probe.command_allowed("rm -rf /") is False


# ------------------- 3.4: the receiver validates bundle_version compatibility


def _versioned(kind="cron", version=1, handoff="hv"):
    return P.MigrationBundle(
        bundle_kind=kind,
        bundle_version=version,
        handoff_id=handoff,
        created_ts=0.0,
        source_crew=P.CrewRef(crew_id="src"),
        payload={"unit_id": "u1", "name": "j"},
        requirements=[],
    )


@pytest.mark.asyncio
async def test_accept_takes_a_known_bundle_version(tmp_path):
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    ack = await r.accept(_versioned("cron", 1))
    assert ack.unit_id == "x"


@pytest.mark.asyncio
async def test_accept_refuses_a_future_bundle_version(tmp_path):
    """A newer source must not have its bundle silently half-understood."""
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    with pytest.raises(ValueError) as exc:
        await r.accept(_versioned("cron", 99))
    assert "version" in str(exc.value).lower()
    assert list(tmp_path.iterdir()) == []  # nothing persisted on refusal


@pytest.mark.asyncio
async def test_accept_refuses_an_unknown_bundle_kind(tmp_path):
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    with pytest.raises(ValueError):
        await r.accept(_versioned("quantum-widget", 1))


@pytest.mark.asyncio
async def test_session_bundles_are_version_2(tmp_path):
    """session_transfer's Layer-B format is v2; the receiver must know that and
    must NOT accept a v1 session bundle as if the layers were the same."""
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "s")
    ack = await r.accept(_versioned("session", 2, handoff="hs2"))
    assert ack.unit_id == "s"
    with pytest.raises(ValueError):
        await r.accept(_versioned("session", 1, handoff="hs1"))


# --------------- 3.5: the receiver re-scans for credential material


@pytest.mark.asyncio
async def test_accept_refuses_a_payload_carrying_credential_material(tmp_path):
    """Defence in depth: the source scans before sending, but a target must not
    trust that the sender did. A compromised or older source is exactly the case
    the receiver-side scan exists for."""
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    bad = P.MigrationBundle(
        bundle_kind="cron",
        bundle_version=1,
        handoff_id="hsec",
        created_ts=0.0,
        source_crew=P.CrewRef(crew_id="src"),
        payload={"unit_id": "u1", "command": "curl -H 'x-api-key: AKIAIOSFODNN7EXAMPLE'"},
        requirements=[],
    )
    with pytest.raises(ValueError) as exc:
        await r.accept(bad)
    assert "credential" in str(exc.value).lower()
    # refused BEFORE materialize and before anything hit disk
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_the_refusal_message_never_echoes_the_secret(tmp_path):
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    bad = P.MigrationBundle(
        bundle_kind="cron",
        bundle_version=1,
        handoff_id="hsec2",
        created_ts=0.0,
        source_crew=P.CrewRef(crew_id="src"),
        payload={"unit_id": "u1", "command": "AKIAIOSFODNN7EXAMPLE"},
        requirements=[],
    )
    with pytest.raises(ValueError) as exc:
        await r.accept(bad)
    assert "AKIA" not in str(exc.value)


@pytest.mark.asyncio
async def test_accept_is_audited_on_the_target_crew(tmp_path):
    """Req 3.5 says BOTH crews record the handoff. The source side is the
    coordinator's; this is the target's half."""
    seen: list[dict] = []
    r = LocalMigrationReceiver(
        store_dir=tmp_path, materialize=lambda p: "x", audit_sink=seen.append
    )
    await r.accept(_versioned("cron", 1, handoff="haud"))
    assert any(e["event"] == "accept.persisted" and e["handoff_id"] == "haud" for e in seen)


@pytest.mark.asyncio
async def test_a_deduped_accept_is_audited_as_a_replay(tmp_path):
    seen: list[dict] = []
    r = LocalMigrationReceiver(
        store_dir=tmp_path, materialize=lambda p: "x", audit_sink=seen.append
    )
    await r.accept(_versioned("cron", 1, handoff="hdup"))
    await r.accept(_versioned("cron", 1, handoff="hdup"))
    events = [e["event"] for e in seen]
    assert events.count("accept.persisted") == 1
    assert "accept.replayed" in events


@pytest.mark.asyncio
async def test_accept_rejects_a_bundle_with_empty_payload(tmp_path):
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    bad = P.MigrationBundle(
        bundle_kind="cron",
        bundle_version=1,
        handoff_id="b",
        created_ts=0.0,
        source_crew=P.CrewRef(crew_id="s"),
        payload={},
        requirements=[],
    )  # empty payload
    with pytest.raises(ValueError):
        await r.accept(bad)


# ------------------------------------ Circle 2: preflight must not fail OPEN


@pytest.mark.asyncio
async def test_preflight_with_no_probe_refuses_a_bundle_that_has_requirements(tmp_path):
    """No probe + real requirements must BLOCK, not wave the migration through.

    An empty findings list is not "conservative" -- it is a GREEN LIGHT. Every
    caller reads `report.blocked`, and with no findings that is False, so a
    receiver built without a probe admitted every migration to any target no
    matter what the unit needed. A gate that refuses nothing is strictly worse
    than no gate, because the surfaces above it believe a check happened.

    The refusal must be proportional: it is the *unverifiable requirement* that
    blocks, so the finding has to name which one and say it could not be
    checked. Asserting only `blocked is True` would let a receiver that refuses
    everything for the wrong reason pass this test.
    """
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")
    bundle = _bundle_with_reqs(
        P.HostRequirement(kind="agent", identity="research-agent", severity="blocking")
    )

    report = await r.preflight(bundle)

    assert report.blocked is True, "an unverifiable requirement must not be admitted"
    assert [f.kind for f in report.findings] == ["agent"], "the finding names the requirement"
    finding = report.findings[0]
    assert finding.severity == "blocking"
    assert finding.detail_key == "research-agent", "names the reference key, not a value"
    # The REASON must distinguish "cannot verify" from "verified and absent" --
    # they call for different operator actions (configure the probe vs install
    # the agent).
    assert (
        "verif" in finding.detail.lower()
    ), f"reason must say it could not verify: {finding.detail!r}"


@pytest.mark.asyncio
async def test_preflight_with_no_probe_still_admits_a_bundle_needing_nothing(tmp_path):
    """Closing the fail-open must not become a fail-CLOSED for everyone.

    A unit with no host requirements has nothing for a probe to check, so a
    missing probe is not a gap in its case. Blocking it would make the fix worse
    than the defect -- no migration could ever run without a probe configured,
    including the ones that need nothing.
    """
    r = LocalMigrationReceiver(store_dir=tmp_path, materialize=lambda p: "x")

    report = await r.preflight(_bundle())

    assert report.blocked is False
    assert report.findings == []


@pytest.mark.asyncio
async def test_preflight_refuses_a_requirement_kind_it_cannot_check(tmp_path):
    """An unknown requirement kind must block, not be silently skipped.

    This is the second fail-open, and the more dangerous one because it fires
    even WITH a probe: `checks.get(req.kind)` returning None fell through to
    `continue`. HostRequirement's own docstring lists seven kinds and the probe
    covers three, so `credential`, `mcp_server`, `project_checkout` and
    `git_repo` were all being admitted unchecked -- and any kind added later
    would inherit that silence instead of failing loudly.
    """
    probe = RequirementProbe(
        agent_exists=lambda name: True,
        script_path_ok=lambda p: True,
        command_allowed=lambda c: True,
    )
    r = LocalMigrationReceiver(
        store_dir=tmp_path, materialize=lambda p: "x", requirement_probe=probe
    )
    bundle = _bundle_with_reqs(
        P.HostRequirement(kind="mcp_server", identity="atlassian", severity="blocking")
    )

    report = await r.preflight(bundle)

    assert report.blocked is True, "an unprobed kind must not be admitted"
    assert report.findings[0].detail_key == "atlassian"
    detail = report.findings[0].detail.lower()
    assert "mcp_server" in detail, "the reason names the kind that could not be checked"
