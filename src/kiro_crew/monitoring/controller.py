"""Probe-first structured monitor controller and compact wake envelope."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Protocol

from kiro_crew import autonudge_provider_trust
from kiro_crew.dashboard.state import MONITOR_WAKE_PREFIX
from kiro_crew.monitoring.azure_devops_pull_request import AzureDevOpsPullRequestProvider
from kiro_crew.monitoring.bitbucket_pull_request import BitbucketPullRequestProvider
from kiro_crew.monitoring.decision import monitor_budget_reason
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider
from kiro_crew.monitoring.gitlab_merge_request import GitLabMergeRequestProvider
from kiro_crew.monitoring.models import (
    MAX_MONITOR_PROVIDER_CONCURRENCY,
    MonitorCreationSurface,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservation,
    MonitorProbeResult,
    MonitorState,
    MonitorVerdict,
    ProviderErrorKind,
    resolve_probe_result,
    transient_probe_failure,
)
from kiro_crew.monitoring.pull_request import provider_error_result
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

MONITOR_WAKE_MAX_CHARS = 4096
# GitHub and GitLab monitors intentionally retain their existing ambient CLI
# identity outside an explicitly authenticated dashboard creation. Every other
# provider fails closed on channel or legacy-unknown provenance unless it is
# explicitly added here with matching security docs.
_CHANNEL_OWNER_CREDENTIAL_KINDS = frozenset({"github_pull_request", "gitlab_merge_request"})

logger = logging.getLogger(__name__)


class _Loop(Protocol):
    id: str
    slot_key: str
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
        result: MonitorProbeResult,
        *,
        now: float,
        config_generation: int,
    ) -> MonitorVerdict: ...

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
        subjects: Sequence[str],
        *,
        previous_observations: Mapping[str, Mapping[str, object]] | None = None,
        use_owner_credentials: bool = True,
    ) -> Mapping[str, MonitorProbeResult]: ...


MonitorDispatcher = Callable[[Any, str], Awaitable[MonitorDispatchResult]]
OwnerCredentialsAuthorizer = Callable[[_Loop, MonitorState], bool]


class MonitorController:
    """Run typed probes and request a turn only for an accepted wake claim."""

    def __init__(
        self,
        service: _Service,
        dispatch: MonitorDispatcher,
        *,
        providers: Mapping[str, _Provider] | None = None,
        clock: Callable[[], float] = time.time,
        owner_credentials_authorized: OwnerCredentialsAuthorizer | None = None,
    ) -> None:
        self._service = service
        self._dispatch = dispatch
        self._clock = clock
        self._provider_gate = asyncio.Semaphore(MAX_MONITOR_PROVIDER_CONCURRENCY)
        self._owner_credentials_authorized = (
            owner_credentials_authorized or self._protected_owner_credentials_authorized
        )
        self._providers = dict(providers or {})
        if not self._providers:
            self._providers = {
                "github_pull_request": GitHubPullRequestProvider(),
                "gitlab_merge_request": GitLabMergeRequestProvider(),
                "azure_devops_pull_request": AzureDevOpsPullRequestProvider(),
                "bitbucket_pull_request": BitbucketPullRequestProvider(),
            }

    @staticmethod
    def _protected_owner_credentials_authorized(loop: _Loop, state: MonitorState) -> bool:
        return autonudge_provider_trust.is_monitor_owner_credentials_recorded(
            loop.id,
            loop.slot_key,
            state.kind,
            state.target,
        )

    async def tick(self, loop: _Loop, *, now: float) -> MonitorVerdict:
        """Run one probe and return its verdict.

        A verdict produced without probing -- a recorded outcome, an
        undelivered wake still in flight -- carries no entries, because nothing
        was observed on this tick.
        """
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
            return MonitorVerdict(decision=MonitorDecision.STOP_BLOCKED)
        if state.wake_in_flight:
            deadline = state.completion_evidence_deadline
            if state.wake_delivery is MonitorDispatchResult.BUSY:
                if now < state.next_probe_at:
                    return MonitorVerdict(decision=MonitorDecision.NO_CHANGE)
                if monitor_budget_reason(state, now=now):
                    await self._service.record_monitor_dispatch_busy(
                        loop.id,
                        state.last_wake_fingerprint,
                        now=now,
                    )
                    return MonitorVerdict(decision=MonitorDecision.STOP_BUDGET)
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
            return MonitorVerdict(decision=MonitorDecision.NO_CHANGE)
        if await self._service.stop_monitor_if_budget_exhausted(loop.id, now=now):
            return MonitorVerdict(decision=MonitorDecision.STOP_BUDGET)
        config_generation = state.config_generation
        target = state.target
        previous_observation = deepcopy(state.last_observation)
        provider = self._providers.get(state.kind)
        result: MonitorProbeResult
        if provider is None:
            result = provider_error_result(ProviderErrorKind.SETUP, "provider_unsupported")
            return await self._service.apply_monitor_probe(
                loop.id,
                result,
                now=now,
                config_generation=config_generation,
            )
        try:
            dashboard_owner_credentials = False
            if state.creation_surface is MonitorCreationSurface.DASHBOARD:
                dashboard_owner_credentials = await asyncio.to_thread(
                    self._owner_credentials_authorized,
                    loop,
                    state,
                )
            async with self._provider_gate:
                results = await asyncio.to_thread(
                    provider.probe,
                    (target,),
                    previous_observations={target: previous_observation},
                    use_owner_credentials=(
                        dashboard_owner_credentials or state.kind in _CHANNEL_OWNER_CREDENTIAL_KINDS
                    ),
                )
        except Exception:
            logger.exception("structured monitor provider raised unexpectedly")
            result = transient_probe_failure()
        else:
            # resolve_probe_result logs the unusable case itself: a status test here
            # cannot distinguish its synthesized fallback from a transient the
            # provider classified correctly, and would log every rate limit as an
            # error.
            result = resolve_probe_result(results, target)
        verdict = await self._service.apply_monitor_probe(
            loop.id,
            result,
            now=now,
            config_generation=config_generation,
        )
        if verdict.decision is not MonitorDecision.WAKE_ACTIONABLE:
            return verdict
        return await self._dispatch_claimed(loop, state, now=now, entries=verdict.entries)

    async def _dispatch_claimed(
        self,
        loop: _Loop,
        state: MonitorState,
        *,
        now: float,
        entries: tuple[MonitorObservation, ...] = (),
    ) -> MonitorVerdict:
        """Deliver one persisted claim or schedule its typed recovery path.

        *entries* are the observations that produced the claim, carried through
        so the delivered verdict still names its evidence. A retry of a claim
        persisted on an earlier tick has none to carry.
        """
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
            return MonitorVerdict(decision=MonitorDecision.STOP_BLOCKED, entries=entries)
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
        return MonitorVerdict(decision=MonitorDecision.WAKE_ACTIONABLE, entries=entries)


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
