"""Compose an ``AcpClient`` session's ``mcpServers`` array.

Member mounts, pooled broker stubs, the per-harness translations, the pi handoff,
pruning of servers that cannot start, the session's MCP registration report, and the
spec's zero-tools answer.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_capability_projection_gaps``,
``_loaded_capability_template``, ``_mcp_mount_opened_at``, ``_mcp_mount_servers``,
``_mcp_pruned``, ``_mcp_pruned_names``,
``_mcp_pruned_omitted``, ``_mcp_report_frame_floor``, ``_member_dispatch_mounted``,
``_pending_oauth_requests``, ``_pi_mcp_handoff``, ``_session_agent_spec``,
``_session_harness_deny_rules``, ``_session_mcp_cache``,
``_session_mcp_excludes_local``, ``_session_mcp_inline_settings``,
``_session_mcp_snapshot``, ``_session_mcp_unhonoured``, ``_spec_denied_tools``,
``_spec_zero_tools``.

Instance fields read: ``_agent``, ``_channel_id``, ``_claude_inline_settings``,
``_claude_local_settings_excluded``, ``_mcp_gateway_overlay``, ``_mcp_gateway_socket``,
``_mcp_notifications``,
``_mcp_ref_spec``, ``_mcp_report``, ``_native_skill_projection``,
``_opencode_config_mcp_servers``, ``_opencode_denies_in_force``, ``_pi_gate_nonce``,
``_pi_mcp_server_names``, ``_session_id``, ``_session_key``, ``_side_read_only``,
``_stub_session_token``, ``_work_dir``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Collection

from kiro_crew import acp_tool_gate
from kiro_crew.acp.mcp_mount_check import schedule_mount_check
from kiro_crew.acp.mcp_ref_guard import warn_remote_header_secret_refs, warn_unresolved_server_refs
from kiro_crew.acp.mcp_session_report import BUCKET_CAP as MCP_REPORT_BUCKET_CAP
from kiro_crew.acp.mcp_session_report import ERROR_CAP as MCP_REPORT_ERROR_CAP
from kiro_crew.acp.mcp_session_report import NAME_CAP as MCP_REPORT_NAME_CAP
from kiro_crew.acp.mcp_session_report import McpSessionReport, sanitize_sink_text
from kiro_crew.acp.mcp_startable import PrunedServer, prune_unstartable
from kiro_crew.acp.session_mcp import agent_spec_snapshot
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    MCP_ROSTER_COMPLETE_NOTE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    overlay_project_scope,
)
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.mcp_gateway.session_servers import (
    attach_stub_session_token,
    injection_server_names,
    pooled_session_servers,
)
from kiro_crew.providers.mirrors import MIRRORS, ProjectionKind, mirror_for, projection_for
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


def authored_spec_declares_zero_tools(self: AcpClient) -> bool:
    """True when the authored spec this agent name resolves to declares ``"tools": []``.

    Reads the spec through :func:`agent_spec_snapshot`, project checkout first and
    user level second, which is the order the harness resolves ``--agent`` in, so
    a project-level spec that shadows the installed one is the one judged.

    Blocking (reads the spec); async callers run it with ``asyncio.to_thread``.

    Fails closed: an unreadable spec, a non-dict result, and a ``tools`` that is
    missing, not a list, or non-empty all answer False.
    """
    try:
        spec = agent_spec_snapshot(self._agent, work_dir=self._work_dir)
    except Exception:
        logger.warning(
            "agent spec for %r unreadable; not confirming zero tools",
            self._agent,
            exc_info=True,
        )
        return False
    if not isinstance(spec, dict):
        return False
    tools = spec.get("tools")
    return isinstance(tools, list) and not tools


def _published_alias_declares_zero_tools(alias: str) -> bool:
    """True when the alias file the harness loads as ``--agent <alias>`` declares ``"tools": []``.

    That file is the one the projection published under the user-level agents
    directory. A checkout cannot claim an alias name
    (:mod:`kiro_crew.acp.managed_agent_shadow` refuses the spawn), so it is the file
    the harness resolved. Read through the hardened spec reader, which answers
    ``None`` for a file that is absent, unreadable, linked, oversized or not an
    object. Blocking, and fails closed: anything but an empty ``tools`` list is
    False.
    """
    from kiro_crew.acp import skill_projection

    spec = _read_agent_spec(
        skill_projection.kiro_agents_dir() / f"{alias}.json",
        operation="zero_tool_confirmation",
        source="acp",
    )
    if not isinstance(spec, dict):
        return False
    tools = spec.get("tools")
    return isinstance(tools, list) and not tools


def effective_spec_declares_zero_tools(
    self: AcpClient, *, authored_before_spawn: bool = False
) -> bool:
    """True when the spec the harness runs under declares an empty ``tools`` list.

    With a prepared projection the harness was launched as the view's published
    alias, so two things must declare an empty list: the view the session prepared
    before the spawn, and the alias file the harness loaded under that name, read
    after start. The view covers a project-level spec that shadows the installed one
    and any tool the projection adds, such as skill search for a spec carrying
    ``skill://`` resources. The file read covers a change written to the published
    file after the view was prepared. The authored spec is not read, so a change to
    it after preparation cannot change the answer, and *authored_before_spawn* is
    ignored. Unlike :attr:`spec_zero_tools` this needs no mirror, so it can confirm
    the ban on a backend that enforces it natively.

    With no prepared projection (the projection switched off, or its alias
    could not be taken) the harness reads the named spec itself. The caller
    reads that spec before the spawn and passes the result as
    *authored_before_spawn*; this method reads it again after the harness
    started, and both reads must declare an empty list.

    In both branches the post-start read narrows a change to the file the harness
    loaded, but cannot close one made and restored inside the spawn window.

    Fails closed: a projection with no view or no alias for this agent, a view
    whose ``tools`` is missing or non-empty, and a published file that is missing,
    unreadable or not an empty list are all unconfirmed, and so is a missing
    projection whose pre-spawn read was not confirmed or whose post-start read is
    not an empty list.

    Both branches read a file, so async callers run this method with
    ``asyncio.to_thread``.
    """
    projection = getattr(self, "_native_skill_projection", None)
    if projection is None:
        return authored_before_spawn and self.authored_spec_declares_zero_tools()
    view = projection.specs.get(self._agent)
    if not isinstance(view, dict):
        return False
    tools = view.get("tools")
    if not (isinstance(tools, list) and not tools):
        return False
    alias = projection.aliases.get(self._agent)
    if not isinstance(alias, str) or not alias:
        return False
    return _published_alias_declares_zero_tools(alias)


def _pooled_mcp_servers(self: AcpClient) -> list[dict[str, Any]]:
    """Broker-stub ``mcpServers`` entries for this session's ``session/new``.

    A session-injected server outranks the same-named entry in the resolved
    agent spec, so injecting the stubs here is what actually pools the
    servers — nothing is written to the user's project or to
    ``~/.kiro/agents/``. Empty when the shared gateway is disabled.

    Empty for EVERY mirrored backend, with no per-backend branch: a mirror's
    projection places the stubs itself (``_resolve_session_mcp_servers`` hands
    them down as ``stub_elements``), so one withhold rule -- the spec's
    ``tools`` allowlist, codex's restriction set, claude's permission-surface
    precondition -- covers both halves of the array. A second unnarrowed
    append here would re-add, as an UNRESTRICTED broker stub, exactly the
    servers that projection withheld: a stub carries the same name as the
    entry it rewrites, so the withhold and the re-add are the same server.

    The shared append remains for the mirror-less backends: kiro-cli reads
    the spec itself via ``--agent``, so this array is the ONLY channel its
    stubs can arrive on, and the injection outranking the spec entry is what
    pools them there.

    Membership in ``MIRRORS`` rather than ``mirror_for``, because this sits
    on the shared session/new / session/load composition: ``mirror_for``
    RAISES for a backend registered in neither map, and kiro's construction
    path must not gain a failure mode in service of an adapter (H13).
    """
    if self.backend in MIRRORS:
        return []
    # A mirror-less member backend's own array (``_mirrorless_member_mounts``)
    # sits beside these stubs on the wire. Its entry carries the member's
    # identity, so a same-named stub is dropped rather than sent twice.
    mounted = {
        element.get("name")
        for element in (getattr(self, "_session_mcp_cache", None) or [])
        if isinstance(element, dict)
    }
    return [s for s in self._pooled_broker_stubs() if s.get("name") not in mounted]


def _resolve_session_mcp_servers(self: AcpClient) -> list[dict[str, Any]]:
    """Translate the agent spec into this session's ``mcpServers`` array.

    Blocking (reads the agent spec and the gateway overlay), so it runs off
    the loop from the spawn path and its result is cached for the call sites
    — see :meth:`_session_mcp_servers` for why the call sites must not do
    this work themselves.

    The names of the pooled broker stubs the caller appends are resolved here
    and passed down, because this layer is the one that holds the overlay: a
    stub wraps — and is keyed by — the same name as the agent-spec entry it
    rewrites, so translating both halves would put two elements with one
    ``name`` into a single array (either the raw entry shadows the stub and the
    session bypasses the broker, or both register and every pooled backend runs
    twice — #927). Empty on error is the safe direction, the same one the KAS
    projection takes: it re-declares a stubbed server, where the injection
    still outranks it, rather than withholding a server nothing else supplies.

    ``permission_surface_owned`` carries whether Crew GOVERNS this session's
    native permission file, because a mirror cannot know that on its own:
    either this client authored it, or it verified the file is a sibling
    session's byte-identical live seed (``_permission_surface_governed``). It
    is a precondition on delivering tools at all: Crew's gate fires on
    ``session/request_permission``, and a tool pre-approved in a file Crew does
    not govern never sends one. The claude mirror fails closed on it; a backend
    that gates natively ignores it. Read here, AFTER the writer has run on the
    spawn path, so the value describes this session's real state.
    """
    try:
        stubbed: Collection[str] = injection_server_names(
            # The same checkout the projection below resolves the agent SPEC
            # against: a project agent's stubs must be read from the file the
            # session is running, not from the user-level agent of that name.
            self._mcp_gateway_overlay,
            self._agent,
            **overlay_project_scope(self.backend, self._work_dir),
        )
    except Exception:
        logger.warning(
            "could not resolve pooled stub names; the session MCP array may re-declare one",
            exc_info=True,
        )
        stubbed = frozenset()
    mirror = mirror_for(self.backend)
    if mirror is None:
        return self._mirrorless_member_mounts(stubbed)
    # Recorded BEFORE the projection reads the governed flag, and beside the
    # array it authorizes: the session/new envelope reads this record, so an
    # array delivered under the exclusion always ships with it.
    excludes_local = bool(getattr(self, "_claude_local_settings_excluded", False))
    self._session_mcp_excludes_local = excludes_local
    self._session_mcp_inline_settings = (
        getattr(self, "_claude_inline_settings", None) if excludes_local else None
    )
    # The STRUCTURED face rather than the wire one, for every mirror alike: two
    # things can come out of the one spec parse and only one of them is wire
    # data -- the array, and a per-tool deny set this client enforces at the
    # approval request. The wire dict must not carry the second, and a second
    # parse for it would be the consistency window the projection exists to
    # close. A mirror with nothing off-wire to say answers with an empty set.
    projection = mirror.session_projection(
        self._agent,
        stub_server_names=stubbed,
        # The broker stubs as ELEMENTS, for a mirror that must place them itself
        # so one withhold rule covers both halves of the array (codex). A mirror
        # that leaves them to the shared append ignores them.
        stub_elements=self._pooled_broker_stubs(),
        permission_surface_owned=self._permission_surface_governed,
        work_dir=self._work_dir,
        # A codex stdio child starts from env_clear() plus an allowlist, so
        # Crew's own servers reach it with an identity only if the ELEMENT
        # carries one. A mirror cannot discover either value; the client can.
        session_key=self._session_key or "",
        channel_id=self._channel_id or "",
        # This session's own name, so its control-plane elements resolve
        # identity through the signed mapping rather than through an env key
        # a warm-pool rekey can leave stale.
        session_token=self._stub_session_token,
        # The deny rules the opencode read-back found in force (None before any
        # were seeded), so a later re-parse withholds a server whose rule is not.
        harness_denies_in_force=getattr(self, "_opencode_denies_in_force", None),
    )
    self._spec_denied_tools = projection.denied_tools
    self._session_mcp_unhonoured = projection.unhonoured_servers
    self._session_harness_deny_rules = projection.harness_deny_rules
    self._spec_zero_tools = projection.zero_tools
    # Kept beside the array it describes, so the post-consume check judges the
    # generation these elements were built from and not a later read of the file.
    self._session_mcp_snapshot = projection.derived_spec_snapshot
    self._session_agent_spec = projection.agent_spec
    servers = projection.params.get("mcpServers") or []
    out = list(servers) if isinstance(servers, list) else []
    # The restriction half of the projection's withhold set, from the SAME parse the
    # array came out of: the member append must not re-add a name this projection
    # refused on a transport where the withhold is the whole of the enforcement.
    return self._append_member_panel_server(
        self._append_member_dispatch_server(
            out, projection.restricted_servers, projection.disabled_servers
        ),
        projection.restricted_servers,
        projection.disabled_servers,
    )


def _mirrorless_member_mounts(self: AcpClient, stubbed: Collection[str]) -> list[dict[str, Any]]:
    """The member mounts of a backend with no mirror, which are its whole array.

    A mirror-less backend translates no spec, so the array Crew composes for it
    carries only what Crew adds on its own: the member session-control and panel
    entries. Empty for every backend outside both member sets, and for every
    non-member session, so an ordinary session's array is unchanged.

    The two switch-offs still bind, read from one parse of the spec and the
    global settings file. ``disabled`` reaches the appends as it does on a
    mirrored backend. A per-tool narrowing is recorded as UNHONOURED, so the
    whole server is withheld: no mirror translates the spec's deny set here, so
    nothing could refuse the narrowed tool once the server was mounted.

    Fail-closed: a spec that cannot be read mounts nothing, and the thread runs
    as plain chat.

    The same parse also answers the spec's ``"tools": []`` for a mirror-less host
    whose declared MCP projection is ``ProjectionKind.BROKER_ONLY`` (pi): no spec
    projection is written for it and the harness reads no spec of Crew's, so no
    mirror reports :attr:`SessionMcpProjection.zero_tools` there and nothing else
    could know the agent claims zero tools. Read here, beside the mounts, rather
    than by a second parse: a spec edited between two reads could otherwise mount
    by one generation and ban by another. It only RECORDS the answer;
    :attr:`_enforces_zero_tool_ban` decides whether this backend's permission
    requests are refused on it. kiro-cli's projection is ``NATIVE`` -- it reads its
    spec itself through ``--agent`` -- so it never reaches this record. An
    unreadable spec records False, the same as a mirror that could not parse.

    Who reads the spec is asked of the declared projection
    (``registry.PROJECTIONS``), the record that states it per backend, rather
    than of a membership set: the caller has already resolved that record
    through ``mirror_for``, so an undeclared backend raised before this point.
    """
    records_zero_tools = projection_for(self.backend).kind is ProjectionKind.BROKER_ONLY
    if (
        not records_zero_tools
        and self.backend not in ACP_BACKENDS_MEMBER_DISPATCH
        and self.backend not in ACP_BACKENDS_MEMBER_PANEL
    ):
        return []
    try:
        from kiro_crew.acp.session_mcp import session_mcp_projection

        projection = session_mcp_projection(
            self._agent,
            stub_server_names=stubbed,
            work_dir=self._work_dir,
            session_key=self._session_key or "",
            worker_tool_limits_honoured=self.backend in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
        )
    except Exception:
        logger.warning(
            "member session %s: agent spec unreadable, so no member tools are "
            "mounted -- the DM thread runs as plain chat this session",
            self._session_key,
            exc_info=True,
        )
        if records_zero_tools:
            self._spec_zero_tools = False
        return []
    if records_zero_tools:
        self._spec_zero_tools = projection.zero_tools
    if (
        self.backend not in ACP_BACKENDS_MEMBER_DISPATCH
        and self.backend not in ACP_BACKENDS_MEMBER_PANEL
    ):
        return []
    narrowed = frozenset(server for server, _tool in projection.disabled_tools)
    self._session_mcp_unhonoured = narrowed
    disabled = projection.disabled_servers
    return self._append_member_panel_server(
        self._append_member_dispatch_server([], narrowed, disabled),
        narrowed,
        disabled,
    )


def _pooled_broker_stubs(self: AcpClient) -> list[dict[str, Any]]:
    """The raw broker stubs for this session, with no per-backend narrowing.

    Split from :meth:`_pooled_mcp_servers` so codex can take them through its
    own withholding rules while that method keeps returning ``[]`` for codex at
    the shared call site. Blocking; both callers are already off the loop.

    Each entry carries this client's stub session token, which is what a
    claim names so gatewayd re-targets the stubs of THIS session. One client
    drives one kiro-cli process serving one session at a time, so the token
    is per client and is re-bound — not re-minted — by every ``rekey()``.
    """
    return attach_stub_session_token(
        pooled_session_servers(
            self._mcp_gateway_overlay,
            self._agent,
            self._channel_id,
            **overlay_project_scope(self.backend, self._work_dir),
        ),
        self._stub_session_token,
    )


def _append_member_dispatch_server(
    self: AcpClient,
    servers: list[dict[str, Any]],
    restricted: Collection[str] = (),
    disabled: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Mount the dashboard session-control server into a member DM session.

    Session-level and additive: the on-disk agent spec is untouched, so every
    other session on the same agent keeps its ordinary tool set. The entry
    carries ``KIROCREW_SESSION_KEY`` for strict identity — the same value this
    client already exports to the child process env.

    The permission-surface precondition is asked of the BACKEND's ROUTING --
    ``acp_tool_gate.is_enforced`` -- rather than read off
    ``_claude_settings_authored`` alone. That flag answers one harness's question:
    claude declares a routing this core does not enforce, so owning
    ``settings.local.json`` is what stands in for the read-back it lacks, and
    appending session control onto a surface Crew does not own would hand a
    pre-approvable file exactly the tools the mirror's withhold keeps off it. A
    harness whose routing IS enforced cannot satisfy that flag and does not need
    to -- its session is refused before its first prompt unless the gate arms --
    and its mirror documents the flag as accepted-and-ignored (see
    ``providers/mirrors/opencode.py``), so reading the flag there would withhold
    every member's tools on the strength of a condition that cannot describe the
    backend.

    A whole-server ``disabled`` on the dashboard server stops the mount outright,
    for every backend and with no second channel to weigh: ``disabled`` has no
    per-tool or per-call form, so a harness handed the server cannot refuse a call
    to it, and the ``tools`` allowlist that keeps it out of the spec-described half
    of the array does not reach an element this method appends itself. Mounting it
    anyway would make the operator's switch-off of session control a no-op for the
    one session type that holds the strongest tools Crew hands out.

    The other composer answers alike: ``AcpRuntime`` asks
    ``session_mcp.session_mcp_server_is_disabled`` on its create and resume paths,
    so a member session on an ``ACP_BACKENDS_ACP_RUNTIME`` host (codex, KAS) gets
    the same answer this method gives. It asks through that reader rather than
    through a field on the projection because half of those hosts have no mirror to
    carry one -- KAS projects through ``acp.kas_agents``, not an array.

    A per-tool restriction on the dashboard server is the second precondition,
    and it is the one this method can UNDO rather than merely fail: the projection
    withholds a narrowed server (*restricted*), and on a backend whose
    ``registry.PerToolDeny`` is ``WHOLE_SERVER`` that withhold is the ONLY
    enforcement there is -- no rule in a file the harness reads, and no per-call
    identity Crew can refuse by. Appending the entry back would make a tool the
    operator switched off callable again. So the mount is withheld instead and the
    thread runs as plain chat. A backend with a second channel keeps its mount:
    codex refuses the call at permission time from ``denied_tools``, and claude's
    deny rules refuse it inside the adapter.

    Both halves of a session's array must agree about this. ``AcpRuntime`` mounts
    the same entry on the resume and create paths, and :meth:`_foreign_mcp_identity`
    judges a trusted tool identity against the array THIS method returns -- scoped to
    ``ACP_BACKENDS_SESSION_MCP_ARRAY``, and reached only from
    :meth:`_refuse_identity_drift`, whose own gate is ``ACP_BACKENDS_META_IDENTITY``
    (goose alone today, so no backend mounted here runs it yet). A backend that later
    joins that set and mounts the server on one path while this one withholds it would
    refuse every dispatch call as a drifted server rather than run a plain chat.
    """
    # Every exit below answers whether THIS composition mounted session
    # control, so the context builder can teach the session_* tools only to a
    # session that holds them (see :attr:`member_dispatch_mounted`).
    self._member_dispatch_mounted = False
    if self.backend not in ACP_BACKENDS_MEMBER_DISPATCH:
        return servers
    # circular import: members' module graph is heavy; resolved at call time.
    from kiro_crew.members import (
        MEMBER_DISPATCH_SERVER,
        is_member_session_key,
        member_dispatch_session_server,
    )

    if not is_member_session_key(self._session_key):
        return servers
    session_key = self._session_key or ""
    if self._member_mount_withheld(MEMBER_DISPATCH_SERVER, "session control", restricted, disabled):
        return servers
    entry = member_dispatch_session_server(session_key, self._stub_session_token)
    if entry is None:
        logger.warning(
            "member session %s: dashboard server unresolved -- the DM thread "
            "runs as plain chat this session",
            self._session_key,
        )
        return servers
    self._member_dispatch_mounted = True
    return [e for e in servers if e.get("name") != entry["name"]] + [entry]


def _member_mount_withheld(
    self: AcpClient,
    server_name: str,
    capability: str,
    restricted: Collection[str],
    disabled: Collection[str],
) -> bool:
    """Whether a member session-array append must be withheld for *server_name*.

    The three preconditions :meth:`_append_member_dispatch_server` documents at
    length, asked once so both member mounts answer them the same way. Each is
    about the array, not about which capability rides it: a whole-server
    ``disabled``, a per-tool restriction on a backend where withholding is the
    only deny channel there is, and a permission surface Crew does not own.

    *capability* is the phrase the log uses for what the session loses, because
    that is the only part that differs between the two mounts.
    """
    if server_name in disabled:
        logger.warning(
            "member session %s: %s is switched off for this session (disabled), so "
            "%s is not mounted -- no backend can refuse a call to a server it was "
            "handed, and mounting it would undo that switch; re-enable that server "
            "to restore it",
            self._session_key,
            server_name,
            capability,
        )
        return True
    if server_name in getattr(self, "_session_mcp_unhonoured", frozenset()):
        logger.warning(
            "member session %s: one of %s's tools is switched off and this session "
            "could not put that restriction in force, so the projection withheld the "
            "server and mounting it here would make that tool reachable again; %s is "
            "not mounted",
            self._session_key,
            server_name,
            capability,
        )
        return True
    if server_name in restricted and self._withhold_is_the_only_deny_channel():
        logger.warning(
            "member session %s: one of %s's tools is switched off and this backend has "
            "no channel to refuse a call to it, so the projection withheld the server "
            "and mounting it here would make that tool reachable again; %s is not "
            "mounted, so stop narrowing that server to restore it",
            self._session_key,
            server_name,
            capability,
        )
        return True
    # An unenforced routing is the ONLY case the governed-surface fallback answers
    # for; see the precondition paragraph above for why an enforced one must not
    # read it.
    if not acp_tool_gate.is_enforced(self.backend) and not self._permission_surface_governed:
        logger.warning(
            "member session %s: permission surface not Crew-owned -- %s is not mounted",
            self._session_key,
            capability,
        )
        return True
    return False


def _append_member_panel_server(
    self: AcpClient,
    servers: list[dict[str, Any]],
    restricted: Collection[str] = (),
    disabled: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Mount the crew-panel server into a member DM session.

    The sibling of :meth:`_append_member_dispatch_server` and subject to the
    same three array-level preconditions, through the one reader
    :meth:`_member_mount_withheld`. One addition: ``agent.crew_panel``, the
    operator's single withdrawal of the capability, read fail-closed.

    Its own append rather than a widening of the dispatch one, because the two
    capabilities are assigned per server and withdrawn by separate switches: a
    member may hold session control without a panel, or a panel without session
    control, and each mount must answer for itself. For the same reason the
    backend question is read from :data:`ACP_BACKENDS_MEMBER_PANEL`, whose
    membership is argued for THIS capability: harness support for session
    control establishes nothing about the panel (harness-parity H6), so
    reusing the dispatch set would grant one capability on another's evidence.
    """
    if self.backend not in ACP_BACKENDS_MEMBER_PANEL:
        return servers
    # circular import: members' module graph is heavy; resolved at call time.
    from kiro_crew.members import (
        MEMBER_PANEL_SERVER,
        crew_panel_enabled,
        is_member_session_key,
        member_panel_session_server,
    )

    if not is_member_session_key(self._session_key):
        return servers
    if not crew_panel_enabled():
        logger.info(
            "member session %s: agent.crew_panel is off, so the crew panel is not "
            "mounted; the member keeps its other tools",
            self._session_key,
        )
        return servers
    if self._member_mount_withheld(MEMBER_PANEL_SERVER, "the crew panel", restricted, disabled):
        return servers
    entry = member_panel_session_server(self._session_key or "", self._stub_session_token)
    if entry is None:
        logger.warning(
            "member session %s: panel server unresolved -- the member runs without "
            "a panel this session",
            self._session_key,
        )
        return servers
    return [e for e in servers if e.get("name") != entry["name"]] + [entry]


def _withhold_is_the_only_deny_channel(self: AcpClient) -> bool:
    """Whether withholding a server is this backend's ONLY per-tool deny channel.

    Read from the declaration each mirror already publishes
    (``registry.PerToolDeny``) rather than from a second membership set, so a
    backend cannot answer one way here and another way in the projection that
    performs the withhold.

    Fail-CLOSED on a backend with no declaration at all: ``projection_for`` raises
    for one, and "no declared deny channel" is exactly the case where re-adding a
    withheld server cannot be shown to be safe. The cost of being wrong in this
    direction is a member thread that runs as plain chat.
    """
    from kiro_crew.providers.mirrors import PerToolDeny, projection_for

    try:
        return projection_for(self.backend).per_tool_deny is PerToolDeny.WHOLE_SERVER
    except Exception:
        logger.warning(
            "member session %s: backend %r declares no MCP projection, so its per-tool "
            "deny channel is unknown; treating a withheld server as un-re-addable",
            self._session_key,
            self.backend,
            exc_info=True,
        )
        return True


def _read_mcp_ref_spec(self: AcpClient) -> dict[str, Any] | None:
    """Snapshot this session's agent spec for the unresolved-ref guard.

    Blocking (reads the spec), so the spawn path runs it off the loop; the
    guard itself then only reads what this left behind.

    Best-effort by construction: every failure resolves to ``None``, which
    makes the detector silent rather than making the spawn fail. That is not
    politeness, it is the H13 constraint spelled out -- this runs on EVERY
    backend's construction path including kiro-cli's, and a diagnostic that
    can fail a session is a worse defect than the one it detects.
    """
    try:
        return agent_spec_snapshot(self._agent, work_dir=self._work_dir)
    except Exception:
        logger.debug("unresolved-ref guard: agent spec unreadable", exc_info=True)
        return None


def _guard_unresolved_mcp_refs(self: AcpClient, wire_servers: Any) -> None:
    """Warn when the spec's ``@server`` refs name nothing in Crew's projection.

    The one place the answer can be known: *wire_servers* is the FINAL array
    -- spec projection plus the broker stubs -- so this is the last point
    before ``session/new`` at which "the spec asked for it" and "Crew's
    projection carries it" can be compared at all.

    Called for every backend, not just the ones with a mirror. The defect it
    detects has landed on three harnesses already
    (``providers/mirrors/README.md``), so a check that only ran on the harness
    someone had already thought about would be the same omission a fourth
    time. A host that mounts the spec's own ``mcpServers`` off the wire
    (``ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE``: kiro-cli loads them via
    ``--agent``) is judged against the spec's definition instead of the array,
    which the detector resolves from the backend id.

    Synchronous, in-memory and non-raising, in that order of importance: the
    composition site is shared with kiro-cli, so this adds no scheduling point
    and no failure mode to that backend's path (harness-parity H13). It also
    changes NOTHING -- not the array, not the session's fate. A ref naming
    nothing is a configuration fact, and the complaint about this defect class
    was that it was invisible, not that it was tolerated.
    """
    spec = self._mcp_ref_spec
    if spec is None:
        # No snapshot: either the spawn path did not warm one (a client driven
        # straight into session/new by a test) or the spec was unreadable.
        # Reading it here would be the disk touch this site must not have.
        return
    # A server this attempt pruned as unstartable IS configured: the report
    # already names it as failed with its reason, and a second row calling it
    # unconfigured would send the user after the projection instead of the
    # command.
    pruned = getattr(self, "_mcp_pruned_names", ())
    if pruned and isinstance(wire_servers, (list, tuple)):
        wire_servers = [*wire_servers, *({"name": name} for name in pruned)]
    try:
        unresolved = warn_unresolved_server_refs(
            spec,
            wire_servers,
            backend=self.backend,
            agent=self._agent,
            gateway_enabled=self._mcp_gateway_overlay is not None,
        )
        if unresolved:
            self._mcp_report.record_unresolved_refs(unresolved)
    except Exception:
        logger.debug("unresolved-ref guard: evaluation failed", exc_info=True)
    try:
        header_refs, omitted = warn_remote_header_secret_refs(
            spec, wire_servers, backend=self.backend, agent=self._agent
        )
        if header_refs:
            self._mcp_report.record_header_secret_refs(header_refs, omitted)
    except Exception:
        logger.debug("remote-header guard: evaluation failed", exc_info=True)
    try:
        # The same spec names the servers an unattended run waits for, but only
        # on kiro-cli: it launches the spec's own servers (via --agent) and
        # reports each one's start. Elsewhere a spec server may be held back or
        # never report, and every run would wait out the bound for nothing.
        if self.backend == ACP_BACKEND_KIRO:
            self._mcp_report.record_declared(spec)
    except Exception:
        logger.debug("declared MCP servers: spec unreadable", exc_info=True)


def _session_mcp_servers(self: AcpClient) -> list[dict[str, Any]]:
    """MCP server array passed to this session's ``session/new`` / ``session/load``.

    Empty for kiro-cli, which receives the same servers through ``--agent``.
    For a harness in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` the array is the ONLY
    channel — the adapter reads no agent spec of CREW'S — so it is built by
    translating this session's agent spec (see
    :mod:`kiro_crew.acp.session_mcp`). "Of Crew's" is the load-bearing half:
    codex-acp does load a config file of its own, and being a member says only
    that this array is the sole channel from HERE to there.

    **In-memory, and deliberately so.** The translation reads disk, but it
    runs once per spawn in :meth:`_resolve_session_mcp_servers` and lands in
    ``_session_mcp_cache``; this accessor only hands the cached list out. That
    is what keeps the shared session-params call site synchronous: awaiting an
    executor hop there would put a new scheduling and failure point on EVERY
    backend's construction path — kiro-cli included, which returns ``[]``
    here and needs no I/O at all — and the kiro path is not allowed to change
    in service of an adapter (harness-parity H13).

    Asks the capability set rather than ``self._is_claude`` on purpose: where a
    harness gets its MCP servers is a property of its transport, not of its
    vendor, so the next such adapter joins the set instead of adding a second
    branch here (harness-parity H6). The set check comes FIRST, so a harness
    outside it returns before the cache is ever consulted and can never be
    made to read a spec.

    A cold cache resolves inline rather than returning nothing: a set member
    whose spawn path does not pre-warm would otherwise come up with zero
    tools, which is the exact defect this module exists to fix. That costs a
    brief blocking read on the loop, which is why the spawn path warms it.

    Per-spawn freshness is preserved: ``_reset_state`` clears the cache, so an
    MCP install or toggle takes effect on the next session with no gateway
    restart.
    """
    if self.backend not in ACP_BACKENDS_SESSION_MCP_ARRAY:
        return []
    if self._session_mcp_cache is None:
        self._session_mcp_cache = self._resolve_session_mcp_servers()
    return list(self._session_mcp_cache)


def _claude_session_mcp_servers(self: AcpClient) -> list:
    """MCP server array passed to a claude ``session/new`` / ``session/load``.

    Overridable seam, now FILLED for the public build. An empty array is
    byte-identical for kiro-cli (it gets its servers via ``--agent``)
    but a REAL GAP for claude: the claude-agent-acp adapter does not read
    ``kirocrew.mcp.json`` on its own, so a claude session had zero MCP tools.
    The harness itself worked — prompts, streaming, permissions — but Crew's own
    tools were absent, with no error anywhere.

    The translation lives in the mirror
    (:mod:`kiro_crew.providers.mirrors.claude_code`), not here: projecting the
    agent spec onto a backend's native shape is one named contract with one
    implementation per backend, rather than a per-harness override each backend
    author rediscovers. See ``docs/request-for-change/rfc-agent-config-mirror.md``.

    The seam is deliberately KEPT rather than replaced by a capability-set call:
    an edition already overrides this method, and swapping the call site for a
    set membership test would silently stop calling that override.

    In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
    this accessor adds no scheduling or failure point to a call site shared with
    kiro-cli (harness-parity H13).
    """
    return self._session_mcp_servers()


def _opencode_session_mcp_servers(self: AcpClient) -> list:
    """MCP server array passed to an opencode ``session/new`` / ``session/load``.

    The opencode twin of
    :meth:`kiro_crew.acp.harness.codex.CodexHarness.session_mcp_servers`, and it
    must stay non-empty for the same reason: ``opencode acp`` reads no
    ``~/.kiro/agents/<name>.json``, so nothing Crew declares reaches the session
    through any other door. Until this hook existed an opencode session held
    none of Crew's own tools at all -- no ``spawn_run``, no ``cron_add``, no
    ``send_message`` -- while working in every visible respect.

    What it does NOT do is the interesting half. There is no transport filter
    here, unlike codex: an ``http`` element and an ``sse`` element are both
    ACCEPTED by ``opencode acp``, so ``drop_unadvertised_transports`` would only
    remove servers the harness would have mounted. Measured, not assumed --
    ``test/test_opencode_session_mcp.py::test_real_opencode_acp_accepts_the_crew_stdio_element``
    drives a real ``opencode acp`` the way its codex sibling drives codex-acp,
    and pins the ``initialize`` ``mcpCapabilities`` shape so a release that
    starts refusing the stdio element goes red here rather than silently
    emptying every session's tool set.

    The failure mode a bad element causes is the OPPOSITE of codex's, which is
    why the shared translator's skip discipline matters more here, not less: a
    malformed element (no ``command``, or an ``env`` that is not an array) fails
    the WHOLE ``session/new`` with ``-32602`` on this harness, where codex drops
    the element and succeeds. ``acp.session_mcp.acp_server_element`` returns
    ``None`` for an entry with neither ``command`` nor ``url`` and stringifies
    what it cannot type, so one hand-edited spec line costs that server rather
    than the session.

    The translation lives in the mirror
    (:mod:`kiro_crew.providers.mirrors.opencode`), not here, for the same reason
    claude's and codex's do: projecting the agent spec onto a backend's native
    shape is one named contract with one implementation per backend.

    The seam is deliberately KEPT rather than replaced by a capability-set
    call: an edition may override this method, and swapping the call site for a
    set membership test would silently stop calling that override.

    In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
    this accessor adds no scheduling or failure point to a call site shared with
    kiro-cli (harness-parity H13).
    """
    return self._session_mcp_servers()


def _deepseek_session_mcp_servers(self: AcpClient) -> list:
    """MCP server array passed to a deepseek ``session/new`` / ``session/load``.

    The deepseek twin of :meth:`_goose_session_mcp_servers`: ``dsh --profile acp``
    reads no ``~/.kiro/agents/<name>.json``, and with deepseek in ``MIRRORS``
    :meth:`_pooled_mcp_servers` answers ``[]`` for it, so this array is the only
    route for Crew's own tools, the spec's servers and a member session's own
    mounts (session control and the panel). An element whose command cannot start
    fails ``session/new`` whole on this harness
    (``test/fixtures/acp_frames/deepseek/mcp-stdio-rollback-live.jsonl``).

    In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
    this accessor adds no scheduling or failure point to a call site shared with
    kiro-cli (harness-parity H13).
    """
    return self._session_mcp_servers()


def _pi_session_mcp_servers(self: AcpClient) -> list:
    """MCP server array passed to a pi ``session/new`` / ``session/load``.

    Only the member mounts: pi has no mirror, so its spec is not
    translated and Crew's pooled tools arrive through the shared broker append.
    pi-acp stores the wire array and never forwards it, so what MOUNTS these
    servers is the handoff file the spawn wrote from the same composition
    (:func:`kiro_crew.acp.harness.pi.pi_mcp_handoff_payload`). The wire copy is
    what the session report reads its roster from. In-memory only.
    """
    return self._session_mcp_servers()


async def _republish_pi_mcp_handoff(self: AcpClient) -> None:
    """Rewrite the MCP handoff file before a request that starts a pi process.

    pi-acp starts pi on ``session/load`` and on ``session/new``, and a load that
    fails falls through to a new session on the same adapter, so the file has to
    be present for each. The extension removes it after reading where its
    sandbox lets it. Off-loop: a file write.
    """
    from kiro_crew.acp.harness.pi import publish_pi_mcp_handoff

    path, payload = getattr(self, "_pi_mcp_handoff", ("", b""))
    if path and self._pi_gate_nonce:
        await asyncio.to_thread(publish_pi_mcp_handoff, self._pi_gate_nonce, payload)


async def _discard_pi_mcp_handoff(self: AcpClient) -> None:
    """Remove the MCP handoff file and forget it. Off-loop: an unlink."""
    from kiro_crew.acp.harness.pi import discard_pi_mcp_handoff

    path, _payload = getattr(self, "_pi_mcp_handoff", ("", b""))
    self._pi_mcp_handoff = ("", b"")
    if path:
        await asyncio.to_thread(discard_pi_mcp_handoff, path)


def _goose_session_mcp_servers(self: AcpClient) -> list:
    """MCP server array passed to a goose ``session/new`` / ``session/load``.

    The goose twin of :meth:`_opencode_session_mcp_servers`, and it must stay
    non-empty for the same reason: ``goose acp`` reads no
    ``~/.kiro/agents/<name>.json``, so nothing Crew declares reaches the session
    through any other door. Without this hook a goose session holds none of Crew's
    own tools at all -- no ``spawn_run``, no ``cron_add``, no ``send_message`` --
    while working in every visible respect. It also holds no pooled broker stubs,
    because :meth:`_pooled_mcp_servers` answers ``[]`` for any backend in
    ``MIRRORS`` and hands that half to the mirror instead.

    No transport filter, for opencode's reason rather than by copying it: this
    harness ACCEPTS the stdio element and mounts it, measured as a round trip
    against 1.50.1 -- the named child is asked ``initialize``,
    ``notifications/initialized``, ``tools/list`` and ``tools/call``, and the
    tool's own result comes back. So a filter would only remove servers the
    harness would have mounted.

    The failure mode a bad element causes is the LOOSEST of the three, and it is
    the one thing about this accessor a reader has to know: an element whose command
    cannot start does not fail ``session/new`` at all. The session is created with
    that element dropped, so a malformed spec line costs Crew's tools rather than the
    session.

    **The drop is unobservable on this harness, and the corpus pin is the accepted
    control.** goose emits no per-server MCP notification of any kind, so
    ``_mcp_timeout_progress`` -- which reads ``mcp_server_initialized`` -- has
    nothing to read here even as the timeout diagnostic it is; and the one
    enumeration ``session/new`` does carry, ``available_commands_update``, is
    byte-identical between a good mount and a dropped one. A session missing every
    Crew tool is therefore indistinguishable on the wire from a healthy one, and
    what stands in for a runtime check is
    ``test/fixtures/acp_frames/goose/mcp-stdio-mount-live.jsonl``: it pins the round
    trip -- ``initialize``, ``notifications/initialized``, ``tools/list`` and
    ``tools/call`` reaching the child, and its result coming back -- so a release
    that stops mounting the element goes red in the corpus rather than silently
    emptying every session's tool set.

    Accepted rather than closed, on two grounds. A dropped mount is a FUNCTIONALITY
    loss and not a gate bypass: permission routing on this harness rides on the
    seeded mode and its read-back, not on MCP, so every tool call a degraded session
    does make still reaches ``HookManager.on_tool_call``. And observing the connect
    is not goose-shaped work -- Crew hosts the broker stubs, so it can watch for the
    child's own ``initialize`` for every member of
    ``ACP_BACKENDS_SESSION_MCP_ARRAY``. That makes it a shared-path feature rather
    than one harness's onboarding cost, and it is named as a follow-up: the broker
    observes an ``initialize`` from the child within N seconds of ``session/new``,
    and otherwise logs a warning and marks the session degraded.

    The translation lives in the mirror
    (:mod:`kiro_crew.providers.mirrors.goose`), not here, for the same reason the
    three siblings' do: projecting the agent spec onto a backend's native shape is
    one named contract with one implementation per backend.

    The seam is deliberately KEPT rather than replaced by a capability-set call: an
    edition may override this method, and swapping the call site for a set
    membership test would silently stop calling that override.

    In-memory only. The spawn path warms ``_session_mcp_cache`` off the loop, so
    this accessor adds no scheduling or failure point to a call site shared with
    kiro-cli (harness-parity H13).

    The count is logged HERE, on the goose arm, because it is the one thing about a
    goose session's tool set that is observable at all: the harness reports nothing
    per server and nothing on a drop, so the number Crew handed over is the only
    greppable fact a degraded session leaves behind until the broker-side mount
    check exists. A count is not a mount -- it says what was offered, not what took.
    """
    if getattr(self, "_side_read_only", False):
        # A Side Chat session mounts no MCP server, as on claude: READ_ONLY
        # refuses every MCP call, and a server named after goose's builtin
        # extension would replace its read tools.
        return []
    servers = self._session_mcp_servers()
    logger.info(
        "goose session MCP: %d server(s) placed on the session array; this harness "
        "reports neither a mount nor a drop per server [session=%s]",
        len(servers),
        self._session_id,
    )
    return servers


def _invalidate_session_mcp_projection(self: AcpClient) -> None:
    """Drop the MCP array, the spec snapshot that authorized it, and the
    member-projection verdict recorded against it.

    NOT ``_session_agent_spec``: that names what the host's live session
    consumed, and the re-seed which calls this runs DURING the start whose
    confirmation reads it (see :meth:`confirm_member_projection`). It is dropped
    with the session, in ``_reset_state``.
    """
    self._session_mcp_cache = None
    self._session_mcp_snapshot = None
    self._loaded_capability_template = ""
    self._capability_projection_gaps = ()


async def _prepare_session_mcp(self: AcpClient) -> None:
    """Warm this session's ``mcpServers`` array off the loop, for its host's launch.

    A host whose array is the session's tool surface asks for this at its own
    point in the launch (:mod:`kiro_crew.acp.harness`). Correctness does not depend
    on it -- :meth:`_session_mcp_servers` resolves a cold cache itself -- the warm
    is what keeps the disk read off the loop.
    """
    self._session_mcp_cache = await asyncio.to_thread(self._resolve_session_mcp_servers)


def _mcp_timeout_progress(self: AcpClient, expected: object) -> str:
    """Summarize the MCP notifications buffered during a stalled session start."""

    def clean(value: object, cap: int = 64) -> str:
        text, _ = redact_exfiltration_urls(str(value or ""))
        text, _ = redact_credentials(text)
        return "".join(ch for ch in " ".join(text.split()) if ch.isprintable())[:cap]

    roster = [
        clean(item.get("name"))
        for item in (expected if isinstance(expected, list) else [])
        if isinstance(item, dict) and item.get("name")
    ]
    ready: set[str] = set()
    failed: dict[str, str] = {}
    auth: set[str] = set()
    # Ready / failed count only THIS attempt's frames: a timed-out
    # ``session/load`` leaves its ``server_initialized`` frames in the
    # buffer (``_drain_notifications`` clears it only at the end of
    # ``_initialize_session``), and the ``session/new`` fallback would
    # otherwise report them as its own progress -- and, with a complete
    # roster, assert the stall is not in those servers on stale evidence.
    # ``_begin_session_report`` moves the floor at the start of every
    # attempt. An OAuth request is NOT floored, the same rule
    # ``_drain_notifications`` applies to its capture: an authorization the
    # failed attempt asked for is still one the user has to answer.
    floor = self._mcp_report_frame_floor
    for idx, msg in enumerate(self._mcp_notifications):
        params = msg.params if isinstance(msg.params, dict) else {}
        name = clean(params.get("serverName") or params.get("name"))
        if not name:
            continue
        if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
            auth.add(name)
        elif idx < floor:
            continue
        elif msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
            ready.add(name)
        elif msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
            failed[name] = clean(params.get("error"), 120)

    def names(values: list[str]) -> str:
        head = values[:8]
        suffix = f" (+{len(values) - 8} more)" if len(values) > 8 else ""
        return ", ".join(head) + suffix

    reported = ready | set(failed)
    # "session-injected", and a note once the roster is complete: the same
    # wording as AcpRuntime._mcp_init_progress, for the same reason -- a bare
    # ``N/N MCP server(s) reported`` read as an MCP verdict it never was.
    parts: list[str] = []
    if roster:
        parts.append(
            f"{len(reported & set(roster))}/{len(roster)} session-injected "
            "MCP server(s) reported"
        )
        missing = [name for name in roster if name not in reported]
        if missing:
            parts.append(f"no report from {names(missing)}")
        elif not set(failed) & set(roster):
            # Same rule as the runtime: the verdict is withheld when a roster
            # member reported an init failure, which the ``failed:`` bucket
            # names.
            parts.append(MCP_ROSTER_COMPLETE_NOTE)
    else:
        # No roster to attribute against (an empty or absent ``mcpServers``):
        # the reports belong to the agent spec's own servers, so the count is
        # not labelled "session-injected" -- but it is still reported, the
        # same way as the runtime, instead of the timeout reading as a bare
        # budget.
        parts.append(f"{len(reported)} MCP server report(s), roster unknown")
    if failed:
        parts.append(
            "failed: "
            + names([f"{name} ({error})" if error else name for name, error in failed.items()])
        )
    if auth:
        parts.append(f"awaiting authorization: {names(sorted(auth))}")
    return "; ".join(parts)


def pop_pending_oauth_requests(self: AcpClient) -> list[dict[str, str]]:
    """Drain and return MCP OAuth requests captured during session init.

    Each entry has keys ``serverName`` and ``oauthUrl``. Callers (typically
    the dashboard chat runner) surface these to the UI as an Authorize
    prompt — kiro-cli's local callback handles the rest of the OAuth flow.
    """
    out = list(self._pending_oauth_requests)
    self._pending_oauth_requests.clear()
    return out


def _begin_session_report(self: AcpClient, servers: Any) -> None:
    """Start the MCP report for a new session attempt.

    A failed ``session/load`` still buffers the notifications it received
    before it raised, and the client then falls back to ``session/new``.
    Replaying those frames into the replacement session's report would
    attribute a server to a session that never came up — the same
    cross-attempt leak ``begin_session`` closes for the derived buckets, one
    layer earlier.

    The buffer itself is NOT cleared: it is shared with the OAuth and
    config-option captures, and an authorization request from the failed
    attempt is still one the user has to answer. Only the report's view is
    floored.
    """
    self._mcp_report_frame_floor = len(self._mcp_notifications)
    self._mcp_report.begin_session(servers)
    # What the session-open mount check compares against, once the session
    # exists (:meth:`_start_mount_check`), and when this attempt went out.
    self._mcp_mount_servers = servers if isinstance(servers, list) else []
    self._mcp_mount_opened_at = time.time()
    # AFTER the clear: a server the array build left out is this attempt's own
    # failure, and recording it first would erase it.
    pruned, self._mcp_pruned = getattr(self, "_mcp_pruned", ()), ()
    omitted, self._mcp_pruned_omitted = getattr(self, "_mcp_pruned_omitted", 0), 0
    # Only a server the report admitted counts as configured for the ref guard:
    # a name the failed bucket refused must get no row anywhere.
    admitted: list[str] = []
    for server in pruned:
        if self._mcp_report.record_pruned(server.name, server.reason):
            if server.name not in admitted:
                admitted.append(server.name)
    self._mcp_report.record_pruned_omitted(omitted)
    self._mcp_pruned_names = tuple(admitted)


async def _prune_unstartable_mcp(self: AcpClient, servers: list[Any]) -> list[Any]:
    """*servers* without the stdio elements whose command certainly cannot start.

    Only for ``ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP``, where such an element is
    otherwise lost without a word (goose) or takes the whole session with it
    (deepseek). Each call site checks that set BEFORE awaiting this, so the kiro
    path gains no awaited step (harness-parity H13). The stats run off the loop.
    Each pruned server is warned about here and recorded on the MCP report as
    failed by :meth:`_begin_session_report`, which every array build reaches
    before its request is sent.
    """
    kept, pruned = await asyncio.to_thread(prune_unstartable, servers)
    # Bounded where it is kept, by the report's own caps: the count by the
    # failed bucket's size, and each name and reason by its string cap. The
    # reason quotes the configured command, which can hold a whole CLI line
    # with a key in it, so it is cleaned before it is kept or logged.
    retained = tuple(
        PrunedServer(
            sanitize_sink_text(str(server.name or ""), MCP_REPORT_NAME_CAP),
            sanitize_sink_text(str(server.reason or ""), MCP_REPORT_ERROR_CAP),
        )
        for server in pruned[:MCP_REPORT_BUCKET_CAP]
    )
    omitted = len(pruned) - len(retained)
    for server in retained:
        logger.warning(
            "%s session MCP: leaving server %r out of this session -- %s. The "
            "session starts without it; fix its command to get its tools back",
            self.backend,
            server.name,
            server.reason,
        )
    if omitted:
        logger.warning(
            "%s session MCP: leaving %d more unstartable servers out of this "
            "session, not listed",
            self.backend,
            omitted,
        )
    self._mcp_pruned = retained
    self._mcp_pruned_omitted = omitted
    return kept


def _start_mount_check(self: AcpClient) -> None:
    """Start the session-open mount check for the array this session consumed.

    Only for ``ACP_BACKENDS_SESSION_MCP_ARRAY``, after the session exists, as a
    background task (:mod:`kiro_crew.acp.mcp_mount_check`): it never blocks or
    fails the start, and the kiro path gains no call (harness-parity H13).
    """
    if self.backend not in ACP_BACKENDS_SESSION_MCP_ARRAY or not self._session_id:
        return
    session_id = self._session_id
    schedule_mount_check(
        self._mcp_report,
        getattr(self, "_mcp_mount_servers", None) or [],
        socket_path=self._mcp_gateway_socket,
        backend=self.backend,
        session_id=self._session_id,
        session_key=self._session_key or "",
        opened_at=getattr(self, "_mcp_mount_opened_at", None),
        # A reset or a new session clears or replaces the id: nothing is owed then.
        alive=lambda: self._session_id == session_id,
    )


def mcp_session_report(self: AcpClient) -> McpSessionReport:
    """This session's MCP registration report (see ``mcp_session_report``).

    Unlike :meth:`pop_pending_oauth_requests` this does NOT drain: the report
    is the session's standing answer to "which servers actually started
    here", read repeatedly and still true. Callers must render an unreported
    server as *not reported*, never as *not mounted* — the drain is time
    bounded and a late frame still arrives mid-turn.
    """
    return self._mcp_report


async def wait_for_declared_mcp(self: AcpClient, timeout: float) -> tuple[str, ...]:
    """Hold until every server the agent spec declares has reported; never raises.

    The dedicated-transport twin of ``AcpSessionHandle.wait_for_declared_mcp``,
    for an unattended run before its first prompt: the init drain stops a second
    after it starts, so a slow server can miss it. Returns the declared servers
    that are not ready, in spec order; empty when all are.
    """
    if self._mcp_report.declared_unreported() and timeout > 0:
        try:
            await self._drain_notifications(duration=timeout, until_declared=True)
        except Exception:
            logger.debug("declared MCP wait failed", exc_info=True)
    return self._mcp_report.declared_not_ready()


def _placed_mcp_server_names(self: AcpClient) -> tuple[str, ...]:
    """The server names Crew placed on this session's ``mcpServers`` array.

    Empty when the session has none, including a client built without
    ``__init__``, which has no array cache to read.
    """
    if getattr(self, "_session_mcp_cache", None) is None:
        return ()
    return tuple(
        element["name"]
        for element in self._session_mcp_servers()
        if isinstance(element, dict) and isinstance(element.get("name"), str)
    )


def _harness_mcp_server_names(self: AcpClient) -> tuple[str, ...]:
    """Every MCP server name this session's harness may state a tool of.

    The servers Crew placed, then those the opencode read-back found in the
    harness's own config, then every server the pi MCP handoff carried -- on pi
    the pooled stubs are not in the placed array, and pi spells each of their
    tools after its name. An instance built without ``__init__`` has none.
    """
    found = getattr(self, "_opencode_config_mcp_servers", ())
    handed_off = getattr(self, "_pi_mcp_server_names", ())
    return tuple(dict.fromkeys([*self._placed_mcp_server_names(), *found, *handed_off]))
