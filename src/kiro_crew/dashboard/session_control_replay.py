"""One answer per ``request_id`` for the session-control routes that open or deliver.

Those are send, create (both also with ``crew``), fork, broadcast, continue and
set-note. A slow gateway can take longer than the MCP client waits. The client then
reports a lost reply (``transport_error``) while the route goes on to deliver the
message or open the session, so a caller that retries sends the message twice or
leaves an empty session behind. The tools for these routes therefore
mint one ``request_id`` per call and, when the reply is lost, send the SAME request
again. :func:`once` makes that second request answer with the first one's reply
instead of doing the work again:

* the first request with an id runs the route and its reply is kept;
* a request with the same id from the same caller while the first is still running
  waits for that reply, and one after it finished gets the kept reply, once the
  caller passes the route's caller-side gate again (:func:`_readmit`);
* the same id with a different body is refused ``request_id_reused``;
* a request with no id runs as before.

Every JSON reply to a request that carried an id echoes it as ``request_id`` and
says how it was produced in :data:`REPLAY_FIELD`: ``first`` (this request ran and
its reply is kept), ``unsaved`` (it ran, but no reply could be kept) or ``replayed``
(answered from an earlier request with the same id). Only ``replayed`` proves a
resend did not run the work again, so the client warns about a possible double for
any other answer to a resend: a full table, a gateway restart that lost the kept
replies, or an older gateway that sends neither field.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _read_session_key

logger = logging.getLogger(__name__)

#: The body field the MCP tools put their per-call id in.
REQUEST_ID_FIELD = "request_id"

#: The body field carrying which post of a tool call this is (0 for the first), and
#: the reply field that names the post a ``replayed`` answer came from. A replay of a
#: later post proves nothing about the earlier ones, whose replies were also lost.
ATTEMPT_FIELD = "request_attempt"
REPLAY_ATTEMPT_FIELD = "replay_attempt"

#: The reply field that says how a keyed reply was produced, and its three values.
REPLAY_FIELD = "replay"
REPLAY_FIRST = "first"
REPLAY_UNSAVED = "unsaved"
REPLAY_REPLAYED = "replayed"

#: The id is the tool's ``uuid4().hex``: 32 lowercase hex characters.
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")

#: Seconds a finished reply stays available to a resend. Well past the client's
#: whole retry window (``mcp_session_once.LOST_REPLY_ATTEMPTS`` posts of up to 30 s
#: each; a broadcast's last resend starts about 520 s after its first post, which
#: the gateway finishes within its own 250 s bound), so a resend never finds its
#: reply already gone.
REPLAY_TTL_SECS = 600.0

#: Most replies kept at once, running or finished, across every caller.
MAX_KEPT_REPLIES = 1024

#: Most replies kept at once for one calling session, so one caller in a loop cannot
#: use up the room every other caller's resends depend on.
MAX_KEPT_REPLIES_PER_CALLER = 128

#: Past either limit a request with a new id still runs, just without a kept reply:
#: memory stays bounded, no kept reply leaves its window early, and no caller is
#: refused because of another caller's traffic. Its reply says ``unsaved``, and a
#: resend of it is not ``replayed``, so the tool warns that the work may have run twice.

#: Longest reply text kept for a resend. A route's refusal can echo caller-chosen
#: text, so without this bound the kept replies could hold up to the request body
#: limit each. A longer reply is answered to its own request in full and kept as
#: :data:`_REPLY_TOO_LARGE`, which still stops a resend from running the work again.
MAX_KEPT_REPLY_CHARS = 64 * 1024

#: The code a resend gets when the first request raised instead of answering. The
#: work may have happened before the failure, so the resend must not run it again.
OUTCOME_UNKNOWN_CODE = "replay_outcome_unknown"

#: The code a resend gets when the first reply was too long to keep.
REPLY_TOO_LARGE_CODE = "replay_reply_too_large"

_FIRST_ATTEMPT_FAILED = {
    "error": (
        "the first attempt of this request failed on the gateway, so whether it "
        "took effect is unknown; it was not run again"
    ),
    "code": OUTCOME_UNKNOWN_CODE,
}

_REPLY_TOO_LARGE = {
    "error": (
        "the first attempt of this request finished, but its reply was too long to "
        "keep, so its outcome cannot be shown again; it was not run again"
    ),
    "code": REPLY_TOO_LARGE_CODE,
}


@dataclass
class _Kept:
    fingerprint: str
    reply: asyncio.Future[tuple[int, str]]
    attempt: int
    finished_at: float | None = field(default=None)


_kept: OrderedDict[tuple[str, str, str], _Kept] = OrderedDict()


def reset_for_tests() -> None:
    _kept.clear()


def _fingerprint(body: dict[str, Any]) -> str:
    rest = {k: v for k, v in body.items() if k not in (REQUEST_ID_FIELD, ATTEMPT_FIELD)}
    return hashlib.sha256(json.dumps(rest, sort_keys=True, default=str).encode()).hexdigest()


def _forget_expired(now: float) -> None:
    # Only age removes a kept reply: dropping one inside its window, finished or
    # running, would let its resend run the work again. Room comes from refusing a
    # new id at capacity instead (``once``).
    for key, kept in list(_kept.items()):
        if kept.finished_at is not None and now - kept.finished_at > REPLAY_TTL_SECS:
            del _kept[key]


def _stamped(
    status: int, text: str, request_id: str, replay: str, attempt: int | None = None
) -> tuple[int, str]:
    """*text* with the id and how the reply was produced, when it is a JSON object.

    *attempt* is the post a ``replayed`` answer came from.
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return status, text
    if isinstance(payload, dict):
        stamp: dict[str, Any] = {REQUEST_ID_FIELD: request_id, REPLAY_FIELD: replay}
        if attempt is not None:
            stamp[REPLAY_ATTEMPT_FIELD] = attempt
        text = json.dumps({**payload, **stamp})
    return status, text


def _attempt_of(body: dict[str, Any]) -> int:
    """Which post of a tool call *body* is; an absent or malformed value counts as a resend."""
    attempt = body.get(ATTEMPT_FIELD)
    return (
        attempt
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0
        else 1
    )


def _json_reply(status: int, text: str) -> web.Response:
    return web.Response(status=status, text=text, content_type="application/json")


async def _readmit(request: web.Request, op: str, body: dict[str, Any]) -> web.Response | None:
    """The refusal for a caller the route's caller gate now refuses, before a replay.

    A kept reply carries what the route read for the caller (a session's title). The
    caller can change between the two requests, such as gaining a channel mirror, so
    a replay passes the route's caller-side gate again, which also records the
    audience the reply is admitted under. A ``crew`` call (or a create the hub places
    on a crew) passes the crew caller gate its route runs instead. A refused replay
    carries no ``replay`` field, so the tool reports the first request's outcome as
    unknown.
    """
    # Deferred: the route modules import this one.
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew_routes
    from kiro_crew.dashboard.handlers.session_control import _refusal

    if crew_routes.has_crew(body) or (
        op == "create" and "work_item" in body and await crew_routes.placement_enabled()
    ):
        try:
            await crew_routes.authorize_remote_dispatch(request, op)
        except crew_routes.CrewCallError as exc:
            return crew_routes._response(exc)
        return None
    caller_session_key = _read_session_key(request)
    deny = sc._deny_factory(caller_session_key=caller_session_key, operation=op, target="")
    # Warmed first, so the identity gate's enabled check reads no config on the loop.
    await sc.prewarm_enabled_check()
    state = request.app["state"]
    try:
        caller_key = sc.refuse_caller_identity(
            state, caller_session_key=caller_session_key, deny=deny
        )
        sc.refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return None


async def once(
    request: web.Request, op: str, run: Callable[[], Awaitable[web.Response]]
) -> web.Response:
    """Answer *request* with :func:`run`'s reply, running it once per ``request_id``.

    *op* names the route, so a send and a create can never share a kept reply.
    A body that is not a JSON object, or carries no id, goes straight to *run*,
    which gives the route's own refusal or behaviour.
    """
    try:
        body = await request.json()
    except Exception:
        return await run()
    if not isinstance(body, dict) or body.get(REQUEST_ID_FIELD) is None:
        return await run()
    request_id = body[REQUEST_ID_FIELD]
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        return web.json_response(
            {
                "error": f"{REQUEST_ID_FIELD} must be 32 lowercase hex characters",
                "code": "invalid_request_id",
            },
            status=400,
        )
    key = (op, _read_session_key(request), request_id)
    fingerprint = _fingerprint(body)
    _forget_expired(time.monotonic())
    kept = _kept.get(key)
    if kept is not None:
        if kept.fingerprint != fingerprint:
            return web.json_response(
                {
                    "error": f"this {REQUEST_ID_FIELD} was already used for a different request",
                    "code": "request_id_reused",
                },
                status=409,
            )
        logger.info("session-control %s: answered a resend of %s from its first reply", op, key[2])
        # Shielded so a resend whose own client gives up cannot cancel the reply
        # every other resend of this id is waiting on.
        status, text = (
            kept.reply.result() if kept.reply.done() else await asyncio.shield(kept.reply)
        )
        refused = await _readmit(request, op, body)
        if refused is not None:
            return refused
        return _json_reply(*_stamped(status, text, request_id, REPLAY_REPLAYED, kept.attempt))
    caller_kept = sum(1 for kept_key in _kept if kept_key[1] == key[1])
    if len(_kept) >= MAX_KEPT_REPLIES or caller_kept >= MAX_KEPT_REPLIES_PER_CALLER:
        logger.warning(
            "session-control %s: replay table full (%d kept, %d for this caller); "
            "running %s without a kept reply",
            op,
            len(_kept),
            caller_kept,
            request_id,
        )
        response = await run()
        return _json_reply(
            *_stamped(response.status, response.text or "", request_id, REPLAY_UNSAVED)
        )
    kept = _Kept(fingerprint, asyncio.get_running_loop().create_future(), _attempt_of(body))
    _kept[key] = kept
    try:
        response = await run()
    except BaseException:
        kept.reply.set_result((500, json.dumps(_FIRST_ATTEMPT_FAILED)))
        kept.finished_at = time.monotonic()
        raise
    status, text = response.status, response.text or ""
    if len(text) <= MAX_KEPT_REPLY_CHARS:
        kept.reply.set_result((status, text))
    else:
        kept.reply.set_result((409, json.dumps(_REPLY_TOO_LARGE)))
    kept.finished_at = time.monotonic()
    return _json_reply(*_stamped(status, text, request_id, REPLAY_FIRST))
