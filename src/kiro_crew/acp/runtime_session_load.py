"""``session/load`` end to end.

Resuming a prior session on this runtime, and waiting for its managed MCP servers.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_session_cwds``, ``_session_inits_in_flight``, ``_session_queues``.

Instance fields read: ``_acp_backend``, ``_agent``, ``_agent_capabilities``,
``_can_load_session``, ``_crew_agent``, ``_expect_mcp_reports``, ``_initialized``,
``_mcp_gateway_overlay``, ``_pid``, ``_pool_lookup``, ``_remote_session_servers``,
``_remote_workspace_agents``, ``_wire_mode_id``, ``_work_dir``, ``_write_remote_token``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import attach_kas_custom_agents, parse_session_modes
from kiro_crew.acp.kas_agents import hoist_servers_for_session
from kiro_crew.acp.mcp_session_report import (
    active_custom_agent,
    required_managed_servers,
    roster_names,
)
from kiro_crew.acp.runtime_session_setup import (
    _disable_check_scope,
    _pooled_session_servers_and_ref_spec,
)
from kiro_crew.acp.session_handle import (
    AcpFrameTooLarge,
    AcpRequestTimeout,
    AcpRuntimeError,
    AcpSessionHandle,
    _load_watchdog_settings,
)
from kiro_crew.acp.session_mcp import (
    session_mcp_server_is_disabled,
    settings_server_names,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    METHOD_SESSION_LOAD,
    JsonRpcMessage,
)
from kiro_crew.agent_files import kas_wire_agent_id
from kiro_crew.dashboard.side_readonly_spec import unavailable_mode_explanation
from kiro_crew.providers.mirrors.registry import has_mirror

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


async def load_session(
    self: AcpRuntime,
    session_file: str,
    resume_sid: str,
    cwd: str | Path | None = None,
    agent: str | None = None,
    crew_agent: str | None = None,
    member_session_key: str = "",
    session_key: str = "",
    channel_id: str = "",
) -> AcpSessionHandle:
    """Resume a prior session via session/load — mirrors AcpClient.

    Unlike create_session()+handle.load(), this issues session/load
    DIRECTLY (no session/new first), using the ORIGINAL sid as sessionId
    and passing cwd + the pooled broker stubs + the full transcript path,
    exactly as AcpClient._initialize_session does. This avoids the
    double-session footgun (fresh session/new context replayed on top of
    the loaded transcript) that produced stopReason='refusal'. Raises on
    failure so the caller can fall back to create_session().

    ``member_session_key`` mirrors create_session(): session/load
    re-initializes the session's MCP servers and re-registers the wire
    agent, so a member session resumed WITHOUT the same injection loses
    its dispatch tools mid-conversation — the mount must ride every path
    that (re)establishes the session's tool set, not just the first one.

    ``session_key`` mirrors create_session() for the same reason: load
    re-declares the broker stubs, so it re-launches them, and a resumed
    session whose stubs carried no token would fall back to resolving as the
    runtime — the parent slot — for the rest of its life.

    ``channel_id`` mirrors create_session() for the same reason the array
    does: session/load re-initializes this session's MCP servers, so a resume
    that dropped it would take the channel identity away from a conversation
    that already had it.
    """
    if not self._initialized:
        raise AcpRuntimeError("Runtime not initialized — call spawn() first")
    if not self._can_load_session:
        raise AcpRuntimeError("Backend does not advertise session/load support")
    agent = await self._source_agent(agent)

    # Re-declare the pooled broker stubs so a resumed session keeps talking
    # to the broker — same injection as create_session() and the AcpClient
    # resume path (client_start.py). session/load re-initializes the session's MCP
    # servers (see the budget note below), so an empty list here is APPLIED,
    # not ignored: the stubs stop shadowing the agent spec's same-named
    # entries and kiro-cli spawns its own copy of every pooled server,
    # silently un-pooling the session for the rest of its life. Resolved off
    # the event loop — the overlay lookup stats and reads files. Empty when
    # the shared gateway is disabled, so non-pooled installs still send [].
    active_agent = agent or self._agent
    # Hoisted above the array for the same reason create_session() hoists it: a
    # mirrored host's projection resolves the agent spec against this directory.
    session_work_dir = str(await self._session_work_dir(cwd))
    denied_tools: frozenset[tuple[str, str]] = frozenset()
    mirrored_snapshot: Any = None
    consumed_spec: Any = None
    ref_spec: Any = None
    # A mirrored host re-declares the array its projection built, not the raw
    # pooled one: session/load re-initializes the session's servers, so an
    # unprojected array here does not merely fail to withhold a stub -- it MOUNTS
    # one on a conversation whose session/new withheld it.
    #
    # Gated on the registry read rather than entered unconditionally, for the
    # reason this method already gives for gating the KAS re-attach on the
    # backend: the kiro resume path must reach a comparison and STOP -- no awaited
    # step, nothing to unwind, no shared coroutine that could grow a failure mode
    # later. A membership read is the sanctioned form of that comparison.
    mirrored = None
    if has_mirror(self.acp_backend):
        mirrored = await self._mirrored_session_mcp(
            active_agent,
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
        pool_agent, launch_spec = self._pool_lookup(active_agent)
        pooled, ref_spec = await asyncio.to_thread(
            _pooled_session_servers_and_ref_spec,
            self._mcp_gateway_overlay,
            pool_agent,
            self.acp_backend,
            session_work_dir,
            launch_spec,
        )
        mcp_servers = await self._unpooled_control_planes(pooled, active_agent, session_work_dir)
        mcp_servers, stub_token = await self._own_stub_session(mcp_servers, session_key)
    member_withheld = False
    # Whether the member session-control entry is IN the array, the one fact
    # the context builder needs to teach the session_* tools truthfully.
    member_mounted = False
    # False for every non-member session, set without an awaited call so the
    # Kiro construction path is untouched by this capability (H13).
    panel_mounted = False
    if member_session_key:
        # circular import: members' module graph is heavy; resolved at call
        # time, same as create_session().
        from kiro_crew.members import MEMBER_DISPATCH_SERVER, member_dispatch_session_server

        # Asked on the resume path for the reason it is asked on create, and it
        # matters MORE here: session/load re-initializes the session's servers, so
        # an unasked question would re-mount a switched-off server onto a
        # conversation whose session/new withheld it.
        member_withheld = await asyncio.to_thread(
            session_mcp_server_is_disabled,
            MEMBER_DISPATCH_SERVER,
            active_agent,
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
            mcp_servers = [e for e in mcp_servers if e.get("name") != member_entry["name"]] + [
                member_entry
            ]
        elif member_withheld:
            logger.warning(
                "member session %s: %s is switched off for this session "
                "(disabled), so session control is not mounted on resume — the DM "
                "thread runs as plain chat; re-enable that server to restore it",
                member_session_key,
                MEMBER_DISPATCH_SERVER,
            )
        else:
            logger.warning(
                "member session %s: dashboard server unresolved on resume — "
                "the DM thread runs as plain chat this session",
                member_session_key,
            )
        # INSIDE the branch, for the reason create_session() states.
        mcp_servers, panel_mounted = await self._mount_member_panel(
            mcp_servers,
            member_session_key=member_session_key,
            agent_name=active_agent,
            session_work_dir=session_work_dir,
            stub_token=stub_token,
            resuming=True,
        )
    # Narrowed by the host for the same reason session/new is, and it matters
    # MORE here: session/load re-initializes the session's servers, so a
    # rejected array does not just fail to add tools -- it takes them away from
    # a conversation that already had them.
    # Bound once, for the same consumers as session/new: see the note there.
    await self._write_remote_token()
    wire_servers = self._remote_session_servers(
        self._harness.session_mcp_servers(mcp_servers, agent_capabilities=self._agent_capabilities)
    )
    load_params: dict[str, Any] = {
        "sessionId": resume_sid,
        "cwd": session_work_dir,
        "mcpServers": wire_servers,
    }
    if session_file:
        # The CALLER decides, because the caller is what knows whether a
        # transcript exists: a host that locates the session from its id is
        # called with an empty path and the field never reaches it. Asking the
        # HOST instead would send the same requests today and cost something
        # real -- the _meta merge invariant below is pinned by a test that puts
        # a transcript path on KAS deliberately, and a per-host gate deletes
        # the only way to construct that collision.
        load_params["_meta"] = {"_kiro.dev/session_file": session_file}
    # Re-inject the agent definition, for the same reason create_session()
    # does: KAS registers client agents per session and has no --agent flag,
    # so a resumed session that is not handed them again advertises only the
    # modes it can find on disk. That set is NOT a superset of what
    # session/new had — KAS skips an agent profile written for kiro-cli — so
    # omitting this made the requested mode genuinely absent on resume, and
    # Guard A below then refused the load rather than run the backend default.
    #
    # Guarded on the backend rather than on the projection answering None, so
    # the kiro resume path reaches a comparison and STOPS: no awaited step,
    # nothing to unwind, no shared coroutine that could grow a failure mode
    # later. create_session() enters the same seam unconditionally, which is
    # the shape this one deliberately does not copy. Reading the backend and
    # stopping is the smallest non-zero delta the kiro path can take for KAS
    # behaviour to exist here at all, and the harness exposes no property
    # meaning "this host needs its agent re-sent on resume" that could answer
    # it instead — the projection's own None is the only such signal, and
    # consuming it is what would cost the kiro path the awaited step.
    # None on the kiro path, where the host took its agent at spawn time; on KAS it is
    # the generation the wire payload was built from, and the activation bracket below
    # compares against it rather than re-reading the file.
    kas_agents = None
    payload_snapshot = None
    request_identity = ""
    if self._acp_backend == ACP_BACKEND_KAS:
        kas_extras = await self._kas_custom_agents(
            active_agent,
            # The grant follows the withhold here too -- see create_session().
            member_dispatch=bool(member_session_key) and not member_withheld,
            crew_panel=panel_mounted,
            session_key=session_key,
        )
        kas_agents = kas_extras.custom_agents
        payload_snapshot = kas_extras.derived_spec_snapshot
        # Mirrors create_session: the vault identity the request goes out under,
        # for the KAS catalog recorder (a load echoes the select too).
        request_identity = kas_extras.vault_identity
        # Same carriage as create_session: a resumed session re-initializes
        # its servers, and the managed ones must win the same-name contest
        # on load exactly as they did on new.  ``include_user_servers=True``
        # additionally hoists user-declared (non-managed) servers: on session/load
        # kiro-cli re-initialises servers from the session-level array only --
        # agent-block declarations are re-registered but not re-launched -- so spec
        # edits adding new MCP servers would be silently ignored without this.
        # session/new (create_session) is unchanged: kiro-cli reads agent-block
        # servers on its own there, and hoist_servers_for_session's default
        # (include_user_servers=False) does not widen that path.
        #
        # A user-declared name that a settings file kiro-cli reads (the global
        # ~/.kiro/settings/mcp.json or the checkout's .kiro/settings/mcp.json)
        # also declares is NOT hoisted: left in the agent block, the resumed
        # session resolves it by kiro-cli's own precedence, the same way
        # session/new did. The checkout is the session's own cwd, the same one
        # native_settings_sources reads beside: kiro-cli reads the workspace
        # settings file from cwd whatever level its agent spec resolves at.
        # Read off the loop thread like the other settings reads here.
        reserved = await asyncio.to_thread(settings_server_names, session_work_dir)
        kas_agents, mcp_servers = hoist_servers_for_session(
            kas_agents,
            active_agent,
            mcp_servers,
            session_token=stub_token,
            include_user_servers=True,
            reserved_names=reserved,
        )
        # REBIND, not just re-assign the param: the hoist changes the array, and
        # wire_servers is what the stall diagnostic and the session report read.
        # Setting only load_params would leave both describing the pre-hoist roster
        # -- naming servers this resume did not send.
        wire_servers = self._remote_session_servers(
            self._harness.session_mcp_servers(
                mcp_servers, agent_capabilities=self._agent_capabilities
            )
        )
        load_params["mcpServers"] = wire_servers
        kas_agents = self._remote_workspace_agents(kas_agents, wire_servers)
        attach_kas_custom_agents(load_params, kas_agents)
    budget = await self._session_start_budget()
    self._session_inits_in_flight += 1
    loaded_session_id = ""
    try:
        # session/load is gated by the SAME MCP (re-)initialization as
        # session/new — kiro-cli re-initializes the session's servers on
        # load, and the runtime stages mcp/oauth_request frames while
        # EITHER request is in flight (the _session_inits_in_flight-keyed
        # staging in _reader_loop, closed by _finish_session_init; see
        # docs/system-specs/modules/acp-client.md "loading a session
        # triggers MCP re-initialization") — so it gets the same budget.
        request_sent_at = time.time()
        resp = await self._send_and_await(METHOD_SESSION_LOAD, load_params, timeout=budget)

        # A genuine resume echoes "modes" in the response (same signal AcpClient
        # keys on). Anything else means load did not actually restore state.
        if "modes" not in resp:
            raise AcpRuntimeError(f"session/load did not resume session {resume_sid}: {resp}")
        loaded_session_id = resume_sid
    except AcpFrameTooLarge as exc:
        exc.session_start_failed = True
        raise
    except AcpRequestTimeout as exc:
        # Read the staged MCP reports before the finally below clears them.
        raise self._session_start_stalled(exc, METHOD_SESSION_LOAD, wire_servers) from exc
    finally:
        buffered_init = self._finish_session_init(loaded_session_id)

    # Register the queue AFTER _send_and_await returns. During session/load
    # kiro-cli replays the full prior transcript on stdout; without a
    # registered queue those replay frames hit the "unknown session -> drop"
    # path in the reader loop and are silently discarded. Only frames
    # arriving AFTER this point (from future prompt() calls) reach the queue.
    # The load response itself routes via _pending_requests, not the session
    # queue, so this reorder is safe.
    queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
    self._session_queues[resume_sid] = queue
    self._session_cwds[resume_sid] = str(session_work_dir)
    for msg in buffered_init:
        queue.put_nowait(msg)

    # Mirrors create_session: a resumed session gets the same
    # canonical-crew watchdog snapshot, resolved off-loop.
    _crew = crew_agent if crew_agent is not None else self._crew_agent
    _wd = await asyncio.to_thread(_load_watchdog_settings, _crew)
    handle = AcpSessionHandle(
        session_id=resume_sid,
        queue=queue,
        runtime=self,
        watchdog=_wd,
        crew_agent=_crew,
        session_key=session_key,
        bound_cwd=str(load_params["cwd"]),
    )
    # Mirrors create_session: the resumed session's own stub token.
    handle.stub_session_token = stub_token
    handle.member_dispatch_mounted = member_mounted
    # A fresh handle has no baseline for the restored ledger, whose counter
    # already carries the session's earlier spend.
    handle.last_prompt_stats.restore_cost_session(resume_sid)
    # Mirrors create_session: the resumed session re-declares the array, so it
    # re-derives the deny set that array came with and re-checks the generation.
    handle.spec_denied_tools = denied_tools
    handle.consumed_agent_spec = consumed_spec
    handle.mirror_used = mirrored is not None
    # Mirrors create_session: the re-registered batch is what this session now
    # runs.
    self._harness.record_session_projection(handle, kas_agents, active_agent)
    if self._mirrored_spec_check_needed(mirrored_snapshot):
        await self._require_unchanged_mirrored_spec(resume_sid, mirrored_snapshot)
    handle.request_vault_identity = request_identity
    handle.store_session_config(resp)
    # Feed and persist the advertised-model cache from the resumed session's
    # capture, same as the create path, and gated the same way so the kiro
    # path gains no call (harness-parity H13).
    if self.acp_backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION:
        handle._feed_advertised_model_cache()
        await handle._persist_advertised_models_if_changed()
    # session/load echoes ``currentModelId`` exactly like session/new, and a
    # session persisted before the account's served list changed can come
    # back on a default the account does not serve — so the resumed session gets
    # the same served-default check as a fresh one.
    await handle.ensure_served_default()
    # Same as create_session, and for the same reason a resumed session gets
    # the served-default check: the option list came back on THIS response,
    # and a resumed session prompts the host exactly as a fresh one does, so
    # its permission boundary has to be armed here too. Refusal terminates
    # the resume_sid session rather than leaking it, matching the set_mode
    # cleanup below.
    try:
        await handle.apply_session_permission_routing()
    except Exception:
        await self.terminate_session(resume_sid)
        raise
    # session/load re-initializes this session's servers, so the resumed
    # session gets its own report against the roster load re-declared.
    handle.mcp_session_report().begin_session(wire_servers)
    self._guard_unresolved_mcp_refs(handle, ref_spec, active_agent, wire_servers)
    # Mirrors create_session: load re-declares the servers, so re-record them.
    handle.record_mcp_launches(ref_spec, wire_servers)
    self._start_mount_check(handle, wire_servers, resume_sid, session_key, request_sent_at)

    mode_switched = False
    staged_before_switch = 0
    set_mode_ran = False
    activated_alias: str | None = None
    # Activate the agent (mirrors AcpClient step 4 — set_mode applies to a
    # resumed session too, not just fresh ones). If set_mode raises, the
    # caller falls back to create_session() (a fresh sid + its own queue),
    # so terminate this resume_sid session first — session/load already
    # succeeded so kiro-cli holds it; a plain local unregister would leak it
    # in the shared process (and leave the reader routing late transcript-
    # replay frames to an abandoned queue). terminate_session unregisters too.
    #
    # Guard (A2), same as create_session: the check below reads `agent`, and
    # this method's only caller passes `agent=agent or None`, so a resume with
    # no override reaches no check at all. A fresh runtime resuming a session
    # re-reads the spec from disk, so the spawn agent can fail to load here
    # exactly as it can on a cold start.
    await self._verify_spawn_agent_active(resume_sid, resp, override=agent)
    # Same routing-table question as create_session: a host with no agent
    # spec has no mode to resume onto either.
    mode_agent = agent if self._activates_agent_by_mode() else None
    # Guard (C) -- see create_session: advertised, but as the host's own.
    # Same wire id as create_session: the batch this load re-sent was renamed.
    mode_wire = (
        kas_wire_agent_id(mode_agent)
        if mode_agent and kas_agents
        else self._wire_mode_id(mode_agent)
    )
    refusal = self._harness.activation_refusal(mode_wire, resp) if mode_wire else None
    if refusal:
        await self.terminate_session(resume_sid)
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
        self._switched_model_record().pop(resume_sid, None)
        activated_alias = await self._activate_mode_bracketed(
            resume_sid,
            mode_agent,
            budget=budget,
            payload_snapshot=payload_snapshot,
            wire_registered=kas_agents is not None,
            mode_id=mode_wire,
        )
        set_mode_ran = True
        handle.active_agent = mode_agent
        await self._served_default_after_mode(handle, resume_sid)
        # See create_session: after a real mode switch, registration frames
        # staged during session/load describe the pre-switch roster.
        _ids, _current, _adv = parse_session_modes(resp)
        mode_switched = mode_wire != _current and (
            bool(_current) or self._harness.notification_aliases.mcp_readiness
        )
    elif mode_agent:
        # Guard (A) — see create_session. A resumed session always echoes a
        # `modes` list (checked above), so an absent agent means its config
        # isn't loaded. Fail closed rather than silently resuming on a
        # different (broader) default agent than the one requested.
        _ids, _current, _adv = parse_session_modes(resp)
        await self.terminate_session(resume_sid)
        cause, remedy = await asyncio.to_thread(unavailable_mode_explanation, mode_agent)
        raise AcpRuntimeError(
            f"Agent mode {agent!r} is not available for resumed session "
            f"{resume_sid} (advertised modes: {_ids or 'none'}); {cause} Refusing "
            f"to run the backend default mode {_current or '(unknown)'} in its place. "
            f"{remedy}"
        )

    # Drain MCP-init / oauth / config notifications before the first prompt
    # (parity with AcpClient). Transcript-replay frames were already dropped
    # before the queue was registered above, so only genuine init frames
    # remain to drain here. MCP-free runtimes skip the no-report ceiling.
    # After a real mode SWITCH, staged reports are pre-switch — don't arm.
    if self._harness.notification_aliases.mcp_readiness:
        await self._wait_managed_mcp(
            handle,
            load_params,
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

    # A resume re-initializes the MCP servers and forks the same agent
    # processes a fresh session does, so it needs the same scan; without it
    # every descendant a resumed session created stays unrecorded. Guarded
    # for the same reason as create_session: session/load already succeeded.
    try:
        await self._snapshot_descendants()
    except BaseException:
        await self.terminate_session(resume_sid)
        raise

    logger.info("Resumed session %s on runtime PID %d", resume_sid, self._pid or 0)
    return handle


def _declared_governed() -> bool:
    """Whether the operator declared registry mode; an unreadable answer is ``False``.

    Deliberately NOT ``session_mcp._registry_mode``, which reads an unreadable
    config as governed: that is the safe direction for WITHHOLDING spec servers,
    but here governed LOOSENS readiness (the session starts without waiting for
    Crew's servers), so the safe direction is the opposite one.
    """
    try:
        # Function-local: the loader closes an import cycle through the config plane.
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        # A half-read config (an unreadable base or overlay) is not a declaration.
        if cfg._base_unreadable or cfg._overlay_unreadable:
            return False
        return cfg.agent.mcp_registry_mode is True
    except Exception:
        logger.debug("registry mode unreadable; keeping the readiness timeout", exc_info=True)
        return False


async def _wait_managed_mcp(
    self: AcpRuntime,
    handle: AcpSessionHandle,
    params: dict[str, Any],
    agent: str,
    timeout: float,
    stale_report_frames: int,
) -> None:
    # *params* carries the batch as sent, so its entry is keyed by the wire id.
    wire_agent = kas_wire_agent_id(agent)
    required = required_managed_servers(params, wire_agent)
    try:
        # Read off the loop: the declaration is config I/O.
        governed = bool(required) and await asyncio.to_thread(_declared_governed)
        if required:
            await handle.wait_mcp_ready(
                required,
                timeout,
                stale_report_frames=stale_report_frames,
                tool_policy=active_custom_agent(params, wire_agent),
                # The names THIS request injected at session level, as sent:
                # the one declaration site a provenance-less backend is
                # known to honour over a same-named global server.
                injected=frozenset(roster_names(params.get("mcpServers"))) & set(required),
                # Declared registry mode: the KAS customAgents wire has no slot for
                # the registry marker (see ``kas_agents._project_mcp_servers``), so
                # a governed host's catalog filter drops Crew's managed servers and
                # never reports them. The full budget would only be spent and then
                # fail, and the retry meets the same filter. After a short settle
                # with nothing named, the session starts instead; a server the host
                # did name keeps the full budget and every readiness guard.
                governed=governed,
            )
        else:
            # An external-only agent still needs the ordinary OAuth/config
            # drain, even though it has no managed readiness requirement.
            await handle.drain_init(
                stale_report_frames=stale_report_frames,
                no_report_ceiling=None if self._expect_mcp_reports else 0.0,
            )
    except BaseException:
        # No handle escapes on failure. A fresh session/new has no history
        # to preserve, so evict it from the host and reap its MCP children.
        # KAS has no evict-only verb: session/load is therefore unregistered
        # locally so its existing native history survives for a later resume.
        if "sessionId" in params:
            self.unregister_session(handle.session_id)
        else:
            await self.terminate_session(handle.session_id)
        raise
