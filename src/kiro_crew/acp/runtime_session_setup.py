"""What a session start is built from and checked against.

Shared by ``session/new`` and ``session/load``: the session's working directory, its MCP
server array (the host's mirrored projection, the stub session, the pooled and unpooled
control planes, the member panel), KAS custom agents, the derived-spec and
member-projection checks, the unresolved-reference guard, the session-open mount check,
the start budget and the copy of the native launch sources.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_session_start_timeout``, ``_start_collect_timeout``.

Instance fields read: ``_acp_backend``, ``_agent``, ``_bound_workspace_fd``,
``_expect_mcp_reports``, ``_launch_agent``, ``_launch_copy_spec``, ``_mcp_gateway_overlay``,
``_mcp_gateway_socket``, ``_member_context``,
``_native_launch_resources``, ``_native_launch_sources``, ``_native_launch_view_alias``,
``_native_skill_projection``, ``_session_queues``, ``_tool_search_wire``, ``_wire_mode_id``,
``_work_dir``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew import member_essential_context
from kiro_crew.acp import runtime_start
from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.mcp_mount_check import schedule_mount_check
from kiro_crew.acp.mcp_ref_guard import warn_remote_header_secret_refs, warn_unresolved_server_refs
from kiro_crew.acp.runtime_start import _SESSION_NEW_TIMEOUT, _resolve_start_collect_timeout
from kiro_crew.acp.session_handle import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.session_mcp import (
    agent_spec_snapshot,
    session_mcp_disabled_tools,
    session_mcp_server_is_disabled,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    overlay_project_scope,
)
from kiro_crew.agent_sdk.tool_search import spec_grants_tool_search
from kiro_crew.config import live
from kiro_crew.mcp_gateway.claim import mint_stub_session_token, send_claim
from kiro_crew.mcp_gateway.session_servers import (
    attach_stub_session_token,
    injection_server_names,
    pooled_session_servers,
)
from kiro_crew.providers.mirrors.registry import mirror_for
from kiro_crew.sandbox import BoundWorkspaceMismatch, resolve_bound_session_workspace
from kiro_crew.session_token_sig import publish_session_token

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


class AcpWorkspaceBindingError(AcpRuntimeError):
    """A live runtime cannot safely serve this session; give it a runtime of its own.

    Raised for another cwd on a descriptor-bound runtime, and by the subclass
    below for a tool surface the process-wide settings would break. The
    run-runtime caller answers both the same way: a dedicated runtime.
    """


class AcpToolSurfaceBindingError(AcpWorkspaceBindingError):
    """A deferral-enabled process cannot serve an agent whose spec grants no loader."""


# Named by the path callers import them from: an error chain renders each raised class
# as ``module.qualname`` (``kiro_crew.subagent._describe_exception``), and that text
# reaches the Subagents panel and tracebacks, so this module's name must not leak into it.
AcpWorkspaceBindingError.__module__ = "kiro_crew.acp.runtime"
AcpToolSurfaceBindingError.__module__ = "kiro_crew.acp.runtime"


def _ref_spec_snapshot(agent: str | None, work_dir: str | Path) -> dict[str, Any] | None:
    """The unresolved-ref guard's spec snapshot, or ``None`` -- never an exception.

    The runtime twin of ``AcpClient._read_mcp_ref_spec``, and best-effort for the
    same reason: this rides inside the hop that builds a session's MCP array, on
    every host's establishment path including kiro's. ``agent_spec_snapshot`` goes
    through the derived-spec freshness gate, which refuses a stale mirror it could
    not repair -- the right answer for the projection, whose output IS the
    session's MCP surface, and the wrong one for a diagnostic: a guard that can
    fail ``session/new`` is a worse defect than the unresolved ref it reports.
    ``None`` makes the guard silent and leaves the session's fate to the array.
    """
    try:
        return agent_spec_snapshot(agent, work_dir=work_dir)
    except Exception:
        logger.debug("unresolved-ref guard: agent spec unreadable", exc_info=True)
        return None


def _disable_check_scope(backend: str, work_dir: Any) -> Any:
    """The checkout a switched-off-server check may read *backend*'s spec from.

    ``None`` for a host that resolves its agent at the USER level only, and the
    session's checkout for every other. Read from :func:`overlay_project_scope`, the
    one decider, rather than spelled again here: the question is the same one the
    array's own projection asks, and answering it twice is how the two scopes come
    apart.

    The mismatch this exists to prevent is specific. ``session_mcp`` resolves a spec
    project-nearest and does NOT fall back, so on a user-level host a same-named file
    in the checkout would decide the answer for a session running the user-level
    agent: a switch-off written where that session's agent actually lives would read
    as "not disabled", and the server it withdraws would mount.
    """
    return overlay_project_scope(backend, work_dir).get("work_dir")


def _pooled_session_servers_and_ref_spec(
    overlay: Any,
    agent: str | None,
    backend: str,
    work_dir: str | Path,
    ref_spec: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The pooled stub array AND the guard's spec snapshot, from one off-loop hop.

    Module-level so the hop is one ``to_thread`` call with no closure, and so the
    two reads stay together: adding the snapshot as a second hop would be the
    scheduling point H13 forbids on the kiro path. The array is resolved FIRST
    and the snapshot is the passenger: a snapshot failure resolves to ``None``
    and cannot cost the session its array.

    The overlay lookup's scope is :func:`overlay_project_scope`'s answer for
    *backend*, splatted at the call itself: the ``session_servers`` ratchet reads
    the scope off the call that resolves the overlay, and this is that call for
    both of the runtime's session-array paths. The snapshot reads the spec against
    the session's checkout regardless -- a user-level-only host scopes its overlay
    lookup to nothing, and the guard still judges the spec it is running.

    *ref_spec* is that spec when the session runs an app's launch copy, read by its
    path at spawn (``AcpRuntime._pool_lookup``). Then *agent* is the registered spec's
    overlay name, and the lookup is user-level: kiro-cli reads no checkout spec for a
    launch copy, so a checkout's file of that name says nothing about this session.
    """
    scope_dir = None if ref_spec is not None else work_dir
    servers = pooled_session_servers(overlay, agent, **overlay_project_scope(backend, scope_dir))
    return servers, ref_spec if ref_spec is not None else _ref_spec_snapshot(agent, work_dir)


class _MirroredSessionMcp(NamedTuple):
    """One mirrored host's ``session/new`` MCP array and what came with it.

    Four things fall out of ONE agent-spec parse, and only the first is wire data.
    Returning them together is what stops a second parse from being needed for the
    others -- which would be the consistency window the projection exists to close,
    since the spec is a user-writable file that can change between two reads.

    ``denied_tools`` is the driver obligation: the ``(server, tool)`` pairs this
    session's spec switched off, spelled as the backend registers them, refused at
    the approval request because this transport has no wire channel for a per-tool
    restriction. ``stub_token`` names this session's brokered servers to gatewayd.
    ``derived_spec_snapshot`` is the generation the array was built from, typed
    loosely so this module stays clear of :mod:`kiro_crew.agent` and its config
    import chain.
    """

    servers: list[dict[str, Any]]
    denied_tools: frozenset[tuple[str, str]]
    stub_token: str
    derived_spec_snapshot: Any
    ref_spec: Any = None
    """The agent spec as the unresolved-ref detector reads it, or ``None``.

    Read in the same off-loop hop as the projection, so the guard that consumes
    it costs session start no scheduling point of its own (H13). ``None`` when the
    spec is unreadable, which the guard treats as nothing to say.
    """
    agent_spec: Any = None
    """The parsed spec the array was built from. A member session's loaded-check
    compares THIS, never a later read of the file, against its saved intent."""


async def _session_work_dir(self: AcpRuntime, cwd: str | Path | None = None) -> str | Path:
    """Resolve an ACP cwd without re-authorizing a mutable macOS pathname."""
    if self._bound_workspace_fd is None:
        return cwd if cwd else self._work_dir
    requested = cwd if cwd else self._work_dir
    # The shared rule lives in sandbox.resolve_bound_session_workspace; only the
    # error mapping is this front end's. What the peer receives is the BOUND
    # DESCRIPTOR's own name, not the caller's spelling -- that one is what a
    # symlink swap controls, and it can name a descendant this check never
    # covered.
    #
    # What no string here can do is bind the PEER's own resolution.
    # ``session/new`` carries a cwd STRING that a separate process re-resolves
    # after this returns, so a same-UID rename of the canonical directory in that
    # window remains open; that is a property of the protocol boundary, not of the
    # spelling. ``/dev/fd/<n>`` is not the alternative: the binding is darwin-only
    # (see bind_voice_safe_agent_workspace, which returns no descriptor off
    # macOS), and macOS cannot resolve those entries at all -- the very bug this
    # change exists to fix, i.e. that spelling never delivered a working session
    # cwd, let alone a safer one. The agent PROCESS's own cwd is pinned by
    # descriptor at spawn (create_subprocess_limited's chdir_fd), which is the
    # part that does not go through a name.
    try:
        return await resolve_bound_session_workspace(self._bound_workspace_fd, requested)
    except BoundWorkspaceMismatch as exc:
        raise AcpWorkspaceBindingError(
            "A delegated macOS Kiro runtime is bound to one exact workspace; "
            "create a runtime bound to the requested workspace"
        ) from exc
    except OSError as exc:
        raise AcpWorkspaceBindingError(
            "Cannot verify the requested macOS session workspace"
        ) from exc


def _refuse_if_loader_unreachable(self: AcpRuntime, active_agent: str, kas_agents: Any) -> None:
    """Refuse a session whose projected spec cannot load what this process defers.

    The Tool Search setting is process-wide on a wire-settings host: it was
    decided at spawn from the spawn agent's spec as it stood then. What a
    session RUNS is the projection built now -- a different agent, or the same
    agent whose user-level spec has since lost the grant -- and if that grants
    no loader its MCP specs would be deferred with no way back, the exact shape
    the handshake gate prevents. So the projected payload is judged every
    time, never the agent's name. There is no per-session knob to send, so the
    session is refused as a binding error, which the run-runtime caller already
    answers by giving the session a runtime of its own (whose handshake then
    decides afresh); a foreground caller surfaces it as the session error.
    """
    # ``getattr``: a bare runtime built with ``object.__new__`` for the
    # projection alone (the same shape ``_kas_custom_agents`` tolerates for
    # ``_work_dir``) has never handshaken and so has nothing to enforce.
    wire = getattr(self, "_tool_search_wire", {}).get("toolSearch")
    if not (isinstance(wire, dict) and wire.get("enabled")) or not kas_agents:
        return
    if any(spec_grants_tool_search(a) for a in kas_agents if isinstance(a, dict)):
        return
    raise AcpToolSurfaceBindingError(
        f"agent {active_agent!r} grants no tool_search loader, but this process "
        f"(spawned for {self._agent!r}) runs with MCP Tool Search deferral on; "
        "its MCP tools would be unreachable -- create a runtime for the agent"
    )


async def _kas_custom_agents(
    self: AcpRuntime,
    agent: str,
    *,
    member_dispatch: bool = False,
    crew_panel: bool = False,
    session_key: str = "",
) -> SessionExtras:
    """The per-session payload for a wire-registered host, and what built it.

    ``custom_agents`` is None when the host took its agent at spawn time.
    ``derived_spec_snapshot`` is the generation that payload was built from, and the
    activation check compares against IT rather than re-reading the file: a
    ``set_mode`` activates a definition that is already registered, so a fresh read
    there would judge a different artifact than the one the host holds.

    A thin read of the harness's per-session extras, kept under this name
    because both session-start paths and the tests around them ask for it
    here. The overlay is handed down rather than looked up by the harness:
    only this layer holds it, and the projection has to subtract the servers
    that will ALSO arrive as session-level broker stubs, because a
    session-injected server outranks an agent-declared one and declaring both
    is a double registration.
    """
    extras = await self._harness.session_extras(
        agent,
        work_dir=getattr(self, "_work_dir", None),
        mcp_gateway_overlay=self._mcp_gateway_overlay,
        member_dispatch=member_dispatch,
        crew_panel=crew_panel,
        session_key=session_key,
    )
    # Judged HERE, on the payload, so every path that builds one -- session/new
    # and session/load alike -- is covered, and a host that builds none (kiro:
    # ``custom_agents`` is None) never reaches the check.
    self._refuse_if_loader_unreachable(agent, extras.custom_agents)
    return extras


async def _mount_member_panel(
    self: AcpRuntime,
    mcp_servers: list[dict[str, Any]],
    *,
    member_session_key: str,
    agent_name: str,
    session_work_dir: Any,
    stub_token: str,
    resuming: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Mount the crew-panel server into a member DM session's server array.

    Returns the array and whether the GRANT may follow it. The two answers are
    one call because they must agree: a grant that outlived the mount would
    leave a switched-off server both named in ``tools`` and pre-approved on the
    very session that is not mounting it, which is the shape
    ``member_dispatch`` already avoids by deriving its flag from its own
    withhold.

    Asked on the resume path as well as on create, and it matters MORE there:
    ``session/load`` re-initializes the session's servers, so an unasked
    question would re-mount a switched-off server onto a conversation whose
    ``session/new`` withheld it.

    Two withholds, each the operator's own, and BOTH spellings of the switch:

    * ``agent.crew_panel`` -- the config ceiling, read through
      :func:`~kiro_crew.members.crew_panel_enabled`, which fails closed on an
      unreadable or degraded config.
    * a whole-server ``disabled`` on ``kirocrew-panel``. ``disabled`` has no
      per-tool or per-call spelling, so a harness handed the server cannot
      refuse a call to it, and the ``tools`` allowlist that keeps a disabled
      server out of a projected array does not reach an entry appended here.
    * a per-tool ``disabledTools`` naming any panel verb. Asked HERE and not
      only on the client sibling, because the two paths serve different
      backends and this one is the only path KAS takes: ``disabledTools`` is a
      hand-editable documented key in the global ``settings/mcp.json`` that
      :func:`~kiro_crew.acp.session_mcp.session_mcp_disabled_tools` reads, and
      the KAS grant that follows this mount puts ``panel_publish`` into
      ``allowedTools`` approval-free. KAS has no wire slot for hooks, so there
      is no later point at which a call to the switched-off verb could be
      refused -- withholding is the only faithful answer, and an operator's
      per-tool switch-off would otherwise be silently undone.

    The per-tool withhold takes the WHOLE server on every runtime-served
    backend rather than only where withholding is the sole deny channel. The
    mount and the grant are one answer here by construction, so keeping the
    mount for a backend that can refuse per call (codex) while withholding the
    grant would need two, and a grant that outlived a withhold is the failure
    this coupling exists to prevent. Withholding a server is an availability
    cost; forwarding an un-narrowed one is a capability the user switched off.

    Asked PER SERVER rather than inherited from the dashboard server's answer:
    the panel and session control are separate capabilities with separate
    switches, so an operator who withdrew session control keeps the drawer
    they never asked to lose, and one who switched the panel off loses only
    the panel.
    """
    if not member_session_key:
        return mcp_servers, False
    # circular import: members' module graph is heavy; resolved at call time
    # like the dispatch seam on both paths.
    from kiro_crew.members import (
        MEMBER_PANEL_SERVER,
        crew_panel_enabled,
        member_panel_session_server,
    )

    where = " on resume" if resuming else ""
    if not await asyncio.to_thread(crew_panel_enabled):
        logger.info(
            "member session %s: agent.crew_panel is off, so the crew panel is "
            "not mounted%s; the member keeps its other tools",
            member_session_key,
            where,
        )
        return mcp_servers, False
    if await asyncio.to_thread(
        session_mcp_server_is_disabled,
        MEMBER_PANEL_SERVER,
        agent_name,
        work_dir=_disable_check_scope(self.acp_backend, session_work_dir),
    ):
        logger.warning(
            "member session %s: %s is switched off for this session (disabled), "
            "so the crew panel is not mounted%s; re-enable that server to restore it",
            member_session_key,
            MEMBER_PANEL_SERVER,
            where,
        )
        return mcp_servers, False
    narrowed = await asyncio.to_thread(
        session_mcp_disabled_tools,
        agent_name,
        work_dir=_disable_check_scope(self.acp_backend, session_work_dir),
    )
    if any(server == MEMBER_PANEL_SERVER for server, _tool in narrowed):
        logger.warning(
            "member session %s: one of %s's tools is switched off, and the grant "
            "that follows this mount is approval-free with no later point to "
            "refuse the call, so the crew panel is not mounted%s; stop narrowing "
            "that server to restore it",
            member_session_key,
            MEMBER_PANEL_SERVER,
            where,
        )
        return mcp_servers, False
    entry = await asyncio.to_thread(member_panel_session_server, member_session_key, stub_token)
    if entry is None:
        logger.warning(
            "member session %s: panel server unresolved%s -- the member runs "
            "without a panel this session",
            member_session_key,
            where,
        )
        return mcp_servers, False
    # Session-level entries outrank same-named spec entries, so drop any stub
    # for the same server rather than registering it twice.
    return [e for e in mcp_servers if e.get("name") != entry["name"]] + [entry], True


async def _session_start_budget(self: AcpRuntime) -> float:
    """The session/new + session/load budget, resolved per session start.

    The config watcher's snapshot is a plain attribute read, so when it is
    armed every session start on this runtime reads the CURRENT
    ``agent.session_start_timeout_secs`` with no I/O -- a config write from
    any writer governs the next session/new on an already-running runtime.
    Before the watcher is armed (early boot, CLI, tests) the value is
    resolved once off-loop and cached: ``_resolve_session_start_timeout``
    calls ``KiroCrewConfig.load()``, which on a cache miss is a synchronous
    disk read + schema validation, and the request paths must not pay that
    per call. The same floor applies on both paths.
    """
    snap = live.snapshot()
    if snap is not None:
        try:
            return max(_SESSION_NEW_TIMEOUT, float(snap.agent.session_start_timeout_secs))
        except Exception:
            logger.debug("session-start timeout snapshot unreadable — using cache", exc_info=True)
    if self._session_start_timeout is None:
        self._session_start_timeout = await asyncio.to_thread(
            runtime_start._resolve_session_start_timeout
        )
    if getattr(self, "_start_collect_timeout", None) is None:
        # Same off-loop resolve, so the timeout path (which cannot block on
        # disk) finds the collector's cleanup budget already cached.
        self._start_collect_timeout = await asyncio.to_thread(_resolve_start_collect_timeout)
    return self._session_start_timeout


async def _mirrored_session_mcp(
    self: AcpRuntime,
    agent: str | None,
    *,
    work_dir: str | Path,
    session_key: str,
    channel_id: str,
) -> _MirroredSessionMcp | None:
    """This session's ``mcpServers``, built by the host's agent-config MIRROR.

    **Only called when the host HAS a mirror.** The decision is a synchronous
    in-memory registry read at the call site
    (:func:`~kiro_crew.providers.mirrors.registry.has_mirror`), not an await that
    returns ``None``, because H13 asks the shared construction path to stay a
    synchronous read: kiro and KAS reach their servers natively and must not gain
    an awaited step, a new call frame or a new failure mode in service of an
    adapter. ``load_session`` states the same requirement for its own resume path
    in as many words. The guard below is therefore a second line of defence rather
    than the gate, and it reads the registry rather than naming a backend, so a
    host joining ``providers.mirrors`` inherits the projection.

    A MIRRORED host cannot take the pooled array directly. The projection does two
    things the pooled resolution does not: it WITHHOLDS a broker stub for a server
    the agent's ``tools`` never references, and it returns the per-tool deny set
    the driver enforces at the approval request. Handing the raw array to the
    harness applies neither -- the harness narrows transports and nothing else --
    and a mirrored host approves its own tools internally, so an unprojected stub
    is a live tool surface Crew never granted.

    Both halves of the array go through ONE owner. The stubs are handed down as
    ``stub_elements`` rather than appended afterwards, because a stub carries the
    same ``name`` as the spec entry it rewrites: an append after the projection
    withheld that name un-withholds it, as the UNRESTRICTED server of the two.

    The stub token is minted BEFORE the projection, so it rides the stubs the
    projection keeps and no spec-translated element ever carries it -- the token
    names this session's brokered servers, and :meth:`_own_stub_session` would
    stamp every element of an array it was handed.

    ``session_key`` and ``channel_id`` reach the ELEMENT because they cannot reach
    the child any other way: a codex stdio server starts from ``env_clear()`` plus
    an allowlist and inherits nothing, so Crew's own control plane comes up with no
    session to act on unless the element carries the identity.

    ``permission_surface_owned`` is False because this runtime authors no native
    permission file. That is the fail-CLOSED direction: a mirror in claude's class
    -- one whose tools could be pre-approved in a file Crew does not own, where
    Crew's gate never fires -- withholds its array here rather than delivering it.

    Blocking work (an agent-spec parse, an overlay read) runs off the loop, the
    same as the pooled resolution it replaces.
    """
    # ONE registry read, not two. ``has_mirror`` is ``backend in MIRRORS`` and this
    # returns ``MIRRORS[backend]()`` when present, so a second read could only ever
    # agree -- and a branch for the disagreement would be dead code whose only
    # possible direction is wrong: falling through to the caller's pooled branch
    # mounts the unprojected broker stubs this whole method exists to withhold.
    # There is no degraded-but-running option worth having for that class, so the
    # state is made unrepresentable instead of handled.
    mirror = mirror_for(self.acp_backend)
    if mirror is None:
        return None
    active_agent = agent or self._agent
    try:
        stubbed = await asyncio.to_thread(
            injection_server_names,
            self._mcp_gateway_overlay,
            active_agent,
            # Same checkout the projection below resolves the agent SPEC
            # against, so the withheld set and the injected set are read from
            # one agent file rather than two.
            **overlay_project_scope(self.acp_backend, work_dir),
        )
    except Exception:
        # Same direction as the AcpClient path: an empty set re-declares a stubbed
        # server, where the session-level injection still outranks it, rather than
        # withholding a server nothing else supplies.
        logger.warning(
            "could not resolve pooled stub names; the session MCP array may re-declare one",
            exc_info=True,
        )
        stubbed = frozenset()
    stubs = await asyncio.to_thread(
        pooled_session_servers,
        self._mcp_gateway_overlay,
        active_agent,
        channel_id or None,
        **overlay_project_scope(self.acp_backend, work_dir),
    )
    stubs, stub_token = await self._own_stub_session(stubs, session_key)

    def _project_and_snapshot() -> tuple[Any, Any]:
        # One hop, two reads of the same file: the projection the array is
        # built from and the snapshot the unresolved-ref guard judges against.
        # Both go through session_mcp's own resolution order, so they resolve
        # the same spec FILE; the bytes can still differ if a save lands
        # between the two reads. That window is accepted for the snapshot,
        # because its only consumer is a diagnostic: a mismatch costs one
        # possibly-wrong warning, never the array, and threading the
        # projection's own parse through every mirror is a wider change than
        # that warning is worth. The projection may refuse (a stale derived
        # spec is the session's MCP surface, so refusing IS the answer); the
        # snapshot may not, and resolves to None instead.
        # This owner is type-checked before the class that binds it, so mypy checks the
        # closure on its own and loses the ``mirror is None`` narrowing above.
        projection = mirror.session_projection(  # type: ignore[union-attr]
            active_agent,
            stub_server_names=stubbed,
            stub_elements=stubs,
            permission_surface_owned=False,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            # THIS session's own name, minted just above. It reaches the projection
            # for the same reason ``session_key`` does -- the control-plane elements
            # are the only carriers a mirrored host has -- but it answers a question
            # the key cannot: on a shared runtime the key of the session that
            # CLAIMED the process is not the key of the subagent session running on
            # it, and after a warm-pool rekey the key baked into an element names the
            # previous owner. The token is per session and its mapping is
            # republished, so it stays right in both cases.
            session_token=stub_token,
        )
        return projection, _ref_spec_snapshot(active_agent, work_dir)

    projection, ref_spec = await asyncio.to_thread(_project_and_snapshot)
    servers = projection.params.get("mcpServers") or []
    return _MirroredSessionMcp(
        servers=list(servers) if isinstance(servers, list) else [],
        denied_tools=projection.denied_tools,
        stub_token=stub_token,
        derived_spec_snapshot=projection.derived_spec_snapshot,
        ref_spec=ref_spec,
        agent_spec=projection.agent_spec,
    )


def _mirrored_spec_check_needed(self: AcpRuntime, snapshot: Any) -> bool:
    """Whether this session has a derived spec to re-check. Synchronous.

    Split from :meth:`_require_unchanged_mirrored_spec` so the two session-start
    paths ask the question with an ordinary attribute test instead of awaiting a
    coroutine that would answer ``None`` for every host with no mirror -- the same
    H13 requirement that keeps the array decision a synchronous read.
    """
    return snapshot is not None


def _member_projection_needs_confirmation(self: AcpRuntime, mirror_used: bool) -> bool:
    """Whether this start must confirm the array a mirror built. Synchronous.

    Asks about the ARRAY's origin, never about the derived spec: a member's own
    saved template is not a derived agent, so ``require_fresh_derived_spec``
    answers ``None`` for it and its ``derived_spec_snapshot`` is None while the
    mirror still built the session's array from it. Keying the confirmation on
    that snapshot -- the freshness bracket's own question -- skipped every
    non-derived member, whose session then carried an empty
    ``confirmed_projection_template`` for ``loaded_stamp`` to refuse as
    ``capability_runtime_unverified``.

    The member read keeps a non-member session on a mirrored host byte-identical
    to before (``create_session``'s docstring promises as much for an empty
    ``member_session_key``), and *mirror_used* is False on every host with no
    mirror, so the kiro construction path takes no step of its own (H13). It is
    a decision about the start, not about the spec's bytes: a member whose
    projection consumed no spec at all is still confirmed, and refused by
    :func:`~kiro_crew.agent_capabilities_consumed.confirm_consumed_projection`.
    """
    return mirror_used and bool(self._member_context)


async def _require_unchanged_mirrored_spec(
    self: AcpRuntime, session_id: str, snapshot: Any
) -> None:
    """End the session when the spec it was built from changed under it.

    The other end of the bracket :meth:`_mirrored_session_mcp` opens. For a
    mirrored host the array IS the derived spec -- the child reads no spec of its
    own -- so a write landing between the array's build and the host consuming it
    would leave a session running restrictions nobody agreed to. Called once
    ``session/new`` / ``session/load`` has returned: a write landing after that
    point cannot change what the host already registered.

    Only called when :meth:`_mirrored_spec_check_needed` said there is one, so the
    kiro and KAS paths never reach it. The guard stays as a second line of defence.
    """
    if snapshot is None:
        return
    from kiro_crew.agent import DerivedSpecStale, require_unchanged_derived_spec

    try:
        await asyncio.to_thread(require_unchanged_derived_spec, snapshot)
    except DerivedSpecStale as exc:
        # session/new already succeeded, so the shared process holds this session;
        # a plain local unregister would leak it (the same reason the activation
        # bracket terminates rather than returns).
        await self.terminate_session(session_id)
        raise AcpRuntimeError(str(exc)) from exc


async def _confirm_member_projection(self: AcpRuntime, handle: AcpSessionHandle) -> None:
    """Refuse a member session whose consumed projection is not the saved intent.

    Called from the adapter-only session permission-routing body for a member
    whose array a mirror built. Kiro and KAS return before that body (H13).
    The runtime records the consumed spec on *handle* from the session's array,
    never from a re-read of the file after startup. A member session
    whose array was withheld or built from a different spec than the saved intent
    is refused; a session with no saved intent has nothing to confirm.
    """
    if not self._member_context:
        return
    from kiro_crew.acp.transport_errors import AcpError
    from kiro_crew.agent_capabilities import CapabilityError
    from kiro_crew.agent_capabilities_consumed import confirm_consumed_projection

    try:
        gaps = await asyncio.to_thread(
            confirm_consumed_projection, self._agent, handle.consumed_agent_spec
        )
    except CapabilityError as exc:
        # session/new (or session/load) already succeeded, so the shared
        # process holds this session; a plain local unregister would leak it
        # (the same reason the mirrored-spec bracket terminates rather than
        # returns).
        await self.terminate_session(handle.session_id)
        raise AcpError(f"{exc.code}: saved spec projection could not be confirmed") from exc
    if gaps is None:
        return
    handle.confirmed_projection_template = self._agent
    handle.capability_projection_gaps = gaps


async def _own_stub_session(
    self: AcpRuntime, entries: list[dict[str, Any]], session_key: str
) -> tuple[list[dict[str, Any]], str]:
    """Mint the token that names ONE session on this shared runtime.

    Returns *entries* carrying the token plus the token itself, which the
    caller records on the session handle so a later ``rekey()`` can name the
    same session — and stamps onto this session's control-plane elements.

    Every OTHER identity channel a Crew MCP child has is keyed on the process
    tree, and this runtime multiplexes N sessions over ONE kiro-cli process, so
    each of them resolves a ``spawn_run`` subagent's server to the PARENT slot
    and lets a parent re-claim overwrite it. The token is the per-SESSION name
    that tree cannot supply.

    Minted UNCONDITIONALLY. A reachable gatewayd socket is not a precondition,
    because gatewayd is not the token's only reader: the token -> session-key
    mapping is published to a MAC-signed file that the strict identity resolver
    reads with no daemon (:mod:`kiro_crew.session_token_sig`). The socket gates
    the CLAIM alone — with the gateway off, ``entries`` is empty and this still
    returns a live token for the control-plane elements to carry.

    Publication happens here, before ``session/new``, and off the event loop:
    a child launched while that request is served may resolve its identity
    before the first turn's republication, so the mapping has to exist by the
    time the element does.

    When the owning session key is already known, the claim is pushed HERE,
    before ``session/new``, and awaited: kiro-cli launches this session's
    stubs while serving that request, so a claim sent afterwards would race
    the register it exists to inform. Best-effort — ``send_claim`` swallows a
    missing/wedged gatewayd under its own timeout and returns False, and a
    session whose key is not known yet (a warm-pool worker, claimed later)
    is named by the ``rekey()`` claim instead.
    """
    token = mint_stub_session_token()
    entries = attach_stub_session_token(entries, token)
    if session_key:
        await asyncio.to_thread(publish_session_token, token, session_key)
    if entries and self._mcp_gateway_socket and session_key and self.pid:
        await send_claim(
            self._mcp_gateway_socket,
            self.pid,
            session_key,
            None,
            token,
        )
    return entries, token


async def _unpooled_control_planes(
    self: AcpRuntime, entries: list[dict[str, Any]], agent: str | None, work_dir: str | Path
) -> list[dict[str, Any]]:
    # A shared Kiro process has no session-valued environment. Its native
    # managed servers need per-element identity even with the broker off.
    if self.acp_backend == ACP_BACKEND_KIRO:
        from kiro_crew.acp.session_mcp import kiro_control_plane_servers

        # The spec kiro-cli runs: a launch copy for this runtime's own agent,
        # read by the copy's name, never through a view built by a name lookup
        # that could reach the checkout's same-named spec.
        spec_agent = self._wire_mode_id(agent or self._agent)
        projection = getattr(self, "_native_skill_projection", None)
        projection_kwargs: dict[str, Any] = (
            {"spec_override": projection.specs.get(spec_agent)} if projection is not None else {}
        )
        if self._launch_agent and spec_agent == self._launch_agent:
            # The copy as read by its path at spawn, whether or not a projection runs:
            # a name lookup here would reach a checkout's spec of the copy's name.
            if self._launch_copy_spec is None:
                raise AcpRuntimeError("the app agent's launch copy was not read at spawn")
            projection_kwargs = {"spec_override": self._launch_copy_spec}
        mount = await asyncio.to_thread(
            kiro_control_plane_servers,
            spec_agent if spec_agent != (agent or self._agent) else agent,
            work_dir=work_dir,
            existing_names={str(entry.get("name")) for entry in entries},
            **projection_kwargs,
        )
        name = spec_agent
        if self._launch_agent and name == self._launch_agent:
            # The copy's own verdict, kept whether or not a projection runs.
            from kiro_crew.apps.agent_launch import is_search_launch_copy

            search_agent = is_search_launch_copy(name)
        else:
            search_agent = projection is not None and name in projection.search_agents
        if search_agent and not any(
            entry.get("name") == "kirocrew-core" for entry in mount.elements
        ):
            # The mount did not produce the element itself: a settings file
            # withheld it, or a broker stub already in ``entries`` pre-empted
            # it (the mount emits no element for a name the array carries),
            # or nothing grants it. The mount judged the files as they are
            # now -- the projection judged the spec at spawn; the dashboard's
            # tool toggle writes the global file while a runtime is warm, and
            # a stub written from an earlier overlay carries the restriction
            # no better than the element does -- and its verdict comes with
            # the array, from the same read. Asking the files again here
            # would open the window in which a toggle undone in between
            # answers "allowed" and this session falls to the generic guard
            # below, naming the wrong file. A withheld element refuses THIS
            # session with the sentence that names the file and the way
            # back; the spawn and every other agent's sessions stand.
            # Refusing is the only honest answer: the view already dropped
            # this agent's skill resources on the element's promise, and
            # without the element kirocrew-core mounts natively, carries no
            # identity and answers identity_unattested to every skill_search.
            withheld = mount.withheld.get("kirocrew-core")
            if withheld is not None:
                raise AcpRuntimeError(f"Agent {name!r}: {withheld.explain('skill search')}")
            if not any(entry.get("name") == "kirocrew-core" for entry in entries):
                raise AcpRuntimeError(
                    "Cannot bind skill_search to this session without losing native MCP "
                    "restrictions. Check the agent's kirocrew-core server configuration."
                )
        return [*entries, *mount.elements]
    return entries


def _copy_native_launch_sources(
    self: AcpRuntime,
    handle: AcpSessionHandle,
    *,
    set_mode_ran: bool = False,
    activated_alias: str | None = None,
) -> None:
    """Copy the launch record to *handle*; the non-member record only on its view.

    A member record (``plan.native_context_documents``) records no view alias
    and is copied whole. The non-member record names the guides the projected
    view it was taken from declares, so a session on another view of the same
    agent gets none of it: that view may declare fewer guides, and the folder
    would skip one kiro-cli never loads. When ``set_mode`` ran, the alias it
    sent is the session's view; otherwise the session runs on the alias the
    process was launched with, and the record applies while the projection
    still names that alias for the agent.
    """
    launch_resources = self._native_launch_resources
    work_dir = str(self._work_dir)
    handle.native_context_documents_fit = (
        functools.partial(
            member_essential_context._declared_files_fit,
            {"resources": launch_resources},
            work_dir,
        )
        if launch_resources is not None
        else None
    )
    recorded_alias = self._native_launch_view_alias
    if recorded_alias is not None:
        if set_mode_ran:
            current_alias = activated_alias
        else:
            projection = getattr(self, "_native_skill_projection", None)
            try:
                current_alias = projection.agent(self._agent) if projection is not None else None
            except ValueError:
                current_alias = None
        if current_alias != recorded_alias:
            return
    handle.native_context_documents.update(self._native_launch_sources)


def _guard_unresolved_mcp_refs(
    self: AcpRuntime,
    handle: AcpSessionHandle,
    spec: Any,
    agent: str | None,
    wire_servers: Any,
) -> None:
    """Warn when the spec's ``@server`` refs name nothing in Crew's projection.

    The runtime-path twin of ``AcpClient._guard_unresolved_mcp_refs``, and it
    exists because a host served here rather than by the client would otherwise
    be the one host whose unresolved refs are never reported -- which for a host
    that reads no agent file of Crew's is the normal case the guard was written
    for, not a corner.

    *wire_servers* is the FINAL array -- harness-filtered projection plus broker
    stubs -- so this is the last point at which "which servers does this session
    actually get" can be known. Judging the pre-filter roster would report a ref
    as satisfied by a server the wire never carried.

    Synchronous, in-memory and non-raising, in that order of importance (H13).
    *spec* was read in the same off-loop hop that resolved the array, so this
    adds no scheduling point to any host's session start; ``None`` -- no hop
    (a caller-supplied array) or an unreadable spec -- means nothing to say.
    Every failure resolves to silence rather than a failed session, because a
    diagnostic that can fail a session is a worse defect than the one it
    detects. It changes nothing: not the array, not the session's fate.
    """
    if spec is None:
        return
    try:
        unresolved = warn_unresolved_server_refs(
            spec,
            wire_servers,
            backend=self.acp_backend,
            agent=agent or "",
            gateway_enabled=self._mcp_gateway_overlay is not None,
        )
        if unresolved:
            handle.mcp_session_report().record_unresolved_refs(unresolved)
    except Exception:
        logger.debug("unresolved-ref guard: evaluation failed", exc_info=True)
    try:
        header_refs, omitted = warn_remote_header_secret_refs(
            spec, wire_servers, backend=self.acp_backend, agent=agent or ""
        )
        if header_refs:
            handle.mcp_session_report().record_header_secret_refs(header_refs, omitted)
    except Exception:
        logger.debug("remote-header guard: evaluation failed", exc_info=True)
    try:
        # The same spec names the servers an unattended run waits for, but
        # only on kiro-cli: it launches the spec's own servers (via --agent)
        # and reports each one's start. Elsewhere a spec server may be held
        # back or never report, and every run would wait out the bound.
        if self._expect_mcp_reports and self.acp_backend == ACP_BACKEND_KIRO:
            handle.mcp_session_report().record_declared(spec)
    except Exception:
        logger.debug("declared MCP servers: spec unreadable", exc_info=True)


def _start_mount_check(
    self: AcpRuntime,
    handle: Any,
    servers: Any,
    session_id: str,
    session_key: str,
    request_sent_at: float | None = None,
) -> None:
    """Start the session-open mount check, as ``AcpClient`` does.

    Only for ``ACP_BACKENDS_SESSION_MCP_ARRAY``; a background task that never
    blocks or fails the session (:mod:`kiro_crew.acp.mcp_mount_check`). It
    records nothing for a session that has left ``_session_queues``.
    *request_sent_at* is when the ``session/new`` or ``session/load`` went out,
    a late-adopted start included; without it the check's own start time
    stands in.
    """
    if self._acp_backend not in ACP_BACKENDS_SESSION_MCP_ARRAY:
        return
    schedule_mount_check(
        handle.mcp_session_report(),
        servers,
        socket_path=self._mcp_gateway_socket,
        backend=self._acp_backend,
        session_id=session_id,
        session_key=session_key,
        opened_at=request_sent_at,
        alive=lambda: session_id in self._session_queues,
    )
