"""The session's MCP servers.

The launch roster the liveness oracle attributes a tool by, the init drain, the readiness
wait, OAuth and sign-in offers, and the failed-server reattempt.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_config_options``, ``_mcp_launches``,
``_mcp_reattempt_counts``, ``_mcp_reattempt_waiting``, ``_mcp_sign_in_completed``,
``_mcp_sign_in_dropped``, ``_mcp_sign_in_last_offered``, ``_mcp_sign_in_needed``,
``_oauth_emitted_servers``, ``_pending_oauth_requests``.

Instance fields read: ``_mcp_report``, ``_queue``, ``_runtime``, ``_session_id``,
``_watchdog``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.client import _is_safe_oauth_url
from kiro_crew.acp.mcp_reattempt import MAX_ATTEMPTS as REATTEMPT_MAX
from kiro_crew.acp.mcp_reattempt import is_recoverable_auth_failure
from kiro_crew.acp.mcp_session_report import (
    BUCKET_CAP,
    NAME_CAP,
    KasMcpReadiness,
    McpSessionReport,
    sanitize_sink_text,
)
from kiro_crew.acp.session_handle_runtime_contract import (
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
)
from kiro_crew.acp.types import (
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_KAS_MCP_STATUS,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# Bounds on one session's MCP launch roster (``mcp_launch_roster``), applied where
# it is RETAINED. The population is the session's MCP servers, which the session
# report already bounds by ``BUCKET_CAP`` servers and ``NAME_CAP`` name length, so
# the roster shares those two. The argv bounds are this structure's own: a real
# launch is a command and a few dozen arguments at most, and an entry past them is
# refused whole, never truncated, because a truncated argv matches a different
# process (or none) and attribution must not rest on it.
_LAUNCH_MAX_ARGS = 64
_LAUNCH_MAX_TOKEN = 4096
# One argv per source naming the server (the agent spec, the wire array), with
# room for a wire array that names a server twice.
_LAUNCHES_PER_SERVER = 4


def _launch_argv(entry: Mapping[str, Any]) -> tuple[str, ...] | None:
    """``(command, *args)`` for one stdio server entry, or None.

    None for a remote (``url``) server, which has no process, and for an entry
    whose launch cannot be predicted: no command, or ``args`` that is not a list
    of strings (the host could not start it as written either).
    """
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    args = entry.get("args", ())
    if args is None:
        args = ()
    if not isinstance(args, (list, tuple)) or not all(isinstance(a, str) for a in args):
        return None
    return (command, *args)


def mcp_launch_roster(
    spec: object, wire_servers: object
) -> tuple[dict[str, tuple[tuple[str, ...], ...]], int]:
    """One session's :data:`~kiro_crew.acp.liveness.LaunchRoster`, and how many
    stdio entries it refused for a bound. Pure, and never raises.

    *spec* is the agent spec the session runs (its ``mcpServers`` map is what the
    host mounts natively); *wire_servers* is the ``mcpServers`` array the session
    sent. Either may be None or malformed, which contributes nothing. An entry is
    refused whole -- no name, no argv kept -- when its name is past ``NAME_CAP``,
    its argv past :data:`_LAUNCH_MAX_ARGS` arguments or :data:`_LAUNCH_MAX_TOKEN`
    characters in any one string, its server past ``BUCKET_CAP`` servers, or its
    launch past :data:`_LAUNCHES_PER_SERVER` for that server. A refused server is
    simply not found by the scan, which keeps its full window.
    """
    roster: dict[str, list[tuple[str, ...]]] = {}
    refused = 0

    def _add(name: object, entry: object) -> None:
        nonlocal refused
        if not isinstance(name, str) or not name or not isinstance(entry, Mapping):
            return
        argv = _launch_argv(entry)
        if argv is None:
            return
        oversized = len(argv) > _LAUNCH_MAX_ARGS + 1 or any(
            len(token) > _LAUNCH_MAX_TOKEN for token in argv
        )
        known = roster.get(name)
        if not oversized and known is not None and argv in known:
            return
        if (
            oversized
            or len(name) > NAME_CAP
            or (known is None and len(roster) >= BUCKET_CAP)
            or (known is not None and len(known) >= _LAUNCHES_PER_SERVER)
        ):
            refused += 1
            return
        roster.setdefault(name, []).append(argv)

    try:
        servers = spec.get("mcpServers") if isinstance(spec, Mapping) else None
        if isinstance(servers, Mapping):
            for name, entry in servers.items():
                _add(name, entry)
        if isinstance(wire_servers, (list, tuple)):
            for element in wire_servers:
                if isinstance(element, Mapping):
                    _add(element.get("name"), element)
    except Exception:
        logger.debug("MCP launch roster unreadable", exc_info=True)
        return {}, 0
    return {name: tuple(argvs) for name, argvs in roster.items()}, refused


# MCP-server-init drain (parity with AcpClient._drain_notifications): after
# set_mode, briefly consume the session queue so MCP-init/oauth/config frames
# are processed before the first prompt, instead of racing into the first turn.
_MCP_DRAIN_DURATION = 1.0
_MCP_DRAIN_IDLE_EXIT = 0.25
# Hard ceiling while NO MCP server has reported yet. The idle shortcut is only
# meaningful once reporting has begun — before the first registration frame,
# queue silence just means the server is still booting (an npx-based stdio
# server spends seconds on npm resolution plus a Node boot before emitting
# anything), so the drain keeps waiting up to this ceiling instead. Sized to
# cover a realistic npx cold start (npm resolve + Node boot, observed 1-6s)
# while bounding the cost for a session whose agent config has no MCP servers
# at all — the one case that pays the full ceiling, since nothing ever arms
# the idle exit. Sessions with fast servers are unaffected: their registration
# frames are staged during session/new and arm the idle exit immediately.
_MCP_DRAIN_NO_REPORT_CEILING = 6.0
# Notification actions that count as "an MCP server reported in" for the
# drain's arming logic. OAuth requests count: a server that asks for OAuth has
# booted and reached its auth step, which is the same liveness signal.
_MCP_DRAIN_REPORT_ACTIONS = frozenset(
    {
        "mcp_server_initialized",
        "mcp_server_init_failure",
        "mcp_oauth_request",
    }
)
_SENTINEL = object()

#: How long a declared-governed start waits before it may start without Crew's
#: managed servers: the same bound the init drain gives a first report.
GOVERNED_SETTLE_SECS = 6.0
#: Failure text on the MCP report for a managed server a governed start skipped.
GOVERNED_DROP_REASON = (
    "not started by the host: registry mode is on and a governed MCP profile drops "
    "Crew's servers. This session runs without it."
)


def _remote_flat_launches(self: AcpSessionHandle) -> dict[str, tuple[tuple[str, ...], ...]] | None:
    """The launch roster declared to the oracle's socket scan, or None while
    the ``remote_flat`` window is off.

    None reads as undeclared, so with ``watchdog.remote_flat_probe_secs`` at
    0 the oracle never tags ``remote_flat`` and the evidence, the metric
    bucket and the window all stay as they were. Read per call, so a config
    reload that turns the key on or off takes effect on the next probe.
    """
    if self._watchdog.remote_flat_probe_secs <= 0:
        return None
    return self._mcp_launches


def record_mcp_launches(self: AcpSessionHandle, spec: Any, wire_servers: Any) -> None:
    """Record how this session launched its stdio MCP servers.

    *spec* is the agent spec the session runs and *wire_servers* the final
    ``mcpServers`` array it sent; both were already read by the runtime, so
    this is in-memory, synchronous and never raises. Replaces any earlier
    roster: ``session/load`` re-declares the session's servers. Entries the
    roster refuses for a bound are counted and logged once per recording, so
    a server missing from the scan is never silent.
    """
    try:
        roster, refused = mcp_launch_roster(spec, wire_servers)
    except Exception:
        logger.debug("session %s: MCP launch roster not recorded", self._session_id, exc_info=True)
        roster, refused = {}, 0
    self._mcp_launches = roster
    if refused:
        logger.warning(
            "session %s: %d MCP server launch(es) past the roster bounds are not "
            "recorded; the remote_flat window cannot attribute a call to them",
            self._session_id,
            refused,
        )


def queued_frame_count(self: AcpSessionHandle) -> int:
    """How many frames are waiting on this session's queue right now.

    For a caller that has to decide which frames predate a request it is
    about to send: read this first, then send. The answer is only meaningful
    at that instant, which is why it is the caller's to take rather than
    something ``drain_init`` re-derives later.
    """
    return self._queue.qsize()


def _owns_mcp_frame(self: AcpSessionHandle, msg: JsonRpcMessage) -> bool:
    """Whether *msg* names THIS session as the server registration's owner.

    The one spelling of MCP-frame ownership on the shared runtime, used by
    both the raw-report path and the events that feed the live one, so the
    two cannot answer it differently.

    A POSITIVE test, deliberately: the runtime's ``fanout_no_owner`` marks a
    sessionless frame only once more than one queue is registered, because
    its original consumer (the subagent idle-stall clock) is right to treat
    a lone session as the sole owner of whatever arrives. This view is not —
    it publishes server names and failure reasons as "what THIS session
    mounted", so a co-tenant that emits a sessionless frame before it has
    registered its own queue would otherwise have its servers attributed
    here. Requiring the frame to name us refuses that regardless of how many
    queues exist, and it stays correct when a third transport arrives.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    return bool(self._session_id) and params.get("sessionId") == self._session_id


def _accept_oauth_request(self: AcpSessionHandle, msg: JsonRpcMessage) -> dict[str, str] | None:
    """Validate and deduplicate one MCP OAuth notification."""
    params = msg.params if isinstance(msg.params, dict) else {}
    server_name = str(params.get("serverName") or params.get("name") or "")
    oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
    if not _is_safe_oauth_url(oauth_url):
        if oauth_url:
            logger.warning(
                "ACP: refusing unsafe MCP OAuth URL for %s",
                server_name or "(unknown)",
            )
        return None
    if not server_name:
        logger.warning("ACP: dropping MCP OAuth request with empty serverName")
        return None
    if server_name in self._oauth_emitted_servers:
        logger.debug("ACP: dropping duplicate MCP OAuth request for %s", server_name)
        return None
    self._oauth_emitted_servers.add(server_name)
    return {"serverName": server_name, "oauthUrl": oauth_url}


def pop_pending_oauth_requests(self: AcpSessionHandle) -> list[dict[str, str]]:
    """Drain OAuth requests captured while this session initialized."""
    pending = list(self._pending_oauth_requests)
    self._pending_oauth_requests.clear()
    return pending


def mcp_session_report(self: AcpSessionHandle) -> McpSessionReport:
    """This session's MCP registration report — parity with AcpClient.

    Does NOT drain: the report is the session's standing answer to "which
    servers actually started here". An unreported server means *not
    reported*, never *not mounted*.
    """
    return self._mcp_report


async def wait_mcp_ready(
    self: AcpSessionHandle,
    required: tuple[str, ...],
    timeout: float,
    *,
    stale_report_frames: int = 0,
    tool_policy: dict[str, Any] | None = None,
    injected: frozenset[str] = frozenset(),
    governed: bool = False,
) -> None:
    """Wait for KAS's active managed roster and catalog, or fail before prompting.

    ``injected`` is the subset of ``required`` that travelled in the request's
    own ``mcpServers`` array; see :class:`KasMcpReadiness`.

    *governed* means the operator declared registry mode. Then at
    :data:`GOVERNED_SETTLE_SECS`, and again at the deadline, if every pending
    server is one the host never named (:attr:`KasMcpReadiness.never_reported`),
    the session starts: each such server is recorded failed with
    :data:`GOVERNED_DROP_REASON`. ``kas_agents`` already logs the drop when it
    projects the agent, so nothing more is logged here. A server the host did name keeps the
    full *timeout*, so a cold server still connecting is not cut short. Every
    other guard is unchanged: a failed, disabled, auth-failed or provenance-less
    server still raises, a foreign same-named entry or a server still connecting
    still times out at the deadline, and runtime death still raises.
    """
    readiness = KasMcpReadiness(self._session_id, required, tool_policy, injected)
    self._mcp_report.include_configured(required)
    start = time.monotonic()
    deadline = start + timeout
    # The governed checkpoint: the first moment the never-named check may start
    # the session. Past it (or without a reason) only the deadline remains.
    checkpoint = (
        start + GOVERNED_SETTLE_SECS if governed and GOVERNED_SETTLE_SECS < timeout else None
    )
    stale = max(0, stale_report_frames)
    while readiness.pending:
        now = time.monotonic()
        if checkpoint is not None and now >= checkpoint:
            checkpoint = None
            if _record_dropped(self._mcp_report, readiness, governed):
                return
        remaining = (checkpoint if checkpoint is not None else deadline) - now
        if checkpoint is None and remaining <= 0:
            if _record_dropped(self._mcp_report, readiness, governed):
                return
            raise _readiness_timeout(readiness, timeout)
        try:
            msg = await asyncio.wait_for(self._queue.get(), max(0.0, remaining))
        except asyncio.TimeoutError as exc:
            # The wait itself ends the stage it was bounded by, so the loop never
            # re-waits on a clock that has not moved.
            stage_was_checkpoint = checkpoint is not None
            checkpoint = None
            if _record_dropped(self._mcp_report, readiness, governed):
                return
            if stage_was_checkpoint:
                continue
            raise _readiness_timeout(readiness, timeout) from exc
        if msg is None:
            self._queue.put_nowait(None)
            raise AcpRuntimeDead("Runtime exited while waiting for managed MCP readiness")
        try:
            # Config/OAuth side effects also belong to external servers and
            # pre-mode frames. They must not become readiness requirements.
            self._apply_init_notification(msg, classify_notification(msg))
        except Exception:
            logger.debug("error processing init notification", exc_info=True)
        if stale:
            stale -= 1
            continue
        self._mcp_report.record_frame(msg, owned=self._owns_mcp_frame(msg))
        readiness.record(msg)
        if readiness.failure:
            raise AcpRuntimeError(f"KAS managed MCP initialization failed: {readiness.failure}")
    for name in required:
        self._mcp_report.record_event(EVENT_MCP_SERVER_INITIALIZED, name)


def _record_dropped(report: McpSessionReport, readiness: KasMcpReadiness, governed: bool) -> bool:
    """On a governed start, record the never-named servers dropped; False otherwise.

    Ready servers are recorded initialized, as a satisfied barrier does.
    """
    absent = readiness.never_reported if governed else ()
    if not absent:
        return False
    for name in readiness.required:
        if name in absent:
            report.record_event(EVENT_MCP_SERVER_INIT_FAILURE, name, GOVERNED_DROP_REASON)
        else:
            report.record_event(EVENT_MCP_SERVER_INITIALIZED, name)
    return True


def _readiness_timeout(readiness: KasMcpReadiness, timeout: float) -> AcpRequestTimeout:
    """The deadline error, pointing at the doctor when the host never named a server.

    A server the host never named at all is what a governed MCP profile produces
    when registry mode is not declared (a declared install starts instead, see
    :func:`_record_dropped`). Silence alone is not proof of
    that, so the start still fails; the message points at ``kirocrew doctor``,
    whose MCP Governance section checks the account's identity before it advises
    the setting, rather than advising a setting that withholds every agent-spec
    server.
    """
    message = f"KAS managed MCP readiness timed out after {timeout:g}s: {readiness.pending}"
    if readiness.never_reported:
        message += (
            ". The host never reported these servers; run `kirocrew doctor` and read its "
            "MCP Governance section to see whether an admin MCP registry applies to this "
            "account."
        )
    return AcpRequestTimeout(message)


async def wait_for_declared_mcp(self: AcpSessionHandle, timeout: float) -> tuple[str, ...]:
    """Hold until every server the agent spec declares has reported; never raises.

    For an unattended run, before its first prompt. ``drain_init`` stops a
    second after the first server reports, so a slow server can still be
    booting when the prompt goes out, and that turn runs without its tools.
    Nobody is there to notice or retry, so this keeps draining init frames
    (exactly as ``drain_init`` handles them) until each declared server has
    reported initialized, failed or awaiting authorization, or *timeout*
    seconds pass. Returns the declared servers that are not ready, in spec
    order; empty when all are.
    """
    report = self._mcp_report
    deadline = time.monotonic() + max(0.0, timeout)
    while report.declared_unreported():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(self._queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if msg is None:
            # Runtime died: re-poison so the turn that follows sees it.
            await self._queue.put(None)
            break
        try:
            report.record_frame(msg, owned=self._owns_mcp_frame(msg))
            self._apply_init_notification(msg, classify_notification(msg))
        except Exception:
            logger.debug("declared MCP wait: error processing init frame", exc_info=True)
    return report.declared_not_ready()


def _note_mcp_sign_in_status(self: AcpSessionHandle, msg: JsonRpcMessage, *, offer: bool) -> None:
    """Track which of this session's servers need an OAuth sign-in.

    Read from this session's own ``_kiro/mcp/status`` snapshots. A server is
    added on ``failedAuthorization`` and removed once it connects, is
    disabled, or leaves the snapshot. A tracked server that connects is
    also recorded as a completed sign-in for the dispatch loop to yield as
    ``EVENT_MCP_SERVER_INITIALIZED``. With ``offer`` a sign-in is then
    offered; the dispatch loop passes it, because a link started there
    arrives while the loop is reading. Session start and the pre-turn drain
    read without offering: a link they started would land between turns,
    and a start that fails is torn down.

    Bounded like the session's MCP report reads the same frame: at most
    ``BUCKET_CAP`` entries, and a name over ``NAME_CAP`` is skipped rather
    than truncated, because a cut name names a server the engine does not
    have. The completed set is bounded the same way: it is drained only by
    the dispatch loop, so a completion that would take it past
    ``BUCKET_CAP`` is dropped rather than held. Every dropped entry is
    counted and the count is logged when it changes, so a server that is
    never offered a sign-in, or whose completion is never yielded, is named
    as such.
    """
    if not msg.is_method(METHOD_KAS_MCP_STATUS) or not self._owns_mcp_frame(msg):
        return
    servers = msg.params.get("servers")
    if not isinstance(servers, list):
        return
    present: set[str] = set()
    dropped = max(0, len(servers) - BUCKET_CAP)
    for server in servers[:BUCKET_CAP]:
        if not isinstance(server, dict) or not isinstance(server.get("name"), str):
            continue
        name = server["name"]
        if not name or len(name) > NAME_CAP:
            dropped += 1
            continue
        present.add(name)
        if server.get("failedAuthorization") is True:
            self._mcp_sign_in_needed.add(name)
            self._mcp_reattempt_waiting.discard(name)
        elif server.get("status") in ("connected", "disabled"):
            tracked = name in self._mcp_sign_in_needed or (
                name in self._mcp_reattempt_waiting and self._mcp_reattempt_counts.get(name, 0) > 0
            )
            if server.get("status") == "connected" and tracked:
                # Only a server that was being signed in to (or re-attempted)
                # counts as a completion; an ordinary connected server yields
                # nothing.
                # A full set refuses the name: between-turn reads never
                # drain it, so an unbounded add grows with every snapshot.
                if (
                    name in self._mcp_sign_in_completed
                    or len(self._mcp_sign_in_completed) < BUCKET_CAP
                ):
                    self._mcp_sign_in_completed.add(name)
                else:
                    dropped += 1
            self._mcp_sign_in_needed.discard(name)
            self._mcp_reattempt_waiting.discard(name)
        elif (
            server.get("status") == "failed"
            and name not in self._mcp_sign_in_needed
            and name not in self._mcp_reattempt_waiting
            and is_recoverable_auth_failure(server.get("errorMessage"))
        ):
            self._mcp_reattempt_waiting.add(name)
    self._mcp_sign_in_needed &= present
    self._mcp_reattempt_waiting &= present
    self._sync_mcp_reattempting()
    if dropped != self._mcp_sign_in_dropped:
        self._mcp_sign_in_dropped = dropped
        if dropped:
            logger.warning(
                "MCP sign-in tracking skipped %d status entr%s on session %s "
                "(over %d servers, a name over %d characters, or %d "
                "completions still to yield); those servers are not offered "
                "a sign-in or their completion is not yielded",
                dropped,
                "y" if dropped == 1 else "ies",
                self._session_id,
                BUCKET_CAP,
                NAME_CAP,
                BUCKET_CAP,
            )
    if offer:
        self._offer_mcp_sign_in()


def _offer_mcp_sign_in(self: AcpSessionHandle) -> None:
    """Ask the runtime to start one pending sign-in, if it can take one.

    Called at turn start, after the pre-turn drain, and from the dispatch
    loop on a status snapshot: both points have a reader for the link the
    engine sends back. Never called during a session-start drain.

    The server's banner dedupe is cleared first: its earlier link is dead
    once a new sign-in starts, and the new link must not be dropped as a
    duplicate.
    """
    begin = getattr(self._runtime, "begin_mcp_sign_in", None)
    if begin is None or not self._session_id:
        return
    names = sorted(self._mcp_sign_in_needed)
    start = next(
        (i for i, name in enumerate(names) if name > self._mcp_sign_in_last_offered),
        0,
    )
    for name in names[start:] + names[:start]:
        if begin(self._session_id, name):
            self._mcp_sign_in_last_offered = name
            self._oauth_emitted_servers.discard(name)
            return


def _sync_mcp_reattempting(self: AcpSessionHandle) -> None:
    """Tell the MCP report which failed servers this session still re-attempts.

    Only on a host that can reconnect one server; elsewhere the set is
    empty and every credential failure is named for a new session.
    """
    report = getattr(self, "_mcp_report", None)
    if report is None:
        return
    pending: set[str] = set()
    if self._runtime_reattempts_mcp():
        pending = {
            name
            for name in self._mcp_reattempt_waiting
            if self._mcp_reattempt_counts.get(name, 0) < REATTEMPT_MAX
        }
    report.set_reattempting(pending)


def _runtime_reattempts_mcp(self: AcpSessionHandle) -> bool:
    """Whether this session's host re-attempts a credential-failed server.

    Asks the runtime's capability rather than whether the method exists:
    every runtime has ``begin_mcp_reattempt``, and on a host without the
    reset request it refuses every call, so a server counted as pending
    there would never be named for a new session.
    """
    if getattr(self._runtime, "begin_mcp_reattempt", None) is None:
        return False
    return getattr(self._runtime, "reattempts_mcp_servers", False) is True


def _reattempt_failed_mcp_servers(self: AcpSessionHandle) -> None:
    """Re-attempt servers that failed for want of a credential.

    Called at turn start, beside the sign-in offer: by then the user or
    the agent may have obtained the credential the server lacked. Each
    waiting server is re-attempted at most ``REATTEMPT_MAX`` times per
    session, and at most ``BUCKET_CAP`` distinct servers are ever counted,
    so the budget holds however many names the engine reports. A healthy
    session has no waiting server and does no work here. Nothing is sent
    while an OAuth sign-in holds the runtime's slot or on a host without
    the reset request; the runtime refuses those, no attempt is spent, and
    the server waits for the next turn.
    """
    if not self._mcp_reattempt_waiting or not self._session_id:
        return
    begin = getattr(self._runtime, "begin_mcp_reattempt", None)
    if begin is None or not self._runtime_reattempts_mcp():
        return
    for name in sorted(self._mcp_reattempt_waiting):
        attempts = self._mcp_reattempt_counts.get(name, 0)
        if attempts >= REATTEMPT_MAX:
            continue
        if attempts == 0 and len(self._mcp_reattempt_counts) >= BUCKET_CAP:
            continue
        if not begin(self._session_id, name):
            continue
        self._mcp_reattempt_counts[name] = attempts + 1
        if attempts + 1 >= REATTEMPT_MAX:
            self._sync_mcp_reattempting()
        logger.info(
            "MCP server %s on %s failed for a credential; re-attempt %d of %d",
            sanitize_sink_text(name, NAME_CAP),
            self._session_id,
            attempts + 1,
            REATTEMPT_MAX,
        )


def _apply_init_notification(self: AcpSessionHandle, msg: JsonRpcMessage, action: str) -> None:
    """Initialization side effects shared by the drain and readiness barrier."""
    self._note_mcp_sign_in_status(msg, offer=False)
    params = msg.params if isinstance(msg.params, dict) else {}
    if action == "update":
        update = params.get("update") or {}
        if isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
            cfg = update.get("configOptions")
            if isinstance(cfg, list):
                self._config_options = cfg
                self._sync_effort_levels()
    elif action == "mcp_oauth_request":
        request = self._accept_oauth_request(msg)
        if request is not None:
            self._pending_oauth_requests.append(request)
    elif action == "mcp_server_init_failure":
        logger.info(
            "MCP server init failure on %s: %s",
            self._session_id,
            params.get("serverName") or "",
        )


async def drain_init(
    self: AcpSessionHandle,
    duration: float = _MCP_DRAIN_DURATION,
    idle_exit: float = _MCP_DRAIN_IDLE_EXIT,
    no_report_ceiling: float | None = None,
    stale_report_frames: int = 0,
) -> None:
    """Drain MCP-init / oauth / config frames from the queue after set_mode.

    Parity with AcpClient._drain_notifications. During session setup there is
    no in-flight prompt, so every frame on this session's queue is an
    init-time notification. Draining them here keeps them out of the first
    turn's event stream and gives MCP servers a window to report in before
    the first prompt races ahead.

    The idle shortcut means "quiet AFTER the servers reported", not "quiet,
    therefore done": until the first MCP registration frame (initialized /
    init_failure / oauth_request) is observed, queue silence is treated as a
    server still booting and the drain keeps waiting, bounded by
    ``no_report_ceiling`` (defaults to ``_MCP_DRAIN_NO_REPORT_CEILING``,
    resolved at call time so tests can shrink the module constant). Once a
    report has been seen, the drain allows up to ``duration`` more and exits
    after ``idle_exit`` seconds of silence, so warm sessions — whose
    registration frames were staged during session/new — arm immediately and
    pay no extra latency. ``config_option_update`` frames refresh cached
    configOptions; OAuth requests remain drainable via
    ``pop_pending_oauth_requests``; everything else is logged/discarded.
    Best-effort — never raises.

    ``stale_report_frames``: how many frames on the queue describe the roster
    that initialized during ``session/new``. For a session whose mode was
    then SWITCHED via ``set_mode``, that is the PRE-switch agent's roster,
    and the switched-to agent's own servers may still be booting. Those
    frames are still drained and processed normally; they just neither arm
    the idle shortcut nor enter the report.

    The COUNT is the caller's to measure, and it must be read before the
    ``set_mode`` request goes out — the only moment "already queued" and
    "pre-switch" mean the same thing. Measuring it here instead would count
    the switched-to agent's own registrations, which the backend can emit
    before it answers set_mode, and consume them without recording: a
    session left at a false "no report" for as long as its servers keep
    talking.
    """
    if no_report_ceiling is None:
        no_report_ceiling = _MCP_DRAIN_NO_REPORT_CEILING
    start = time.monotonic()
    deadline = start + duration
    hard_deadline = start + max(duration, no_report_ceiling)
    # A nonpositive ceiling means "do not hold for a first report" — the
    # caller knows no MCP server can register (MCP-free runtime). The idle
    # shortcut is then active from the start, i.e. the pre-fix behavior.
    reported = no_report_ceiling <= 0.0
    # Exactly the frames the caller counted before it sent set_mode are the
    # pre-switch agent's. A count, not a test for emptiness: a queue that the
    # ACTIVE agent refills before the backlog is drained never goes empty, so
    # an emptiness test would keep the flag set for the whole drain and skip
    # recording every report the switched-to agent makes. And it comes from
    # the CALLER rather than a qsize() read here, because by the time this
    # runs the active agent's own registrations may already have landed.
    stale_frames = max(0, stale_report_frames)
    drained = 0
    while True:
        now = time.monotonic()
        limit = deadline if reported else hard_deadline
        if now >= limit:
            break
        remaining = limit - now
        try:
            msg = await asyncio.wait_for(
                self._queue.get(),
                timeout=min(remaining, idle_exit) if reported else remaining,
            )
        except asyncio.TimeoutError:
            # Armed: queue went quiet after reporting — servers are done.
            # Unarmed: the no-report ceiling elapsed with nothing to show.
            break
        if msg is None:
            # Runtime died during init — re-poison so the next consumer sees it.
            await self._queue.put(None)
            break
        drained += 1
        # This frame is the pre-switch agent's iff the snapshot still has
        # room for it; the counter is spent here so a refill cannot buy a
        # later frame the same treatment.
        stale_backlog = stale_frames > 0
        if stale_backlog:
            stale_frames -= 1
        try:
            action = classify_notification(msg)
            if not stale_backlog:
                # Deliberately narrower than the "still drained and
                # processed normally" treatment the stale backlog gets
                # otherwise: those frames describe the PRE-switch agent's
                # roster, so recording one could show a server the current
                # agent does not have as mounted here. Skipping it can
                # instead leave a server that is genuinely up looking
                # unreported until it reports again — and that is the safe
                # direction, because the report renders an unreported server
                # as "no report", never as "not mounted".
                self._mcp_report.record_frame(msg, owned=self._owns_mcp_frame(msg))
            if not reported and not stale_backlog and action in _MCP_DRAIN_REPORT_ACTIONS:
                # First server report: arm the idle shortcut and give the
                # remaining servers up to ``duration`` from this point.
                reported = True
                deadline = time.monotonic() + duration
            self._apply_init_notification(msg, action)
        except Exception:
            logger.debug("drain_init: error processing init frame", exc_info=True)
    if drained:
        logger.debug("drain_init: drained %d init frame(s) for %s", drained, self._session_id)
