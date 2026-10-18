"""The single stdout reader.

Routing every frame by id and sessionId, the host's notification aliases, oversize-frame
recovery, and the error an awaited request fails with.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_answer_tasks``, ``_last_activity``, ``_pending_requests``,
``_routed_requests``.

Instance fields read: ``_acp_backend``, ``_kas_host_auth``, ``_launch_cwd_roots``,
``_map_launch_cwd_paths``, ``_max_answer_tasks``,
``_mcp_secret_server``, ``_native_skill_projection``, ``_process``,
``_session_inits_in_flight``, ``_session_queues``, ``_start_collectors``,
``_subagent_owner``, ``_subagent_roster_overflow``, ``_subagent_sessions``,
``_turn_active_sessions``, ``recording_allowed``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import kas_secret_storage
from kiro_crew.acp._frame_record import record_frame
from kiro_crew.acp.harness import NotificationAliases
from kiro_crew.acp.runtime_frame_accounting import _DROP_NO_SESSION, _ROSTER_OVERFLOW_REJECT_REASON
from kiro_crew.acp.runtime_host_requests import _UNREGISTERED_REJECT_REASON
from kiro_crew.acp.session_handle import AcpFrameTooLarge, AcpModeNotFound, AcpRuntimeError
from kiro_crew.acp.transport_framing import (
    _STDOUT_BUFFER_LIMIT,
    OversizeLineUnrecoverable,
    _drain_oversize_line,
)
from kiro_crew.acp.types import (
    METHOD_AGENT_SWITCHED,
    METHOD_KAS_OPEN_EXTERNAL_URL,
    METHOD_REQUEST_PERMISSION,
    JsonRpcMessage,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.metrics.events import CHILD_PERMISSION_ROUTED, emit_counter

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


# How many in-flight request ids to name in the oversize-frame warning. A dropped
# frame can carry a response; when its head names the awaited request, that
# request fails with AcpFrameTooLarge, and otherwise its caller times out, which
# naming what was in flight at the drop makes attributable. Capped so the line
# stays bounded.
_DROP_IDS_IN_LOG = 8

# The ``id`` of a JSON-RPC envelope near the front of a dropped oversize frame. A
# numeric id only: this runtime numbers its own requests, and a string id or a
# nested mode ``"id": "<name>"`` is never one of them.
_OVERSIZE_FRAME_ID_RE = re.compile(rb'"id"\s*:\s*(\d{1,18})\s*[,}]')


def _oversize_frame_request_id(head: bytes) -> int | None:
    """The request id a dropped oversize frame answered, when its head names one.

    A frame carrying ``"method"`` is a request or notification FROM the host, whose
    id counter is independent of ours, so it never names one of our requests.
    """
    if b'"method"' in head:
        return None
    match = _OVERSIZE_FRAME_ID_RE.search(head)
    if match is None:
        return None
    # The envelope's own id precedes its payload; one found after ``result`` /
    # ``error`` began is a nested field, not the envelope's.
    for key in (b'"result"', b'"error"'):
        at = head.find(key)
        if 0 <= at < match.start():
            return None
    return int(match.group(1))


def _oversize_frame_message(size: int) -> str:
    """The error an awaited request fails with when its reply was dropped as oversize."""
    return (
        f"The agent backend's reply was {size:,} bytes, over Kiro Crew's "
        f"{_STDOUT_BUFFER_LIMIT:,}-byte frame limit, so it was dropped. On kiro-cli, a "
        "session/new or session/load reply lists every agent in ~/.kiro/agents with its "
        "description and welcomeMessage, so an agent package whose specs carry very large "
        "welcomeMessage text is the usual cause: trim those fields, or remove agents you "
        "do not use."
    )


#
# kiro-cli returns this when session/set_mode names an agent it cannot resolve:
# no ``<name>.json`` in its agents directory, or one published after the process
# started, which kiro-cli 2.26.0 does not pick up. The wire shape is a
# bare -32603 "Internal error", so nothing about the frame says which.
# The name charset is bounded to what a real spec filename can hold (see
# validation of agent names elsewhere) rather than a greedy match, so a hostile
# or malformed backend string is not echoed back into a user-facing message.
_MODE_NOT_FOUND_RE = re.compile(r"""Mode ['"](?P<name>[A-Za-z0-9._-]{1,64})['"] not found""")


def _format_runtime_rpc_error(error: object) -> str:
    """Format an awaited-request JSON-RPC error into user-facing text.

    Awaited requests are the handshake ones — ``initialize``, ``session/new``,
    ``session/set_mode`` — so this is NOT the same population as
    ``client._format_acp_error``, which rewrites PROMPT-time provider failures
    (throttling, auth, 5xx) and has no branch that matches a missing agent spec.
    The two are deliberately separate rather than merged: their inputs come from
    different protocol phases and share no shape.

    Exactly one shape is rewritten today: ``Mode '<name>' not found``. Left raw it
    surfaces to the user as ``RPC error: {'code': -32603, 'message': 'Internal
    error', 'data': "Mode 'kirocrew' not found"}`` — which names an internal ACP
    concept and reads as a backend bug. For an authored spec the cause is a local
    file and the fix is one command. For a skill-view alias it is neither: the
    alias is generated, so setup does not restore it, and the file may be on
    disk but unloaded by this process, so that text names the retry and the
    projection switch instead. Every other shape falls through to the raw dict,
    so a shape nobody has classified is surfaced rather than swallowed.
    """
    if isinstance(error, dict):
        match = _MODE_NOT_FOUND_RE.search(str(error.get("data", "") or ""))
        if match:
            name = match.group("name")
            if name.startswith(NATIVE_SKILL_ALIAS_PREFIX):
                # A skill-view alias is Crew-generated, not an installed spec, so
                # setup cannot restore it, and "Mode not found" does not mean the
                # file is missing: kiro-cli also answers it for a spec that was
                # published after the process started.
                from kiro_crew.acp.skill_projection import remembered_view_source

                source = remembered_view_source(name)
                who = f"agent '{source}'" if source else "this agent"
                return (
                    f"kiro-cli could not switch to the skill view '{name}' that Kiro "
                    f"Crew generated for {who}. It may be on disk in "
                    f"{kiro_agents_dir()} but not loaded by this kiro-cli process. "
                    f"Start a new session to retry. If it keeps failing, set "
                    f"KIROCREW_NATIVE_SKILL_PROJECTION=0 in the gateway's environment "
                    f"and restart the gateway."
                )
            # ``--clean`` is not the repair: it skips merging the existing
            # config, so it drops the operator's own MCP servers and tools.
            return (
                f"Agent spec '{name}' is not installed: kiro-cli found no "
                f"'{name}.json' in {kiro_agents_dir()}. Every turn fails until it "
                f"is restored — repair with `kirocrew setup --agent-only`, "
                f"then restart the gateway."
            )
    return f"RPC error: {error}"


def _runtime_rpc_exception(error: object) -> AcpRuntimeError:
    """The exception an awaited request fails with; :func:`_format_runtime_rpc_error`'s text.

    A missing mode is an :class:`AcpModeNotFound` carrying the name, so the
    ``set_mode`` path can recover from a skill-view alias the host has not
    loaded yet without parsing the sentence it would show a user.
    """
    message = _format_runtime_rpc_error(error)
    if isinstance(error, dict):
        match = _MODE_NOT_FOUND_RE.search(str(error.get("data", "") or ""))
        if match:
            return AcpModeNotFound(message, match.group("name"))
    return AcpRuntimeError(message)


async def _reader_loop(self: AcpRuntime) -> None:
    """Single reader task — owns stdout exclusively. Routes frames by type.

    Routing:
      1. Response with id in _pending_requests → resolve Future
      2. Response with id in _routed_requests → put in session queue
      3. Notification with params.sessionId → session queue
      4. Request (method + id) with no sessionId → answered ONCE at
         connection level (-32601), never broadcast
      5. No sessionId → broadcast to all queues
    """
    assert self._process and self._process.stdout
    stdout = self._process.stdout
    # This host's spellings for the three aliasable inbound events, read ONCE
    # per reader rather than per frame: they are fixed for the process's life
    # and this is the hot demux path.
    _aliases = self._notification_aliases()
    _session_update = _aliases.session_update
    _subagent_list_update = _aliases.subagent_list_update
    _mcp_init = _aliases.mcp_init

    try:
        while True:
            try:
                line = await stdout.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                # EOF, possibly holding a trailing unterminated line. Keep
                # readline()'s old shape: hand the partial to the parser, and
                # an empty partial falls through to the exit branch below.
                line = exc.partial
            except asyncio.LimitOverrunError as exc:
                # ONE oversize frame must not kill the demux — same invariant
                # as the non-dict and non-numeric-id guards below. Tearing
                # the runtime down here ends EVERY multiplexed session
                # mid-turn, which is what users see as "process exited /
                # chat failure" after a single huge tool result.
                #
                # _drain_oversize_line consumes the whole line THROUGH its
                # terminating newline and discards it, so the stream is back
                # on a frame boundary and no byte-slice of the oversize line
                # ever reaches json.loads. Its budget is per call and needs no
                # cross-iteration state, because every call that returns ends
                # on a boundary — so a replay of oversize-but-terminated
                # frames is survivable frame after frame.
                #
                # An awaited request whose response was in a dropped frame is
                # never orphaned: when the frame's head names it, it fails at
                # once with AcpFrameTooLarge (below); otherwise
                # _send_and_await's wait_for(timeout=...) ends it, and the ids
                # in flight at the drop are logged so that timeout is
                # attributable.
                head = bytearray()
                try:
                    dropped = await _drain_oversize_line(stdout, exc, head=head)
                except asyncio.IncompleteReadError:
                    # The same observed EOF as the empty-line branch below,
                    # reached with a torn frame in hand; the exit is confirmed
                    # and retired the same way.
                    self._mark_dead("stdout closed mid-oversize-line")
                    await self._retire_tracking_after_exit()
                    return
                except OversizeLineUnrecoverable as fatal:
                    logger.error("stdout unrecoverable: %s", fatal)
                    self._mark_dead(f"stdout overrun: {fatal}")
                    return
                logger.warning(
                    "dropped an oversize stdout frame (%d bytes); resynced at "
                    "next frame (in-flight awaited=%s routed=%s): %s",
                    dropped,
                    sorted(self._pending_requests)[:_DROP_IDS_IN_LOG],
                    sorted(self._routed_requests)[:_DROP_IDS_IN_LOG],
                    exc,
                )
                # A reply to an awaited request fails THAT request now, with
                # the size and the limit, instead of the timeout it would
                # otherwise hit much later under an unrelated name.
                req_id = _oversize_frame_request_id(bytes(head))
                pending = self._pending_requests.get(req_id) if req_id is not None else None
                if pending is not None and not pending.done():
                    self._pending_requests.pop(req_id, None)
                    pending.set_exception(AcpFrameTooLarge(_oversize_frame_message(dropped)))
                continue

            if not line:
                rc = self._process.returncode if self._process else "?"
                self._mark_dead(self._exit_reason(rc))
                # The one death path that OBSERVES the exit rather than
                # causing it. ``kill()`` retires the registry entries after
                # its own reap; nothing else did, so an externally killed
                # root stayed in ``kiro_session_pids.txt`` until the
                # periodic sweep's next tick -- up to 300s of a dead pid a
                # reaper could resignal.
                await self._retire_tracking_after_exit()
                return

            self._last_activity = time.monotonic()

            # Only a JSON object is a frame. Every other line is skipped here
            # and never reaches the crash arm below, which would mark this
            # single-owner reader dead and end EVERY multiplexed session:
            # text that is not JSON, a bare scalar or array (`123`, `"foo"`,
            # `[1,2]`, `null`), nesting past the decoder's ceiling
            # (RecursionError), and an integer past the int-string digit
            # limit. Bytes that are not UTF-8 decode with replacement, as in
            # AcpClient._read_message, so one bad byte inside a string costs
            # that character, not the frame.
            parsed = parse_json_object_line(line, errors="replace")
            if parsed is None:
                if self.recording_allowed:
                    logger.debug("stdout line that is not a JSON object: %s", line[:200])
                continue
            data: dict[str, Any] = parsed

            # Opt-in raw-frame recording for the replay corpus. A no-op
            # unless KIROCREW_ACP_RECORD_FRAMES names a directory: it
            # returns before awaiting anything, so an ordinary run pays one
            # env lookup. When it IS set the frame is queued for the
            # recorder's own writer thread rather than written here,
            # because a filesystem syscall on this loop stalls every
            # multiplexed session. It never raises -- see
            # kiro_crew.acp._frame_record.
            # A secret-storage frame is never recorded: its value is an MCP
            # OAuth grant, and the recorder's redaction is best-effort.
            if self.recording_allowed and not kas_secret_storage.is_secret_method(
                data.get("method")
            ):
                await record_frame(self._acp_backend, data, len(line))

            projection = getattr(self, "_native_skill_projection", None)
            if projection is not None:
                data = projection.frame(data)
            if self._launch_cwd_roots:
                self._map_launch_cwd_paths(data)
            msg = JsonRpcMessage.from_dict(data)

            # Route responses
            if msg.id is not None and (msg.result is not None or msg.error is not None):
                # JSON-RPC allows string ids, and this runtime only ever
                # issues int ids — but the id in the response is agent-
                # controlled. int("req-1") / int([...]) raises ValueError/
                # TypeError, which the catch-all below turns into
                # _mark_dead, poisoning EVERY multiplexed session over one
                # unmatched frame. Same invariant as the non-dict guard
                # above: skip the frame, don't kill the demux.
                try:
                    req_id = msg.id if isinstance(msg.id, int) else int(msg.id)
                except (TypeError, ValueError, OverflowError):
                    # OverflowError: json parses 1e9999 to float("inf"),
                    # which int() rejects differently from a bad string.
                    #
                    # Left per-frame on purpose (same for the unmatched-id
                    # line below), unlike the two session-routing drops:
                    # here the ID is the whole diagnostic value, and it is a
                    # distinct value per frame — aggregating by it would give
                    # the counter an unbounded key space, while aggregating
                    # without it would throw away the only datum that
                    # identifies the correlation bug. Both branches also
                    # require a response-shaped frame, i.e. one per request
                    # THIS runtime issued (bounded by turns), so neither has
                    # the after-teardown steady state that made the
                    # unknown-session line a flood.
                    logger.debug("Response with non-numeric id %r dropped", msg.id)
                    continue

                # Check awaited requests first (init, session/new, set_mode)
                future = self._pending_requests.pop(req_id, None)
                if future and not future.done():
                    if msg.error:
                        future.set_exception(_runtime_rpc_exception(msg.error))
                    else:
                        future.set_result(msg.result or {})
                    continue

                # Check routed requests (prompt response → session queue)
                session_id = self._routed_requests.pop(req_id, None)
                if session_id and session_id in self._session_queues:
                    await self._session_queues[session_id].put(msg)
                    continue

                logger.debug("Unmatched response id=%d", req_id)
                continue

            # Inbound server→client REQUEST (method + id, no result/error).
            # A connection-level request Crew answers ITSELF is one this host's
            # harness names, and today that is the engine's credential callback
            # (_kiro/auth/getAccessToken) — answered only on a process spawned
            # with Crew as the auth owner (see _resolve_spawn_plan /
            # kas_transport.build_kas_argv). It carries no sessionId — the
            # first one arrives before session/new has even returned — so it is
            # handled here, OFF this loop: resolving and possibly refreshing a
            # token must not block stdout demux for every other multiplexed
            # session. On a cli-owned spawn the frame never arrives, and a
            # method the harness does not name falls through to the -32601
            # ownerless answer below rather than ever being paid with a
            # credential. The harness resolves that list from
            # ACP_BACKENDS_HOST_AUTH_CALLBACK, so it and this guard cannot
            # disagree about who answers what.
            if (
                msg.id is not None
                and msg.result is None
                and msg.error is None
                and self._kas_host_auth
                and msg.method in self._harness.host_answered_methods
            ):
                # Same bounded progress-or-dead admission as permission
                # answers: this is a request, so the counted-drop path that
                # is valid for notifications is not — and it shares the one
                # answer-task set so the combined total stays under the real
                # resource ceiling.
                if not await self._wait_for_answer_capacity(msg, request_kind="KAS auth"):
                    continue
                _auth_task = asyncio.ensure_future(
                    self._answer_host_request(msg.id, msg.method or "")
                )
                self._answer_tasks.add(_auth_task)
                _auth_task.add_done_callback(self._answer_tasks.discard)
                continue
            # The consent URL of an MCP sign-in this runtime started. It
            # names no session, so it is answered here and handed to the
            # session that owns the sign-in; a host that never declared the
            # capability falls through to the -32601 answer below.
            if (
                msg.id is not None
                and msg.result is None
                and msg.error is None
                and msg.method == METHOD_KAS_OPEN_EXTERNAL_URL
                and self._harness.opens_external_urls
            ):
                if not await self._wait_for_answer_capacity(msg, request_kind="MCP sign-in"):
                    continue
                _url_task = asyncio.ensure_future(
                    self._answer_open_external_url(msg.id, msg.params)
                )
                self._answer_tasks.add(_url_task)
                _url_task.add_done_callback(self._answer_tasks.discard)
                continue
            # The engine's MCP sign-in store, answered only on a process whose
            # handshake declared secretStorage; otherwise -32601 below.
            if (
                msg.id is not None
                and msg.result is None
                and msg.error is None
                and kas_secret_storage.is_secret_method(msg.method)
                and self._mcp_secret_server is not None
            ):
                if not await self._wait_for_answer_capacity(msg, request_kind="MCP secret"):
                    continue
                _secret_task = asyncio.ensure_future(
                    self._answer_secret_request(
                        self._mcp_secret_server, msg.id, msg.method or "", msg.params
                    )
                )
                self._answer_tasks.add(_secret_task)
                _secret_task.add_done_callback(self._answer_tasks.discard)
                continue
            # Any other request that arrives without a sessionId is
            # unroutable and is answered -32601 by
            # _answer_ownerless_request below, rather than being left to
            # hang.

            # Route notifications by sessionId
            session_id = (msg.params or {}).get("sessionId")
            if not session_id and _subagent_list_update and msg.method == _subagent_list_update:
                # Snapshot backend-internal subagent session ids before the
                # broadcast below delivers the frame to the UI consumers.
                # Each frame carries the FULL current list, so replace.
                self._snapshot_subagent_sessions(msg.params or {})
            if session_id:
                # A frame tagged with a sessionId belongs to exactly one
                # session. Route to it if registered; otherwise DROP it.
                # Broadcasting a known-but-unregistered session's frame to
                # every other session would be cross-talk.
                queue = self._session_queues.get(session_id)
                if queue is not None:
                    if msg.is_method(METHOD_AGENT_SWITCHED):
                        self._note_switched_model(session_id, msg.params)
                    await queue.put(msg)
                elif (
                    session_id in self._subagent_sessions
                    and self._subagent_owner is not None
                    and list(self._session_queues) == [self._subagent_owner]
                    and (msg.is_method(METHOD_REQUEST_PERMISSION) or msg.method in _session_update)
                ):
                    # A frame for a backend-internal subagent the backend
                    # itself announced via `subagent/list_update`, on a
                    # runtime with an UNAMBIGUOUS consumer (exactly one
                    # registered session — the dashboard-slot shape).
                    #
                    # - session/update — under EITHER spelling: kiro-cli
                    #   2.21.x emits child updates as the extension method
                    #   `_kiro.dev/session/update` where earlier versions
                    #   used plain `session/update`. Routed so the
                    #   consumer's per-toolCallId caches capture the
                    #   child's REAL command bytes; the handle re-tags
                    #   them as crew activity, never as parent transcript.
                    #   Both spellings must route: a dropped child update
                    #   leaves the caches empty, child MCP identity
                    #   unverified, and every auto-approve path falls to
                    #   the interactive card.
                    # - session/request_permission: routed so the child's
                    #   approval flows through the exact policy pipeline a
                    #   main-agent approval takes — with the command bytes
                    #   above, mode behavior (normal/read/trust/yolo) is
                    #   IDENTICAL to the main agent's. Dropping a REQUEST
                    #   is never an option: it strands the backend's
                    #   response oneshot and wedges the child's whole tool
                    #   batch until process teardown, with every approval in
                    #   that batch hanging invisibly for as long as that
                    #   runtime lives.
                    #
                    # With several registered sessions the frame names no
                    # owner; a permission request then falls to the
                    # fail-closed auto-answer below and updates are
                    # counted drops as before.
                    #
                    # A permission REQUEST is routed only while the owner
                    # has an in-flight prompt (an outstanding routed
                    # request = the dispatch loop is consuming the queue).
                    # Between turns nothing reads the queue until the next
                    # prompt's drain, so a background child's request
                    # would sit unanswered — the original hang with extra
                    # steps. Answer it fail-closed NOW instead.
                    _owner_turn_active = self._subagent_owner in self._turn_active_sessions
                    if (
                        msg.id is not None
                        and msg.is_method(METHOD_REQUEST_PERMISSION)
                        and not _owner_turn_active
                    ):
                        await self._spawn_answer_task(
                            msg,
                            session_id,
                            # Registered + announced — the owner just
                            # has no in-flight prompt. A distinct SEL
                            # tag keeps normal background-child
                            # behavior distinguishable from a real
                            # misconfiguration in the audit trail.
                            reason="owner_no_active_turn",
                        )
                        # Yield so spawned answer tasks actually RUN
                        # between frames: with 129+ frames already
                        # buffered, readline() returns without
                        # suspending, and the reader would hit the
                        # flood cap before any answer task had a chance
                        # to complete — falsely killing a responsive
                        # runtime. One loop-tick lets quick answers
                        # drain; a genuinely wedged backend still
                        # accumulates blocked tasks and trips the cap.
                        await asyncio.sleep(0)
                    elif not _owner_turn_active:
                        # An UPDATE between the owner's turns (either
                        # session/update spelling). Nothing reads the
                        # queue until the next prompt's dispatch loop,
                        # and _run_turn clears the per-toolCallId caches
                        # at turn start and then discards stale
                        # non-permission frames from the queue — so a
                        # between-turns update can never contribute a
                        # cache write or an activity event. Queueing it
                        # would only grow an unbounded queue in gateway
                        # memory while the slot idles (session queues
                        # have no depth cap). Unlike a REQUEST there is
                        # no protocol obligation to answer, so take the
                        # counted-drop path.
                        self._note_dropped_frame(session_id, msg.method)
                    else:
                        # Hang-resilience series: a child permission
                        # request delivered to the mode-parity pipeline.
                        # Counted so routed requests are observable next
                        # to the dropped-frame counter — a permission
                        # request that is neither routed nor answered
                        # leaves its crew waiting on a prompt nobody can
                        # see.
                        if msg.id is not None and msg.is_method(METHOD_REQUEST_PERMISSION):
                            emit_counter(CHILD_PERMISSION_ROUTED, {"surface": "runtime"})
                        await next(iter(self._session_queues.values())).put(msg)
                elif msg.id is not None and msg.is_method(METHOD_REQUEST_PERMISSION):
                    # Unannounced, ambiguous, or announced past the
                    # recognition cap: nobody on this client can see or
                    # answer the prompt — answer NOW with the request's own
                    # least-destructive reject option so the backend
                    # subagent gets a tool error instead of hanging. Never
                    # auto-APPROVE: no policy engine has seen the tool call
                    # and, past the cap, the id cannot be shown to be this
                    # owner's child at all.
                    #
                    # The reason names WHICH of those it was. Past the cap a
                    # roster truncation, not the backend's silence, is what
                    # cost this child its approval card, and that is the one
                    # thing an operator can act on (the two counts in the
                    # snapshot warning above are the same signal).
                    await self._spawn_answer_task(
                        msg,
                        session_id,
                        reason=(
                            _ROSTER_OVERFLOW_REJECT_REASON
                            if self._subagent_roster_overflow
                            else _UNREGISTERED_REJECT_REASON
                        ),
                    )
                    # Same yield rationale as the routed-owner branch above.
                    await asyncio.sleep(0)
                elif msg.method in _mcp_init and (
                    # Read live, never hoisted beside _mcp_init: collectors
                    # appear and settle during one reader lifetime.
                    self._session_inits_in_flight
                    or self._start_collectors
                ):
                    self._stage_init_frame(msg)
                else:
                    # Counted, not logged per frame: this is the measured
                    # flood (transcript replay during session/load, plus any
                    # backend still streaming after teardown).
                    self._note_dropped_frame(session_id, msg.method)
                continue

            # No sessionId. An id-carrying frame that still has a method is
            # a server→client REQUEST that names no session — it expects
            # exactly ONE response, so the runtime answers it at connection
            # level (same shape as the KAS auth callback above) instead of
            # broadcasting. Broadcasting would hand it to EVERY registered
            # session's dispatch loop, each of which replies -32601 on the
            # shared stdin: one id, N responses — a JSON-RPC protocol
            # violation that widens with session sharing. Frames with an id
            # but NO method are responses (e.g. a result of null slips past
            # the result/error check above); their handling is unchanged.
            if msg.id is not None and msg.method is not None:
                # Same volume bound as the permission auto-answers: each
                # reply can block on stdin drain() against a backend that
                # floods frames while never reading, so the task must be
                # retained (a bare ensure_future can be GC'd mid-flight)
                # and counted. Past the cap the frame takes the counted-
                # drop path — the flooding backend hangs on its own
                # unanswered request instead of growing the task set.
                if len(self._answer_tasks) >= self._max_answer_tasks:
                    self._note_dropped_frame(_DROP_NO_SESSION, msg.method)
                    continue
                _t = asyncio.ensure_future(self._answer_ownerless_request(msg.id, msg.method))
                self._answer_tasks.add(_t)
                _t.add_done_callback(self._answer_tasks.discard)
                continue

            # No sessionId → genuinely global notification; broadcast to all.
            if self._session_queues:
                # Snapshot: `await queue.put` yields, and a concurrent
                # unregister_session() could pop mid-iteration otherwise.
                _queues = list(self._session_queues.values())
                # Fanning one ownerless frame out to SEVERAL sessions means
                # at most one recipient produced it and nothing says which,
                # so mark it: a consumer that measures its own activity (the
                # subagent idle-stall clock) must not count another tenant's
                # traffic. A lone session IS the sole owner, so it is left
                # unmarked and keeps reading the frame as its own.
                if len(_queues) > 1:
                    msg.fanout_no_owner = True
                for queue in _queues:
                    await queue.put(msg)
            else:
                # Same unbounded shape as the unknown-session branch: with
                # zero registered sessions EVERY global notification lands
                # here, so a backend that keeps streaming after the last
                # teardown floods at frame rate. Counted the same way, with
                # a sentinel for the session half of the key.
                self._note_dropped_frame(_DROP_NO_SESSION, msg.method)

    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.error(
            "Reader loop crashed: %s",
            exc if self.recording_allowed else type(exc).__name__,
            exc_info=self.recording_allowed,
        )
        self._mark_dead(f"reader crash: {exc}")
    finally:
        # Report the residual count on EVERY exit (EOF, overrun, cancel,
        # crash) so a trickle that never reached the interval is still
        # accounted for instead of vanishing with the task. Both throttled
        # summaries: the reader is the only thing that feeds either, so its
        # exit is the last chance to state what they were holding.
        self._flush_dropped_frames()
        self._flush_roster_overflow()


def _notification_aliases(self: AcpRuntime) -> NotificationAliases:
    """This host's inbound method spellings, or none when no harness serves it.

    The reader loop is the one seam that must not demand a harness. It is
    driven for every id in ``ACP_BACKENDS_KNOWN``, two of which the
    shared-process runtime has no harness for, and a refusal inside the single
    stdout owner would mark the runtime dead and take every multiplexed session
    with it. The SPAWN is where an unserved backend is refused, before any
    process exists.

    Declaring no aliases DROPS the three aliasable events rather than reading
    them as kiro's, which is the direction that cannot mistake one host for
    another. Routing a frame that names its own session sits upstream of this
    and is unaffected.
    """
    try:
        return self._harness.notification_aliases
    except ValueError:
        return NotificationAliases()
