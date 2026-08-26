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
    NudgeAdmissionRefused,
    is_channel_key,
    scrub_loop_text,
)
from kiro_crew.config.loader import workspace_dir_for
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


def normalize_banner(banner: Any, *, absent_ok: bool) -> tuple[str, str | None]:
    """strip -> cap -> redact x2 -> re-cap, in ONE place, called per site.

    Returns ``(value, error)``. The error is a plain string rather than a
    ``_deny`` result because ``_deny`` is nested per authorizer, closing over that
    path's ``_audit`` -- so each caller routes the refusal through its OWN
    ``_deny`` and the rejection still lands in that path's SEL audit. That is what
    keeps this a shared helper CALLED PER SITE rather than a single choke point:
    the previous objection to sharing ("one site would be left silently
    unbounded") was about a single call, not about a single definition, and it
    applies equally to two inlined copies -- which is how the two had already
    drifted apart in ordering.

    ``absent_ok`` is the one genuine difference between the two callers: on the
    arm path ``None`` means "no banner supplied", while on the update path it
    means "leave unchanged" and is filtered out before we get here, so a ``None``
    reaching this function on that path IS a type error.

    ORDERING NOTE: strip runs BEFORE the cap on both paths now. The update path
    previously capped first, so a 500-character banner with trailing whitespace
    was refused there and accepted on the arm path -- the same value, two answers,
    depending only on which endpoint you used. Stripping first is the forgiving
    and consistent choice, and it does not weaken the bound the pre-redaction cap
    exists for: an unbounded string still cannot reach the two linear regex scans.
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
    if len(banner) > MAX_BANNER_CHARS:
        return "", f"banner too long (max {MAX_BANNER_CHARS} chars)"
    if banner:
        # Caller-supplied text that is PERSISTED to the loop store and broadcast to
        # every connected browser as a transcript row on each fire. Being short
        # does not make it a safe place to park a credential.
        #
        # Through the PLATFORM policy, not the bare ``security.redact``: this is an
        # egress site, and ``redact_via_context`` routes to
        # ``current_context().credentials.redact`` so a composed host's own
        # credential patterns apply to the one field this feature adds. The Default
        # policy delegates to ``security.redact``, so a standalone process keeps
        # byte-for-byte the previous behaviour -- the change is only ever additive
        # coverage. It is deliberately FAIL-CLOSED: a host that could not compose
        # its companion raises rather than silently scrubbing to the weaker OSS
        # baseline, which is the right answer for a value we are about to persist.
        banner = redact_via_context(banner)
        # AGAIN, on the redacted value, because redaction can GROW the string:
        # ``[REDACTED: credential]`` is 22 characters and replaces a 20-character
        # AWS access key ID, so an at-cap banner carrying one measures 502 here.
        # A composed policy can grow it further still, which is the other reason
        # this arm cannot be folded into the check above.
        # The check above bounds what we RECEIVE; this one bounds what we STORE.
        # Without it the cap holds at the door and is breached in the store, and
        # the loader's own cap arm then blanks the banner on a later boot -- the
        # operator loses a banner the API accepted with a 200, with no error ever
        # surfaced. A 400 is the visible answer.
        if len(banner) > MAX_BANNER_CHARS:
            return "", (
                f"banner exceeds {MAX_BANNER_CHARS} chars once credentials are "
                "masked — masking can lengthen the text, so shorten the banner"
            )
    return banner, None


def message_is_echoed_projection(current: Any, message: Any) -> bool:
    """True when *message* is exactly the scrubbed projection of the stored one.

    THE single spelling of this predicate. ``authorize_and_update_nudge`` uses it to
    decide whether to drop the field, and the PATCH handler uses it to decide whether to
    set ``message_ignored`` on the response -- if each derived it separately the response
    and the behaviour could disagree, which is the same class of drift the shared
    ``redact_store_value`` spelling exists to avoid.

    Raises ``PlatformCompositionError`` on a host that cannot compose its policy; both
    callers already have to answer for that.
    """
    if current is None or message is None:
        return False
    return scrub_loop_text(getattr(current, "message", None), field="message") == message


def banner_unsupported_for(slot_key: str, banner: Any) -> str | None:
    """Refuse a banner on a channel-bound loop; ``None`` when it is fine.

    ``banner`` shortens the DASHBOARD transcript row, and nothing else. ``_fire``
    routes a channel key to ``_fire_slack_nudge`` / ``_fire_discord_nudge`` /
    ``_fire_webex_nudge``, none of which reads ``loop.banner`` -- both read sites
    live inside ``_fire_dashboard_nudge``. Accepting the field there stored a
    setting the runtime can never honour, and the caller got a 200, so the only
    way to discover it was to notice the row never changed.

    Refusing beats honouring it: a channel nudge IS the turn's own input, so there
    is no second surface to shorten, and rendering the banner instead of the
    message would truncate what the model receives -- the one thing this feature
    exists NOT to do.

    Returns an error string (not a ``_deny`` result) for the same reason
    ``normalize_banner`` does: ``_deny`` is nested per authorizer, so each site
    routes the refusal through its own and keeps its own SEL audit.

    Blank is not "setting a banner" -- ``banner=""`` is the default every
    channel-bound caller already passes, so treating absence as a refusal would
    break all of them. A non-``str`` truthy value still counts as an attempt to
    set one, and is reported as the channel problem it is: on a channel loop the
    field is unsupported whatever its type, so pointing at the type first would
    invite a caller to "fix" it and be refused again.
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


def _scrub_policy_unavailable() -> bool:
    """True when the active credential policy cannot scrub, so nothing may be written.

    Both authorizers mutate and then hand the loop back to a caller that SERIALIZES
    it -- ``dashboard/handlers/autonudge._serialize`` runs every field through
    ``scrub_loop_text`` -> ``redact_via_context``, which is fail-closed. A request
    that scrubs nothing during authorization (a blank banner returns early from
    ``normalize_banner``, and the message compare is gated on ``message is not
    None``) therefore reached ``svc.add``/``svc.update``, COMMITTED, and only then
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
        current = svc.get_by_id(loop_id) if hasattr(svc, "get_by_id") else None
        # ``scrub_loop_text`` routes through ``redact_via_context``, which is
        # FAIL-CLOSED and re-raises ``PlatformCompositionError`` on a host that
        # declares a credential policy it could not compose. Uncaught, that escaped
        # BEFORE ``_deny`` and before the critical ``invoked`` audit below, so a PATCH
        # carrying a message died as an unaudited 500 -- the same "no SEL event at
        # all" hole the banner block guards against, reintroduced above it. Caught
        # here for the same two reasons given there: the status is 503 and must stay
        # distinguishable from the 400s above, and ``_deny`` is nested per authorizer
        # so the refusal lands in THIS path's audit. Refusing, not degrading: this is
        # an ingress decision, unlike the loader arms in ``autonudge.py``.
        #
        # Reachable even though ``_load`` refuses persisted rows on such a host:
        # ``svc.add`` does not scrub the message, and the arm path does not scrub
        # when the banner is empty, so a bannerless loop still reaches ``_loops``.
        #
        # The try spans ONLY the comparison. ``get_by_id`` above is a plain
        # ``_loops.get`` with no scrub, ``message`` is a plain dataclass field, and
        # the ``message = None`` below cannot raise -- so widening the span would
        # catch nothing more and would hide an unrelated raise.
        try:
            resubmitted_projection = message_is_echoed_projection(current, message)
        except PlatformCompositionError:
            return _deny(
                "Safety checks are temporarily unavailable, so this goal cannot be saved. Try again shortly.",
                503,
            )
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
        # Same treatment as ``message``, and for the same reason: a banner is
        # caller-supplied, PERSISTED to the loop store, and broadcast to every
        # connected browser as a transcript row on each fire. Being short does
        # not make it a safe place to park a credential. The sequence lives in
        # ``normalize_banner``; the refusal is routed through THIS path's
        # ``_deny`` so it lands in this path's SEL audit.
        #
        # ``normalize_banner`` scrubs through ``redact_via_context``, which is
        # FAIL-CLOSED: it re-raises ``PlatformCompositionError`` on a host that
        # declares a credential policy it could not compose. Uncaught, that escapes
        # normalisation BEFORE ``_deny`` and before the critical audit, so the
        # request fails with no SEL event at all -- the one guarantee every refusal
        # on this path is supposed to carry. Caught HERE rather than inside
        # ``normalize_banner`` for two reasons: the status is 503 and must stay
        # distinguishable from the 400 that ``banner_error`` carries, and ``_deny``
        # is nested per authorizer precisely so a refusal lands in THIS path's audit.
        # Refusing, not degrading: unlike the loader arms in ``autonudge.py``, which
        # must never raise and so fall back to a placeholder, this is an ingress
        # decision and the honest answer is to reject the write.
        try:
            banner, banner_error = normalize_banner(banner, absent_ok=False)
        except PlatformCompositionError:
            return _deny(
                "Safety checks are temporarily unavailable, so this banner cannot be saved. Try again shortly.",
                503,
            )
        if banner_error:
            return _deny(banner_error, 400)
        if banner:
            # This path holds an OPAQUE ``loop_id`` and no slot key, so the
            # channel check has to resolve the loop first -- refusing only on the
            # arm path would move the hole here rather than close it. Gated on a
            # non-blank banner so a clear (``banner=""``) never pays for a lookup.
            #
            # An unresolvable id is deliberately NOT treated as channel-bound:
            # the loop can legitimately have been removed between request and
            # authorization, and ``svc.update`` answers that with its own 404.
            # Guessing here would report a channel problem for a loop that does
            # not exist.
            # ``get_by_id`` rather than a ``list_all()`` scan, because the same lookup
            # already happens forty lines above for the message compare and one
            # function should not carry two spellings of it.
            #
            # The id is RE-CHECKED rather than trusting the accessor's return, because
            # that is exactly the predicate the scan applied
            # (``getattr(lp, "id", None) == loop_id``). Keeping it preserves the
            # documented behaviour above for a duck-typed service whose accessor
            # answers every id -- without it, an unresolvable id would be reported as
            # channel-bound, which the comment above says it must not be.
            candidate = svc.get_by_id(loop_id) if hasattr(svc, "get_by_id") else None
            bound = candidate if getattr(candidate, "id", None) == loop_id else None
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
            "Safety checks are temporarily unavailable, so auto-nudge details cannot be shown. Try again shortly.",
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
    # Decidable from the ARGUMENTS alone, so it sits with the other cheap shape
    # guards rather than beside the banner normalization further down: reaching
    # that point first requires passing channel-session validation, which would
    # answer an unroutable channel + banner request with a 404 about the session
    # and leave the banner problem undiagnosed. The full reasoning is in
    # ``banner_unsupported_for``.
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

        def _dashboard_admission() -> bool:
            return slot_key in state._slots and state._slots.get(slot_key) is authorized_slot

        admission_check = _dashboard_admission
    if len(message) > 8000:
        return _deny("message too long (max 8000 chars)", 400)
    # ``banner`` is optional and display-only, so absent/blank is not an error —
    # it means "show the message, as always". Validated HERE rather than beside
    # the message redaction at the top so a rejection routes through ``_deny``
    # and lands in the SEL audit like every other refusal on this path. The
    # sequence itself lives in ``normalize_banner``, shared with the update path.
    #
    # Same fail-closed catch as the update path, and it has to be at BOTH sites:
    # fixing one would move the hole rather than close it. See that site for why the
    # catch lives here and why the answer is 503-with-an-audit rather than a weaker
    # scrub.
    try:
        banner, banner_error = normalize_banner(banner, absent_ok=True)
    except PlatformCompositionError:
        return _deny(
            "Safety checks are temporarily unavailable, so this banner cannot be saved. Try again shortly.",
            503,
        )
    if banner_error:
        return _deny(banner_error, 400)
    stop_sentinel_path = (stop_sentinel_path or "").strip()
    if stop_sentinel_path and is_sensitive_path(stop_sentinel_path):
        return _deny("stop_sentinel_path points to a sensitive location", 400)
    # BEFORE the sentinel unlink below, not after. The auto-default unlink is
    # unconditional (`missing_ok=True`), so an operator's LIVE stop file for an
    # already-running loop is deleted by it. Probing afterwards meant a host whose
    # policy cannot compose destroyed that stop signal and only then refused the arm,
    # leaving the old unattended loop running with no way to stop it.
    #
    # The banner probe above cannot stand in for this one: `normalize_banner` returns
    # early on a BLANK banner, so a bannerless arm reaches here with the policy still
    # unprobed. Same gate as the update path, and for the same reason -- the arm
    # response is serialized through the fail-closed projection, so an unusable policy
    # must cost a clean 503 rather than a 500 on top of an armed loop.
    if _scrub_policy_unavailable():
        return _deny(
            "Safety checks are temporarily unavailable, so auto-nudge details cannot be shown. Try again shortly.",
            503,
        )
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
            banner=banner,
            admission_check=admission_check,
        )
    except NudgeAdmissionRefused:
        return _deny("session changed before nudge arm committed", 409)
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
        logger.warning(
            "autonudge success audit failed (invoked event covers the arm)", exc_info=True
        )
    return loop, None, 200
