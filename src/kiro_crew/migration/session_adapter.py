"""Session migration adapter — plan side.

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


class SessionMigrationAdapter:
    """MigrationUnitAdapter for chat sessions — plan side (Task 3.1).

    Assembles the session analysis pieces — ``classify_session_portability``,
    ``carry_session_ledger``, ``layer_b_fidelity_findings`` — behind the generic
    seam, so one plan implementation serves every unit kind.

    The dashboard-runtime touchpoint is INJECTED, keeping this adapter pure and
    testable: ``bundle_builder(session_id) -> dict`` wraps
    ``build_transfer_bundle_async`` (Layer A transcript + Layer B context).

    ``serialize`` runs the builder, strips + reports non-portable references
    (project/model/workspace/agent), and stashes the findings on
    ``last_findings`` for the UI to surface (Req 5.6).

    Quiesce, the monitor-loop handoff and the tombstone are NOT here: they are
    steps of the transfer, and they land with the change that wires it. A
    session's quiesce in particular is only meaningful when the gateway can
    actually block new turns on the slot, which is the same change.
    """

    bundle_kind = "session"
    bundle_version = 2  # matches session_transfer's Layer-B bundle_version

    def __init__(self, *, session_id: str, bundle_builder) -> None:
        # A None builder is a wiring error, not a degraded mode: without it there
        # is nothing to plan from, and deferring the failure turns it into a
        # confusing TypeError inside serialize.
        if bundle_builder is None:
            raise ValueError("SessionMigrationAdapter requires a bundle_builder callable")
        self._sid = session_id
        self._build = bundle_builder
        self.last_findings: list[P.Finding] = []

    async def describe(self, unit_id: str) -> dict:
        return {"unit_id": unit_id, "kind": self.bundle_kind}

    async def requirements(self, unit_id: str) -> list[P.HostRequirement]:
        """Derive what the target must have for this session to continue.

        An empty list would leave a session plan with nothing to check, so the
        requirement machinery would be unreachable for the session kind.

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
