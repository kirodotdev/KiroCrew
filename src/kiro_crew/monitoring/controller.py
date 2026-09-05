"""Probe-first structured monitor controller and compact wake envelope."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol

from kiro_crew.dashboard.state import MONITOR_WAKE_PREFIX
from kiro_crew.monitoring.azure_devops_pull_request import AzureDevOpsPullRequestProvider
from kiro_crew.monitoring.bitbucket_pull_request import BitbucketPullRequestProvider
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider
from kiro_crew.monitoring.gitlab_merge_request import GitLabMergeRequestProvider
from kiro_crew.monitoring.models import (
    MAX_MONITOR_PROVIDER_CONCURRENCY,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorState,
    ProviderErrorKind,
)
from kiro_crew.monitoring.pull_request import PullRequestProbeResult, provider_error_result
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

MONITOR_WAKE_MAX_CHARS = 4096

logger = logging.getLogger(__name__)


class _Loop(Protocol):
    id: str
    monitor: MonitorState | None


class _Service(Protocol):
    async def stop_monitor_if_budget_exhausted(
        self,
        monitor_id: str,
        *,
        now: float,
    ) -> bool: ...

    async def apply_monitor_probe(
        self,
        monitor_id: str,
        result: PullRequestProbeResult,
        *,
        now: float,
        config_generation: int,
    ) -> MonitorDecision: ...

    async def record_monitor_dispatch_failure(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float | None = None,
    ) -> None: ...

    async def record_monitor_dispatch_busy(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float,
    ) -> None: ...

    async def record_monitor_dispatched(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float,
    ) -> None: ...

    async def record_monitor_completion_evidence_unavailable(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float,
    ) -> None: ...

    async def monitor_dispatch_is_authorized(
        self,
        monitor_id: str,
        fingerprint: str,
    ) -> bool: ...


class _Provider(Protocol):
    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> PullRequestProbeResult: ...


MonitorDispatcher = Callable[[Any, str], Awaitable[MonitorDispatchResult]]


class MonitorController:
    """Run typed probes and request a turn only for an accepted wake claim."""

    def __init__(
        self,
        service: _Service,
        dispatch: MonitorDispatcher,
        *,
        providers: Mapping[str, _Provider] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._service = service
        self._dispatch = dispatch
        self._clock = clock
        self._provider_gate = asyncio.Semaphore(MAX_MONITOR_PROVIDER_CONCURRENCY)
        self._providers = dict(providers or {})
        if not self._providers:
            self._providers = {
                "github_pull_request": GitHubPullRequestProvider(),
                "gitlab_merge_request": GitLabMergeRequestProvider(),
                "azure_devops_pull_request": AzureDevOpsPullRequestProvider(),
                "bitbucket_pull_request": BitbucketPullRequestProvider(),
            }

    async def tick(self, loop: _Loop, *, now: float) -> MonitorDecision:
        state = getattr(loop, "monitor", None)
        if state is None:
            raise ValueError("structured monitor state is required")
        if state.outcome is not None:
            if (
                state.wake_in_flight
                and state.completion_evidence_deadline > 0
                and now >= state.completion_evidence_deadline
            ):
                await self._service.record_monitor_completion_evidence_unavailable(
                    loop.id,
                    state.last_wake_fingerprint,
                    now=now,
                )
            return MonitorDecision.STOP_BLOCKED
        if state.wake_in_flight:
            deadline = state.completion_evidence_deadline
            if state.wake_delivery is MonitorDispatchResult.BUSY:
                if now < state.next_probe_at:
                    return MonitorDecision.NO_CHANGE
                if await self._service.stop_monitor_if_budget_exhausted(loop.id, now=now):
                    return MonitorDecision.STOP_BUDGET
                return await self._dispatch_claimed(loop, state, now=now)
            if (
                state.wake_delivery is MonitorDispatchResult.DISPATCHED
                and deadline > 0
                and now >= deadline
            ):
                await self._service.record_monitor_completion_evidence_unavailable(
                    loop.id,
                    state.last_wake_fingerprint,
                    now=now,
                )
            return MonitorDecision.NO_CHANGE
        if await self._service.stop_monitor_if_budget_exhausted(loop.id, now=now):
            return MonitorDecision.STOP_BUDGET
        config_generation = state.config_generation
        target = state.target
        previous_observation = deepcopy(state.last_observation)
        provider = self._providers.get(state.kind)
        if provider is None:
            result = provider_error_result(ProviderErrorKind.SETUP, "provider_unsupported")
            return await self._service.apply_monitor_probe(
                loop.id,
                result,
                now=now,
                config_generation=config_generation,
            )
        try:
            async with self._provider_gate:
                result = await asyncio.to_thread(
                    provider.probe,
                    target,
                    previous_observation=previous_observation,
                )
        except Exception:
            logger.exception("structured monitor provider raised unexpectedly")
            result = provider_error_result(ProviderErrorKind.TRANSIENT, "provider_transient")
        decision = await self._service.apply_monitor_probe(
            loop.id,
            result,
            now=now,
            config_generation=config_generation,
        )
        if decision is not MonitorDecision.WAKE_ACTIONABLE:
            return decision
        return await self._dispatch_claimed(loop, state, now=now)

    async def _dispatch_claimed(
        self,
        loop: _Loop,
        state: MonitorState,
        *,
        now: float,
    ) -> MonitorDecision:
        """Deliver one persisted claim or schedule its typed recovery path."""
        envelope = format_monitor_wake(
            monitor_id=loop.id,
            target=state.target,
            objective=state.objective,
            fingerprint=state.last_wake_fingerprint,
            reason_code=state.last_wake_reason_code,
            canonical=state.last_observation,
            wake_instructions=state.wake_instructions,
        )
        if not await self._service.monitor_dispatch_is_authorized(
            loop.id,
            state.last_wake_fingerprint,
        ):
            return MonitorDecision.STOP_BLOCKED
        try:
            delivered = await self._dispatch(loop, envelope)
        except asyncio.CancelledError:
            await self._service.record_monitor_dispatch_busy(
                loop.id,
                state.last_wake_fingerprint,
                now=now,
            )
            raise
        except Exception:
            logger.exception("structured monitor delivery raised unexpectedly")
            delivered = MonitorDispatchResult.BUSY
        if not isinstance(delivered, MonitorDispatchResult):
            logger.error("structured monitor dispatcher returned an untyped result")
            delivered = MonitorDispatchResult.UNAVAILABLE
        if delivered is MonitorDispatchResult.UNAVAILABLE:
            await self._service.record_monitor_dispatch_failure(
                loop.id,
                state.last_wake_fingerprint,
                now=now,
            )
        elif delivered is MonitorDispatchResult.BUSY:
            await self._service.record_monitor_dispatch_busy(
                loop.id,
                state.last_wake_fingerprint,
                now=now,
            )
        elif delivered is MonitorDispatchResult.DISPATCHED:
            await self._service.record_monitor_dispatched(
                loop.id,
                state.last_wake_fingerprint,
                now=self._clock(),
            )
        return MonitorDecision.WAKE_ACTIONABLE


def format_monitor_wake(
    *,
    monitor_id: str,
    target: str,
    objective: str,
    fingerprint: str,
    reason_code: str,
    canonical: Mapping[str, object],
    wake_instructions: str = "",
) -> str:
    """Render only allowlisted canonical facts, redacted before the hard cap."""
    checks = canonical.get("checks")
    changed: list[str] = []
    if isinstance(checks, Mapping):
        for state in ("failed", "pending", "unknown"):
            values = checks.get(state)
            if isinstance(values, list) and values:
                changed.append(f"{state} checks: {len(values)}")
    for name in ("blocking_review", "mergeability", "review_decision", "state"):
        value = canonical.get(name)
        if isinstance(value, (str, int, bool)):
            changed.append(f"{name}={value}")
    head = canonical.get("head_revision")
    action = wake_instructions.strip() or "Inspect the changed facts and take the next safe action."
    envelope = (
        f"{MONITOR_WAKE_PREFIX}\n"
        f"Monitor {monitor_id}: pull request {target}; objective: {objective}.\n"
        f"Fingerprint: {fingerprint}. Classification: {reason_code or 'actionable'}.\n"
        f"Head: {head if isinstance(head, str) else 'unknown'}. "
        f"Changed: {'; '.join(changed) or 'canonical state changed'}.\n"
        f"Next action: {action}"
    )
    envelope, _ = redact_exfiltration_urls(envelope)
    envelope, _ = redact_credentials(envelope)
    return envelope[:MONITOR_WAKE_MAX_CHARS]
