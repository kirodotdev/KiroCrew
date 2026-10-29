"""What an enrolled crew member's session may load and mount on each harness.

A saved spec verified at spawn, its per-tool limits, the per-session dispatch tools
and the member's own webview: four capabilities, each opted into per harness on its
own evidence (harness-parity H6).

One of the owners of the harness vocabulary in :mod:`kiro_crew.agent_sdk.backends`,
which re-exports every name defined here and is the import path callers use. Like that
module it is a leaf: nothing here imports ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.platform`` at module scope. ``scripts/check_harness_parity.py`` lists this file
in ``VOCABULARY_PATHS``, which is what lets its ``ACP_BACKEND*`` names be defined here.
"""

from __future__ import annotations

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

# Backends that can verify an enrolled member's saved spec at spawn.
# Separate from session sharing and per-session dispatch (harness-parity H6):
# support for either does not establish saved-spec loading. kiro-cli reads the
# saved file at spawn (``--agent``). KAS is a member because the spec reaches it
# whole over the wire (``_meta.kiro.customAgents``, ``session/set_mode``). Claude
# and DeepSeek (one-process-per-session AcpClient path) and Codex (shared
# AcpRuntime path) confirm the consumed projection against the saved intent
# (``agent_capabilities_consumed.consumed_spec_matches``) after session creation.
# Each requires a live dedicated runtime before reporting a saved template.
# Claude's consumed projection is captured live on claude-agent-acp 0.94.0
# (``test/fixtures/acp_frames/claude/saved-spec-projection-live.jsonl``): the
# server the mirror projected out of a saved spec ran in the session. Codex's is
# captured live on codex-acp 2.2.2, the lane's pin
# (``test/fixtures/acp_frames/codex/saved-spec-projection-live.jsonl``): the
# element codex's mirror projected out of a saved spec, on a session armed
# read-only, answered the model's call with the server's own result after one
# permission request. DeepSeek rests on unit tests only.
# kiro-cli and KAS are captured live on kiro-cli 2.32.0 / KAS 0.66.43 with one probe
# spec whose only tool is an MCP server it declares:
# ``test/fixtures/acp_frames/kiro/saved-spec-live.jsonl`` (``--agent probe``) and
# ``test/fixtures/acp_frames/kas/saved-spec-live.jsonl`` (``customAgents`` plus
# ``session/set_mode``). kiro-cli reported ``probe`` as the current mode, KAS accepted
# ``set_mode probe``, and on both the model called the spec's server tool.
#
# pi is a member on the same confirmation, with no mirror behind it:
#
#   * the consumed spec is recorded -- pi has no mirror (``providers/mirrors/registry.py``
#     ``MIRRORS``), so ``AcpClient._resolve_session_mcp_servers`` takes the mirror-less
#     branch, and ``_mirrorless_member_mounts`` records ``_session_agent_spec`` from the
#     one ``session_mcp_projection`` parse its member mounts and zero-tool answer come
#     from. ``confirm_member_projection`` digests that parse against the saved intent,
#     so a spec edited after its save is refused with ``capability_runtime_unverified``;
#   * the projection is ``BROKER_ONLY``: of the servers a saved spec declares, only the
#     ones the shared gateway pools reach pi (all of them are absent with pooling off),
#     and on a member session those stubs pass the spec's ``tools`` allowlist from the
#     same parse (``_pooled_mcp_servers``), as a mirror's do, so a pooled server the
#     spec never references is not mounted beside it. The confirmation does not claim
#     the declared servers run. ``session_capabilities.runtime_view`` shows
#     the template applied only when every declared server is in the session's ready
#     report, so a declared server that did not reach pi leaves the pane unverified
#     (``capability_mcp_unreported``) instead of overstating what runs;
#   * pi is in ``ACP_BACKENDS_HONOR_ZERO_TOOL_BAN``, so a saved ``"tools": []`` refuses
#     pi's native tools too, and is not run wider than saved.
#
# ``test/fixtures/acp_frames/pi/saved-spec-projection-live.jsonl`` is a member session on
# pi-acp 0.0.34 over pi 1.3.0 whose saved spec the client confirmed after
# ``session/new``; the same drive with the spec edited after its save was refused.
ACP_BACKENDS_MEMBER_CAPABILITIES = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_PI,
    }
)

# Backends of the set above that also honour a saved member spec's per-tool
# ``disabledTools`` on an MCP server. kiro-cli reads the field off the saved file.
# The KAS customAgents wire accepts it and then discards it
# (``kas_agents._KAS_DISCARDED_ENTRY_KEYS``), so a server declared with one runs
# there with those tools on. A saved spec that uses it is refused before spawn on
# a non-member (``capability_tool_limits_unsupported``) rather than run wider than
# it was saved.
ACP_BACKENDS_MEMBER_TOOL_LIMITS = frozenset({ACP_BACKEND_KIRO})

# Backends that honour a crewmate WORKER's single-tool limits. The worker's spec
# is built per session (``mate_worker.worker_session_spec``), with every tool the
# crewmate switches off rewritten into ``excludedTools``. KAS takes that whole
# definition over the wire (``_meta.kiro.customAgents``) and projects
# ``excludedTools``, so the limits hold. claude mounts the worker's servers
# whole and reports ``excludedTools`` as a projection gap, so a worker with
# single-tool limits is refused there before
# spawn (``capability_tool_limits_unsupported``); without limits it runs.
# KAS is captured live on KAS 0.66.58 (kiro-cli 2.33.1):
# ``test/fixtures/acp_frames/kas/worker-kit-live.jsonl``. A crewmate file's
# ``excludedTools [shell]``, carried in the worker's customAgents entry, withheld
# ``shell``: the model listed only the ``read`` tools.
ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS = frozenset({ACP_BACKEND_KAS})

# Backends that carry a crewmate WORKER's per-session spec (its kit): a spec built
# per session from the worker base plus that crewmate's own file
# (``mate_worker.worker_session_spec``), never one read off disk by name. KAS takes
# it over the wire (``_meta.kiro.customAgents``); claude builds its ``session/new``
# MCP array from ``session_mcp_projection``, which reads it by the session key.
# codex and deepseek build theirs the same way but have no live capture of it, so
# they are not here yet. kiro-cli is a member chat backend but is not here: it reads
# ``--agent`` off disk at spawn from ``~/.kiro/agents/``, where the worker spec is not,
# and has no per-session channel. A worker allocated anywhere else is refused before
# spawn (``capability_harness_unsupported``).
# KAS is captured live on KAS 0.66.58 (kiro-cli 2.33.1):
# ``test/fixtures/acp_frames/kas/worker-kit-live.jsonl``. A worker base with
# tools ``read`` and ``shell``, merged with a crewmate file that adds a prompt and
# excludes ``shell``, ran with only the ``read`` tools and followed the added prompt.
# claude is captured live on claude-agent-acp 0.94.0:
# ``test/fixtures/acp_frames/claude/saved-spec-projection-live.jsonl``. The server a
# spec declares, projected into the ``session/new`` array, ran in the session; the
# worker's spec reaches claude through that same array.
ACP_BACKENDS_MATE_WORKER_KIT = frozenset({ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE})

# Backends of the set above whose worker gets its prompt only from the
# ``[AGENT SYSTEM PROMPT]`` context block: their mirrors project no spec prompt
# (``Concern.PROMPT`` is ``WITHHELD``), so the block carries the worker's own
# principles and its crewmate's prompt (``context_assembly.agent_spec
# ._crewmate_worker_prompt``). KAS is not here: its wire spec already carries both,
# so its block stays the base.
ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT = frozenset({ACP_BACKEND_CLAUDE})

# Backends that can mount a DIFFERENT MCP tool set on one session than the
# on-disk agent template declares — the capability crew-member dispatch rides
# on. claude-agent-acp takes the whole server list as a per-session
# ``session/new`` ``mcpServers`` array; the KAS engine takes the full agent
# definition over the wire (``_meta.kiro.customAgents``). kiro-cli v2 reads
# the template from disk at spawn and exposes no wire channel, so a member
# session on it stays a plain chat: the dispatch tools are simply not
# mounted, never mounted-and-refused.
#
# codex-acp is a member, and both things membership requires hold:
#
#   * the per-session mount exists -- ``providers/mirrors/codex.py`` projects the
#     whole array onto ``session/new`` (codex is in
#     ``ACP_BACKENDS_SESSION_MCP_ARRAY``), so the dispatch element rides the same
#     channel the session's own servers do;
#   * the session is GATED -- codex's routing is ``SESSION_CONFIG``, one of the
#     three mechanisms in ``tool_gate.ENFORCED_ROUTINGS``, so a session that cannot
#     arm ``mode=read-only`` is REFUSED before its first prompt. That is
#     structurally stronger than claude's ``settings.local.json`` ownership check,
#     which covers a routing this core declares and does not enforce.
#
# Captured live on codex-acp 2.2.2, the lane's pin:
# ``test/fixtures/acp_frames/codex/member-dispatch-live.jsonl``. With the member
# entry as the session's one ``session/new`` element and ``mode`` set to
# ``read-only``, the model's ``session_status`` call raised one permission request
# and then reached the server, which answered it (it refuses, as the probe key
# names no live session).
#
# Membership is a DECISION on top of those two rather than a consequence of them:
# mounting session control into a codex DM thread is a capability separate from
# giving a codex session the tools its own agent spec declares. The decision is
# that a member DM thread on codex holds the session-control tools.
#
# Membership un-withholds nothing, because the entry is Crew's OWN.
# ``mirrors.identity.identity_bound_crew_servers`` keeps the dashboard server out of
# the projection, and that withhold judges SPEC-DESCRIBED elements: one the agent
# file names carries no session identity and answers ``identity_unattested`` to
# every call. The dispatch entry comes from
# ``members.member_dispatch_session_server`` carrying this session's key and its
# signed stub token, the same way the projection rebuilds the control plane.
#
# opencode is a member, and it holds the same two things:
#
#   * the per-session mount exists -- opencode is in
#     ``ACP_BACKENDS_SESSION_MCP_ARRAY`` and ``providers/mirrors/opencode.py``
#     projects the array onto ``session/new``, so the dispatch element rides the
#     channel the session's own servers ride. The harness reads no agent file of
#     Crew's, so that array is the ONLY channel any tool set reaches it on;
#   * the session is GATED -- its routing is ``VERIFIED_SEEDED_SETTINGS``, one of
#     the three mechanisms in ``tool_gate.ENFORCED_ROUTINGS``. The value is seeded
#     on ``OPENCODE_CONFIG_CONTENT`` and READ BACK from the harness's own config
#     resolution before the first prompt, so a session that cannot establish the
#     asking posture is REFUSED rather than run. The read-back is what makes this
#     routing VERIFIED rather than merely seeded, and it is the whole of the
#     difference from claude's.
#
# H6 is explicit that supporting one harness establishes nothing about another, so
# the decision above is codex's alone and this membership carries its own: a member
# DM thread on opencode holds the session-control tools.
#
# Captured through ``AcpClient`` on opencode 1.19.1, the lane's pin:
# ``test/fixtures/acp_frames/opencode/member-dispatch-live.jsonl``. On a member key
# the client appended the entry, the model's ``session_status`` call raised a
# permission request, and once approved the server answered it.
#
# The mount asks for no owned permission file here, and must not: the client's
# fallback answers for an UNENFORCED routing alone (``tool_gate.is_enforced`` is true
# for this one), and ``providers/mirrors/opencode.py`` documents
# ``permission_surface_owned`` as accepted-and-ignored for that same reason -- the
# flag stands in for a read-back this harness performs, and no opencode session owns
# a ``settings.local.json`` to satisfy it with.
#
# Membership un-withholds nothing, for the reason it un-withholds nothing on codex:
# ``mirrors.identity.identity_bound_crew_servers`` keeps the dashboard server out of
# the SPEC projection, because an element the agent file names carries no session
# identity, while the entry mounted here is Crew's own and carries this session's key
# and its signed stub token.
#
# One restriction the mount must NOT step over: switching off a tool of the dashboard
# server. This harness carries it as a ``deny`` rule in the permission config Crew
# seeds (``registry.PerToolDeny.SETTINGS_FILE``), so the mount keeps its server and the
# harness hides the tool. Where that rule did not come out in force -- a lower config
# source outranked it -- the projection names the server in ``unhonoured_servers`` and
# ``AcpClient._member_mount_withheld`` withholds the mount, so that thread runs as plain
# chat rather than reaching a tool the operator switched off.
#
# Switching that server off WHOLE (``disabled``) is a stronger rule and carries no
# backend condition, because the form has no per-call spelling for any harness to
# refuse by (``acp.session_mcp.session_mcp_disabled_servers``). It binds on BOTH paths
# that compose a session's array -- ``AcpClient``'s, which opencode and claude take,
# and ``AcpRuntime``'s create and resume paths, which codex and KAS take -- so the
# operator's switch-off reaches a member session whichever one runs.
#
# goose is a member, and it holds the same two things opencode does:
#
#   * the per-session mount exists, on a RECORDED ROUND TRIP rather than on an
#     advertisement -- ``test/fixtures/acp_frames/goose/mcp-stdio-mount-live.jsonl``
#     (measured on goose 1.50.1) carries one ``session/new`` element shaped as
#     ``acp.session_mcp`` emits, and the named child is asked ``initialize``,
#     ``notifications/initialized``, ``tools/list`` and ``tools/call`` with the tool's
#     own result coming back. So the element is MOUNTED and its tools are REACHABLE,
#     not merely accepted. The harness reads no agent file of Crew's, so that array is
#     the ONLY channel any tool set reaches it on;
#   * the session is GATED -- its routing is ``VERIFIED_SEEDED_SETTINGS``, one of the
#     three mechanisms in ``tool_gate.ENFORCED_ROUTINGS``. The asking mode is seeded on
#     the environment and READ BACK off the ``session/new`` response before the first
#     prompt, so a session that cannot establish the asking posture is REFUSED rather
#     than run.
#
# H6 again: opencode's membership establishes nothing here, so both facts are held for
# THIS harness. The mount asks for no owned permission file, and must not: the client's
# fallback answers for an UNENFORCED routing alone (``tool_gate.is_enforced`` is true for
# this one), and ``providers/mirrors/goose.py`` documents ``permission_surface_owned`` as
# accepted-and-ignored for that same reason -- the flag stands in for a read-back this
# harness performs, and no goose session owns a ``settings.local.json`` to satisfy it
# with.
#
# One thing is goose's ALONE, and it is why this membership carries a test of its own:
# goose is the only member of ``ACP_BACKENDS_META_IDENTITY``, so it is the only member
# whose every tool approval runs ``AcpClient._refuse_identity_drift``. That refusal
# judges a call's trusted server name against the names Crew PLACED on this session's
# array plus the harness's own builtin extension -- and the dashboard server is neither
# a builtin nor named by the agent template. It passes because both halves read the
# SAME array: the mount is appended inside ``_resolve_session_mcp_servers``, whose
# result is the cache ``_foreign_mcp_identity`` enumerates, so a server this session
# mounted is a placed server by construction. The identity goose reports for it is the
# element's own name (``_meta.goose.toolCall.extensionName``, the field the mount
# fixture pins for ``crew-probe``), so a dispatch call arrives as a placed server
# rather than as a drifted one.
#
# The per-tool rule binds here: this harness's declared ``registry.PerToolDeny`` is
# ``WHOLE_SERVER``, so narrowing a dashboard tool withholds the whole mount and the
# thread runs as plain chat.
#
# pi is a member, on the same two facts held for THIS harness:
#
#   * the per-session mount exists, on a RECORDED ROUND TRIP --
#     ``test/fixtures/acp_frames/pi/mcp-stdio-mount-live.jsonl``: one stdio element
#     the client composed reaches pi through the gate extension's handoff file (the
#     adapter drops the wire array), pi's built-in MCP support registers it, and the
#     named child is asked ``initialize``, ``tools/list`` and ``tools/call`` with the
#     tool's own result coming back. The member mount is one more element in that
#     same composition: ``_mirrorless_member_mounts`` builds it, and
#     ``acp.harness.pi.pi_mcp_handoff_payload`` carries it, env included;
#   * the session is GATED -- its routing is ``VERIFIED_GATE_EXTENSION``. Crew's
#     extension raises a confirm dialog for every tool call, MCP calls included, and
#     the harness's own command registry is READ BACK before the first prompt, so a
#     session that cannot establish it is REFUSED rather than run.
#
# Like deepseek it has no mirror, so a narrowed dashboard tool withholds the whole
# mount and the thread runs as plain chat. And a same-named server in pi's own
# ``mcp.json`` outranks the registration, so the gate extension refuses that name's
# tools rather than let another command answer for session control.
# deepseek is a member, and it holds the same two things goose does:
#
#   * the per-session mount exists, on a RECORDED ROUND TRIP --
#     ``test/fixtures/acp_frames/deepseek/mcp-stdio-mount-live.jsonl`` carries one
#     ``session/new`` element shaped as a broker stub, and the named child is asked
#     ``initialize``, ``tools/list`` and ``tools/call`` with the tool's own result
#     coming back. The harness reads no agent file of Crew's, so that array is the
#     ONLY channel any tool set reaches it on;
#   * the session is GATED -- its routing is ``VERIFIED_GATE_EXTENSION``, one of the
#     mechanisms in ``tool_gate.ENFORCED_ROUTINGS``. Crew's plugin answers every tool
#     call, MCP calls included, with ``ask``, and its load marker is READ BACK before
#     the first prompt, so a session that cannot establish it is REFUSED rather than
#     run.
#
# H6 again: goose's membership establishes nothing here, so both facts are held for
# THIS harness. The decision is that a member DM thread on deepseek holds the
# session-control tools.
#
# deepseek's array is built by its mirror (``providers/mirrors/deepseek.py``),
# which places the spec's servers and the pooled broker stubs, and the member
# mounts are appended to it as on every client-composed mirrored backend. No
# per-tool deny path is verified for dsh, so the mirror withholds a narrowed
# server whole (``PerToolDeny.WHOLE_SERVER``): a narrowed dashboard tool withholds
# the whole mount and the thread runs as plain chat. ``disabled`` binds as it does
# on every backend. And an element whose command cannot start fails the WHOLE
# session here (``mcp-stdio-rollback-live.jsonl``), so an entry that does not
# resolve is never placed (``member_dispatch_session_server`` answers ``None``).
#
# WHERE the mount happens differs by backend, and opencode's is the client's.
# ``AcpClient._append_member_dispatch_server`` serves the backends whose array the
# CLIENT composes -- claude's, opencode's, goose's, deepseek's and pi's -- while codex and
# KAS are served by
# ``AcpRuntime``: their arrays come from ``AcpRuntime._mirrored_session_mcp`` and
# ``create_session`` / ``load_session`` append the entry themselves, on a non-empty
# ``member_session_key``. That key is where membership is read
# (``providers/acp_runtime_start.py`` ``_member_session_key``), which is why adding a backend here
# is the whole of the change for a runtime-served harness. Both establishment paths
# carry it, because ``session/load`` re-initializes a session's MCP servers and a
# resume that skipped the append would strip a member thread of its tools
# mid-conversation.
#
# The client path's own PRECONDITION is read from the routing rather than from one
# harness's flag: ``tool_gate.is_enforced``. A harness whose routing this core
# enforces needs nothing further -- an ungated session never reaches a prompt. A
# harness whose routing is declared-but-unenforced (claude) additionally needs Crew
# to OWN the session's native permission file, because a tool pre-approved in a file
# Crew does not own never sends ``session/request_permission`` and Crew's gate never
# fires. Reading the flag for a harness whose mirror documents it as
# accepted-and-ignored would withhold every member's tools on a condition that
# cannot describe that backend.
#
# KAS is captured live on KAS 0.66.43: ``test/fixtures/acp_frames/kas/member-dispatch-live.jsonl``.
# With the member entry on ``session/new`` and the ``member_dispatch=True`` projection, the
# model called ``@kirocrew-dashboard/session_status`` with no permission request, and the
# server answered it (``identity_unattested``, as the probe key names no live session).
ACP_BACKENDS_MEMBER_DISPATCH = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_PI,
    }
)

# Backends a crew member's OWN WEBVIEW may be mounted into (``kirocrew-panel``:
# ``panel_publish``, ``panel_templates``).
#
# Its own set, and its own argument, because H6 requires membership to be opted
# into PER CAPABILITY: supporting session control establishes nothing about the
# panel, so reusing ``ACP_BACKENDS_MEMBER_DISPATCH`` would grant a distinct
# capability on evidence gathered for a different one. The two memberships happen
# to coincide today; what must not coincide is the DECISION, and a backend added
# to one set does not join the other.
#
# The panel asks a SMALLER question than session control does, which is why every
# dispatch-capable harness also clears this one. Session control needs a
# server-side ownership fence (``created_by``) to bound which OTHER sessions a
# member may drive; the panel reaches no other session at all. ``panel_publish``
# takes no crew or session argument, resolves the publishing crew strictly from
# the calling session, and refuses a subagent, so the worst an auto-approved call
# can do is rewrite the caller's own drawer. What the panel still needs is exactly
# what makes an approval-free grant safe to project at all, and it is the same
# two preconditions the dispatch set is argued on:
#
# * a session-level ``mcpServers`` array this core composes, so the element can
#   carry the session identity the panel server's tool-policy read demands --
#   ``ACP_BACKENDS_SESSION_MCP_ARRAY`` for the client-composed harnesses, and
#   ``AcpRuntime``'s own array for codex and KAS; and
# * a permission surface Crew either gates (``tool_gate.is_enforced``) or owns
#   (claude's ``settings.local.json``), so a verb Crew did not grant is not
#   silently pre-approved in a file Crew does not write.
#
# deepseek is a member on the two preconditions above, held for THIS harness: its
# array is one this core composes (its mirror, in ``AcpClient``), and its
# routing (``Routing.VERIFIED_GATE_EXTENSION``) is enforced. The decision is that a
# member session on deepseek gets a panel.
#
# pi is a member on the same two preconditions, held for THIS harness and on its own
# evidence (H6), not on its session-control membership:
#
# * the array is one this core composes: pi has no mirror, so
#   ``AcpClient._mirrorless_member_mounts`` builds it, and the panel entry rides the
#   gate extension's handoff file (``acp.harness.pi.pi_mcp_handoff_payload``) with its
#   identity env intact, because pi-acp drops the wire array;
# * its routing (``Routing.VERIFIED_GATE_EXTENSION``) is enforced: every call, MCP
#   included, raises the extension's confirm dialog, and the read-back refuses a
#   session that cannot load it.
#
# The mount is driven, not inferred:
# ``test/fixtures/acp_frames/pi/member-panel-mount-live.jsonl`` is a member DM session
# key on pi-acp 0.0.34 over pi 1.3.0, in which the client's own resolver composed the
# panel element, pi registered ``kirocrew-panel`` from the handoff and listed its
# tools, the model's ``panel_publish`` call asked ``session/request_permission``
# through the gate first, and the panel server's own answer reached the turn: the
# identity the handoff carried reached the gateway, which refused to vouch for a
# session it had not created (that host can run no gateway pi session, having no
# sandbox backend). Like deepseek, a narrowed panel tool withholds the whole mount (no
# mirror, so no per-tool deny channel), and ``agent.crew_panel: false`` withholds it
# outright.
#
# kiro is excluded and cannot be added by this set alone: a member session's key is
# what every mount reads, and ``providers/acp_runtime_start.py`` ``_member_session_key`` gates
# that key on ``ACP_BACKENDS_MEMBER_DISPATCH``. So this set decides whether a
# member session that EXISTS gets a panel; whether one exists at all is that
# gate's decision, and a backend must clear both.
ACP_BACKENDS_MEMBER_PANEL = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_PI,
    }
)
