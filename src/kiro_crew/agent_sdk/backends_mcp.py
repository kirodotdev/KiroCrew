"""Where a harness gets its MCP servers and agent specs, and how its tool surface is
shaped.

Whether the server list travels as a per-session array or in an agent file, which
spec forms and levels a host reads, how the broker-overlay lookup is scoped, and the
tool-surface memberships beside them: a total ``tools: []`` ban, pruning an
unstartable stdio element, the Tool Search settings channels and config hot reload.

One of the owners of the harness vocabulary in :mod:`kiro_crew.agent_sdk.backends`,
which re-exports every name defined here and is the import path callers use. Like that
module it is a leaf: nothing here imports ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.platform`` at module scope. ``scripts/check_harness_parity.py`` lists this file
in ``VOCABULARY_PATHS``, which is what lets its ``ACP_BACKEND*`` names be defined here.
"""

from __future__ import annotations

from typing import Any, FrozenSet

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)

# ── Capability: where a harness gets its MCP servers ──

#: Harnesses that receive their MCP servers as a PER-SESSION array on
#: ``session/new`` / ``session/load`` instead of reading an agent file.
#:
#: kiro-cli (and KAS, which is kiro-cli's relay) is handed ``--agent`` and loads
#: the spec itself, so Crew passes it an empty array — a duplicate there would
#: shadow the spec's own entries. claude-agent-acp reads no agent file at all, so
#: the array is the only channel CREW has onto the session's MCP surface (the harness
#: mounts what its own user- and project-scope ``mcpServers`` and plugins declare
#: beside it): an empty one means the harness works while every Crew tool is
#: silently absent. Unlike goose, deepseek and pi, claude's membership has no
#: recorded round trip: the one claude capture
#: (``test/fixtures/acp_frames/claude/session.jsonl``) is a single turn with no
#: Crew MCP element on it.
#:
#: codex-acp is the second member, and it joins on the same terms rather than on
#: an exact likeness to claude: it does load a config file of its OWN
#: (``~/.codex/config.toml``, which Crew never writes — create-or-decline), and its
#: ``build_session_config`` merges the client's array on top of what that file
#: declared. What makes it a member is the part that matters here: it reads no
#: ``~/.kiro/agents/<name>.json``, so this array is the only channel CREW has, and
#: an empty one means Crew's own control plane never reaches the session.
#:
#: A membership set rather than ``_is_claude`` because this is a property of the
#: transport, not of Anthropic: any ACP adapter that does not read Crew's agent
#: spec belongs here, and the next such harness should join the set rather than
#: add a second branch at the call site (harness-parity H6).
#
# opencode is the third member, and it is here because the reason it was EXCLUDED
# was wrong rather than because anything about the harness changed. That reason read
# its ``initialize`` result -- ``mcpCapabilities: {"http": true, "sse": true}`` --
# as an advertisement carrying "no stdio", and concluded the array could not mount
# the stdio servers Crew puts in it. ACP's ``McpCapabilities`` schema has exactly
# two boolean fields, ``http`` and ``sse``, and NO stdio field, so a conforming
# agent cannot advertise stdio at all and that answer is what full support looks
# like. Absence of a flag that cannot exist is not evidence. Driven against
# opencode 1.18.30, the element ``acp.session_mcp.acp_server_element`` already
# emits is accepted, the named child is spawned, its tools are listed and the
# element's ``env`` reaches it -- so the excluded harness had in fact been serving
# sessions with none of Crew's own tools for no reason at all. The exclusion also
# contradicted the shipped code it sat beside: the shared gateway's broker stubs
# are stdio elements too (``mcp_gateway.session_servers._acp_server_entry``) and
# ``_pooled_mcp_servers`` appended them to this very array for opencode whenever
# pooling was on. See ``providers/mirrors/opencode.py``.
# Pi is a member, and it is the one member whose array is NOT what mounts the
# servers. ``pi-acp`` accepts the array on ``session/new``, stores it on its session
# state, and never hands it to the ``pi`` process (its own documentation lists MCP
# under its limitations, and an array-only drive spawns no server). What mounts
# them is Crew's gate extension: the launch writes the SAME composition to an
# owner-only handoff file bound to the session nonce and sealed with a key only that
# spawn's environment carries (``acp.harness.pi.pi_mcp_handoff_payload``), and the extension registers each
# entry with pi's built-in MCP support (``pi.registerMcpServer``; the release that
# carries it is ``PI_MIN_VERSION``). The array still travels on the wire because
# the session report reads its roster from it. Captured as a ROUND TRIP against
# pi-acp 0.0.34 over pi 1.2.0: ``test/fixtures/acp_frames/pi/mcp-stdio-mount-live.jsonl``
# -- the server is asked ``initialize``, ``tools/list`` and ``tools/call``, the call
# raises ``session/request_permission`` through the gate first, and the tool's own
# result reaches the turn. A same-named server in pi's own ``mcp.json`` (user-level,
# or a trusted project's) outranks an extension's registration, so the extension
# refuses that name's tools.
# goose is the fourth member and it joins on a ROUND TRIP rather than on an accepted
# element, which is the distinction pi's exclusion exists to draw. Driven against
# goose 1.50.1: one element shaped as ``acp.session_mcp.acp_server_element`` emits is
# placed on ``session/new``, and goose asks the named stdio child ``initialize``,
# ``notifications/initialized``, ``tools/list`` and ``tools/call``, with the tool's
# own result arriving back on ``tool_call_update``. Its ``initialize`` advertises
# ``mcpCapabilities: {"http": true, "sse": false}`` and no stdio flag, which is the
# same absence-of-an-impossible-field non-evidence recorded for opencode above.
# goose surfaces such a tool as ``<serverName>__<toolName>`` -- a double underscore
# and no ``mcp__`` prefix -- and carries ``toolName`` and ``extensionName``
# separately in ``_meta.goose.toolCall``.
#
# One hazard rides along, and it is the INVERSE of a session that fails whole: an
# element whose command cannot start does not fail ``session/new`` on goose. The
# session is created normally with that element dropped, so an unstartable pooled
# broker stub costs no session here but leaves a healthy-looking one carrying none of
# Crew's tools.
#
# deepseek IS a member, on a captured ROUND TRIP rather than on the advertisement or
# on a mount that was merely attempted:
# ``test/fixtures/acp_frames/deepseek/mcp-stdio-mount-live.jsonl``. Its ``initialize``
# result carries ``mcpCapabilities: {"http": true}``, which names only the transports
# ACP v1 treats as OPTIONAL -- stdio is the baseline every v1 agent may serve, so an
# absent stdio flag is not a refusal. In that capture ``session/new`` is sent one
# element shaped as ``mcp_gateway.session_servers._acp_server_entry`` emits a pooled
# broker stub, pointing at a real stdio MCP server; it returns a sessionId, the server
# is asked ``initialize``, ``tools/list`` AND ``tools/call``, and the turn carries a
# ``tool_call`` titled ``mcp__<serverName>__<toolName>`` with the server's own result.
# The premise is load-bearing, which is why it is captured and not declared: if stdio
# were refused, ``session/new`` would fail WHOLE rather than degrade, and the harness
# would be broken rather than tool-less -- see
# ``mcp-stdio-rollback-live.jsonl`` for what an unstartable element actually does.
# It reads no ``~/.kiro/agents/<name>.json``, so this array is the only channel Crew
# has to it.
ACP_BACKENDS_SESSION_MCP_ARRAY: FrozenSet[str] = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_PI,
    }
)

# Backends that load an agent defined as ONE markdown file (YAML frontmatter
# plus the body as the system prompt) -- the form the v3 engine and Kiro IDE
# read from ``~/.kiro/agents/<name>.md``. Kiro Crew's own discovery lists that
# form for every backend (``kiro_crew.agent_spec_format``), so a session can
# select an agent its HOST cannot load: kiro-cli discovers ``*.json`` only, so a
# markdown-only agent selected there is not the active mode after
# ``session/new`` and fails the runtime's existing activation guard, exactly as
# a missing JSON spec does. Nothing is refused BEFORE the spawn on account of
# this set (harness-parity H13); membership decides only how that guard
# explains the failure -- the markdown file and the members that can run it,
# rather than a JSON repair. Read through the harness
# (``reads_markdown_agent_specs``), never as "is KAS": a host added later that
# reads markdown joins here and gets no markdown explanation. KAS is
# a member because Crew reads the spec itself and hands it over the wire, so the
# on-disk form is Crew's to parse; codex-acp, opencode, pi, claude-agent-acp,
# goose and deepseek are not members because none of them reads
# ``~/.kiro/agents`` at all (``ACP_BACKENDS_SESSION_MCP_ARRAY``).
ACP_BACKENDS_MARKDOWN_AGENT_SPECS = frozenset({ACP_BACKEND_KAS})

# Backends whose agent's MCP SERVERS come from the USER-LEVEL spec alone, so a
# checkout's same-named spec never puts a server of its own in their session.
#
# Every other host takes the nearer layer's servers too: kiro-cli reads
# ``<project>/.kiro/agents/`` itself for ``--agent``, and a MIRRORED host receives
# the array ``acp/session_mcp.py`` translates, which is project-nearest-first. KAS
# resolves the agent nearest-first as well (``acp.harness.kas.resolve_projected_spec``),
# but a checkout's spec there never supplies a server: a project-only agent gets
# its prompt and visible tools and no servers, and a name the user level declares
# keeps the user-level spec, because a server is a command and a cloned repository
# is untrusted input.
#
# What membership decides is the SCOPE of the broker-overlay lookup
# (``mcp_gateway.session_servers``). That overlay is keyed by agent name and is
# also written from the user-level directory, so for every OTHER host a
# checkout-declared name means the overlay holds no stubs for this session. For a
# member the reverse holds: the user-level servers ARE the ones running, so scoping
# its lookup would suppress the stubs for servers the session really has and run
# them outside the pool, outside caller-identity attribution and outside broker
# governance. Read through the runtime's own scope helper, never as "is KAS": a
# host added later whose servers come from the user level alone joins here.
ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY = frozenset({ACP_BACKEND_KAS})


def overlay_project_scope(backend: str, work_dir: Any) -> dict[str, Any]:
    """The overlay-lookup scope keywords for *backend*'s session.

    The ONE decider, so every call site that resolves the overlay -- ``AcpClient``
    and ``AcpRuntime`` alike -- answers from
    :data:`ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY` rather than from its own
    spelling. A host reading the user level alone joins the set and both paths
    change together; the alternative, one path keyed on the set and the other
    hardcoding the session's checkout, silently mis-scopes the next such host,
    which is the defect this function exists to make unreachable.

    Returned as keywords to SPLAT, because the checkout is not the whole scope:
    which spec FORMATS this session's agent resolution SEES is the other half. A
    project spec shadows the user-level overlay only in a form that resolution
    honours, and the two halves must name the same file.

    ``markdown_specs`` and ``dispatchable_only`` are two facets of ONE question --
    which resolver decides this session's agent spec -- so they are computed here
    together rather than derived from each other at a call site.

    A MIRRORED host's array is composed by Crew from a spec ``acp.session_mcp``
    resolves through ``_project_spec_path_for``: it scans both forms and matches on
    ``agent_discovery.project_agent_name``, which falls back to the filename stem.
    So the overlay lookup must match the same way -- both forms, no parse
    requirement. That is not a formality: when that resolver matches a project file
    it returns that file's read and does NOT fall back to the user level, so a
    MALFORMED project spec leaves the projection with no spec at all, no ``tools``
    allowlist and no project servers. Keeping the user-level stubs on top of that
    would put servers in the session that nothing in force declares.

    kiro-cli instead resolves ``--agent`` from the checkout ITSELF, discovers the
    JSON form there (measured on 2.22.0, and pinned by the
    ``KIROCREW_E2E_REAL_KIRO_CLI``-gated test), and reports a malformed spec as an
    error offering no such mode -- so it runs the user-level agent, whose stubs must
    therefore be kept. Hence JSON only, and a parse required.

    :data:`ACP_BACKENDS_MARKDOWN_AGENT_SPECS` -- a host reading the markdown form
    from a checkout itself -- is deliberately NOT OR-ed in: its only member also
    takes its servers from the user level alone and leaves above, so the term would have no caller
    able to reach it. ``test_agent_sdk_capabilities`` pins that containment, so a
    host which breaks it fails there naming this function.

    The import is deferred because the mirror registry imports THIS module at its
    own top level. It is not wrapped: an import that does not resolve is a
    packaging fault, and answering "kiro-shaped" for a mirrored host because of one
    would silently reinstate the stub-shadowing this scope exists to prevent.

    ``work_dir`` is passed through untouched (``str``, ``Path`` or ``None``) so
    the caller keeps whichever form it already holds.
    """
    if backend in ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY:
        return {}
    from kiro_crew.providers.mirrors.registry import has_mirror

    mirrored = has_mirror(backend)
    return {
        "work_dir": work_dir,
        "markdown_specs": mirrored,
        "dispatchable_only": not mirrored,
    }


# Backends whose agent spec's own ``mcpServers`` reach the session OFF the wire --
# by a channel other than the ``session/new`` ``mcpServers`` array.
#
# The unresolved-``@server``-ref detector (``agent_sdk.mcp_refs``) judges a spec's
# ``tools`` refs against the servers Crew's projection delivers, and for an
# array-backed host that is the wire array: what is not in it, Crew did not mount
# (the harness may still mount a same-named server from a configuration of its
# own, which the detector never reads -- so its line says "may", never "absent").
# These two hosts mount the spec's servers by another channel, so for them the
# spec's own server names are satisfied by construction and only a ref naming a
# server the spec does NOT declare is unresolved. kiro-cli resolves ``--agent``
# itself and loads the spec's servers from disk, which is why Crew hands it an
# empty array. KAS receives the spec's servers as a projected agent definition in
# ``_meta.kiro.customAgents`` (``acp/kas_agents.py``), with only the broker stubs
# on the array. codex is NOT a member: the array Crew sends is the whole of what
# it mounts, so judging its refs against that array is exactly right. Read by
# membership rather than as "is kiro": a host added later that mounts a spec's
# servers by its own channel joins here, and the detector says nothing wrong about
# it on day one.
ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends on which an agent spec's ``"tools": []`` is honoured as a total ban --
# no MCP server AND no harness-native tool (Bash, file edit, ...) is callable --
# rather than merely an MCP-server allowlist that leaves native tools reachable.
#
# Membership is the join of three facts, not a name pattern:
#
# * ``Routing.AGENT_SPEC`` (kiro, kas) -- the harness itself reads the spec and
#   refuses every tool NATIVELY, so nothing else is needed.
# * a routing in ``tool_gate.ENFORCED_ROUTINGS`` whose non-ROUTED verdict actually
#   refuses a session, AND a mirror in ``providers/mirrors/registry.py``'s
#   ``MIRRORS`` that reports ``SessionProjection.zero_tools`` from the spec parse
#   (``acp/session_mcp.py``), AND the session is served by ``AcpClient`` rather than
#   ``AcpRuntime`` -- because the refusal that closes the gap,
#   ``AcpClient._deny_zero_tools``, lives ONLY on ``AcpClient``.
#
# Today that is opencode and goose (``Routing.VERIFIED_SEEDED_SETTINGS``) and
# deepseek (``Routing.VERIFIED_GATE_EXTENSION``): mirrored, not in
# ``ACP_BACKENDS_ACP_RUNTIME``. deepseek's mirror reads ``zero_tools`` from
# the shared session MCP projection.
#
# claude is NOT a member: its routing, ``Routing.SEEDED_SETTINGS``, is declared but
# not enforced by this core (see ``Routing``'s docstring and
# ``tool_gate.ENFORCED_ROUTINGS``) -- an operator's own ``~/.claude`` settings can
# pre-approve a tool, which skips ``session/request_permission`` entirely, so
# ``_deny_zero_tools`` never runs to close the mirror's allowlist-only translation.
#
# codex is NOT a member: its sessions are served by ``AcpRuntime`` /
# ``AcpSessionHandle`` (``acp/runtime.py``, ``acp/session_handle.py``), which carry
# ``spec_denied_tools`` but no ``zero_tools`` and no ``_deny_zero_tools`` equivalent
# -- the refusal exists only on ``AcpClient``, so nothing refuses on a codex session
# even though its ``Routing.SESSION_CONFIG`` is itself enforced.
#
# pi is NOT a member: it has no mirror (``providers/mirrors/registry.py``'s
# ``MIRRORS``), so nothing computes ``zero_tools`` for it and ``AcpClient`` never
# refuses on its sessions.
ACP_BACKENDS_HONOR_ZERO_TOOL_BAN = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_KAS,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    }
)

# Backends whose ``session/new`` array Crew prunes of stdio elements whose command
# certainly cannot be found (``acp.mcp_startable``), recording each one as a failed
# server on the session's MCP report. Membership is a MEASURED bad outcome without it:
# goose drops such an element silently and starts a session missing that server's
# tools (``test/fixtures/acp_frames/goose/mcp-stdio-dropped-live.jsonl``), and
# deepseek fails the whole ``session/new`` over it
# (``test/fixtures/acp_frames/deepseek/mcp-stdio-rollback-live.jsonl``). Both run on
# ``AcpClient``, which is where the prune is applied.
ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP = frozenset({ACP_BACKEND_GOOSE, ACP_BACKEND_DEEPSEEK})

# Backends that read the MCP Tool Search setting from the workspace ``cli.json``
# overlay (``toolSearch.*`` keys). Only kiro-cli's Rust engine does. KAS shares
# the ``ACP_BACKENDS_KIRO_SLASH_COMMANDS`` dialect but never opens that file: on the relay path
# nothing forwards it, so a Tool Search value written there for a KAS session is
# dead -- the setting looks on in the dashboard while the engine runs with it off.
# KAS takes the setting from the handshake instead (the set below).
ACP_BACKENDS_TOOL_SEARCH_OVERLAY = frozenset({ACP_BACKEND_KIRO})

# Backends that take feature settings from the ACP ``initialize`` request, under
# ``clientCapabilities._meta.kiro.settings``. KAS is the only member: it opened
# that channel (``KAS_CLIENT_CAPABILITIES``), and the runtime fills it at spawn
# with the settings the harness declares it reads -- today Tool Search, gated on
# the spawn agent's spec granting the ``tool_search`` loader, because KAS defers
# every MCP spec when told to and does not check that a loader exists. kiro-cli
# is not a member: it has no such channel and reads the overlay file instead.
ACP_BACKENDS_CLIENT_META_SETTINGS = frozenset({ACP_BACKEND_KAS})

# Backends that reconcile an edited agent config into their RUNNING sessions: a
# file watcher on ``~/.kiro/agents`` and ``mcp.json`` restarts only the changed
# MCP servers, keeps the conversation, and applies the edit at the next turn
# boundary. Membership is what lets the dashboard's MCP writers SKIP the session
# reset they otherwise perform after a config change — so a wrong member here
# leaves a user's freshly installed server unmounted until they restart by hand,
# with nothing red to tell them why. :mod:`kiro_crew.mcp_hot_reload` owns the
# gate and additionally pins a version floor: the capability belongs to a
# kiro-cli release, not to the harness name alone.
#
# KAS is NOT a member: its MCP servers are broker stubs injected on
# ``session/new`` (:mod:`kiro_crew.acp.kas_agents`), so nothing on disk
# describes its running set for a watcher to reconcile against. claude-agent-acp
# reads no agent file at all (``ACP_BACKENDS_SESSION_MCP_ARRAY``), and codex-acp
# has not demonstrated the capability — neither inherits it.
# opencode is not a member either, and its reason is unchanged by the mirror: its
# MCP set now comes from the ``session/new`` array, resolved per spawn, so there is
# still no file on disk a watcher could reconcile a RUNNING session against. A
# change takes effect on the next session, as it does for every array backend.
# pi is not a member: same reason, and its session has no Crew MCP set at all.
# deepseek is not a member: its MCP servers arrive as a ``session/new`` array, so its
# running set is described by the request that made the session and by no file.
# goose is not a member, on the same reason: its Crew MCP set is the ``session/new``
# array, so no file on disk describes a running session's set.
ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD = frozenset({ACP_BACKEND_KIRO})
