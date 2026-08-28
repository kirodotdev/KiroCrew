"""Whether a backend's tool decisions reach Kiro Crew's PreToolUse gate.

One place resolves the verdict for every backend, so the refusal message, the
doctor row and the in-band check cannot disagree about why a session was allowed
or refused.

The gate itself — the bundled denied-command rules, the sensitive-path block, the
governance ceiling — runs only from ``HookManager.on_tool_call``, reached only
from the permission-request branch of the dispatch parser. A backend that does not
send ``session/request_permission`` per tool call is a backend where none of those
controls execute, so "does it ask?" is a security question, not a compatibility
one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from kiro_crew.acp import backends as acp_backends
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE

logger = logging.getLogger(__name__)


class _SeededSettings(Protocol):
    """Claude / OpenCode module surface used by the SEEDED_SETTINGS dispatch."""

    def ensure_routed_settings(self, work_dir: Path | str) -> bool: ...  # noqa: E704

    def routing_verdict(self, work_dir: Path | str) -> tuple[Verdict, str]: ...  # noqa: E704

    def remediation_hint(self, work_dir: Path | str) -> str: ...  # noqa: E704


class ToolGateUnroutable(Exception):
    """Raised when a backend's tool calls would not reach the PreToolUse gate.

    Deliberately NOT retryable: the condition is a configuration fact, so retrying
    re-reads the same file and refuses again while consuming a reconnect budget
    that exists for transport faults.

    Plain ``Exception`` rather than an ``AcpError`` subclass because ``AcpError``
    lives in ``acp.client``, which imports THIS module — subclassing it here is an
    import cycle. ``AcpClient._spawn`` translates this into the public
    ``AcpToolGateUnroutable(AcpError)`` so branch-less callers keep degrading to
    their generic ACP-error handling.
    """


def _seeded_settings_module(backend: str) -> _SeededSettings | None:
    """The adapter module that owns this backend's SEEDED_SETTINGS files.

    Claude and OpenCode share the routing mechanism but own different
    files. Selecting the module here keeps identity positive (H5) without
    merging the seeders — each module still writes and reads its own shape.
    An added SEEDED_SETTINGS harness that is not named here stays None.
    """
    if backend == ACP_BACKEND_OPENCODE:
        from kiro_crew.acp import opencode as opencode_backend

        return opencode_backend
    if backend == ACP_BACKEND_CLAUDE:
        from kiro_crew.acp import claude as claude_backend

        return claude_backend
    return None


def _seed_routed_settings(backend: str, work_dir: Path | str) -> None:
    """Write the conservative permission seed for ``SEEDED_SETTINGS`` backends.

    Spawn / ``enforce`` only. A dashboard GET or doctor probe must not call this:
    OpenCode and Claude become ROUTED by this write, and doing it on every
    Settings load creates files under ``config_dir()/workspace``.
    """
    descriptor = acp_backends.descriptor_for(backend)
    if descriptor.routing is not acp_backends.Routing.SEEDED_SETTINGS:
        return
    seeded = _seeded_settings_module(backend)
    if seeded is not None:
        seeded.ensure_routed_settings(work_dir)


def routing_verdict(
    backend: str,
    work_dir: Path | str,
    *,
    registry_adapters: Mapping[str, Any] | None = None,
) -> tuple[Verdict, str]:
    """Read-only routing probe. Never writes Claude or OpenCode settings.

    Dispatches on the descriptor's ``routing`` rather than the backend id, so a
    new backend declaring an existing routing mechanism needs no change here.
    ``SEEDED_SETTINGS`` reads the file back only; the seed that makes those
    backends ROUTED lives on ``enforce`` / ``_spawn``.
    """
    descriptor = acp_backends.descriptor_for(
        backend,
        registry_adapters=registry_adapters,
    )

    if descriptor.routing is acp_backends.Routing.AGENT_SPEC:
        # kiro-cli and KAS are made to ask by naming an agent on the spawn, so the
        # precondition holds by construction and there is nothing to probe.
        return (Verdict.ROUTED, "the spawn names an agent")

    if descriptor.routing is acp_backends.Routing.SEEDED_SETTINGS:
        seeded = _seeded_settings_module(backend)
        if seeded is not None:
            return seeded.routing_verdict(work_dir)
        return (
            Verdict.INDETERMINATE,
            f"no settings seed for {descriptor.label}",
        )

    if descriptor.routing is acp_backends.Routing.SESSION_CONFIG:
        return (
            Verdict.ROUTED,
            f"the client enforces {descriptor.permission_config_id}="
            f"{descriptor.permission_config_value} before the first prompt",
        )

    if descriptor.routing is acp_backends.Routing.PERMISSION_REQUEST:
        # goose / pi send session/request_permission for privileged tools on
        # the ACP path. We do not advertise fs/* or terminal/*, so file I/O
        # stays in the adapter; the permission frame is still what reaches
        # HookManager.on_tool_call. OpenCode is SEEDED_SETTINGS instead: its own
        # default is permissive, so a seed+readback is the probe, not this
        # structural ROUTED.
        #
        # goose 1.47+ defaults every session to mode ``auto`` (auto-approve).
        # Kiro Crew has no such permission mode. The client pins
        # ``session/set_mode`` to ``approve`` after session/new — this probe
        # names that pin so doctor/GET do not claim a structural ask that
        # the unpinned default would skip.
        if backend == ACP_BACKEND_GOOSE:
            return (
                Verdict.ROUTED,
                "the client pins goose session mode approve after session/new "
                "(goose defaults to auto, which auto-approves tools)",
            )
        return (
            Verdict.ROUTED,
            "the adapter asks per privileged tool via session/request_permission",
        )

    if descriptor.routing is acp_backends.Routing.UNVERIFIED:
        # Everything discovered through the registry lands here until someone
        # establishes how it can be made to ask. INDETERMINATE, never BYPASSED:
        # Kiro Crew is not claiming the adapter ignores permissions, only that it
        # has no evidence either way — and absent evidence the gate must not be
        # reported as armed.
        return (
            Verdict.INDETERMINATE,
            "Kiro Crew has not established how this adapter routes tool calls",
        )

    # Unreachable while Routing is exhaustive; fail closed rather than assume.
    return (
        Verdict.INDETERMINATE,
        f"no routing probe for {descriptor.routing.value!r}",
    )


def resolve_verdict(backend: str, work_dir: Path | str) -> tuple[Verdict, str]:
    """Return the routing verdict for ``backend`` plus an operator-facing reason.

    Read-only. The seed that makes ``SEEDED_SETTINGS`` backends ROUTED is
    ``enforce`` / ``_spawn``; this probe must agree with ``GET /api/acp-backends``
    and ``kirocrew doctor`` without creating files.
    """
    return routing_verdict(backend, work_dir)


def remediation_for(backend: str, work_dir: Path | str) -> str:
    """The concrete change an operator must make, per backend."""
    descriptor = acp_backends.descriptor_for(backend)
    if descriptor.routing is acp_backends.Routing.SEEDED_SETTINGS:
        seeded = _seeded_settings_module(backend)
        if seeded is not None:
            return seeded.remediation_hint(work_dir)
        return ""
    if descriptor.routing is acp_backends.Routing.SESSION_CONFIG:
        return (
            f"Install a compatible {descriptor.label} adapter that advertises "
            f"ACP session config option {descriptor.permission_config_id!r} with "
            f"value {descriptor.permission_config_value!r}."
        )
    if descriptor.routing is acp_backends.Routing.UNVERIFIED:
        # There is no setting the operator can change to make this true, so do not
        # invent one. The honest remedy is that the adapter has to be verified —
        # and the alternative is the named opt-out, which `enforce` already names.
        return (
            f"{descriptor.label} has not been verified to route tool calls through "
            "Kiro Crew. Verifying an adapter means establishing that it either "
            "delegates file and terminal work to the client, or asks per tool call."
        )
    return ""


def enforce(
    backend: str,
    work_dir: Path | str,
    *,
    allow_ungated: bool,
    session_key: str = "",
) -> None:
    """Refuse to start a session whose tool calls would bypass the gate.

    ``allow_ungated`` is the single named opt-out
    (``agent.acp_backend_allow_ungated_tools``). When it is on, the session starts
    and every start logs which controls are not being enforced — a warning per
    session rather than one at startup, because the exposure is per session and a
    single boot line scrolls away.

    INDETERMINATE refuses alongside BYPASSED. A guarantee that lapses whenever a
    file is unreadable is not a guarantee, and the alternative — proceeding when
    the probe cannot tell — is exactly the "assumed but not present" case that
    silently disables the gate.
    """
    _seed_routed_settings(backend, work_dir)
    verdict, reason = routing_verdict(backend, work_dir)
    if verdict is Verdict.ROUTED:
        return
    enforce_runtime_routing(
        backend,
        reason,
        allow_ungated=allow_ungated,
        session_key=session_key,
        verdict=verdict,
        remedy=remediation_for(backend, work_dir),
    )


def session_config_issue(backend: str, config_options: object) -> str:
    """Return why a required ACP v1 permission config cannot be applied.

    Empty means the exact option and value were advertised. Permission routing
    is stricter than optional model/effort configuration: missing options cannot
    be treated as lazy advertising because the first prompt would run ungated.
    """
    descriptor = acp_backends.descriptor_for(backend)
    if descriptor.routing is not acp_backends.Routing.SESSION_CONFIG:
        return ""
    if not descriptor.permission_config_id or not descriptor.permission_config_value:
        return "the backend descriptor has no session permission configuration"
    if not isinstance(config_options, list):
        return "session/new did not advertise configOptions"
    for option in config_options:
        if not isinstance(option, dict) or option.get("id") != descriptor.permission_config_id:
            continue
        raw_values = option.get("options")
        if not isinstance(raw_values, list):
            return f"config option {descriptor.permission_config_id!r} has no values"
        values = {
            entry.get("value")
            for entry in raw_values
            if isinstance(entry, dict) and isinstance(entry.get("value"), str)
        }
        if descriptor.permission_config_value in values:
            return ""
        return (
            f"config option {descriptor.permission_config_id!r} does not advertise "
            f"required value {descriptor.permission_config_value!r}"
        )
    return f"session/new did not advertise config option {descriptor.permission_config_id!r}"


def enforce_runtime_routing(
    backend: str,
    reason: str,
    *,
    allow_ungated: bool,
    session_key: str = "",
    verdict: Verdict = Verdict.BYPASSED,
    remedy: str = "",
) -> bool:
    """Enforce a routing fact learned after the adapter process starts.

    Returns False only for the named operator opt-out. Otherwise raises before
    the first prompt can run.
    """
    label = acp_backends.descriptor_for(backend).label
    unenforced = (
        "the bundled denied-command rules, the sensitive-path block and the governance ceiling"
    )
    if allow_ungated:
        logger.warning(
            "%s tool calls may bypass Kiro Crew's PreToolUse gate (%s), so %s are "
            "NOT consulted for them. Starting anyway because "
            "agent.acp_backend_allow_ungated_tools is enabled.",
            label,
            reason,
            unenforced,
        )
        _audit_ungated_start(backend, verdict, reason, session_key)
        return False
    suffix = f" {remedy}" if remedy else ""
    raise ToolGateUnroutable(
        f"{label} tool calls would not reach Kiro Crew's security gate ({reason}), "
        f"so {unenforced} would not be consulted for them.{suffix} "
        "To start anyway, set agent.acp_backend_allow_ungated_tools to true."
    )


def _audit_ungated_start(backend: str, verdict: Verdict, reason: str, session_key: str) -> None:
    """Record the ungated start in the security event log.

    Uses ``log_governance_degraded`` because that is precisely what this is: a
    chokepoint degrading to permit, with the operator's narrowing silently not
    applied to the tool calls the backend self-approves. ``failed_closed`` stays
    False — the session was permitted, not denied.

    Best-effort: an audit-backend failure must not become the reason a session the
    operator explicitly permitted cannot start. The warning above is emitted
    unconditionally, so the event is never the only trace.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_governance_degraded(
            session_key=session_key,
            chokepoint="acp_tool_gate",
            scope=f"acp_backend:{backend}",
            reason=f"{verdict.value}: {reason}",
            failed_closed=False,
        )
    except Exception:  # pragma: no cover - audit must not break startup
        logger.debug("Could not record ungated-start audit event", exc_info=True)


__all__ = [
    "ToolGateUnroutable",
    "enforce",
    "enforce_runtime_routing",
    "remediation_for",
    "resolve_verdict",
    "routing_verdict",
    "session_config_issue",
]
