"""``session/new`` end to end.

``create_session``, the hand-off of a start that outlives its budget to its collector and
the teardown of a late session, the finish every answered start shares, and the
entitlement probe session that is never prompted.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_entitlement_probe_attempt_at``, ``_entitlement_probe_result``,
``_entitlement_probe_result_at``, ``_first_session_ready``, ``_saw_delegate_launch_failure``, ``_saw_sandbox_init_failure``,
``_session_cwds``, ``_session_inits_in_flight``, ``_session_queues``, ``_start_collectors``, ``_stderr_lines``,
``recording_allowed``.

Instance fields read: ``_agent``, ``_agent_capabilities``, ``_crew_agent``, ``_dead``,
``_entitlement_probe_lock``, ``_expect_mcp_reports``, ``_initialized``,
``_mcp_gateway_overlay``, ``_member_context``, ``_pending_init_notifications``, ``_pid``,
``_pool_lookup``, ``_process``, ``_remote_session_servers``, ``_remote_workspace_agents``,
``_start_collect_timeout``, ``_wire_mode_id``, ``_work_dir``, ``_write_remote_token``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.acp import runtime_start
from kiro_crew.acp._dispatch import (
    attach_kas_custom_agents,
    build_session_new_params,
    parse_session_modes,
)
from kiro_crew.acp.kas_agents import hoist_servers_for_session
from kiro_crew.acp.runtime_session_setup import (
    _disable_check_scope,
    _pooled_session_servers_and_ref_spec,
)
from kiro_crew.acp.runtime_start import _START_COLLECT_TIMEOUT_DEFAULT, StartCollector, StartPermit
from kiro_crew.acp.session_handle import (
    AcpFrameTooLarge,
    AcpRequestTimeout,
    AcpRuntimeError,
    AcpSessionHandle,
    _load_watchdog_settings,
    advertised_models_from_session,
)
from kiro_crew.acp.session_mcp import session_mcp_server_is_disabled
from kiro_crew.acp.types import (
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    METHOD_SESSION_NEW,
    JsonRpcMessage,
)
from kiro_crew.agent_files import kas_wire_agent_id
from kiro_crew.config import live
from kiro_crew.dashboard.side_readonly_spec import unavailable_mode_explanation
from kiro_crew.providers.mirrors.registry import has_mirror
from kiro_crew.start_priority import START_QUEUE_SESSION_NEW, StartPriority, notify_start_queue

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


class AcpSessionStartTimeout(AcpRequestTimeout):
    """``session/new`` exceeded its budget while a :class:`StartCollector` still owns it.

    Distinct from a bare :class:`AcpRequestTimeout` so a caller can tell "the
    answer was lost" from "the request never went out": the session may still
    be created by the runtime, the collector holds the request id, and the
    caller may :meth:`StartCollector.adopt` the late session or let the
    collector tear it down. Re-queuing the task, or starting a dedicated
    process in its place, would leave that session running unowned.
    ``collector`` is None when the request never reached the wire (nothing to
    own).
    """

    def __init__(self, message: str, *, collector: "StartCollector | None") -> None:
        super().__init__(message)
        self.collector = collector
        # Every instance of this type is by definition a session start that did
        # not answer in time; see AcpRequestTimeout.session_start_failed.
        self.session_start_failed = True


# Named by the path callers import them from: an error chain renders each raised class
# as ``module.qualname`` (``kiro_crew.subagent._describe_exception``), and that text
# reaches the Subagents panel and tracebacks, so this module's name must not leak into it.
AcpSessionStartTimeout.__module__ = "kiro_crew.acp.runtime"


# Entitlement probe (probe_advertised_models). The probe session carries no MCP
# servers and activates no mode, so it is far cheaper than a real session start;
# the timeout is still generous because the probe runs exactly when something is
# already wrong (a rejection is being revalidated) and a loaded host must not
# turn a recoverable verdict into a spurious probe failure.
_ENTITLEMENT_PROBE_TIMEOUT = 30.0
# A fresh answer is reused for this window so a burst of rejections (several
# chats revalidating at once) costs one round-trip, not one per rejection.
_ENTITLEMENT_PROBE_TTL_SECS = 20.0


async def create_session(
    self: AcpRuntime,
    cwd: str | Path | None = None,
    agent: str | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    crew_agent: str | None = None,
    member_session_key: str = "",
    session_key: str = "",
    channel_id: str = "",
    memory_mode: str = "persistent",
    on_gate_acquired: Callable[..., None] | None = None,
    late_adopter: "Callable[[AcpSessionHandle], Awaitable[bool]] | None" = None,
    on_gate_queued: Callable[..., None] | None = None,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    bg_runtime_start: bool = False,
) -> AcpSessionHandle:
    """Create a new ACP session on this runtime. Returns a session handle.

    ``crew_agent`` is the canonical Kiro Crew identity for THIS session;
    None falls back to the runtime's own (spawn-time or rekeyed) identity.

    ``session_key`` is the Kiro Crew session that will OWN this ACP session.
    It is what makes the session's broker stubs resolvable as this session
    rather than as the runtime — see :meth:`_own_stub_session`. Empty when
    the owner is not known yet (a pooled worker claimed later), and the
    ``rekey()`` claim then carries the token.

    ``member_session_key`` marks a crew member's DM session and carries its
    session key: the dashboard session-control server is mounted as a
    session-level entry (identity via ``KIROCREW_SESSION_KEY``), and the
    KAS wire agent's projection widens to grant its tools. Empty — every
    non-member session — leaves both paths byte-identical to before.

    ``channel_id`` is the channel this session belongs to, and it reaches a
    MIRRORED host's ``mcpServers`` ELEMENTS rather than the process env: a
    codex stdio server starts from ``env_clear()`` plus an allowlist, so a
    server that reports its caller's channel can only learn it from the
    element. Empty — and unread — for a host with no mirror.

    ``session/new`` runs under the loop's :class:`SessionStartGate`
    (``agent.session_start_concurrency``). ``on_gate_queued(queue)`` fires
    immediately before the wait for a permit begins and
    ``on_gate_acquired(queue_wait_ms, queue)`` at gate EXIT, so the caller can
    PAUSE its own clock for exactly the span spent queued and resume it at
    acquisition: the queue wait is not start time. ``start_priority``
    orders this start in the gate's queue (rule: ``kiro_crew.start_priority``);
    the callbacks receive ``queue`` = :data:`START_QUEUE_SESSION_NEW`. On a ``session/new`` timeout the request
    is NOT abandoned: a :class:`StartCollector` keeps it for
    ``agent.start_collect_timeout_secs`` and either hands the late session
    to ``late_adopter`` (which returns True to keep it) or tears it down;
    the raised :class:`AcpSessionStartTimeout` carries that collector. The
    gate permit is released exactly once on every path.

    ``bg_runtime_start=True`` (only the shared ``_bg`` runtime's one-liner
    starts) takes the permit from the loop's separate one-permit ``_bg`` gate
    instead, so those starts never hold or queue for a user's permit
    (``runtime_start.bg_runtime_session_start_gate``).
    """
    if memory_mode not in {"persistent", "incognito", "temporary"}:
        raise ValueError("Invalid session memory mode")
    agent = await self._source_agent(agent)
    if memory_mode != "persistent":
        # A mixed runtime cannot attribute every raw diagnostic frame to a
        # session. Latch recording off before session/new can emit a payload.
        self.recording_allowed = False
        self._stderr_lines.clear()
    if not self._initialized:
        raise AcpRuntimeError("Runtime not initialized — call spawn() first")

    # Inject the shared gateway's broker stubs unless the caller supplied an
    # explicit list. A session-injected server outranks the same-named entry
    # in the agent spec, so this is what actually pools the servers — no file
    # is written anywhere. Empty when the gateway is disabled.
    # Resolved here rather than beside the params below, because a mirrored
    # host's projection needs it: an agent spec can live in the project, and
    # resolving a narrower spec set silently drops the ``tools`` allowlist that
    # spec declared. One call either way -- no path resolves it twice.
    session_work_dir = await self._session_work_dir(cwd)
    denied_tools: frozenset[tuple[str, str]] = frozenset()
    # Defined for every path, not just the array-building one: a caller that
    # supplied its own array never enters the block below, and the member
    # confirmation asks whether a MIRROR built this session's array.
    mirrored: Any = None
    mirrored_snapshot: Any = None
    consumed_spec: Any = None
    ref_spec: Any = None
    if mcp_servers is None:
        # A mirrored host takes its whole array from the mirror; every other host
        # takes the pooled stubs it always took. Which one is a synchronous
        # in-memory registry read, so the kiro path reaches a comparison and stops
        # rather than awaiting an adapter seam (H13). Both branches resolve their
        # own I/O off the event loop: the lookup stats/reads files, and blocking
        # the loop stalls every other session's I/O.
        if has_mirror(self.acp_backend):
            mirrored = await self._mirrored_session_mcp(
                agent,
                work_dir=session_work_dir,
                session_key=session_key,
                channel_id=channel_id,
            )
        if mirrored is not None:
            mcp_servers = mirrored.servers
            stub_token = mirrored.stub_token
            denied_tools = mirrored.denied_tools
            mirrored_snapshot = mirrored.derived_spec_snapshot
            consumed_spec = mirrored.agent_spec
            ref_spec = mirrored.ref_spec
        else:
            pool_agent, launch_spec = self._pool_lookup(agent or self._agent)
            pooled, ref_spec = await asyncio.to_thread(
                _pooled_session_servers_and_ref_spec,
                self._mcp_gateway_overlay,
                pool_agent,
                self.acp_backend,
                session_work_dir,
                launch_spec,
            )
            mcp_servers = await self._unpooled_control_planes(
                pooled, agent or self._agent, session_work_dir
            )
            mcp_servers, stub_token = await self._own_stub_session(mcp_servers, session_key)
    else:
        # An explicit array is the caller's own composition (a mirror's
        # projection, a test double); it is not this method's to re-key, and it
        # carries no spec snapshot, so the unresolved-ref guard has nothing to
        # judge against and stays silent -- as the client does with no warmed
        # snapshot.
        stub_token = ""
    member_withheld = False
    # Whether the member session-control entry is IN the array, the one fact
    # the context builder needs to teach the session_* tools truthfully.
    member_mounted = False
    # False for every non-member session, set without an awaited call so the
    # Kiro construction path is untouched by this capability (H13).
    panel_mounted = False
    if member_session_key:
        # circular import: members' module graph is heavy; resolved at call
        # time like the projection seams below.
        from kiro_crew.members import MEMBER_DISPATCH_SERVER, member_dispatch_session_server

        # The operator's switch-off of the dashboard server, asked on THIS path
        # too. ``disabled`` has no per-tool or per-call spelling, so a harness
        # handed the server cannot refuse a call to it, and the ``tools``
        # allowlist that keeps a disabled server out of a projected array does
        # not reach an entry appended here. Only a member session reaches this
        # branch, so no other host gains a suspension point (H13).
        member_withheld = await asyncio.to_thread(
            session_mcp_server_is_disabled,
            MEMBER_DISPATCH_SERVER,
            agent or self._agent,
            work_dir=_disable_check_scope(self.acp_backend, session_work_dir),
        )
        member_entry = (
            None
            if member_withheld
            else await asyncio.to_thread(
                member_dispatch_session_server, member_session_key, stub_token
            )
        )
        member_mounted = member_entry is not None
        if member_entry is not None:
            # Session-level entries outrank same-named spec entries, so drop
            # any stub for the same server rather than registering it twice.
            mcp_servers = [e for e in mcp_servers if e.get("name") != member_entry["name"]] + [
                member_entry
            ]
        elif member_withheld:
            logger.warning(
                "member session %s: %s is switched off for this session "
                "(disabled), so session control is not mounted — the DM thread "
                "runs as plain chat; re-enable that server to restore it",
                member_session_key,
                MEMBER_DISPATCH_SERVER,
            )
        else:
            logger.warning(
                "member session %s: dashboard server unresolved — the DM "
                "thread runs as plain chat this session",
                member_session_key,
            )
        # INSIDE the member branch, like the mount above it: a session with no
        # member key reaches no part of this composition, so the Kiro
        # construction path gains no conditional, no awaited step and no new
        # failure mode from the panel capability (harness-parity H13).
        mcp_servers, panel_mounted = await self._mount_member_panel(
            mcp_servers,
            member_session_key=member_session_key,
            agent_name=agent or self._agent,
            session_work_dir=session_work_dir,
            stub_token=stub_token,
        )
    # The agent to run: an explicit request, else the runtime default. KAS
    # has no --agent spawn flag, so its default must be BOTH injected (below)
    # and activated (via set_mode after session/new); the kiro default is
    # already active from the --agent spawn.
    active_agent = agent or self._agent
    # Adapter-only seam: _kas_custom_agents returns None on the kiro backend,
    # so the kiro construction path gains no conditional, no new required
    # argument, and no new failure mode (harness-parity H13).
    kas_extras = await self._kas_custom_agents(
        active_agent,
        # The GRANT follows the same answer the mount does. This widening adds
        # ``@kirocrew-dashboard`` to the KAS agent's ``tools`` and merges the member
        # verbs into ``allowedTools``, which is an approval-free path: a grant that
        # outlived the withhold would leave the switched-off server both named and
        # pre-approved on the very session that is not mounting it.
        member_dispatch=bool(member_session_key) and not member_withheld,
        # Same rule, its own withhold: see _mount_member_panel, which answers
        # the mount and the grant together so the two cannot disagree.
        crew_panel=panel_mounted,
        session_key=session_key,
    )
    kas_agents = kas_extras.custom_agents
    # The generation the wire payload was built from, or None when this host takes its
    # agent at spawn time. Consumed by the activation bracket below.
    payload_snapshot = kas_extras.derived_spec_snapshot
    # The vault identity the request goes out under, for the KAS catalog recorder;
    # read by the harness with the payload, ``""`` on every host but KAS.
    request_identity = kas_extras.vault_identity
    # Crew's managed servers travel in the session-level array, the one
    # declaration site the captured 2.18.0 release honours over a same-named
    # global/workspace entry and 2.20.0 reports as the client's own (see
    # ``hoist_servers_for_session``). The kiro path returns None here and is
    # untouched.
    kas_agents, mcp_servers = hoist_servers_for_session(
        kas_agents, active_agent, mcp_servers, session_token=stub_token
    )
    # The host's last word on its own tool surface. A host that reads an
    # agent spec passes the list straight back; one that has nothing else
    # describing its tools narrows it to the transports it advertised at
    # handshake, because a single unsupported element can cost the whole
    # session/new rather than that one server.
    # Bound ONCE and threaded to every consumer that means "what this session
    # was sent": the request, the stall diagnostic, the session report and the
    # unresolved-ref guard. The pre-filter roster is not that; a host that
    # narrows its array would otherwise be reported as having been sent servers
    # the wire never carried, and the guard would judge refs against them.
    await self._write_remote_token()
    wire_servers = self._remote_session_servers(
        self._harness.session_mcp_servers(mcp_servers, agent_capabilities=self._agent_capabilities)
    )
    kas_agents = self._remote_workspace_agents(kas_agents, wire_servers)
    params = build_session_new_params(
        session_work_dir,
        mcp_servers=wire_servers,
        kas_custom_agents=kas_agents,
    )

    projected_sources: dict[str, str] = {}
    if self._member_context:
        from kiro_crew.member_essential_context import projected_resource_documents

        for definition in kas_agents or ():
            if definition.get("id") == active_agent:
                projected_sources.update(
                    await asyncio.to_thread(
                        projected_resource_documents, definition, str(session_work_dir)
                    )
                )

    budget = await self._session_start_budget()
    # The init scope opens BEFORE the admission gate, not after: the gate's
    # queue is unbounded in practice, and ``has_active_or_initializing_
    # sessions`` is the predicate every recycle and displacement decision
    # asks -- a runtime whose claim is still queued behind the gate must
    # already read as busy, or a concurrent spawn-identity displacement
    # pass sees it idle and kills it under the claim. A gate failure or a
    # cancellation landing in the wait closes the scope on the way out.
    self._session_inits_in_flight += 1
    try:
        # Gate BEFORE the request goes out, released exactly once: on success
        # right after the answer (the rest of session setup is not what the
        # gate protects), on a timeout by the collector that now owns the
        # request, on any other failure here.
        gate = await (
            runtime_start.bg_runtime_session_start_gate()
            if bg_runtime_start
            else runtime_start.session_start_gate()
        )
        notify_start_queue(logger, on_gate_queued, START_QUEUE_SESSION_NEW)
        permit = await gate.acquire(start_priority)
        try:
            notify_start_queue(
                logger, on_gate_acquired, permit.queue_wait_ms, START_QUEUE_SESSION_NEW
            )
        except BaseException:
            permit.release()
            raise
    except BaseException:
        self._finish_session_init("")
        raise
    session_id = ""
    # Start latency is measured from gate EXIT: the queue wait is admission's
    # cost, not the runtime's, and the adaptive controller reads these
    # samples for its ``start_latency`` / ``timeouts`` signals.
    start_t0 = time.monotonic()
    # Wall time the request went out, for the session-open mount check.
    request_sent_at = time.time()
    try:
        resp = await self._send_and_await(METHOD_SESSION_NEW, params, timeout=budget)
        session_id = str(resp.get("sessionId") or "")
        permit.release()
        if not session_id:
            # One failed start, one sample: the ``except`` below records it.
            raise AcpRuntimeError(f"session/new did not return sessionId: {resp}")
        runtime_start._record_session_start(start_t0, ok=True)
    except AcpFrameTooLarge as exc:
        # The reply was dropped as oversize: a start that failed, never one
        # a retry fixes. Tagged like a stalled start so a self-driving caller
        # counts the streak and backs off instead of re-issuing session/new.
        # Cleaned up exactly like the catch-all below, which this arm precedes.
        exc.session_start_failed = True
        permit.release()
        runtime_start._record_session_start(start_t0, ok=False)
        raise
    except AcpRequestTimeout as exc:
        # A start that outlived its budget is the congestion signal the
        # controller keys its decrease on (attributable timeout).
        runtime_start._record_session_start(start_t0, ok=False, attributable_timeout=True)
        # Read the staged MCP reports before the finally below clears them.
        stalled = self._session_start_stalled(exc, METHOD_SESSION_NEW, wire_servers)
        collector = self._collect_late_start(
            exc,
            permit,
            agent=agent,
            crew_agent=crew_agent,
            kas_agents=kas_agents,
            mcp_servers=wire_servers,
            budget=budget,
            stub_token=stub_token,
            denied_tools=denied_tools,
            mirrored_snapshot=mirrored_snapshot,
            consumed_spec=consumed_spec,
            mirror_used=mirrored is not None,
            ref_spec=ref_spec,
            active_agent=active_agent,
            session_work_dir=session_work_dir,
            projected_sources=projected_sources,
            payload_snapshot=payload_snapshot,
            late_adopter=late_adopter,
            memory_mode=memory_mode,
            session_key=session_key,
            member_dispatch_mounted=member_mounted,
            request_sent_at=request_sent_at,
            request_identity=request_identity,
        )
        if collector is None:
            permit.release()
        raise AcpSessionStartTimeout(str(stalled), collector=collector) from exc
    except BaseException:
        permit.release()
        runtime_start._record_session_start(start_t0, ok=False)
        raise
    finally:
        buffered_init = self._finish_session_init(session_id)

    return await self._finish_create_session(
        session_id,
        resp,
        buffered_init=buffered_init,
        memory_mode=memory_mode,
        agent=agent,
        crew_agent=crew_agent,
        kas_agents=kas_agents,
        mcp_servers=wire_servers,
        budget=budget,
        stub_token=stub_token,
        denied_tools=denied_tools,
        mirrored_snapshot=mirrored_snapshot,
        consumed_spec=consumed_spec,
        mirror_used=mirrored is not None,
        ref_spec=ref_spec,
        active_agent=active_agent,
        session_work_dir=session_work_dir,
        projected_sources=projected_sources,
        payload_snapshot=payload_snapshot,
        session_key=session_key,
        member_dispatch_mounted=member_mounted,
        request_sent_at=request_sent_at,
        request_identity=request_identity,
    )


def _collect_late_start(
    self: AcpRuntime,
    exc: AcpRequestTimeout,
    permit: StartPermit,
    *,
    agent: str | None,
    crew_agent: str | None,
    kas_agents: Any,
    mcp_servers: list[dict[str, Any]],
    budget: float,
    stub_token: str,
    denied_tools: frozenset[tuple[str, str]],
    mirrored_snapshot: Any,
    consumed_spec: Any = None,
    mirror_used: bool = False,
    ref_spec: Any,
    active_agent: str,
    session_work_dir: str | Path,
    projected_sources: dict[str, str],
    payload_snapshot: Any,
    late_adopter: "Callable[[AcpSessionHandle], Awaitable[bool]] | None",
    memory_mode: str = "persistent",
    session_key: str = "",
    member_dispatch_mounted: bool = False,
    request_sent_at: float | None = None,
    request_identity: str = "",
) -> StartCollector | None:
    """Hand a timed-out ``session/new`` to a :class:`StartCollector`.

    None when the request never went out (a patched or pre-wire failure
    leaves no ``req_id`` on the exception): there is nothing to own, and
    the caller releases the permit itself. The collector's cleanup budget
    is read from the live snapshot when armed, else the cached value from
    the last off-loop resolve (default 300s) -- a collector is created on
    the timeout path and must not block on disk there.
    """
    req_id = getattr(exc, "req_id", None)
    future = getattr(exc, "adopted_future", None)
    if req_id is None or future is None:
        return None
    # Hand the permit over only while the gate still has a reserved slot for
    # a start that has not gone out. Denied, the permit is released here and
    # the collector gets none -- it keeps owning the request either way, so
    # the late session is still adopted or torn down; what it stops doing is
    # holding a permit for up to ``start_collect_timeout_secs`` that a fresh
    # session/new is queued behind (see _COLLECTOR_PERMIT_HEADROOM).
    collector_permit: StartPermit | None = permit
    if not permit.hold_for_collector():
        permit.release()
        collector_permit = None
        logger.warning(
            "acp_startup_stage stage=session_new outcome=gate_permit_returned "
            "req_id=%d collector_holds=%d ceiling=%d -- the collecting "
            "population already holds the gate's collector budget, so this "
            "permit is released instead of parked",
            int(req_id),
            permit._gate.collector_holds,
            permit._gate.collector_hold_ceiling,
        )
    timeout = getattr(self, "_start_collect_timeout", None)
    if timeout is None:
        snap = live.snapshot()
        try:
            timeout = (
                max(10.0, float(snap.agent.start_collect_timeout_secs))
                if snap is not None
                else _START_COLLECT_TIMEOUT_DEFAULT
            )
        except Exception:
            timeout = _START_COLLECT_TIMEOUT_DEFAULT
    collector = StartCollector(
        self,
        int(req_id),
        future,
        permit=collector_permit,
        timeout=timeout,
        context={"agent": agent or "", "crew_agent": crew_agent or ""},
        memory_mode=memory_mode,
    )
    # Seeded and registered with no await in between, so the reader loop
    # cannot stage a frame into only one of the two holders: what this start
    # already staged is copied here, and everything from now on is handed to
    # the collector as well as to any init still in flight. The caller's
    # ``finally`` closes the in-flight scope a moment from now and clears that
    # deque; the collector's copy is what survives to be claimed.
    collector.seed_init_frames(self._pending_init_notifications)
    if late_adopter is not None:

        async def _adopt(session_id: str, resp: dict[str, Any]) -> bool:
            handle = await self._finish_create_session(
                session_id,
                resp,
                buffered_init=collector.take_init_frames(session_id),
                memory_mode=memory_mode,
                agent=agent,
                crew_agent=crew_agent,
                kas_agents=kas_agents,
                mcp_servers=mcp_servers,
                budget=budget,
                stub_token=stub_token,
                denied_tools=denied_tools,
                mirrored_snapshot=mirrored_snapshot,
                consumed_spec=consumed_spec,
                mirror_used=mirror_used,
                ref_spec=ref_spec,
                active_agent=active_agent,
                session_work_dir=session_work_dir,
                projected_sources=projected_sources,
                payload_snapshot=payload_snapshot,
                session_key=session_key,
                member_dispatch_mounted=member_dispatch_mounted,
                request_sent_at=request_sent_at,
                request_identity=request_identity,
            )
            # A declining (or raising) adopter answers False and the
            # collector performs the one teardown.
            # This owner is type-checked before the class that binds it, so mypy checks
            # the closure on its own and loses the ``late_adopter is not None`` narrowing.
            return bool(await late_adopter(handle))  # type: ignore[misc]

        collector.adopt(_adopt)
    self._start_collectors[int(req_id)] = collector
    logger.warning(
        "acp_startup_stage stage=session_new outcome=collecting req_id=%d "
        "collect_budget_s=%g priority=%s gate_active=%d gate_queued=%d %s",
        int(req_id),
        timeout,
        permit.priority.value,
        *permit.gate_counts(),
        permit.gate_state(),
    )
    return collector.start()


async def _teardown_late_session(self: AcpRuntime, session_id: str) -> None:
    """Tear down a session that arrived after its caller gave up.

    Per-session teardown only (the harness's cancel/terminate verb plus the
    local unregister); the shared runtime and its other sessions are never
    touched. A dead runtime has nothing to tear down.
    """
    if self._dead or self._process is None:
        return
    await self.terminate_session(session_id)


def start_collectors(self: AcpRuntime) -> list[StartCollector]:
    """Live collectors, for diagnostics and tests."""
    return list(self._start_collectors.values())


async def _finish_create_session(
    self: AcpRuntime,
    session_id: str,
    resp: dict[str, Any],
    *,
    buffered_init: list[JsonRpcMessage],
    agent: str | None,
    crew_agent: str | None,
    kas_agents: Any,
    mcp_servers: list[dict[str, Any]],
    budget: float,
    stub_token: str,
    denied_tools: frozenset[tuple[str, str]],
    mirrored_snapshot: Any,
    consumed_spec: Any = None,
    mirror_used: bool = False,
    ref_spec: Any,
    active_agent: str,
    session_work_dir: str | Path,
    projected_sources: dict[str, str],
    payload_snapshot: Any,
    memory_mode: str = "persistent",
    session_key: str = "",
    member_dispatch_mounted: bool = False,
    request_sent_at: float | None = None,
    request_identity: str = "",
) -> AcpSessionHandle:
    """Everything after a successful ``session/new``: queue, handle, mode, drain.

    Shared by the direct path and a late adoption through
    :class:`StartCollector`. ``buffered_init`` comes from whichever holder
    staged the frames while this id was unknown: the runtime's in-flight
    scope on the direct path, the collector on an adoption.
    """
    # Register session queue
    queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
    self._session_queues[session_id] = queue
    self._session_cwds[session_id] = str(session_work_dir)
    for msg in buffered_init:
        queue.put_nowait(msg)

    # Resolve the watchdog snapshot OFF the loop before constructing the
    # handle: the load is config file reads + jsonschema validation on a
    # config change, and the handle constructor is synchronous. The crew
    # identity is canonical (a cfg.agents key) — the kiro ``agent`` name
    # is a different namespace and is not stored on the handle.
    _crew = crew_agent if crew_agent is not None else self._crew_agent
    _wd = await asyncio.to_thread(_load_watchdog_settings, _crew)
    handle = AcpSessionHandle(
        session_id=session_id,
        queue=queue,
        runtime=self,
        watchdog=_wd,
        crew_agent=_crew,
        session_key=session_key,
        bound_cwd=str(session_work_dir),
    )
    handle.memory_mode = memory_mode
    # The token this session's stubs carry, so a later claim (warm-pool
    # rekey) can name THIS session instead of every session on the runtime.
    handle.stub_session_token = stub_token
    handle.member_dispatch_mounted = member_dispatch_mounted
    # The projection's client obligation, on the driver that answers this
    # session's permission requests. Empty for a host with no mirror and for a
    # caller-supplied array, and the handle's check is a no-op on empty.
    handle.spec_denied_tools = denied_tools
    # The spec the array was built from, for a member session's loaded-check.
    handle.consumed_agent_spec = consumed_spec
    handle.mirror_used = mirror_used
    # What the registered agent batch grants, recorded by the harness that
    # registered one; a host that took its agent at spawn time records nothing.
    self._harness.record_session_projection(handle, kas_agents, active_agent)
    if self._mirrored_spec_check_needed(mirrored_snapshot):
        await self._require_unchanged_mirrored_spec(session_id, mirrored_snapshot)

    # The vault identity the request went out under, read by the KAS catalog
    # recorder inside ``store_session_config``; see the handle's field.
    handle.request_vault_identity = request_identity
    # Populate state from session/new response (configOptions, available models)
    handle.store_session_config(resp)
    # A backend whose served list is the authority on what it accepts feeds
    # that list into the cross-session advertised-model cache and persists it
    # off the loop, so the picker keeps it after this session is gone. Gated
    # HERE rather than inside the methods so the first-class kiro path gains no
    # call and no failure point in service of an adapter (harness-parity H13).
    if self.acp_backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION:
        handle._feed_advertised_model_cache()
        await handle._persist_advertised_models_if_changed()
    # Both halves of that snapshot are now known, which is what makes the
    # served-default check answerable: the model the backend picked for
    # this session can be one the account's partition does not serve.
    await handle.ensure_served_default()
    # Make a SESSION_CONFIG host actually ask, before anything can prompt it.
    # Wired HERE and not earlier because the call reads the option list
    # ``store_session_config`` just parsed: it has to know whether the option
    # was advertised to tell "not advertised" (INDETERMINATE) from "the write
    # was rejected" (BYPASSED). Self-gating on the routing table, so kiro-cli
    # and the KAS relay send nothing extra — they route through their agent
    # spec. An enforced host that cannot be routed refuses, and the refusal
    # travels the same cleanup path as a failed set_mode below: session/new
    # already succeeded, so a plain local unregister would leak the session in
    # the shared process.
    try:
        await handle.apply_session_permission_routing()
    except Exception:
        await self.terminate_session(session_id)
        raise
    # The roster this session put on the wire. Set BEFORE drain_init so the
    # report can be read as "of the N we sent, these reported" rather than
    # as a bare list of names.
    handle.mcp_session_report().begin_session(mcp_servers)
    self._guard_unresolved_mcp_refs(handle, ref_spec, active_agent, mcp_servers)
    # How the session's stdio servers were launched, from the spec and array
    # already in hand: the watchdog's remote_flat scan finds the in-flight
    # tool's own server process by it. In-memory, so no scheduling point.
    handle.record_mcp_launches(ref_spec, mcp_servers)
    self._start_mount_check(handle, mcp_servers, session_id, session_key, request_sent_at)

    mode_switched = False
    staged_before_switch = 0
    set_mode_ran = False
    activated_alias: str | None = None
    # Set agent mode if specified. If set_mode raises, no handle is returned
    # to the caller, so terminate the session we just created above —
    # session/new already succeeded so the session exists in kiro-cli; a
    # plain local unregister would leak it in the shared process. terminate_
    # session also unregisters the queue. Mirrors the same cleanup in
    # load_session().
    #
    # Guard (A): only activate the mode when the backend advertised it in the
    # session/new `modes` list, or advertised no modes at all (older kiro-cli
    # / fake backend → attempt, backward-compatible). If modes ARE advertised
    # but the requested agent is absent, its ~/.kiro/agents/<agent>.json never
    # loaded (pre-spawn self-heal covers only the managed default). FAIL CLOSED
    # rather than silently leaving the session on kiro-cli's default mode: for
    # a restricted/app agent that would run a BROADER agent than requested (a
    # privilege escalation), so we terminate and raise an actionable error.
    #
    # Guard (A2) runs FIRST: the guard below never sees the agent `--agent`
    # selected, which on kiro-cli is every ordinary session's agent. See
    # _verify_spawn_agent_active.
    await self._verify_spawn_agent_active(session_id, resp, override=agent)
    # The agent to ACTIVATE. An explicit request always applies. When a KAS
    # custom agent was injected (``kas_agents`` non-empty) the runtime
    # default must be activated too: KAS has no --agent flag, so an injected
    # default that is not set here stays registered-but-inactive and the
    # session silently runs KAS's own default mode. On kiro ``kas_agents`` is
    # None and the --agent spawn already selected the default, so only an
    # explicit override reaches set_mode here.
    #
    # Asked of the routing table first (see _activates_agent_by_mode): a host
    # that governs its privileged tools some other way has no agent for
    # set_mode to resolve, so there is nothing to activate and nothing for
    # Guard (A) to fail closed on.
    mode_agent = (
        agent or (self._agent if kas_agents else None) if self._activates_agent_by_mode() else None
    )
    # Guard (C): the id may be advertised, yet as the HOST's own agent; a
    # set_mode would succeed and run that agent under the crewmate's name.
    # Asked of the harness as a seam (H13): the spawn-time hosts answer None
    # and the wire-registered one reads the stamp the engine put on the mode.
    # The id the host knows the agent by: a wire-registered batch went out under
    # kas_wire_agent_id (attach_kas_custom_agents), so the advertised modes and
    # set_mode name that; everything Crew records keeps the crewmate's own id.
    mode_wire = (
        kas_wire_agent_id(mode_agent)
        if mode_agent and kas_agents
        else self._wire_mode_id(mode_agent)
    )
    refusal = self._harness.activation_refusal(mode_wire, resp) if mode_wire else None
    if refusal:
        await self.terminate_session(session_id)
        raise AcpRuntimeError(refusal)
    if mode_agent and mode_wire and self._mode_available(mode_wire, resp):
        # Measured BEFORE the request goes out, which is the only moment the
        # answer is unambiguous: everything queued right now initialized
        # under the pre-switch mode. Reading it after set_mode returns would
        # count the switched-to agent's own registrations -- which kiro-cli
        # can emit before it answers -- as pre-switch, and those frames are
        # then consumed without being recorded, leaving the panel at a false
        # "no report" for the rest of the session.
        staged_before_switch = handle.queued_frame_count()
        # Only the frame THIS activation sends may be judged afterwards.
        self._switched_model_record().pop(session_id, None)
        activated_alias = await self._activate_mode_bracketed(
            session_id,
            mode_agent,
            budget=budget,
            payload_snapshot=payload_snapshot,
            wire_registered=kas_agents is not None,
            mode_id=mode_wire,
        )
        set_mode_ran = True
        handle.active_agent = mode_agent
        await self._served_default_after_mode(handle, session_id)
        # Whether set_mode actually SWITCHED modes: the servers that
        # initialized during session/new belong to the mode kiro-cli
        # started the session on. If the requested agent differs, those
        # staged registration frames describe the pre-switch roster and
        # must not arm the drain's idle shortcut while the switched-to
        # agent's own servers may still be booting.
        _ids, _current, _adv = parse_session_modes(resp)
        mode_switched = mode_wire != _current and (
            bool(_current) or self._harness.notification_aliases.mcp_readiness
        )
    elif mode_agent:
        _ids, _current, _adv = parse_session_modes(resp)
        await self.terminate_session(session_id)
        cause, remedy = await asyncio.to_thread(unavailable_mode_explanation, mode_agent)
        raise AcpRuntimeError(
            f"Agent mode {mode_agent!r} is not available on this session "
            f"(advertised modes: {_ids or 'none'}); {cause} Refusing to run "
            f"the backend default mode {_current or '(unknown)'} in its place. "
            f"{remedy}"
        )

    # Drain MCP-server-init / oauth / config notifications before the first
    # prompt so they don't race into the first turn (parity with
    # AcpClient._drain_notifications). Best-effort, bounded: exits shortly
    # after the servers report, or at the no-report ceiling if none do.
    # A runtime declared MCP-free skips the ceiling — nothing can arm it.
    # After a real mode SWITCH, reports staged during session/new describe
    # the pre-switch roster, so they must not arm the idle shortcut.
    if self._harness.notification_aliases.mcp_readiness:
        readiness_params: dict[str, Any] = {"mcpServers": mcp_servers}
        attach_kas_custom_agents(readiness_params, kas_agents)
        await self._wait_managed_mcp(
            handle,
            readiness_params,
            active_agent,
            budget,
            staged_before_switch if mode_switched else 0,
        )
    elif self._expect_mcp_reports:
        await handle.drain_init(stale_report_frames=staged_before_switch if mode_switched else 0)
    else:
        await handle.drain_init(no_report_ceiling=0.0)

    if active_agent == self._agent and str(session_work_dir) == str(self._work_dir):
        self._copy_native_launch_sources(
            handle, set_mode_ran=set_mode_ran, activated_alias=activated_alias
        )
    handle.native_context_documents.update(projected_sources)
    # Inline prompt bytes and file resources come from the same activated
    # wire definition. Conditional and indexed resources remain native.
    for definition in kas_agents or ():
        if definition.get("id") == active_agent and isinstance(definition.get("prompt"), str):
            handle.native_context_documents[f"template://{active_agent}#prompt"] = definition[
                "prompt"
            ]

    # Each session start forks another agent process under the root, and
    # its MCP servers have just reported, so scan again: the spawn snapshot
    # predates all of them. Safe to repeat -- see _snapshot_descendants.
    #
    # Guarded like every other post-session/new step here: session/new has
    # already succeeded, so a cancellation inside the scan would leave the
    # session live in the shared process with no handle returned to anyone.
    # Only a cancellation can reach this arm; the scan swallows its own
    # failures.
    #
    # Startup is over by this point: session/new has succeeded, which is the
    # proof no sandbox refusal on the way here was fatal. So the startup latch
    # is spent, and spending it -- rather than merely ceasing to arm it -- is
    # what makes every consumer correct by construction: a translator reached
    # after this can only ever see False, so none of them re-derives the window.
    #
    # First session only. A later session/new on a warm runtime is ordinary
    # mid-life work, and a refusal its child prints then says nothing about
    # whether the agent process can start.
    self._first_session_ready = True
    self._saw_sandbox_init_failure = False
    self._saw_delegate_launch_failure = False
    try:
        await self._snapshot_descendants()
    except BaseException:
        await self.terminate_session(session_id)
        raise

    logger.info("Created session %s on runtime PID %d", session_id, self._pid or 0)
    return handle


async def probe_advertised_models(
    self: AcpRuntime, *, force: bool = False, not_before: float = 0.0
) -> list[dict[str, str]]:
    """Fetch a fresh advertised-model (entitlement) snapshot from this backend.

    A session's ``availableModels`` is captured once, from its own
    ``session/new`` response, and the backend resolves that answer from the
    account state it holds at that instant — a lookup racing a token refresh
    or a cold start can answer with the default (free-tier) set. A long-lived
    session holding such an answer refuses models the account actually has,
    and nothing ever corrects it. This re-asks the question on the SAME live
    process with a throwaway minimal session (no MCP servers, no mode
    activation), terminated before returning.

    Single-flight + short TTL, on TWO clocks. A non-empty SUCCESS is replayed
    for :data:`_ENTITLEMENT_PROBE_TTL_SECS` (its result clock); an empty or
    FAILED attempt replays as ``[]`` (no evidence) for the same window (its
    attempt clock) without re-opening a session, so a burst of failures on
    the picker read path costs one round-trip. A failure never revives an
    expired success — the two clocks are independent.

    ``force=True`` skips ONLY the attempt-clock replay: a USER ACTION (an
    explicit ``set_model`` pick, the spawn-time pin withhold) must earn a
    fresh probe rather than be refused on a recent no-evidence failure: an
    explicit action always earns a real answer. It still honours the
    result-clock replay of a recent non-empty success (fresh evidence —
    nothing is gained by re-probing) and still serializes on the
    single-flight lock. The picker read path leaves ``force=False`` so it
    keeps the burst cap.

    ``not_before`` is a freshness floor on the result-clock replay: a caller
    passes the monotonic time of the snapshot it already holds, and a replay
    never answers with a result older than that snapshot. The shared result
    cache spans every session on this process, so without the floor a broad
    answer cached before an entitlement downgrade would overwrite a newer
    session's correctly narrower ``session/new`` snapshot. A cached result
    older than the floor is not evidence for that caller: the attempt-clock
    logic below decides between ``[]`` and a fresh probe, whose result is
    always newer than the floor.

    Returns the normalized advertised list, or ``[]`` when the probe fails
    or advertises nothing. An empty return is NOT evidence about
    entitlement — callers must keep whatever snapshot they already hold.
    """
    async with self._entitlement_probe_lock:
        now = time.monotonic()
        # Two-clock guard so a failure never extends the life of an old
        # success. If the last SUCCESSFUL result is still within TTL, replay
        # it (even under force: a fresh success is fresh evidence). Otherwise,
        # if the last ATTEMPT of any outcome (incl. a failure that re-stamped
        # only the attempt clock) is within TTL, return [] — no evidence, fail
        # open — WITHOUT opening a fresh session/new, UNLESS force=True, which
        # a user action passes to earn a fresh probe rather than be refused on
        # a recent no-evidence failure. Only past both windows (or forced past
        # the attempt window) do we probe again. Both clocks are 0.0 until the
        # first completed attempt, so neither branch fires before one. Both
        # replays also require their clock to be at least as new as the
        # caller's own snapshot (``not_before``): a cached answer never
        # replaces a newer one, and a failed attempt that predates the
        # caller's snapshot never stands in for the probe that snapshot has
        # yet to receive.
        if (
            self._entitlement_probe_result
            and self._entitlement_probe_result_at > 0.0
            and self._entitlement_probe_result_at >= not_before
            and now - self._entitlement_probe_result_at < _ENTITLEMENT_PROBE_TTL_SECS
        ):
            return list(self._entitlement_probe_result)
        if (
            not force
            and self._entitlement_probe_attempt_at > 0.0
            and self._entitlement_probe_attempt_at >= not_before
            and now - self._entitlement_probe_attempt_at < _ENTITLEMENT_PROBE_TTL_SECS
        ):
            return []
        if not self._initialized or self._dead or self._process is None:
            return []
        params = build_session_new_params(await self._session_work_dir(), mcp_servers=[])
        session_id = ""
        self._session_inits_in_flight += 1
        try:
            try:
                resp = await self._send_and_await(
                    METHOD_SESSION_NEW, params, timeout=_ENTITLEMENT_PROBE_TIMEOUT
                )
                session_id = str(resp.get("sessionId") or "")
            finally:
                # Close the init scope even on failure so staged init
                # notifications from this probe never leak into a later
                # real session's queue.
                self._finish_session_init(session_id)
        except Exception:
            logger.debug("entitlement probe session/new failed", exc_info=True)
            # Stamp the ATTEMPT clock only (never the result clock), so a
            # burst of failing reads costs one session/new within the TTL
            # while the last SUCCESSFUL result keeps expiring on its own
            # clock — a failure can never revive a stale success.
            self._entitlement_probe_attempt_at = time.monotonic()
            return []
        # The answer's freshness is the moment it ARRIVED, stamped before the
        # teardown below. Stamping after the terminate round-trip would date
        # the answer later than a real session/new that completed during
        # that await, and the freshness floor would then let this older
        # answer replay over that session's newer snapshot.
        completed_at = time.monotonic()
        try:
            # Reads BOTH shapes, through the same fold the session-init capture
            # uses. Reading only ``models`` answers [] for a host whose list is a
            # ``configOptions`` select, and [] is contractually "no evidence" --
            # so the degraded snapshot this probe exists to correct would be the
            # one thing it could never correct.
            fresh = advertised_models_from_session(resp, self.acp_backend)
        finally:
            if session_id:
                # Evict the probe session from the shared process; never
                # raises (best-effort by contract).
                await self.terminate_session(session_id)
        # A non-empty answer updates the stored result and its OWN clock, so
        # it is replayed until that clock expires. An empty answer leaves the
        # stored result (and its clock) untouched — no evidence, fail open.
        if fresh:
            self._entitlement_probe_result = list(fresh)
            self._entitlement_probe_result_at = completed_at
        # The attempt clock is stamped on EVERY completed probe, empty
        # included, so a burst of reads within the TTL costs one session/new
        # whether or not the backend advertised anything.
        self._entitlement_probe_attempt_at = completed_at
        return fresh
