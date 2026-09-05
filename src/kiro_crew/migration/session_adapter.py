"""Session migration adapter (slice 3 of issue #7577).

First circle: the non-portability classifier only. When a session moves to
another crew, references that are meaningful only on the source host must be
DROPPED and REPORTED (Req 5.5-5.6) — a Mac worktree path does not exist on a
Linux EC2 host, so carrying it would mislead rather than help.

Rules (design.md → Per-Unit → Session):
  * ``project`` / ``model`` / ``workspace`` — hard-dropped.
  * ``agent`` — hint-only: also dropped from the durable payload, but the
    target may use it as a resolution hint.
Each dropped reference that was actually present yields ONE advisory Finding,
so the user is told what will not transfer instead of it vanishing silently.

The dashboard-runtime reuse (build_transfer_bundle_async, quiesce, monitor-loop
disarm/re-arm, tombstone) is deliberately a later circle — this layer is pure
and independently testable.
"""

from __future__ import annotations

import time

from kiro_crew.migration import protocol as P

# References that do not survive a crew-to-crew move.
SESSION_NONPORTABLE: tuple[str, ...] = ("project", "model", "workspace", "agent")

# The Finding.kind to report for each non-portable reference.
_KIND = {
    "project": "project_checkout",
    "model": "model",
    "workspace": "workspace",
    "agent": "agent",
}


def classify_session_portability(meta: dict) -> tuple[dict, list[P.Finding]]:
    """Split session metadata into the portable subset + reports for the rest.

    Returns ``(portable, findings)`` where ``portable`` is ``meta`` minus every
    non-portable reference, and ``findings`` has one advisory Finding per
    non-portable reference that was actually present in ``meta``.
    """
    portable = {k: v for k, v in meta.items() if k not in SESSION_NONPORTABLE}
    findings: list[P.Finding] = []
    for key in SESSION_NONPORTABLE:
        if key not in meta:
            continue  # only report what was present
        hint = " (kept as a resolution hint)" if key == "agent" else ""
        findings.append(
            P.Finding(
                kind=_KIND[key],
                detail=f"session reference '{key}' is not portable and was dropped; "
                f"it will not transfer to the target crew{hint}",
                severity="advisory",
                detail_key=key,
            )
        )
    return portable, findings


# Ledger fields that are pure working state and travel as-is (Req 5.4).
_LEDGER_WORKING_STATE: tuple[str, ...] = ("goal", "phase", "next", "tried", "events")


def _is_host_local_path(value) -> bool:
    """True when an artifact value looks like an absolute filesystem path.

    A worktree path is the same class of reference as the dropped ``project``:
    it names a location on the SOURCE host and does not exist on the target.
    Branch names, PR numbers and other opaque handles are portable and stay.
    """
    if not isinstance(value, str):
        return False
    return (
        value.startswith("/")
        or value.startswith("~")
        or (len(value) > 2 and value[1] == ":" and value[2] in "\\/")  # C:\ or C:/
    )


def carry_session_ledger(state: dict) -> tuple[dict, list[P.Finding]]:
    """Carry the session ledger as durable working state (Task 3.3 / Req 5.4).

    ``goal`` / ``phase`` / ``next`` / ``tried`` (plus the event log) are pure
    reasoning state — host-independent, and exactly what makes a cold resume on
    the target coherent, so they travel verbatim.

    ``artifacts`` is mixed: a branch name or PR number is portable, an absolute
    worktree path is not. Host-local paths are DROPPED and REPORTED, one advisory
    finding each, consistent with the non-portable-reference rule — and the
    finding names the key, never the path, so a report cannot leak a local
    filesystem layout.
    """
    carried: dict = {
        "goal": state.get("goal", "") or "",
        "phase": state.get("phase", "") or "",
        "next": state.get("next", "") or "",
        "tried": list(state.get("tried") or []),
        "events": list(state.get("events") or []),
        "artifacts": {},
    }
    findings: list[P.Finding] = []
    for key, value in (state.get("artifacts") or {}).items():
        if _is_host_local_path(value):
            findings.append(
                P.Finding(
                    kind="project_checkout",
                    detail=f"ledger artifact '{key}' is a host-local path and was "
                    f"dropped; it does not exist on the target crew",
                    severity="advisory",
                    detail_key=key,
                )
            )
            continue
        carried["artifacts"][key] = value
    return carried, findings


def layer_b_fidelity_findings(bundle: dict) -> list[P.Finding]:
    """Warn when Layer B is missing and the move degrades (Task 3.6 / Req 5.3).

    Layer B is the kiro-cli context window itself. Without it the target can
    only rebuild context from the visible transcript, which is a real loss of
    fidelity — advisory rather than blocking, because a transcript-prefix resume
    is still useful and the user may legitimately want it. What must not happen
    is the degradation being silent.
    """
    if bundle.get("layer_b"):
        return []
    return [
        P.Finding(
            kind="session_context",
            detail="Layer B (the model context window) is unavailable; the move "
            "degrades to transcript-prefix fidelity — the target rebuilds "
            "context from the visible transcript instead of resuming it",
            severity="advisory",
            detail_key="layer_b",
        )
    ]


class SessionMonitorHandoff:
    """Enforces the monitor-loop invariant across a session move (Req 5.8).

    An armed monitor loop on BOTH crews would double-fire. The only safe
    ordering is: disarm on the source at quiesce, then arm on the target ONLY
    after a durable ack — so at no observable point is a loop armed on both.
    On a pre-ack failure the source re-arms and the target is never touched.

    The loop machinery is injected (``controller``) so this invariant is pure
    and testable; the real controller wraps the dashboard monitor service.
    The controller must expose ``disarm_source()``, ``arm_target(spec)``,
    ``source_armed``, ``target_armed`` and hold the disarmed spec in
    ``saved_spec``.
    """

    def __init__(self, controller) -> None:
        self._c = controller

    def quiesce_source(self) -> None:
        """Disarm the source loop. Target is NOT armed here — never both."""
        self._c.disarm_source()

    def rearm_target_after_ack(self) -> None:
        """Arm the target loop, but only if the source actually had one.

        Called strictly AFTER the durable ack, so the source is already
        disarmed — the two are never armed simultaneously.
        """
        if self._c.saved_spec is not None:
            self._c.arm_target(self._c.saved_spec)

    def rearm_source_on_failure(self) -> None:
        """Roll back: a pre-ack failure reclaims the source loop, target stays off."""
        if self._c.saved_spec is not None:
            self._c.source_armed = True


class SessionQuiesce:
    """Quiesce a chat session for migration (Req 5.7).

    The ordering is load-bearing: BLOCK new turns first, THEN drain any turn
    already in flight. Blocking first means no new turn can start during the
    drain, so the drain terminates. If the in-flight turn will not drain within
    the budget, quiesce REFUSES (MidRunError) and un-blocks the session — a
    session left blocked-but-not-migrated is the failure this must avoid, and
    matches the invariant that a pre-ack failure leaves the source usable.

    The slot machinery is injected (``controller``) so this is pure and
    testable; the real controller wraps the dashboard slot. It must expose
    ``block_new_turns()``, ``allow_new_turns()`` and
    ``drain_in_flight(timeout) -> bool``.
    """

    def __init__(self, controller, *, drain_timeout: float = 30.0) -> None:
        self._c = controller
        self._drain_timeout = drain_timeout

    def quiesce(self, session_id: str) -> P.QuiesceToken:
        self._c.block_new_turns()
        if not self._c.drain_in_flight(self._drain_timeout):
            self._c.allow_new_turns()  # roll back — never leave it blocked
            raise P.MidRunError(f"session {session_id!r} has an in-flight turn that did not drain")
        return P.QuiesceToken(unit_id=session_id, token="session-quiesced")

    def unquiesce(self, session_id: str, token: P.QuiesceToken) -> None:
        self._c.allow_new_turns()


class SessionTombstone:
    """Tombstone a migrated source session (Task 3.7 / Req 5.9, 5.11).

    The source slot is RETAINED and its transcript stays readable — a user who
    looks for the old session still finds it, now displaying its new home — but
    it refuses new turns, because the live session is the target's now. This is
    'retained, readable, redirected', never deleted.

    The slot is injected; it must expose ``block_new_turns()`` and a
    settable ``new_home`` (the Tombstone shown in the UI). ``transcript_readable``
    is left untouched — tombstoning never disturbs the transcript.
    """

    def __init__(self, slot) -> None:
        self._slot = slot
        self._tombstones: dict[str, P.Tombstone] = {}

    def tombstone(self, session_id: str, target: P.CrewRef, remote_id: str) -> None:
        ts = P.Tombstone(
            unit_kind="session",
            target_crew=target,
            remote_unit_id=remote_id,
            migrated_ts=time.time(),
        )
        self._slot.block_new_turns()  # refuses new turns; transcript untouched
        self._slot.new_home = ts  # UI shows where it went
        self._tombstones[session_id] = ts

    def tombstone_of(self, session_id: str) -> P.Tombstone:
        return self._tombstones[session_id]


class SessionMigrationAdapter:
    """MigrationUnitAdapter for chat sessions (Task 3.1).

    Assembles the tested session pieces — SessionQuiesce, SessionMonitorHandoff,
    SessionTombstone, classify_session_portability — behind the generic seam,
    so the coordinator drives a session move with no session-specific knowledge.

    The two dashboard-runtime touchpoints are INJECTED, keeping this adapter
    pure and testable:
      * ``bundle_builder(session_id) -> dict`` wraps
        ``build_transfer_bundle_async`` (Layer A transcript + Layer B context).
      * ``importer(payload) -> str`` wraps ``api_chat_slot_import`` on the
        target, returning the new session id.

    ``serialize`` runs the builder, strips + reports non-portable references
    (project/model/workspace/agent), and stashes the findings on
    ``last_findings`` for the coordinator/UI to surface (Req 5.6).
    """

    bundle_kind = "session"
    bundle_version = 2  # matches session_transfer's Layer-B bundle_version

    def __init__(
        self,
        *,
        session_id: str,
        controller,
        bundle_builder,
        importer,
        monitor_controller=None,
        registry=None,
    ) -> None:
        self._sid = session_id
        self._quiesce = SessionQuiesce(controller)
        self._tomb = SessionTombstone(controller)
        # Durable tombstone registry (Req 7.3). ``slot.new_home`` alone is memory
        # only, so a restart lost where a moved session went; and no listing
        # surface can read it without holding this adapter instance.
        self._registry = registry
        self._monitor = (
            SessionMonitorHandoff(monitor_controller) if monitor_controller is not None else None
        )
        self._build = bundle_builder
        self._import = importer
        self.last_findings: list[P.Finding] = []

    async def describe(self, unit_id: str) -> dict:
        return {"unit_id": unit_id, "kind": self.bundle_kind}

    async def requirements(self, unit_id: str) -> list[P.HostRequirement]:
        """Derive what the target must have for this session to continue.

        Previously this returned `[]`, which meant a session preflight had
        nothing to check and could therefore never refuse — the requirement
        machinery existed but was unreachable for the session kind.

        Severity follows the design's non-portability rules rather than being
        uniform:

        * ``agent`` — advisory. The existing transfer path treats the agent as a
          hint, so an absent one degrades resolution; it does not lose the work.
        * ``project_checkout`` — advisory. Rematerialization is explicitly out of
          scope, so a missing checkout is *reported as a requirement*, which is
          the whole reason HostRequirement names things instead of moving them.
        * ``mcp_server`` — blocking. A session whose tools do not exist on the
          target cannot continue the work it was doing; silently arriving without
          them looks like the session broke.
        """
        raw = self._build(unit_id)
        reqs: list[P.HostRequirement] = []

        agent = (raw.get("agent") or "").strip()
        if agent:
            reqs.append(P.HostRequirement(kind="agent", identity=agent, severity="advisory"))

        project = (raw.get("project") or "").strip()
        if project:
            reqs.append(
                P.HostRequirement(kind="project_checkout", identity=project, severity="advisory")
            )

        for server in raw.get("mcp_servers") or []:
            name = (server or "").strip() if isinstance(server, str) else ""
            if name:
                reqs.append(
                    P.HostRequirement(kind="mcp_server", identity=name, severity="blocking")
                )
        return reqs

    async def quiesce(self, unit_id: str) -> P.QuiesceToken:
        token = self._quiesce.quiesce(unit_id)
        if self._monitor is not None:
            self._monitor.quiesce_source()  # disarm loop at quiesce (5.8)
        return token

    async def unquiesce(self, unit_id: str, token: P.QuiesceToken) -> None:
        self._quiesce.unquiesce(unit_id, token)
        if self._monitor is not None:
            self._monitor.rearm_source_on_failure()

    async def serialize(self, unit_id: str) -> dict:
        raw = self._build(unit_id)
        portable, findings = classify_session_portability(raw)
        # Layer B may be absent (a mid-turn bundle skips it) — report the
        # fidelity degradation rather than letting it pass silently (Req 5.3).
        findings = findings + layer_b_fidelity_findings(raw)
        # The ledger is durable working state; carry it, reporting any
        # host-local artifact path it holds (Req 5.4).
        if raw.get("ledger"):
            carried, ledger_findings = carry_session_ledger(raw["ledger"])
            portable["ledger"] = carried
            findings = findings + ledger_findings
        self.last_findings = findings
        return portable

    async def materialize(self, payload: dict) -> str:
        remote_id = self._import(payload)
        if self._monitor is not None:
            self._monitor.rearm_target_after_ack()  # arm target only post-ack
        if self._registry is not None:
            # Live here now — drop any tombstone from a previous move away
            # (the move-back case, Req 7.4).
            self._registry.clear(self.bundle_kind, remote_id)
        return remote_id

    async def tombstone(self, unit_id: str, target: P.CrewRef, remote_id: str) -> None:
        self._tomb.tombstone(unit_id, target, remote_id)
        if self._registry is not None:
            self._registry.record(self.bundle_kind, unit_id, self._tomb.tombstone_of(unit_id))


def build_session_adapter(
    *,
    session_id: str,
    controller,
    bundle_builder,
    importer,
    monitor_controller=None,
    registry=None,
) -> "SessionMigrationAdapter":
    """Assemble a SessionMigrationAdapter from the dashboard-provided callables.

    This is the single wiring point the live dashboard fills in. It exists so
    the runtime dependency shapes are pinned in code — the rest of the session
    migration path is pure and tested against fakes, and only these two
    callables reach the loop-sensitive dashboard machinery:

      * ``bundle_builder(session_id: str) -> dict`` — MUST wrap
        ``dashboard.session_transfer.build_transfer_bundle_async`` (Layer A
        transcript + Layer B kiro-cli context, bundle_version 2). The dashboard
        supplies it already bound to its ``DashboardState`` and the source
        ``_ChatSlot``; the adapter never sees those types.
      * ``importer(payload: dict) -> str`` — MUST wrap the target's
        session-import path (``api_chat_slot_import``'s core), returning the new
        session id. Because that endpoint is an aiohttp handler, the dashboard
        is responsible for adapting it to this plain callable shape (build the
        request / call the extracted core) — that adaptation is the remaining
        live-environment wiring, out of scope for the pure layer here.
      * ``controller`` — the source slot, satisfying SessionQuiesce's
        (block_new_turns / allow_new_turns / drain_in_flight) and
        SessionTombstone's (block_new_turns / new_home) contracts.
      * ``monitor_controller`` — optional; the monitor-loop machinery for the
        disarm-source / arm-target-after-ack invariant (Req 5.8).

    Both callables are REQUIRED: a None builder or importer is a wiring error,
    not a degraded mode, so it is rejected loudly rather than failing later
    mid-migration.
    """
    if bundle_builder is None:
        raise ValueError("build_session_adapter requires a bundle_builder callable")
    if importer is None:
        raise ValueError("build_session_adapter requires an importer callable")
    return SessionMigrationAdapter(
        session_id=session_id,
        controller=controller,
        bundle_builder=bundle_builder,
        importer=importer,
        monitor_controller=monitor_controller,
        registry=registry,
    )
