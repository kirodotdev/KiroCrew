"""The read projections the Ops Mission Control dashboard polls.

``/state`` paints the whole board and ``/handover`` renders the shift digest; ``/incidents``,
``/incident``, ``/signals``, ``/providers`` and ``/rotation`` answer the panels. All of them
are polled, so every call that parses a growing file or can spawn ``gh`` runs through
``asyncio.to_thread``, and the two that report "waiting on you" first reconcile each open
incident against its live investigation slot. None of them writes to a provider.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    companion,
    handover,
    ledger,
    notify_out,
    rotation,
    slack_out,
    slot_watch,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import (
    APP_NAME,
    RegistryLookup,
    _slack_client,
    logger,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATE_FIRING,
    STATE_OK,
    STATE_SUPPRESSED,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import provider_config
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook as webhook_mod
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import describe_secrets


def canonical_slot_key(incident_id: str) -> str:
    """The chat-slot key for an incident. DERIVED, never read back from the record.

    The dispatch cron prompt tells the agent the key is "EXACTLY
    ``ops-mission-control-<incident_id>``, so any other key leaves the user watching an empty
    conversation" — and that sentence was the only thing enforcing it. Same objection this
    change makes about tier arming: prose is not an enforcement mechanism, and a misfollowed
    turn produced an incident whose panel silently showed nothing.

    So the convention is computed here and the stored ``slot_key`` is not consulted. That is
    safe because it is what every consumer already does: the frontend derives the key from
    the incident id (``IncidentChat.incidentSlotKey``) and never reads the field, and the two
    backend call sites already fell back to this exact expression. The field stays on the
    record for forensics — what the agent *reported* using is worth keeping when a panel came
    up empty — but nothing resolves a slot through it.
    """
    return f"{APP_NAME}-{incident_id}"


def _slot_state(request: web.Request, slot_key: str) -> dict[str, Any] | None:
    """Read an investigation slot's live state IN PROCESS.

    Read through the gateway's own ``DashboardState`` rather than by calling our
    own HTTP API: a handler that HTTP-calls its own server has to carry an auth
    token and can deadlock the loop under load. Returns ``None`` when the slot
    does not exist (yet) — which ``slot_watch.derive_status`` treats as "no
    evidence", never as blocked or as done.
    """
    if not slot_key:
        return None
    state = request.app.get("state")
    getter = getattr(state, "get_slot", None)
    if getter is None:
        return None
    try:
        slot = getter(slot_key)
    except Exception:  # noqa: BLE001 — a state read must never 500 the board
        logger.exception("ops-mission-control: slot lookup failed for %r", slot_key)
        return None
    if slot is None:
        return None

    # `to_dict()` is the slot's PUBLIC serializer and it already derives `pending_approval`
    # from the approval futures (state.py). Reading `slot._approval_futures` directly is
    # wrong: a private attribute is not a contract, so a core refactor that renamed it
    # would silently turn "waiting on you" into "progressing" on this board, with nothing
    # failing anywhere. Asking the owner is the fix.
    #
    # Falls back to the public attribute if `to_dict` is absent or raises: this read paints
    # the whole board, so it degrades rather than 500s. The fallback is deliberately NOT a
    # private reach-in — a narrower truth beats a fragile one.
    pending = bool(getattr(slot, "pending_approval", False))
    to_dict = getattr(slot, "to_dict", None)
    if callable(to_dict):
        try:
            pending = bool(to_dict().get("pending_approval", pending))
        except Exception:  # noqa: BLE001 — a slot serializer fault must not blank the board
            logger.exception("ops-mission-control: slot to_dict() failed for %r", slot_key)

    return {
        "running": bool(getattr(slot, "running", False)),
        "pending_approval": pending,
        "waiting_for_input": bool(getattr(slot, "waiting_for_input", False)),
        "messages": [
            {"role": getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)}
            for m in (getattr(slot, "messages", None) or [])
        ],
    }


def _ledger_sync_status() -> dict[str, Any]:
    """Shared-ledger sync status, tolerating any failure.

    Deferred import for the same reason the hygiene handler defers it: ``ledger_sync``
    pulls in the git/sandbox machinery. Never raises — ``/state`` paints the whole
    board, so a probe of an optional feature must not be able to blank it.

    The failure fallback carries the SAME key set as ``ledger_sync.status()``, not a
    two-key subset. One shape means the UI can type every field as required and read it
    straight; the narrower fallback meant a panel had to guard each field individually,
    and the failure mode of forgetting one is rendering ``undefined`` as a remote URL —
    which reads as "your team repo is called undefined" rather than as "we could not tell".
    """
    try:
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        return ledger_sync.status()
    except Exception:  # noqa: BLE001 — an optional feature must not 500 the board
        logger.exception("ops-mission-control: ledger sync status failed")
        return {
            "enabled": False,
            "remote": "",
            "branch": "",
            # The branch pair. ``branch_matches`` is True in the fallback because it gates a
            # WARNING: we could not read the repo at all, so claiming a branch mismatch we
            # did not observe would be the overstated claim in the other direction.
            "local_branch": "",
            "branch_matches": True,
            "detached": False,
            "initialized": False,
            "ready": False,
            "conflict": False,
            "schedule_conflict": False,
            "detail": "Sync status unavailable.",
        }


async def _handle_state(
    request: web.Request, *, get_registry: RegistryLookup
) -> web.StreamResponse:
    """Everything the board needs in one call: incidents, sources, rotation, ledger.

    Reconciles each open incident against its investigation slot first, so an
    agent parked on a tool approval shows as ``needs_human`` rather than as
    still-progressing ``dispatched``. Done on read because that is the moment the
    answer is looked at — a stored flag would go stale the instant the operator
    approves from the embedded chat.
    """
    registry = get_registry()
    shift = await registry.resolve_shift()

    # ONE off-loop read for the reconcile pass. This was `store.open_incidents()` inline,
    # and then called AGAIN inline below — two full parses of the incident index on the
    # event loop, per poll.
    for inc in await asyncio.to_thread(store.open_incidents):
        slot_key = canonical_slot_key(inc.incident_id)
        await asyncio.to_thread(
            slot_watch.reconcile, inc.incident_id, _slot_state(request, slot_key)
        )

    # Re-read AFTER the reconcile pass, which mutates statuses — so this genuinely is a
    # second read rather than a redundant one. Off-loop like the first.
    open_incidents = await asyncio.to_thread(store.open_incidents)
    # `describe()` off the loop for the same reason the authorization gate is: it calls
    # `is_primary()` -> `_schedule_me()` -> `schedule_file.resolve_login()`, which spawns
    # `gh api user` synchronously (10s timeout) on a cold login cache.
    #
    # An earlier revision of this handler reasoned that the awaited `resolve_shift()` above
    # always warms that cache first, so an inline call was safe. That was WRONG, and review
    # caught it: `resolve_shift` wraps each source in `asyncio.wait_for(...,
    # DEFAULT_POLL_TIMEOUT_SECS)`, and a timeout cancels the awaiting coroutine while the
    # `to_thread` worker keeps running — so the poll can give up with `_login_cache` still
    # unset and `describe()` then pays the full spawn, inline, on the loop. "Something
    # upstream probably warmed it" is not a guarantee; `to_thread` is.
    rotation_view = await asyncio.to_thread(rotation.describe, shift)
    companions = await asyncio.to_thread(companion.companion_summary)
    # `stats()` parses the WHOLE ledger JSONL, so it scales with the team's accumulated
    # knowledge on a POLLED endpoint. Measured: 0.1ms empty, 1.8ms at 100 entries, 13ms at
    # 1k, 93ms at 5k, 275ms at 20k.
    #
    # A previous round measured this at 0.03ms and left it inline as "negligible" beside the
    # companion scan. That measurement was taken against an EMPTY ledger and the conclusion
    # generalised from it — the one case where the cost is zero by construction. Review
    # caught it. The lesson is in the numbers above: for anything that parses an accumulating
    # file, the empty case is not the case worth measuring.
    #
    # `store.counts_by_status` parses the same index and is worse: 4ms at 100 incidents,
    # 42ms at 1k, 188ms at 5k — and this app's own spec notes a flapping alarm can mint
    # hundreds. Both go off-loop, and concurrently, since neither depends on the other.
    ledger_stats, counts = await asyncio.gather(
        asyncio.to_thread(ledger.stats),
        asyncio.to_thread(store.counts_by_status),
    )
    return web.json_response(
        {
            "incidents": [inc.to_dict() for inc in open_incidents],
            "counts": counts,
            "blocked": slot_watch.blocked_summary(open_incidents),
            "providers": [_provider_dict(p) for p in registry.catalog()],
            "rotation": rotation_view,
            "ledger": ledger_stats,
            # Shared-ledger git sync. ``ledger_sync.status()`` was written to be
            # "surfaced in Settings" — and then never returned by any route, so the
            # team memory-exchange repo was invisible as well as unsettable.
            #
            # Off the loop because the probe now reads three files (config, ledger.jsonl
            # and rotation.yaml, the last two to detect conflict markers) and ``/state``
            # is the dashboard's hot poll. ``_ledger_sync_status`` stays synchronous so
            # the tests that call it directly do not have to care.
            "ledger_sync": await asyncio.to_thread(_ledger_sync_status),
            "slack": slack_out.status(_slack_client(request)),
            # Local desktop notifications. Rides on ``/state`` for the same reason
            # Slack's status does: readiness depends on live gateway state (is there a
            # notification bus in this process), not on config alone — so it cannot be
            # answered from the unauthenticated config file the panel already has.
            #
            # Off the loop, unlike Slack's status: this one PARSES the installed
            # manifest (to report the declared channels) on top of the config read, and
            # `/state` is polled continuously by an open dashboard. Same treatment
            # `_ledger_sync_status` already gets, and for the same reason.
            "notify": await asyncio.to_thread(notify_out.status, request.app.get("state")),
            # What companion packages are INSTALLED. Reported separately from the
            # provider list because "no companion installed" and "companion
            # installed but rejected by admission" look identical in the provider
            # list and need completely different fixes.
            #
            # Off-loop: this walks `importlib.metadata.entry_points()`, which enumerates
            # every installed distribution's metadata from disk. `/state` is a POLLED
            # endpoint, so on a fat site-packages (or a cold page cache) that scan pauses
            # the chat turn and the liveness heartbeat on every poll. Found in review.
            "companions": companions,
            "webhook_queue": webhook_mod.queue_depth(),
        }
    )


async def _handle_handover(
    request: web.Request, *, get_registry: RegistryLookup
) -> web.StreamResponse:
    """Shift handover digest — a read-only projection, computed fresh.

    Reconciles open incidents against their live slots first, exactly as ``/state``
    does: the digest's most important section is "waiting on you", and that is derived
    from ``blocked_reason``, which is only true if it has just been reconciled.
    Returns both the structured digest and a rendered text form, so an agent can paste
    it into a handover thread without re-deriving the wording.
    """
    registry = get_registry()
    shift = await registry.resolve_shift()

    # Off-loop: a full parse of the incident index, on a request path. Same reason as
    # `/state` — measured 4ms at 100 incidents, 188ms at 5k.
    for inc in await asyncio.to_thread(store.open_incidents):
        slot_key = canonical_slot_key(inc.incident_id)
        await asyncio.to_thread(
            slot_watch.reconcile, inc.incident_id, _slot_state(request, slot_key)
        )

    providers = [_provider_dict(p) for p in registry.catalog()]
    # `describe()` was evaluated INLINE here as an argument — the `to_thread` moved
    # `handover.build` off the loop but the argument is computed before the call, so the
    # `gh` spawn inside `describe()` still ran on it. Both off-loop now.
    rotation_view = await asyncio.to_thread(rotation.describe, shift)
    digest = await asyncio.to_thread(handover.build, providers, rotation_view)
    return web.json_response({**digest, "text": handover.render_text(digest)})


#: Incidents returned by ``/incidents`` in one response. The board shows recent work; a
#: responder scrolling to incident 900 is not a workflow this app has. Bounded because
#: serializing the ENTIRE index is fine at 3 incidents and a growing payload on every
#: dashboard poll once a flapping alarm has minted hundreds.
MAX_INCIDENTS_RESPONSE = 200


async def _handle_incidents(request: web.Request) -> web.StreamResponse:
    status_filter = request.query.get("status", "").strip()
    # ``id`` narrows to one incident. It exists for the agent surface: the
    # single-incident ``GET /incident`` route cannot be admitted to
    # internal-secret callers without prefix-admitting the human-only
    # ``/incident/proposal/decide`` (see ``_MIXED_INTERNAL_API_PATHS`` in
    # dashboard/server.py), so SOP-driven agents read one incident here.
    id_filter = request.query.get("id", "").strip()
    # Off-loop: full index parse on a polled endpoint.
    index = await asyncio.to_thread(store.read_index)
    matching = [
        inc
        for inc in sorted(index.values(), key=lambda i: i.claimed_at, reverse=True)
        if (not status_filter or inc.status == status_filter)
        and (not id_filter or inc.incident_id == id_filter)
    ]
    items = [inc.to_dict() for inc in matching[:MAX_INCIDENTS_RESPONSE]]
    payload: dict[str, Any] = {"incidents": items}
    if len(matching) > len(items):
        # Say so rather than silently truncating: a board that shows 200 of 640 while
        # claiming to be the whole picture is how someone concludes an incident vanished.
        payload["truncated"] = True
        payload["total"] = len(matching)
    return web.json_response(payload)


async def _handle_incident(request: web.Request) -> web.StreamResponse:
    """One incident plus its rendered postmortem.

    ``log`` is the Markdown artifact ``store.write_log`` writes when the incident closes,
    and ``log_path`` is where that file lives — reported so an operator can hand a
    colleague the FILE rather than only a clipboard, without the UI guessing a path that
    ``KIROCREW_HOME`` can move.

    ``log_path`` is empty unless the file is really there. A path is a promise that
    something is at the other end of it, and naming one for an open incident (or for
    anything closed before the writer was wired up) would be the app asserting an artifact
    it does not have. There is deliberately no download route: a second, non-JSON egress
    boundary would need its own redaction and its own posture registration, and the JSON
    field already makes the artifact readable.
    """
    incident_id = request.query.get("id", "").strip()
    incident = await asyncio.to_thread(store.get_incident, incident_id) if incident_id else None
    if incident is None:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    try:
        log_file = store.incident_log_path(incident_id)
        log_path = str(log_file) if log_file.is_file() else ""
    except (OSError, ValueError):
        # ``incident_log_path`` validates the id even though we generated it. A
        # hand-edited index.json is the only way here, and it must not 500 the route.
        log_path = ""
    return web.json_response(
        {
            "incident": incident.to_dict(),
            "log": await asyncio.to_thread(store.read_log, incident_id),
            "log_path": log_path,
        }
    )


async def _handle_signals(
    request: web.Request, *, get_registry: RegistryLookup
) -> web.StreamResponse:
    """Current provider state: what is firing, what is unclaimed, and what we could not read.

    ``firing`` is the list a caller should reason about, and it is filtered the same way
    ``dispatch.run_cycle`` filters. Returning every signal regardless of state would put
    an already-cleared one in the very list the reconcile SOP reads as "what is still
    firing", and in ``unclaimed`` as apparent work — the two must agree on what firing
    means.

    ``poll_health`` is the other half of that contract: absence from ``firing`` only
    means "it cleared" for a source whose poll actually SUCCEEDED. Resolving an incident
    because its signal is missing from a source that returned 429 closes live work with
    a false resolution.

    ``suppressed`` is the THIRD reason a signal can be absent from ``firing``, and it is
    neither of the first two: a human parked it at the provider. So it must not be
    resolved on absence (nothing was fixed) and must not be treated as ``cleared``
    either (the provider is not reporting recovery, it is reporting that somebody asked
    to stop hearing about it). It exists as its own bucket because that is the only way a
    caller can say "parked" at all — ``signals`` alone would put it back in the raw list
    where reconcile and the source table would both count it as live work.
    """
    registry = get_registry()
    signals, errors = await registry.poll_all()
    # Off-loop: full index parse on a polled endpoint.
    claimed = {inc.signal.id for inc in (await asyncio.to_thread(store.read_index)).values()}
    firing = [s for s in signals if s.state == STATE_FIRING]
    cleared = [s for s in signals if s.state == STATE_OK]
    suppressed = [s for s in signals if s.state == STATE_SUPPRESSED]
    health = registry.poll_health()
    return web.json_response(
        {
            # Kept for compatibility: every signal the poll returned, any state.
            "signals": [s.to_dict() for s in signals],
            "firing": [s.to_dict() for s in firing],
            # Signals a provider positively reports as recovered. A caller may resolve
            # on these WITHOUT consulting poll_health — an explicit `ok` is evidence,
            # unlike an absence.
            "cleared": [s.to_dict() for s in cleared],
            # Parked by a human at the provider. Carries `suppressed_by` /
            # `suppressed_reason` when the provider published attribution, which is what
            # separates "the app ignored my alarm" from "someone silenced it".
            "suppressed": [s.to_dict() for s in suppressed],
            "unclaimed": [s.to_dict() for s in firing if s.id not in claimed],
            "errors": errors,
            "poll_health": health,
            # The one boolean a caller needs before resolving anything on absence.
            "all_sources_healthy": bool(health) and all(h.get("ok") for h in health.values()),
        }
    )


def _provider_dict(info: Any) -> dict[str, Any]:
    return {
        "id": info.id,
        "display_name": info.display_name,
        "roles": list(info.roles),
        "configured": info.configured,
        "config_fields": list(info.config_fields),
        "secret_fields": list(info.secret_fields),
        "detail": info.detail,
        # Non-secret config is safe to echo; secrets report set/unset only.
        "config": provider_config(info.id),
        "secrets": describe_secrets(info.id, tuple(info.secret_fields)),
    }


async def _handle_providers(
    request: web.Request, *, get_registry: RegistryLookup
) -> web.StreamResponse:
    return web.json_response({"providers": [_provider_dict(p) for p in get_registry().catalog()]})


async def _handle_rotation(
    request: web.Request, *, get_registry: RegistryLookup
) -> web.StreamResponse:
    shift = await get_registry().resolve_shift()
    # Off-loop: `describe()` -> `is_primary()` -> `resolve_login()` can spawn `gh api user`.
    return web.json_response(await asyncio.to_thread(rotation.describe, shift))
