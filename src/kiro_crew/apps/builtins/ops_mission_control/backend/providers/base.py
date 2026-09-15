"""Ops Mission Control — provider extension points.

Four narrow Protocols, each answering exactly one question:

===================  ==============================================
``SignalSource``     What is firing right now?
``RotationSource``   Who is on shift?
``ActionSink``       How do I acknowledge / resolve / comment?
``EvidenceSource``   What context surrounds this signal?
===================  ==============================================

This mirrors the Composed Platform Providers (CPP) pattern in
``kiro_crew/platform/interfaces.py``: the core defines Protocols, ships a default
adapter for each, and never branches on which edition is running. The public
adapters (CloudWatch, PagerDuty, Datadog, GitHub Issues, webhook, no-op) live
beside this file; a companion package contributes its own through the
ADD-only registry and is never imported here.

Splitting the seam four ways rather than defining one fat ``OpsProvider`` is
deliberate: real providers cover different subsets. CloudWatch has alarms and
metrics but no rotation and nothing to resolve; a static YAML rota answers only
"who is on shift". A fat interface would force every adapter to stub three
quarters of itself.

See ``docs/system-specs/modules/ops-mission-control.md`` (provider interfaces).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

#: Default caps for one investigation's evidence gathering. These exist because
#: evidence sources are paid, rate-limited third-party APIs: an investigation
#: that fans out without a ceiling can burn a user's Datadog quota or stall the
#: dispatch heartbeat behind a slow Logs Insights query.
DEFAULT_EVIDENCE_TIMEOUT_SECS = 20.0
DEFAULT_EVIDENCE_MAX_CALLS = 6
DEFAULT_EVIDENCE_MAX_BYTES = 64 * 1024

#: Per-source cap for one poll cycle. A provider returning thousands of stale
#: alarms must not be able to flood the board or the claim loop.
DEFAULT_POLL_LIMIT = 100

#: How many items a poll fetches to DETECT truncation: one past the cap. If a source hands
#: back `DEFAULT_POLL_LIMIT + 1`, the estate is larger than a poll can carry, and reporting
#: the result as a complete snapshot would let `reconcile` terminally resolve the omitted
#: still-firing signals. Requesting exactly the cap makes "full" and "capped" indistinguishable;
#: the extra item is the difference.
POLL_FETCH_LIMIT = DEFAULT_POLL_LIMIT + 1


class TruncatedSignals(list):
    """A poll result the SOURCE could not deliver completely — carried as a type.

    A `list` subclass so every consumer (`extend`, `len`, iteration) is unchanged and only the
    registry's health builder checks `isinstance`. An adapter that fetched more than
    `DEFAULT_POLL_LIMIT` matching signals returns its capped list wrapped in this, and
    `poll_all` marks the poll non-authoritative so absence from it is not read as recovery.

    Distinct from the registry's own post-slice detection because several adapters filter
    client-side (Datadog keeps only open monitors from a full page), so the raw fetch is the
    only place that knows the provider had more than we asked for.
    """


#: Wall-clock budget for a single source's poll. The dispatch heartbeat polls all
#: sources concurrently and must finish well inside its 2-minute interval, so one
#: unreachable provider cannot stall the others.
DEFAULT_POLL_TIMEOUT_SECS = 15.0


@dataclass(frozen=True)
class EvidenceBudget:
    """Caps for one investigation's evidence gathering.

    One budget served every adapter, which does not fit how they actually behave: a
    CloudWatch Logs Insights query is a submit-then-poll round trip that legitimately
    wants ~25s, while a Datadog REST call either answers in a couple of seconds or is
    broken. CloudWatch had already noticed — it declared ``_LOG_MAX_WAIT_SECS = 25.0``
    and then applied ``min(25.0, budget.timeout_secs)``, so with the global default of
    20s its own ceiling was unreachable dead code.

    ``for_source`` resolves a per-adapter budget from an adapter's declared
    ``evidence_budget_hint``, clamped so an adapter can never exceed the operator's
    configured ceiling. The hint expresses "this is what I need"; the operator's value
    stays the authority.
    """

    timeout_secs: float = DEFAULT_EVIDENCE_TIMEOUT_SECS
    max_calls: int = DEFAULT_EVIDENCE_MAX_CALLS
    max_bytes: int = DEFAULT_EVIDENCE_MAX_BYTES

    def for_source(self, source: Any) -> "EvidenceBudget":
        """This budget narrowed (never widened) by ``source``'s declared hint.

        An adapter with no hint gets this budget unchanged, so adding the attribute is
        opt-in and no existing adapter changes behavior. Every field is clamped with
        ``min``: a hint asking for MORE than the operator allowed is ignored, because a
        provider adapter must not be able to raise its own spend ceiling — the same
        reason the autonomy gate is resolved outside the adapter.
        """
        # Mapping, not dict: an adapter is encouraged to expose the hint as an
        # immutable MappingProxyType (a mutable class attribute shared across
        # instances invites an adapter rewriting its own ceiling at runtime), and a
        # mappingproxy is NOT a dict. Checking for dict silently ignored every
        # correctly-written hint — caught only because the test asserted the
        # clamped VALUE rather than that the call returned something.
        hint = getattr(source, "evidence_budget_hint", None)
        if not isinstance(hint, Mapping) or not hint:
            return self

        def _clamp(key: str, current: float) -> float:
            raw = hint.get(key)
            if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
                return current
            return min(float(raw), current)

        return EvidenceBudget(
            timeout_secs=_clamp("timeout_secs", self.timeout_secs),
            max_calls=int(_clamp("max_calls", self.max_calls)),
            max_bytes=int(_clamp("max_bytes", self.max_bytes)),
        )


@dataclass(frozen=True)
class Evidence:
    """One piece of context gathered about a signal.

    ``body`` is caller-facing text destined for a model prompt and/or a Slack
    thread. It MUST be passed through ``security.redact`` before it leaves the
    adapter — provider payloads routinely embed credentials, presigned URLs, and
    account identifiers. The gather helpers in this package do that centrally so
    an adapter cannot forget.
    """

    source: str
    kind: str
    title: str
    body: str
    url: str = ""


@dataclass(frozen=True)
class ShiftStatus:
    """Who is on shift, per a ``RotationSource``."""

    on_shift: bool
    who: str = ""
    until: str = ""
    #: True when the source cannot answer (unconfigured, API down). The tier gate
    #: treats this as ARMED rather than disarmed: failing to reach a rotation API
    #: must not silently switch off a team's incident response. Fail-open is
    #: correct here precisely because the on_shift tier only *observes* by
    #: default — arming it costs API polls, while wrongly disarming it costs
    #: missed incidents.
    unknown: bool = False


@dataclass(frozen=True)
class ActionResult:
    """Outcome of an ``ActionSink.execute`` call."""

    ok: bool
    action: str
    detail: str = ""
    error: str = ""
    #: True when the sink RECORDED the intent instead of performing it — the observe-only
    #: default, and anything a companion ships in the same spirit (a dry-run mode).
    #:
    #: Exists because ``ok=True`` from such a sink means "we successfully did nothing", and
    #: post-action verification cannot tell that apart from a real provider write. It read
    #: the still-firing alarm as the ACTION having failed and charged a ``miss_count`` to
    #: every ledger entry the investigation cited — so on the default install, where
    #: ``cloudwatch``/``webhook`` register no ``ActionSink`` at all and every action falls
    #: through to ``noop``, exercising the proposal flow demoted the operator's own proven
    #: knowledge for a write that was never attempted. Verified: an entry at
    #: verified/high/2 uses went to ``miss_count=1`` and lost the fast path after one
    #: observe-only "resolve".
    #:
    #: Defaults False, so every existing sink and every companion that has not heard of
    #: this field keeps being verified — the safe direction, since a real write is the case
    #: that must be checked.
    simulated: bool = False

    #: Seconds of provider-side SUPPRESSION this call actually established, or 0.
    #:
    #: Set it whenever the write leaves the signal quiet for a bounded window, regardless
    #: of which verb the caller asked for. It exists because the verb is not always the
    #: truth about what happened: Datadog implements ``resolve`` as an ALIAS onto the same
    #: bounded mute as ``silence`` (a monitor cannot be "resolved" through the API), so a
    #: resolve established a 4-hour mute while carrying no ``duration_secs`` — only
    #: ``EXPIRING_ACTIONS`` (i.e. ``silence``) gets one from the route. Verification then
    #: used its 5-minute default, rechecked INSIDE the mute, read the monitor as still
    #: Alert/Warn, and charged a ``miss`` to every ledger entry the investigation cited.
    #: The same false-miss accounting the ``simulated`` flag above exists to prevent,
    #: arriving through the schedule instead of through the sink.
    #:
    #: Reported by the sink rather than inferred at the boundary because only the adapter
    #: knows its provider aliased one verb onto another. Defaults 0, so a sink that has not
    #: heard of this field keeps the previous schedule. Found in review (GPT 5.6).
    suppressed_secs: int = 0


@dataclass(frozen=True)
class ProviderInfo:
    """Catalog metadata for the settings UI.

    ``config_fields`` are non-secret and land in the app's ``data/config.json``.
    ``secret_fields`` are write-only and land in the keystone-protected secret
    store — they are NEVER returned by any read endpoint (spec §5.1).
    """

    id: str
    display_name: str
    roles: tuple[str, ...]
    configured: bool
    config_fields: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()
    detail: str = ""
    labels: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SignalSource(Protocol):
    """A source of work items."""

    @property
    def id(self) -> str:
        """Stable adapter id, e.g. ``"cloudwatch"``. Used as the registry key."""
        ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool:
        """True when this adapter has everything it needs to poll.

        An unconfigured source is skipped by the heartbeat and shown as
        unconfigured in the UI — it never raises and never blocks a poll cycle.
        """
        ...

    async def poll(self) -> list[Signal]:
        """Return currently-firing signals, normalized via ``Signal.create``."""
        ...


#: Sources whose ``poll`` returns a COMPLETE snapshot of what is currently firing, so an
#: absence from the result is positive evidence the signal cleared.
#:
#: Read off ``SignalSource.is_snapshot`` when the adapter declares it, defaulting TRUE
#: because every polled API (CloudWatch, Datadog, PagerDuty, GitHub) is one and only the
#: exceptions need to say so.
#:
#: ``webhook`` is the exception, and its absence here was a real wrong answer rather than a
#: taxonomy nicety. It is a PUSH spool: ``poll`` calls ``drain``, which empties the queue —
#: so the very next cycle returns ``[]`` for a signal that is still firing at the sender,
#: and ``poll_health`` recorded that as ``ok: True`` with ``signals: 0``. Every consumer
#: that reads "the poll succeeded and the signal is absent" as recovery therefore got a
#: confident wrong answer one cycle after any webhook delivery. Verified: a claimed webhook
#: signal reached the verdict ``cleared`` ("the resolve held") off an empty drain, with
#: nothing having changed at the sender.
DEFAULT_IS_SNAPSHOT = True


@runtime_checkable
class RotationSource(Protocol):
    """Answers whether this operator is currently on shift."""

    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool: ...

    async def on_shift(self) -> ShiftStatus: ...


@runtime_checkable
class ActionSink(Protocol):
    """Performs a write against a provider (ack / resolve / comment)."""

    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool: ...

    def supported_actions(self) -> frozenset[str]:
        """Subset of ``models.VALID_ACTIONS`` this sink can perform."""
        ...

    async def execute(self, signal: Signal, action: str, payload: dict[str, Any]) -> ActionResult:
        """Perform ``action`` for ``signal``.

        A sink does not police its own authority (spec §5.3). The autonomy gate is resolved
        before this is reached, and that is enforced structurally rather than by convention:
        ``routes._execute_authorized`` is the only caller, and it requires an
        ``_Authorized`` permit that only ``routes._authorize`` can mint. A new caller
        therefore cannot reach a provider write without passing the gate — review flagged
        the earlier "callers MUST have resolved the gate first" wording as exactly the
        convention a third caller could silently skip.

        The no-op sink is the default, so an unconfigured install cannot write anywhere.
        """
        ...


@runtime_checkable
class EvidenceSource(Protocol):
    """Gathers read-only context about a signal."""

    @property
    def id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    def configured(self) -> bool: ...

    async def gather(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]: ...
