"""Provider-neutral retry/resume/replan policy.

The policy is deliberately conservative: automatic retries are allowed only
before visible/model/tool state exists, while durable resume is allowed only
when the previous attempt has no unresolved side-effect boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str
    safe: bool = False


class RecoveryPolicy:
    """Small deterministic recovery policy shared by live turns and durable runs."""

    _RETRYABLE_CATEGORIES = frozenset({"transport", "rate_limit", "timeout", "overloaded"})
    _NON_RESUMABLE_STATES = frozenset({"unknown_side_effect", "manual_verification_required", "unsafe"})

    @classmethod
    def live_provider_failure(
        cls,
        exc: BaseException,
        *,
        pristine: bool,
        side_effect_risk: bool,
        same_model_retries: int,
        has_fallback: bool,
        can_replan: bool,
    ) -> RecoveryDecision:
        if side_effect_risk:
            return RecoveryDecision("fail_closed", "possible tool side effects already exist", False)
        category = str(getattr(exc, "category", "provider") or "provider")
        retryable = bool(getattr(exc, "retryable", False))
        if pristine and retryable and same_model_retries < 1:
            return RecoveryDecision("retry_same_model", f"retryable {category} failure before visible state", True)
        if pristine and has_fallback:
            return RecoveryDecision("fallback", "clean turn can fail over to a prepared route", True)
        if pristine and can_replan and (retryable or category in cls._RETRYABLE_CATEGORIES):
            return RecoveryDecision("replan", "clean turn can be freshly re-routed", True)
        if retryable or category in cls._RETRYABLE_CATEGORIES:
            return RecoveryDecision("pause_resume", f"{category} failure is safe for explicit durable resume", True)
        return RecoveryDecision("fail", f"non-retryable {category} failure", False)

    @classmethod
    def durable_run(cls, record: dict[str, Any] | None, *, strategy: str = "resume") -> RecoveryDecision:
        if not isinstance(record, dict):
            return RecoveryDecision("reject", "run record is unavailable", False)
        state = str(record.get("recovery_state") or "clean")
        if state in cls._NON_RESUMABLE_STATES:
            return RecoveryDecision("reject", f"run requires manual side-effect verification ({state})", False)
        if str(record.get("status") or "") == "completed":
            return RecoveryDecision("reject", "run already completed", False)
        strategy = str(strategy or "resume").lower()
        if strategy not in {"resume", "retry", "replan"}:
            return RecoveryDecision("reject", f"unsupported recovery strategy: {strategy}", False)
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        try:
            user_event_seq = int(metadata.get("user_event_seq", -1))
        except (TypeError, ValueError):
            user_event_seq = -1
        if user_event_seq < 0:
            return RecoveryDecision("reject", "run has no persisted user-message anchor", False)

        selection = str(record.get("selection") or "")
        is_type_selection = selection.startswith("kiro-byo-type:")
        if strategy == "replan":
            if not is_type_selection:
                return RecoveryDecision("reject", "concrete model selections cannot be re-routed; use retry/resume", False)
            if state in {"pending_restart", "retryable", "clean", "paused", "replan_required", "retrying", "resuming", "replanning"}:
                return RecoveryDecision("replan", f"fresh Type routing allowed from {state}", True)
            return RecoveryDecision("reject", f"run recovery state is not replannable: {state}", False)

        if state == "replan_required":
            return RecoveryDecision("reject", "failure requires a fresh Type replan rather than same-route replay", False)
        if state in {"pending_restart", "retryable", "clean", "paused", "retrying", "resuming", "replanning"}:
            return RecoveryDecision("resume", f"durable recovery allowed from {state}", True)
        return RecoveryDecision("reject", f"run recovery state is not resumable: {state}", False)

