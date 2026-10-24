"""The sessions this runtime holds and how each one leaves.

Init frames staged before a session id is known, the start-stall MCP diagnostics, turn-active
marks, and terminate, evict and unregister.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_pending_init_notifications``, ``_routed_requests``,
``_session_cwds``, ``_session_inits_in_flight``, ``_session_queues``,
``_subagent_owner``, ``_subagent_roster_overflow``, ``_subagent_sessions``, ``_turn_active_sessions``.

Instance fields read: ``_dead``, ``_process``, ``_record_keeping_sessions``, ``_start_collectors``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.runtime_start import _split_init_frames
from kiro_crew.acp.session_handle import AcpRequestTimeout
from kiro_crew.acp.types import (
    MCP_ROSTER_COMPLETE_NOTE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_SESSION_CLOSE,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


# Caps for the MCP progress line attached to a session-start timeout: a
# 70-server agent must not turn one error into a multi-kilobyte string, and
# neither a server's own error text nor its NAME is trusted for length. Both are
# config-derived, and an installed app supplies its own server names.
_MCP_PROGRESS_NAME_CAP = 8
_MCP_PROGRESS_ERROR_CAP = 120
_MCP_PROGRESS_NAME_LEN_CAP = 64


def _strip_unprintable(text: str) -> str:
    """Drop the control characters a whitespace collapse cannot reach.

    ``str.split`` removes whitespace controls (newline, tab, CR), but ESC and
    the other non-whitespace controls survive it, and a terminal rendering the
    gateway log interprets them -- an MCP server's failure text could forge or
    recolor terminal output. Spaces are printable, so a collapsed string keeps
    its word separation.
    """
    return "".join(ch for ch in text if ch.isprintable())


def _sanitize_progress_name(name: str) -> str:
    """Make one MCP server name safe to put in a log line and an exception.

    A name is config-derived, so an installed app chooses it. Four hazards, all
    closed here rather than at each use: an embedded newline would forge a line
    in the gateway log, a non-whitespace control (ESC) would inject terminal
    escapes into it, an unbounded name would defeat the count cap that keeps
    one error from becoming a wall of text, and a name carrying
    credential-shaped text would leak it into a sink the error message reaches.
    Whitespace collapse and the control strip run AFTER redaction so a
    redaction marker cannot reintroduce a break.
    """
    from kiro_crew.security import redact

    scrubbed = redact(name)
    return _strip_unprintable(" ".join(scrubbed.split()))[:_MCP_PROGRESS_NAME_LEN_CAP]


def _capped_names(names: list[str]) -> str:
    """Join names for an error line, truncating the tail to a countable summary.

    A pure formatter: names arrive already sanitized from the two points that
    admit them, so a composite like ``name (error)`` keeps its own error cap
    instead of being re-truncated to a name's length.
    """
    head = names[:_MCP_PROGRESS_NAME_CAP]
    rest = len(names) - len(head)
    joined = ", ".join(head)
    return f"{joined} (+{rest} more)" if rest > 0 else joined


def _queued_behind_note(ahead: list[tuple[str, float]]) -> str:
    """Describe the requests a timed-out session start was queued behind.

    ``ahead`` holds ``(method, seconds sent before this start)`` for each request
    in :data:`_ONE_AT_A_TIME_METHODS` that was outstanding when the start went out
    and was STILL unanswered at its deadline. An empty list says nothing.
    """
    if not ahead:
        return ""
    counts: dict[str, int] = {}
    for method, _age in ahead:
        counts[method] = counts.get(method, 0) + 1
    kinds = ", ".join(f"{n} {m}" for m, n in sorted(counts.items()))
    oldest = max(age for _m, age in ahead)
    return (
        f"queued behind {len(ahead)} earlier request(s) this agent process had not "
        f"answered by the deadline ({kinds}; oldest sent {oldest:.0f}s earlier), "
        "and it answers those one at a time"
    )


# Teardown must be snappy: a session is usually terminated on a hot path
# (background task done, subagent reaped). kiro-cli's terminate handler responds
# as soon as it enqueues the eviction (the actual shutdown runs in its actor
# loop), so a healthy runtime acks well under this bound; a slow/dead one must
# not turn teardown into a multi-second stall.
_TERMINATE_TIMEOUT = 5.0


def unregister_session(self: AcpRuntime, session_id: str) -> None:
    """Unregister a session queue (called by AcpSessionHandle.destroy)."""
    self._session_queues.pop(session_id, None)
    self._session_cwds.pop(session_id, None)
    self._switched_model_record().pop(session_id, None)
    # Clean up any pending routed requests for this session
    stale = [k for k, v in self._routed_requests.items() if v == session_id]
    for k in stale:
        del self._routed_requests[k]
    # The departing session takes its subagent ownership with it: a later
    # session on this warm runtime must never inherit a stale child's
    # approvals (the announce set is re-learned from the next
    # subagent/list_update, which re-establishes ownership explicitly).
    self._turn_active_sessions.discard(session_id)
    if self._subagent_owner == session_id:
        self._subagent_owner = None
        self._subagent_sessions = set()
        # The overflow count describes the set that just went away, so it
        # travels with it: a stale non-zero value would keep auditing later
        # unknown-session denials as cap truncations on a runtime whose
        # roster is empty.
        self._subagent_roster_overflow = 0
        # Same boundary for the log: the departing roster's truncation
        # episode is over, so its residual repeat count is flushed and the
        # next owner's first truncation is loud again.
        self._end_roster_overflow_episode()
    logger.debug("Removed session %s", session_id)


def mark_turn_active(self: AcpRuntime, session_id: str, active: bool) -> None:
    """Record whether a session's prompt dispatch loop is consuming.

    Called by ``AcpSessionHandle.prompt()`` on entry and (in a finally)
    on every exit path. Child permission requests are routed only while
    the owner is marked active; otherwise they are answered fail-closed
    immediately, because nothing reads the queue until the next turn.
    """
    if active:
        self._turn_active_sessions.add(session_id)
    else:
        self._turn_active_sessions.discard(session_id)


async def terminate_session(self: AcpRuntime, session_id: str) -> None:
    """Evict a session from kiro-cli (freeing its memory), then unregister locally.

    Sends the ``_kiro.dev/session/terminate`` request so the multiplexed
    kiro-cli process drops this session from its in-memory session map and
    shuts the session's agent down (reaping its MCP child processes). WITHOUT
    this, a finished session's transcript + context stay resident in the
    shared process for its entire lifetime — so RSS grows without bound as
    background tasks and subagents accumulate (the multiplexed design has no
    per-turn compaction, so per-session eviction is the only reclaim signal).

    This is co-tenant-safe: it targets exactly one ``sessionId`` and never
    touches the process, unlike ``kill()`` (which would take every sibling
    session down with it).

    Best-effort and bounded: teardown must never hang or raise. If the
    runtime is already dead the session's memory died with the process, so
    the round-trip is skipped. The local ``unregister_session`` ALWAYS runs
    (``finally``) so the reader loop stops routing to an abandoned queue even
    when the terminate request could not be delivered — including when the
    enclosing task is cancelled mid-await (``asyncio.CancelledError`` is a
    ``BaseException``, so it would otherwise slip past the ``except Exception``).

    The DELIVERY comes from the harness's own ``TeardownPolicy``, not from one
    shape applied to every host: a verb the host answers is a request bounded by
    ``_TERMINATE_TIMEOUT``, and a verb it does not answer is a notification. The
    distinction is invisible in the method name, and getting it wrong is expensive
    in the direction that looks like nothing is wrong — the eviction still happens,
    just a whole budget later, with a timeout logged against it.
    """
    if session_id in getattr(self, "_record_keeping_sessions", ()):
        # A load that re-injects a LIVE conversation's projection: every failure arm
        # on that path ends the session through this method, and on KAS the
        # teardown verb removes the stored record -- the very conversation the
        # re-injection exists to keep. The eviction keeps the record instead.
        await self.evict_session(session_id)
        return
    try:
        if not self._dead and self._process is not None:
            policy = self._harness.teardown
            try:
                if policy.notification:
                    # A verb the host does not answer. Awaiting a reply here would
                    # spend the whole teardown budget on every eviction and report
                    # it as a control-plane timeout, so the delivery follows what
                    # the harness declares rather than one shape for every host.
                    await self.send_notification(policy.method, {"sessionId": session_id})
                else:
                    await self._send_and_await(
                        policy.method,
                        {"sessionId": session_id},
                        timeout=_TERMINATE_TIMEOUT,
                    )
            except Exception:
                logger.debug(
                    "session teardown failed for %s (runtime dead/slow); "
                    "proceeding with local unregister",
                    session_id,
                    exc_info=True,
                )
    finally:
        self.unregister_session(session_id)


class _RecordKeepingScope:
    """The scope :func:`keep_record_on_teardown` opens; see it for the why.

    A class rather than a ``contextlib.contextmanager`` generator so the bound
    member keeps its own code object and qualname (the owner-binding contract the
    runtime's members are held to). Re-entrant for one id: the record is
    released when the outermost scope ends.
    """

    def __init__(self, runtime: AcpRuntime, session_id: str) -> None:
        self._runtime = runtime
        self._session_id = session_id
        self._already = False

    def __enter__(self) -> None:
        keeping = getattr(self._runtime, "_record_keeping_sessions", None)
        if keeping is None:
            keeping = set()
            self._runtime._record_keeping_sessions = keeping
        self._already = self._session_id in keeping
        keeping.add(self._session_id)

    def __exit__(self, *exc: Any) -> None:
        if not self._already:
            self._runtime._record_keeping_sessions.discard(self._session_id)


def keep_record_on_teardown(self: AcpRuntime, session_id: str) -> _RecordKeepingScope:
    """While the returned scope is open, a teardown of *session_id* through
    :func:`terminate_session` evicts instead of deleting its stored record.

    For the re-injection load (``acp/session_provider_mcp_reinject.py``): the
    load path ends the session on an activation refusal, a stale spec or a
    timed-out ``set_mode`` by calling :func:`terminate_session`, which on KAS is
    ``_kiro/session/delete``. Inside this scope that call is
    :func:`evict_session`, so a failed re-injection leaves the conversation
    where the next attempt -- or a plain resume -- can load it.
    """
    return _RecordKeepingScope(self, session_id)


async def evict_session(self: AcpRuntime, session_id: str) -> None:
    """Make the host drop a RESIDENT session while keeping its stored record.

    The standard ``session/close`` as a bounded request, then the local
    unregister. Distinct from :func:`terminate_session`, whose verb on KAS is
    ``_kiro/session/delete`` -- that frees the session AND removes the record a
    later ``session/load`` would restore. This one frees only the resident
    copy, which is the precondition for re-loading the same id with a changed
    projection: the engine takes a new ``_meta.kiro.customAgents`` batch on
    ``session/load`` only for a session it is not already holding
    (``test/fixtures/acp_frames/kas/mcp-live-apply-live.jsonl``).

    Best-effort on the wire for the reasons terminate is: a dead runtime took
    the resident copy with it, and a close the host refuses (an id it does not
    hold) is the state this call was reaching for. The local unregister always
    runs, so the reader loop stops routing to the departing queue even when the
    request could not be delivered.
    """
    try:
        if not self._dead and self._process is not None:
            try:
                await self._send_and_await(
                    METHOD_SESSION_CLOSE, {"sessionId": session_id}, timeout=_TERMINATE_TIMEOUT
                )
            except Exception:
                logger.debug(
                    "session/close failed for %s (runtime dead/slow); proceeding with local unregister",
                    session_id,
                    exc_info=True,
                )
    finally:
        self.unregister_session(session_id)


def _session_teardown_method(self: AcpRuntime) -> str:
    """The verb that frees one session on this backend.

    kiro-cli's terminate evicts the session from the process and leaves the
    transcript on disk for the caller to deal with. KAS offers no evict-only
    equivalent, so its delete does both at once.

    That difference is invisible here but matters to the caller: on KAS the
    local ``AcpSessionHandle._cleanup_transcript`` is a NO-OP, because it
    unlinks from kiro-cli's sessions dir and KAS keeps its own store. So the
    ``keep_transcript`` guard does not protect anything on KAS — a KAS
    session's record is gone once this verb returns. The only capability that
    loses is opportunistic ``spawn_continue`` on a shared subagent, which
    degrades to a typed ``conversation_gone`` and a re-spawn; explicitly
    continuable runs are dedicated sessions that never reach this path.
    """
    return self._harness.teardown.method


def _mcp_init_progress(self: AcpRuntime, expected: Any, *, queued_behind: bool = False) -> str:
    """Describe MCP registration progress for a session start that stalled.

    Reads the frames the reader loop already staged in
    ``_pending_init_notifications`` so a session-start timeout can name the
    servers that never reported, instead of reporting only the elapsed
    budget. Must run BEFORE ``_finish_session_init``, which drops that
    buffer once the last in-flight init closes.

    ``expected`` is the ``mcpServers`` array sent with the request. Its
    entries carry the roster names, and that is what makes the ABSENT
    servers nameable rather than only the present ones.

    The text says what that roster IS. On kiro-cli the array holds only the
    broker stubs Kiro Crew injects (``pooled_session_servers``); the agent
    spec's own servers are started by the backend and are not in it, and
    the backend's session-start steps after MCP init are not observable
    from here at all. A bare ``4/4 MCP server(s) reported`` therefore read
    as "all MCP is up, so MCP is the problem" -- a field report was
    triaged that way on the strength of the suffix alone -- when it only
    ever meant that the four injected servers had spoken. The count is
    now labelled ``session-injected``, and a complete roster is followed
    by what it does and does not cover, so a reader is not sent to chase
    MCP for a stall that is past it.

    Reports are runtime-wide rather than per-session: a request that never
    answered has no session id to match its frames against, so a concurrent
    init is called out in the text instead of being silently folded in. What
    keeps that bounded to the CONCURRENT inits is that a start whose caller
    already gave up keeps its own frames on its :class:`StartCollector`
    instead of here — un-keyed by name, they would otherwise be read as this
    attempt's progress and hide the servers that never reported.
    Likewise the staging deque is bounded, so on a very large fleet the
    reported count is a floor, not an exact tally.
    """
    roster = [
        _sanitize_progress_name(str(e.get("name") or ""))
        for e in (expected if isinstance(expected, list) else [])
        if isinstance(e, dict) and e.get("name")
    ]
    ready: list[str] = []
    failed: list[str] = []
    failure_text: dict[str, str] = {}
    awaiting_auth: list[str] = []
    for msg in self._pending_init_notifications:
        params = msg.params if isinstance(msg.params, dict) else {}
        name = _sanitize_progress_name(str(params.get("serverName") or params.get("name") or ""))
        if not name:
            continue
        if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
            if name not in ready:
                ready.append(name)
        elif msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
            # A failed server's error text can carry connection strings or
            # tokens from its startup, so it takes the same scrub the
            # dashboard banner applies before it lands in an exception.
            from kiro_crew.security import redact

            err = redact(str(params.get("error") or ""))
            err = _strip_unprintable(" ".join(err.split()))[:_MCP_PROGRESS_ERROR_CAP]
            if name not in failed:
                failed.append(name)
            if err:
                failure_text[name] = err
        elif msg.is_method(METHOD_MCP_OAUTH_REQUEST):
            if name not in awaiting_auth:
                awaiting_auth.append(name)

    reported = set(ready) | set(failed)
    parts: list[str] = []
    if roster:
        # Count only reports that belong to the roster. kiro-cli initializes
        # the agent spec's own servers as well as the session-injected ones,
        # so the staged frames are a SUPERSET of the roster and a raw
        # len(reported) can exceed the denominator -- "2/1 reported". The
        # out-of-roster servers still appear by name in the failed and
        # awaiting-authorization buckets, where naming them is the point.
        parts.append(
            f"{len(reported & set(roster))}/{len(roster)} session-injected "
            "MCP server(s) reported"
        )
        silent = [n for n in roster if n not in reported]
        if silent:
            parts.append(f"no report from {_capped_names(silent)}")
        elif not set(failed) & set(roster) and not queued_behind:
            # Every roster member reported READY. A member that reported an
            # init failure counts as reported (so it is never chased as
            # silent) but is named under ``failed:`` below, and the stall
            # may be in it -- so the "not in those servers" verdict is
            # withheld then. It is withheld too when the start was sent
            # behind unanswered requests: the budget may have gone to those
            # before this start ran at all, which is not "later in startup".
            parts.append(MCP_ROSTER_COMPLETE_NOTE)
    else:
        # No roster to attribute against: these reports belong to the agent
        # spec's own servers or to a concurrent start, so the count is not
        # labelled "session-injected" here.
        parts.append(f"{len(reported)} MCP server report(s), roster unknown")
    if failed:
        parts.append(
            "failed: "
            + _capped_names(
                [f"{n} ({failure_text[n]})" if n in failure_text else n for n in failed]
            )
        )
    if awaiting_auth:
        parts.append(f"awaiting authorization: {_capped_names(awaiting_auth)}")
    if self._session_inits_in_flight > 1:
        parts.append(
            f"{self._session_inits_in_flight} session inits in flight, "
            "so these reports are runtime-wide"
        )
    return "; ".join(parts)


def _session_start_stalled(
    self: AcpRuntime, exc: AcpRequestTimeout, method: str, expected: Any
) -> AcpRequestTimeout:
    """Attach MCP progress to a session-start timeout before it reaches the user.

    Session start is the one request whose cost is dominated by work the
    runtime can observe, so the bare budget is the least useful half of the
    answer. Returns a replacement to raise rather than raising here, so the
    caller keeps the ``from exc`` chain.
    """
    queued = _queued_behind_note(getattr(exc, "queued_behind", None) or [])
    progress = "; ".join(
        part
        for part in (queued, self._mcp_init_progress(expected, queued_behind=bool(queued)))
        if part
    )
    logger.warning("%s stalled: %s", method, progress or "no MCP reports staged")
    # Tag the exception the caller will raise as a SESSION-START failure,
    # whichever of the two it is: both reach a self-driving caller as "the
    # cycle never got a session", and the tag is how that caller counts the
    # streak without reading the message text.
    exc.session_start_failed = True
    if not progress:
        return exc
    replacement = AcpRequestTimeout(f"{exc} ({progress})")
    replacement.session_start_failed = True
    return replacement


def _stage_init_frame(self: AcpRuntime, msg: JsonRpcMessage) -> None:
    """Hold one MCP-init frame until the session id that claims it is known.

    ``session/new`` can emit OAuth and MCP registration frames before its
    response, and the response is what gives ``create_session`` the id needed
    to register the queue. Registration frames matter beyond logging:
    ``drain_init()`` arms its idle shortcut on the first one, so dropping them
    here makes the session look report-less and pay the full no-report
    ceiling.

    Two holders, and a frame goes to both — the in-flight init scope's deque,
    which ``_mcp_init_progress`` also reads un-keyed, and every live
    :class:`StartCollector`, which owns a start whose id is still unknown.
    Neither can hand a frame to the other's session (both claim by the id in
    the frame, and two ``session/new`` answers never carry the same one), and
    both are bounded, so a frame nobody claims cannot accumulate.
    """
    if self._session_inits_in_flight:
        self._pending_init_notifications.append(msg)
    for collector in self._start_collectors.values():
        collector.stage_init_frame(msg)


def _finish_session_init(self: AcpRuntime, session_id: str) -> list[JsonRpcMessage]:
    """Take staged init frames for one session and close its init scope."""
    matched, self._pending_init_notifications = _split_init_frames(
        self._pending_init_notifications, session_id
    )
    self._session_inits_in_flight -= 1
    if self._session_inits_in_flight == 0:
        # Anything unmatched belongs to a failed/abandoned init. Never let
        # it survive into the next session creation attempt.
        self._pending_init_notifications.clear()
    return matched
