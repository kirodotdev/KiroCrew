"""Coordinator-first reconciliation after a gateway restart."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from kiro_crew import platform_compat
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

from .delivery import OutboxDeliveryAdapter
from .legacy import LegacyImportReport, LegacyRunImporter
from .models import (
    CoordinatorDecision,
    DeliveryState,
    ObservedState,
    OwnerLease,
    RunCompletion,
    RunCoordinator,
    RunOutcome,
    RunRecord,
)

logger = logging.getLogger(__name__)

_RECOVERY_LEASE_SECONDS = 90.0
_RECOVERY_BATCH_SIZE = 128
_DELIVERY_BATCH_SIZE = 256
_EVENT_TYPE = "subagent_completion"


def _audit_orphan_termination(run_id: str, pid: int) -> None:
    """Durably authorize an orphan kill before the process side effect."""
    sel().log_tool_invocation(
        session_key=f"subagent:{run_id}",
        source="subagent",
        tool_name="orphan_reconcile_kill",
        outcome="kill_authorized",
        metadata={"subagent_id": run_id, "pid": pid},
        critical=True,
    )


@dataclass(frozen=True)
class RecoveryReport:
    imported: int = 0
    existing: int = 0
    corrupt: int = 0
    interrupted: int = 0
    terminated: int = 0
    delivered: int = 0


def _redact(value: str) -> str:
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


class RunRecovery:
    """Converge durable terminal delivery without replaying execution commands."""

    def __init__(
        self,
        coordinator: RunCoordinator,
        delivery: OutboxDeliveryAdapter,
        *,
        owner_id: str | None = None,
        clock: Callable[[], float] = time.time,
        terminate_process: Callable[[int], object] | None = None,
        process_identity: Callable[[int], str | None] = platform_compat.process_identity_token,
        process_alive: Callable[[int], bool] = platform_compat.pid_exists,
        lease_seconds: float = _RECOVERY_LEASE_SECONDS,
    ) -> None:
        self._coordinator = coordinator
        self._delivery = delivery
        self._owner_id = owner_id or f"recovery:{uuid.uuid4().hex}"
        self._clock = clock
        self._terminate_process = terminate_process
        self._process_identity = process_identity
        self._process_alive = process_alive
        self._lease_seconds = max(1.0, lease_seconds)

    def completion_for(
        self,
        run: RunRecord,
        *,
        outcome: RunOutcome = RunOutcome.INTERRUPTED,
    ) -> RunCompletion:
        error = "interrupted by gateway restart" if outcome is RunOutcome.INTERRUPTED else run.error
        task = _redact(run.task)[:1000]
        safe_error = _redact(error)[:2000]
        payload = json.dumps(
            {
                "id": run.run_id,
                "parent_session_key": run.parent_session,
                "agent": _redact(run.agent),
                "task": task,
                "outcome": outcome.value,
                "error": safe_error,
                "result_path": run.result_path,
                "result_summary": "",
                "result_truncated": bool(run.result_path),
                "user_stopped": outcome is RunOutcome.STOPPED,
                "batch_id": "",
                "batch_total": 0,
                "elapsed": max(0.0, self._clock() - run.created_at),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return RunCompletion(
            run_id=run.run_id,
            outcome=outcome,
            result_path=run.result_path,
            error=safe_error,
            event_type=_EVENT_TYPE,
            destination=run.parent_session,
            payload_json=payload,
            terminal_at=self._clock(),
        )

    async def _verified_live_process(self, run: RunRecord) -> tuple[tuple[int, str] | None, bool]:
        """Return a coordinator-pinned child and whether its identity is unverified."""

        if not run.process_owned or run.process_id <= 1:
            return None, False
        if not await asyncio.to_thread(self._process_alive, run.process_id):
            return None, False
        if not run.process_start_id:
            return None, True
        current = await asyncio.to_thread(self._process_identity, run.process_id)
        if current is None:
            return None, True
        if current != run.process_start_id:
            durable_pair = platform_compat.is_durable_process_identity_token(
                run.process_start_id
            ) and platform_compat.is_durable_process_identity_token(current)
            if not durable_pair and not platform_compat.IS_WINDOWS:
                # Older Linux rows lack a boot id, while older BSD rows use a
                # locale/TZ-formatted value. A mismatch cannot prove replacement.
                return None, True
            # The recorded child is gone and this PID now belongs to another process.
            # Never signal the replacement, but do let the original run converge to
            # interrupted instead of deferring recovery forever.
            return None, False
        return (run.process_id, run.process_start_id), False

    async def reconcile(
        self,
        *,
        importer: LegacyRunImporter | None = None,
        exclude_run_ids: frozenset[str] = frozenset(),
    ) -> RecoveryReport:
        """Import once, fence expired runs, interrupt them, then drain delivery."""

        imported = LegacyImportReport()
        if importer is not None:
            imported = await importer.import_all(exclude_run_ids=exclude_run_ids)
        interrupted = 0
        terminated = 0
        while True:
            now = self._clock()
            claims = await self._coordinator.claim_recovery(
                OwnerLease(self._owner_id, now + self._lease_seconds),
                _RECOVERY_BATCH_SIZE,
                exclude_run_ids,
            )
            if not claims:
                break
            for claim in claims:
                terminal_cleanup = claim.run.observed_state is ObservedState.TERMINAL
                process, live_unverified = await self._verified_live_process(claim.run)
                if live_unverified:
                    logger.warning(
                        "Live orphan process identity is unverified for run %s; retrying later",
                        claim.run.run_id,
                    )
                    continue
                if process is not None:
                    try:
                        await asyncio.to_thread(
                            _audit_orphan_termination,
                            claim.run.run_id,
                            process[0],
                        )
                    except Exception:
                        logger.warning(
                            "SEL audit blocked orphan termination for run %s",
                            claim.run.run_id,
                            exc_info=True,
                        )
                        continue
                    try:
                        if self._terminate_process is None:
                            termination_result: object = await asyncio.to_thread(
                                platform_compat.kill_process_tree_pinned,
                                process[0],
                                process[1],
                                platform_compat.SIGKILL,
                            )
                        else:
                            termination_result = await asyncio.to_thread(
                                self._terminate_process,
                                process[0],
                            )
                        if termination_result is False:
                            logger.warning(
                                "Verified orphan identity changed before termination for run %s",
                                claim.run.run_id,
                            )
                            continue
                        terminated += 1
                    except ProcessLookupError:
                        pass
                    except (OSError, ValueError):
                        logger.warning(
                            "Failed to terminate verified orphan process for run %s",
                            claim.run.run_id,
                        )
                        continue
                if terminal_cleanup:
                    await self._coordinator.clear_recovered_process(
                        claim.run.run_id,
                        claim.fence,
                        claim.run.version,
                    )
                    continue
                result = await self._coordinator.complete(
                    self.completion_for(claim.run),
                    claim.fence,
                    claim.run.version,
                )
                if result.decision is not CoordinatorDecision.REJECTED:
                    interrupted += 1
                    if claim.run.process_owned and result.value is not None:
                        await self._coordinator.clear_recovered_process(
                            claim.run.run_id,
                            claim.fence,
                            result.value.run_version,
                        )
            if len(claims) < _RECOVERY_BATCH_SIZE:
                break

        attempts = await self._delivery.drain_once(limit=_DELIVERY_BATCH_SIZE)
        delivered = sum(1 for attempt in attempts if attempt.status is DeliveryState.DELIVERED)
        return RecoveryReport(
            imported=imported.imported,
            existing=imported.existing,
            corrupt=imported.corrupt,
            interrupted=interrupted,
            terminated=terminated,
            delivered=delivered,
        )
