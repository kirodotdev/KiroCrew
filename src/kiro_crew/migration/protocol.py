"""Generic crew-to-crew migration protocol (slice 1, no unit-kind knowledge).

The design (``.kiro/specs/crew-work-migration/design.md``) states the central
invariant plainly:

    Every failure mode short of a durable ack leaves the SOURCE owning the work.

This module encodes exactly that. ``MigrationCoordinator`` drives the five
ordered steps and owns the failure semantics; anything unit-type-specific lives
behind ``MigrationUnitAdapter`` and never leaks into the coordinator.

Ownership is a *release-after-ack* protocol, not a distributed lock: the source
is authoritative until it durably records that the target holds the unit. That
yields at-most-one executor at every instant.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from kiro_crew.credential_patterns import AWS_KEY_ID

from .journal import InFlightHandoff, MigrationJournal

logger = logging.getLogger(__name__)


class MidRunError(RuntimeError):
    """Raised by an adapter's quiesce() when the unit is mid-execution.

    Requirements 4.9 / 6.6: a unit cannot be migrated while a run is in
    flight — the coordinator surfaces this as a 'refused' outcome with a
    'mid-run' reason, and nothing is quiesced-then-lost.
    """


# --------------------------------------------------------------------- data model


@dataclass(frozen=True)
class CrewRef:
    """A reference to a crew endpoint. Identity only — no credentials."""

    crew_id: str
    label: str = ""


@dataclass(frozen=True)
class QuiesceToken:
    """Opaque proof that a unit was quiesced, handed back to ``unquiesce``."""

    unit_id: str
    token: str


@dataclass(frozen=True)
class HostRequirement:
    """A NAMED requirement the target must satisfy — never a transferred value.

    Kinds mirror the design: credential, mcp_server, agent, project_checkout,
    script_path, command_policy, git_repo.
    """

    kind: str
    identity: str
    severity: str = "advisory"  # "blocking" | "advisory"


@dataclass(frozen=True)
class Finding:
    """A single preflight observation."""

    kind: str
    detail: str
    severity: str = "advisory"  # "blocking" | "advisory"
    detail_key: str = ""  # optional: the reference this finding concerns


@dataclass(frozen=True)
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)
    resume_class: str | None = None  # taskrun only: "resume" | "restart"

    @property
    def blocked(self) -> bool:
        return any(f.severity == "blocking" for f in self.findings)


@dataclass(frozen=True)
class MigrationBundle:
    bundle_kind: str  # "cron" | "session" | "taskrun"
    bundle_version: int
    handoff_id: str  # idempotency key
    created_ts: float
    source_crew: CrewRef
    payload: dict
    requirements: list[HostRequirement] = field(default_factory=list)


@dataclass(frozen=True)
class AcceptAck:
    """Durable acknowledgement from the target that it persisted the unit."""

    unit_id: str


@dataclass(frozen=True)
class Tombstone:
    unit_kind: str
    target_crew: CrewRef
    remote_unit_id: str
    migrated_ts: float


@dataclass(frozen=True)
class MigrationResult:
    """Terminal outcome of a migrate() call.

    outcome:
      "migrated" — target durably holds the unit, source released
      "refused"  — preflight blocked or target refused; nothing quiesced-then-lost
      "failed"   — a step after quiesce failed; source un-quiesced, still owns
    """

    outcome: str
    remote_unit_id: str | None = None
    report: PreflightReport | None = None
    reason: str = ""


@dataclass(frozen=True)
class ReconcileResult:
    """Result of startup reconciliation for the ack->tombstone crash window."""

    owner: str  # "source" | "target"
    remote_unit_id: str | None = None


# ------------------------------------------------------ allow-list serialization


def allow_list_serialize(source: dict, allowed: tuple[str, ...]) -> dict:
    """Return only the explicitly-allowed keys present in ``source``.

    Requirement 3.4: a field not named is DROPPED. An allowed field absent from
    the source is simply omitted (not emitted as ``None``), so the output never
    invents state the source did not have.
    """
    return {k: source[k] for k in allowed if k in source}


# ----------------------------------------------------------- credential scanning

# Conservative, high-precision patterns. The goal is a blocking finding on
# obvious credential material in a serialized payload, not exhaustive DLP.
#
# The AWS key-ID spelling is IMPORTED, never written out here. The repo keeps one
# source of truth for it in `credential_patterns` and a test fails any module
# that pastes a sixth copy — which this module did, and which only the full
# suite caught.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(rf"\b{AWS_KEY_ID}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*")),
    (
        "api_key_header",
        re.compile(r"(?i)\b(?:x-api-key|api[_-]?key|authorization)\b\s*[:=]\s*\S{12,}"),
    ),
    ("generic_secret_kv", re.compile(r"(?i)\b(?:secret|password|passwd|token)\b\s*[:=]\s*\S{8,}")),
)


def _walk_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _walk_strings(v)


def scan_for_secrets(payload: Any) -> list[Finding]:
    """Scan a serialized payload for credential-shaped material.

    Returns a ``blocking`` Finding per matched pattern. The detail names the
    pattern, never the matched value — findings are surfaced to the user and
    must not themselves leak the secret.
    """
    findings: list[Finding] = []
    for text in _walk_strings(payload):
        for name, pat in _SECRET_PATTERNS:
            if pat.search(text):
                findings.append(
                    Finding(
                        kind="credential",
                        detail=f"payload matches credential pattern '{name}'",
                        severity="blocking",
                    )
                )
    return findings


# ------------------------------------------------------------------ audit sink


def _sel_audit(entry: dict) -> None:
    """Default audit sink: the security event log.

    A migration transfers the RIGHT TO EXECUTE between hosts, which makes it a
    permission decision, so it belongs in the same audit trail as every other
    one (Requirement 3.5). Imported lazily because ``protocol`` is otherwise
    dependency-free and imported by the CLI.
    """
    from kiro_crew.security_event_log import sel

    unit = entry.get("unit_id", "")
    sel().log_api_access(
        caller="migration",
        operation=entry.get("event", "migrate"),
        outcome=entry.get("outcome", "progress"),
        source="migration",
        resources=(
            f"unit_id={unit} target={entry.get('target', '')} "
            f"handoff_id={entry.get('handoff_id', '')} "
            f"duration={entry.get('duration', '')}"
        ),
    )


# ------------------------------------------------------------------ adapter seam


@runtime_checkable
class MigrationUnitAdapter(Protocol):
    """One per unit kind. Owns what the unit's durable state is and how to stop
    it safely. No unit-type knowledge leaks past this seam into the coordinator.
    """

    bundle_kind: str
    bundle_version: int

    async def describe(self, unit_id: str) -> dict: ...
    async def requirements(self, unit_id: str) -> list[HostRequirement]: ...
    async def quiesce(self, unit_id: str) -> QuiesceToken: ...
    async def unquiesce(self, unit_id: str, token: QuiesceToken) -> None: ...
    async def serialize(self, unit_id: str) -> dict: ...
    async def materialize(self, payload: dict) -> str: ...
    async def tombstone(self, unit_id: str, target: CrewRef, remote_id: str) -> None: ...


class MigrationReceiver:
    """Target-side endpoint base. Two operations reach it over the tunnel:

    - ``preflight``: pure, read-only capability/reference probe.
    - ``accept``: validate -> persist -> fsync -> ack, idempotent on handoff_id.

    Subclasses wire these to real persistence; slice 1 ships the protocol and a
    fake receiver lives in the tests.
    """

    async def preflight(
        self, bundle: MigrationBundle
    ) -> PreflightReport:  # pragma: no cover - base
        raise NotImplementedError

    async def accept(self, bundle: MigrationBundle) -> AcceptAck:  # pragma: no cover - base
        raise NotImplementedError

    async def lookup(self, handoff_id: str) -> AcceptAck | None:  # pragma: no cover - base
        """Return the unit the target holds for ``handoff_id``, or None.

        Used by source-side startup reconciliation to resolve the crash window.
        """
        raise NotImplementedError


# -------------------------------------------------------------- the coordinator


class MigrationCoordinator:
    """Source-side driver of the five-step handoff.

    Strict order: preflight -> quiesce -> transmit -> await durable ack ->
    tombstone + release. The coordinator holds NO unit-type knowledge; every
    type-specific action goes through the adapter.
    """

    def __init__(
        self,
        *,
        adapter: MigrationUnitAdapter,
        receiver: MigrationReceiver,
        source_crew: CrewRef,
        target_crew: CrewRef,
        clock=time.time,
        audit_sink=None,
        journal_dir=None,
    ) -> None:
        self.adapter = adapter
        self.receiver = receiver
        self.source_crew = source_crew
        self.target_crew = target_crew
        self._clock = clock
        # Durable record of handoffs whose outcome is not yet known. Optional so
        # every existing caller and test keeps working, but a coordinator built
        # WITHOUT it cannot survive a crash between ack and tombstone: nothing
        # would know the handoff existed. Production call sites must supply it.
        self._journal = MigrationJournal(store_dir=journal_dir) if journal_dir else None
        # Injected for tests; when omitted the default is resolved at LOG time
        # (not captured here) so the module-level sink stays swappable and a
        # coordinator built with no sink still leaves an auditable trail rather
        # than only an in-process list nobody reads.
        self._audit_sink = audit_sink
        self.audit: list[dict] = []
        self._ctx: dict = {}

    # -- helpers --------------------------------------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        """Record one step locally AND to the audit sink.

        Every entry carries the migration's identifying context (unit, target,
        handoff id) so a single audit line is meaningful on its own — an auditor
        reading `migrate.failed` should not have to correlate it with an earlier
        line to learn which unit it was about.
        """
        entry = {"ts": self._clock(), "event": event, **self._ctx, **fields}
        self.audit.append(entry)
        sink = self._audit_sink if self._audit_sink is not None else _sel_audit
        try:
            sink(entry)
        except Exception:  # pragma: no cover - audit must never break a migration
            logger.debug("migration audit sink failed for %s", event, exc_info=True)

    def _build_bundle(
        self,
        unit_id: str,
        payload: dict,
        *,
        handoff_id: str | None = None,
        requirements: list[HostRequirement] | None = None,
    ) -> MigrationBundle:
        return MigrationBundle(
            bundle_kind=self.adapter.bundle_kind,
            bundle_version=self.adapter.bundle_version,
            handoff_id=handoff_id or uuid.uuid4().hex,
            created_ts=self._clock(),
            source_crew=self.source_crew,
            payload=payload,
            requirements=requirements or [],
        )

    # -- the five-step handoff ------------------------------------------------

    async def migrate(self, unit_id: str, *, handoff_id: str | None = None) -> MigrationResult:
        started = self._clock()
        handoff_id = handoff_id or uuid.uuid4().hex
        # Context every audit line inherits, so one line is self-describing.
        self._ctx = {
            "unit_id": unit_id,
            "handoff_id": handoff_id,
            "target": self.target_crew.crew_id,
            "source": self.source_crew.crew_id,
        }
        self._log("migrate.start")

        # Build the bundle up-front (read-only) so preflight can inspect it and
        # the secret scan runs BEFORE anything is quiesced.
        try:
            payload = await self.adapter.serialize(unit_id)
        except Exception as exc:  # serialize is read-only; nothing to roll back
            self._log(
                "migrate.refused",
                outcome="failed",
                reason=f"serialize failed: {exc}",
                duration=self._clock() - started,
            )
            return MigrationResult(outcome="failed", reason=str(exc))

        requirements = list(await self.adapter.requirements(unit_id))
        bundle = self._build_bundle(
            unit_id, payload, handoff_id=handoff_id, requirements=requirements
        )

        # Local containment: a credential-shaped payload is a blocking finding
        # here, before quiesce, so the source is never disturbed.
        secret_findings = scan_for_secrets(payload)

        # Step 1: preflight (read-only, remote). Refuse early — nothing quiesced.
        try:
            report = await self.receiver.preflight(bundle)
        except Exception as exc:
            self._log(
                "migrate.refused",
                outcome="refused",
                reason=f"preflight unreachable: {exc}",
                duration=self._clock() - started,
            )
            return MigrationResult(outcome="refused", reason=f"target unreachable: {exc}")

        if secret_findings:
            report = PreflightReport(
                findings=list(report.findings) + secret_findings,
                resume_class=report.resume_class,
            )

        if report.blocked:
            self._log(
                "migrate.refused",
                outcome="refused",
                reason="preflight blocking finding",
                duration=self._clock() - started,
            )
            return MigrationResult(
                outcome="refused", report=report, reason="preflight reported a blocking finding"
            )

        # Step 2: quiesce. From here a failure MUST roll back (un-quiesce).
        token = await self.adapter.quiesce(unit_id)
        self._log("migrate.quiesced")

        # Durable in-flight record, written BEFORE the unit is transmitted. From
        # the next line on, the target may accept and this process may die; the
        # journal is the only thing that lets a rebooted one discover the window
        # and collapse it to one owner. Written after quiesce so the token it
        # carries is the real one a reclaim will need.
        if self._journal is not None:
            self._journal.open(
                InFlightHandoff(
                    handoff_id=bundle.handoff_id,
                    unit_id=unit_id,
                    kind=self.adapter.bundle_kind,
                    target_crew_id=self.target_crew.crew_id,
                    quiesce_token=token.token,
                )
            )

        try:
            # Step 3+4: transmit and await durable ack.
            ack = await self.receiver.accept(bundle)
        except Exception as exc:
            # Pre-ack failure: retain ownership, un-quiesce, no tombstone.
            await self.adapter.unquiesce(unit_id, token)
            # The outcome IS known here — the source kept it — so settle the
            # journal rather than leaving a boot to re-derive an answer we have.
            # A retained entry would make the next start un-quiesce a unit that
            # is already running.
            if self._journal is not None:
                self._journal.close(bundle.handoff_id)
            self._log(
                "migrate.failed",
                outcome="failed",
                reason=f"transmit/ack failed: {exc}",
                duration=self._clock() - started,
            )
            return MigrationResult(
                outcome="failed", reason=f"transmit failed, source retained: {exc}"
            )

        # Step 5: durable ack in hand — tombstone then release ownership.
        await self.adapter.tombstone(unit_id, self.target_crew, ack.unit_id)
        # Settled: the target owns and the redirect is recorded. Closing last
        # means a crash anywhere above still leaves the entry for reconciliation.
        if self._journal is not None:
            self._journal.close(bundle.handoff_id)
        self._log(
            "migrate.done",
            outcome="migrated",
            remote_unit_id=ack.unit_id,
            duration=self._clock() - started,
        )
        return MigrationResult(outcome="migrated", remote_unit_id=ack.unit_id)

    # -- startup reconciliation (crash window between ack and tombstone) ------

    async def reconcile(
        self, *, handoff_id: str, unit_id: str, token: QuiesceToken | None = None
    ) -> ReconcileResult:
        """Resolve the ack->tombstone crash window to EXACTLY one owner.

        Query the target by handoff_id. If it holds the unit, finish the
        tombstone and the target owns. Otherwise the ack never landed — the
        source un-quiesces and reclaims. Never resumes unconditionally.

        ``lookup`` is part of the receiver contract and is called directly. An
        earlier version fell back to reading a receiver's private ``_accepted``
        map when ``lookup`` was missing — a test fake's shape leaking into
        production, and worse, a silent "target holds nothing" answer for any
        receiver that genuinely could not be queried. A receiver that cannot
        answer must raise, because guessing here is how a unit ends up with two
        owners.
        """
        self._ctx = {
            "unit_id": unit_id,
            "handoff_id": handoff_id,
            "target": self.target_crew.crew_id,
            "source": self.source_crew.crew_id,
        }
        held: AcceptAck | None = await self.receiver.lookup(handoff_id)

        if held is not None:
            await self.adapter.tombstone(unit_id, self.target_crew, held.unit_id)
            self._log("reconcile.target_owns", outcome="migrated", remote_unit_id=held.unit_id)
            return ReconcileResult(owner="target", remote_unit_id=held.unit_id)

        # Target does not hold it — reclaim on the source.
        await self.adapter.unquiesce(unit_id, token or QuiesceToken(unit_id, ""))
        self._log("reconcile.source_owns", outcome="retained")
        return ReconcileResult(owner="source")

    async def reconcile_outstanding(self) -> list[ReconcileResult]:
        """Collapse every journalled ack->tombstone window to exactly one owner.

        Takes NO handoff id: that is the point. A booting gateway cannot be told
        which handoffs were in flight, because the ids died with the process that
        opened them — it must read them back from the journal. This is the entry
        point a startup hook calls; ``reconcile`` remains the single-window
        primitive it drives.

        An entry is closed once resolved, so a settled handoff is not replayed on
        the next boot. One handoff that cannot be resolved must not strand the
        rest: a receiver that refuses to answer is logged and its entry KEPT
        (deliberately — dropping it would silently abandon a window), and the
        sweep continues.
        """
        if self._journal is None:
            return []

        results: list[ReconcileResult] = []
        for entry in self._journal.outstanding():
            token = (
                QuiesceToken(entry.unit_id, entry.quiesce_token)
                if entry.quiesce_token is not None
                else None
            )
            try:
                resolved = await self.reconcile(
                    handoff_id=entry.handoff_id, unit_id=entry.unit_id, token=token
                )
            except Exception as exc:
                # Keep the entry: an unanswerable receiver is a window still
                # open, and the unreconciled-handoffs band is meant to see it.
                self._ctx = {"unit_id": entry.unit_id, "handoff_id": entry.handoff_id}
                self._log("reconcile.unresolved", outcome="unknown", reason=str(exc))
                continue
            self._journal.close(entry.handoff_id)
            results.append(resolved)
        return results
