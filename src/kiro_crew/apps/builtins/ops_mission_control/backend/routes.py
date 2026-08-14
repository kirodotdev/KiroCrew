"""Ops Mission Control — backend routes.

Builtin-app contract: ``register_routes(app: web.Application) -> None`` registering
FULL paths directly on the gateway router (confirmed against the call site in
``dashboard/server.py``: ``_mod.register_routes(app)``, single argument). This is
NOT the external-app ``AppRoute``-list contract — mixing them up produces routes
that silently never dispatch.

Every handler is wrapped in ``_require_enabled``: builtin routes exist from gateway
startup even while the app is disabled, so a default-disabled opt-in app would
otherwise stay callable.

Secrets are **write-only** over this surface. ``PUT /providers/<id>/secret`` accepts
a token; nothing ever returns one. The read endpoints report only whether a field
is set.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    companion,
    dispatch,
    handover,
    ledger,
    notify_out,
    policy_store,
    rotation,
    slack_out,
    slot_watch,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    CLAIMED_BY_OPERATOR,
    DEFAULT_VERIFY_AFTER_SECS,
    EXPIRING_ACTIONS,
    MODE_ORDER,
    STATE_FIRING,
    STATE_OK,
    STATE_SUPPRESSED,
    STATUS_NEEDS_HUMAN,
    VALID_ACTIONS,
    VERIFIABLE_ACTIONS,
    VERIFY_NOT_CHECKABLE,
    VERIFY_PENDING,
    LedgerEntry,
    Signal,
    resolve_silence_secs,
    utc_now_iso,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
    merge_provider_config,
    provider_config,
    set_top_level,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook as webhook_mod
from kiro_crew.apps.builtins.ops_mission_control.backend.registry import get_registry
from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
    delete_secret,
    describe_secrets,
    put_secret,
    redact_tokens,
)
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

APP_NAME = store.APP_NAME
_BASE = f"/api/apps/{APP_NAME}"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: Cap on a secret value. Real provider tokens are well under this; a larger body
#: is a misuse (or an attempt to bloat the keystone file) and is refused.
_MAX_SECRET_LEN = 512

#: Cap on an operator-supplied note attached to an action.
_MAX_NOTE_LEN = 4000

#: Cap on the shared-ledger git remote URL. An ssh/https remote is short; a longer
#: value is a paste accident, not a repo.
_MAX_REMOTE_LEN = 512
#: Cap on an opaque provider-side identifier (a PagerDuty user id). Generous, because the
#: vendor owns the format; this only refuses something that is obviously not an id.
_MAX_PROVIDER_ID_LEN = 128

#: Branch names we will hand to ``git``. Deliberately narrow: letters, digits, and
#: ``._/-``, not starting with ``-`` (which would read as an option). The value is
#: already passed as its own argv entry, never interpolated into a shell string, so
#: this is about failing clearly rather than about injection.
_SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,98}")


class _NotABool(ValueError):
    """A field that must be a JSON boolean was not one."""


def _require_bool(body: dict[str, Any], field: str, *, default: bool | None = None) -> bool | None:
    """A JSON boolean, or raise. NEVER ``bool(value)``.

    `bool()` on a string is true for ANY non-empty text, so every spelling of "no" a client
    might send — `"false"`, `"False"`, `"no"`, `"0"` — coerced to True. On `/incident/proposal/
    decide` that inverted the operator's answer: a request saying "reject this" reached
    `decide_proposal(approve=True)` and executed the authorized production action instead of
    refusing it. The same coercion sat on `schedule_strict_gating` (setting it "false" would
    have READ as enabling strict gating, which is at least the safe direction, but by luck
    rather than design) and on `primary_instance`, which decides who may prune the shared
    ledger. Found in review.

    Refusing is the only correct behavior: there is no safe guess about which way an operator
    meant an ambiguous answer to a question about executing a production write. Accepts the
    JSON booleans and nothing else — not `1`/`0`, because a caller sending those is a caller
    whose serializer this endpoint should not be quietly accommodating.
    """
    if field not in body:
        return default
    value = body[field]
    if isinstance(value, bool):
        return value
    raise _NotABool(field)


#: GitHub's own login shape: alphanumerics and single hyphens, 1-39 chars. This value is
#: compared against names in the shared `rotation.yaml` to decide whether this instance is on
#: shift and whether it is the ledger leader, so a shape guard here keeps a junk value from
#: silently never matching (which would read as "always off shift") rather than being refused.
_SAFE_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


def _require_enabled(handler: Handler) -> Handler:
    """Deny every request while the app is disabled (deny-by-default).

    ``is_app_enabled`` is a synchronous ``installed.json`` read, so it runs off the
    event loop — same treatment as the other builtin apps' gates.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a 400, not a 500
        return None
    return body if isinstance(body, dict) else None


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    sel().log_api_access(
        caller=f"core:{APP_NAME}",
        operation=op,
        outcome=outcome,
        resources=target,
        error=error,
    )


# ---------------------------------------------------------------------------
# Board / state
# ---------------------------------------------------------------------------

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
    # from the approval futures (state.py). This used to read `slot._approval_futures`
    # directly — review flagged it, correctly: a private attribute is not a contract, so a
    # core refactor that renamed it would silently turn "waiting on you" into "progressing"
    # on this board, with nothing failing anywhere. Asking the owner is the fix.
    #
    # Falls back to the public attribute if `to_dict` is absent or raises: this read paints
    # the whole board, so it degrades rather than 500s. The fallback is deliberately NOT the
    # old private reach-in — a narrower truth beats a fragile one.
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


async def _handle_state(request: web.Request) -> web.StreamResponse:
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


async def _handle_handover(request: web.Request) -> web.StreamResponse:
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
#: the endpoint used to serialize the ENTIRE index — fine at 3 incidents, a growing
#: payload on every dashboard poll once a flapping alarm has minted hundreds.
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


def _slack_client(request: web.Request) -> Any | None:
    """The gateway's live Slack client, or None when Slack is not configured.

    Passed explicitly into slack_out rather than fetched from a global: Kiro Crew
    has no global state accessor, and an explicit dependency is testable.
    """
    return slack_out.client_from_state(request.app.get("state"))


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


async def _handle_transition(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    incident_id = str(body.get("id", "")).strip()
    new_status = str(body.get("status", "")).strip()
    if not incident_id or not new_status:
        return web.json_response(
            {"error": "id and status are required", "code": "missing_required_field"}, status=400
        )

    updates: dict[str, Any] = {}
    for field_name in ("diagnosis", "resolution", "slot_key", "slack_thread_ts"):
        if field_name in body:
            value = str(body[field_name])
            # `diagnosis` and `resolution` are AGENT-AUTHORED free text that this app then
            # persists and renders — on the board, in the handover digest, and in the Slack
            # mirror. An investigating agent that pasted a provider token into its writeup
            # stored that token in the incident index and painted it on the dashboard. Same
            # shape as the action-note, Slack and ledger sinks, which were already covered
            # while this one was not. Found in review.
            #
            # `slot_key`/`slack_thread_ts` are machine ids, shape-checked downstream, and are
            # deliberately NOT run through the redactor: it would corrupt an id that happened
            # to match a token pattern.
            if field_name in ("diagnosis", "resolution"):
                value = _safe_outbound(value)
            updates[field_name] = value
    # Captured BEFORE the write, because the desktop notification below must fire on the
    # EDGE into ``needs_human`` and not on every later write while it sits there.
    # ``update_fields`` re-enters ``transition`` with the SAME status on an unrelated
    # field edit, so without this an incident parked on an approval would re-toast on
    # each one.
    previous = await asyncio.to_thread(store.get_incident, incident_id)
    previous_status = previous.status if previous is not None else ""
    try:
        incident = await asyncio.to_thread(store.transition, incident_id, new_status, **updates)
    except KeyError:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    except ValueError as exc:
        # An illegal transition is a client error, not a server fault.
        return web.json_response({"error": str(exc), "code": "illegal_transition"}, status=409)
    _audit("incident_transition", f"{incident_id}->{new_status}", "success")

    # Refresh the Slack pin board so its line tracks the new state, and put any
    # new diagnosis/resolution in the thread. Both are no-ops when Slack output is
    # off, and neither can fail the transition — the state change is already
    # durable at this point.
    client = _slack_client(request)
    await slack_out.publish(incident, client)
    detail = updates.get("resolution") or updates.get("diagnosis") or ""
    if detail:
        await slack_out.post_detail(incident, detail, client)

    # Make the board thread answerable. Done HERE rather than at claim time because the
    # investigation slot does not exist yet when the incident is claimed — the dispatch
    # SOP creates it immediately afterwards and reports the key on its first transition.
    # Re-linking an already-linked thread is idempotent.
    refreshed = await asyncio.to_thread(store.get_incident, incident_id)
    if refreshed is not None:
        incident = refreshed
    # ON THE LOOP, deliberately — NOT `asyncio.to_thread`. Both of these reach
    # loop-owned objects in `DashboardState`: the link path mutates the slot dicts and
    # the reverse Slack index, and the notify path ends in `_deliver_note` ->
    # `_broadcast`, which does `Queue.put_nowait` on every SSE client's queue and
    # `asyncio.Event.set()`. Those primitives are not thread-safe: `Event.set` resolves
    # waiter futures through `loop.call_soon`, which CPython documents as callable only
    # from the loop's own thread (`call_soon_threadsafe` is the cross-thread door). Off
    # the loop it happens to work — the waiter future is marked done synchronously and
    # the loop notices on its next poll — which is exactly what makes it the wrong kind
    # of correct: a latent race that passes every test.
    #
    # Running them here is also strictly BETTER for the blocking I/O, which is why
    # `to_thread` is not needed to protect the loop: `_deliver_note` already offloads its
    # own disk append via `run_in_executor`, but only when it can see a running loop.
    # Called from a worker thread it took the `RuntimeError` fallback and wrote to disk
    # INLINE in that thread — so the thread hop bought no I/O isolation while costing
    # thread-safety. Found in review (GPT 5.6); the review proposed deleting both calls,
    # which would have removed the replyable-thread link and the needs-human alert —
    # features, not incidental work — so they are marshalled instead.
    thread_linked = slack_out.link_thread_to_investigation(incident, request.app.get("state"))

    # The one state change worth interrupting for: an incident now waiting on a person.
    # Only on the EDGE — a transition that leaves the status where it already was is the
    # unchanged condition the noise rule forbids re-notifying for. After Slack and after
    # the write, so it can cost neither.
    if new_status == STATUS_NEEDS_HUMAN and previous_status != STATUS_NEEDS_HUMAN:
        notify_out.notify_needs_human(
            request.app.get("state"),
            incident.incident_id,
            incident.signal.title,
            incident.blocked_reason,
        )

    return web.json_response(
        {
            "incident": incident.to_dict(),
            # Reported so a caller can tell whether a reply into the Slack thread will
            # actually reach the investigation, instead of assuming it will.
            "slack_thread_replyable": thread_linked,
        }
    )


async def _handle_claim(request: web.Request) -> web.StreamResponse:
    """Manually claim a signal the operator picked off the board."""
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    raw_signal = body.get("signal")
    if not isinstance(raw_signal, dict):
        return web.json_response(
            {"error": "signal object is required", "code": "missing_required_field"}, status=400
        )
    claimed_id = str((raw_signal or {}).get("id", "")).strip()
    if not claimed_id:
        return web.json_response(
            {"error": "signal must carry an id", "code": "missing_required_field"}, status=400
        )

    # RESOLVE THE SIGNAL SERVER-SIDE, by id, from a fresh poll — do NOT authorize against the
    # caller-supplied object.
    #
    # `resolve_mode` matches act-rules on `source`, `resource` and `labels`. A caller who
    # controls the whole Signal can pair a resource the operator's rule authorizes
    # (`resource="prod-db-1"`, matching `resource_glob="prod-*"`) with a DIFFERENT provider's
    # target in `labels` (`dd_monitor_id=<someone else's monitor>`) — the resource satisfies
    # the gate while a different field drives the downstream sink. The authorization then
    # describes a signal that does not exist. Found in review.
    #
    # The provider is the authority on what is firing, so we ask it: poll, and take the
    # server's OWN copy of the signal with this id. The claim is refused if the id is not
    # currently firing. The board already sends a signal it got from `/signals`, so this
    # rejects only a fabricated or stale one — exactly the case that must not authorize a
    # write.
    # `poll_all` returns EVERY state — firing, ok and suppressed — so the state filter is
    # required and was missing. The local was even named `firing`, which is what hid it: a
    # signal that recovered between the board's poll and this one came back as `ok`, matched
    # on id alone, and minted an incident for a fault that had already cleared. The two other
    # `poll_all` consumers (`dispatch.run_cycle`, `GET /signals`) both filter explicitly.
    # Found in review.
    #
    # `suppressed` is excluded by the same predicate and must be: somebody parked that signal
    # at the provider, so claiming it is precisely what they asked not to happen.
    signals, _errors = await get_registry().poll_all()
    signal = next(
        (s for s in signals if s.id == claimed_id and s.state == STATE_FIRING), None
    )
    if signal is None:
        return web.json_response(
            {
                "error": (
                    "no firing signal with that id — a manual claim authorizes against the "
                    "provider's current signal, not a caller-supplied one"
                ),
                "code": "signal_not_firing",
            },
            status=409,
        )

    mode = rotation.resolve_mode(signal)
    # `operator`, not the heartbeat default: this route IS the board's manual claim,
    # and telling the two apart afterwards is the whole point of the field.
    incident = await asyncio.to_thread(
        store.claim, signal, operating_mode=mode, claimed_by=CLAIMED_BY_OPERATOR
    )
    if incident is None:
        return web.json_response(
            {"error": "signal is already claimed", "code": "signal_already_claimed"}, status=409
        )

    # Acknowledge the push spool here too. `dispatch.run_cycle` acks what IT claims, and this
    # route is the second place a claim becomes durable — so without this a hand-claimed
    # webhook signal stayed spooled forever, and on a full (200-entry) spool the next signed
    # delivery evicted the OLDEST unclaimed entry to make room for it: a real alert lost to a
    # duplicate nobody needed. A direct consequence of moving consumption off `poll()`;
    # `drain()` used to cover this path by accident. Found in review.
    #
    # Cheap and unconditional: `ack` on an id that is not spooled removes nothing, so no
    # source check is needed and a future push provider gets the same treatment for free.
    await asyncio.to_thread(webhook_mod.ack, {signal.id})

    # Attach what the ledger already knows, exactly as the heartbeat does — a
    # manual claim from the board must not start colder than an automatic one.
    claimed = await asyncio.to_thread(dispatch.attach_ledger_matches, incident)
    # Broker the provider evidence too, for the same reason: the agent that picks this
    # up has no AWS credentials, so the gateway is the only thing that can read the
    # alarm history and logs it needs to diagnose. Non-fatal.
    claimed.evidence = await dispatch.gather_evidence_safely(get_registry(), signal)
    # Onto the pin board, exactly as the heartbeat does — a hand-claimed incident
    # must not be invisible to the channel watching the board.
    await slack_out.publish(claimed.incident, _slack_client(request))
    _audit("incident_claim", incident.incident_id, "success")
    return web.json_response({**claimed.to_dict(), "brief": dispatch.investigation_brief(claimed)})


async def _handle_dispatch(request: web.Request) -> web.StreamResponse:
    """Run one dispatch cycle: poll, claim, match the ledger, release stale work.

    This is what the dispatch cron calls. It returns ``changed: false`` when
    nothing happened, which is the cron's signal to stay completely silent.

    **Deliberately NOT shift-gated**, unlike ``authorize_action``. Audited after the
    off-shift write hole, since this has the same shape — a route reachable independently
    of the tier that pauses its cron. The difference is what it does: claiming a signal and
    reading evidence changes nothing in the operator's tooling, whereas
    ``rotation.authorize_action`` guards an actual provider write.

    Its two callers are the dispatch cron (paused off shift by the tier gate, so the
    automated path IS gated) and the dashboard's "Check now" button — a deliberate human
    action. Blocking the button off shift would stop an operator from proving a
    freshly-configured provider works, which is the one thing they most need right after
    setup; and claiming is idempotent across the team because ``store.claim`` is a
    compare-and-set, so a second instance finds nothing left to claim rather than
    duplicating work.

    The residual exposure is a duplicate *investigation session* if two instances both
    dispatch by hand at once. That is a wasted turn, not a production change — the same
    trade the claim design already accepts (see ``store.claim``).
    """
    result = await dispatch.run_cycle(
        slack_client=_slack_client(request),
        # Threaded in for the local notification bus, which lives on gateway state.
        # Same explicit-dependency rule as the Slack client: no global accessor.
        state=request.app.get("state"),
    )
    payload = result.to_dict()
    # Give the caller a ready-to-use brief per claim so the investigating agent
    # does not spend its first turn re-fetching context Python already has.
    payload["briefs"] = {
        c.incident.incident_id: dispatch.investigation_brief(c) for c in result.claimed
    }
    return web.json_response(payload)


async def _handle_action(request: web.Request) -> web.StreamResponse:
    """Execute (or refuse) a provider action for an incident.

    The autonomy gate runs BEFORE the sink is touched: a sink does not police its
    own authority. A refusal returns 403 with the reason, which is what the UI
    renders as "needs a rule to do this".
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    incident_id = str(body.get("id", "")).strip()
    action = str(body.get("action", "")).strip()
    sink_id = str(body.get("sink", "")).strip()
    # Redact BEFORE the clip: truncating first could sever a token so the pattern no
    # longer matches, and clipping after masking only ever shortens a placeholder.
    note = _safe_outbound(str(body.get("note", "")))[:_MAX_NOTE_LEN]

    if action not in VALID_ACTIONS:
        return web.json_response(
            {"error": f"action must be one of {sorted(VALID_ACTIONS)}", "code": "invalid_action"},
            status=400,
        )
    incident = await asyncio.to_thread(store.get_incident, incident_id) if incident_id else None
    if incident is None:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )

    # `_authorize` is the gate AND the only minter of the permit `_execute_authorized`
    # demands, so the write below cannot happen without this line having allowed it. It also
    # runs the gate off the event loop — see its docstring for why that matters.
    permit, reason = await _authorize(incident.signal, action)
    if permit is None:
        return web.json_response(
            {"error": reason, "code": "not_authorized", "authorized": False}, status=403
        )

    registry = get_registry()
    # A caller-supplied sink must name THIS incident's provider, or nothing.
    #
    # `authorize_action` gates on `incident.signal`, and `AutonomyRule.matches` keys on
    # `signal.source` — so a rule only ever grants authority over the provider that
    # RAISED the signal. `sink_id` used to be honoured verbatim, which let a grant on one
    # provider execute against another: a webhook signal carrying `dd_monitor_id`, a
    # webhook-scoped act-rule, and `sink="datadog"` passed the webhook check and then
    # silenced an unrelated Datadog monitor. The gate was correct; the code just did not
    # act on the thing the gate had approved.
    #
    # Rejected rather than silently ignored. A caller that names the wrong sink has a
    # wrong model of what it is authorized to do, and quietly redirecting the write to the
    # right provider would confirm the wrong model while still performing a mutation.
    if sink_id and sink_id != incident.signal.source:
        return web.json_response(
            {
                "error": (
                    f"sink {sink_id!r} does not own this incident's signal "
                    f"({incident.signal.source!r}); authority is per provider"
                ),
                "code": "sink_not_owner",
                "authorized": False,
            },
            status=403,
        )
    # Default to the sink that owns this signal's provider, falling back to
    # observe-only so a proposal always has somewhere to land.
    sink = registry.action_sink(incident.signal.source) or registry.action_sink("noop")
    if sink is None:
        return web.json_response(
            {"error": "no action sink available", "code": "no_action_sink"}, status=503
        )

    refusal = _sink_refuses(sink, action)
    if refusal:
        # 422, not 403: authority is fine — the target simply cannot do this. A 403 would
        # send an operator to their autonomy rules, which are not the problem.
        return web.json_response({"error": refusal, "code": "action_unsupported"}, status=422)

    payload: dict[str, Any] = {"note": note}
    if action in EXPIRING_ACTIONS:
        # Clamped HERE, not in the adapter. A suppression with no expiry is the one
        # outcome the verb exists to prevent, so the bound is applied at the boundary
        # every sink goes through rather than trusted to each sink separately — an
        # adapter that forgot the check would silence a monitor forever.
        payload["duration_secs"] = resolve_silence_secs(body.get("duration_secs"))

    result = await _execute_authorized(sink, permit, payload)
    _audit(
        "incident_action",
        f"{incident_id} {action} via {sink.id}"
        + (f" for {payload['duration_secs']}s" if "duration_secs" in payload else ""),
        "success" if result.ok else "failed",
        error=result.error,
    )
    verification = ""
    verify_after = ""
    # A SIMULATED result schedules nothing. ``ok=True`` from the observe-only sink means "we
    # successfully did nothing", and the recheck cannot tell that from a real write: it read
    # the still-firing alarm as the action having failed and charged a ``miss_count`` to
    # every ledger entry the investigation cited. On a default install that is the ONLY
    # path, because `cloudwatch` and `webhook` register no ActionSink and every action falls
    # through to `noop` — so watching the proposal flow, which is exactly what an operator
    # is told to do before granting real authority, demoted their own proven knowledge for
    # a write nobody made. Verified before fixing: act mode plus one scoped cloudwatch rule
    # took a verified/high/2-use entry to `miss_count=1` and off the fast path.
    if result.ok and not result.simulated:
        # The SINK's reported suppression wins over the requested duration: an adapter
        # that aliases one verb onto a bounded mute (Datadog's `resolve`) is the only
        # party that knows the real window, and rechecking inside it manufactures a miss.
        verification, verify_after = await asyncio.to_thread(
            _schedule_verification,
            incident_id,
            action,
            result.suppressed_secs or payload.get("duration_secs"),
        )
    return web.json_response(
        {
            "ok": result.ok,
            "action": result.action,
            "detail": result.detail,
            "error": result.error,
            # Echoed so a caller can see the window actually applied, which may be
            # smaller than the one it asked for.
            "duration_secs": payload.get("duration_secs"),
            # What a 2xx from the provider now DOES and does not mean, reported in the
            # same response that used to imply "applied". ``pending`` says a recheck is
            # scheduled; ``not_checkable`` says this app cannot observe this verb's
            # outcome; ``""`` says the call failed so nothing was scheduled.
            "verification": verification,
            "verify_after": verify_after,
            # Present on both branches so a client reads ONE shape. `ok` already carries
            # the boolean; `code` is what a caller switches on, and the 502 branch is a
            # genuine error response the localized UI must not render as English prose.
            "code": "" if result.ok else "sink_execute_failed",
        },
        status=200 if result.ok else 502,
    )


def _schedule_verification(incident_id: str, action: str, duration_secs: Any) -> tuple[str, str]:
    """Record what was just done and when to re-read the signal. Returns (verdict, due).

    Two schedules, and the difference is the point ``ACTION_SILENCE``'s mandatory expiry
    buys. A suppression is rechecked at the END of its own window — which is the
    interesting moment, because a suppression that expires straight back into the same
    firing condition is positive evidence nothing was fixed. Everything else is rechecked
    after ``DEFAULT_VERIFY_AFTER_SECS``, long enough for a provider evaluating on a period
    to catch up.

    "A suppression", not "a ``silence``": the caller passes whatever window was actually
    ESTABLISHED, which the sink reports via ``ActionResult.suppressed_secs`` and which can
    be non-zero for a verb that is not ``silence`` at all. Datadog implements ``resolve``
    as an alias onto the same bounded mute, so keying this on the verb rechecked a 4-hour
    mute after 5 minutes and charged a false miss.

    An action outside ``VERIFIABLE_ACTIONS`` is stamped ``not_checkable`` with NO due
    date, so ``verify_pending_actions`` never picks it up. That is deliberate honesty
    rather than a gap left open: an ack leaves an alert firing by design, so a verdict
    derived from firing state would be a confident wrong answer about an unverifiable
    write. The board says "not checked" instead.

    Never raises: the provider write already happened and cannot be undone, so a failure
    to record the bookkeeping must not turn a completed action into a 500. It degrades to
    "no verification scheduled", which the response then reports honestly.
    """
    if action not in VERIFIABLE_ACTIONS:
        verdict, due = VERIFY_NOT_CHECKABLE, ""
    else:
        verdict = VERIFY_PENDING
        try:
            wait = int(duration_secs) if duration_secs else DEFAULT_VERIFY_AFTER_SECS
        except (TypeError, ValueError):
            wait = DEFAULT_VERIFY_AFTER_SECS
        due = (datetime.now(timezone.utc) + timedelta(seconds=max(1, wait))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    now = utc_now_iso()
    try:
        store.update_fields(
            incident_id,
            last_action=action,
            last_action_at=now,
            verify_after=due,
            verification=verdict,
            verification_detail="",
        )
    except (KeyError, ValueError, OSError):
        logger.exception("ops-mission-control: could not schedule verification for %s", incident_id)
        return "", ""
    return verdict, due


# ---------------------------------------------------------------------------
# Signals / providers
# ---------------------------------------------------------------------------


def _sink_refuses(sink: Any, action: str) -> str:
    """Why ``sink`` cannot perform ``action``, or "" when it can.

    ``supported_actions()`` was declared on every adapter and enforced NOWHERE — a UI
    hint rather than a gate. So an action the autonomy rules authorized could still reach
    an adapter with no defined behaviour for it: GitHub Issues supports only
    ``{resolve, comment}``, and an authorized ``ack`` arriving there is an undefined call
    against a real repository, with the adapter free to do whatever its ``execute`` falls
    through to. Found in review.

    FAIL CLOSED. This gate fronts a real provider write, and every way the probe fails to
    return a set CONTAINING the action means the same thing: we could not positively confirm
    the adapter supports it. "I could not confirm" is not "it is supported" — an earlier
    version treated a missing method, a raising probe, and an EMPTY set as ALLOW, which is the
    exact undefined-`execute`-call this function exists to prevent, reached three other ways.
    `supported_actions` is part of the `ActionSink` protocol, so its absence is a broken
    adapter, not a legacy one; a raise is a broken probe; an empty set is an adapter that
    declares it can do nothing. In all three the safe answer against a production write is to
    refuse, and the ordinary autonomy gate having authorized the action does not change that,
    because authorization says "the operator permits this verb", not "this sink can perform
    it". A broken companion probe therefore degrades to "this action is refused", never to a
    crash and never to an unconfirmed write. Found in review (GPT 5.6).
    """
    probe = getattr(sink, "supported_actions", None)
    sink_id = getattr(sink, "id", "?")
    if not callable(probe):
        return (
            f"sink {sink_id!r} does not declare supported_actions(), so {action!r} cannot be "
            "confirmed as supported"
        )
    try:
        supported = probe()
    except Exception:  # noqa: BLE001 — a faulty probe refuses, it does not crash the path
        logger.exception("ops-mission-control: supported_actions() raised for sink %r", sink_id)
        return f"sink {sink_id!r} could not report its supported actions, so {action!r} is refused"
    if action in (supported or frozenset()):
        return ""
    return (
        f"sink {sink_id!r} does not support {action!r} " f"(supports {sorted(supported or [])})"
    )


def _url_has_userinfo(remote: str) -> bool:
    """True when an http(s) ``remote`` carries ANY userinfo (``scheme://anything@host``).

    **Any userinfo at all, not just a password.** The first version of this checked
    ``parts.password`` only, reasoning that a bare ``user@`` is a username rather than a
    secret. That was wrong, and wrong in the shape that matters most: GitHub's own documented
    token remote puts the PAT in the USERNAME position —
    ``https://ghp_xxx@github.com/org/repo.git`` — with no password at all. So the single
    likeliest way an operator pastes a token was the one shape explicitly allowed through, and
    a test asserted that as correct. Found in review.

    On http(s) there is no case where userinfo is worth storing in a world-readable file: a
    real username is supplied by a credential helper, and anything else is a secret. Refuse
    both halves.

    Still narrow where narrowness is right — two legitimate shapes contain an ``@``:

    - **scp-style**: ``git@github.com:org/repo.git`` — no ``://``, so no userinfo component
      exists. The single most common remote form there is; flagging it would break the
      recommended setup.
    - **ssh:// with a user**: ``ssh://git@github.com/org/repo.git`` — userinfo, but SSH
      authenticates by key, so the username is not a secret.

    Parsed with ``urlsplit`` rather than matched by hand: the authority ends at the first
    ``/``, ``?`` or ``#``, so a path-embedded ``@`` (``https://host/a@b``) is not misread as
    userinfo.
    """
    if "://" not in remote:
        return False  # scp-style; no userinfo component exists
    try:
        parts = urlsplit(remote)
    except ValueError:  # pragma: no cover — malformed enough that git will reject it too
        return True  # unparseable: refuse rather than guess
    if parts.scheme.lower() not in {"http", "https"}:
        return False  # ssh/git authenticate by key; a bare username is not a secret
    # `username`/`password` come from the AUTHORITY only, so a path-embedded `@` is not
    # mistaken for userinfo.
    return bool(parts.username or parts.password)


def _safe_outbound(text: str) -> str:
    """The redaction floor for text this app SENDS to a provider.

    An action note is agent- or operator-authored free text that becomes an
    acknowledgement comment, a resolve reason or a mute note **on someone else's system**,
    where we cannot unpublish it. It reached the sink verbatim: an agent that pasted a
    provider token into its diagnosis published that token into the provider's own comment
    thread. Found in review — the same shape as the Slack sink and the ledger write path,
    which were already covered while this one was not.

    Both passes, and `redact_via_context` rather than `security.redact` directly, for the
    same reasons the ledger write path documents: the two redactors cover different token
    families, and the context shim makes a loaded companion's declared patterns apply while
    an enterprise host that fails to compose its companion fails CLOSED.
    """
    return redact_tokens(redact_via_context(text))


class _Authorized:
    """Proof that the autonomy gate ran and allowed this exact (signal, action).

    Why a token rather than a comment. ``ActionSink.execute`` does not police its own
    authority — by design, spec §5.3 — and the gate lived at two independent call sites
    with the ordering held together by convention. Review named the shape: "autonomy
    enforcement is a convention, not a chokepoint. A third caller can silently skip the
    gate." Nothing in the code disagreed.

    So the permission is now a VALUE that only ``_authorize`` mints, and the only function
    that touches ``sink.execute`` demands one. A new caller cannot reach the write without
    holding a token, and cannot fabricate one without going through the gate that mints it
    — the mistake becomes a type error at authoring time instead of an unauthorized
    provider write in production.

    Deliberately not a general capability object. It carries the exact signal and action it
    was minted for, and ``_execute_authorized`` reads the write's target FROM the permit
    rather than from a parallel argument — so there is no way to hold a permit for
    ``comment`` and spend it on ``resolve``: the mismatch is not rejected, it is
    unrepresentable.
    """

    __slots__ = ("signal", "action", "reason")

    def __init__(self, signal: Signal, action: str, reason: str) -> None:
        self.signal = signal
        self.action = action
        self.reason = reason


async def _authorize(signal: Signal, action: str) -> tuple[_Authorized | None, str]:
    """Run the autonomy gate. Returns ``(token, reason)``; the token is ``None`` on deny.

    The ONLY place an ``_Authorized`` is created, which is what makes it proof.

    Off the event loop: ``rotation.authorize_action`` is synchronous by design, and its
    off-shift check reads the committed schedule and — with no ``schedule-file.github_login``
    configured, the documented default — resolves this instance's identity by spawning
    ``gh api user``, a blocking HTTPS round trip with a 10s timeout on the first call of a
    fresh gateway process. Run inline it freezes every other task on the loop, the user's
    chat turn and the liveness heartbeat included. The handlers that reach this gate never
    ``await registry.resolve_shift()`` first, so nothing has warmed the login cache
    off-loop by the time we get here.
    """
    allowed, reason = await asyncio.to_thread(rotation.authorize_action, signal, action)
    if not allowed:
        return None, reason
    return _Authorized(signal, action, reason), reason


async def _execute_authorized(sink: Any, permit: _Authorized, payload: dict[str, Any]) -> Any:
    """The ONLY path to ``ActionSink.execute``. Requires a gate-minted permit.

    The signal and action come FROM the permit, never from a separate argument — that is
    what makes "executed something other than what was authorized" unrepresentable rather
    than merely checked for.
    """
    return await sink.execute(permit.signal, permit.action, payload)


async def _execute_stored_proposal(
    incident: Any, proposal: dict[str, Any], permit: _Authorized
) -> dict[str, Any]:
    """Run an APPROVED proposal's stored terms through the ordinary sink path.

    Shares the per-provider ownership rule with ``_handle_action``: the sink must be the
    one that raised the signal. A proposal could otherwise name any sink at draft time
    and have it honoured at approve time, which would route around the very check that
    made caller-selected sinks safe.

    Only the payload is rebuilt, and only from stored fields — never from the approving
    request. The suppression window is re-clamped here because a stored value could have
    been written before the clamp existed.
    """
    registry = get_registry()
    sink_id = str(proposal.get("sink", ""))
    if sink_id and sink_id != incident.signal.source:
        return {
            "ok": False,
            "executed": False,
            "error": (
                f"proposal names sink {sink_id!r}, which does not own this incident's "
                f"signal ({incident.signal.source!r})"
            ),
            "code": "sink_not_owner",
        }
    sink = registry.action_sink(incident.signal.source) or registry.action_sink("noop")
    if sink is None:
        return {
            "ok": False,
            "executed": False,
            "error": "no action sink available",
            "code": "no_action_sink",
        }

    # From the PERMIT, not from the proposal dict. Both are derived from the same stored
    # field at the call site, so they agree today — but reading it twice is exactly the
    # coupling-by-convention this permit exists to remove: the capability check, the payload
    # shape and the write must all describe one action, and the permit is that one.
    action = permit.action
    refusal = _sink_refuses(sink, action)
    if refusal:
        return {"ok": False, "executed": False, "error": refusal, "code": "action_unsupported"}
    payload: dict[str, Any] = {"note": _safe_outbound(str(proposal.get("note", "")))}
    if action in EXPIRING_ACTIONS:
        payload["duration_secs"] = resolve_silence_secs(proposal.get("duration_secs"))

    result = await _execute_authorized(sink, permit, payload)
    _audit(
        "incident_action",
        f"{incident.incident_id} {action} via {sink.id} (approved proposal)",
        "success" if result.ok else "failed",
        error=result.error,
    )

    # Schedule the post-action recheck, exactly as `_handle_action` does. An approved
    # proposal executes the SAME real provider write as a direct action, so it must record
    # `last_action`/`last_action_at` and arm verification the same way — otherwise a resolve
    # or silence went out, `verify_pending_actions` never ran, `last_action` stayed empty,
    # and the incident record and postmortem showed a write that "never happened". The two
    # execution paths converge on `_execute_authorized`; this makes their FOLLOW-UP converge
    # too. `result.ok and not result.simulated` for the same reason: a `noop`-simulated write
    # changed nothing at the provider, so rechecking it would read the still-firing alarm as
    # a failure and charge a miss to the cited ledger entries. Found in review.
    # `result.suppressed_secs or payload.get("duration_secs")` — the SINK's reported window
    # first, identical to `_handle_action`. Datadog aliases `resolve` onto a bounded mute, and
    # only `EXPIRING_ACTIONS` (i.e. `silence`) gets a `duration_secs` from the route, so reading
    # the payload alone scheduled a five-minute recheck against a four-hour suppression and
    # charged a false miss to the cited ledger entries. The fix landed on the direct-action path
    # and not here, which is this PR's recurring lesson in miniature: the two paths converge on
    # `_execute_authorized` for the WRITE, so their follow-up has to converge too, and a fix
    # applied one layer up does not protect a path that does not pass through it. Found in
    # review (GPT 5.6).
    verification = verify_after = ""
    if result.ok and not result.simulated:
        verification, verify_after = await asyncio.to_thread(
            _schedule_verification,
            incident.incident_id,
            action,
            result.suppressed_secs or payload.get("duration_secs"),
        )
    return {
        "ok": result.ok,
        "executed": True,
        "action": result.action,
        "detail": result.detail,
        "error": result.error,
        "verification": verification,
        "verify_after": verify_after,
        "code": "" if result.ok else "sink_execute_failed",
    }


async def _handle_propose(request: web.Request) -> web.StreamResponse:
    """Record the exact action an agent would take, for a human to approve.

    Deliberately NOT gated on the autonomy mode. A proposal changes nothing in the
    operator's tooling — it is the safe half of the loop, and refusing to let an
    ``observe`` instance draft one would mean the mode below ``act`` still had nothing
    to show. The gate that matters runs on APPROVE, where the write happens.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"},
            status=400,
        )
    incident_id = str(body.get("id", "")).strip()
    action = str(body.get("action", "")).strip()
    sink = str(body.get("sink", "")).strip()
    # Redact BEFORE the clip: truncating first could sever a token so the pattern no
    # longer matches, and clipping after masking only ever shortens a placeholder.
    note = _safe_outbound(str(body.get("note", "")))[:_MAX_NOTE_LEN]
    if not incident_id:
        return web.json_response(
            {"error": "id is required", "code": "missing_required_field"}, status=400
        )
    if action not in VALID_ACTIONS:
        return web.json_response(
            {
                "error": f"action must be one of {sorted(VALID_ACTIONS)}",
                "code": "invalid_action",
            },
            status=400,
        )
    duration = (
        resolve_silence_secs(body.get("duration_secs")) if action in EXPIRING_ACTIONS else None
    )
    try:
        incident = await asyncio.to_thread(
            store.propose_action,
            incident_id,
            action=action,
            sink=sink or "",
            note=note,
            duration_secs=duration,
        )
    except KeyError:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_proposal"}, status=400)

    _audit("incident_propose", f"{incident_id} {action} via {sink}", "success")
    return web.json_response({"incident": incident.to_dict()})


async def _handle_decide_proposal(request: web.Request) -> web.StreamResponse:
    """Approve or reject a pending proposal, then EXECUTE the stored terms verbatim.

    Approving runs the action through the same ``authorize_action`` gate a direct call
    uses, so approval cannot launder a write past the autonomy ceiling: an operator who
    approves a proposal on an ``observe`` instance gets the decision recorded and the
    execution refused, which is the honest outcome rather than a silent upgrade.

    The executed terms come from the STORE, never from this request body. That is the
    point of the whole mechanism — a request that could supply its own note would let the
    text change between the operator reading it and the action firing.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"},
            status=400,
        )
    incident_id = str(body.get("id", "")).strip()
    try:
        # REQUIRED and strictly boolean: this field decides whether a production write happens,
        # so an ambiguous value must 400 rather than be guessed at in either direction.
        approve = _require_bool(body, "approve")
    except _NotABool:
        return web.json_response(
            {
                "error": "approve must be true or false (a JSON boolean, not a string)",
                "code": "invalid_field_type",
            },
            status=400,
        )
    if approve is None:
        return web.json_response(
            {"error": "approve is required", "code": "missing_required_field"}, status=400
        )
    digest = str(body.get("digest", "")).strip()
    if not incident_id:
        return web.json_response(
            {"error": "id is required", "code": "missing_required_field"}, status=400
        )
    try:
        decision = await asyncio.to_thread(
            store.decide_proposal, incident_id, approve=approve, digest=digest
        )
    except KeyError:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    if not decision["ok"]:
        _audit("incident_proposal_decide", incident_id, "rejected", error=decision["reason"])
        return web.json_response(
            {"error": decision["reason"], "code": "proposal_conflict"}, status=409
        )

    proposal = decision["proposal"]
    _audit(
        "incident_proposal_decide",
        f"{incident_id} {proposal['action']} {proposal['state']}",
        "success",
    )
    if not approve:
        return web.json_response({"ok": True, "proposal": proposal, "executed": False})

    # Approved: execute the STORED terms through the normal gate.
    incident = await asyncio.to_thread(store.get_incident, incident_id)
    if incident is None:  # pragma: no cover — decided above, so it existed a moment ago
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    # Same gate, same minter as the direct-action path. The permit is PASSED to the executor
    # rather than the executor re-deriving authority, so the gate and the write cannot drift
    # apart the way two independent call sites can.
    permit, reason = await _authorize(incident.signal, str(proposal["action"]))
    if permit is None:
        return web.json_response(
            {
                "ok": False,
                "proposal": proposal,
                "executed": False,
                "error": reason,
                "code": "not_authorized",
                "authorized": False,
            },
            status=403,
        )
    result = await _execute_stored_proposal(incident, proposal, permit)
    return web.json_response({"ok": result["ok"], "proposal": proposal, **result})


async def _handle_proposals(request: web.Request) -> web.StreamResponse:
    """The pending-proposal queue — the thing an operator could not see at all before."""
    pending = await asyncio.to_thread(store.pending_proposals)
    return web.json_response(
        {
            "proposals": [
                {
                    "incident_id": inc.incident_id,
                    "title": inc.signal.title,
                    "source": inc.signal.source,
                    "severity": inc.signal.severity,
                    **(inc.proposed_action or {}),
                }
                for inc in pending
            ],
            "total": len(pending),
        }
    )


async def _handle_signals(request: web.Request) -> web.StreamResponse:
    """Current provider state: what is firing, what is unclaimed, and what we could not read.

    ``firing`` is the list a caller should reason about, and it is filtered the same way
    ``dispatch.run_cycle`` filters — previously this route returned every signal
    regardless of state under the key ``signals``, while dispatch claimed only firing
    ones. That was harmless while no adapter could emit ``ok``; once one can, an
    already-cleared signal would appear in the very list the reconcile SOP reads as
    "what is still firing", and in ``unclaimed`` as apparent work.

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


async def _handle_providers(request: web.Request) -> web.StreamResponse:
    return web.json_response({"providers": [_provider_dict(p) for p in get_registry().catalog()]})


async def _handle_put_provider_config(request: web.Request) -> web.StreamResponse:
    """Update one provider's NON-SECRET config (enable flag, region, ids, …).

    Two guards, both load-bearing because this file is served unauthenticated:

    1. Only keys the adapter declares in ``config_fields`` are accepted — an
       unknown key cannot become a place to stash data.
    2. Any key matching the adapter's ``secret_fields`` is REFUSED. A settings
       form that accidentally posted a token here would otherwise write it into a
       world-readable-over-the-port file; secrets must go to the keystone route.
    """
    provider_id = request.match_info.get("provider_id", "").strip()
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )

    known = {p.id: p for p in get_registry().catalog()}
    info = known.get(provider_id)
    if info is None:
        return web.json_response(
            {"error": "unknown provider", "code": "unknown_provider"}, status=404
        )

    allowed = set(info.config_fields)
    secret_names = set(info.secret_fields)
    updates: dict[str, Any] = {}
    for key, value in body.items():
        name = str(key)
        if name in secret_names:
            _audit(
                "provider_config_put",
                f"{provider_id}.{name}",
                "rejected",
                error="secret field submitted to the non-secret config route",
            )
            return web.json_response(
                {
                    "error": (
                        f"{name!r} is a secret field — use "
                        f"PUT /providers/{provider_id}/secret so it lands in the "
                        f"protected store, not the unauthenticated config file"
                    ),
                    "code": "secret_field_on_config_route",
                },
                status=400,
            )
        if name not in allowed:
            return web.json_response(
                {
                    "error": f"provider {provider_id!r} has no config field {name!r}",
                    "code": "unknown_config_field",
                },
                status=400,
            )
        # Coerce to the JSON-safe scalars the adapters read.
        updates[name] = value if isinstance(value, (bool, int, float, list)) else str(value)

    if not updates:
        return web.json_response(
            {"error": "no config fields supplied", "code": "no_recognized_fields"}, status=400
        )

    saved = await asyncio.to_thread(merge_provider_config, provider_id, updates)
    _audit("provider_config_put", f"{provider_id}:{sorted(updates)}", "success")
    return web.json_response({"ok": True, "provider": provider_id, "config": saved})


async def _handle_put_settings(request: web.Request) -> web.StreamResponse:
    """Update app-level settings: autonomy mode, primary flag, cycle tuning.

    ``mode`` is the autonomy ceiling, so an unrecognized value is refused rather
    than silently falling back — a typo must not quietly change what the agent is
    allowed to do.

    EVERY FIELD IS VALIDATED BEFORE ANY FIELD IS WRITTEN. The handler is two phases with
    nothing interleaved: phase 1 parses and validates into locals and can only ``return 400``;
    phase 2 performs the writes and cannot fail validation. This took three rounds to get
    right, and the shape of the mistake repeated each time, so it is worth stating plainly:

    - Round one: ``mode`` was written before the rules were validated, so ``mode=act`` plus one
      malformed rule wrote the mode, returned 400, and left the instance in ``act`` —
      activating whatever grants were already stored, from a request the operator was told had
      FAILED.
    - Round two: validating both halves of that PAIR first made the pair atomic but not the
      REQUEST — ``mode=act`` plus an over-long ``ledger_sync_remote`` still persisted ``act``
      and then 400'd. So the ceiling writes moved to the end.
    - Round three (this one): moving only the ceiling was still the wrong scope. Every other
      field had the same defect — ``{"primary_instance": false, "ledger_sync_branch": "--bad"}``
      returned 400 having already flipped leadership, which changes which instance passes the
      ``not_primary`` gate on ``POST /ledger/hygiene``. "Which field is dangerous to
      half-apply?" is the wrong question to keep re-answering; a rejected request must change
      NOTHING. Found in review each time.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )

    # ---- PHASE 1: validate everything. Only `return 400` below this line, never a write. ----

    mode: str | None = None
    if "mode" in body:
        mode = str(body["mode"]).strip()
        if mode not in MODE_ORDER:
            return web.json_response(
                {"error": f"mode must be one of {sorted(MODE_ORDER)}", "code": "invalid_mode"},
                status=400,
            )

    rules: list[dict[str, Any]] | None = None
    if "autonomy_rules" in body:
        # A rule that fails validation is REFUSED, not stored-and-ignored: `load_rules` drops
        # unparseable entries, so writing them would show the operator a saved grant that
        # silently never matches.
        ok, code, rules = await asyncio.to_thread(rotation.validate_rules, body["autonomy_rules"])
        if not ok:
            return web.json_response(
                {
                    "error": (
                        "an autonomy rule was rejected: a rule must name a source plus at "
                        "least one of resource_glob or label_match, and an act-rule may not "
                        "be a blanket grant"
                    ),
                    "code": code,
                },
                status=400,
            )

    # The rotation identity and the strict-gating flag are the OTHER two inputs to the same
    # authorization decision as `mode`/`autonomy_rules`, and they live on the same fenced floor.
    login: str | None = None
    if "schedule_github_login" in body:
        login = str(body["schedule_github_login"]).strip()
        if login and not _SAFE_LOGIN_RE.fullmatch(login):
            return web.json_response(
                {
                    "error": (
                        "schedule_github_login must be a GitHub login: letters, digits and "
                        "single hyphens, up to 39 characters"
                    ),
                    "code": "invalid_github_login",
                },
                status=400,
            )

    strict: bool | None = None
    if "schedule_strict_gating" in body:
        try:
            strict = _require_bool(body, "schedule_strict_gating")
        except _NotABool:
            return web.json_response(
                {
                    "error": "schedule_strict_gating must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    # PagerDuty's rotation identity, fenced for the same reason and written here for the same
    # reason. Shape-checked only for length: PagerDuty ids are opaque (`PXXXXXX` today), so a
    # tighter pattern would be this app asserting a vendor format it does not own.
    pd_user: str | None = None
    if "pagerduty_user_id" in body:
        pd_user = str(body["pagerduty_user_id"]).strip()
        if len(pd_user) > _MAX_PROVIDER_ID_LEN:
            return web.json_response(
                {"error": "pagerduty_user_id is too long", "code": "value_too_long"}, status=400
            )

    primary: bool | None = None
    if "primary_instance" in body:
        try:
            primary = _require_bool(body, "primary_instance")
        except _NotABool:
            return web.json_response(
                {
                    "error": "primary_instance must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    slack_enabled: bool | None = None
    if "slack_enabled" in body:
        try:
            slack_enabled = _require_bool(body, "slack_enabled")
        except _NotABool:
            return web.json_response(
                {
                    "error": "slack_enabled must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    slack_channel: str | None = None
    if "slack_channel" in body:
        slack_channel = str(body["slack_channel"]).strip()

    notify_enabled: bool | None = None
    if "notify_enabled" in body:
        try:
            notify_enabled = _require_bool(body, "notify_enabled")
        except _NotABool:
            return web.json_response(
                {
                    "error": "notify_enabled must be true or false (a JSON boolean)",
                    "code": "invalid_field_type",
                },
                status=400,
            )

    # Shared-ledger git sync: the team's memory-exchange repo. A remote URL and a
    # branch name are not credentials (auth is the operator's own git/ssh/gh
    # config), so they belong in plain app config like the Slack channel above.
    #
    # These were previously settable ONLY by hand-editing ``data/config.json``:
    # ``ledger_sync.set_settings`` existed and worked, but nothing outside the
    # tests ever called it, so the app's headline team feature had no way in. An
    # operator looking for "where do I point this at my team repo?" correctly
    # found nothing.
    wants_sync = (
        "ledger_sync_remote" in body
        or "ledger_sync_branch" in body
        or "ledger_sync_enabled" in body
    )
    remote_url = str(body["ledger_sync_remote"]).strip() if "ledger_sync_remote" in body else None
    branch_name = str(body["ledger_sync_branch"]).strip() if "ledger_sync_branch" in body else None
    try:
        sync_enabled = _require_bool(body, "ledger_sync_enabled")
    except _NotABool:
        return web.json_response(
            {
                "error": "ledger_sync_enabled must be true or false (a JSON boolean)",
                "code": "invalid_field_type",
            },
            status=400,
        )
    if remote_url is not None and len(remote_url) > _MAX_REMOTE_LEN:
        return web.json_response(
            {"error": "ledger_sync_remote is too long", "code": "value_too_long"}, status=400
        )
    # REFUSE a credential-bearing remote instead of storing it.
    #
    # `data/config.json` is served over `/api/apps/<name>/config` WITHOUT session auth,
    # and `redact_tokens` has no pattern for a PAT embedded in a URL — so
    # `https://user:ghp_xxx@github.com/org/repo.git` pasted here was persisted verbatim
    # into a world-readable file and echoed into SEL output. The frontend's
    # `displayRemote()` strips userinfo for DISPLAY, and its own docstring said outright
    # that the value is still stored and "this function changes nothing about that" — a
    # documented hole rather than a fixed one. Review blocked on it, correctly.
    #
    # Refusing rather than silently stripping: the token the operator pasted is now
    # compromised-by-paste either way, and a remote quietly rewritten to an
    # unauthenticated URL would fail to push later with no hint why. Tell them, so they
    # can rotate it and use a credential helper or SSH.
    if remote_url and _url_has_userinfo(remote_url):
        return web.json_response(
            {
                "error": (
                    "the remote URL contains an embedded username/password — remove it "
                    "and let git supply credentials (a credential helper, or an SSH "
                    "remote). Anything stored here is served unauthenticated, so treat "
                    "a token you already pasted as compromised and rotate it."
                ),
                "code": "remote_has_credentials",
            },
            status=400,
        )
    if branch_name and not _SAFE_BRANCH_RE.fullmatch(branch_name):
        # A branch name reaches a ``git`` argv. It is already passed as its own
        # argument and never interpolated into a shell string, so this is about
        # refusing option-like or whitespace-bearing values up front rather than
        # letting them surface later as a confusing sync failure.
        return web.json_response(
            {"error": "ledger_sync_branch is not a valid ref", "code": "invalid_branch_ref"},
            status=400,
        )

    numerics: dict[str, int] = {}
    for numeric_key in (
        "max_claims_per_cycle",
        "stale_after_secs",
        # Sits beside ``stale_after_secs`` because it is the same knob for the other
        # sweepable class: how long an unanswered ``needs_human`` incident may hold its
        # signal before the sweep releases it. Unset means "derive from
        # ``stale_after_secs``" (see ``store.sweep_stale``).
        "needs_human_stale_after_secs",
    ):
        if numeric_key not in body:
            continue
        try:
            numeric_value = int(body[numeric_key])
        except (TypeError, ValueError):
            return web.json_response(
                {"error": f"{numeric_key} must be an integer", "code": "invalid_field_type"},
                status=400,
            )
        if numeric_value <= 0:
            return web.json_response(
                {"error": f"{numeric_key} must be positive", "code": "value_out_of_range"},
                status=400,
            )
        numerics[numeric_key] = numeric_value

    recognized = (
        mode is not None
        or rules is not None
        or login is not None
        or strict is not None
        or primary is not None
        or slack_enabled is not None
        or slack_channel is not None
        or notify_enabled is not None
        or pd_user is not None
        or wants_sync
        or bool(numerics)
    )
    if not recognized:
        return web.json_response(
            {"error": "no recognized settings supplied", "code": "no_recognized_fields"}, status=400
        )

    # ---- PHASE 2: write. Everything above validated, so nothing here can 400. ----

    applied: dict[str, Any] = {}

    # Slack output. A channel ID is not a credential, so it belongs here rather
    # than in the secret store — and this app stores no Slack token at all, it
    # reuses Kiro Crew's own client (see slack_out for why).
    if slack_enabled is not None:
        await asyncio.to_thread(slack_out.set_settings, enabled=slack_enabled)
        applied["slack_enabled"] = slack_enabled

    if slack_channel is not None:
        await asyncio.to_thread(slack_out.set_settings, channel_id=slack_channel)
        applied["slack_channel"] = slack_channel

    # Local desktop notifications. Nothing to configure beyond on/off — there is no
    # destination and no credential, which is the whole point of this channel.
    if notify_enabled is not None:
        await asyncio.to_thread(notify_out.set_settings, enabled=notify_enabled)
        applied["notify_enabled"] = notify_enabled

    if wants_sync:
        # Deferred import, matching the hygiene handler below: ``ledger_sync`` pulls in
        # the git/sandbox machinery, and this module is imported at gateway start.
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        await asyncio.to_thread(
            ledger_sync.set_settings,
            enabled=sync_enabled,
            remote_url=remote_url,
            branch_name=branch_name,
        )
        for sync_key, sync_value in (
            ("ledger_sync_remote", remote_url),
            ("ledger_sync_branch", branch_name),
            ("ledger_sync_enabled", sync_enabled),
        ):
            if sync_value is not None:
                applied[sync_key] = sync_value

    for numeric_key, numeric_value in numerics.items():
        await asyncio.to_thread(set_top_level, numeric_key, numeric_value)
        applied[numeric_key] = numeric_value

    # The authorization inputs go to the keystone store, not `set_top_level` (which writes
    # the agent-writable config.json): they ARE the security ceiling, and this authenticated PUT
    # is their sole writer. See `policy_store`.
    if primary is not None:
        await asyncio.to_thread(policy_store.put, policy_store.PRIMARY_KEY, primary)
        applied["primary_instance"] = primary
    # ONE call for both halves, so they commit under ONE lock acquisition. `mode` and
    # `autonomy_rules` are a single authorization decision (`effective = min(app_mode,
    # rule_mode)`), and two separately-locked writes let a CONCURRENT settings PUT interleave
    # between them — request A's `act` landing with request B's broader rules, authorizing a
    # write neither operator asked for. The two-phase validate-then-write discipline above
    # cannot close that window, because the interleaving comes from another request rather
    # than from ordering inside this one. Found in review (GPT 5.6).
    if mode is not None or rules is not None:
        await asyncio.to_thread(policy_store.set_ceiling, mode=mode, rules=rules)
        if mode is not None:
            applied["mode"] = mode
        if rules is not None:
            applied["autonomy_rules"] = rules
    if login is not None:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import schedule_file

        await asyncio.to_thread(policy_store.put, policy_store.SCHEDULE_LOGIN_KEY, login)
        # A changed identity invalidates the cached `gh` answer, which is only a fallback for
        # an unset login — leaving it would keep answering with the previous operator.
        await asyncio.to_thread(schedule_file.reset_login_cache)
        applied["schedule_github_login"] = login
    if strict is not None:
        await asyncio.to_thread(policy_store.put, policy_store.SCHEDULE_STRICT_KEY, strict)
        applied["schedule_strict_gating"] = strict
    if pd_user is not None:
        await asyncio.to_thread(policy_store.put, policy_store.PAGERDUTY_USER_KEY, pd_user)
        applied["pagerduty_user_id"] = pd_user

    _audit("settings_put", f"{sorted(applied)}", "success")
    return web.json_response({"ok": True, "applied": applied})


async def _handle_put_secret(request: web.Request) -> web.StreamResponse:
    """Store a provider secret. Write-only: the value is never readable back."""
    provider_id = request.match_info.get("provider_id", "").strip()
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    field_name = str(body.get("field", "")).strip()
    value = str(body.get("value", ""))
    if not provider_id or not field_name:
        return web.json_response(
            {"error": "provider_id and field are required", "code": "missing_required_field"},
            status=400,
        )
    if not value:
        return web.json_response(
            {"error": "value must not be empty", "code": "missing_required_field"}, status=400
        )
    if len(value) > _MAX_SECRET_LEN:
        return web.json_response(
            {"error": "value is too long", "code": "value_too_long"}, status=400
        )

    known = {p.id: p for p in get_registry().catalog()}
    info = known.get(provider_id)
    if info is None:
        return web.json_response(
            {"error": "unknown provider", "code": "unknown_provider"}, status=404
        )
    if field_name not in info.secret_fields:
        # Reject unknown field names so the keystone file cannot be used as
        # arbitrary agent-inaccessible storage.
        return web.json_response(
            {
                "error": f"provider {provider_id!r} has no secret field {field_name!r}",
                "code": "unknown_secret_field",
            },
            status=400,
        )

    await asyncio.to_thread(put_secret, provider_id, field_name, value)
    return web.json_response({"ok": True, "provider": provider_id, "field": field_name})


async def _handle_delete_secret(request: web.Request) -> web.StreamResponse:
    provider_id = request.match_info.get("provider_id", "").strip()
    if not provider_id:
        return web.json_response(
            {"error": "provider_id is required", "code": "missing_required_field"}, status=400
        )
    removed = await asyncio.to_thread(delete_secret, provider_id)
    return web.json_response({"ok": True, "removed": removed})


async def _handle_rotation(request: web.Request) -> web.StreamResponse:
    shift = await get_registry().resolve_shift()
    # Off-loop: `describe()` -> `is_primary()` -> `resolve_login()` can spawn `gh api user`.
    return web.json_response(await asyncio.to_thread(rotation.describe, shift))


async def _handle_rotation_arm(request: web.Request) -> web.StreamResponse:
    """Arm/disarm this app's crons to match the tier map — server-side, not agent-driven.

    The whole point is that the agent no longer decides WHICH crons to pause. It POSTs here;
    ``rotation.apply_tiers`` computes the tier map and refuses to pause an always-tier job
    unconditionally. See that function for why prose in the SOP was not sufficient.
    """
    state = request.app.get("state")
    cron_service = getattr(state, "crons", None)
    if cron_service is None:
        return web.json_response(
            {
                "ok": False,
                "error": "cron service unavailable",
                "code": "cron_service_unavailable",
            },
            status=503,
        )
    shift = await get_registry().resolve_shift()
    return web.json_response(await rotation.apply_tiers(shift, cron_service))


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


async def _handle_get_ledger(request: web.Request) -> web.StreamResponse:
    # BOTH off-loop. `read_entries` already was; `stats()` re-scanned the same file inline
    # right after it, which was both a second full parse and a parse on the event loop.
    entries, stats = await asyncio.gather(
        asyncio.to_thread(ledger.read_entries),
        asyncio.to_thread(ledger.stats),
    )
    entries.sort(key=lambda e: (-e.use_count, e.pattern))
    return web.json_response({"entries": [e.to_dict() for e in entries], "stats": stats})


async def _handle_ledger_contradictions(request: web.Request) -> web.StreamResponse:
    """Entry pairs claiming different fixes for the same fingerprint.

    A read-only diagnostic for the hygiene SOP, which is told to "resolve contradictions"
    and previously had to find them by eye across the whole ledger. Detection is
    deterministic and cheap; the resolution (split the two patterns so each names its own
    cause) needs the model, so this endpoint deliberately changes nothing.
    """
    found = await asyncio.to_thread(ledger.find_contradictions)
    return web.json_response({"contradictions": found, "count": len(found)})


async def _handle_post_ledger(request: web.Request) -> web.StreamResponse:
    """Add or promote a learned pattern.

    ``miss_count`` / ``last_miss`` / ``decayed_at_miss_count`` are deliberately NOT
    accepted from a body, and this is the security-shaped half of §5.9's demotion path.
    The hygiene SOP promotes ``observed`` → ``verified`` by re-POSTing the same
    pattern+fix (ids are content-addressed, so it merges) — so an accepted
    ``miss_count: 0`` on that route would make the promotion step double as a way to
    erase every recorded failure, with one curl, on the exact entries most likely to
    have them. Miss evidence is only ever produced by ``ledger.record_miss``, from an
    observed recheck, and ``upsert`` takes the MAX so a merge cannot lower it either.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    pattern = str(body.get("pattern", "")).strip()
    fix = str(body.get("fix", "")).strip()
    if not pattern or not fix:
        return web.json_response(
            {"error": "pattern and fix are required", "code": "missing_required_field"}, status=400
        )

    # Redact on the WRITE path, before the id is computed.
    #
    # `ledger.jsonl` is the one artifact that leaves this machine: `ledger_sync` commits
    # and pushes it verbatim to a shared remote. Nothing sanitised it. Evidence→prompt and
    # incident→Slack both pass a chokepoint; this path did not, and a `fix` field is the
    # single likeliest place for a pasted credential because that is literally what a fix
    # looks like — a command line, a hostname, a token in a header.
    #
    # Write-path, not sync-path, for two reasons. The entry is already on local disk and in
    # the vector index by the time sync runs; and an operator who enables sync LATER would
    # otherwise retroactively publish everything written before. Recovery from the other
    # ordering is a git history rewrite across every teammate's clone.
    #
    # This changes the content-addressed id, and that is correct: two entries differing
    # only in a redacted secret SHOULD dedupe to one.
    #
    # `redact_via_context` rather than `security.redact` directly, so a loaded companion's
    # declared patterns apply and an enterprise host that fails to compose its companion
    # fails CLOSED on redaction instead of silently falling back to public patterns.
    pattern = redact_tokens(redact_via_context(pattern))
    fix = redact_tokens(redact_via_context(fix))

    raw_fps = body.get("fingerprints")
    raw_keys = body.get("provider_keys")
    entry = LedgerEntry.create(
        pattern=pattern,
        fix=fix,
        fingerprints=[str(f) for f in raw_fps] if isinstance(raw_fps, list) else [],
        # Optional and additive: an entry with no provider key still matches by shape,
        # which is every entry written before this field existed.
        provider_keys=[str(k) for k in raw_keys] if isinstance(raw_keys, list) else [],
        confidence=str(body.get("confidence", "medium")),
        trust=str(body.get("trust", "observed")),
        source=str(body.get("source", "human")),
    )
    stored = await asyncio.to_thread(ledger.upsert, entry)
    return web.json_response({"entry": stored.to_dict()})


async def _handle_ledger_hygiene(request: web.Request) -> web.StreamResponse:
    """Run the deterministic ledger maintenance pass: sync, hygiene, index.

    Called by the ledger-hygiene cron. Deterministic Python rather than an agent
    judgement call, so the mechanical part costs no tokens and the SOP's model
    time goes to the parts that need reasoning (contradictions, promotions).

    **Order is load-bearing:** pull → hygiene → index → push.

    - Pull FIRST so hygiene sees teammates' entries. Deduping before the merge would
      leave freshly-arrived duplicates to sit until tomorrow's pass.
    - Index AFTER hygiene so we do not embed rows hygiene is about to prune, and so a
      promoted ``observed → verified`` entry is indexed at its new importance.
    - Push LAST, carrying hygiene's result — otherwise every instance re-derives the
      same dedupe locally and the repo never converges.

    This is also where the two halves of the git-native memory loop finally get a
    caller. ``ledger_sync`` and ``ledger_index.import_pending`` were both built,
    tested, and **wired to nothing**: sync had no caller at all, and the semantic-recall
    search in ``dispatch`` was querying an index that nothing ever populated — so recall
    silently returned zero hits forever on a real install. A daily cadence is right for
    both: shared lessons are not latency-sensitive, and embedding is the expensive step.

    Every stage is independently fault-tolerant. A missing remote, an offline network, a
    conflicted ledger, or an absent embedding model each degrade to a reported
    sub-result; none prevents the local dedupe/decay/prune from running, because local
    hygiene is the part that always works and always matters.

    **Only the primary instance may run it.** This pass PRUNES a shared ledger, and on a
    team every instance reaching it means N concurrent dedupe/decay/prune passes over one
    file. That is strictly worse than the double-claim the single-owner model exists to
    prevent: a duplicate claim wastes an agent turn, a duplicate prune deletes knowledge.
    ``is_primary()`` was added for exactly this and then never wired to an enforcement
    point — while ``sops/rotation-check.md`` told operators this route "self-gates on
    ``is_primary()`` at runtime", which was not true of any code. A SOP asserting a gate
    that does not exist is worse than no gate, because it stops anyone looking for one.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

    # 409, not 403: the caller is authenticated and permitted, it is simply not this
    # instance's job. A 403 would read as "your credentials are wrong" and send an
    # operator looking in the wrong place.
    if not await asyncio.to_thread(rotation.is_primary):
        leader = await asyncio.to_thread(rotation.primary_owner)
        _audit("ledger_hygiene", f"leader={leader or 'unset'}", "rejected", error="not primary")
        return web.json_response(
            {
                "error": (
                    "this instance is not the primary — ledger hygiene prunes shared "
                    "knowledge, so exactly one instance may run it"
                    + (f" (currently {leader})" if leader else "")
                ),
                "code": "not_primary",
                "changed": False,
            },
            status=409,
        )

    pulled = await ledger_sync.sync_safely(direction="pull")
    summary = await asyncio.to_thread(ledger.hygiene)
    indexed = await asyncio.to_thread(_index_ledger_safely)
    # Retire old CLOSED incidents. Here rather than on the claim path because pruning is
    # maintenance: doing it in `claim` would make an ordinary claim occasionally pay for a
    # large rewrite. Open work is never pruned, whatever the age.
    incidents_pruned = await asyncio.to_thread(store.prune_closed)
    pushed = await ledger_sync.sync_safely(direction="push")

    changed = any(summary.get(k) for k in ("deduped", "decayed", "pruned"))
    if changed or indexed.get("written") or pulled or incidents_pruned:
        _audit(
            "ledger_hygiene",
            f"{summary} pull={pulled or 'skipped'} index={indexed}",
            "success",
        )
    return web.json_response(
        {
            "summary": summary,
            # Empty strings when sync is unconfigured, which is the common single-user
            # case — the UI shows nothing rather than a scary "not configured".
            "sync": {"pull": pulled, "push": pushed},
            "index": indexed,
            "incidents_pruned": incidents_pruned,
            # ``changed`` drives whether the cron speaks at all, so it must reflect
            # anything a human would want to hear about — including a pull that brought
            # in a teammate's lesson, which changes what the agent knows tomorrow.
            "changed": bool(changed or pulled or indexed.get("written") or incidents_pruned),
        }
    )


def _index_ledger_safely() -> dict[str, int]:
    """Project new ledger entries into the vector store. Never raises.

    Resolves the store here rather than holding one open: an install with no vector
    store (model still downloading, or a deliberately minimal setup) must complete
    hygiene exactly as before. Mirrors ``dispatch._attach_similar_safely``.
    """
    from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_index

    store_obj = None
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.vector_memory import VectorMemoryStore

        store_obj = VectorMemoryStore(embedding_dim=KiroCrewConfig.load().memory.embedding_dim)
        store_obj.init()
        return ledger_index.import_pending(store_obj)
    except Exception:  # noqa: BLE001 — no store, or a broken one, is a supported state
        logger.debug(
            "ops-mission-control: ledger indexing unavailable; hygiene still ran",
            exc_info=True,
        )
        return {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0}
    finally:
        if store_obj is not None:
            try:
                store_obj.close()
            except Exception:  # noqa: BLE001
                logger.debug("ops-mission-control: vector store close failed", exc_info=True)


async def _handle_delete_ledger(request: web.Request) -> web.StreamResponse:
    entry_id = request.query.get("id", "").strip()
    if not entry_id:
        return web.json_response(
            {"error": "id is required", "code": "missing_required_field"}, status=400
        )
    removed = await asyncio.to_thread(ledger.remove, entry_id)
    if not removed:
        # Split from the success return rather than computing the status. A 404 IS an error
        # response and needs a `code` the localized UI can switch on; the previous single
        # `status=200 if removed else 404` produced one body shape for both outcomes, so the
        # error branch could not carry one without also putting it on the success branch.
        return web.json_response(
            {"error": "unknown ledger entry", "ok": False, "removed": False, "code": "not_found"},
            status=404,
        )
    return web.json_response({"ok": True, "removed": True}, status=200)


# ---------------------------------------------------------------------------
# Webhook ingress
# ---------------------------------------------------------------------------


#: Rejections that mean "I don't trust you" rather than "your body is wrong".
#: ``enqueue`` returns these before parsing anything, so they are the only ones that
#: are genuinely authentication/authorization failures.
#: ``Retry-After`` for a 503 from a full webhook spool. One dispatch interval (the
#: manifest's ``dispatch`` cron is ``every: 120``), because that is when the spool next
#: drains — a shorter value invites a hot loop against a queue that cannot have moved.
_SPOOL_RETRY_AFTER_SECS = 120

_WEBHOOK_AUTH_REJECTIONS = frozenset(
    {
        "webhook source is not enabled",
        "no signing secret configured",
        "signature mismatch",
    }
)

#: Rejections that mean "you are trusted, but this body is wrong" — a 400. Listed
#: explicitly rather than inferred as "everything else" so an unclassified reason
#: falls through to 401 instead of being silently reported as a body fault.
_WEBHOOK_PAYLOAD_REJECTIONS = frozenset(
    {
        "malformed JSON",
        "payload must be a JSON object",
        "payload has no title",
    }
)


async def _read_capped(request: web.Request, cap: int) -> bytes | None:
    """Read at most ``cap`` bytes of body; ``None`` if the client sent more.

    Reads ONE byte past the cap so "exactly at the limit" is still accepted while
    anything larger is detected without buffering it — the peak is ``cap + chunk``,
    not whatever the sender chose. ``request.read()`` cannot do this: it returns only
    after the whole body is in memory, and these routes sit on the shared gateway
    application whose ``client_max_size`` is 60 MiB.

    ``Content-Length`` is checked first when present, so an honest oversized delivery
    is refused before a single chunk is read; a lying or absent header is caught by the
    streaming count, which is the authority.
    """
    declared = request.content_length
    if declared is not None and declared > cap:
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await request.content.read(webhook_mod.READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _webhook_reject_status(detail: str) -> int:
    """Map a rejection reason to its HTTP status.

    Everything used to return 401, including "malformed JSON" and "payload has no
    title" — which are *authenticated* requests with a bad body. A sender debugging
    a payload was told "Unauthorized" and would go re-check credentials that were
    fine, while a real signature failure looked identical to a typo. Payload faults
    are 400; only the trust checks are 401. Defaults to 401 for an unrecognized
    reason, so a newly-added rejection is treated as auth-ish rather than
    accidentally advertised as "your request was fine".
    """
    if detail in _WEBHOOK_AUTH_REJECTIONS:
        return 401
    if detail == webhook_mod.REJECT_BODY_TOO_LARGE:
        return 413
    if detail == webhook_mod.REJECT_SPOOL_FULL:
        # 503, not 4xx: the delivery was well-formed and trusted, WE are the ones who cannot
        # take it right now. That distinction is what makes it retriable — Alertmanager and
        # friends re-deliver on a 5xx, so a full spool becomes a delay rather than a lost page.
        return 503
    if detail in _WEBHOOK_PAYLOAD_REJECTIONS:
        return 400
    # Unrecognized: fail toward 401 rather than 400. A new rejection reason added to
    # ``enqueue`` without classifying it here is more likely to be a trust check than
    # a body complaint, and telling a caller "your request was fine, just malformed"
    # about a refusal we do not understand is the wrong default.
    return 401


async def _handle_webhook(request: web.Request) -> web.StreamResponse:
    """Accept a signed inbound signal.

    Fail-closed on the HMAC: an unsigned or mis-signed delivery is rejected, so
    enabling this adapter cannot open an unauthenticated path that manufactures
    work on the board. Note the check ORDER in ``webhook.enqueue`` — enabled →
    secret → size → signature → parse. Nothing unauthenticated is ever parsed, and
    an oversized body is refused before it is hashed.

    The body is read INCREMENTALLY, and that is a memory bound rather than a
    nicety. ``enqueue``'s ``len(raw_body) > MAX_BODY_BYTES`` check can only run on a
    body already in memory, and these routes register on the shared gateway
    application whose ``client_max_size`` is 60 MiB (it carries file uploads), so a
    plain ``await request.read()`` buffered up to 60 MiB per concurrent delivery
    before refusing 256 KiB of it. Stopping one byte past the cap keeps the refusal
    O(cap) instead of O(what the client chose to send). Found in review (GPT 5.6).
    """
    raw = await _read_capped(request, webhook_mod.MAX_BODY_BYTES)
    if raw is None:
        # Reuses ``enqueue``'s own reason string so the body and the audit line read the
        # same whether the cap is hit here or inside ``enqueue``. The status is the
        # LITERAL 413 rather than `_webhook_reject_status(detail)`: this branch has exactly
        # one reason, so the mapping call would compute a statically-known value — and the
        # error-code contract gate ratchets computed statuses precisely because hoisting a
        # status into an expression is how a missing `code` escapes review.
        detail = webhook_mod.REJECT_BODY_TOO_LARGE
        _audit("webhook_ingest", detail, "rejected", error=detail)
        return web.json_response({"error": detail, "code": "webhook_rejected"}, status=413)
    signature = request.headers.get(webhook_mod.SIGNATURE_HEADER, "")
    accepted, detail = await asyncio.to_thread(webhook_mod.enqueue, raw, signature)
    _audit(
        "webhook_ingest",
        detail,
        "success" if accepted else "rejected",
        error="" if accepted else detail,
    )
    if not accepted:
        status = _webhook_reject_status(detail)
        headers = {}
        if status == 503:
            # Tell the sender WHEN, so a retry does not hot-loop against a full spool. One
            # dispatch interval is the honest answer: that is when the spool next drains.
            headers["Retry-After"] = str(_SPOOL_RETRY_AFTER_SECS)
        return web.json_response(
            {"error": detail, "code": "webhook_rejected"}, status=status, headers=headers
        )
    return web.json_response({"ok": True, "signal": detail, "queued": webhook_mod.queue_depth()})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes(app: web.Application) -> None:
    """Register Ops Mission Control's routes on the gateway application."""
    add = app.router
    add.add_get(f"{_BASE}/state", _require_enabled(_handle_state))
    add.add_get(f"{_BASE}/incidents", _require_enabled(_handle_incidents))
    add.add_get(f"{_BASE}/incident", _require_enabled(_handle_incident))
    add.add_post(f"{_BASE}/incident/transition", _require_enabled(_handle_transition))
    add.add_post(f"{_BASE}/incident/claim", _require_enabled(_handle_claim))
    add.add_post(f"{_BASE}/incident/action", _require_enabled(_handle_action))
    # The propose loop: draft -> queue -> decide. Separate routes because they have
    # different authority: proposing changes nothing, deciding may write.
    add.add_post(f"{_BASE}/incident/propose", _require_enabled(_handle_propose))
    add.add_post(f"{_BASE}/incident/proposal/decide", _require_enabled(_handle_decide_proposal))
    add.add_get(f"{_BASE}/proposals", _require_enabled(_handle_proposals))
    add.add_post(f"{_BASE}/dispatch", _require_enabled(_handle_dispatch))
    add.add_get(f"{_BASE}/signals", _require_enabled(_handle_signals))
    add.add_get(f"{_BASE}/handover", _require_enabled(_handle_handover))
    add.add_get(f"{_BASE}/providers", _require_enabled(_handle_providers))
    add.add_put(
        f"{_BASE}/providers/{{provider_id}}/config",
        _require_enabled(_handle_put_provider_config),
    )
    add.add_put(f"{_BASE}/providers/{{provider_id}}/secret", _require_enabled(_handle_put_secret))
    add.add_delete(
        f"{_BASE}/providers/{{provider_id}}/secret", _require_enabled(_handle_delete_secret)
    )
    add.add_put(f"{_BASE}/settings", _require_enabled(_handle_put_settings))
    add.add_get(f"{_BASE}/rotation", _require_enabled(_handle_rotation))
    add.add_post(f"{_BASE}/rotation/arm", _require_enabled(_handle_rotation_arm))
    add.add_get(f"{_BASE}/ledger", _require_enabled(_handle_get_ledger))
    add.add_get(f"{_BASE}/ledger/contradictions", _require_enabled(_handle_ledger_contradictions))
    add.add_post(f"{_BASE}/ledger", _require_enabled(_handle_post_ledger))
    add.add_post(f"{_BASE}/ledger/hygiene", _require_enabled(_handle_ledger_hygiene))
    add.add_delete(f"{_BASE}/ledger", _require_enabled(_handle_delete_ledger))
    add.add_post(f"{_BASE}/webhook", _require_enabled(_handle_webhook))

    # Warm the provider registry HERE, at gateway startup, not on the first request.
    # `get_registry()` populates lazily: entry-point enumeration, signed-plugin admission
    # I/O and companion import all run on the first call. Every producer of that first call
    # is a request handler (`_handle_signals`, `_handle_claim`, …), so the discovery cost
    # landed on the event loop — the gateway's first `/signals` poll stalled the heartbeat
    # and every other task for the length of a filesystem plugin scan. `register_routes`
    # runs synchronously before the loop serves anything, so paying it here is free.
    # Found in review.
    #
    # Fail-open: this app is default-disabled and an install that never enables it must not
    # crash gateway startup on a discovery fault (`get_registry` already swallows companion
    # errors; this guards the enumeration around it).
    try:
        get_registry()
    except Exception:  # noqa: BLE001 — a discovery fault must not break gateway startup
        logger.exception("ops-mission-control: registry warm-up failed; will retry lazily")

    logger.info("ops-mission-control: routes registered")
