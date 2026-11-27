"""Post a session-control call once per tool call, across lost replies.

Used by every session tool whose call opens a session or delivers something:
``session_create``, ``session_send`` (both also with ``crew``), ``session_fork``,
``session_broadcast``, ``session_continue``, ``session_set_note`` and
``work_dispatch``'s create and seed. The gateway can finish such a call after the
client stopped waiting for its reply. Each call therefore carries one request id, the post is repeated under that
id while the reply is lost, and the gateway answers a repeat with the first post's
reply (``dashboard.session_control_replay``). :mod:`kiro_crew.mcp_dashboard_session_runs`
calls these helpers.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, NamedTuple

from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import ToolContext

#: The body field these tools carry their per-call id in,
#: the gateway's ``session_control_replay.REQUEST_ID_FIELD`` (a test pins the two).
#: Spelled out here because importing the gateway module would load aiohttp into
#: every chat's tool server.
_REQUEST_ID_FIELD = "request_id"

#: Posts each of these tools makes while its reply is lost
#: (``transport_error``: the gateway may have done the work). Each repeat carries the
#: same id, so the gateway answers it with the first post's reply instead of doing
#: the work again. ``ask_question`` resends its open on the same terms.
LOST_REPLY_ATTEMPTS = 3

#: Seconds between those posts.
LOST_REPLY_PAUSE_SECS = 1.0


#: The reply field and value the gateway sets when it answered a repeat from the
#: first post (``session_control_replay.REPLAY_FIELD`` / ``REPLAY_REPLAYED``; a test
#: pins them). Only that answer proves a repeat did not run the work again.
_REPLAY_FIELD = "replay"
_REPLAYED = "replayed"

#: The body field naming which post of the call this is, and the reply field naming
#: the post a replay came from (``ATTEMPT_FIELD`` / ``REPLAY_ATTEMPT_FIELD``). Only a
#: replay of the FIRST post proves no post ran the work twice: when the first and a
#: later post both lose their replies, a replay of the later one says nothing about
#: whether the first ran too.
_ATTEMPT_FIELD = "request_attempt"
_REPLAY_ATTEMPT_FIELD = "replay_attempt"


class _OnceReply(NamedTuple):
    """The reply to a post made once per id, and what the caller must be told about it.

    ``resent_unprotected`` is set when the reply came from a repeat post that the
    gateway did not answer from the first post: it ran the repeat as new work (an
    older gateway, a restart that lost the kept reply, or a full replay table), so
    the work may have happened twice.
    """

    resp: Any
    resent_unprotected: bool


#: Codes the gateway's replay answers a resend with when it cannot show the first
#: post's outcome (``session_control_replay.OUTCOME_UNKNOWN_CODE`` and
#: ``REPLY_TOO_LARGE_CODE``; a test pins them).
_REPLAY_UNKNOWN_CODES = frozenset({"replay_outcome_unknown", "replay_reply_too_large"})


def _is_replay(reply: Any, request_id: str) -> bool:
    """Whether *reply* is the gateway answering a repeat from the first post."""
    return (
        isinstance(reply, dict)
        and reply.get(_REQUEST_ID_FIELD) == request_id
        and reply.get(_REPLAY_FIELD) == _REPLAYED
        and reply.get(_REPLAY_ATTEMPT_FIELD) == 0
    )


def _replays_first_outcome(refused: DashboardError, request_id: str) -> bool:
    """Whether a refused resend is the gateway replaying the first post's own refusal."""
    return _is_replay(refused.body, request_id) and refused.code not in _REPLAY_UNKNOWN_CODES


def _post_once(
    ctx: ToolContext, path: str, body: dict[str, Any], *, timeout: float | None = None
) -> _OnceReply:
    """POST *body* under one fresh id, repeating it only while the reply is lost.

    *timeout* is each post's own wait, for a route that needs longer than the default.

    Raises the last :class:`DashboardError`: a refusal at once, a lost reply only
    after :data:`LOST_REPLY_ATTEMPTS` posts. A resend's refusal that cannot show the
    first post's outcome is raised as a lost reply too.
    """
    request_id = uuid.uuid4().hex
    for attempt in range(LOST_REPLY_ATTEMPTS):
        keyed = {**body, _REQUEST_ID_FIELD: request_id, _ATTEMPT_FIELD: attempt}
        try:
            resp = ctx.client.post(path, keyed, session_key=ctx.caller_key, timeout=timeout)
        except DashboardError as refused:
            lost = bool(refused.body.get("transport_error"))
            if attempt > 0 and not lost and not _replays_first_outcome(refused, request_id):
                # A refused resend says nothing about the first post, whose reply was
                # lost: it may have done the work. Keep it an unknown outcome.
                raise DashboardError({**refused.body, "transport_error": True}) from refused
            if not lost or attempt + 1 == LOST_REPLY_ATTEMPTS:
                raise
            time.sleep(LOST_REPLY_PAUSE_SECS)
            continue
        return _OnceReply(resp, resent_unprotected=attempt > 0 and not _is_replay(resp, request_id))
    raise AssertionError("unreachable: the last attempt returns or raises")


def lost_reply_error(
    verb: str, outcome: str, check: str, refused: DashboardError, *, again: str = ""
) -> str:
    """The reply when every post's reply was lost: the work may still have happened.

    *again* is the verb of the "Do NOT ... it again" line when *verb* is a noun.
    """
    return (
        f"Error: no reply from the gateway to this {verb} after {LOST_REPLY_ATTEMPTS} "
        f"tries ({refused.error}). The {outcome}. Do NOT {again or verb} it again: "
        f"{check} first."
    )


def possible_double(what: str) -> str:
    """The warning for a resend the gateway ran as new work instead of replaying."""
    return (
        "the first reply was lost and the gateway could not answer the resend from it "
        f"(an older gateway, a restart, or a full replay table), so {what}"
    )
