"""Cron migration adapter — plan side.

Wraps a ``CronJob`` in the generic ``MigrationUnitAdapter`` seam. The whole
point of the slice is the allow-list: every ``CronJob`` field is either
SHIPPED or DROPPED by explicit name, so a field someone adds next year is a
loud test failure (the drift guard) rather than a silent leak.

Dropped, and why (see design.md → Per-Unit → Cron):
  * the four Runtime_Only_Fields — meaningless off the source host;
  * every failure-accounting / dedup field — observations of the SOURCE's
    execution history, not portable state;
  * ``session_key`` — a source-local ownership scope the target re-binds;
  * ``id`` — the target allocates its own;
  * ``created_ts`` / ``created_by`` / ``folder_id`` — source-local provenance
    and grouping.
"""

from __future__ import annotations

import dataclasses

from kiro_crew.cron import CronJob
from kiro_crew.migration import protocol as P

# Durable, portable fields — shipped in the bundle.
CRON_SHIP_FIELDS: tuple[str, ...] = (
    "name",
    "message",
    "schedule",
    "channel",
    "thread_ts",
    "enabled",
    "user_paused",
    "delete_after_run",
    "context_enabled",
    "agent_id",
    "approval_mode",
    "silent",
    "skip_dates",
    "timezone",
    "persistent_session",
    "minimal_context",
    "hide_in_chat",
    "model",
    "agent_sequence",
    "env",
    "timeout_secs",
    "strict_schedule",
    "script",
    "command",
    "timeout",
)

# Everything not shipped — dropped by explicit decision.
CRON_DROP_FIELDS: tuple[str, ...] = (
    # id + source-local provenance / grouping
    "id",
    "created_ts",
    "created_by",
    "folder_id",
    "session_key",
    # the four Runtime_Only_Fields
    "fire_time_denied",
    "run_never_started",
    "result_produced",
    "failure_recorded",
    # execution history / auto-pause state
    "last_run_ts",
    "last_status",
    "last_error",
    "auto_paused",
    "last_result",
    # ...including the run's identity and its already-rendered header stamp.
    # Both describe a run that happened on the SOURCE host, so shipping them
    # would have the target attribute someone else's execution to itself.
    "last_result_ts",
    "last_result_stamp",
    "acked_items",
    # dedup / failure-accounting
    "last_posted_hash",
    "consecutive_dupes",
    "last_posted_at",
    "last_failure_hash",
    "last_failure_at",
    "consecutive_failures",
    # Owner-approved vault grants, and the agent's pending request for one.
    # Dropped on four independent grounds. (1) `secret_env` is minted ONLY by
    # the owner approving a request on the Schedule page, specifically so an
    # agent cannot grant itself vault access; shipping it would make migration
    # that bypass, handing the target's owner an authorization they never gave.
    # (2) The values are secret NAMES resolved against the SOURCE host's vault,
    # so they name nothing on the target. (3) `secret_env_pin` is a keyed,
    # epoch-bound HMAC over the approved code, so it cannot verify on the
    # target and would fail closed at fire time regardless. (4) A *pending*
    # request is still awaiting the source owner's decision and must not be
    # inherited by an owner who never saw it. The target re-requests and its
    # own owner approves.
    "secret_env",
    "secret_env_pin",
    "secret_env_pending",
    "secret_env_pending_pin",
    "secret_env_pending_ts",
)


class CronMigrationAdapter:
    """MigrationUnitAdapter for cron jobs — plan side.

    Takes ``job_lookup``: the plan reads the job and reports what would ship.
    The target-side construction (``create_job``, ``target_session_key``) and the
    running-state probe belong to the transfer and land with it.
    """

    bundle_kind = "cron"
    bundle_version = 1

    def __init__(self, *, job_lookup: dict[str, CronJob] | None = None) -> None:
        self._jobs = job_lookup or {}

    # -- source side ----------------------------------------------------------

    def _job(self, unit_id: str) -> CronJob:
        try:
            return self._jobs[unit_id]
        except KeyError as exc:
            raise KeyError(f"no cron job {unit_id!r} on this crew") from exc

    async def describe(self, unit_id: str) -> dict:
        job = self._job(unit_id)
        return {"unit_id": unit_id, "kind": self.bundle_kind, "name": job.name}

    async def requirements(self, unit_id: str) -> list[P.HostRequirement]:
        job = self._job(unit_id)
        reqs: list[P.HostRequirement] = []
        if job.agent_id:
            # The target must already have this agent — refuse rather than let
            # it silently fall back to its default agent (Requirement 4.6).
            reqs.append(P.HostRequirement(kind="agent", identity=job.agent_id, severity="blocking"))
        if job.script:
            reqs.append(
                P.HostRequirement(kind="script_path", identity=job.script, severity="blocking")
            )
        if job.command:
            reqs.append(
                P.HostRequirement(kind="command_policy", identity=job.command, severity="blocking")
            )
        return reqs

    async def serialize(self, unit_id: str) -> dict:
        raw = dataclasses.asdict(self._job(unit_id))
        return P.allow_list_serialize(raw, allowed=CRON_SHIP_FIELDS)
