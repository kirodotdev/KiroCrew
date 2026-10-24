"""What a turn in flight exchanges with the dashboard: approvals, directives, keepalives.

``GET /api/approvals`` and ``POST /api/approvals/{id}/{action}`` (pending tool approvals,
judged per resource for an app caller), ``POST /api/session-directive`` (a directive tool's
payload, carried out of band) and ``POST /api/session-keepalive`` with the
``wait`` countdown it services. The directive and keepalive routes verify and scope their own
caller. New callbacks a running turn makes go here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web

from kiro_crew import session_directive
from kiro_crew.dashboard import directive_queue
from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes, internal_memory_scope
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import (
    _STATE_APPROVALS_NOT_APP_VISIBLE,
    _app_not_found,
    _audit_app_denial,
    _sel,
)
from kiro_crew.dashboard.interaction_coordinator import _slot_decision
from kiro_crew.dashboard.state import DashboardState

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


# ── Approvals ──


async def api_approvals(request: web.Request) -> web.Response:
    """GET /api/approvals — list pending tool approvals.

    The list holds only STATE-level approvals (cron, autonudge, subagent,
    taskrunner), and an app token never resolves one of those -- the same rule
    ``POST /api/chat/slots/{slot}/approve`` applies. So an app caller is shown
    none of them rather than every pending request on the instance.
    """
    state: DashboardState = request.app["state"]
    request_app = str(request.get("app") or "")
    if request_app:
        _audit_app_denial(
            request_app, "approval_list", "approvals=*", _STATE_APPROVALS_NOT_APP_VISIBLE
        )
        return web.json_response([])
    return web.json_response(list(state._pending_approvals.values()))


async def _resolve_app_approval(
    request: web.Request, state: DashboardState, request_app: str, approval_id: str, action: str
) -> web.Response:
    """Resolve *approval_id* for an app caller, or refuse it.

    The slot approve route's rule, in its order: ``deny_session_approval_caller``
    first, so an app without ``permissions.sessionApproval`` gets the same 403
    there and here, even for its own slot; then ``_app_may_send_to_slot`` on the
    slot whose future owns the id. A state-level approval is never resolvable by
    an app, so the coordinator target and the state registry are not consulted.

    ACP request ids are connection-scoped and recur across slots, so the id must
    name exactly ONE pending request the app may control. Two or more is refused
    rather than guessed: the slot route, which names the slot, is the way to
    decide one of them. The candidates are the (slot, future) pairs live BEFORE
    the awaited permission reads, and only the judged future itself is resolved:
    a request settled meanwhile is never replaced by a same-id sibling, on
    another slot or on the same one. Every other refusal and a missing id return
    the same 404 body, so an app cannot tell "not yours" from "does not exist".
    A resolution is recorded in the SEL under the app's name, as the slot route
    records it.
    """
    # Imported here: chat_handlers imports this package at module scope.
    from kiro_crew.dashboard.chat_handlers import (
        _app_may_send_to_slot,
        _pending_elicitation_ids,
        deny_session_approval_caller,
    )

    denied = await deny_session_approval_caller(request, "approval_resolve")
    if denied is not None:
        return denied
    live: list[tuple[Any, asyncio.Future[str]]] = []
    if "origin" not in request.query:
        # Snapshot before any await: the permission reads below yield the loop.
        # An MCP elicitation shares the slot's registry so a Stop and the Board
        # see it, but it is the SERVER's question to a person, not a tool
        # approval this grant covers: its future is not a candidate here, and
        # the id reads as "does not exist" like every other refusal.
        live = [
            (slot, future)
            for slot in list(state._slots.values())
            if (future := slot._approval_futures.get(approval_id))
            and not future.done()
            and approval_id not in _pending_elicitation_ids(slot)
        ]
    controllable = [pair for pair in live if await _app_may_send_to_slot(request_app, pair[0])]
    if len(controllable) == 1:
        slot, future = controllable[0]
        approved, rejected_once = action == "approve", action == "reject_once"
        if state.resolve_slot_approval(
            slot, approval_id, approved, rejected_once=rejected_once, expected_future=future
        ):
            # The slot route's record shape, so either route's decision names the app.
            try:
                _sel().log_api_access(
                    caller=f"app:{request_app}",
                    operation=f"tool_approval:{action}",
                    outcome=_slot_decision(approved, rejected_once),
                    resources=approval_id,
                )
            except Exception:  # pragma: no cover - audit is best-effort
                logger.warning("SEL audit failed for approval %s", approval_id, exc_info=True)
            return web.json_response({"ok": True})
    reason = (
        "approval id is pending on more than one session this app may control"
        if len(controllable) > 1
        else "no pending approval on a session this app may control"
    )
    return _app_not_found(request_app, "approval_resolve", f"approval={approval_id}", reason)


async def api_approval_resolve(request: web.Request) -> web.Response:
    """POST /api/approvals/{id}/{action} — approve, reject, or reject_once."""
    state: DashboardState = request.app["state"]
    approval_id = request.match_info["id"]
    action = request.match_info["action"]
    if action not in ("approve", "reject", "reject_once"):
        return web.json_response({"error": "invalid action"}, status=400)
    request_app = str(request.get("app") or "")
    if request_app:
        return await _resolve_app_approval(request, state, request_app, approval_id, action)
    if "origin" in request.query:
        # Dynamic Dashboard echoes the inventory record's origin, exact slot and
        # instance. Do not fall through to native futures when that displayed
        # record expires. The instance binds the decision to the request the card
        # showed: the request id is minted by the caller and can recur in the same
        # slot, so a card left up from an expired record would otherwise resolve
        # the request that replaced it.
        slot = request.query.get("slot", "")
        instance = request.query.get("instance", "")
        if request.query["origin"] != "coordinator" or not slot or not instance:
            return web.json_response(
                {"error": "invalid approval target", "code": "invalid_approval_target"}, status=400
            )
        pending = state._pending_approvals.get(approval_id)
        # No await between checking the record and resolving its state-only future.
        ok = bool(
            pending
            and pending.get("slot") == slot
            and pending.get("instance") == instance
            and state.resolve_state_approval(approval_id, action == "approve")
        )
    else:
        # Imported here: chat_handlers imports this package at module scope.
        from kiro_crew.dashboard.chat_handlers import bare_approval_decide_allowed

        if not bare_approval_decide_allowed(request):
            # Only the dashboard owner's browser session may decide by id alone;
            # any other caller names the request it means with the target above.
            return web.json_response(
                {
                    "error": "an owner-bound approval target is required",
                    "code": "approval_target_required",
                },
                status=404,
            )
        ok = state.resolve_approval(
            approval_id, action == "approve", rejected_once=action == "reject_once"
        )
    if not ok:
        return web.json_response({"error": "not found or expired"}, status=404)
    return web.json_response({"ok": True})


async def api_session_directive(request: web.Request) -> web.Response:
    """POST /api/session-directive — park a validated session directive.

    The provider-neutral leg of the session-directive protocol. Its only caller is
    a Kiro Crew directive tool (``mcp_tools.control._emit_directive``), which has
    already validated the payload; this route carries that payload to the gateway
    OUT OF BAND so the turn's consumer can apply it without having to trust the
    model-visible marker in the tool result. See
    :mod:`kiro_crew.dashboard.directive_queue` for why that is not weaker than the
    marker gate it backs up.

    Authenticated via X-Internal-Secret, and the session is selected by the
    X-Session-Key header every MCP subprocess already sends — the SAME shape as
    ``api_session_keepalive``. The header is not taken on faith: on the unix
    socket ``token_auth`` kernel-verifies the peer and denies 403 when it resolves
    to a session key other than the declared one, which is the check that stops a
    caller parking a directive against somebody else's session.

    A call that derives no directive (unknown tool, validation refusal) is a 400,
    not a silent drop: the only legitimate callers are Kiro Crew's own directive
    tools, so a request that does not derive did not come from one.
    """
    _, refusal = await internal_memory_scope(
        request, "api_session_directive", claimed_session=request.headers.get("X-Session-Key", "")
    )
    if refusal is not None:
        return refusal
    # Re-assert the caller's locality BEFORE the header is read. The route is in
    # server.py's strict allowlist, but a ``local_only=False`` deployment
    # reclassifies strict paths as MIXED — so the auth middleware also admits a
    # cookie/token-authenticated browser caller here, and such a caller picks its
    # own ``X-Session-Key``. That is somebody else's session: a parked record is
    # applied verbatim by the next consumer frame in the named turn, so accepting
    # it would hand a remote cookie holder a cross-session mutation (arm a loop,
    # retarget a project). ``internal_auth`` is set only after a constant-time
    # ``X-Internal-Secret`` match on a same-machine transport; ``peer_verified``
    # only after the kernel-attested AF_UNIX peer resolved to the DECLARED key —
    # either one is positive proof of a local caller, and a cookie carries
    # neither. Same predicate as ``handlers/updates_step_up.py``'s host-locality gate.
    if not (request.get("internal_auth") or request.get("peer_verified")):
        logger.warning(
            "session-directive REFUSED (not_local_caller): neither internal_auth nor "
            "peer_verified was set, so the caller could not be proven local. "
            "Nothing was parked."
        )
        return web.json_response(
            {"error": "local caller required", "code": "not_local_caller"},
            status=403,
        )
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        logger.warning(
            "session-directive REFUSED (session_key_required): the caller sent no "
            "X-Session-Key, so no session could be named and nothing was parked. On a "
            "backend that emits no _meta.kiro identity this is fatal: the marker cannot "
            "be trusted either, so the directive is dropped entirely."
        )
        return web.json_response(
            {"error": "X-Session-Key required", "code": "session_key_required"},
            status=400,
        )
    body: dict = {}
    try:
        if request.can_read_body:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        return web.json_response(
            {"error": "malformed JSON body", "code": "invalid_body"}, status=400
        )
    # The body names the CALL -- the directive tool's name and the raw
    # ``tools/call`` arguments the MCP stub served -- and nothing else. The
    # payload is DERIVED here by re-running that tool on those arguments
    # (``mcp_core.derive_directive``), and the claim key is computed here from the
    # same pair. A caller who can reach this route therefore controls only what
    # the named session's own call would have produced: it cannot pair payload X
    # with the digest of call Y, which a caller-supplied (args, input_digest) body
    # allowed and which is exactly the substitution the two-channel design exists
    # to prevent.
    # Lazy on purpose: mcp_core is the MCP server module and imports every tool
    # handler; no dashboard module loads it at import time, and this route is
    # the only one that needs it.
    from kiro_crew import mcp_core, session_environment

    tool = str(body.get("tool") or "").strip()
    raw_args = body.get("raw_args")
    if not isinstance(raw_args, dict):
        raw_args = {}
    if not tool and "kind" in body:
        # The body shape a Kiro Crew MCP server from BEFORE the call-input
        # protocol sends: the directive's ``kind``/``args`` rather than the call.
        # That server is running older code than this gateway -- a pooled MCP
        # backend that outlived a code change -- and every directive it emits
        # will land here until it is replaced. Name that, rather than the
        # generic "not derivable", because the generic wording sent an operator
        # to the directive tools when the fault was a stale process.
        logger.warning(
            "session-directive REFUSED (stale_mcp_backend) for session_key=%r: the "
            "MCP server sent a pre-call-input body (kind=%r) -- it is running OLDER "
            "code than this gateway. A pooled backend survived a code change; run "
            "`kirocrew restart` (which replaces the MCP gateway daemon too) or "
            "`kirocrew doctor` to see the daemon's code revision. Nothing was parked.",
            session_key,
            body.get("kind"),
        )
        return web.json_response(
            {
                "error": "MCP server runs older code than the gateway; restart it",
                "code": "stale_mcp_backend",
            },
            status=400,
        )
    if tool in session_environment.LOCAL_ONLY_CORE_TOOLS:
        # The replay's remote-workspace guard would read the choices store on this
        # loop; look it up in a thread and hand the replay the answer.
        remote = await asyncio.to_thread(session_environment.session_runs_remotely, session_key)
        with session_environment.known_placement(session_key, remote):
            derived = mcp_core.derive_directive(tool, raw_args, session_key)
    else:
        derived = mcp_core.derive_directive(tool, raw_args, session_key)
    if derived is None:
        logger.warning(
            "session-directive REFUSED (not_derivable) for session_key=%r tool=%r: "
            "re-running the tool on the reported arguments published no directive "
            "(unknown tool, validation refusal, or handler error). Nothing was parked.",
            session_key,
            tool,
        )
        return web.json_response(
            {"error": "directive could not be derived from the call", "code": "not_derivable"},
            status=400,
        )
    kind, args = derived
    input_digest = session_directive.call_input_digest(tool, raw_args)
    try:
        record_id = directive_queue.publish(session_key, kind, args, input_digest)
    except ValueError as exc:
        logger.warning(
            "session-directive REFUSED (invalid_directive) for session_key=%r kind=%r: "
            "%s. Nothing was parked.",
            session_key,
            kind,
            exc,
        )
        return web.json_response({"error": str(exc), "code": "invalid_directive"}, status=400)
    return web.json_response({"ok": True, "id": record_id})


async def api_session_keepalive(request: web.Request) -> web.Response:
    """POST /api/session-keepalive — refresh activity timestamp on the
    session's provider so idle-detection/stale-checks don't SIGTERM a
    session that's intentionally blocking in a long-running MCP tool
    (e.g. the `wait` tool).

    Authenticated via X-Internal-Secret; session is selected via the
    X-Session-Key header that all MCP subprocesses already send.

    Doubles as the sleeping `wait` tool's only inbound channel. When the body
    names a ``wait_id`` the reply may carry ``end_wait: <wait_id>``, which the
    tool treats as "return early, keep the turn". The body is optional and every
    field in it is advisory: a caller that sends ``{}`` gets the original
    touch-only behaviour.
    """
    _, refusal = await internal_memory_scope(
        request, "api_session_keepalive", claimed_session=request.headers.get("X-Session-Key", "")
    )
    if refusal is not None:
        return refusal
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response({"error": "X-Session-Key required"}, status=400)
    provider = state.sessions.get_provider(session_key)
    if provider is None:
        return web.json_response({"error": "session not found"}, status=404)
    body: dict = {}
    try:
        if request.can_read_body:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        # A malformed body must never cost the session its keepalive — that is
        # the half of this route that keeps the watchdog from killing the ACP
        # subprocess mid-wait.
        body = {}
    try:
        provider.touch_activity()
    except Exception as exc:
        logger.debug("touch_activity failed for %s: %s", session_key, exc)
        return web.json_response({"error": "touch failed"}, status=500)
    # Also advance the session's own last_used clock. touch_activity() only
    # refreshes the ACP runtime's activity timestamp, which feeds
    # is_responsive()/the stall watchdog, while the periodic idle sweep reads
    # ``last_used`` instead. The sweep skips any session whose turn permit is
    # held, and a tool reaching this route runs inside such a turn, so the idle
    # verdict for a session blocking in a long `wait` is settled by that guard
    # rather than by this touch. What the touch buys is the boundary: it leaves
    # ``last_used`` pointing at the end of the turn's work instead of its start,
    # so once the permit drops the sweep measures idleness from when the session
    # went quiet.
    try:
        touched = getattr(state.sessions, "touch", None)
        if callable(touched):
            touched(session_key)
    except Exception:
        logger.debug("last_used touch failed for %s", session_key, exc_info=True)
    reply: dict = {"ok": True}
    wait_id = str(body.get("wait_id") or "").strip()[:64]
    if wait_id:
        _service_wait_ping(state, session_key, wait_id, body, reply, provider)
    return web.json_response(reply)


def _wait_end_reason(slot, wait_id: str, provider: Any) -> str | None:
    """Why this sleep should return early, or None to keep sleeping.

    Exactly two reasons, and the narrowness is the design:

    ``"user"``
        The End-wait button parked an explicit request naming this ``wait_id``,
        or another session parked the same request through ``session_end_wait``
        (``slot._end_wait_by`` then names it).

    ``"steer"``
        A mid-turn steer reached the backend AFTER this sleep began. kiro-cli
        can only inject a steer at a model-inference boundary and an in-flight
        tool call is the absence of one, so without this the user's correction
        sits in the backend's steer queue until the sleep elapses — up to the
        tool's 1800s ceiling — while the agent sleeps through it.

    "After this sleep began" is decided by comparing the provider's steer stamp
    against the reading taken when this sleep was minted, so the handler reads
    no clock of its own: the only two values ever compared are two readings of
    the same monotonic source. That is deliberate — a wall-clock stamp on one
    side and a monotonic one on the other is how a suspend silently reorders
    the comparison.

    Re-taking the baseline at every mint is also what makes the reason fire
    once. A steer the backend has accepted but not yet injected stays newer
    than nothing at all, so without a per-sleep baseline it would end sleep
    after sleep for the rest of the turn and hand the model a `wait` that
    returns instantly.

    Not extended to the other long block on this route. ``spawn_sub_agents``
    can wait 7200s on live sub-agents and pings the same endpoint, but sends no
    ``wait_id`` and so never reaches this decision — an exclusion worth keeping
    deliberately: ending a sleep discards nothing, while cutting a sub-agent
    collection short orphans work that keeps running with nobody left to read
    its results.
    """
    if slot._end_wait_request and slot._end_wait_request == wait_id:
        return "user"
    tracked = slot._wait_state or {}
    if tracked.get("wait_id") != wait_id:
        # Not the sleep this slot is tracking (contested identity, stale ping).
        return None
    steered_at = _provider_steer_stamp(provider)
    return "steer" if steered_at > slot._wait_steer_baseline else None


def _provider_steer_stamp(provider: Any) -> float:
    """Monotonic time of the session's last steer, 0.0 when never steered or
    when the provider does not expose one (a non-kiro backend, a test double).

    Read through ``getattr`` rather than an interface method because this route
    is reached by every backend, and a missing stamp must read as "no steer" —
    the direction that keeps sleeping — rather than raise on the keepalive that
    stops the watchdog killing the session mid-sleep.
    """
    try:
        return float(getattr(provider, "last_steer_monotonic", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _service_wait_ping(
    state: DashboardState,
    session_key: str,
    wait_id: str,
    body: dict,
    reply: dict,
    provider: Any = None,
) -> None:
    """Track an in-flight `wait` sleep and hand back any early-end request.

    Mutates ``reply`` in place, adding ``end_wait`` when the sleep should return
    early (see :func:`_wait_end_reason`). Silent no-op when the calling session
    has no dashboard tab (a Slack/cron session can call `wait` too — it just has
    nothing to render a countdown on).
    """
    # Function-local, as it was in the one module.
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    slot_name = dashboard_slot_key(session_key)
    slot = state.get_slot(slot_name) if slot_name else None
    if slot is None:
        return
    now = time.time()
    # How stale the incumbent's last ping must be before its sleep is presumed
    # gone. 2.5 intervals tolerates one dropped ping plus scheduling jitter
    # without tolerating a sleep that is simply still running.
    try:
        interval = float(body.get("interval") or 5.0)
    except (TypeError, ValueError):
        interval = 5.0
    window = max(2.0, min(60.0, interval) * 2.5)
    if body.get("wait_done"):
        # Only the wait that owns the state may retire it, so a late final ping
        # from a previous sleep cannot blank the countdown of the current one.
        if slot._wait_state and slot._wait_state.get("wait_id") == wait_id:
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_last_ping = 0.0
            slot._wait_steer_baseline = 0.0
            state.push_slots_update()
        return
    # ── Ambiguous-identity guard ──
    # `_resolve_session_key()` answers per RUNTIME, not per ACP session: with the
    # MCP gateway disabled (the default) KIROCREW_SESSION_KEY is unset and one MCP
    # process serves the whole runtime, so a subagent's `wait` and its parent's
    # resolve to the SAME session key and land on this one slot. Taking the newer
    # wait over would then attribute one sleep's countdown to the other's pill,
    # and worse, hand the user's End-wait click to whichever sleep polled next.
    #
    # The ping doubles as a heartbeat, which makes the ambiguity detectable: a
    # second wait_id arriving while the incumbent is still pinging means two
    # sleeps genuinely share this slot. There is no way to tell which one the
    # user is looking at, so track neither, and stay that way for the rest of the
    # turn (see the latch below -- a self-expiring window flapped the hole back
    # open). Deliberately a containment, not a cure: the cure is per-session
    # identity, which this cannot synthesize.
    # Tracked in https://github.com/kirodotdev/KiroCrew/issues/2347, which also
    # lists this guard among the things to delete once identity is fixed.
    if slot._wait_contested:
        # Latched for the REST OF THE TURN, not for a fixed window. An expiring
        # window reopened the hole it was built to close: both sleeps keep
        # pinging, so on expiry whichever pinged first re-minted state, and for
        # up to one ping interval that wait's id and deadline were published and
        # painted onto the OTHER one's pill -- with a live button that would end
        # the wrong sleep. Re-detection closed it again a ping later, so the
        # attribution flapped open every window instead of staying shut.
        if slot._wait_state is not None or slot._end_wait_request is not None:
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_last_ping = 0.0
            slot._wait_steer_baseline = 0.0
            state.push_slots_update()
        return
    prev = slot._wait_state
    if prev and prev.get("wait_id") != wait_id:
        if now - slot._wait_last_ping < window:
            slot._wait_contested = True
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_last_ping = 0.0
            slot._wait_steer_baseline = 0.0
            logger.info("two concurrent waits share session %s; countdown suppressed", session_key)
            state.push_slots_update()
            return
        # The incumbent stopped pinging: its sleep is over (a missed wait_done, a
        # killed MCP process, a hard stop). Safe to hand the slot to this one.
    if not prev or prev.get("wait_id") != wait_id:
        try:
            remaining = max(0, min(1800, body_int(body.get("remaining") or 0)))
            total = max(0, min(1800, body_int(body.get("seconds") or 0)))
        except (TypeError, ValueError):
            remaining, total = 0, 0
        slot._wait_state = {
            "wait_id": wait_id,
            "seconds": total,
            # Absolute deadline on the DASHBOARD's clock, derived once on first
            # sight from the tool's own remaining budget. Two reasons not to
            # recompute it every ping: the countdown would jitter by one
            # round-trip each tick, and the tool's monotonic clock has no shared
            # epoch it could send instead.
            "deadline_ts": now + remaining,
        }
        # A brand-new wait cannot inherit an end request aimed at an older one.
        slot._end_wait_request = None
        # Baseline for the steer reason: re-read on every mint, so only a steer
        # that lands after THIS sleep began can end it. No clock read here —
        # the baseline and the later comparison are two readings of the same
        # provider stamp.
        slot._wait_steer_baseline = _provider_steer_stamp(provider)
        slot._wait_last_ping = now
        state.push_slots_update()
    else:
        # Heartbeat only. Held OFF the wire payload so the deadline the browser
        # counts down against stays byte-identical between pushes.
        slot._wait_last_ping = now
    reason = _wait_end_reason(slot, wait_id, provider)
    if reason is not None:
        ended_by = getattr(slot, "_end_wait_by", "") if reason == "user" else ""
        # Consume exactly once. Leaving it set would make the NEXT wait in this
        # session return instantly, which is the failure mode a session-scoped
        # boolean flag would have had.
        slot._end_wait_request = None
        slot._wait_state = None
        slot._wait_last_ping = 0.0
        slot._wait_steer_baseline = 0.0
        reply["end_wait"] = wait_id
        # Advisory, for the tool's result text only: a tool that predates this
        # field honours ``end_wait`` alone exactly as before.
        if ended_by:
            reply["end_wait_by"] = ended_by
        logger.info("wait ending early for %s (reason=%s)", session_key, reason)
        state.push_slots_update()


# Every ``api_session*`` handler is an owner surface, so a private member's
# internal call is refused before it runs (audit label = handler name).
# The ones named below verify and scope their own caller instead.
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset({"api_session_directive", "api_session_keepalive"}),
)
