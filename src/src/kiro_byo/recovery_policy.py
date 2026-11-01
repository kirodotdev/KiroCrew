"""Provider-neutral retry/resume/replan policy.

The policy is deliberately conservative: automatic retries are allowed only
before visible/model/tool state exists, while durable resume is allowed only
when the previous attempt has no unresolved side-effect boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .durable_runs import DurableRunStore


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
        reason = DurableRunStore.recovery_rejection(record, strategy=strategy)
        if reason:
            return RecoveryDecision("reject", reason, False)
        strategy = str(strategy or "resume").lower()
        return RecoveryDecision(
            "replan" if strategy == "replan" else "resume",
            f"durable {strategy} allowed after ledger admission checks",
            True,
        )
