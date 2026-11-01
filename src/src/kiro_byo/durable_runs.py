"""Durable run/checkpoint state for long-running BYO execution.

The store is intentionally small and provider-neutral.  A *Run* is a durable
business execution owned by a session, a *Step* is a restartable unit of work,
and an *Attempt* is one concrete execution of that step.  Checkpoints only
store control-plane facts and references into the session event log; they do
not duplicate the conversation transcript or credentials.

Hotfix6 is the persistence foundation.  Automatic background scheduling and
cross-process auto-resume are deliberately not implemented yet; callers can
inspect pending runs and decide when it is safe to resume.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .paths import app_home
from .state_io import _atomic_json_write, _file_transaction_lock

_RUN_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_STEP_TERMINAL = frozenset({"completed", "failed", "cancelled", "skipped", "interrupted"})
_ATTEMPT_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


def _now() -> float:
    return time.time()


def _safe_id(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum() or ch in "-_.")[:160] or "invalid"


class DurableRunStore:
    """Atomic JSON run ledger shared by the ACP runtime and diagnostics UI."""

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root: Path | None = None, *, max_runs: int = 200) -> None:
        self.root = root or (app_home() / "data" / "runs")
        self.max_runs = max(16, min(int(max_runs), 2000))
        key = str(self.root.resolve()) if self.root.exists() else str(self.root)
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _path(self, run_id: str) -> Path:
        return self.root / f"{_safe_id(run_id)}.json"

    def _read(self, run_id: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._path(run_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _write(self, record: Mapping[str, Any]) -> None:
        run_id = str(record.get("run_id") or "")
        if not run_id:
            raise ValueError("run_id is required")
        _atomic_json_write(self._path(run_id), record)

    @staticmethod
    def public_record(record: Mapping[str, Any]) -> dict[str, Any]:
        """Return a small, allowlisted diagnostic view, never a ledger snapshot.

        Stored steps, tool receipts, checkpoints and arbitrary metadata may
        contain user input, tool arguments or credentials. Recovery decisions
        must always use the private canonical record, not this projection.
        """
        keys = (
            "schema_version", "revision", "run_id", "session_id", "plan_id",
            "mode", "selection", "status", "created_at", "updated_at",
            "completed_at", "session_event_seq", "recovery_state",
            "side_effect_seen", "retry_count", "resume_count", "replan_count",
            "last_recovery_action",
        )
        safe = {key: record[key] for key in keys if key in record}
        metadata = record.get("metadata")
        # The Records UI displays this identifier. Do not publish any other
        # arbitrary metadata: paths, prompts and credential-like data can live there.
        safe["metadata"] = {
            "model_id": str(metadata.get("model_id") or "")[:256]
        } if isinstance(metadata, Mapping) else {}
        lease = record.get("lease")
        if isinstance(lease, Mapping):
            safe["lease"] = {
                "generation": int(lease.get("generation") or 0),
                "expires_at": float(lease.get("expires_at") or 0.0),
                "active": DurableRunStore.is_lease_active(record),
            }
        return safe

    @staticmethod
    def is_lease_active(record: Mapping[str, Any], *, now: float | None = None) -> bool:
        """An active process may still own this run: never infer a crash."""
        lease = record.get("lease")
        if not isinstance(lease, Mapping):
            return False
        return float(lease.get("expires_at") or 0.0) > (_now() if now is None else now)

    @staticmethod
    def _authorize(record: Mapping[str, Any], *, owner_token: str, recovery_write: bool = False) -> None:
        lease = record.get("lease")
        if not isinstance(lease, Mapping):
            return  # Historical unleased ledgers remain compatible.
        if recovery_write:
            if DurableRunStore.is_lease_active(record):
                raise ValueError("active run lease prevents restart reconciliation")
        elif not owner_token or str(lease.get("owner_token") or "") != owner_token or not DurableRunStore.is_lease_active(record):
            raise ValueError("run lease missing, expired, or owned by another process")

    def renew_lease(self, run_id: str, *, owner_token: str, lease_ttl: float = 120.0) -> dict[str, Any] | None:
        """Renew under the same advisory lock as all other ledger mutations."""
        ttl = max(1.0, min(float(lease_ttl), 3600.0))
        def mutate(record: dict[str, Any]) -> None:
            if record.get("status") != "running" or not isinstance(record.get("lease"), dict):
                raise ValueError("cannot renew lease on non-running or legacy run")
            record["lease"]["expires_at"] = _now() + ttl
            record["lease"]["renewed_at"] = _now()
        return self._mutate(run_id, mutate, owner_token=owner_token)

    def _mutate(self, run_id: str, fn, *, owner_token: str = "", recovery_write: bool = False) -> dict[str, Any] | None:
        with self._lock, _file_transaction_lock(self._path(run_id)):
            record = self._read(run_id)
            if record is None:
                return None
            self._authorize(record, owner_token=owner_token, recovery_write=recovery_write)
            fn(record)
            record["revision"] = int(record.get("revision") or 0) + 1
            record["updated_at"] = _now()
            self._write(record)
            return record

    def create_run(
        self,
        session_id: str,
        *,
        plan_id: str = "",
        mode: str = "",
        selection: str = "",
        session_event_seq: int = -1,
        metadata: Mapping[str, Any] | None = None,
        lease_owner: str = "", lease_ttl: float = 120.0,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        now = _now()
        record: dict[str, Any] = {
            "schema_version": 1,
            "revision": 0,
            "run_id": run_id,
            "session_id": str(session_id or ""),
            "plan_id": str(plan_id or ""),
            "mode": str(mode or ""),
            "selection": str(selection or ""),
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "completed_at": 0.0,
            "session_event_seq": int(session_event_seq),
            "recovery_state": "clean",
            # Never infer safety only from the bounded checkpoint window. A
            # mutating tool admitted early in a long run may have its original
            # checkpoint rotated out before a process restart.
            "side_effect_seen": False,
            "retry_count": 0,
            "resume_count": 0,
            "replan_count": 0,
            "last_recovery_action": "",
            "metadata": dict(metadata or {}),
            "steps": [],
            "checkpoints": [],
            "tool_batches": {},  # Monotonic, metadata-only tool admission receipts.
        }
        if lease_owner:
            record["lease"] = {
                "owner_token": str(lease_owner), "generation": 1,
                "expires_at": now + max(1.0, min(float(lease_ttl), 3600.0)),
                "renewed_at": now,
            }
        with self._lock, _file_transaction_lock(self._path(run_id)):
            self._write(record)
        with self._lock:
            self._prune_locked()
        return record

    def start_step(self, run_id: str, *, kind: str, objective: str = "", owner_token: str = "") -> dict[str, Any] | None:
        step_id = uuid.uuid4().hex
        now = _now()
        out: dict[str, Any] = {}
        def mutate(record: dict[str, Any]) -> None:
            if record.get("status") != "running":
                raise ValueError("cannot start a step in a terminal or inactive run")
            step = {
                "step_id": step_id,
                "kind": str(kind or "turn"),
                "objective": str(objective or "")[:1000],
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "completed_at": 0.0,
                "attempts": [],
            }
            record.setdefault("steps", []).append(step)
            out.update(step)
        return out if self._mutate(run_id, mutate, owner_token=owner_token) is not None else None

    def start_attempt(
        self, run_id: str, step_id: str, *, provider_id: str = "", model_id: str = "", owner_token: str = ""
    ) -> dict[str, Any] | None:
        attempt_id = uuid.uuid4().hex
        now = _now()
        out: dict[str, Any] = {}
        def mutate(record: dict[str, Any]) -> None:
            if record.get("status") != "running":
                raise ValueError("cannot start an attempt in a terminal or inactive run")
            for step in record.get("steps", []):
                if step.get("step_id") != step_id:
                    continue
                if step.get("status") != "running":
                    raise ValueError("attempt requires a running step")
                attempt = {
                    "attempt_id": attempt_id,
                    "status": "running",
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": 0.0,
                    "provider_id": str(provider_id or ""),
                    "model_id": str(model_id or ""),
                    "error_kind": "",
                    "error_category": "",
                }
                if any(row.get("status") == "running" for row in step.get("attempts", [])):
                    raise ValueError("running step already owns an active attempt")
                step.setdefault("attempts", []).append(attempt)
                step["updated_at"] = now
                out.update(attempt)
                return
            raise ValueError("unknown step for durable attempt")
        return out if self._mutate(run_id, mutate, owner_token=owner_token) is not None and out else None

    def checkpoint(
        self,
        run_id: str,
        *,
        step_id: str = "",
        attempt_id: str = "",
        name: str,
        session_event_seq: int = -1,
        agent_tasks: list[Mapping[str, Any]] | None = None,
        data: Mapping[str, Any] | None = None,
        owner_token: str = "",
    ) -> dict[str, Any] | None:
        now = _now()
        checkpoint_id = uuid.uuid4().hex
        out: dict[str, Any] = {}
        def mutate(record: dict[str, Any]) -> None:
            cp = {
                "checkpoint_id": checkpoint_id,
                "name": str(name or "checkpoint"),
                "created_at": now,
                "step_id": str(step_id or ""),
                "attempt_id": str(attempt_id or ""),
                "session_event_seq": int(session_event_seq),
                "agents": [dict(row) for row in (agent_tasks or [])],
                "data": dict(data or {}),
            }
            if record.get("status") in {"completed", "cancelled"}:
                raise ValueError("cannot checkpoint terminal durable run")
            # Stage 8: a real ACP tool batch records a deterministic opaque ID
            # before dispatch. Check and mutate this receipt in the *same*
            # cross-process transaction as the checkpoint. Historical callers
            # without a batch_id retain their existing checkpoint semantics.
            batch_id = str(cp["data"].get("batch_id") or "")
            if batch_id and cp["name"] in {"tool_batch_admitted", "tool_batch_completed"}:
                if len(batch_id) > 128 or not all(ch.isalnum() or ch in "-_" for ch in batch_id):
                    raise ValueError("invalid opaque tool batch ID")
                if record.get("status") != "running":
                    raise ValueError("tool batch requires a running run")
                steps = record.get("steps") or []
                step = next((row for row in steps if row.get("step_id") == step_id), None)
                if not isinstance(step, dict) or step.get("status") != "running":
                    raise ValueError("tool batch requires a running step")
                attempt = next((row for row in step.get("attempts", []) if row.get("attempt_id") == attempt_id), None)
                if not isinstance(attempt, dict) or attempt.get("status") != "running":
                    raise ValueError("tool batch requires a running attempt")
                batches = record.setdefault("tool_batches", {})
                if not isinstance(batches, dict):
                    raise ValueError("invalid durable tool batch ledger")
                prior = batches.get(batch_id)
                if cp["name"] == "tool_batch_admitted":
                    if prior is not None:
                        raise ValueError("duplicate durable tool batch admission")
                    if len(batches) >= 2048:
                        raise ValueError("durable tool batch ledger capacity exhausted")
                    count = int(cp["data"].get("tool_count") or 0)
                    if count < 1 or count > 256:
                        raise ValueError("invalid durable tool batch count")
                    batches[batch_id] = {
                        "step_id": step_id, "attempt_id": attempt_id,
                        "status": "admitted", "tool_count": count,
                        "side_effect_risk": bool(cp["data"].get("side_effect_risk")),
                        "admitted_at": now, "completed_at": 0.0,
                    }
                else:
                    if not isinstance(prior, dict):
                        raise ValueError("tool batch was not admitted")
                    if prior.get("step_id") != step_id or prior.get("attempt_id") != attempt_id:
                        raise ValueError("tool batch completion has mismatched step or attempt")
                    if prior.get("status") != "admitted":
                        raise ValueError("tool batch already completed")
                    prior["status"] = "completed"
                    prior["completed_at"] = now
            record.setdefault("checkpoints", []).append(cp)
            record["checkpoints"] = record["checkpoints"][-64:]
            if bool(cp["data"].get("side_effect_risk")):
                record["side_effect_seen"] = True
            record["session_event_seq"] = int(session_event_seq)
            out.update(cp)
        return out if self._mutate(run_id, mutate, owner_token=owner_token) is not None else None

    def finish_attempt(
        self,
        run_id: str,
        step_id: str,
        attempt_id: str,
        *,
        status: str,
        provider_id: str = "",
        model_id: str = "",
        error_kind: str = "",
        error_category: str = "",
        owner_token: str = "",
    ) -> dict[str, Any] | None:
        if status not in _ATTEMPT_TERMINAL:
            raise ValueError(f"invalid attempt terminal status: {status}")
        now = _now()
        def mutate(record: dict[str, Any]) -> None:
            if record.get("status") in {"completed", "cancelled"}:
                raise ValueError("cannot finish attempt of terminal durable run")
            for step in record.get("steps", []):
                if step.get("step_id") != step_id:
                    continue
                if step.get("status") != "running":
                    raise ValueError("durable step is not running; late completion rejected")
                for attempt in step.get("attempts", []):
                    if attempt.get("attempt_id") != attempt_id:
                        continue
                    if attempt.get("status") != "running":
                        raise ValueError("durable attempt is not running; late completion rejected")
                    attempt.update({
                        "status": status, "updated_at": now, "completed_at": now,
                        "provider_id": str(provider_id or attempt.get("provider_id") or ""),
                        "model_id": str(model_id or attempt.get("model_id") or ""),
                        "error_kind": str(error_kind or ""),
                        "error_category": str(error_category or ""),
                    })
                    step["updated_at"] = now
                    return
                raise ValueError("unknown durable attempt ID")
            raise ValueError("unknown durable step ID")
        return self._mutate(run_id, mutate, owner_token=owner_token)

    def finish_step(self, run_id: str, step_id: str, *, status: str, owner_token: str = "") -> dict[str, Any] | None:
        if status not in _STEP_TERMINAL:
            raise ValueError(f"invalid step terminal status: {status}")
        now = _now()
        def mutate(record: dict[str, Any]) -> None:
            if record.get("status") in {"completed", "cancelled"}:
                raise ValueError("cannot finish step of terminal durable run")
            for step in record.get("steps", []):
                if step.get("step_id") == step_id:
                    if step.get("status") != "running":
                        raise ValueError("durable step is not running; late completion rejected")
                    step.update({"status": status, "updated_at": now, "completed_at": now})
                    return
            raise ValueError("unknown durable step ID")
        return self._mutate(run_id, mutate, owner_token=owner_token)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        recovery_state: str = "clean",
        session_event_seq: int = -1,
        owner_token: str = "",
    ) -> dict[str, Any] | None:
        if status not in _RUN_TERMINAL:
            raise ValueError(f"invalid run terminal status: {status}")
        now = _now()
        def mutate(record: dict[str, Any]) -> None:
            if record.get("status") in _RUN_TERMINAL:
                raise ValueError("cannot rewrite terminal durable run")
            # A successful turn cannot seal a Run if an admitted tool batch
            # never got its completion receipt. This is evidence of unknown
            # execution status, not a completed durable transaction.
            if status == "completed" and any(
                isinstance(batch, Mapping) and batch.get("status") == "admitted"
                for batch in (record.get("tool_batches") or {}).values()
            ):
                raise ValueError("unresolved tool batch receipt prevents completed run")
            record.update({
                "status": status,
                "completed_at": now,
                "recovery_state": str(recovery_state or "clean"),
                "session_event_seq": int(session_event_seq),
            })
            if isinstance(record.get("lease"), dict):
                # Releasing at a terminal state permits explicit user recovery
                # of failed attempts without waiting for the TTL.
                record["lease"]["expires_at"] = 0.0
        return self._mutate(run_id, mutate, owner_token=owner_token)

    def mark_recovery(self, run_id: str, state: str) -> dict[str, Any] | None:
        def mutate(record: dict[str, Any]) -> None:
            if record.get("status") in {"completed", "cancelled"}:
                raise ValueError("cannot change recovery state of terminal durable run")
            record["recovery_state"] = str(state or "pending")
        return self._mutate(run_id, mutate, recovery_write=True)

    @staticmethod
    def recovery_rejection(record: Mapping[str, Any], *, strategy: str = "resume") -> str:
        """Single state-machine guard shared by ACP policy and ledger admission.

        Caller still verifies session ownership and the user event's contents.
        """
        strategy = str(strategy or "resume").lower()
        if strategy not in {"retry", "resume", "replan"}:
            return f"unsupported recovery strategy: {strategy}"
        status = str(record.get("status") or "")
        if status == "running" and DurableRunStore.is_lease_active(record):
            return "active run lease: another harness may still be executing"
        state = str(record.get("recovery_state") or "clean")
        if status == "completed":
            return "completed durable run cannot be reopened"
        if status == "cancelled":
            return "cancelled durable run cannot be reopened; create a new run"
        if status not in {"running", "failed"}:
            return f"durable run has invalid status: {status}"
        if DurableRunStore.restart_recovery_state(record) == "unknown_side_effect" or state in {
            "unknown_side_effect", "manual_verification_required", "unsafe",
        }:
            return "durable run has unverified tool side effects (side-effect risk)"
        if status == "running" and state in {"clean", "retrying", "resuming", "replanning"}:
            return "durable run is already active; restart reconciliation is required"
        if state not in {"pending_restart", "retryable", "paused", "replan_required", "clean"}:
            return f"durable run cannot be reopened from state {state}"
        if state == "replan_required" and strategy != "replan":
            return "durable run requires replan"
        if strategy == "replan" and not str(record.get("selection") or "").startswith("kiro-byo-type:"):
            return "concrete model selections cannot be re-routed; use retry/resume"
        metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
        try:
            anchor = int(metadata.get("user_event_seq", -1))
        except (TypeError, ValueError):
            anchor = -1
        if anchor < 0:
            return "run has no persisted user-message anchor"
        return ""

    def prepare_resume(
        self, run_id: str, *, session_id: str, strategy: str = "resume",
        owner_token: str = "", lease_ttl: float = 120.0,
    ) -> dict[str, Any] | None:
        """Re-open a recoverable run and close any orphaned live attempt.

        This method never decides whether resume is safe; callers must apply the
        recovery policy first. It only makes the durable ledger internally
        consistent before a new Step/Attempt is created.
        """
        now = _now()
        strategy = str(strategy or "resume").lower()
        if strategy not in {"retry", "resume", "replan"}:
            raise ValueError(f"unsupported recovery strategy: {strategy}")

        def mutate(record: dict[str, Any]) -> None:
            if str(record.get("session_id") or "") != str(session_id or ""):
                raise ValueError("durable run belongs to a different session")
            # The public ACP handler evaluates RecoveryPolicy before taking the
            # session turn lock. Enforce the final admission decision under the
            # ledger lock as well: two competing callers may both have observed
            # the *old* recoverable state. A second admission must not create a
            # second Step/Attempt for one logical recovery.
            refusal = self.recovery_rejection(record, strategy=strategy)
            if refusal:
                raise ValueError(refusal)
            for step in record.get("steps", []):
                if str(step.get("status") or "") != "running":
                    continue
                for attempt in step.get("attempts", []):
                    if str(attempt.get("status") or "") == "running":
                        attempt.update({
                            "status": "interrupted",
                            "updated_at": now,
                            "completed_at": now,
                            "error_kind": str(attempt.get("error_kind") or "ProcessRestart"),
                            "error_category": str(attempt.get("error_category") or "recovery"),
                        })
                step.update({"status": "interrupted", "updated_at": now, "completed_at": now})
            record["status"] = "running"
            record["completed_at"] = 0.0
            record["recovery_state"] = (
                "replanning" if strategy == "replan" else "retrying" if strategy == "retry" else "resuming"
            )
            key = {"retry": "retry_count", "resume": "resume_count", "replan": "replan_count"}[strategy]
            record[key] = int(record.get(key) or 0) + 1
            record["last_recovery_action"] = strategy
            previous = record.get("lease")
            if isinstance(previous, Mapping) and not owner_token:
                raise ValueError("resume of leased run requires a new run lease owner")
            if owner_token:
                record["lease"] = {
                    "owner_token": str(owner_token),
                    "generation": int(previous.get("generation") or 0) + 1 if isinstance(previous, Mapping) else 1,
                    "expires_at": _now() + max(1.0, min(float(lease_ttl), 3600.0)),
                    "renewed_at": _now(),
                }

        return self._mutate(run_id, mutate, recovery_write=True)

    @staticmethod
    def restart_recovery_state(record: Mapping[str, Any]) -> str:
        """Classify an interrupted process restart from durable checkpoints.

        The risk flag is monotonic, because only the latest 64 checkpoints are
        retained. Legacy runs without the flag and a saturated checkpoint
        window must fail closed: old side-effect evidence could have rotated
        out before this build was installed.
        """
        if bool(record.get("side_effect_seen")):
            return "unknown_side_effect"
        checkpoints = record.get("checkpoints") if isinstance(record, Mapping) else None
        if isinstance(checkpoints, list):
            for cp in reversed(checkpoints):
                if not isinstance(cp, Mapping):
                    continue
                data = cp.get("data") if isinstance(cp.get("data"), Mapping) else {}
                if bool(data.get("side_effect_risk")):
                    return "unknown_side_effect"
            if "side_effect_seen" not in record and len(checkpoints) >= 64:
                return "unknown_side_effect"
        return "pending_restart"

    def resumable(self, *, session_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Return runs that are not completed and are not blocked by side effects."""
        rows = self.list(limit=max(limit, self.max_runs), session_id=session_id, include_terminal=True)
        out: list[dict[str, Any]] = []
        for row in rows:
            if self.recovery_rejection(row, strategy="resume") and self.recovery_rejection(row, strategy="replan"):
                continue
            out.append(row)
            if len(out) >= max(1, min(int(limit), 500)):
                break
        return out

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(run_id)

    def list(self, *, limit: int = 50, session_id: str = "", include_terminal: bool = True) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            try:
                paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                return []
            out: list[dict[str, Any]] = []
            for path in paths:
                if len(out) >= limit:
                    break
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                if session_id and str(raw.get("session_id") or "") != session_id:
                    continue
                if not include_terminal and str(raw.get("status") or "") in _RUN_TERMINAL:
                    continue
                out.append(raw)
            return out

    def pending(self, *, session_id: str = "") -> list[dict[str, Any]]:
        # Do not hide protected pending records merely because their count
        # exceeds the completed-history retention target. The list API still
        # caps one response at 500 to avoid an unbounded ACP payload.
        return self.list(limit=500, session_id=session_id, include_terminal=False)

    @staticmethod
    def _safe_to_clear(record: Mapping[str, Any]) -> bool:
        """Only unambiguously finished runs can lose their recovery evidence."""
        return (
            record.get("status") == "completed"
            and str(record.get("recovery_state") or "") not in {
                "unknown_side_effect", "manual_verification_required", "unsafe"
            }
            and not DurableRunStore.is_lease_active(record)
        )

    def clear(self) -> int:
        """Clear completed safe history; never erase a resumable/unsafe Run."""
        removed = 0
        with self._lock:
            if not self.root.exists():
                return 0
            for path in self.root.glob("*.json"):
                try:
                    with _file_transaction_lock(path):
                        record = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(record, dict) or not self._safe_to_clear(record):
                            continue
                        path.unlink()
                        removed += 1
                except (OSError, ValueError, TypeError):
                    continue
        return removed

    def _prune_locked(self) -> None:
        """Prune completed history only, never active or recoverable runs.

        Keeping more than max_runs is intentional when the excess consists of
        unfinished, failed or cancelled runs: deleting recovery evidence to
        satisfy a history display limit can lose work or hide side effects.
        """
        try:
            paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        overflow = max(0, len(paths) - self.max_runs)
        if not overflow:
            return
        for path in reversed(paths):
            if overflow <= 0:
                break
            try:
                with _file_transaction_lock(path):
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(record, dict) or not self._safe_to_clear(record):
                        continue
                    path.unlink()
                    overflow -= 1
            except (OSError, ValueError):
                continue



class AgentStateStore:
    """Small cross-process projection for the management UI.

    The authoritative task object remains the in-process AgentRegistry.  This
    projection contains no transcript and no queued message text; it exists so
    the App can show task/model/lifecycle truth even though the ACP harness runs
    in a separate process.
    """

    def __init__(self, path: Path | None = None, *, max_tasks: int = 200) -> None:
        self.path = path or (app_home() / "data" / "agent_runtime" / "tasks.json")
        self.max_tasks = max(20, min(int(max_tasks), 2000))
        self._lock = threading.RLock()

    @staticmethod
    def _project(task: Mapping[str, Any]) -> dict[str, Any]:
        result = task.get("result") if isinstance(task.get("result"), Mapping) else None
        return {
            "task_id": str(task.get("task_id") or ""),
            "plan_id": str(task.get("plan_id") or ""),
            "session_id": str(task.get("session_id") or ""),
            "parent_task_id": str(task.get("parent_task_id") or ""),
            "name": str(task.get("name") or ""),
            "role": str(task.get("role") or ""),
            "objective": str(task.get("objective") or "")[:500],
            "backend": str(task.get("backend") or "model"),
            "backend_id": str(task.get("backend_id") or ""),
            "tool_policy": str(task.get("tool_policy") or ""),
            "workspace_policy": str(task.get("workspace_policy") or ""),
            "depth": int(task.get("depth") or 0),
            "workspace_id": str(task.get("workspace_id") or ""),
            "workspace_kind": str(task.get("workspace_kind") or ""),
            "workspace_state": str(task.get("workspace_state") or ""),
            "status": str(task.get("status") or ""),
            "provider_id": str(task.get("provider_id") or ""),
            "model_id": str(task.get("model_id") or ""),
            "created_at": float(task.get("created_at") or 0.0),
            "started_at": float(task.get("started_at") or 0.0),
            "completed_at": float(task.get("completed_at") or 0.0),
            "generation": int(task.get("generation") or 0),
            "continuation_count": int(task.get("continuation_count") or 0),
            "pending_messages": int(task.get("pending_messages") or 0),
            "cancel_requested": bool(task.get("cancel_requested")),
            "error": str(task.get("error") or "")[:1000],
            "duration_ms": float((result or {}).get("duration_ms") or 0.0),
            "result_status": str((result or {}).get("status") or ""),
        }

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "tasks": {}}
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise OSError("agent state ledger cannot be read safely") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), dict):
            raise OSError("agent state ledger has invalid shape")
        return raw

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json_write(self.path, state)

    def upsert(self, task: Mapping[str, Any], *, event: str = "") -> None:
        projected = self._project(task)
        task_id = projected["task_id"]
        if not task_id:
            return
        with self._lock, _file_transaction_lock(self.path):
            state = self._read()
            tasks = state.setdefault("tasks", {})
            projected["updated_at"] = _now()
            projected["event"] = str(event or "")
            projected["controls"] = {
                "cancel": projected["status"] in {"planned", "running", "paused"},
                "send": projected["status"] in {"running", "completed", "paused"},
                "resume": projected["status"] in {"completed", "failed", "cancelled", "paused"} and projected["role"] in {"worker", "writer", "specialist"},
                "transport": "ACP _kiro/agent/*",
            }
            tasks[task_id] = projected
            rows = sorted(tasks.values(), key=lambda row: float(row.get("updated_at") or 0.0), reverse=True)
            state["tasks"] = {str(row["task_id"]): row for row in rows[:self.max_tasks]}
            state["updated_at"] = _now()
            self._write(state)

    def list(self, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = list(self._read().get("tasks", {}).values())
        if session_id:
            rows = [row for row in rows if str(row.get("session_id") or "") == session_id]
        rows.sort(key=lambda row: float(row.get("updated_at") or 0.0), reverse=True)
        return rows[:limit]

    def mark_session_stale(self, session_id: str) -> int:
        changed = 0
        with self._lock, _file_transaction_lock(self.path):
            state = self._read()
            tasks = state.get("tasks", {})
            now = _now()
            for row in tasks.values():
                if str(row.get("session_id") or "") != str(session_id or ""):
                    continue
                if str(row.get("status") or "") not in {"planned", "running", "paused"}:
                    continue
                row["status"] = "paused"
                row["event"] = "process_restart"
                row["updated_at"] = now
                row["controls"] = {
                    "cancel": False, "send": False, "resume": False,
                    "transport": "ACP _kiro/agent/*",
                    "note": "in-process context was lost; durable child resume is not available yet",
                }
                changed += 1
            if changed:
                state["updated_at"] = now
                self._write(state)
        return changed

    def clear(self) -> int:
        with self._lock, _file_transaction_lock(self.path):
            count = len(self._read().get("tasks", {}))
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                return 0
            return count
