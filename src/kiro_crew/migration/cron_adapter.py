"""Cron migration adapter (slice 2 of issue #7577).

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
import time
from typing import Any, Callable

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
)


class CronMigrationAdapter:
    """MigrationUnitAdapter for cron jobs.

    Source-side construction passes ``job_lookup`` (and optionally
    ``is_running``); target-side construction passes ``create_job`` and
    ``target_session_key``. One class serves both ends because the protocol
    only ever calls the source methods on the source and ``materialize`` on
    the target.
    """

    bundle_kind = "cron"
    bundle_version = 1

    def __init__(
        self,
        *,
        job_lookup: dict[str, CronJob] | None = None,
        is_running: Callable[[str], bool] | None = None,
        create_job: Callable[[dict], str] | None = None,
        target_session_key: str = "",
        registry=None,
    ) -> None:
        self._jobs = job_lookup or {}
        self._is_running = is_running or (lambda _jid: False)
        self._create_job = create_job
        self._target_session_key = target_session_key
        # Optional durable tombstone registry (Req 7.3). The in-memory dict below
        # is scoped to this adapter instance, so it cannot answer "where did this
        # job go?" for the Schedule page or `kirocrew cron list`, which never
        # hold the adapter that performed the move. Optional so existing call
        # sites keep working unchanged.
        self._registry = registry
        self._tombstones: dict[str, P.Tombstone] = {}

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

    async def quiesce(self, unit_id: str) -> P.QuiesceToken:
        if self._is_running(unit_id):
            raise P.MidRunError(f"cron job {unit_id!r} has a run in flight")
        job = self._job(unit_id)
        job.enabled = False  # mark non-executing on the source
        return P.QuiesceToken(unit_id=unit_id, token="cron-quiesced")

    async def unquiesce(self, unit_id: str, token: P.QuiesceToken) -> None:
        self._job(unit_id).enabled = True

    async def serialize(self, unit_id: str) -> dict:
        raw = dataclasses.asdict(self._job(unit_id))
        return P.allow_list_serialize(raw, allowed=CRON_SHIP_FIELDS)

    async def tombstone(self, unit_id: str, target: P.CrewRef, remote_id: str) -> None:
        job = self._job(unit_id) if unit_id in self._jobs else None
        if job is not None:
            job.enabled = False  # retained, non-executing (Requirement 2.8)
        self._tombstones[unit_id] = P.Tombstone(
            unit_kind=self.bundle_kind,
            target_crew=target,
            remote_unit_id=remote_id,
            migrated_ts=time.time(),
        )
        if self._registry is not None:
            # Durable + queryable, so the surface that listed this job before the
            # move can still say where it went (Req 7.3).
            self._registry.record(self.bundle_kind, unit_id, self._tombstones[unit_id])

    def tombstone_of(self, unit_id: str) -> P.Tombstone:
        return self._tombstones[unit_id]

    def should_fire(self, unit_id: str, *, now_ts: float) -> bool:
        """Scheduler predicate: a tombstoned or disabled source never fires.

        This is the double-fire guard (Requirement 4.7): after migration the
        source is non-executing regardless of how far past the next due
        instant the clock advances.
        """
        if unit_id in self._tombstones:
            return False
        job = self._jobs.get(unit_id)
        return bool(job and job.enabled and not job.user_paused)

    # -- target side ----------------------------------------------------------

    async def materialize(self, payload: dict) -> str:
        """Re-create the job on the target, re-binding the owning scope.

        ``user_paused`` and the job's own ``timezone`` are preserved; the
        target computes the next fire from the schedule + that timezone, and
        allocates its own id. ``session_key`` is re-bound to the target.
        """
        if self._create_job is None:
            raise RuntimeError("materialize requires a target-side create_job")
        fields = dict(payload)  # already allow-listed at source
        fields["session_key"] = self._target_session_key  # re-bind scope
        new_id = self._create_job(fields)
        if self._registry is not None:
            # This unit is live HERE now. If it had previously been migrated away
            # from this crew — the move-back case (Req 7.4) — a leftover tombstone
            # would tell the user their running job had moved elsewhere.
            self._registry.clear(self.bundle_kind, new_id)
            source_id = payload.get("id")
            if source_id and source_id != new_id:
                self._registry.clear(self.bundle_kind, source_id)
        return new_id
