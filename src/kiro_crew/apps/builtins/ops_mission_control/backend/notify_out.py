"""The local notification bus as an output channel — the credential-free push.

``app.json`` has declared the ``notification`` event permission since the app's first
commit and the app never produced a single notification: no ``notification_bus``
reference, no ``notifications.channels`` block, no push of any shape. So the ONE
push channel Kiro Crew offers that needs no credential and no inbound URL was inert,
and every operator-facing fact this app computes — an incident waiting on a person,
a source that stopped answering, work released because an agent died — required
either an open dashboard tab or a Slack workspace this app deliberately holds no
token for. A declared permission that produces nothing is exactly the machinery
that looks deliberate while doing nothing.

**Why in-process, and what that obliges us to replicate.** The HTTP producer
endpoint (``POST /api/notifications/push``) is unreachable from here, twice over.
First, it authenticates with an app token whose secret lives at
``~/.kiro/crew/apps/<name>/.app_secret``, and ``register_builtin_apps`` writes that
file only for a manifest declaring ``backend.entryPoint``. This app declares
``backend.routes`` — an in-gateway route module — so no secret exists (verified on
disk: ``dev-fleet``/``file-explorer``/``workflows`` have one, this app does not).
Second, even with a secret, a handler that HTTP-calls its own gateway needs an auth
token and can deadlock the loop under load — the reason ``routes._slot_state`` and
``slack_out.link_thread_to_investigation`` already read through ``DashboardState``
instead.

That makes the decision forced, not preferred: we push in-process, and we therefore
owe the two guards the HTTP handler owns. ``_push`` replicates BOTH — the
manifest-declared-channel check, and the rate limiter — and uses the SAME
``AppRateLimiter`` instance off gateway state rather than a fresh one, so the
in-process path and any future HTTP push share one 30-per-300s budget instead of
two. A local-first app must not gain an unthrottled notification path, and the
cheapest way to guarantee that is to consume the same tokens.

**One push per STATE CHANGE, never per tick.** The dispatch cron runs every 120
seconds. ``SKILL.md``'s noise discipline forbids re-notifying for an unchanged
condition, and a source that has been failing for an hour is an unchanged condition
— so the caller diffs against the previous cycle and pushes only on the edge. The
``group_key`` is the incident id, so if a state does recur the notification feed
collapses it into one stack rather than a column of near-identical rows.

**Nothing is pushed on a claim.** A claim is the heartbeat working correctly, and it
is already visible on the board and in Slack. Notifying every claim would turn this
channel into the heartbeat feed the whole design refuses. Stated here so a later
reader does not "fix" the omission.

**Silent by default.** ``notify_enabled`` absent reads as False, so every existing
install stays quiet until an operator turns this on in Settings.

**Everything outbound runs both redaction passes here** as well as the central one in
``DashboardState._deliver_note``. Titles and bodies carry provider text, and the note
lands in the OS notification centre and in persisted JSONL — a separate egress boundary
from the Slack board, registered in ``security_posture._REDACTION_SINKS``. Both passes,
because core ``security.redact`` alone leaves a provider ``?api_key=`` in a URL intact;
see :func:`_redacted`.

See ``docs/system-specs/modules/ops-mission-control.md`` § Local notifications.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    read_config,
    set_top_level,
)

# Imported at module scope, not deferred into ``_push``. ``routes`` already imports both of
# these at module scope and imports this module, so nothing is saved by deferring — and a
# deferred ``from ... import`` resolves through ``sys.modules`` while a test's
# ``mock.patch("pkg.mod.fn")`` resolves through PACKAGE ATTRIBUTES. Those two are normally
# the same object and are NOT after ``test_ledger_sync_git`` evicts this app's modules to
# simulate two processes: the patch then lands on one copy and the gate reads the other, so
# a mocked "app is enabled" silently did not apply and every push test failed with "0
# notifications" depending only on test order. Binding here makes the gate patchable by
# identity (``mock.patch.object(notify_out, ...)``) and immune to that.
from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
from kiro_crew.notifications.bus import NotificationPayload, NotificationValidationError

logger = logging.getLogger(__name__)

#: Must match ``app.json``'s ``name``: the bus namespaces an app's channels as
#: ``<app>.<channel-id>`` and the limiter buckets by this exact string.
APP_NAME = "ops-mission-control"

_ENABLED_KEY = "notify_enabled"

#: Channel ids, mirroring ``app.json``'s ``notifications.channels``. Duplicated as
#: constants rather than read from the manifest at call time because a typo here must
#: fail loudly in a test, not silently at 3am — ``_push`` refuses an id the manifest
#: does not declare, and ``test_notify_out.py`` pins these three against the manifest.
CHANNEL_WAITING_ON_YOU = "waiting-on-you"
CHANNEL_SOURCE_HEALTH = "source-health"
CHANNEL_INCIDENT_RELEASED = "incident-released"

#: The dashboard page this app owns. Path-only, because ``bus._validate_internal_url``
#: refuses anything else — and deliberately NOT a per-incident deep link: the page
#: selects an incident from React state and reads no query parameter, so ``?id=`` would
#: be the UI promising a jump it cannot make.
_APP_URL = "/ops-mission-control"

#: Seconds a released-incident note lives before the passive sweeper drops it. A
#: release is history the moment the work is picked up again, so it should not sit in
#: the feed for a week. The other two channels are deliberately untl'd: a person is
#: still waiting, and a source is still down, until something changes.
RELEASED_TTL_SECS = 24 * 60 * 60

#: Bus caps are 500/20000; stay well under so we truncate rather than being refused.
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 2000


def configured() -> bool:
    """True when the operator turned this channel on.

    Unlike Slack there is nothing else to configure — no destination, no credential —
    so this is the whole of the operator's half. Whether a bus exists at all is a
    runtime condition, reported by :func:`status`.
    """
    return bool(read_config().get(_ENABLED_KEY))


def set_settings(*, enabled: bool | None = None) -> None:
    """Persist the operator's choice. Non-secret, so plain app config."""
    if enabled is None:
        return
    # `set_top_level` holds `_ConfigLock` across the read-modify-write; the open-coded sequence
    # here did not, so a concurrent settings PUT could silently drop this flag. Found in review.
    set_top_level(_ENABLED_KEY, bool(enabled))


def bus_from_state(state: Any | None) -> Any | None:
    """Pull the live notification bus off gateway state, tolerating its absence.

    Threaded in from the route layer for the same reason ``slack_out.client_from_state``
    is: Kiro Crew has no global state accessor (state is per ``web.Application``), and an
    explicit dependency is what lets every push be tested without a gateway.
    """
    return getattr(state, "notification_bus", None) if state is not None else None


def declared_channels() -> list[dict[str, str]]:
    """The channels this app's INSTALLED manifest declares, for Settings to render.

    Read from the manifest rather than from the bus on purpose. Registration is lazy —
    a channel appears in ``GET /api/notifications/channels`` (and therefore in the
    central Settings → Notifications rail) only after its first push — so a freshly
    installed app would show nothing there until something fired. Returning the
    declaration closes that window: an operator can see which channels exist before any
    of them has ever spoken.

    Empty when the manifest cannot be read (not installed, or a non-gateway process).
    """
    try:
        manifest = get_app_manifest(APP_NAME)
    except Exception:  # noqa: BLE001 — a status probe must never raise
        return []
    if manifest is None:
        return []
    return [
        {
            "id": ch.id,
            "name": ch.name,
            "icon": ch.icon,
            "default_priority": ch.defaultPriority,
        }
        for ch in manifest.notifications.channels
    ]


def status(state: Any | None = None) -> dict[str, Any]:
    """Why this channel is or is not usable — surfaced in Settings.

    Distinguishes the two failure modes because they need different fixes: off (flip
    the toggle) and no bus (this is not the gateway process — a CLI or test run holds
    no ``DashboardState``, so there is nothing to push through and no operator action
    would change that).
    """
    enabled = bool(read_config().get(_ENABLED_KEY))
    bus = bus_from_state(state)
    bus_available = bus is not None
    if not enabled:
        detail = (
            "Off. Turn on to get a desktop notification when an incident needs you, "
            "a source stops answering, or work is released."
        )
    elif not bus_available:
        detail = (
            "The notification bus is not available in this process, so nothing can be "
            "delivered. It lives on the running gateway — if the dashboard is up and "
            "this persists, check the gateway log."
        )
    else:
        detail = "Notifying you on state changes only — never on a quiet heartbeat."
    return {
        "enabled": enabled,
        "bus_available": bus_available,
        "ready": enabled and bus_available,
        "detail": detail,
        "channels": declared_channels(),
    }


def _redacted(text: str) -> str:
    """Run ``text`` through BOTH redaction passes, as ``store._redacted`` does.

    Which passes are needed is not obvious, so it is stated rather than rediscovered:
    core ``security.redact`` catches recognizable vendor credentials and exfiltration
    URLs but leaves a bare-hex Datadog key and a provider ``?api_key=`` in a URL
    untouched, and the app's ``secrets.redact_tokens`` covers exactly those provider
    shapes. Measured, not assumed — a source-health note whose body is
    ``401 from https://api.datadoghq.com?api_key=<hex>`` comes back UNCHANGED from core
    alone, and that string is a real provider error message, not a contrivance.

    ``slack_out`` runs core only, which is why it was not the pattern to copy here.
    ``store.write_log`` and ``registry.gather_evidence`` compose both, and the symmetry
    is deliberate: the places provider text leaves this app must not sanitize to
    different standards.

    Imported inside the function for the same reason those two do it: ``security`` is a
    large module and this one is imported by the route module at gateway start.
    """
    if not text:
        return text
    from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import redact_tokens

    # Through the CPP shim, not the core directly — see the note in `slack_out`.
    from kiro_crew.platform.context import redact_via_context as redact

    return redact_tokens(redact(text))


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _push(
    state: Any | None,
    channel_id: str,
    title: str,
    body: str,
    *,
    group_key: str,
    priority: str | None = None,
    url: str | None = _APP_URL,
    ttl: int | None = None,
) -> bool:
    """The single chokepoint. Returns whether a note was delivered; never raises.

    The order of the gates mirrors ``dashboard/handlers/notifications_push.py``
    deliberately — two paths that check the same things in a different order drift, and
    the drift is invisible until one of them lets something through. So:

    1. ``is_app_enabled`` — deny-by-default, the same call ``routes._require_enabled``
       makes on every request.
    2. The channel must be DECLARED in the installed manifest. This is the guard the
       HTTP handler owns and the in-process path would otherwise skip entirely.
    3. Register lazily, once, exactly as the handler does — re-registering on every
       push would stomp a runtime priority override.
    4. Validate BEFORE the limiter. An invalid payload delivers nothing, so it must not
       drain the budget and then throttle a legitimate note (the handler's reasoning,
       kept verbatim in spirit).
    5. Consume from the limiter ON STATE, not a fresh one, so both paths share one
       30-per-300s budget.

    Every lookup is ``getattr``-guarded: a state with no bus, no limiter, or an
    unreadable manifest is a quiet False, exactly as ``slack_out`` treats a missing
    Slack client. The persist future is deliberately NOT awaited — this is a
    fire-and-forget producer like the legacy system callers, and the future belongs to
    whoever pushed last.
    """
    if not configured():
        return False
    bus = bus_from_state(state)
    if bus is None:
        return False

    try:
        if not is_app_enabled(APP_NAME):
            return False

        declared = {ch["id"]: ch["default_priority"] for ch in declared_channels()}
        if channel_id not in declared:
            # A push at an undeclared id is a bug in THIS module, not an operator
            # problem — say so loudly in the log rather than inventing a channel.
            logger.warning(
                "ops-mission-control: refusing to push to undeclared channel %r "
                "(declared: %s)",
                channel_id,
                sorted(declared),
            )
            return False

        full_channel = f"{APP_NAME}.{channel_id}"
        if not bus.is_registered(full_channel):
            bus.register_channel(full_channel, declared[channel_id])

        payload = NotificationPayload(
            source=f"app:{APP_NAME}",
            channel=full_channel,
            # Redacted HERE as well as centrally in ``_deliver_note``: this text starts
            # in a third-party provider's alarm payload and ends up in the OS
            # notification centre and on disk.
            title=_clip(_redacted(title), _MAX_TITLE_CHARS),
            body=_clip(_redacted(body), _MAX_BODY_CHARS),
            priority=priority,
            group_key=group_key,
            url=url,
            ttl=ttl,
        )
        try:
            payload.validate()
        except NotificationValidationError as exc:
            logger.warning(
                "ops-mission-control: dropped an invalid notification on %s: %s",
                full_channel,
                exc,
            )
            return False

        limiter = getattr(state, "notification_rate_limiter", None)
        if limiter is not None and not limiter.allow(APP_NAME):
            logger.info(
                "ops-mission-control: notification rate limit reached, dropping a %s note",
                channel_id,
            )
            return False

        bus.push(payload)
    except Exception as exc:  # noqa: BLE001 — never fatal to a cycle or a transition
        logger.warning("ops-mission-control: notification push failed: %s", exc)
        return False
    return True


def notify_needs_human(
    state: Any | None, incident_id: str, title: str, blocked_reason: str = ""
) -> bool:
    """An incident just started waiting on a person.

    Critical priority, because it is the one state in this app that blocks an agent
    turn — but it is NOT ``system.approval`` and stays mutable: an app must not be able
    to hand itself an unsilenceable channel.

    ``blocked_reason`` is included when known for the same reason the Slack board shows
    it instead of the bare status: "needs human" does not say whether a click or a
    decision is wanted.
    """
    reason = blocked_reason.replace("_", " ").strip()
    body = f"{title}\n\n{reason}" if reason else title
    return _push(
        state,
        CHANNEL_WAITING_ON_YOU,
        f"{incident_id} is waiting on you",
        body,
        group_key=incident_id,
    )


def notify_source_unhealthy(state: Any | None, source_id: str, detail: str) -> bool:
    """A signal source stopped answering, on the cycle it flipped.

    The ``group_key`` is the source id rather than an incident id: consecutive
    failures of one source are one condition, and stacking them is the honest
    rendering. Body carries the backend's verbatim reason — this module invents no
    diagnosis of a provider it cannot see.
    """
    return _push(
        state,
        CHANNEL_SOURCE_HEALTH,
        f"{source_id} stopped answering",
        (
            f"{detail}\n\nUntil it answers again, a signal missing from the board means "
            "\"we could not look\" — not \"it recovered\"."
        ),
        group_key=f"source:{source_id}",
    )


def notify_incidents_released(state: Any | None, incident_ids: list[str]) -> int:
    """Work released for re-pickup because its investigation went idle.

    Passive and TTL'd: it is worth knowing, and it is not worth interrupting for. One
    note per incident (not one summary) so the ``group_key`` stays the incident id and
    a re-release of the same incident collapses instead of accumulating.
    """
    pushed = 0
    for incident_id in incident_ids:
        if _push(
            state,
            CHANNEL_INCIDENT_RELEASED,
            f"{incident_id} was released for re-pickup",
            (
                "Its investigation went idle, so the claim was dropped and the next "
                "heartbeat may pick it up again. Nothing was resolved."
            ),
            group_key=incident_id,
            ttl=RELEASED_TTL_SECS,
        ):
            pushed += 1
    return pushed
