"""Slice 2 — cron migration adapter tests (issue #7577).

Covers plan.md Task 2.1–2.8: allow-list ship/drop over the real CronJob
dataclass, the drift guard, mid-run quiesce refusal, materialize re-bind
(user_paused preserved, next fire from the job's own timezone), tombstone,
and the double-fire guard.

Side-effect discipline (writing-tests skill): CronJob objects are built in
memory; no crons.json, no gateway, no threads.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.migration import protocol as P
from kiro_crew.migration.cron_adapter import (
    CRON_DROP_FIELDS,
    CRON_SHIP_FIELDS,
    CronMigrationAdapter,
)

# The four fields CronJob documents as "Runtime-only (never serialized)".
RUNTIME_ONLY = ("fire_time_denied", "run_never_started", "result_produced", "failure_recorded")


def _job(**over):
    base = dict(
        id="j1",
        name="nightly",
        message="run backup",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
        agent_id="kirocrew",
        timezone="America/New_York",
        user_paused=True,
        session_key="src-session-xyz",
        consecutive_failures=4,
        last_result="prev output",
        folder_id="grp-1",
    )
    base.update(over)
    return CronJob(**base)


# ------------------------------------------------------------ 2.1 allow-list


def test_ship_and_drop_partition_covers_every_cronjob_field():
    all_fields = {f.name for f in dataclasses.fields(CronJob)}
    partitioned = set(CRON_SHIP_FIELDS) | set(CRON_DROP_FIELDS)
    missing = all_fields - partitioned
    assert not missing, f"CronJob fields with no ship/drop decision: {missing}"
    overlap = set(CRON_SHIP_FIELDS) & set(CRON_DROP_FIELDS)
    assert not overlap, f"fields in BOTH ship and drop: {overlap}"


def test_runtime_only_and_failure_accounting_and_session_key_are_dropped():
    for f in RUNTIME_ONLY:
        assert f in CRON_DROP_FIELDS, f"{f} must be dropped"
    for f in (
        "consecutive_failures",
        "last_failure_at",
        "last_posted_hash",
        "last_result",
        "session_key",
        "id",
    ):
        assert f in CRON_DROP_FIELDS, f"{f} must be dropped"


@pytest.mark.asyncio
async def test_serialize_ships_allowed_and_omits_dropped():
    a = CronMigrationAdapter(job_lookup={"j1": _job()})
    payload = await a.serialize("j1")
    # allow-listed durable fields present
    assert payload["name"] == "nightly"
    assert payload["agent_id"] == "kirocrew"
    assert payload["timezone"] == "America/New_York"
    assert payload["user_paused"] is True
    # every dropped field absent — the regression guard that survives new fields
    for f in RUNTIME_ONLY:
        assert f not in payload
    for f in (
        "consecutive_failures",
        "last_result",
        "session_key",
        "id",
        "folder_id",
        "created_ts",
    ):
        assert f not in payload


# --------------------------------------------------------- 2.8 drift guard


def test_allow_list_drift_guard_named_fields_still_exist_on_cronjob():
    # If a CronJob field is renamed/removed, the ship/drop lists must be updated.
    all_fields = {f.name for f in dataclasses.fields(CronJob)}
    for f in CRON_SHIP_FIELDS:
        assert f in all_fields, f"ship field '{f}' no longer on CronJob"
    for f in CRON_DROP_FIELDS:
        assert f in all_fields, f"drop field '{f}' no longer on CronJob"


# --------------------------------------------------------- 2.3 quiesce mid-run


@pytest.mark.asyncio
async def test_quiesce_refuses_when_a_run_is_in_flight():
    a = CronMigrationAdapter(job_lookup={"j1": _job()}, is_running=lambda jid: True)
    with pytest.raises(P.MidRunError):
        await a.quiesce("j1")


@pytest.mark.asyncio
async def test_quiesce_marks_non_executing_when_idle():
    job = _job()
    a = CronMigrationAdapter(job_lookup={"j1": job}, is_running=lambda jid: False)
    token = await a.quiesce("j1")
    assert isinstance(token, P.QuiesceToken)
    assert job.enabled is False  # non-executing on source


# --------------------------------------------- 2.4 materialize re-bind + tz


@pytest.mark.asyncio
async def test_materialize_rebinds_scope_and_preserves_user_paused():
    src = CronMigrationAdapter(job_lookup={"j1": _job()})
    payload = await src.serialize("j1")

    created: dict = {}

    def create_job(fields):
        created.update(fields)
        return "remote-j1"

    dst = CronMigrationAdapter(create_job=create_job, target_session_key="dst-session")
    remote_id = await dst.materialize(payload)

    assert remote_id == "remote-j1"
    assert created["user_paused"] is True  # preserved
    assert created["timezone"] == "America/New_York"  # own tz kept
    assert created.get("session_key") == "dst-session"  # re-bound to target
    assert "id" not in created or created["id"] != "j1"  # target allocates id


# ------------------------------------------------------------- 2.5 tombstone


@pytest.mark.asyncio
async def test_tombstone_retains_non_executing_and_names_target():
    job = _job()
    a = CronMigrationAdapter(job_lookup={"j1": job})
    await a.tombstone("j1", P.CrewRef(crew_id="dst", label="target"), "remote-j1")
    assert job.enabled is False  # non-executing
    assert job.id == "j1"  # retained, not deleted
    ts = a.tombstone_of("j1")
    assert ts.target_crew.crew_id == "dst"
    assert ts.remote_unit_id == "remote-j1"


# ----------------------------------------------- 2.7 double-fire guard


def test_migrated_source_job_does_not_fire_after_next_due_instant():
    job = _job()
    a = CronMigrationAdapter(job_lookup={"j1": job})
    # after tombstone the source is non-executing; the scheduler predicate
    # the adapter exposes must report "do not fire" even past the due instant.
    import asyncio

    asyncio.get_event_loop() if False else None
    asyncio.run(a.tombstone("j1", P.CrewRef(crew_id="dst"), "remote-j1"))
    assert a.should_fire("j1", now_ts=4102444800.0) is False  # far future
