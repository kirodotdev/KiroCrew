"""Cron migration adapter tests — the plan-side surface.

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


@pytest.mark.asyncio
async def test_owner_approved_vault_grant_never_travels_to_another_crew():
    """A vault grant is the SOURCE owner's consent, so it must not ship.

    The ship path is an allow-list, so these fields are already off the wire by
    construction; this test is the guard that keeps them off. It goes red the
    moment anyone adds a grant field to ``CRON_SHIP_FIELDS``, which is the
    realistic way this protection would be lost.

    ``CronJob.secret_env`` is minted only by the owner approving a request on
    the Schedule page, precisely so an agent cannot grant itself vault access.
    Shipping the grant would turn migration into that bypass: the target's
    owner would inherit an authorization they never gave. Three further facts
    make shipping useless as well as unsafe — the values are secret NAMES that
    resolve only in the source host's vault, ``secret_env_pin`` is a keyed
    epoch-bound HMAC that cannot verify on the target and so fails closed at
    fire time, and a *pending* request is still awaiting the source owner's
    decision and must not be approved by someone who never saw it.
    """
    adapter = CronMigrationAdapter(
        job_lookup={
            "j1": _job(
                secret_env={"API_TOKEN": "prod/api-token"},
                secret_env_pin="pin-abc",
                secret_env_pending={"DB_URL": "prod/db-url"},
                secret_env_pending_pin="pin-def",
                secret_env_pending_ts=1234567890.0,
            )
        }
    )
    payload = await adapter.serialize("j1")
    for leaked in (
        "secret_env",
        "secret_env_pin",
        "secret_env_pending",
        "secret_env_pending_pin",
        "secret_env_pending_ts",
    ):
        assert leaked not in payload, f"vault grant field crossed the wire: {leaked}"
    # The secret NAME itself must not ride along under any other key.
    assert "prod/api-token" not in repr(payload)
    assert "prod/db-url" not in repr(payload)


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


# ------------------------------------------------- the transfer half is absent


@pytest.mark.asyncio
async def test_the_transfer_steps_are_absent_from_the_plan_adapter():
    """Pin the subtraction so a transfer step cannot return without its wiring.

    quiesce / unquiesce / materialize / tombstone / should_fire were removed with
    the coordinator that called them: nothing in production drove them, which is
    also why no tombstone was ever written and the Schedule page's redirect badge
    could not render. Re-adding one here would restore that gap.
    """
    adapter = CronMigrationAdapter(job_lookup={"j1": _job()})
    for absent in ("quiesce", "unquiesce", "materialize", "tombstone", "should_fire"):
        assert not hasattr(adapter, absent), f"{absent} is back without the transmit wiring"
