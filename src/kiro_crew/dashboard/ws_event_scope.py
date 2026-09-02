"""Per-app WebSocket event scope enforcement.

App tokens connected to /api/ws must only receive events they are authorised to
see, based on the ``permissions.events`` declarations in their ``app.json``.


## Event tiers

Tier 0 — always delivered to every connected client (no sensitive payload):
    ``dashboard``  — gateway status snapshot (version, cron count, etc.)

Tier 1 — slot-scoped: delivered only when the calling app is allowed to see the
    slot that the event belongs to.  All events carrying a ``slot`` field fall into
    this tier.  Visibility is controlled by the ``slots:*`` and ``subagent:*``
    declarations in ``permissions.events`` (see below).

Tier 2 — global events with no ``slot`` field.  An app must explicitly list the
    event name (or a wildcard scope) in ``permissions.events`` to receive it.

## Slot visibility scopes (``slots:*``)

Declaration             Slots visible
─────────────────────────────────────────────────────────
(default / slots:own)   Only slots where owner_app == self
slots:user              All slots where origin == SlotOrigin.USER
slots:app:<name>        Slots owned by ``<name>`` — requires ``<name>`` to
                        declare this app in its ``exposeToApps`` list
slots:all               All slots (broad -- see the note on self-declaration below)

The same scope also controls which slot-scoped subagent events are visible.
The ``subagent:*`` declarations are an independent, additive dimension:

Declaration             Subagent events visible (overrides slots:* for subagent events)
─────────────────────────────────────────────────────────────────────────────────────────
(default / subagent)    Own slots only (same as slots:own)
subagent:user           Subagents in user-initiated slots
subagent:app:<name>     Subagents in <name>'s slots (requires exposeToApps opt-in)
subagent:all            All subagent events (broad)

## Global event declarations (``permissions.events``)

notification            Own app's notifications (source_app == self) only.
                        Foreign and system-sourced notifications still denied.
notification:system     Gateway-internal pushes with no source_app -- cron
                        results, send_message, watchlist output. Separate from
                        ``notification`` because that stream is user content
                        rather than the app's own: bundling them would make
                        one declaration a broad grant, the very shape this
                        module exists to remove.
notification_ack / notification_unack carry only a timestamp, so they cannot be
attributed to a source at all and need `notification:all`.
notifications_clear carries no payload at all -- it fires when the WHOLE log is
cleared, not one app's slice -- so it needs `notification:all` for the same reason.
notification_channel_settings IS attributable -- its channel is `<app>.<id>` or
`system.<kind>` -- so own-channel settings ride `notification`, system channels
ride `notification:system`, and foreign channels need `notification:all`.
notification:all        All notifications regardless of source (broad).
sessions                sessions_restarting
yolo                    yolo_expired
artifacts               artifact_update ({slug, version, deleted}; metadata only)
workflow_run_event      Declared by its own literal name -- already the correct
                        per-event Tier 2 shape, and the only events declaration
                        present in the tree today (the workflows app).
log                     Gateway log stream (broad)
browser                 browser_event (broad)

## Scope declarations are SELF-declared

Every scope above is read from the app's own ``app.json``. Nothing in this module
consults an install-time approval, so an app can widen itself by writing
``slots:all`` into its manifest -- which is consistent with the trust model
(installing an app already grants it in-process gateway privilege) but means the
tiers are a *structuring* mechanism plus an audit trail, not a barrier against a
hostile manifest. The one asymmetric check is ``exposeToApps``: cross-app
visibility requires consent from the app being observed, because there the
manifest being trusted is not the one being widened.

Gating the broad scopes through the install-time consent path
(``apps/admission.py``) is tracked separately.

## Dashboard users (empty app claim) are unaffected — full event stream as before.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

# circular import: apps.manager imports config helpers that transitively pull
# in ``kiro_crew.dashboard.state``; keeping ``get_app_manifest`` at module
# scope requires it to load after this module's own definitions are visible,
# which the current import order satisfies (state.py imports us lazily inside
# broadcast_ws, and ws.py imports us before apps.manager). Kept top-level per
# the top-level-imports guideline.
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SEL audit deduplication — WS broadcast is a hot path.  A misconfigured app
# spamming reconnects could otherwise emit tens of thousands of identical
# deny audits per minute and drown out real signal.  We instead guarantee
# *at least one* audit per (app, event_type, reason) tuple per window, which
# gives per-scenario observability with bounded volume.  Repeated denies of
# the same pair within the window are counted and the tally rides the NEXT
# emitted record (``suppressed=N``), so the trail keeps the volume signal a
# burst carries without one record per frame: this gate runs per event PER
# CONNECTED CLIENT on the broadcast hot path, so a streaming response alone is
# chunks x clients decisions.
# ---------------------------------------------------------------------------

_SEL_DEDUP_WINDOW_SECS = 300.0  # 5 minutes
#: (app, event_type, reason) -> (last emitted monotonic ts, denies suppressed since)
_sel_last_audit: dict[tuple[str, str, str], tuple[float, int]] = {}


def _audit_decision(app: str, event_type: str, outcome: str, dedup_reason: str) -> None:
    """Emit a deduplicated SEL audit event for one WS authorization decision.

    ``AUTOSDE.yaml`` (``backend-security-controls``) requires a SEL event for
    every permission decision, which includes the grants, not only the refusals.
    A WS decision is made per client per frame, so an un-deduplicated record per
    grant would be an unbounded write on the broadcast path — hence the same
    window that already collapses deny floods.

    Guaranteed to emit at least once per (app, event_type, *dedup_reason*) tuple
    per ``_SEL_DEDUP_WINDOW_SECS`` window, so a sustained pattern stays
    observable while bursts collapse into one record carrying the suppressed
    count. Grants and denials use different *dedup_reason* values, so one never
    starves the other out of the window.
    """
    key = (app, event_type, dedup_reason)
    now = time.monotonic()
    entry = _sel_last_audit.get(key)
    if entry is not None and (now - entry[0]) < _SEL_DEDUP_WINDOW_SECS:
        # Still inside the window: record that one more identical decision
        # happened so the next emitted audit can report the true volume.
        _sel_last_audit[key] = (entry[0], entry[1] + 1)
        return
    suppressed = entry[1] if entry is not None else 0
    _sel_last_audit[key] = (now, 0)
    try:
        # circular import: sel.py loads config that transitively pulls in
        # ``kiro_crew.dashboard`` submodules — import lazily.
        from kiro_crew.sel import sel as _sel
        _sel().log_api_access(
            caller=app,
            operation="ws_event_scope",
            outcome=outcome,
            source="ws_event_scope",
            resources=(
                f"{event_type} (suppressed={suppressed})" if suppressed else event_type
            ),
        )
    except Exception as exc:
        logger.debug(
            "ws_event_scope: SEL audit for %s %s/%s failed: %s",
            outcome, app, event_type, exc,
        )


def _audit_deny(app: str, event_type: str, reason: str) -> None:
    """Record a denied WS event, keyed by the reason it was denied."""
    _audit_decision(app, event_type, f"denied:{reason}", reason)


def _audit_allow(app: str, event_type: str) -> None:
    """Record a granted WS event.

    The granting BRANCH is deliberately not part of the record. Threading it out
    of every ``return True`` would put the burden on each future branch to
    remember to report itself, which is the failure mode this module keeps
    designing away from — the decision is audited at the one chokepoint instead
    (see :func:`ws_event_allowed`). What an operator needs from this record is
    which app received which event; which declaration earned it is a manifest
    lookup away.
    """
    _audit_decision(app, event_type, "granted", "granted")


# ---------------------------------------------------------------------------
# Events that are always delivered regardless of app scope (no sensitive data)
# ---------------------------------------------------------------------------

_TIER0_ALWAYS = frozenset({
    "dashboard",
    # Dashboard-wide progress signals with no user data.
    "refresh",
    "update_progress",
})

# ---------------------------------------------------------------------------
# Slot-scoped event types: events that carry a ``slot`` key and are
# subject to slot-visibility filtering.  Subagent events are a subset.
# ---------------------------------------------------------------------------

_SLOT_SCOPED_EVENTS = frozenset({
    # Chat content
    "chat_chunk", "chat_thinking", "chat_status", "chat_message", "chat_done",
    "chat_segment", "chat_append", "chat_message_update", "chat_variant_switch",
    # Side-conversation channel (``broadcast_side_result``); carries ``slot``.
    "chat.side_result",
    "heartbeat", "context_usage",
    # Tool / queue
    "tool_call", "tool_result",
    "queue_push", "queue_cancel", "queue_edit", "queue_pop", "queue_reorder",
    "steer_push",
    # Slot metadata / lifecycle
    "slot_title", "slot_clear", "slot_agent_switch", "todo_update",
    # The slot's own MCP session report. Slot-scoped like todo_update and for the
    # same reason: it carries ``slot`` and describes only that slot's session, so
    # a token already scoped to the slot learns nothing wider from it.
    "mcp_report_update",
    "activity_event", "session_summary",
    # Voice
    "voice_chunk", "voice_complete", "voice_error",
    # Approvals and question cards (carry a slot field)
    "approval", "approval_resolved", "question_card",
    # Subagent lifecycle. Every one of these is fired through
    # ``SubagentManager._fire_event`` and reaches the wire via
    # ``broadcast_ws(etype, ...)`` with a VARIABLE etype (slack/gateway.py's
    # ``_subagent_event``), so a guard that scans broadcast call sites for string
    # literals cannot see them -- ``TestFireEventNamesAreClassified`` reads the
    # emitter's own literals instead. Each payload carries id + slot.
    "subagent_spawn", "subagent_done", "subagent_tool", "subagent_chunk",
    "subagent_snapshot", "subagent_status", "subagent_queued",
    "subagent_stalled", "subagent_retrying", "subagent_recovering",
    "subagent_injection_failed",
    # Slack-gateway driven, slot-scoped
    "autonudge_state", "batch_finished", "spawn_batch_started",
    # Workflows / misc
    "workflow_result_injected", "refine",
})

_SUBAGENT_EVENTS = frozenset({
    "subagent_spawn", "subagent_done", "subagent_tool", "subagent_chunk",
    "subagent_snapshot", "subagent_status", "subagent_queued",
    "subagent_stalled", "subagent_retrying", "subagent_recovering",
    "subagent_injection_failed",
})

#: Coalesced subagent frames emitted above ``SubagentEventCoalescer`` threshold
#: (default 8 active subagents): ONE frame carries MANY subagents' rows, so
#: there is no single top-level ``slot`` to scope it by. Left OUT of
#: ``_SLOT_SCOPED_EVENTS`` deliberately — the gate admits the frame and
#: ``DashboardState._serialize_for_client`` drops the individual items the app
#: may not see (the same split already used for the ``slots`` re-push). The
#: classification is what keeps them out of the unknown-event deny, which would
#: cost an app all subagent status and output once the coalescer engages.
_SUBAGENT_BATCH_EVENTS = frozenset({
    "subagent_batch_update",
    "subagent_batch_chunks",
})

#: Payload key holding the per-item list, per batch event type.
_SUBAGENT_BATCH_ITEM_KEY = {
    "subagent_batch_update": "updates",
    "subagent_batch_chunks": "chunks",
}

# ---------------------------------------------------------------------------
# Global event type → required declaration mapping
# ---------------------------------------------------------------------------

# Notification events whose delivery depends on WHO sent them, not just on the
# declaration. ``notification_channel_settings`` is deliberately absent: it is
# channel config ({muted, priority}) with no source_app, so source filtering
# would deny it outright rather than scope it.
# Only events whose payload actually CARRIES a source. `notification_ack` /
# `notification_unack` broadcast a bare `{"ts": ...}` (see state.py), so they are
# absent here: filtering by a field the payload never has is not a filter, and a
# source-less frame would read as the system source. They are gated by their
# plain `notification` declaration via `_GLOBAL_EVENT_DECLARATIONS` instead,
# which is the strongest statement available for a payload that carries a
# timestamp and nothing else.
_SOURCE_FILTERED_EVENTS = frozenset({
    "notification",
})

# The canonical `source` values a note can carry, set server-side and not
# overridable from a request body:
#   "system"      -- payload_from_legacy() / _deliver_note() in notifications/bus.py
#   "app:<name>"  -- api_push_notification() in handlers/notifications_push.py
# Kept as constants here rather than imported (bus.py's is private) with
# `TestNotificationSourceParsing` pinning that the two agree.
# Mirrors apps/event_bus.APP_EVENT_WS_TYPE. Kept local rather than imported so
# the WS gate does not pull the app layer in at module scope;
# `TestAppEventFrames` pins that the two agree.
_APP_EVENT_WS_TYPE = "app_event"

_SYSTEM_SOURCE = "system"
_APP_SOURCE_PREFIX = "app:"


#: Notification metadata that carries NO attribution -- a bare ``{"ts": ...}``
#: (see state.py). Nothing in the payload says whose notification was acked, so
#: own-only ``notification`` cannot be honoured for them and they take the broad
#: scope. Allowing them on the plain declaration would grant an app the ack
#: stream for every OTHER app's and the system's notifications.
#: ``notifications_clear`` joins them for the same reason: it fires with an
#: empty payload when the WHOLE log is cleared, not just this app's slice, so
#: it cannot be judged "own" either.
_UNATTRIBUTED_NOTIFICATION_EVENTS = frozenset({
    "notification_ack",
    "notification_unack",
    "notifications_clear",
})

#: `<app>.<channel_id>` (handlers/notifications_push) or `system.<kind>`
#: (notifications/bus SYSTEM_CHANNELS) -- the prefix names the owner.
_CHANNEL_SETTINGS_EVENT = "notification_channel_settings"


def notification_channel_owner(channel: str) -> str:
    """The app (or "system") that owns a notification channel."""
    return channel.split(".", 1)[0] if "." in channel else ""


def notification_source_app(source: str) -> str:
    """The app that produced a notification, or "" if it was not an app push.

    The source is a CANONICAL, prefixed identity — `app:mochi`, never `mochi` —
    so comparing it against a bare app name silently never matches.
    """
    if source.startswith(_APP_SOURCE_PREFIX):
        return source[len(_APP_SOURCE_PREFIX):]
    return ""


_GLOBAL_EVENT_DECLARATIONS: dict[str, str] = {
    "notification": "notification",
    # Present so the completeness guard sees them classified -- the ACTUAL gate is
    # the stricter branch in ws_event_allowed, which these never reach.
    "notification_ack": "notification",
    "notification_unack": "notification",
    "notifications_clear": "notification",
    # Channel mute/priority metadata only ({muted, priority}) -- same domain as
    # the notification events themselves, so it rides the same declaration.
    "notification_channel_settings": "notification",
    "sessions_restarting": "sessions",
    "yolo_expired": "yolo",
    # Artifact metadata only ({slug, version, deleted}) -- no content, no slot.
    "artifact_update": "artifacts",
    # Metadata only ({slug, kind, target}) -- but a pending skill's slug names
    # what the user is building, so it takes an explicit declaration rather
    # than riding Tier 0. Same treatment as artifact_update above.
    "skills.pending_changed": "skills",
    # Declared by its own literal name: this is already the correct Tier 2
    # shape (per-event opt-in), and it is the one declaration that exists in
    # the tree today (the workflows app), so the name must not change.
    "workflow_run_event": "workflow_run_event",
    # Privileged
    "log": "log",
    "browser_event": "browser",
}


#: The ``slots`` frame is the one event whose ENVELOPE carries fields that are
#: not part of the thing being filtered. ``data`` is the slot list, and
#: ``_serialize_for_client`` re-filters that per app -- but the envelope also
#: carries two GLOBAL safety-posture booleans that no slot filter can narrow:
#:
#:   ``yolo``           -- is the blanket approval override active right now
#:   ``channelTrusted`` -- does any channel currently hold trust
#:
#: Both describe the operator's security posture, not the app's own slots, so
#: re-emitting them verbatim would give every app token an undeclared live read
#: of it on each re-push. ``yolo`` already HAS a scope in the
#: vocabulary (it gates ``yolo_expired``), so it reuses that one rather than
#: inventing a parallel name; ``channelTrusted`` has none and nothing in the app
#: SDK reads it, so it is withheld from app tokens outright instead of growing
#: the grant surface for a field with no consumer.
_YOLO_SCOPE = _GLOBAL_EVENT_DECLARATIONS["yolo_expired"]


def slots_envelope_extras(
    allowed_events: frozenset[str], *, yolo: bool
) -> dict[str, bool]:
    """The envelope fields beyond ``data`` an app token may see on ``slots``.

    Returns only the permitted keys -- an omitted key must be left OUT of the
    envelope rather than sent as a falsy default: ``false`` is a factual claim
    ("the override is off") that a client acts on, and the shipped client
    already handles absence (it checks ``r.yolo !== undefined`` before it
    dispatches). ``channelTrusted`` is never returned. Callers build the
    envelope from this mapping instead of re-deriving the scope names, so the
    gate stays the single source of truth for what a declaration buys.
    """
    if _YOLO_SCOPE in allowed_events or f"{_YOLO_SCOPE}:all" in allowed_events:
        return {"yolo": yolo}
    return {}


# Everything the legacy ``"*"`` declaration grants. Derived from the tables
# above rather than hand-listed, so a declaration added later is covered by the
# wildcard automatically instead of being silently dropped from it.
_WILDCARD_SCOPES: frozenset[str] = frozenset(
    {"slots:all", "subagent:all", "notification:all", "notification:system"}
    | set(_GLOBAL_EVENT_DECLARATIONS.values())
    | {f"{d}:all" for d in _GLOBAL_EVENT_DECLARATIONS.values()}
)


# ---------------------------------------------------------------------------
# Allowed-event set computation (called once at WS connect time)
# ---------------------------------------------------------------------------

def build_allowed_event_set(events_declared: list[str]) -> frozenset[str]:
    """Convert a raw ``permissions.events`` list into a normalised frozenset.

    The returned set is stored on the WS connection object and passed to
    :func:`ws_event_allowed` on every broadcast.

    ``"*"`` is EXPANDED here, not carried through. It predates this scope
    vocabulary and meant "every event", but as an opaque set member no gate
    below recognises it — so a manifest that already declares ``["*"]`` would
    keep its subscription and silently receive nothing. Expanding at the one
    place that builds the set (instead of special-casing the wildcard in the
    event gate and again in each replay-subscription gate) keeps that decision
    in a single spot and cannot drift out of sync with them.
    """
    if "*" in events_declared:
        return _WILDCARD_SCOPES
    return frozenset(events_declared)


# ---------------------------------------------------------------------------
# Core filter: is this event allowed for this app token?
# ---------------------------------------------------------------------------

def ws_event_allowed(
    event_type: str,
    data: dict[str, Any],
    *,
    app: str,
    allowed_events: frozenset[str],
    state: DashboardState,
) -> bool:
    """Return True if an app token identified by *app* may receive this event.

    A thin wrapper over :func:`_decide_ws_event` whose only job is to make the
    SEL audit unmissable. The decision function has many ``return True`` paths
    (Tier 0, the always-admitted envelopes, each slot scope, each global scope),
    and auditing at each one would mean every future branch has to remember to
    report itself. Auditing the RESULT here means a branch added later is
    covered by construction.

    Denials are audited inside the decision function, where the reason is known.
    """
    allowed = _decide_ws_event(
        event_type, data, app=app, allowed_events=allowed_events, state=state
    )
    if allowed:
        _audit_allow(app, event_type)
    return allowed


def _decide_ws_event(
    event_type: str,
    data: dict[str, Any],
    *,
    app: str,
    allowed_events: frozenset[str],
    state: DashboardState,
) -> bool:
    """The authorization decision itself. Audits its own denials.

    ``data`` is the payload dict passed to ``broadcast_ws``.  This function is
    called **before** sending to each WS client; returning False suppresses the
    send.

    Dashboard-user tokens (empty *app*) always receive everything — callers
    should skip this function for them.  As a defense-in-depth measure this
    function still fails closed (returns False) if called with an empty app,
    since ``assert`` is compiled out under ``python -O``.
    """
    if not app:
        # Deny-by-default (CWE-269): a caller invoking this without an app
        # identity has no basis for authorisation.  Audit the denial so a
        # future refactor that reaches this branch (bypassing the caller's
        # dashboard-user pre-check) is observable in the trail.
        _audit_deny(app or "<empty>", event_type, "empty_app_denied")
        return False

    # Tier 0: always delivered
    if event_type in _TIER0_ALWAYS:
        return True

    # Revoked app: Tier 0 and nothing else. Checked here rather than left to the
    # declaration intersection because the branches below (``slots``, subagent
    # batches, and the own-slot default in ``_slot_visible``) deliberately admit
    # frames WITHOUT consulting ``allowed_events``, so an empty declaration set
    # does not reach them.
    if app_events_revoked(app):
        _audit_deny(app, event_type, "app_disabled")
        return False

    # ``slots`` is a full slot-list re-push.  The event itself is always
    # delivered — payload-level per-app filtering in
    # ``DashboardState._serialize_for_client`` enforces that an app only
    # sees the slots it's authorised for (own slots by default; foreign
    # slots require ``slots:user`` / ``slots:app:X`` / ``slots:all``).
    # Denying at the gate level would break the documented default that
    # every app sees updates to its OWN slots without an explicit
    # declaration, and would leave the app's sidebar stale after connect.
    if event_type == "slots":
        return True

    # Coalesced subagent batches carry per-item slots, not one frame slot —
    # admit here and let ``_serialize_for_client`` filter the items.
    if event_type in _SUBAGENT_BATCH_EVENTS:
        return True

    # Tier 1: slot-scoped events
    slot_key: str | None = data.get("slot")
    # ``slot_title`` and ``session_summary`` use ``key`` for the slot
    # identifier (rather than ``slot``) — normalise so we don't misclassify
    # them as no-slot-key.
    if slot_key is None and event_type in ("slot_title", "session_summary"):
        slot_key = data.get("key")
    # A GLOBALLY classified event must be judged by its own declaration, never
    # by a slot field that happens to appear in its payload. ``notify()`` meta
    # keys merge FLAT into the note (notifications/bus.py: "the frontend reads
    # note.job_id / note.slot / note.session_key directly"), and the notification
    # branch of ``DashboardState._broadcast`` hands that whole note to this gate
    # as ``data`` — so a real caller like the Slack heartbeat
    # (``notify("heartbeat", ..., meta={"slot": slot.key})``) puts a top-level
    # ``slot`` on a ``notification`` frame. Inferring slot-scoping from it would
    # route the frame into the slot branch below and return on slot visibility
    # ALONE, delivering the title/body to an app holding only ``slots:*`` and
    # never enforcing the ``notification`` scope this event actually requires.
    # The two tables are disjoint, so this only ever strips smuggled keys.
    if event_type in _GLOBAL_EVENT_DECLARATIONS:
        slot_key = None
    if event_type in _SLOT_SCOPED_EVENTS or slot_key is not None:
        if slot_key is None:
            # Unknown slot-scoped event shape — deny to be safe
            _audit_deny(app, event_type, "slot_scoped_no_slot_key")
            return False
        slot: _ChatSlot | None = state._slots.get(slot_key)
        if slot is None:
            # Slot no longer exists (race) — deny
            _audit_deny(app, event_type, "slot_missing")
            return False

        if event_type in _SUBAGENT_EVENTS:
            allowed = _subagent_visible(slot, app, allowed_events, state)
        else:
            allowed = _slot_visible(slot, app, allowed_events, state)
        if not allowed:
            _audit_deny(app, event_type, "slot_scope_denied")
        return allowed

    # Tier 2: global events
    # Unattributable notification metadata: only the broad scope covers it.
    if event_type in _UNATTRIBUTED_NOTIFICATION_EVENTS:
        if "notification:all" in allowed_events:
            return True
        _audit_deny(app, event_type, "notification_metadata_needs_all")
        return False

    # Channel settings ARE attributable, so do not force the broad scope on them:
    # an app learning its OWN channel was muted is exactly what `notification`
    # grants, while another app's channel is not.
    if event_type == _CHANNEL_SETTINGS_EVENT:
        owner = notification_channel_owner(str(data.get("channel", "")))
        if "notification:all" in allowed_events:
            return True
        if owner and owner == app and "notification" in allowed_events:
            return True
        if owner == _SYSTEM_SOURCE and "notification:system" in allowed_events:
            return True
        _audit_deny(app, event_type, "notification_channel_not_owned")
        return False

    # App-published events all ride ONE fixed WS type with the real event name
    # inside the envelope (see apps/event_bus.APP_EVENT_WS_TYPE), so the table
    # lookup above can never match them and this branch is what keeps them out
    # of the global deny -- without it an app receives none of its OWN events.
    #
    # Ownership decides it: the envelope names its publisher, and EventBus
    # already refused to publish anything outside that app's declared
    # `permissions.events`. So a publisher receiving its own event back needs no
    # second declaration check -- and re-checking would BREAK the apps that
    # declare `["*"]`, whose wildcard is expanded into core scopes that do not
    # contain their app-chosen event names. Frames from another app are denied:
    # app events have no cross-app opt-in (that is what exposeToApps does for
    # slots), and an unattributable envelope is denied too.
    if event_type == _APP_EVENT_WS_TYPE:
        publisher = str(data.get("app", ""))
        inner = str(data.get("event", ""))
        if publisher and inner and publisher == app:
            return True
        _audit_deny(app, event_type, "app_event_not_owned")
        return False

    required_decl = _GLOBAL_EVENT_DECLARATIONS.get(event_type)
    if required_decl is None:
        # Unknown event type — deny unknown events for app tokens
        logger.debug("ws_event_scope: unknown event %r denied for app %r", event_type, app)
        _audit_deny(app, event_type, "unknown_event")
        return False

    # notification: filtered by source_app.  An app must declare
    # ``notification`` to receive its OWN notifications and ``notification:all``
    # to receive foreign ones.  Deny-by-default (CWE-269): if neither is
    # declared, own-app notifications are also denied — apps that want push
    # notifications must opt in explicitly.
    #
    # System-sourced notifications (source == "system" or source == "") are
    # gateway-internal sends (send_message MCP tool, heartbeat, cron fallback).
    # They carry no ``source_app`` because they flow through state.notify(),
    # which pre-dates per-app channels. They need their OWN declaration
    # (``notification:system``): that stream is user content, not the app's
    # own, so folding it into ``notification`` would make a single declaration
    # a broad grant -- the shape this module exists to remove.
    if event_type in _SOURCE_FILTERED_EVENTS:
        # The note's field is `source` and its app form is PREFIXED
        # (e.g. `app:mochi-pet`); `source_app` and `app` are not keys any emitter
        # writes. Parse the canonical value and let ONLY the literal "system"
        # mean system, so an unrecognised or absent source is denied rather than
        # treated as the shared stream that every `notification:system` holder
        # reads.
        source = str(data.get("source", ""))
        source_app = notification_source_app(source)
        if "notification:all" in allowed_events:
            return True
        if source_app and source_app == app and "notification" in allowed_events:
            return True
        if source == _SYSTEM_SOURCE and "notification:system" in allowed_events:
            # ``notification:all`` is handled above; the system stream has its
            # own opt-in because it carries user content, not the app's own.
            return True
        if source_app and source_app == app:
            _audit_deny(app, event_type, "notification_not_declared")
        elif source == _SYSTEM_SOURCE:
            _audit_deny(app, event_type, "notification_system_not_declared")
        else:
            _audit_deny(app, event_type, "notification_scope_denied")
        return False

    if required_decl in allowed_events or f"{required_decl}:all" in allowed_events:
        return True
    _audit_deny(app, event_type, "global_scope_denied")
    return False


# ---------------------------------------------------------------------------
# Slot visibility helpers
# ---------------------------------------------------------------------------

def _slot_visible(
    slot: _ChatSlot,
    app: str,
    allowed_events: frozenset[str],
    state: DashboardState,
) -> bool:
    """Return True if *app* may receive slot-scoped events for *slot*."""
    # circular import: state.py's broadcast_ws imports ws_event_allowed from
    # this module at runtime; importing SlotOrigin at module scope would
    # create a bootstrap cycle during state.py initialisation.
    from kiro_crew.dashboard.state import SlotOrigin

    # Own slot: visible unless the app has been revoked. ``filter_slots_for_app``
    # calls this directly, bypassing ``ws_event_allowed``'s revocation check, so
    # the guard has to repeat here or a disabled app still gets its own slots.
    if getattr(slot, "_app", "") == app:
        return not app_events_revoked(app)

    # slots:all -- broad, self-declared (see module docstring)
    if "slots:all" in allowed_events:
        return True

    # slots:user — user-initiated slots
    # slots:user — user-initiated slots only.  ``getattr`` defaults to ``""``
    # (a sentinel that matches NO scope declaration) so a pre-migration slot
    # or a race condition that leaves ``_origin`` unset remains INVISIBLE
    # rather than being silently classified as USER.  Deny-by-default (CWE-269).
    if getattr(slot, "_origin", "") == SlotOrigin.USER and "slots:user" in allowed_events:
        return True

    # slots:app:<name> — specific app's slots, requires target opt-in
    slot_owner = getattr(slot, "_app", "")
    if slot_owner and f"slots:app:{slot_owner}" in allowed_events:
        if _target_exposes_to(slot_owner, app, state):
            return True

    return False


def _subagent_visible(
    slot: _ChatSlot,
    app: str,
    allowed_events: frozenset[str],
    state: DashboardState,
) -> bool:
    """Return True if *app* may receive subagent events for *slot*.

    Subagent visibility is an independent dimension from slot content visibility:
    an app may declare ``subagent:user`` without needing ``slots:user``.
    Falls back to slot visibility if no subagent-specific declaration is present.
    """
    # circular import: see _slot_visible above.
    from kiro_crew.dashboard.state import SlotOrigin

    # Own slot: visible unless the app has been revoked — see _slot_visible.
    # ``filter_subagent_batch_for_app`` also reaches this without passing through
    # ``ws_event_allowed``.
    if getattr(slot, "_app", "") == app:
        return not app_events_revoked(app)

    # subagent:all
    if "subagent:all" in allowed_events:
        return True

    # subagent:user
    # subagent:user — same fail-closed default as _slot_visible above.
    if getattr(slot, "_origin", "") == SlotOrigin.USER and "subagent:user" in allowed_events:
        return True

    # subagent:app:<name>
    slot_owner = getattr(slot, "_app", "")
    if slot_owner and f"subagent:app:{slot_owner}" in allowed_events:
        if _target_exposes_to(slot_owner, app, state):
            return True

    # Fall back to general slot visibility (if you can see the slot, you can see its subagents)
    return _slot_visible(slot, app, allowed_events, state)


# ---------------------------------------------------------------------------
# Manifest exposure cache — ``get_app_manifest`` stats, reads and re-parses the
# JSON file on every call (no internal cache).  ``_target_exposes_to`` runs on
# the WS broadcast hot path (once per cross-app slot event via
# ``_slot_visible``/``_subagent_visible``), and ``ws_event_allowed`` is a
# SYNCHRONOUS function called from inside ``_send_ws_all``'s per-client loop —
# it cannot await, so it must never touch the disk itself.  Every load is
# therefore pushed off the loop and the cache is served
# stale-while-revalidate:
#
#   fresh entry    -> return it
#   stale entry    -> return the STALE value, schedule a refresh
#   no entry       -> return empty (fail closed), schedule a refresh
#
# A cold miss withholds one cross-app frame until the refresh lands, which is
# the safe direction and matches this module's fail-closed policy.  Serving a
# stale entry costs at most ``_MANIFEST_EXPOSE_TTL_SECS`` of staleness on a
# manifest edit — the same bound the previous blocking implementation had.
# ---------------------------------------------------------------------------

_MANIFEST_EXPOSE_TTL_SECS = 30.0
_exposeto_cache: dict[str, tuple[float, frozenset[str]]] = {}
#: Apps with a refresh already scheduled, so a broadcast burst on a cold or
#: stale entry queues ONE thread job instead of one per event per client.
_exposeto_refreshing: set[str] = set()


def _read_expose_to(target_app: str) -> frozenset[str]:
    """Blocking read of *target_app*'s ``exposeToApps`` set. Fail-closed.

    Synchronous by design — callers MUST run it off the event loop (see
    ``_schedule_expose_to_refresh``).

    A disabled *target_app* exposes to nobody, even if its manifest still
    declares ``exposeToApps``: disabling does not delete the manifest, so
    without this check an observing app's ``slots:app:<target_app>`` grant
    would outlive the disable — mirroring the check :func:`app_events_revoked`
    already applies to the target's OWN socket.
    """
    try:
        if not is_app_enabled(target_app):
            return frozenset()
        manifest = get_app_manifest(target_app)
        if manifest is None:
            return frozenset()
        return frozenset(manifest.permissions.exposeToApps)
    except Exception:
        logger.debug(
            "ws_event_scope: could not load manifest for %r, denying cross-app access",
            target_app,
        )
        return frozenset()


def _schedule_expose_to_refresh(target_app: str) -> None:
    """Refresh one cache entry off the event loop, at most one job in flight.

    Falls back to a synchronous read when there is no running loop (tests, CLI
    paths): blocking is only a problem on the loop thread, and refusing to load
    at all there would make the cache permanently cold.
    """
    if target_app in _exposeto_refreshing:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _exposeto_cache[target_app] = (time.monotonic(), _read_expose_to(target_app))
        return

    _exposeto_refreshing.add(target_app)

    try:
        future = loop.run_in_executor(None, _read_expose_to, target_app)
    except RuntimeError:
        # Loop shutting down / executor gone — drop the refresh; the entry stays
        # as-is and the next broadcast reschedules.
        _exposeto_refreshing.discard(target_app)
        return

    def _store(fut: Any) -> None:
        _exposeto_refreshing.discard(target_app)
        try:
            _exposeto_cache[target_app] = (time.monotonic(), fut.result())
        except BaseException:
            # BaseException, not Exception: a cancelled future raises
            # CancelledError, which does not derive from Exception, and letting
            # it escape a call_soon callback only produces loop-handler noise.
            # The entry is already released above, so a failed refresh just
            # leaves the previous value (or nothing) for the next broadcast to
            # retry against.
            logger.debug(
                "ws_event_scope: exposeToApps refresh failed for %r", target_app
            )

    future.add_done_callback(_store)


def _load_expose_to(target_app: str) -> frozenset[str]:
    """Return the *target_app*'s ``permissions.exposeToApps`` set, cached.

    Never blocks: an empty frozenset means "expose to nobody", which is also
    what a not-yet-loaded entry returns (fail closed).
    """
    cached = _exposeto_cache.get(target_app)
    if cached is not None:
        if (time.monotonic() - cached[0]) < _MANIFEST_EXPOSE_TTL_SECS:
            return cached[1]
        _schedule_expose_to_refresh(target_app)
        return cached[1]
    _schedule_expose_to_refresh(target_app)
    return _exposeto_cache.get(target_app, (0.0, frozenset()))[1]


# ---------------------------------------------------------------------------
# Live declaration refresh — a socket's scope must be able to SHRINK
#
# ``_allowed_events`` is resolved once at connect. A manifest edit or an
# ``app disable`` (which rewrites the registry without closing sockets) would
# otherwise leave the revoked scopes usable on an already-open connection until
# it happens to drop.
#
# ``effective_allowed_events`` therefore INTERSECTS the connect-time snapshot
# with the currently declared set on every decision. Intersection, not
# replacement, is the whole design:
#
#   * a NARROWED or deleted manifest takes effect immediately (the point), and
#   * a WIDENED manifest does NOT retroactively grant an open socket new scopes
#     — that requires a reconnect, so the grant is always one the connection was
#     authenticated for, and a manifest edit can never escalate a live session.
#
# Closing the socket instead was rejected: it would cut a streaming turn
# mid-flight and turn a manifest save into a reconnect storm, while delivering
# no tighter guarantee than withholding the events does.
#
# The reload is the same shape as the ``exposeToApps`` cache above — never a
# disk read on the loop, refreshed off-loop, stale-while-revalidate — because
# this runs on the same synchronous broadcast path. The one difference is what a
# COLD miss returns: an unknown ``exposeToApps`` denies (a cross-app grant is
# opt-in), but an unknown declaration set must fall back to the CONNECT-TIME
# SNAPSHOT rather than to empty, or the first broadcast after a gateway restart
# would withhold every event from every app until the refresh lands. The
# snapshot is itself an authenticated read of the same file, so leaning on it
# for one refresh interval never widens anything.
# ---------------------------------------------------------------------------

_declared_cache: dict[str, tuple[float, bool, frozenset[str]]] = {}
_declared_refreshing: set[str] = set()


def _read_declared_events(app: str) -> tuple[bool, frozenset[str]]:
    """Blocking read of *app*'s ``enabled`` flag and ``permissions.events`` set.

    Synchronous by design — callers MUST run it off the event loop.

    ENABLEMENT is part of the answer, not a separate concern: ``disable_app``
    flips ``enabled`` in ``installed.json`` and leaves ``app.json`` untouched, so
    reading the manifest alone reports a disabled app's declarations unchanged and
    the intersection would keep honouring them.

    Both facts come back from ONE read, and are cached as one tuple, because they
    are only sound together: the empty declaration set of a disabled app is
    indistinguishable from that of an enabled app which declares no events, and
    those two must be treated differently (the latter still sees its OWN slots by
    documented default, the former must see nothing but Tier 0). A second cache
    keyed independently could report ``enabled`` for a set that was read while the
    app was disabled.

    An app that is disabled or uninstalled reports ``(False, frozenset())`` — no
    declarations AND revoked, so ``app_events_revoked`` also withholds the
    own-slot default. Revocation requires POSITIVE evidence of disablement: an
    unreadable or missing ``app.json`` on a still-enabled app reports ``(True,
    frozenset())`` instead, because a corrupt/transient manifest read is not a
    revocation and blanking the app's own chat over one would be a costly false
    positive. It still declares nothing, so every DECLARED scope is withheld.
    """
    try:
        enabled = is_app_enabled(app)
    except Exception:
        # Indeterminate — not positive evidence of disablement. Withhold the
        # declared scopes, but leave the own-slot default alone.
        logger.debug("ws_event_scope: could not read enablement for %r", app)
        return (True, frozenset())
    if not enabled:
        return (False, frozenset())
    try:
        manifest = get_app_manifest(app)
        if manifest is None:
            return (True, frozenset())
        return (True, build_allowed_event_set(list(manifest.permissions.events)))
    except Exception:
        logger.debug("ws_event_scope: could not reload declarations for %r", app)
        return (True, frozenset())


def _schedule_declared_refresh(app: str) -> None:
    """Refresh one declaration entry off the event loop, one job in flight."""
    if app in _declared_refreshing:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        enabled, events = _read_declared_events(app)
        _declared_cache[app] = (time.monotonic(), enabled, events)
        return

    _declared_refreshing.add(app)

    try:
        future = loop.run_in_executor(None, _read_declared_events, app)
    except RuntimeError:
        _declared_refreshing.discard(app)
        return

    def _store(fut: Any) -> None:
        _declared_refreshing.discard(app)
        try:
            enabled, events = fut.result()
            _declared_cache[app] = (time.monotonic(), enabled, events)
        except BaseException:
            # BaseException: a cancelled future raises CancelledError, which is
            # not an Exception, and letting it escape a call_soon callback only
            # produces loop-handler noise.
            logger.debug("ws_event_scope: declaration refresh failed for %r", app)

    future.add_done_callback(_store)


def load_declared_events_for_connect(app: str) -> tuple[bool, frozenset[str]]:
    """Blocking connect-time read of *app*'s enablement + scopes, priming the cache.

    Synchronous by design — the WS connect path MUST run it off the event loop.

    Callers get the pair to act on directly (refuse the socket when disabled) AND
    the cache is primed as a side effect, which closes the cold-miss window that
    would otherwise open on every reconnect: ``app_events_revoked`` reports NOT
    revoked on a cold cache, so without this the initial slots push and the log
    replay would both be judged against an unverified snapshot before the first
    background refresh landed. Priming here means the first frame is already
    gated on an authoritative read.

    This is also the only place the connect snapshot is built, so the connect
    path cannot drift from what the live narrowing path reads.
    """
    enabled, events = _read_declared_events(app)
    _declared_cache[app] = (time.monotonic(), enabled, events)
    return (enabled, events)


def effective_allowed_events(app: str, connect_snapshot: frozenset[str]) -> frozenset[str]:
    """The scopes *app* may use RIGHT NOW on a socket opened with *connect_snapshot*.

    Never blocks. Returns the intersection with the currently declared set, so a
    revoked scope stops being honoured within one refresh interval without the
    socket being closed.
    """
    cached = _declared_cache.get(app)
    if cached is not None:
        if (time.monotonic() - cached[0]) >= _MANIFEST_EXPOSE_TTL_SECS:
            _schedule_declared_refresh(app)
        return connect_snapshot & cached[2]
    _schedule_declared_refresh(app)
    return connect_snapshot


def app_events_revoked(app: str) -> bool:
    """Return True if *app* is currently disabled/uninstalled, so it gets Tier 0 only.

    Never blocks. This is the companion to :func:`effective_allowed_events`: the
    intersection there withholds every DECLARED scope from a disabled app, but the
    own-slot default in :func:`_slot_visible` / :func:`_subagent_visible` grants an
    app its own slots WITHOUT consulting declarations at all, so narrowing the
    declaration set cannot reach it. ``disable_app`` does not revoke the app token
    (``token_auth`` has no enablement check, and every app backend route gates on
    ``is_app_enabled`` itself), so a disabled app can still hold an open
    ``/api/ws`` socket — without this, its own slots' chat content keeps streaming.

    A COLD miss reports NOT revoked (and schedules the refresh) for the same
    reason the declaration cache falls back to the connect snapshot: reporting
    "revoked" for an unknown app would blank every app's own slots on the first
    broadcast after a gateway restart. One refresh interval of the pre-existing
    behaviour is the conservative side here; the socket was authenticated against
    the same file at connect.
    """
    cached = _declared_cache.get(app)
    if cached is None:
        _schedule_declared_refresh(app)
        return False
    if (time.monotonic() - cached[0]) >= _MANIFEST_EXPOSE_TTL_SECS:
        _schedule_declared_refresh(app)
    return not cached[1]


def _target_exposes_to(target_app: str, requesting_app: str, state: DashboardState) -> bool:
    """Return True if *target_app*'s manifest allows *requesting_app* to observe it.

    Reads ``permissions.exposeToApps`` from the target app's installed
    manifest.  Uses a 30-second local TTL cache (``_exposeto_cache``) to
    bound WS broadcast hot-path disk I/O — ``get_app_manifest`` itself
    re-reads and re-parses the JSON on every call, so without this cache a
    busy multi-app dashboard would hit the filesystem on every cross-app
    slot event.
    """
    expose = _load_expose_to(target_app)
    return "*" in expose or requesting_app in expose


# ---------------------------------------------------------------------------
# slots initial-push filter (applied when a new WS client connects)
# ---------------------------------------------------------------------------

def filter_subagent_batch_for_app(
    items: list[dict[str, Any]],
    app: str,
    allowed_events: frozenset[str],
    state: DashboardState,
    *,
    msg_type: str = "subagent_batch_update",
) -> list[dict[str, Any]]:
    """Filter one coalesced subagent batch frame down to the permitted items.

    Each item carries its own ``slot`` (``SubagentEventCoalescer`` seeds every
    buffered entry with one), so the frame is filtered per row rather than
    accepted or denied whole. Reuses :func:`_subagent_visible` — the same
    predicate the per-event path uses — so batched and unbatched delivery
    cannot drift apart. An item with no resolvable slot is dropped (fail
    closed), matching the per-event gate's ``slot_missing`` denial.

    The frame ITSELF is already audited once by :func:`ws_event_allowed` (it is
    admitted unconditionally at the gate, per-item filtering happens here
    instead). Each item decision gets its own deduplicated record too — a
    ``_item`` reason suffix keeps it out of the frame-level dedup key — since a
    per-item CWE-269 decision is exactly the kind of permission decision
    ``AUTOSDE.yaml`` requires an SEL event for, hot-path volume notwithstanding:
    the existing dedup window bounds it the same way it bounds every other
    decision here.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slot = state._slots.get(str(item.get("slot", "")))
        if slot is None:
            continue
        if _subagent_visible(slot, app, allowed_events, state):
            out.append(item)
            _audit_allow(app, f"{msg_type}_item")
        else:
            _audit_deny(app, f"{msg_type}_item", "slot_scope_denied")
    return out


def filter_slots_for_app(
    slots: list[dict[str, Any]],
    app: str,
    allowed_events: frozenset[str],
    state: DashboardState,
) -> list[dict[str, Any]]:
    """Filter the slots list sent on initial WS connect for an app token.

    ``slots`` is the raw list from ``[s.to_dict() for s in state._slots.values()]``.
    Returns only the slots the app is allowed to see.

    The ``slots`` frame itself is already audited once by :func:`ws_event_allowed`
    (it is admitted unconditionally at the gate; per-item filtering happens
    here). Each item decision gets its own deduplicated ``slots_item`` record
    too, for the same reason :func:`filter_subagent_batch_for_app` audits its
    items: this is a per-slot CWE-269 decision, not a detail of the frame-level
    one already logged.
    """
    result = []
    for slot_dict in slots:
        slot_key = slot_dict.get("key", "")
        slot = state._slots.get(slot_key)
        if slot is None:
            continue
        if _slot_visible(slot, app, allowed_events, state):
            result.append(_strip_source_link_status(slot_dict))
            _audit_allow(app, "slots_item")
        else:
            _audit_deny(app, "slots_item", "slot_scope_denied")
    return result


# Credential-backed chip fields attached by ``state._project_source_links``.
# The general broadcast carries them for a dashboard user (public repos), but
# an app token must never receive provider status — its scope is slot metadata,
# not the operator's credential-backed view of a repository. Stripped here so
# widening the general list for dashboard users cannot leak into an app frame.
_SOURCE_LINK_STATUS_KEYS = ("ci", "state", "mergeable", "mergeStateStatus")


def _strip_source_link_status(slot_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *slot_dict* with chip status removed from source links.

    Only touches slots that carry source links with status; otherwise returns
    the input unchanged (no needless copy on the hot path).
    """
    links = slot_dict.get("source_links")
    if not links or not any(
        any(k in link for k in _SOURCE_LINK_STATUS_KEYS) for link in links
    ):
        return slot_dict
    cleaned = dict(slot_dict)
    cleaned["source_links"] = [
        {k: v for k, v in link.items() if k not in _SOURCE_LINK_STATUS_KEYS}
        for link in links
    ]
    return cleaned
