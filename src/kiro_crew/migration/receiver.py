"""Durable target-side migration receiver (slice 1, circle 2 — issue #7577).

``LocalMigrationReceiver`` is the concrete target endpoint the coordinator's
transmit step reaches over the tunnel. Its contract (design.md → Components):

  * ``preflight`` — pure, read-only capability/reference probe; writes nothing.
  * ``accept``    — validate -> persist -> fsync -> ack, and DEDUPE on
                    ``handoff_id`` so a retransmit returns the same unit id
                    rather than creating a second unit (Req 2.7).
  * ``lookup``    — resolve a ``handoff_id`` to its held unit, for the source's
                    startup reconciliation of the ack->tombstone crash window
                    (Req 2.6).

The materialization step (turning a payload into a live unit on this host) is
injected as ``materialize`` so the receiver stays unit-kind-agnostic and
testable — the real wiring passes the per-kind adapter's ``materialize``.

Durability: each accepted handoff is a JSON file written with
``atomic_write(..., fsync=True)``, so the ack the source hears is backed by a
record that survives a crash and a fresh receiver instance.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.migration import protocol as P

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequirementProbe:
    """Injected target-side checks for a bundle's HostRequirements.

    Each callable takes the requirement's ``identity`` and returns True when the
    target can satisfy it. Injected so preflight is pure and testable; the real
    probe wraps agent-registry / crons-dir / command-policy lookups.

    The three original fields covered three of the seven kinds
    ``HostRequirement`` names, and preflight used to ``continue`` past the rest —
    so ``mcp_server``, ``project_checkout`` and ``git_repo`` were admitted
    unchecked even though the session and task-run adapters emit them as
    blocking. They are declared here so the gap is visible in the type rather
    than hidden in a loop, and ``None`` means "cannot verify" (which preflight
    reports at the requirement's own severity), never "satisfied".
    """

    agent_exists: Callable[[str], bool]
    script_path_ok: Callable[[str], bool]
    command_allowed: Callable[[str], bool]
    mcp_server_exists: Callable[[str], bool] | None = None
    project_checkout_ok: Callable[[str], bool] | None = None
    git_repo_ok: Callable[[str], bool] | None = None


class LocalMigrationReceiver(P.MigrationReceiver):
    # Bundle formats this receiver understands. A version it does not know is
    # REFUSED rather than half-understood: a newer source may have moved a field
    # this code would then silently drop, and the session kind is v2 because it
    # carries session_transfer's Layer A + Layer B envelope, which a v1 reader
    # would misread as transcript-only.
    SUPPORTED_VERSIONS: dict[str, frozenset[int]] = {
        "cron": frozenset({1}),
        "session": frozenset({2}),
        "taskrun": frozenset({1}),
    }

    def __init__(
        self,
        *,
        store_dir: Path | str,
        materialize: Callable[[dict], str],
        requirement_probe: "RequirementProbe | None" = None,
        audit_sink=None,
    ) -> None:
        self._dir = Path(store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._materialize = materialize
        self._probe = requirement_probe
        # Req 3.5 wants the handoff on BOTH crews' records. The coordinator
        # covers the source; this is the target's half.
        self._audit_sink = audit_sink

    def _audit(self, event: str, bundle: P.MigrationBundle, **fields) -> None:
        entry = {
            "event": event,
            "handoff_id": bundle.handoff_id,
            "bundle_kind": bundle.bundle_kind,
            "bundle_version": bundle.bundle_version,
            "source": bundle.source_crew.crew_id,
            **fields,
        }
        sink = self._audit_sink if self._audit_sink is not None else P._sel_audit
        try:
            sink(entry)
        except Exception:  # pragma: no cover - audit must never break accept
            logger.debug("receiver audit sink failed for %s", event, exc_info=True)

    def _record_path(self, handoff_id: str) -> Path:
        # handoff_id is an allocated hex/opaque token; keep the filename simple
        # and collision-free by using it verbatim with a .json suffix.
        safe = "".join(c for c in handoff_id if c.isalnum() or c in "-_") or "_"
        return self._dir / f"{safe}.json"

    async def preflight(self, bundle: P.MigrationBundle) -> P.PreflightReport:
        """Read-only probe. Checks each HostRequirement against the target and
        turns anything it cannot confirm into a BLOCKING finding. Persists
        nothing.

        This method FAILS CLOSED, and that is the whole point of it. An earlier
        version returned an empty report when no probe was injected, described as
        "conservative" — but callers decide by reading ``report.blocked``, and no
        findings means False, i.e. a green light. With no probe wired in
        production, preflight admitted every migration to every target regardless
        of what the unit needed. A gate that refuses nothing is worse than no
        gate, because the surfaces above it believe a check happened.

        Refusals are PROPORTIONAL, so closing the hole does not become a
        fail-closed for everyone: a bundle with no requirements has nothing to
        verify and still passes without a probe. Only an actual requirement that
        cannot be confirmed blocks, and the finding says whether it was
        *unverifiable* or *verified absent* — different operator fixes (wire the
        probe vs install the thing).
        """
        findings: list[P.Finding] = []

        if self._probe is None:
            # Nothing to verify means nothing to refuse.
            for req in bundle.requirements:
                findings.append(
                    P.Finding(
                        kind=req.kind,
                        detail=(
                            f"cannot verify required {req.kind} {req.identity!r}: "
                            "this receiver has no requirement probe configured"
                        ),
                        # The REQUIREMENT declares how much it matters, not this
                        # branch. An unverifiable advisory requirement is still
                        # advisory -- hardcoding "blocking" here would turn every
                        # session's advisory agent hint into a refusal.
                        severity=req.severity,
                        detail_key=req.identity,
                    )
                )
            return P.PreflightReport(findings=findings)

        checks = {
            "agent": (self._probe.agent_exists, "agent"),
            "script_path": (self._probe.script_path_ok, "script_path"),
            "command_policy": (self._probe.command_allowed, "command_policy"),
            "mcp_server": (self._probe.mcp_server_exists, "mcp_server"),
            "project_checkout": (self._probe.project_checkout_ok, "project_checkout"),
            "git_repo": (self._probe.git_repo_ok, "git_repo"),
        }
        for req in bundle.requirements:
            entry = checks.get(req.kind)
            check = entry[0] if entry is not None else None
            if check is None:
                # A kind this receiver cannot check is NOT a kind it may admit.
                # This covers both an unrecognised kind and a recognised one
                # whose probe callable was not injected: in either case the
                # answer is unknown, and unknown is not a pass. Previously a
                # bare `continue` here admitted it silently, so a kind added
                # later would inherit that silence rather than failing loudly.
                findings.append(
                    P.Finding(
                        kind=req.kind,
                        detail=(
                            f"cannot verify required {req.kind} {req.identity!r}: "
                            f"this receiver has no check for requirement kind "
                            f"{req.kind!r}"
                        ),
                        severity=req.severity,
                        detail_key=req.identity,
                    )
                )
                continue
            kind = entry[1]  # type: ignore[index]
            if not check(req.identity):
                findings.append(
                    P.Finding(
                        kind=kind,
                        detail=f"target cannot satisfy required {req.kind} " f"{req.identity!r}",
                        severity="blocking",
                        detail_key=req.identity,
                    )
                )
        return P.PreflightReport(findings=findings)

    async def accept(self, bundle: P.MigrationBundle) -> P.AcceptAck:
        # validate: the payload must be non-empty and the bundle must carry the
        # identity fields the protocol owns. Unit identity lives on the BUNDLE
        # (handoff_id + bundle_kind), NOT inside the payload — an adapter's
        # allow-list legitimately drops the source-local id (the target
        # allocates its own), so requiring payload['unit_id'] here would reject
        # every real bundle.
        if not bundle.payload:
            raise ValueError("bundle payload is empty")
        if not bundle.handoff_id:
            raise ValueError("bundle missing handoff_id")

        # Format compatibility. Refuse an unknown kind or version rather than
        # reading it optimistically — a partially-understood bundle materializes
        # a unit that is quietly missing state.
        known = self.SUPPORTED_VERSIONS.get(bundle.bundle_kind)
        if known is None:
            raise ValueError(
                f"unsupported bundle kind {bundle.bundle_kind!r}; this crew "
                f"understands {sorted(self.SUPPORTED_VERSIONS)}"
            )
        if bundle.bundle_version not in known:
            raise ValueError(
                f"unsupported bundle version {bundle.bundle_version} for kind "
                f"{bundle.bundle_kind!r}; this crew understands {sorted(known)}"
            )

        # Defence in depth: the source scans before sending, but a target must
        # not trust that it did. A compromised or older source is exactly the
        # case this second scan exists for. The message names the matched
        # PATTERN, never the matched value.
        secrets = P.scan_for_secrets(bundle.payload)
        if secrets:
            self._audit(
                "accept.refused", bundle, outcome="denied", reason="credential material in payload"
            )
            raise ValueError(
                "refusing bundle: payload carries credential material "
                f"({', '.join(sorted({f.detail for f in secrets}))})"
            )

        # dedupe on handoff_id — a retransmit returns the existing unit
        existing = await self.lookup(bundle.handoff_id)
        if existing is not None:
            self._audit("accept.replayed", bundle, outcome="allowed", unit_id=existing.unit_id)
            return existing

        # materialize the live unit, then durably record the mapping.
        # A real adapter's materialize is async (it may touch a store or a
        # scheduler), while a simple injected callable is not -- accept both so a
        # caller never has to mirror adapter logic in a sync wrapper. Mirroring
        # is how a test ends up asserting against its own copy of the behaviour
        # instead of the behaviour.
        unit_id = self._materialize(bundle.payload)
        if inspect.isawaitable(unit_id):
            unit_id = await unit_id
        record = {
            "handoff_id": bundle.handoff_id,
            "unit_id": unit_id,
            "bundle_kind": bundle.bundle_kind,
            "created_ts": bundle.created_ts,
        }
        atomic_write(self._record_path(bundle.handoff_id), json.dumps(record), fsync=True)
        # Audited only AFTER the fsync: the ack the source will hear is backed by
        # a durable record, so the audit line must not claim it earlier.
        self._audit("accept.persisted", bundle, outcome="allowed", unit_id=unit_id)
        return P.AcceptAck(unit_id=unit_id)

    async def lookup(self, handoff_id: str) -> P.AcceptAck | None:
        path = self._record_path(handoff_id)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        uid = record.get("unit_id")
        return P.AcceptAck(unit_id=uid) if uid else None


def build_requirement_probe(
    *,
    list_agent_names: Callable[[], list[str]] | None = None,
    crons_dir: Path | str | None = None,
    command_allowed: Callable[[str], bool] | None = None,
    mcp_server_exists: Callable[[str], bool] | None = None,
) -> RequirementProbe:
    """Assemble a RequirementProbe backed by real target-side lookups.

    Each dependency is injectable (tests pass fakes); the defaults wire the
    real Kiro Crew sources:
      * ``list_agent_names`` -> ``agent_discovery.list_agents`` names,
      * ``crons_dir``        -> the target's crons directory,
      * ``command_allowed``  -> the caller's command policy (no default: a
        missing policy denies, since a command whose policy is unknown must
        not be assumed safe).

    Requirement semantics:
      * agent: the identity must be a known agent name (Req 4.6 — refuse rather
        than fall back to a default).
      * script_path: the ``<path>:func`` identity's path must resolve UNDER the
        target's crons dir AND exist (Req 4.4).
      * command_policy: delegated to ``command_allowed`` (Req 4.5).
    """
    if list_agent_names is None:

        def list_agent_names():  # pragma: no cover - real wiring
            from kiro_crew.agent_discovery import list_agents

            return [a.name for a in list_agents()]

    crons_root = (
        Path(crons_dir) if crons_dir is not None else Path.home() / ".kiro" / "crew" / "crons"
    )

    def agent_exists(identity: str) -> bool:
        return identity in set(list_agent_names())

    def script_path_ok(identity: str) -> bool:
        # identity is "<path>:func" — split off the callable suffix.
        path_part = identity.rsplit(":", 1)[0] if ":" in identity else identity
        try:
            p = Path(path_part).expanduser().resolve()
            root = crons_root.expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        # must exist AND be under the target's crons dir
        if not p.exists():
            return False
        return root == p or root in p.parents

    def cmd_allowed(identity: str) -> bool:
        return bool(command_allowed(identity)) if command_allowed else False

    def project_checkout_ok(identity: str) -> bool:
        # Rematerializing a checkout is explicitly out of scope, so the honest
        # check is whether the path is already there on the target.
        try:
            return Path(identity).expanduser().is_dir()
        except (OSError, RuntimeError):
            return False

    def git_repo_ok(identity: str) -> bool:
        # A git repo, not merely a directory: a task run resumed against a path
        # that happens to exist but is not the checkout would run against the
        # wrong tree. `.git` is a dir in a normal clone and a FILE in a worktree
        # or submodule, so accept either rather than rejecting worktrees.
        try:
            root = Path(identity).expanduser()
            return root.is_dir() and (root / ".git").exists()
        except (OSError, RuntimeError):
            return False

    def mcp_present(identity: str) -> bool:
        # No default, same reasoning as command_allowed: the real lookup is
        # async and sits behind a capability manager that is itself fail-closed
        # when unavailable, so there is no honest synchronous default. An MCP
        # server whose presence is unknown must not be assumed present -- a
        # session arriving without its tools looks like the session broke.
        return bool(mcp_server_exists(identity)) if mcp_server_exists else False

    return RequirementProbe(
        agent_exists=agent_exists,
        script_path_ok=script_path_ok,
        command_allowed=cmd_allowed,
        mcp_server_exists=mcp_present,
        project_checkout_ok=project_checkout_ok,
        git_repo_ok=git_repo_ok,
    )
