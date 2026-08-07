"""Transport-agnostic AutoNudge authorization — the security chokepoint.

``authorize_and_add_nudge`` is the SINGLE enforcement point for arming a nudge
loop: dashboard slot ownership, Slack routability, the Discord deny-by-default
allowlist + current-session match, the message-length limit, sensitive
``stop_sentinel_path`` refusal, and the audit-or-deny SEL policy. Every caller
— the ``POST /api/autonudge`` REST handler AND the workflow ``ctx.nudge``
bridge (``dashboard/server.py``) — MUST route through it; none may call
``AutoNudgeService.add`` directly with caller-influenced input.

This lives OUTSIDE ``dashboard/handlers/`` deliberately: the logic is
security-critical and transport-agnostic, so its home is next to the AutoNudge
service (like ``autonudge.binding_key_for``), not inside an HTTP-mapping
module where edits get reviewed as handler cleanup. ``state`` is typed as a
narrow structural Protocol so non-HTTP callers don't need a hard
``DashboardState`` import.

Spec: the AutoNudge section of ``docs/system-specs/modules/learn-cron-dashboard.md``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kiro_crew.autonudge import is_channel_key
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


@runtime_checkable
class NudgeAuthzState(Protocol):
    """The narrow slice of gateway state the authorizer needs.

    Satisfied structurally by ``DashboardState`` (and by test fakes) without
    importing it — keeping this module free of dashboard dependencies.
    """

    _slots: dict
    sessions: Any
    channel_transports: Any


def resolve_stop_sentinel(slot_key: str, workspace: str = "default") -> str:
    """Compute the per-slot sentinel path."""
    ws_dir = workspace_dir_for(workspace)
    safe_key = slot_key.replace("/", "_").replace(":", "_")
    return str(ws_dir / f".stop-{safe_key}")


# Wall-clock budget ceiling (7 days), the single authoritative bound. The
# MONITOR_*_SCHEMA FieldSpecs mirror it for the MCP tools; enforcing it here
# too covers the REST and workflow paths, which do not pass through those
# schemas (GPT review on #2116: REST accepted 604801 unchanged).
MAX_RUNTIME_SECS_CEILING = 604800
# Exit-gate commands are one-liners like `pytest -q tests/` or
# `curl -fsS localhost:8080/health` — the cron `command` cap (2000) fits.
_EXIT_GATE_MAX_LEN = 2000


def _governance_session_key(binding_key: str) -> str:
    """Re-namespace a loop binding key for governance evaluation.

    Dashboard loops bind on the BARE slot key (``chat-N-TS`` — the
    ``dashboard:`` prefix is stripped by ``binding_key_for``), but governance
    profiles classify surfaces by the namespaced session key; a bare key would
    be misclassified (GPT review: a command denied by a dashboard-bound
    profile but permitted elsewhere would slip through). Channel keys
    (``slack:``/``discord:``…) already carry their namespace.
    """
    if not binding_key or is_channel_key(binding_key):
        return binding_key
    return f"dashboard:{binding_key}"


def vet_exit_gate_cmd(cmd: str, session_key: str = "") -> str | None:
    """Storage-time vetting for a loop's exit-gate shell command.

    The gate is model-supplied via ``monitor_start``/``monitor_update`` (or the
    REST API), persisted, and later executed by the gateway via ``sh -c`` in
    the STRICT command sandbox — the same low-trust exec shape as a cron
    ``command``, and like it entirely outside the kiro-cli ACP permission/hook
    flow. So it gets the SAME storage-time guards (``mcp_cron._vet_shell_command``:
    deny-list, sensitive paths, exfiltration URLs, credential-path references,
    no command substitution / runtime composition), plus a control-character
    refusal — an embedded newline would smuggle a second line past guards that
    reason about a one-liner.

    ``session_key`` is the ARMING session's identity: ``_vet_shell_command``
    evaluates governance against the *cron* surface (``cron:_vet``), so a
    command an enterprise profile denies for this caller but allows for cron
    would slip through it. The additional check here applies the caller's own
    governance ceiling; both apply (intersection). Best-effort in the same way
    as cron's own governance vet: a governance-machinery failure degrades with
    an audit rather than refusing, while a composition failure propagates
    (fail-closed CPP invariant).

    Vetting cannot see INSIDE a referenced interpreter script
    (``python3 /tmp/gate.py``); the exec-time strict sandbox is the control
    that bounds such a script (it hides credential dirs including ``~/.ssh``)
    — see ``session_directive_apply._run_exit_gate``.

    Returns a human-readable refusal, or ``None`` when the command is clean.
    The always-on checks FAIL CLOSED: if ``_vet_shell_command`` itself errors,
    the command is refused — an unvetted gate must never be stored.
    """
    if len(cmd) > _EXIT_GATE_MAX_LEN:
        return f"exit_gate_cmd too long (max {_EXIT_GATE_MAX_LEN} chars)"
    if any(ord(c) < 32 or ord(c) == 127 for c in cmd):
        return "exit_gate_cmd must not contain control characters or newlines"
    # REDACTION STABILITY (GPT review): the stored command is broadcast
    # verbatim to every dashboard client in the autonudge WS payload and
    # echoed in arm/refusal messages, so a credential-bearing gate
    # (`curl -H "Authorization: Bearer sk-..."` ) would expose the secret on
    # every surface that renders the loop. Refuse any command the credential
    # or exfiltration redactors would alter — a gate must be safe to display
    # exactly as stored. (Storing a REDACTED form instead would corrupt the
    # command, and executing an unredacted secret while displaying a redacted
    # one would hide what actually runs.)
    for _redact in (redact_credentials, redact_exfiltration_urls):
        _changed, _hits = _redact(cmd)
        if _changed != cmd:
            return (
                "exit_gate_cmd must not embed credentials or exfiltration "
                "URLs — the stored command is displayed verbatim on loop "
                "surfaces. Read secrets from the environment or a config "
                "file inside the gate instead."
            )
    try:
        # DELIBERATELY function-local, documented exception to the
        # top-level-imports rule: this module is on the dashboard package's
        # import path (dashboard/server.py imports it), and the dashboard
        # import is contractually a LEAF (test_perf_boot_path
        # TestDashboardImportIsLeaf) — a top-level import here drags
        # mcp_cron → mcp_core (the whole MCP tool tree) into dashboard boot.
        # The deferred-import failure mode the rule guards against is already
        # handled: the except below FAILS CLOSED, refusing the gate rather
        # than storing it unvetted.
        from kiro_crew.mcp_cron import _vet_shell_command

        err = _vet_shell_command(cmd)
    except Exception:  # noqa: BLE001 - fail closed: unvetted ⇒ refused
        logger.warning("exit_gate_cmd vetting failed — refusing the gate", exc_info=True)
        return "exit_gate_cmd could not be vetted — gate refused"
    if err:
        # _vet_shell_command returns "Error: cron command blocked: ..." —
        # re-frame for this surface without losing the reason.
        return err.removeprefix("Error: ").replace("cron command", "exit_gate_cmd", 1)
    if session_key:
        from kiro_crew.platform.context import PlatformCompositionError

        try:
            # Late import mirrors mcp_cron._vet_command_governance: the
            # governance machinery is optional platform composition, and its
            # absence must degrade (audited), not break standalone installs.
            from kiro_crew.platform.governance_profiles import governance_permits

            decision = governance_permits(
                "commands", cmd, session_key=session_key, log_warning=False
            )
            if not getattr(decision, "permitted", True):
                reason = str(getattr(decision, "reason", "") or "")
                reason, _ = redact_exfiltration_urls(reason)
                reason, _ = redact_credentials(reason)
                return f"exit_gate_cmd blocked by governance policy: {reason}"
        except PlatformCompositionError:
            raise
        except Exception:  # noqa: BLE001 - degrade like cron's governance vet
            try:
                from kiro_crew.platform.governance_profiles import audit_governance_degraded

                audit_governance_degraded(
                    "exit_gate_cmd", session_key=session_key, scope="commands",
                    log_warning=False,
                )
            except Exception:  # noqa: BLE001
                pass
    return None


async def authorize_and_update_nudge(
    *,
    svc: Any,
    loop_id: str,
    message: Any = None,
    idle_secs: Any = None,
    max_cycles: Any = None,
    active: Any = None,
    max_runtime_secs: Any = None,
    exit_gate_cmd: Any = None,
    source: str,
    caller: str = "",
) -> tuple[Any | None, str | None, int]:
    """Validate + audit + apply a loop update; return ``(loop, error, status)``.

    The update-side twin of :func:`authorize_and_add_nudge`, and for the same
    reason it lives here rather than in the HTTP handler: ``message`` is the
    field that gets PERSISTED and re-injected into chat (or posted to a
    messaging channel) on every fire, so its redaction must sit at a
    transport-agnostic chokepoint. Redacting only on the arm path would make an
    update a trivial bypass of the arm-time guard, and putting the guard in the
    HTTP layer would leave any future non-HTTP caller uncovered.

    Enforces, in order: type/length validation of ``message`` (a non-string
    yields 400 rather than a ``len()`` TypeError 500), integer coercion of
    ``idle_secs``/``max_cycles`` (matching the arm handler, so ``"abc"``/``[]``
    is a 400 and not a 500), credential + exfiltration-URL redaction, then an
    AUDIT-OR-DENY critical ``invoked`` event BEFORE the mutation — if that write
    fails the update is DENIED with 503, because a recurring instruction that
    drives unattended turns must never be rewritten unaudited.

    Ownership is NOT checked here: ``loop_id`` is opaque and this module has no
    session identity. Callers that have one (the ``monitor_update`` MCP tool)
    resolve the id from their own binding key so a cross-session update is
    unrepresentable; the REST route is user-token gated for the dashboard UI.
    """
    loop_id = (loop_id or "").strip()

    def _audit(outcome: str, err: str | None = None, **extra: Any) -> None:
        try:
            sel().log_tool_invocation(
                session_key=str(extra.pop("session_key", "")),
                source=source,
                tool_name="autonudge_update",
                outcome=outcome,
                error=err or "",
                metadata={"loop_id": loop_id, "caller": caller, **extra},
            )
        except Exception:  # noqa: BLE001 - auditing must never break the flow
            logger.warning("autonudge update audit failed", exc_info=True)

    def _deny(reason: str, status: int) -> tuple[None, str, int]:
        _audit("denied", reason)
        return None, reason, status

    if svc is None:
        _audit("error", "autonudge disabled")
        return None, "auto-nudge disabled (KIROCREW_AUTONUDGE not set)", 503
    if not loop_id:
        return _deny("loop_id required", 400)
    if message is not None:
        if not isinstance(message, str):
            return _deny("message must be a string", 400)
        if len(message) > 8000:
            return _deny("message too long (max 8000 chars)", 400)
        message, _ = redact_exfiltration_urls(message)
        message, _ = redact_credentials(message)
    try:
        # Reject non-integral values rather than silently truncating: idle_secs
        # 59.9 must not become 59, and `Infinity` (legal JSON in many parsers)
        # raises OverflowError from int(), which would surface as a 500.
        for _name, _val in (
            ("idle_secs", idle_secs),
            ("max_cycles", max_cycles),
            ("max_runtime_secs", max_runtime_secs),
        ):
            if _val is None or isinstance(_val, bool):
                continue
            if isinstance(_val, float) and not _val.is_integer():
                return _deny(f"{_name} must be a whole number", 400)
        idle_secs = None if idle_secs is None else int(idle_secs)
        max_cycles = None if max_cycles is None else int(max_cycles)
        max_runtime_secs = None if max_runtime_secs is None else int(max_runtime_secs)
    except (TypeError, ValueError, OverflowError):
        return _deny("idle_secs, max_cycles and max_runtime_secs must be integers", 400)
    if max_runtime_secs is not None and not (0 <= max_runtime_secs <= MAX_RUNTIME_SECS_CEILING):
        return _deny(
            f"max_runtime_secs must be between 0 and {MAX_RUNTIME_SECS_CEILING} (7 days)", 400
        )
    # ``active`` must be a real boolean. bool("false") is True, so accepting a
    # JSON string would turn an explicit pause request into a RESUME — the
    # opposite of what the caller asked for on a loop that runs tools
    # unattended.
    if active is not None and not isinstance(active, bool):
        return _deny("active must be a boolean", 400)
    if exit_gate_cmd is not None:
        if not isinstance(exit_gate_cmd, str):
            return _deny("exit_gate_cmd must be a string", 400)
        exit_gate_cmd = exit_gate_cmd.strip()
        # USER-ARMED GATES ONLY at the chokepoint (design review, mirroring
        # the add path): setting, changing, AND clearing ("") a gate are all
        # user actions — an agent/workflow caller that could clear a gate
        # would disarm the very control that constrains it. There is no
        # inheritance case on the update path (updates never replace loops),
        # so any non-dashboard exit_gate_cmd is denied outright.
        if source != "dashboard":
            return _deny(
                "exit_gate_cmd can only be set, changed, or removed by the "
                f"user (dashboard or REST) — not by {source} callers",
                403,
            )
        # "" clears the gate and needs no vetting; a non-empty replacement is
        # vetted exactly like the arm path, so an update can never install a
        # gate that monitor_start/POST would have refused. Governance is
        # evaluated against the loop's own bound session (looked up by id),
        # matching what the arm path enforces.
        if exit_gate_cmd:
            target = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
            # Offloaded: vetting reads governance profiles / policy files from
            # disk (via _vet_shell_command -> governance), and this coroutine
            # runs on the gateway event loop — a slow home mount would freeze
            # every session (GPT review).
            gate_err = await asyncio.to_thread(
                vet_exit_gate_cmd,
                exit_gate_cmd,
                _governance_session_key(str(getattr(target, "slot_key", "") or "")),
            )
            if gate_err:
                return _deny(gate_err, 400)

    def _critical_invoked_audit() -> None:
        sel().log_tool_invocation(
            session_key=loop_id,
            source=source,
            tool_name="autonudge_update",
            outcome="invoked",
            critical=True,
            metadata={
                "loop_id": loop_id,
                "fields": sorted(
                    k
                    for k, v in (
                        ("message", message),
                        ("idle_secs", idle_secs),
                        ("max_cycles", max_cycles),
                        ("max_runtime_secs", max_runtime_secs),
                        ("active", active),
                        ("exit_gate_cmd", exit_gate_cmd),
                    )
                    if v is not None
                ),
                "caller": caller,
            },
        )

    try:
        await asyncio.get_running_loop().run_in_executor(None, _critical_invoked_audit)
    except Exception:  # noqa: BLE001 - fail closed: no audit ⇒ no mutation
        logger.error("autonudge update denied: SEL audit unavailable", exc_info=True)
        return None, "audit log unavailable — nudge loop not updated", 503
    try:
        loop = await svc.update(
            loop_id,
            message=message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            active=active,
            max_runtime_secs=max_runtime_secs,
            exit_gate_cmd=exit_gate_cmd,
        )
    except Exception as exc:  # noqa: BLE001 - audit the failure, then propagate
        _audit("error", f"svc.update failed: {type(exc).__name__}")
        raise
    if loop is None:
        return _deny("loop not found", 404)
    _audit("success", session_key=loop.slot_key)
    return loop, None, 200


async def authorize_and_add_nudge(
    *,
    svc: Any,
    state: NudgeAuthzState,
    slot_key: str,
    message: str,
    idle_secs: int = 60,
    max_cycles: int = 0,
    stop_sentinel_path: str = "",
    max_runtime_secs: int = 0,
    exit_gate_cmd: str = "",
    source: str,
    caller: str = "",
) -> tuple[Any | None, str | None, int]:
    """Validate + authorize + arm a nudge loop; return ``(loop, error, status)``.

    The single chokepoint shared by the ``POST /api/autonudge`` REST handler and
    the workflow ``ctx.nudge`` bridge, so BOTH enforce identical slot/channel
    ownership checks (dashboard slot must exist; Slack session must be routable;
    Discord DM must be an allowlisted user's CURRENT session — deny-by-default),
    the 8000-char message limit, and sensitive-``stop_sentinel_path`` refusal.
    ``slot_key`` must already be the resolved binding key (bare ``chat-N-TS`` for
    dashboard, ``slack:``/``discord:`` for channels) — callers that hold a
    namespaced session key map it first (``autonudge.binding_key_for``).
    ``source`` tags the SEL audit (``"dashboard"`` for REST, ``"workflow"`` for
    ctx.nudge).

    SEL AUDIT: emits an event for EVERY outcome — ``denied`` for each
    validation/authorization rejection, ``error`` for a disabled service or an
    ``svc.add`` failure, ``success`` for an armed loop — so an attempted
    cross-session or disallowed nudge always leaves a security audit trail
    (backend-security-controls rule). Never raises for a validation/authz
    failure — returns the ``(error, status)`` so the REST handler can map it to
    an HTTP response and the workflow bridge can log-and-skip.
    """
    slot_key = (slot_key or "").strip()
    message = (message or "").strip()
    # The nudge message is LLM-influenced (workflow-authored ctx.nudge and
    # agent-issued monitor_start alike), gets PERSISTED to the loop store, and
    # is later re-injected into chat / posted to messaging channels on every
    # fire. Redact credential patterns and exfiltration URLs at this single
    # chokepoint so no delivery surface can leak them (same guard as other
    # LLM-influenced output paths; backend-security-controls).
    if message:
        message, _ = redact_exfiltration_urls(message)
        message, _ = redact_credentials(message)

    def _audit(outcome: str, err: str | None = None) -> None:
        try:
            sel().log_tool_invocation(
                session_key=slot_key,
                source=source,
                tool_name="autonudge_start",
                outcome=outcome,
                error=err or "",
                metadata={
                    "slot_key": slot_key,
                    "idle_secs": idle_secs,
                    "max_cycles": max_cycles,
                    "max_runtime_secs": max_runtime_secs,
                    "caller": caller,
                },
            )
        except Exception:  # noqa: BLE001 - auditing must never break the flow
            logger.warning("autonudge audit failed", exc_info=True)

    def _deny(reason: str, status: int) -> tuple[None, str, int]:
        _audit("denied", reason)
        return None, reason, status

    if svc is None:
        _audit("error", "autonudge disabled")
        return None, "auto-nudge disabled (KIROCREW_AUTONUDGE not set)", 503
    if not slot_key or not message:
        return _deny("session_key (or slot_key) and message required", 400)
    try:
        _budget = int(max_runtime_secs)
    except (TypeError, ValueError, OverflowError):
        return _deny("max_runtime_secs must be an integer", 400)
    if not (0 <= _budget <= MAX_RUNTIME_SECS_CEILING):
        return _deny(
            f"max_runtime_secs must be between 0 and {MAX_RUNTIME_SECS_CEILING} (7 days)", 400
        )
    if is_channel_key(slot_key):
        # Channel-bound loop (Slack / Discord ...). Validate the session is
        # routable so a nudge fired later has somewhere to reply.
        if slot_key.startswith("slack:"):
            sessions = getattr(state, "sessions", None)
            if sessions is None or not sessions.get_channel(slot_key):
                return _deny(f"unknown slack session {slot_key}", 404)
        elif slot_key.startswith("discord:"):
            # Deny-by-default (mirrors the Discord inbound allowlist): only DM
            # sessions of ALLOWLISTED users, and only the user's CURRENT
            # session key exactly as the dispatcher derives it. Anything else
            # would let an authenticated caller mint loops that DM arbitrary
            # Discord users through the agent.
            transports = getattr(state, "channel_transports", None) or {}
            transport = transports.get("discord")
            dispatcher = transport.dispatcher if transport is not None else None
            if transport is None or dispatcher is None:
                return _deny("discord transport not running", 404)
            parts = slot_key.split(":")
            if len(parts) < 4 or parts[2] != "direct":
                return _deny(f"unsupported discord session {slot_key} (DM sessions only)", 400)
            user_id = parts[3]
            if not dispatcher.is_authorized(user_id):
                return _deny("discord user is not in the allowed_user_ids allowlist", 403)
            try:
                current_key = dispatcher.current_session_key(user_id)
            except Exception:
                current_key = ""
            if slot_key != current_key:
                return _deny("discord session key does not match the user's current session", 404)
        else:
            return _deny(f"unsupported channel session {slot_key}", 400)
    elif slot_key not in state._slots:
        return _deny(f"unknown slot {slot_key}", 404)
    if len(message) > 8000:
        return _deny("message too long (max 8000 chars)", 400)
    exit_gate_cmd = str(exit_gate_cmd or "").strip()
    # USER-ARMED GATES ONLY, enforced AT THE CHOKEPOINT (design review):
    # the transport edges (MCP schemas, directive applier) already refuse
    # agent-supplied gates, but this module's own doctrine is that policy
    # lives here so no future non-HTTP caller can bypass it — e.g. the
    # LLM-authored workflow ``ctx.nudge`` port growing an exit_gate_cmd
    # field would otherwise arm agent-authored shell silently. Only the
    # user surface (source="dashboard") may AUTHOR a gate. For every other
    # caller the slot's EXISTING gate is authoritative (GPT review): an add
    # replaces the slot's loop, so a non-dashboard add that OMITS the field
    # would otherwise silently discard a user-armed gate — the omitted gate
    # is inherited here, and a non-verbatim replacement is rejected. All
    # verified against the store, never trusted from a caller-supplied flag.
    if source != "dashboard":
        existing = svc.get_by_slot(slot_key)
        existing_gate = (
            str(getattr(existing, "exit_gate_cmd", "") or "").strip()
            if existing
            else ""
        )
        if not exit_gate_cmd and existing_gate:
            # Omitted -> inherit. The applier already does this for
            # mcp-directives; enforcing it here covers every other
            # non-dashboard caller (workflow ctx.nudge, future ports).
            exit_gate_cmd = existing_gate
        elif exit_gate_cmd and exit_gate_cmd != existing_gate:
            return _deny(
                "exit_gate_cmd can only be armed by the user (dashboard "
                f"or REST) — {source} callers cannot author a gate; an "
                "existing gate is inherited automatically on replace",
                403,
            )
        if exit_gate_cmd and existing is not None:
            # TERMINAL-BOUND CLAMPS travel WITH the inheritance (GPT review):
            # an inherited gate with a tiny cap/budget expires the replacement
            # through a deliberately-ungated terminal — e.g. workflow
            # ctx.nudge(max_cycles=1) — the same indirect disarm the applier
            # clamps for mcp-directives. Enforced here so every non-dashboard
            # caller gets identical protection: each bound floors at the
            # replaced loop's remaining allowance; an unlimited replaced
            # bound forces an unlimited replacement bound.
            replaced_cap = int(getattr(existing, "max_cycles", 0) or 0)
            if replaced_cap == 0:
                max_cycles = 0
            else:
                remaining_cycles = max(
                    replaced_cap - int(getattr(existing, "cycle_count", 0) or 0), 1
                )
                if max_cycles and max_cycles < remaining_cycles:
                    max_cycles = remaining_cycles
            replaced_budget = int(getattr(existing, "max_runtime_secs", 0) or 0)
            if replaced_budget == 0:
                max_runtime_secs = 0
            else:
                created = float(getattr(existing, "created_ts", 0.0) or 0.0)
                elapsed = int(time.time() - created) if created else 0
                remaining_budget = max(replaced_budget - elapsed, 1)
                if max_runtime_secs and max_runtime_secs < remaining_budget:
                    max_runtime_secs = remaining_budget
    if exit_gate_cmd:
        # Offloaded for the same reason as the update path: vetting reads
        # governance/policy files from disk and this runs on the event loop.
        gate_err = await asyncio.to_thread(
            vet_exit_gate_cmd, exit_gate_cmd, _governance_session_key(slot_key)
        )
        if gate_err:
            return _deny(gate_err, 400)
    stop_sentinel_path = (stop_sentinel_path or "").strip()
    if stop_sentinel_path and is_sensitive_path(stop_sentinel_path):
        return _deny("stop_sentinel_path points to a sensitive location", 400)
    # Auto-default: per-session sentinel so multiple loops don't clash. The
    # unlink is filesystem I/O — offloaded (no-blocking-call-on-event-loop).
    if not stop_sentinel_path:
        if is_channel_key(slot_key):
            stop_sentinel_path = resolve_stop_sentinel(slot_key)
        else:
            slot = state._slots.get(slot_key)
            if slot:
                stop_sentinel_path = resolve_stop_sentinel(
                    slot_key, getattr(slot, "workspace", "default")
                )
        if stop_sentinel_path:
            sentinel = Path(stop_sentinel_path)

            def _unlink_sentinel() -> None:
                sentinel.unlink(missing_ok=True)

            await asyncio.get_running_loop().run_in_executor(None, _unlink_sentinel)

    # AUDIT-OR-DENY: the loop must never be armed unaudited. Emit a CRITICAL
    # ``invoked`` event BEFORE svc.add — ``critical=True`` writes synchronously
    # and re-raises on failure, so an unauditable arm is DENIED rather than
    # armed silently. The write is OFFLOADED to the default executor and
    # awaited (no-blocking-call-on-event-loop rule: a slow/wedged disk must not
    # freeze the gateway loop) — awaiting it preserves the audit-before-action
    # ordering and exception propagation. The terminal success event below is
    # then best-effort: if it fails, the armed loop is still covered by this
    # invoked record.
    def _critical_invoked_audit() -> None:
        sel().log_tool_invocation(
            session_key=slot_key,
            source=source,
            tool_name="autonudge_start",
            outcome="invoked",
            critical=True,
            metadata={
                "slot_key": slot_key,
                "idle_secs": int(idle_secs),
                "max_cycles": int(max_cycles),
                "max_runtime_secs": int(max_runtime_secs),
                "has_exit_gate": bool(exit_gate_cmd),
                "caller": caller,
            },
        )

    try:
        await asyncio.get_running_loop().run_in_executor(None, _critical_invoked_audit)
    except Exception:  # noqa: BLE001 - fail closed: no audit ⇒ no loop
        logger.error("autonudge arm denied: SEL audit unavailable", exc_info=True)
        return None, "audit log unavailable — nudge loop not armed", 503
    try:
        loop = await svc.add(
            slot_key=slot_key,
            message=message,
            idle_secs=int(idle_secs),
            max_cycles=int(max_cycles),
            stop_sentinel_path=stop_sentinel_path,
            max_runtime_secs=int(max_runtime_secs),
            exit_gate_cmd=exit_gate_cmd,
        )
    except Exception as exc:  # noqa: BLE001 - audit the failure, then propagate
        _audit("error", f"svc.add failed: {type(exc).__name__}")
        raise
    try:
        sel().log_tool_invocation(
            session_key=slot_key,
            source=source,
            tool_name="autonudge_start",
            outcome="success",
            metadata={
                "loop_id": loop.id,
                "idle_secs": loop.idle_secs,
                "max_cycles": loop.max_cycles,
                "caller": caller,
            },
        )
    except Exception:  # noqa: BLE001 - armed loop already covered by ``invoked``
        logger.warning("autonudge success audit failed (invoked event covers the arm)",
                       exc_info=True)
    return loop, None, 200
