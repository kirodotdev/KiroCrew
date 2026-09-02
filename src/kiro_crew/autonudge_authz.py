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
from typing import Any, Callable, Protocol, runtime_checkable

from kiro_crew.autonudge import (
    MAX_BANNER_CHARS,
    AutoNudgeStoreUnvetted,
    MonitorUpdateConflict,
    NudgeAdmissionRefused,
    is_channel_key,
    scrub_loop_text,
)
from kiro_crew.config.loader import workspace_dir_for
from kiro_crew.monitoring.models import MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS, MonitorState
from kiro_crew.platform import PlatformCompositionError, redact_via_context
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


async def authorize_and_update_monitor(
    *,
    svc: Any,
    state: NudgeAuthzState,
    loop_id: str,
    session_key: str,
    patch: dict[str, Any],
    source: str,
    caller: str = "",
) -> tuple[Any | None, str | None, int]:
    """Audit-or-deny one ownership-resolved structured monitor patch."""
    safe_patch = dict(patch)
    wake_instructions = safe_patch.get("wake_instructions")
    if isinstance(wake_instructions, str):
        wake_instructions, _ = redact_exfiltration_urls(wake_instructions)
        wake_instructions, _ = redact_credentials(wake_instructions)
        safe_patch["wake_instructions"] = wake_instructions

    async def _audit(outcome: str, error: str = "") -> bool:
        try:
            await asyncio.to_thread(
                lambda: sel().log_tool_invocation(
                    session_key=session_key,
                    source=source,
                    tool_name="monitor_update",
                    outcome=outcome,
                    error=error,
                    critical=True,
                    metadata={"fields": sorted(safe_patch), "caller": caller},
                )
            )
        except Exception:
            logger.error("monitor update SEL audit unavailable", exc_info=True)
            return False
        return True

    if (
        isinstance(wake_instructions, str)
        and len(wake_instructions) > MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS
    ):
        error = (
            "wake_instructions too long after redaction "
            f"(max {MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS} chars)"
        )
        await _audit("denied", error)
        return None, error, 400

    if not await _audit("invoked"):
        return None, "audit log unavailable — monitor not updated", 503
    if not is_channel_key(session_key):
        current = state._slots.get(session_key)
        if current is None:
            error = "owning dashboard session is no longer available"
            await _audit("denied", error)
            return None, error, 404
        mode = str(getattr(current, "mode", ""))
        if mode in {"crew", "member"}:
            error = f"{mode}-mode sessions do not accept direct automation turns"
            await _audit("denied", error)
            return None, error, 409
        if str(getattr(current, "memory_mode", "persistent")) != "persistent":
            error = "incognito and temporary sessions cannot host automation loops"
            await _audit("denied", error)
            return None, error, 403
    try:
        loop = await svc.update_monitor(loop_id, **safe_patch)
    except MonitorUpdateConflict as exc:
        error = str(exc)
        await _audit("denied", error)
        return None, error, 409
    if loop is None:
        error = "structured monitor not found or already terminal"
        await _audit("denied", error)
        return None, error, 404
    return loop, None, 200


async def authorize_and_stop_monitor(
    *,
    svc: Any,
    loop_id: str,
    session_key: str,
    source: str,
    caller: str = "",
    user_reason: str = "",
) -> tuple[Any | None, str | None, int]:
    """Audit before retaining one ownership-resolved user-stop outcome."""
    try:
        await asyncio.to_thread(
            lambda: sel().log_tool_invocation(
                session_key=session_key,
                source=source,
                tool_name="monitor_stop",
                outcome="invoked",
                critical=True,
                metadata={"caller": caller},
            )
        )
    except Exception:
        logger.error("monitor stop denied: SEL audit unavailable", exc_info=True)
        return None, "audit log unavailable — monitor not stopped", 503
    loop = await svc.stop_monitor(loop_id, user_reason=user_reason)
    if loop is None:
        return None, "structured monitor not found", 404
    return loop, None, 200


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


def normalize_banner(
    banner: Any, *, absent_ok: bool, truncate: bool = False
) -> tuple[str, str | None]:
    """strip -> cap -> redact -> re-cap, in ONE place, called per site.

    Returns ``(value, error)``. The error is a plain string rather than a
    ``_deny`` result because ``_deny`` is nested per authorizer, closing over
    that path's ``_audit`` -- so each caller routes the refusal through its OWN
    ``_deny`` and the rejection still lands in that path's SEL audit.

    ``absent_ok`` is the one genuine difference between the two callers: on the
    arm path ``None`` means "no banner supplied", while on the update path it
    means "leave unchanged" and is filtered out before we get here, so a ``None``
    reaching this function on that path IS a type error.

    A non-blank banner is credential-scrubbed with the SAME two write-path passes
    the sibling ``message`` field already gets in both authorizers
    (``redact_exfiltration_urls`` then ``redact_credentials``): a banner is
    caller-supplied, PERSISTED to the loop store, and served by
    ``GET /api/autonudge``, so being short does not make it a safe place to park a
    credential. The cap is re-checked AFTER redaction because redaction can GROW
    the string -- ``[REDACTED: credential]`` is 22 chars replacing a 20-char AWS
    key id -- so the first check bounds what we RECEIVE and the second bounds what
    we STORE, keeping the loader from having to blank an over-cap banner later.

    ``truncate`` is for the ONE producer whose banner is derived from arbitrarily
    long text it does not control: ``/goal`` uses the objective as the row. The
    API/MCP callers keep ``truncate=False`` and REJECT an over-cap banner (a user
    typed it and can shorten it). With ``truncate=True`` the over-cap value is not
    rejected but cut to the cap — critically AFTER redaction, never before, so a
    credential straddling the cap boundary is masked while the whole string is
    still present. Slicing first (the caller doing ``objective[:cap]``) would feed
    a truncated token to the scanner, defeat full-token detection, and persist a
    raw credential prefix; redacting the full text first means the cut can only
    land inside plain text or a ``[REDACTED: …]`` placeholder, never a live secret.
    """
    if absent_ok:
        if banner is not None and not isinstance(banner, str):
            return "", "banner must be a string"
        banner = banner or ""
    elif not isinstance(banner, str):
        return "", "banner must be a string"
    # Whitespace-only means "clear it": "   " must not become a blank display row
    # that hides the cycle body while showing nothing in its place.
    banner = banner.strip()
    if len(banner) > MAX_BANNER_CHARS and not truncate:
        return "", f"banner too long (max {MAX_BANNER_CHARS} chars)"
    if banner:
        banner, _ = redact_exfiltration_urls(banner)
        banner, _ = redact_credentials(banner)
        if len(banner) > MAX_BANNER_CHARS:
            if truncate:
                # Cut AFTER redaction: every full credential is already a
                # placeholder, so the cut can only fall in plain text or inside
                # ``[REDACTED: …]`` — never mid-secret.
                banner = banner[:MAX_BANNER_CHARS]
            else:
                return "", (
                    f"banner exceeds {MAX_BANNER_CHARS} chars once credentials are "
                    "masked — masking can lengthen the text, so shorten the banner"
                )
    return banner, None


def banner_unsupported_for(slot_key: str, banner: Any) -> str | None:
    """Refuse a banner on a channel-bound loop; ``None`` when it is fine.

    ``banner`` shortens the DASHBOARD transcript row, and nothing else. ``_fire``
    routes a channel key to ``_fire_slack_nudge`` / ``_fire_discord_nudge`` /
    ``_fire_webex_nudge``, none of which reads ``loop.banner`` -- both read sites
    live inside ``_fire_dashboard_nudge``. Accepting the field there stored a
    setting the runtime can never honour, and the caller got a 200, so the only
    way to discover it was to notice the row never changed.

    Blank is not "setting a banner" -- ``banner=""`` is the default every
    channel-bound caller already passes, so treating absence as a refusal would
    break all of them. A non-``str`` truthy value still counts as an attempt to
    set one, and is reported as the channel problem it is.
    """
    if not is_channel_key(slot_key):
        return None
    if isinstance(banner, str) and not banner.strip():
        return None
    if banner is None or banner is False:
        return None
    return (
        "banner is not supported for a channel-bound loop "
        f"({slot_key.split(':', 1)[0]}:): the nudge IS the turn's input there, so "
        "there is no separate transcript row to shorten"
    )


def message_is_echoed_projection(current: Any, message: Any) -> bool:
    """True when *message* is exactly the scrubbed projection of the stored one.

    THE single spelling of this predicate, and it has exactly ONE caller.
    ``authorize_and_update_nudge`` evaluates it once against the row it is about to
    write, drops the field when it matches, and reports that same decision through its
    ``echo_decision`` out-param. The PATCH handler keys ``message_ignored`` on that
    value rather than re-deriving it: a second evaluation reads the store again, and an
    update landing between the two reads lets the response describe a decision the
    write never made.

    Raises ``PlatformCompositionError`` on a host that cannot compose its policy; the
    caller answers for that with a 503.
    """
    if current is None or message is None:
        return False
    return scrub_loop_text(getattr(current, "message", None), field="message") == message


def _scrub_policy_unavailable() -> bool:
    """True when the active credential policy cannot scrub, so nothing may be written.

    Both authorizers mutate and then hand the loop back to a caller that SERIALIZES
    it -- ``dashboard/handlers/autonudge._serialize`` runs every field through
    ``scrub_loop_text`` -> ``redact_via_context``, which is fail-closed. A request
    that scrubs nothing during authorization (the message compare is gated on
    ``message is not None``) therefore reached ``svc.add``/``svc.update``, COMMITTED,
    and only then
    hit the raise while rendering the response: HTTP 500 with the mutation persisted
    and audited as a success. The store and the caller's belief about it then
    disagree permanently, and a retry applies the change twice.

    So the ordering is the fix: ask ONCE, before the critical ``invoked`` audit and
    before the mutation, whether the projection will be able to scrub. If it cannot,
    refuse with an audited 503 and write nothing.

    ``redact_via_context("")`` is the probe, the same spelling ``autonudge._load``
    uses for this question. The empty string is deliberate and sufficient: the shim
    calls ``current_context().credentials.redact(text)`` with no short-circuit, so
    composition -- the thing that fails on a mis-composed host -- is exercised
    regardless of the text. It also scrubs nothing real, so the probe cannot leak.

    Only ``PlatformCompositionError`` counts. Every other adapter failure already
    degrades to ``security.redact`` inside the shim, so the projection will still
    succeed and refusing would deny a request that would have worked.
    """
    try:
        redact_via_context("")
    except PlatformCompositionError:
        return True
    return False


async def authorize_and_update_nudge(
    *,
    svc: Any,
    loop_id: str,
    message: Any = None,
    idle_secs: Any = None,
    max_cycles: Any = None,
    active: Any = None,
    max_runtime_secs: Any = None,
    banner: Any = None,
    source: str,
    caller: str = "",
    echo_decision: dict[str, bool] | None = None,
    row: Any = None,
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
    # ONE read serving BOTH consumers below -- the echo projection and the banner
    # channel refusal. A caller already holding the row passes it so it is not re-read.
    if row is None and hasattr(svc, "get_by_id"):
        row = svc.get_by_id(loop_id)
    if message is not None:
        if not isinstance(message, str):
            return _deny("message must be a string", 400)
        if len(message) > 8000:
            return _deny("message too long (max 8000 chars)", 400)
        # A client that RE-SUBMITS the projection it was served has not edited the
        # message, and must not be allowed to overwrite the stored one with it.
        #
        # The two rules differ on purpose and that is what made this reachable: a
        # message armed through ``svc.add`` skips the pair below, while the REST
        # projection and the websocket broadcast run the WIDER
        # ``redact_via_context`` (a composed host adds its own patterns). So the
        # popover loads ``[REDACTED: ...]``, its Save PATCHes that back, and the
        # operator's instruction is destroyed with no error and no warning.
        #
        # Compared with ``scrub_loop_text`` ITSELF -- the very function the
        # projection uses, including its empty-string short-circuit -- and not a
        # second hand-rolled redaction, because two copies of "what is
        # credential-shaped" would drift and silently re-open this.
        #
        # Read BEFORE the pair below, since the projection was made from the STORED
        # text. A missing loop is left alone: ``svc.update`` returns ``None`` and the
        # existing 404 below reports it, rather than a second not-found path here.
        #
        # ACCEPTED, and the reason this is nulled here rather than under
        # ``_update_unserialized``'s lock: a concurrent write landing between this
        # read and the update could drop one genuine edit. That window is narrow and
        # its worst case is the consequence this fix already accepts, whereas doing
        # the comparison inside the service would leave the critical ``invoked``
        # audit below claiming a ``message`` change on EVERY such save -- a record
        # that disagrees with the store, systematically. Nulling here keeps
        # ``fields`` truthful.
        current = row
        # ``scrub_loop_text`` routes through ``redact_via_context``, which is
        # FAIL-CLOSED and re-raises ``PlatformCompositionError`` on a host that
        # declares a credential policy it could not compose. Uncaught, that escaped
        # BEFORE ``_deny`` and before the critical ``invoked`` audit below, so a PATCH
        # carrying a message died as an unaudited 500 -- no SEL event at all, which is
        # the one guarantee every refusal on this path carries. Caught
        # here for two reasons: the status is 503 and must stay
        # distinguishable from the 400s above, and ``_deny`` is nested per authorizer
        # so the refusal lands in THIS path's audit. Refusing, not degrading: this is
        # an ingress decision, unlike the loader arms in ``autonudge.py``.
        #
        # Reachable even though ``_load`` refuses persisted rows on such a host:
        # ``svc.add`` does not scrub the message, so the arm path can still put a
        # loop into ``_loops`` without any scrub having run.
        #
        # The try spans ONLY the comparison. ``get_by_id`` above is a plain
        # ``_loops.get`` with no scrub, ``message`` is a plain dataclass field, and
        # the ``message = None`` below cannot raise -- so widening the span would
        # catch nothing more and would hide an unrelated raise.
        try:
            resubmitted_projection = message_is_echoed_projection(current, message)
        except PlatformCompositionError:
            return _deny(
                "Safety checks are temporarily unavailable, so this goal cannot be saved. If this keeps happening, restart Kiro Crew.",
                503,
            )
        # THE decision, handed to the caller so the response cannot re-derive it from a
        # second read that may observe a value this write never considered.
        if echo_decision is not None:
            echo_decision["ignored"] = resubmitted_projection
        if resubmitted_projection:
            message = None
            # NOT silent: the drop is recorded so a caller that really did mean to set
            # this exact text can see why it had no effect. The popover now sends
            # ``message`` only when the user edited it (a dirty check in
            # ``AutoNudgePopover.save``), so this guard is the belt to that braces and
            # should not fire from the shipped client at all -- if it does, the log line
            # is the signal that some caller is echoing the scrubbed projection back.
            logger.info(
                "autonudge update: dropped a `message` identical to the scrubbed "
                "projection of the stored one (loop=%s, source=%s); the stored message "
                "is unchanged. A caller intending to set this exact text must change it "
                "first -- see the popover dirty check.",
                scrub_loop_text(loop_id, field="id"),
                source,
            )
    if message is not None:
        message, _ = redact_exfiltration_urls(message)
        message, _ = redact_credentials(message)
    if banner is not None:
        # Optional and display-only; ``None`` reached here means "leave
        # unchanged" and was filtered by the caller, so a value present now is a
        # set-or-clear request. The sequence lives in ``normalize_banner``,
        # shared with the arm path; the refusal routes through THIS path's
        # ``_deny`` so it lands in this path's SEL audit.
        banner, banner_error = normalize_banner(banner, absent_ok=False)
        if banner_error:
            return _deny(banner_error, 400)
        if banner:
            # An OPAQUE ``loop_id`` with no slot key, so the refusal needs the stored
            # row -- taken from the single read above, which the write also uses.
            bound = row
            if bound is not None:
                banner_channel_error = banner_unsupported_for(
                    getattr(bound, "slot_key", ""), banner
                )
                if banner_channel_error:
                    return _deny(banner_channel_error, 400)
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

    # Last gate before anything is recorded or written: the caller will serialize the
    # loop we return, and that projection is fail-closed. Asked here so an unusable
    # policy costs a clean 503 instead of a 500 stacked on a committed mutation.
    # AFTER the 400s above, deliberately -- a malformed request should still learn
    # WHAT is malformed rather than be told the policy is down.
    if _scrub_policy_unavailable():
        return _deny(
            "Safety checks are temporarily unavailable, so this goal cannot be saved. If this keeps happening, restart Kiro Crew.",
            503,
        )

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
                        ("banner", banner),
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
            banner=banner,
        )
    except AutoNudgeStoreUnvetted as exc:
        # Same REFUSAL as the composition case above, so it must present identically:
        # the store is untouched, and a bare 500 with no ``code`` reads as a crash.
        _audit("error", f"svc.update refused: {type(exc).__name__}")
        return (
            None,
            "Safety checks are temporarily unavailable, so this goal cannot be saved. If this keeps happening, restart Kiro Crew.",
            503,
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
    banner: str = "",
    source: str,
    caller: str = "",
    # UNGATED by default: this chokepoint is shared with callers whose work is not
    # a pull request (an app's own timer, a goal loop), and inferring a monitor from
    # a message that merely mentions one PR throttles those and can deactivate them
    # outright. The monitor_start surfaces pass ``gate=True`` themselves.
    gate: bool = False,
    monitor: MonitorState | None = None,
    replace_existing: bool = True,
    # Opt-in for the session-directive re-arm path ONLY: with
    # ``replace_existing=False`` it permits displacing a retained row whose
    # stop the SYSTEM imposed (an approval stall, a spent cap or budget, a
    # finished or vanished subject — the ``_stopped_row_is_replaceable``
    # allowlist), so such a row cannot deadlock the re-arm that
    # monitor_update's own refusal message prescribes. Consumer-recorded
    # stops (manual pauses, user stops, session-close retention, research
    # tombstones, quarantined records) stay refused as retained evidence.
    # Dashboard REST creates never set it: their documented contract is
    # any-record 409, preserving retained inspection records.
    replace_stopped: bool = False,
    expected_existing_monitor_id: str | None = None,
    expected_existing_config_generation: int | None = None,
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
    audit_tool = "monitor_watch" if monitor is not None else "autonudge_start"

    def _audit(outcome: str, err: str | None = None) -> None:
        try:
            sel().log_tool_invocation(
                session_key=slot_key,
                source=source,
                tool_name=audit_tool,
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
    monitor_wake_instructions = ""
    if monitor is not None:
        monitor_wake_instructions = monitor.wake_instructions
        if len(monitor_wake_instructions) > MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS:
            return _deny(
                "wake_instructions too long " f"(max {MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS} chars)",
                400,
            )
        monitor_wake_instructions, _ = redact_exfiltration_urls(monitor_wake_instructions)
        monitor_wake_instructions, _ = redact_credentials(monitor_wake_instructions)
        if len(monitor_wake_instructions) > MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS:
            return _deny(
                "wake_instructions too long after redaction "
                f"(max {MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS} chars)",
                400,
            )
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
    # Decidable from the ARGUMENTS alone (slot_key is in hand here), so it sits
    # with the other cheap shape guards rather than beside the banner
    # normalization further down: reaching that point first requires passing
    # channel-session validation, which would answer an unroutable channel +
    # banner request with a 404 about the session and leave the banner problem
    # undiagnosed. The full reasoning is in ``banner_unsupported_for``.
    banner_channel_error = banner_unsupported_for(slot_key, banner)
    if banner_channel_error:
        return _deny(banner_channel_error, 400)
    admission_check: Callable[[], bool]
    if is_channel_key(slot_key):
        # Channel-bound loop (Slack / Discord ...). Validate the session is
        # routable so a nudge fired later has somewhere to reply.
        if slot_key.startswith("slack:"):
            sessions = getattr(state, "sessions", None)
            if sessions is None:
                return _deny(f"unknown slack session {slot_key}", 404)
            channel = sessions.get_channel(slot_key)
            if channel is None:
                return _deny(f"unknown slack session {slot_key}", 404)

            def _slack_admission() -> bool:
                return sessions.get_channel(slot_key) is channel

            admission_check = _slack_admission
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
            authorized_transport = transport
            authorized_dispatcher = dispatcher

            def _discord_admission() -> bool:
                try:
                    return (
                        (getattr(state, "channel_transports", None) or {}).get("discord")
                        is authorized_transport
                        and authorized_dispatcher.is_authorized(user_id)
                        and authorized_dispatcher.current_session_key(user_id) == slot_key
                    )
                except Exception:
                    return False

            admission_check = _discord_admission
        elif slot_key.startswith("webex:"):
            # Deny-by-default, mirroring the Discord branch and for the same
            # reason: an authenticated caller must not be able to mint a loop that
            # DMs an arbitrary Webex user through the agent. DM sessions of
            # allow-listed people only, and only the user's CURRENT key exactly as
            # the dispatcher derives it.
            transports = getattr(state, "channel_transports", None) or {}
            transport = transports.get("webex")
            dispatcher = transport.dispatcher if transport is not None else None
            if transport is None or dispatcher is None:
                return _deny("webex transport not running", 404)
            parts = slot_key.split(":")
            if len(parts) < 4 or parts[2] != "direct":
                return _deny(f"unsupported webex session {slot_key} (DM sessions only)", 400)
            email = parts[3]
            if not transport.is_authorized(email):
                return _deny("webex user is not in the allowed_emails allowlist", 403)
            try:
                current_key = dispatcher.current_session_key(email)
            except Exception:
                current_key = ""
            if slot_key != current_key:
                return _deny("webex session key does not match the user's current session", 404)
            authorized_transport = transport
            authorized_dispatcher = dispatcher

            def _webex_admission() -> bool:
                try:
                    return (
                        (getattr(state, "channel_transports", None) or {}).get("webex")
                        is authorized_transport
                        and authorized_transport.is_authorized(email)
                        and authorized_dispatcher.current_session_key(email) == slot_key
                    )
                except Exception:
                    return False

            admission_check = _webex_admission
        else:
            return _deny(f"unsupported channel session {slot_key}", 400)
    else:
        if slot_key not in state._slots:
            return _deny(f"unknown slot {slot_key}", 404)
        authorized_slot = state._slots.get(slot_key)
        slot_mode = str(getattr(authorized_slot, "mode", ""))
        if slot_mode in {"crew", "member"}:
            return _deny(f"{slot_mode}-mode sessions do not accept direct automation turns", 409)
        if str(getattr(authorized_slot, "memory_mode", "persistent")) != "persistent":
            return _deny("incognito and temporary sessions cannot host automation loops", 403)

        def _dashboard_admission() -> bool:
            current = state._slots.get(slot_key)
            return (
                current is authorized_slot
                and str(getattr(current, "mode", "")) not in {"crew", "member"}
                and str(getattr(current, "memory_mode", "persistent")) == "persistent"
            )

        admission_check = _dashboard_admission
    if len(message) > 8000:
        return _deny("message too long (max 8000 chars)", 400)
    # BEFORE the sentinel unlink below, not after. The auto-default unlink is
    # unconditional (`missing_ok=True`), so an operator's LIVE stop file for an
    # already-running loop is deleted by it. Probing afterwards meant a host whose
    # policy cannot compose destroyed that stop signal and only then refused the arm,
    # leaving the old unattended loop running with no way to stop it.
    #
    # Nothing earlier on this path probes the policy, so this is the ONLY gate: an arm
    # request reaches here with the policy still unprobed. Hoisted ABOVE the
    # ``monitor is None`` arms rather than inside one, because the arm response is
    # serialized through the fail-closed projection for a structured monitor too, so
    # an unusable policy must cost a clean 503 rather than a 500 on an armed loop.
    if _scrub_policy_unavailable():
        return _deny(
            "Safety checks are temporarily unavailable, so this goal cannot be saved. If this keeps happening, restart Kiro Crew.",
            503,
        )
    if monitor is None:
        get_by_slot = getattr(svc, "get_by_slot", None)
        existing = get_by_slot(slot_key) if callable(get_by_slot) else None
        existing_monitor = getattr(existing, "monitor", None)
        if isinstance(existing_monitor, MonitorState) and existing_monitor.wake_in_flight:
            return _deny(
                "existing monitor cannot be replaced while a wake is in flight",
                409,
            )
    if monitor is None:
        # ``banner`` is optional and display-only, so absent/blank is not an error —
        # it means "show the message, as always". Validated HERE rather than beside
        # the message redaction at the top so a rejection routes through ``_deny``
        # and lands in the SEL audit like every other refusal on this path. The
        # sequence itself lives in ``normalize_banner``, shared with the update path.
        # A monitor loop shows its wake row, not a banner, so this only applies to
        # message loops (the ``monitor is None`` arm).
        banner, banner_error = normalize_banner(banner, absent_ok=True)
        if banner_error:
            return _deny(banner_error, 400)
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
    def _audit_metadata() -> dict[str, Any]:
        if monitor is not None:
            return {
                "slot_key": slot_key,
                "kind": monitor.kind,
                "objective": monitor.objective,
                "cadence_secs": monitor.cadence_secs,
                "max_runtime_secs": monitor.budgets.max_runtime_secs,
                "max_agent_turns": monitor.budgets.max_agent_turns,
                "max_tokens": monitor.budgets.max_tokens,
                "max_provider_errors": monitor.budgets.max_provider_errors,
                "caller": caller,
            }
        return {
            "slot_key": slot_key,
            "idle_secs": int(idle_secs),
            "max_cycles": int(max_cycles),
            "max_runtime_secs": int(max_runtime_secs),
            "caller": caller,
        }

    def _critical_invoked_audit() -> None:
        sel().log_tool_invocation(
            session_key=slot_key,
            source=source,
            tool_name=audit_tool,
            outcome="invoked",
            critical=True,
            metadata=_audit_metadata(),
        )

    try:
        await asyncio.get_running_loop().run_in_executor(None, _critical_invoked_audit)
    except Exception:  # noqa: BLE001 - fail closed: no audit ⇒ no loop
        logger.error("autonudge arm denied: SEL audit unavailable", exc_info=True)
        return None, "audit log unavailable — nudge loop not armed", 503
    try:
        if monitor is None:
            add_kwargs: dict[str, Any] = {
                "slot_key": slot_key,
                "message": message,
                "idle_secs": int(idle_secs),
                "max_cycles": int(max_cycles),
                "stop_sentinel_path": stop_sentinel_path,
                "max_runtime_secs": int(max_runtime_secs),
                "banner": banner,
                "admission_check": admission_check,
                "gate": gate,
            }
            if not replace_existing:
                add_kwargs["replace_existing"] = False
            if replace_stopped:
                add_kwargs["replace_stopped"] = True
            loop = await svc.add(
                **add_kwargs,
            )
        else:
            add_monitor_kwargs: dict[str, Any] = {
                "slot_key": slot_key,
                "kind": monitor.kind,
                "target": monitor.target,
                "objective": monitor.objective,
                "cadence_secs": monitor.cadence_secs,
                "budgets": monitor.budgets,
                "wake_instructions": monitor_wake_instructions,
                "admission_check": admission_check,
            }
            if not replace_existing:
                add_monitor_kwargs["replace_existing"] = False
            if replace_stopped:
                add_monitor_kwargs["replace_stopped"] = True
            if expected_existing_monitor_id is not None:
                add_monitor_kwargs["expected_existing_monitor_id"] = expected_existing_monitor_id
                add_monitor_kwargs["expected_existing_config_generation"] = (
                    expected_existing_config_generation
                )
            loop = await svc.add_monitor(
                **add_monitor_kwargs,
            )
    except NudgeAdmissionRefused:
        return _deny("session changed before nudge arm committed", 409)
    except MonitorUpdateConflict as exc:
        return _deny(str(exc), 409)
    except AutoNudgeStoreUnvetted as exc:
        # Mirrors the update path: a refused persist is not a crash, and the arm
        # committed nothing.
        _audit("error", f"svc.add refused: {type(exc).__name__}")
        return (
            None,
            "Safety checks are temporarily unavailable, so this goal cannot be armed. If this keeps happening, restart Kiro Crew.",
            503,
        )
    except Exception as exc:  # noqa: BLE001 - audit the failure, then propagate
        _audit("error", f"svc.add failed: {type(exc).__name__}")
        raise
    try:
        success_metadata = (
            {"loop_id": loop.id, **_audit_metadata()}
            if monitor is not None
            else {
                "loop_id": loop.id,
                "idle_secs": loop.idle_secs,
                "max_cycles": loop.max_cycles,
                "caller": caller,
            }
        )
        sel().log_tool_invocation(
            session_key=slot_key,
            source=source,
            tool_name=audit_tool,
            outcome="success",
            metadata=success_metadata,
        )
    except Exception:  # noqa: BLE001 - armed loop already covered by ``invoked``
        logger.warning(
            "autonudge success audit failed (invoked event covers the arm)", exc_info=True
        )
    return loop, None, 200
