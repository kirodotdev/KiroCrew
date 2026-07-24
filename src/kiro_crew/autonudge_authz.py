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


async def authorize_and_add_nudge(
    *,
    svc: Any,
    state: NudgeAuthzState,
    slot_key: str,
    message: str,
    idle_secs: int = 60,
    max_cycles: int = 0,
    stop_sentinel_path: str = "",
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
