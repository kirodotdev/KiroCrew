"""Ops Mission Control — ADD-only provider registry.

The registry is the seam an out-of-tree companion package plugs into. Its one
non-obvious rule is that registration is **additive**: a companion can ADD
adapters but can never repoint a core one. Registering an id that already exists
is refused with a warning, and the core adapter wins.

That mirrors ``AppsLoader.registry_rows()`` in the platform CPP seam, and it
exists for the same reason: if a companion could shadow ``cloudwatch``, then
auditing what the public core does would require auditing every companion too. The
core never imports a companion and never branches on edition.

See ``docs/system-specs/modules/ops-mission-control.md`` (provider registry).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal, utc_now_iso
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    DEFAULT_IS_SNAPSHOT,
    DEFAULT_POLL_LIMIT,
    DEFAULT_POLL_TIMEOUT_SECS,
    ActionSink,
    Evidence,
    EvidenceBudget,
    EvidenceSource,
    ProviderInfo,
    RotationSource,
    ShiftStatus,
    SignalSource,
    TruncatedSignals,
)

logger = logging.getLogger(__name__)

#: How much raw body to scan per evidence item, as a multiple of the byte budget.
#: Bounds the redaction regex work without letting marker expansion push the final
#: text under the budget — 2x is far above the worst measured expansion (~1.09x).
_REDACT_HEADROOM = 2


#: Backoff applied to a source that failed without asking for a specific delay.
#: Deliberately short — the goal is to stop hammering a provider that is shedding
#: load, not to switch a source off after one bad cycle.
DEFAULT_BACKOFF_SECS = 5 * 60


class OpsProviderRegistry:
    """Holds the adapter sets and fans out across them."""

    def __init__(self) -> None:
        self._signal_sources: dict[str, SignalSource] = {}
        self._rotation_sources: dict[str, RotationSource] = {}
        self._action_sinks: dict[str, ActionSink] = {}
        self._evidence_sources: dict[str, EvidenceSource] = {}
        #: source id -> monotonic deadline before which we will not poll it again.
        #: Honours a provider's ``Retry-After`` when it sent one. Monotonic rather
        #: than wall-clock so a clock adjustment cannot strand a source.
        self._backoff_until: dict[str, float] = {}
        #: source id -> whether its LAST poll attempt actually succeeded, and when.
        #: This is the distinction reconcile depends on: "the signal is gone because
        #: it cleared" and "the signal is gone because we could not look" are opposite
        #: facts, and without this they were indistinguishable — so a 429 read as
        #: "everything resolved" and closed live work.
        self._poll_health: dict[str, dict[str, Any]] = {}

    # -- registration (ADD-only) ------------------------------------------

    @staticmethod
    def _add(bucket: dict[str, Any], item: Any, kind: str) -> bool:
        item_id = getattr(item, "id", "")
        if not item_id:
            logger.warning("ops-mission-control: refusing %s adapter with no id", kind)
            return False
        if item_id in bucket:
            # ADD-only: the incumbent wins. A companion cannot silently replace a
            # core adapter, so what the public core does stays auditable on its own.
            logger.warning(
                "ops-mission-control: %s adapter %r already registered — "
                "refusing to replace (registration is ADD-only)",
                kind,
                item_id,
            )
            return False
        bucket[item_id] = item
        logger.info("ops-mission-control: registered %s adapter %r", kind, item_id)
        return True

    def register_signal_source(self, src: SignalSource) -> bool:
        return self._add(self._signal_sources, src, "signal")

    def register_rotation_source(self, src: RotationSource) -> bool:
        return self._add(self._rotation_sources, src, "rotation")

    def register_action_sink(self, sink: ActionSink) -> bool:
        return self._add(self._action_sinks, sink, "action")

    def register_evidence_source(self, src: EvidenceSource) -> bool:
        return self._add(self._evidence_sources, src, "evidence")

    # -- lookup ------------------------------------------------------------

    def signal_sources(self) -> list[SignalSource]:
        return list(self._signal_sources.values())

    def configured_signal_sources(self) -> list[SignalSource]:
        """Signal sources the operator has actually set up.

        A ``configured()`` that raises counts as NOT configured: an adapter whose own
        readiness check is broken cannot be trusted to poll, and treating it as ready
        would turn "nothing is watching" into a source-level error every cycle.
        """
        ready: list[SignalSource] = []
        for src in self._signal_sources.values():
            try:
                if src.configured():
                    ready.append(src)
            except Exception:  # noqa: BLE001 — a faulty adapter must not break the cycle
                logger.exception(
                    "ops-mission-control: configured() raised for signal source %r", src.id
                )
        return ready

    def rotation_sources(self) -> list[RotationSource]:
        return list(self._rotation_sources.values())

    def action_sink(self, sink_id: str) -> ActionSink | None:
        return self._action_sinks.get(sink_id)

    def action_sinks(self) -> list[ActionSink]:
        return list(self._action_sinks.values())

    def evidence_sources(self) -> list[EvidenceSource]:
        return list(self._evidence_sources.values())

    def catalog(self) -> list[ProviderInfo]:
        """Adapter catalog for the settings UI.

        Merges the four role buckets by adapter id, so a provider that is both a
        signal source and an action sink (PagerDuty) appears once with both roles
        rather than twice.
        """
        merged: dict[str, dict[str, Any]] = {}
        buckets: tuple[tuple[str, dict[str, Any]], ...] = (
            ("signal", self._signal_sources),
            ("rotation", self._rotation_sources),
            ("action", self._action_sinks),
            ("evidence", self._evidence_sources),
        )
        for role, bucket in buckets:
            for adapter_id, adapter in bucket.items():
                slot = merged.setdefault(
                    adapter_id,
                    {
                        "id": adapter_id,
                        "display_name": getattr(adapter, "display_name", adapter_id),
                        "roles": [],
                        "configured": False,
                        "config_fields": getattr(adapter, "config_fields", ()),
                        "secret_fields": getattr(adapter, "secret_fields", ()),
                        "detail": getattr(adapter, "detail", ""),
                    },
                )
                slot["roles"].append(role)
                try:
                    if adapter.configured():
                        slot["configured"] = True
                except Exception:  # noqa: BLE001 — a broken adapter must not break the catalog
                    logger.exception("ops-mission-control: configured() raised for %r", adapter_id)
        return [
            ProviderInfo(
                id=str(slot["id"]),
                display_name=str(slot["display_name"]),
                roles=tuple(slot["roles"]),
                configured=bool(slot["configured"]),
                config_fields=tuple(slot["config_fields"]),
                secret_fields=tuple(slot["secret_fields"]),
                detail=str(slot["detail"]),
            )
            for slot in sorted(merged.values(), key=lambda s: str(s["id"]))
        ]

    # -- fan-out -----------------------------------------------------------

    async def poll_all(
        self,
        *,
        timeout_secs: float = DEFAULT_POLL_TIMEOUT_SECS,
        limit: int = DEFAULT_POLL_LIMIT,
    ) -> tuple[list[Signal], dict[str, str]]:
        """Poll every configured signal source concurrently.

        Returns ``(signals, errors)``. One unreachable provider must never stall
        the heartbeat or suppress the others, so each source gets its own timeout
        and its failure is reported rather than raised.

        Two things happen around each poll, both of which exist because a failed poll
        used to be indistinguishable from a quiet one:

        1. **A source in backoff is skipped**, and says so in ``errors``. A provider
           that returned 429 was previously re-polled at full rate on the very next
           heartbeat, which is how a rate limit becomes a ban.
        2. **The outcome is recorded in ``poll_health``.** Absence of a signal only
           means "it cleared" if the poll that would have reported it actually
           succeeded; callers that resolve work on absence MUST consult this.
        """
        sources = [s for s in self._signal_sources.values() if self._safe_configured(s)]
        if not sources:
            return [], {}

        now = time.monotonic()

        async def _poll_one(src: SignalSource) -> tuple[str, list[Signal] | str]:
            deadline = self._backoff_until.get(src.id, 0.0)
            if deadline > now:
                # Not an error the operator caused, but it IS a reason the source
                # contributed nothing this cycle, so it belongs in `errors` where the
                # Signals tab already renders per-source reasons.
                return (
                    src.id,
                    f"backing off for another {deadline - now:.0f}s after a prior failure",
                )
            try:
                signals = await asyncio.wait_for(src.poll(), timeout=timeout_secs)
                # An adapter that filters client-side (e.g. Datadog keeps only open monitors
                # from a full page) is the only party that knows the provider had more than we
                # asked for, so it reports truncation by returning `TruncatedSignals`. Preserve
                # that verdict even when the post-filter count is under the cap.
                source_truncated = isinstance(signals, TruncatedSignals)
                collected = list(signals)
                if source_truncated:
                    logger.warning(
                        "ops-mission-control: %r reported a truncated poll — marking it "
                        "non-authoritative so the omitted signals are not read as recovered",
                        src.id,
                    )
                    return src.id, TruncatedSignals(collected[:limit])
                if len(collected) > limit:
                    # TRUNCATION MUST NOT LOOK LIKE A COMPLETE SNAPSHOT. The cap bounds
                    # memory and prompt size and is worth keeping, but the omitted signals
                    # are absent from this poll — and absence from a snapshot source is read
                    # as recovery by `reconcile` and `verify_pending_actions`. So a provider
                    # returning limit+1 firing alarms would have had the surplus verify as
                    # CLEARED while they were still firing. Reported via the existing
                    # `snapshot=False` channel (built for the webhook spool's drain) rather
                    # than a new one, because the two cases are the same fact: this poll did
                    # not see everything. Found in review (GPT 5.6).
                    logger.warning(
                        "ops-mission-control: %r returned %d signals, over the %d cap — "
                        "keeping the first %d and marking this poll non-authoritative so "
                        "the omitted ones are not read as recovered",
                        src.id,
                        len(collected),
                        limit,
                        limit,
                    )
                    return src.id, TruncatedSignals(collected[:limit])
                return src.id, collected
            except asyncio.TimeoutError as exc:
                # Backs off too, and the ordering of these two clauses is why it needed
                # saying: ``asyncio.TimeoutError`` IS an ``Exception``, so this clause
                # shadows the one below and a timing-out source was the one failure mode
                # that never armed a window. It was also the most expensive one to leave
                # unarmed — a failure that returns fast costs a socket, while a hung
                # provider burns the FULL per-source timeout (15s) out of every heartbeat,
                # for as long as it stays hung. Verified before fixing: three consecutive
                # timing-out polls left ``_backoff_until`` empty.
                logger.info("ops-mission-control: poll timed out for %r", src.id)
                self._note_backoff(src.id, exc)
                return src.id, f"timed out after {timeout_secs:.0f}s"
            except Exception as exc:  # noqa: BLE001 — surfaced as per-source health
                logger.exception("ops-mission-control: poll failed for %r", src.id)
                self._note_backoff(src.id, exc)
                return src.id, str(exc) or exc.__class__.__name__

        results = await asyncio.gather(*(_poll_one(s) for s in sources))

        signals: list[Signal] = []
        errors: dict[str, str] = {}
        for source_id, outcome in results:
            if isinstance(outcome, str):
                errors[source_id] = outcome
                self._poll_health[source_id] = {"ok": False, "detail": outcome, "at": utc_now_iso()}
            else:
                truncated = isinstance(outcome, TruncatedSignals)
                signals.extend(outcome)
                # A success clears any backoff: the provider is answering again.
                self._backoff_until.pop(source_id, None)
                self._poll_health[source_id] = {
                    "ok": True,
                    "detail": (
                        f"returned more than {limit} signals; showing the first {limit}"
                        if truncated
                        else ""
                    ),
                    "at": utc_now_iso(),
                    "signals": len(outcome),
                    # Whether ABSENCE from this successful poll means anything. `ok` alone
                    # answers "did we look", not "did we see everything" — and for the
                    # webhook spool those differ, because a drain empties the queue so a
                    # still-firing pushed signal is absent from every cycle after the one
                    # that delivered it. Carried here rather than re-derived per consumer:
                    # `verify_pending_actions` and the reconcile SOP both reason from
                    # absence, and a second copy of this rule is a second place to forget
                    # it.
                    "snapshot": self._is_snapshot(source_id) and not truncated,
                }
        return signals, errors

    def _is_snapshot(self, source_id: str) -> bool:
        """Whether absence from this source's successful poll is evidence of recovery.

        Defaults TRUE for a source that does not declare the attribute, because every
        polled provider API is a snapshot and only the exceptions (the push spool) need to
        opt out — so a companion adapter written before this existed keeps its previous,
        correct behaviour, and only one that genuinely drains a queue has to say so.
        """
        src = self._signal_sources.get(source_id)
        return bool(getattr(src, "is_snapshot", DEFAULT_IS_SNAPSHOT))

    def _note_backoff(self, source_id: str, exc: BaseException) -> None:
        """Arm a backoff window for a source that just failed.

        Honours the provider's own ``Retry-After`` when it sent one (already clamped in
        ``http.py``), otherwise a flat default. EVERY failure backs off, not just a
        retryable one: a source failing on a 401 every 120 seconds burns the heartbeat's
        budget re-learning the same thing, and the operator has to fix the config either
        way. What ``is_retryable`` buys is honouring the provider's *own* delay, which is
        why only that path can exceed the default.
        """
        retry_after = int(getattr(exc, "retry_after", 0) or 0)
        delay = float(retry_after) if retry_after > 0 else float(DEFAULT_BACKOFF_SECS)
        self._backoff_until[source_id] = time.monotonic() + delay
        logger.info(
            "ops-mission-control: backing off %r for %.0fs (retry_after=%s)",
            source_id,
            delay,
            retry_after,
        )

    def poll_health(self) -> dict[str, dict[str, Any]]:
        """Per-source outcome of the LAST poll attempt this process made.

        ``{source_id: {"ok": bool, "detail": str, "at": iso, "signals": int}}``. A
        source absent from this map has not been polled since the process started,
        which is NOT the same as healthy — a caller reasoning about absence must treat
        "unknown" as "cannot conclude the signal cleared".
        """
        return {k: dict(v) for k, v in self._poll_health.items()}

    async def resolve_shift(self) -> ShiftStatus:
        """Resolve on-shift status across rotation sources.

        Any configured REAL source reporting on-shift arms the tier (a person on two
        rotations is on shift). If every real source errors or none is configured, the
        result is ``unknown=True`` and the tier gate treats that as ARMED — failing to
        reach a rotation API must not silently switch off incident response.

        **Fallback sources are consulted only when no real source can answer.**
        ``AlwaysOnRotationSource`` is always configured and always on-shift, so
        including it in the normal vote made every real rotation unhearable: this
        method returns the first on-shift status, and the always-on default was always
        one of them. A real source answering "someone else is on call" was therefore
        discarded — the on-shift tier armed permanently for everyone, which is exactly
        the failure a rotation exists to prevent. Verified against the pre-fix code
        (a real off-shift source resolved to ``on_shift=True``) before changing this.
        """
        configured = [s for s in self._rotation_sources.values() if self._safe_configured(s)]
        # A fallback declares itself; a source that never heard of the flag is real.
        sources = [s for s in configured if not getattr(s, "is_fallback", False)]
        if not sources:
            # Only defaults are available: report the floor (armed), and say it is not
            # a real answer so the UI can distinguish "no rotation" from "on call".
            return ShiftStatus(on_shift=True, who="", until="", unknown=True)

        async def _one(src: RotationSource) -> ShiftStatus | None:
            try:
                return await asyncio.wait_for(src.on_shift(), timeout=DEFAULT_POLL_TIMEOUT_SECS)
            except Exception:  # noqa: BLE001
                logger.exception("ops-mission-control: rotation check failed for %r", src.id)
                return None

        statuses = [s for s in await asyncio.gather(*(_one(s) for s in sources)) if s is not None]
        if not statuses:
            return ShiftStatus(on_shift=True, who="", until="", unknown=True)
        for status in statuses:
            if status.on_shift:
                return status
        return statuses[0]

    async def gather_evidence(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]:
        """Gather evidence from every configured source, within budget.

        Every returned body passes through token redaction here — centrally, so an
        adapter author cannot forget and leak a credential into a model prompt.

        Redaction goes through ``platform.redact_via_context``, NOT ``security.redact``
        directly. That shim is the documented canonical egress point, and the difference is
        load-bearing for exactly the case this app is built to support: a loaded companion
        contributes its own credential patterns declaratively through the
        ``CredentialPolicy`` seam, so internal token shapes are scrubbed without this
        module knowing they exist. Calling ``security.redact`` bypassed that seam entirely
        — an enterprise host would have silently redacted with public patterns only, and
        one that failed to compose its companion would have degraded quietly instead of
        failing closed. The whole reason an internal adapter is allowed to return raw text
        is that this choke is trustworthy.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens
        from kiro_crew.platform.context import redact_via_context as core_redact

        sources = [s for s in self._evidence_sources.values() if self._safe_configured(s)]
        if not sources:
            return []

        async def _one(src: EvidenceSource) -> list[Evidence]:
            # Per-adapter budget: an adapter may declare what it actually needs
            # (`evidence_budget_hint`) and `for_source` clamps it against the
            # operator's ceiling, so a hint can only ever narrow. The wait_for uses
            # the SAME resolved value the adapter was handed — passing one timeout and
            # enforcing another is how an adapter gets killed mid-call while believing
            # it had budget left.
            per_source = budget.for_source(src)
            try:
                return list(
                    await asyncio.wait_for(
                        src.gather(signal, per_source), timeout=per_source.timeout_secs
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception("ops-mission-control: evidence failed for %r", src.id)
                return []

        gathered = await asyncio.gather(*(_one(s) for s in sources))

        out: list[Evidence] = []
        for batch in gathered:
            for item in batch:
                # THE redaction chokepoint. This is the only caller of any adapter's
                # ``gather()``, so redacting here is what lets ``Evidence``'s
                # docstring promise that an adapter cannot forget. Provider payloads
                # and log lines routinely embed credentials — governance guidance on
                # logging is explicit that logs must not contain secrets, which means
                # log content is exactly the place they turn up by accident.
                #
                # Truncate AFTER redacting, not before: a redaction marker is longer
                # than most of what it replaces, so capping first let the final body
                # exceed the budget (measured ~1.09x on an all-credential body). The
                # budget exists to bound what reaches the model's context, so it has
                # to bound the text actually emitted.
                #
                # A pre-trim still bounds the REGEX work — redacting an unbounded
                # body would let a misbehaving adapter hand us arbitrarily much text
                # to scan. The headroom is generous enough that expansion cannot pull
                # the post-redaction result below the budget.
                pre = item.body[: budget.max_bytes * _REDACT_HEADROOM]
                body = redact_tokens(core_redact(pre))[: budget.max_bytes]
                out.append(
                    Evidence(
                        source=item.source,
                        kind=item.kind,
                        # Redacted like the body: an evidence TITLE is provider-controlled too
                        # (a CloudWatch alarm name, a Datadog monitor title), and it flows into
                        # `ClaimedIncident.to_dict()` — the dashboard JSON and the model brief.
                        # The body was redacted here while the title beside it was copied raw,
                        # the same title/body asymmetry the Slack and brief sinks each had.
                        # Found in review. Titles are short, so no headroom/budget dance.
                        title=redact_tokens(core_redact(item.title)),
                        body=body,
                        url=item.url,
                    )
                )
        return out

    @staticmethod
    def _safe_configured(adapter: Any) -> bool:
        try:
            return bool(adapter.configured())
        except Exception:  # noqa: BLE001
            logger.exception(
                "ops-mission-control: configured() raised for %r",
                getattr(adapter, "id", "?"),
            )
            return False


_registry: OpsProviderRegistry | None = None


def get_registry() -> OpsProviderRegistry:
    """The process-wide registry, populated on first use.

    Public adapters are installed FIRST, then any admitted companion package's.
    That order is what makes ADD-only meaningful: since the incumbent wins, a
    companion cannot take an id the core already holds no matter what it registers.
    """
    global _registry
    if _registry is None:
        _registry = OpsProviderRegistry()
        _install_public_adapters(_registry)
        _install_companions(_registry)
    return _registry


def _install_companions(registry: OpsProviderRegistry) -> None:
    """Give admitted companion packages a chance to add adapters.

    Imported lazily so the app still loads if anything in the discovery path is
    unavailable, and wrapped because a companion must never be able to prevent the
    public core from starting — see ``companion.py`` for why this fails open where
    platform plugin discovery fails closed.
    """
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend.companion import (
            install_companion_adapters,
        )

        install_companion_adapters(registry)
    except Exception:  # noqa: BLE001 — a companion fault must not break the app
        logger.exception("ops-mission-control: companion adapter discovery failed")


def reset_registry() -> None:
    """Drop the registry (tests only)."""
    global _registry
    _registry = None


def _install_public_adapters(registry: OpsProviderRegistry) -> None:
    """Register the adapters that ship in the public core.

    Imported lazily and defensively: an adapter whose optional dependency is
    missing must not prevent the app from loading — it reports unconfigured
    instead. ``boto3`` in particular is an optional lazy import here, matching the
    existing STT precedent.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
        cloudwatch,
        datadog,
        github_issues,
        noop,
        pagerduty,
        schedule_file,
        webhook,
    )

    # No-op first: it is the observe-only default, and registering it first means
    # a later adapter can never take the ``noop`` id.
    registry.register_action_sink(noop.NoopActionSink())
    registry.register_rotation_source(noop.AlwaysOnRotationSource())

    registry.register_signal_source(cloudwatch.CloudWatchSignalSource())
    registry.register_evidence_source(cloudwatch.CloudWatchEvidenceSource())

    pd = pagerduty.PagerDutyAdapter()
    registry.register_signal_source(pd)
    registry.register_rotation_source(pd)
    registry.register_action_sink(pd)

    dd = datadog.DatadogAdapter()
    registry.register_signal_source(dd)
    registry.register_action_sink(dd)
    registry.register_evidence_source(datadog.DatadogEvidenceSource())

    gh = github_issues.GitHubIssuesAdapter()
    registry.register_signal_source(gh)
    registry.register_action_sink(gh)

    registry.register_rotation_source(schedule_file.ScheduleFileRotationSource())

    registry.register_signal_source(webhook.WebhookSignalSource())
