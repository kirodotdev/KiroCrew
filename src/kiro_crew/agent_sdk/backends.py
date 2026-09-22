"""Which ACP backends this build can serve — the one place that decides.

The question this module owns is **capability**: can this build drive the harness
at all? The public baseline registers kiro-cli, Claude Code, KAS and Codex; an
edition plugin adds its own from ``ProviderRegistry.register_acp_backends`` by calling
:func:`register_selectable_backend`, the structural twin of
``publish_provider.register_provider``.

A LEAF module on purpose. ``kiro_crew/acp/__init__.py`` imports the ACP client and
runtime, so reaching ``kiro_crew.acp.types`` executes that package init and lands
back in ``config.loader`` — a cycle ``_normalize_acp_backend`` can only escape by
deferring the import. Under that cycle the selectable list can live in one place
only if that place imports nothing: the loader's ``acp_backend`` field metadata,
the dashboard's PATCH allowlist and ``acp.types`` cannot import each other, so each
would otherwise carry its own literal with a drift test standing in for a code
owner. Nothing here imports ``kiro_crew.acp``,
``kiro_crew.config`` or ``kiro_crew.platform``, so all three now derive from this
module — and a plugin-registered backend reaches the dashboard without a core
edit, which a literal could never do.

Whether a registered backend may be selected on a *given deployment* is a separate
question (an enterprise policy bounding the fleet to one harness). It is
deliberately NOT answered here: it needs a governance ceiling, resolving a ceiling
reaches ``current_context()``, and that call's lazy branch loads config — so asking
it from :func:`resolve_selected_backend`, which runs inside
``KiroCrewConfig.load()``, re-enters that load and recurses. Keeping this module
capability-only is what makes the load path safe.

Where this module lives, and why it moved
-----------------------------------------
This file WAS ``kiro_crew/acp_backends.py``. RFC PR 3 offered two ways to stop
application code from asking a backend's IDENTITY: move the tables in here, or
leave them where they were and put a query layer in front. Option 1 — the move —
is what landed, because a table left outside the boundary keeps its old import
path reachable, and a reachable old path is the one a new consumer finds. The
top-level module survives as a pure re-export shim so no existing call site had
to change in the same commit as the move.

``kiro_crew.acp_backends`` still imports, still exports the same names, and still
mutates the SAME registry state: the shim re-exports the functions defined here
rather than copying them, so ``register_selectable_backend`` and
``apply_selectable_denials`` reach one ``_baseline``/``_selectable`` pair however
they were imported.

The leaf property is preserved and it is load-bearing. Nothing on the import
chain this module now sits behind (``agent_sdk/__init__`` ->
``backend_install`` + ``native_commands`` -> ``agent_sdk.drivers.acp``) imports
``kiro_crew.config``, ``kiro_crew.platform`` or ``kiro_crew.acp`` at module
scope — the driver defers every ACP import into a function body — so
``config.loader`` can still reach the registry from inside
``KiroCrewConfig.load()`` without re-entering it.

Capability-set dispositions
---------------------------
Every ``ACP_BACKENDS_*`` name below is one of three things, and saying which is
what keeps the next reader from exposing a driver-internal membership as a
consumer-facing question. ``test_agent_sdk_capabilities`` fails if a set exists
with no row here.

* **semantic question** — a consumer outside the boundary asks it, so
  :class:`kiro_crew.agent_sdk.capabilities.SessionCapabilities` carries a field
  for it and the consumer reads that field, never the set.
* **pre-session registry query** — asked ABOUT a backend id before any session
  exists (config load, the dashboard's option list, an install probe), so a
  session-scoped capability object is the wrong shape for it.
* **driver-internal** — read only inside ``kiro_crew.acp`` while it drives the
  harness. It has no consumer above the boundary and must not grow one.

.. list-table::
   :header-rows: 1

   * - set
     - disposition
   * - ``ACP_BACKENDS_KNOWN``
     - pre-session registry query (membership gate on the ``acp_backend`` kwarg)
   * - ``ACP_BACKENDS_SELF_SERVED_ACP``
     - driver-internal (whether this harness's whole launch is a value
       :data:`ACP_BACKEND_LAUNCH` already holds, so the spawn path, the install
       probe and the driver seams resolve it from that row)
   * - ``ACP_BACKENDS_SESSION_MCP_ARRAY``
     - driver-internal (which channel carries the MCP server list)
   * - ``ACP_BACKENDS_META_IDENTITY``
     - driver-internal (which harnesses publish a ``_meta`` tool identity, and so
       accept being refused when a frame classifies as nothing)
   * - ``ACP_BACKENDS_SESSION_SHARING``
     - pre-session registry query (subagent session allocation)
   * - ``ACP_BACKENDS_MEMBER_CAPABILITIES``
     - pre-session registry query (whether enrolled members can load a full saved spec)
   * - ``ACP_BACKENDS_MEMBER_DISPATCH``
     - driver-internal (whether a per-session tool set can be mounted)
   * - ``ACP_BACKENDS_STEER``
     - pre-session registry query (whether ``_session/steer`` exists)
   * - ``ACP_BACKENDS_COMPACT``
     - pre-session registry query (whether manual ``/compact`` is offered at all)
   * - ``ACP_BACKENDS_INLINE_COMPACTION``
     - semantic question (``SessionCapabilities.compacts_inline``)
   * - ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION``
     - semantic question (whether a skipped autocompact is answered by the harness)
   * - ``ACP_BACKENDS_CONTEXT_RECYCLE``
     - semantic question (whether a full context is answered by recycling)
   * - ``ACP_BACKENDS_INTERNAL_SANDBOX``
     - driver-internal (whether Crew's seatbelt is skipped at spawn)
   * - ``ACP_BACKENDS_POD_HOME_REMAP``
     - driver-internal (whether ``$HOME`` is relocated onto the pod tree)
   * - ``ACP_BACKENDS_ACP_RUNTIME``
     - pre-session registry query (which start path a session takes)
   * - ``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``
     - driver-internal (whether the host loads the markdown agent form; nothing
       is gated before the spawn -- the activation guard that runs after
       ``session/new`` reads it through the harness, only on its refusal
       branch, to explain a markdown-only agent the host did not load)
   * - ``ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE``
     - driver-internal (whether the agent spec's own ``mcpServers`` reach the
       session by a channel other than the ``session/new`` array, so the
       unresolved-``@server``-ref detector counts them as satisfied instead of
       judging the spec against an array that was never meant to carry them)
   * - ``ACP_BACKENDS_SESSION_EVICTION``
     - pre-session registry query (whether this harness's teardown verb disposes
       one session, which is what decides if a path that creates and destroys
       sessions on a shared process -- the high-churn background handles, warm
       pooled reuse, the entitlement probe -- may run on it). Separate from the
       row above because multiplexing and eviction are separate claims: a harness
       can serve N sessions on one process and still have no verb that frees one
   * - ``host_auth.backends_retired_by_host_logout()``
     - pre-session registry query (whether a kiro-cli logout retires the child).
       Declared per harness in :mod:`kiro_crew.agent_sdk.host_auth`, not here, and a
       function rather than a set because it is derived rather than vocabulary
   * - ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION``
     - driver-internal (which wire request switches the model)
   * - ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION``
     - semantic question (``SessionCapabilities.effort_via_config_option``)
   * - ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS``
     - driver-internal (whether an advertised ``<model>[<effort>]`` id is applied
       as two config-option writes)
   * - ``effort_config_option_id``
     - driver-internal (which ``configId`` carries the reasoning effort)
   * - ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``
     - semantic question (``SessionCapabilities.resolves_model_from_advertised_list``)
   * - ``ACP_BACKENDS_SEED_LOCAL_SETTINGS``
     - driver-internal (whether ``settings.local.json`` is re-seeded on switch)
   * - ``ACP_BACKENDS_KIRO_SLASH_COMMANDS``
     - driver-internal (whether ``_kiro.dev/commands/execute`` exists)
   * - ``ACP_BACKENDS_TOOL_SEARCH_OVERLAY``
     - driver-internal (whether the workspace ``cli.json`` Tool Search keys are written)
   * - ``ACP_BACKENDS_CLIENT_META_SETTINGS``
     - driver-internal (whether ``initialize`` carries ``_meta.kiro.settings``)
   * - ``ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD``
     - pre-session registry query (whether the dashboard may skip a session reset)
   * - ``ACP_BACKENDS_STRUCTURED_REFUSAL``
     - driver-internal (whether the metadata refusal parser is consulted)
   * - ``ACP_BACKENDS_HOST_AUTH_CALLBACK``
     - driver-internal (whether the reader loop may answer the engine's
       ``_kiro/auth/getAccessToken`` from Crew's own vault)
   * - ``ACP_BACKENDS_SIDE_READONLY``
     - pre-session registry query (whether a side-chat turn may execute
       read-only tools under the derived ``<agent>--readonly`` spec; asked
       about the configured backend id before the side session is created)
   * - ``ACP_BACKENDS_HARNESS_OWNED_SESSIONS``
     - driver-internal (whether ``session/load`` is gated on a Crew-side transcript)
   * - ``ACP_BACKENDS_LOAD_WITHOUT_MODES``
     - driver-internal (whether a successful session-restore result carries no
       ``modes`` block)
   * - ``ACP_BACKENDS_RESUME_WITHOUT_LOAD``
     - driver-internal (which ACP verb restores a session, and which capability
       key advertises it)
   * - ``ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY``
     - driver-internal (whether the session's project checkout scopes the broker
       overlay lookup, read only through :func:`overlay_project_scope` while
       ``kiro_crew.acp`` composes the ``session/new`` MCP array). Deliberately not
       a semantic question: it describes where a HOST reads agent specs from, and
       no consumer above the boundary asks it -- what a consumer would ask about
       is the resulting server list, which it already receives

The two non-set tables ``SessionCapabilities`` also translates are
:func:`model_registry_namespace` (the model-id namespace) and
:func:`kiro_crew.agent_sdk.backend_identity.is_claude_backend_name` (the provider
seam). Both already existed; neither gained a member here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, FrozenSet, Mapping, Set

logger = logging.getLogger(__name__)

# ── Backend identifiers ──
# ``acp.types`` re-exports these, so every existing call site keeps importing
# them from there; this module is only where they are DEFINED.

ACP_BACKEND_CLAUDE = "claude"
ACP_BACKEND_KAS = "kas"
# The Codex ACP adapter: a Node stdio server that boots the Codex app server and
# translates ACP onto its operations. Selectable on a plain build, with an install
# probe in ``agent_sdk/backend_install.py`` behind the switch.
ACP_BACKEND_CODEX = "codex"
# OpenCode: a single binary that serves ACP itself (``opencode acp``). No npm
# adapter and no Node floor, because the harness's own published package ships the
# executable -- which is why its install probe names one component and its
# ``install_command`` is the harness's own installer rather than an ``npm i -g``.
ACP_BACKEND_OPENCODE = "opencode"
# Pi: the ``pi`` coding agent reached through a third-party npm adapter, ``pi-acp``.
# TWO components, and the split is load-bearing for the install probe: the adapter
# is the ACP server and the agent is what it spawns (``pi --mode rpc``), and either
# can be absent on its own. There is no ``pi acp`` subcommand. Pi runs no
# permission gate of its own, so Crew loads one INTO it -- see
# :data:`Routing.VERIFIED_GATE_EXTENSION`.
ACP_BACKEND_PI = "pi"
# goose: a single binary that serves ACP itself (``goose acp``). No npm adapter and
# no Node floor, so its install probe names ONE component -- the shape opencode has
# and the opposite of pi's two. What distinguishes it is where its permission route
# comes from: goose resolves ``GOOSE_MODE`` out of its own ENVIRONMENT and above its
# config file, so the mode is settled in the ``session/new`` result rather than
# applied to a session that already exists -- see
# :data:`Routing.VERIFIED_SEEDED_SETTINGS`.
#
# VERIFIED RANGE: goose 1.50.x (1.50.1 is the recorded binary). Three things Crew holds
# this harness to are wire FACTS of that release rather than spec guarantees, and they do
# not all fail the same way, so each is named with its direction:
#
# * ``modes.currentModeId`` on every ``session/new`` and ``session/load`` -- fails CLOSED:
#   a session with no readable mode is refused.
# * the tool identity in ``_meta.goose.toolCall`` on every ``tool_call`` frame -- fails
#   CLOSED: an unclassifiable approval is refused, on every session and on both sites
#   that answer a permission request, whether or not the session carries a deny set.
# * a ``current_mode_update`` on the session's own connection whenever the mode MOVES --
#   fails OPEN: the mid-session tripwire fires on that frame, so a release that stops
#   emitting it leaves a session that left the required mode running unrefused until
#   the next restore. This is the one fact of the three whose loss is not self-announcing.
#
# So a release that moves either of the first two turns into refused sessions or refused
# approvals; a release that drops the third silently narrows the tripwire back to the
# open/restore read-back. The corpus pins all three
# (``test/fixtures/acp_frames/goose/``; the mode-move emission is pinned by the tripwire
# test reading a live ``current_mode_update`` naming ``auto`` off
# ``session-load-live.jsonl``). Re-verify all three against a fresh capture before raising
# this range, and re-capture the mode move deliberately -- it is the one a routine
# re-record would not exercise. A session on a release outside the range is named at the
# handshake (``AcpClient._note_goose_version`` reads ``agentInfo.version`` off
# ``initialize``), so the one silent loss has a signal before the first prompt.
ACP_BACKEND_GOOSE = "goose"
# DeepSeek Harness: a plugin host whose ``acp`` profile serves ACP v1 over stdio
# (``dsh --profile acp``). The profile is shipped by the harness and initialized on
# first use, and its two bundles sit in the installed package's own dependency
# closure -- so one global install is the whole precondition, with no workspace
# checkout and no per-profile dependency step.
ACP_BACKEND_DEEPSEEK = "deepseek"
# The kiro-cli backend is spelled as the empty string throughout, so name it
# rather than leaving every call site to infer it from "not claude".
ACP_BACKEND_KIRO = ""

# Membership gate for the ``acp_backend`` kwarg. An unrecognized value would
# otherwise fall through every ``_is_<backend>`` check and silently spawn
# kiro-cli, so provider construction rejects it instead.
ACP_BACKENDS_KNOWN: FrozenSet[str] = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    }
)

# ── Capability: where a harness gets its MCP servers ──

#: Harnesses that receive their MCP servers as a PER-SESSION array on
#: ``session/new`` / ``session/load`` instead of reading an agent file.
#:
#: kiro-cli (and KAS, which is kiro-cli's relay) is handed ``--agent`` and loads
#: the spec itself, so Crew passes it an empty array — a duplicate there would
#: shadow the spec's own entries. claude-agent-acp reads no agent file at all, so
#: the array is the ENTIRE MCP surface of the session: an empty one means the
#: harness works while every Crew tool is silently absent.
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
# Pi is NOT a member, for a reason that is worse than absence and is exactly why
# membership is evidence-based: ``pi-acp`` ACCEPTS the array on ``session/new``
# without error, stores it on its session state, and never hands it to the ``pi``
# process -- driven against pi-acp 0.0.33, a stdio server placed in the array
# produced no error and no tool, and the adapter's own documentation lists MCP
# under its limitations. A server in the array is therefore accepted and inert.
# Membership here would make the dashboard report Crew tools as mounted on a
# session where none can be called, which is the one state an absent tool never
# produces. See ``providers/mirrors/registry.py`` for the projection that says so.
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
# Harnesses opted into the fail-closed approval posture: a ``tool_call`` frame carrying
# NEITHER an ACP ``kind`` nor the harness's own ``_meta`` identity channel classifies as
# nothing, and on an auto-approve path with a deny set configured its approval is refused
# rather than answered blind.
#
# Membership is the SCOPE of that refusal, and the scope is the point. What proves a
# harness classifies EVERY tool class is a recording of every tool class -- and a corpus
# of a few frames shows a channel exists, never that no class omits it. So membership is
# held to a measurement, per backend: the coverage pin in ``test_acp_goose_backend.py``
# asserts every committed ``tool_call`` frame in the member's corpus is classifiable, and
# the member's corpus has to have been recorded broadly enough for that to mean anything.
#
# goose is in because its corpus is the case that NEEDS the refusal -- its builtin shell
# frames carry no ``kind`` at all, captured live -- and every recorded frame carries its
# channel. kiro-cli is deliberately absent, for the same reason ``kas`` is: both publish
# ``_meta.kiro``, but the recorded kiro corpus holds two tool_call frames of two classes,
# which is not a measurement of every class, and no kiro defect (an approval answered
# blind) has been observed that the refusal would close. Neither loses anything by being
# out: a harness outside the set keeps its behaviour exactly. Adding one is one entry here
# plus one on its channel row, and the pin then checks it against its own recordings --
# which for kiro means first recording a frame per builtin tool class.
#
# The channel TABLE lives in ``kiro_crew.acp._dispatch`` (it holds wire field names, which
# are driver detail); this set is the vocabulary half, and a test asserts the two agree so
# a row and its membership cannot drift apart.
ACP_BACKENDS_META_IDENTITY: FrozenSet[str] = frozenset({ACP_BACKEND_GOOSE})

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
    }
)

# ── The selectable registry ──

#: What the public edition ships.
#:
#: ``ACP_BACKEND_CLAUDE`` is included because the public build can genuinely serve a
#: session with it: ``acp/client.py`` owns the whole spawn path (the ``_is_claude``
#: branch, ``_resolve_claude_acp_bin``, ``_resolve_claude_code_executable``) and the
#: adapter it needs is a PUBLIC npm package (``CLAUDE_ACP_NPM_PKG``). Nothing about it
#: is edition-private. An earlier revision left it out and described it as a "dormant
#: seam ... not something a public build can serve a session with", which made the
#: option render as permanently unavailable on exactly the builds that could run it —
#: the switch was the only missing piece, not the harness.
#:
#: Whether it is USABLE on a given machine is a separate question with its own answer:
#: :mod:`kiro_crew.agent_sdk.backend_install` probes for the two binaries and the
#: dashboard reports what is absent plus the command that installs it.
#:
#: ``ACP_BACKEND_CODEX`` is included, and the two things that were missing when it
#: was not are both worth naming, because each was a separate reason:
#:
#: * ``backend_install`` now has a probe, so the install row reads ``missing`` with
#:   the component and the command rather than ``unknown``. A switch that cannot say
#:   what is absent when a session fails is a switch offered ahead of the code that
#:   answers for it.
#: * its tool calls are ROUTED. ``acp_tool_gate`` verifies ``session/new``
#:   advertised ``mode=read-only`` and applies it before the first prompt, refusing
#:   the session otherwise, so the PreToolUse gate is armed for the calls it makes.
#:
#: One gap REMAINS and is survivable rather than closed: ACP v1 offers no way to
#: make an adapter ask for a passive READ, so the sensitive-path block cannot see
#: reads this harness performs. What made that dangerous was the credential homes
#: the standard sandbox tier leaves open, and those are denied to its child at the
#: OS boundary by ``acp_tool_gate.adapter_hidden_credential_dirs`` -- derived from
#: the read-gate floor itself, so the compensating control covers exactly what the
#: control it compensates for covers, minus the harness's own token store.
#: ``ACP_BACKEND_OPENCODE`` is included on the same two conditions Codex had to
#: meet, and it meets them by a different mechanism:
#:
#: * ``backend_install`` probes for the ``opencode`` binary, so the install row
#:   names the component and the command that installs it rather than reading
#:   ``unknown``.
#: * its tool calls are ROUTED, and the routing is VERIFIED rather than declared.
#:   OpenCode asks per tool call only while its own ``permission`` setting is
#:   ``ask``; its default is permissive. So the client supplies ``permission: "ask"``
#:   as inline config in the child's environment -- which this harness resolves
#:   above its own project config file, so nothing is written into a checked-out
#:   repository -- then READS THE HARNESS'S OWN RESOLVED CONFIGURATION BACK and
#:   refuses the session when the required value is not in force. See
#:   :data:`Routing.VERIFIED_SEEDED_SETTINGS`.
#:
#: ``ACP_BACKEND_PI`` is included on the same two conditions, met by a third
#: mechanism, because this harness has NO permission setting to seed: ``pi`` runs
#: every tool call without asking, by design, and ``pi-acp`` sends
#: ``session/request_permission`` only when an EXTENSION inside ``pi`` raises a
#: confirm dialog. So:
#:
#: * ``backend_install`` probes for BOTH components -- the adapter and the agent --
#:   and names whichever is absent, since either can be missing on its own.
#: * its tool calls are ROUTED because Crew loads its own gate extension into the
#:   ``pi`` process at spawn, which raises that dialog for every tool call, and the
#:   precondition is VERIFIED before the first prompt by asking the harness's own
#:   command registry whether the extension loaded from the shipped file. See
#:   :data:`Routing.VERIFIED_GATE_EXTENSION`.
#:
#: ``ACP_BACKEND_GOOSE`` is included on the same two conditions, met by the FIRST
#: mechanism, and it is the only member whose routing needs nothing applied after
#: the session exists:
#:
#: * ``backend_install`` probes for the ``goose`` binary, one component, because the
#:   harness's own release ships the executable that serves ACP.
#: * its tool calls are ROUTED and the routing is VERIFIED. goose asks per tool call
#:   only in its ``approve`` mode and its own default is ``auto``, which
#:   auto-approves; the client supplies ``GOOSE_MODE=approve`` in the child's
#:   environment, which goose resolves ABOVE its own config file, and then reads the
#:   mode goose reports back in ``modes.currentModeId`` on the very response that
#:   creates or restores the session. The read-back is on the SAME connection as the
#:   session -- no second child, and no window in which the session is live and the
#:   mode is unconfirmed. See :data:`Routing.VERIFIED_SEEDED_SETTINGS`.
#: ``ACP_BACKEND_DEEPSEEK`` is NOT included, and it fails the SECOND condition
#: rather than the first: it has an install probe, and its tool calls are not routed.
#: Its sandbox decides them itself -- an in-policy action runs silently, an
#: out-of-policy one is denied with the denial in the tool result -- and
#: ``session/request_permission`` carries only a model-initiated request to escalate
#: past that sandbox. So Crew's PreToolUse gate, and with it the bundled
#: denied-command rules, the sensitive-path block and the governance ceiling, would
#: run for almost nothing a session actually does. Offering the switch would be
#: offering a harness Crew cannot gate; ``test_agent_backend_editable`` names it in
#: ``NOT_SHIPPED_SELECTABLE`` with that reason.
BASELINE_SELECTABLE_BACKENDS: FrozenSet[str] = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
    }
)

# ── Policy-facing spelling ──
# A governance rule is written by a human into ``security_policy.json`` and is
# matched as an identifier, so the kiro backend cannot be spelled the way the code
# spells it: ``ACP_BACKEND_KIRO`` is the empty string, and an empty allow/deny
# entry is indistinguishable from a typo'd blank that a JSON linter would keep.
# ``"kiro"`` is therefore the WIRE name, translated here rather than at each
# reader, so the policy vocabulary has one owner.

POLICY_ID_KIRO = "kiro"

POLICY_ID_BY_BACKEND: dict = {
    ACP_BACKEND_KIRO: POLICY_ID_KIRO,
    ACP_BACKEND_KAS: ACP_BACKEND_KAS,
    ACP_BACKEND_CLAUDE: ACP_BACKEND_CLAUDE,
    # Every known id needs an entry: a policy author has to be able to name — and so
    # to deny — any id this build can spell, and the mapping is what makes the id
    # nameable in a rule at all.
    ACP_BACKEND_CODEX: ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE: ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI: ACP_BACKEND_PI,
    ACP_BACKEND_GOOSE: ACP_BACKEND_GOOSE,
    ACP_BACKEND_DEEPSEEK: ACP_BACKEND_DEEPSEEK,
}

#: The backend a deployment policy may never deny.
#:
#: A governance scope that can empty the selectable set is a scope that can brick
#: the install — there would be no harness left to start a session with, and the
#: operator's remedy (edit the trust-root policy) is the one file the dashboard
#: cannot reach. So the scope is additive over a floor: it can WIDEN the set past
#: what this deployment would otherwise select, never shrink it below this member.
#:
#: kiro-cli, not KAS, deliberately: KAS is not an independent harness — it is
#: served by kiro-cli's own ACP relay (``acp/kas_transport.build_kas_argv`` returns
#: ``[kiro_bin, "acp", "--agent-engine", "v3", "--auth-method", "cli"]``), so a KAS
#: floor would rest on the same binary while adding a second thing that can be
#: absent. The floor has to be the member with the fewest preconditions of its own.
#: Revisit if KAS ever ships a binary of its own.
GOVERNANCE_FLOOR_BACKEND: str = ACP_BACKEND_KIRO

# ── Two sets, because policy must be RE-APPLIED, not applied once ──
#
# ``_baseline`` is what the BUILD can serve: the public default plus whatever an
# edition registered. ``_selectable`` is what this DEPLOYMENT may currently select,
# i.e. the baseline minus whatever the live policy denies.
#
# Keeping them apart is what makes the policy re-appliable in BOTH directions. An
# earlier revision of this module had one set and a destructive
# ``deny_selectable_backend``: a ceiling installed at runtime
# (``policy_distribution.apply_ceiling`` replaces ``current_context().governance``
# mid-process) could then never be re-evaluated, so a TIGHTENED fleet policy stayed
# inert until every gateway restarted and a LOOSENED one could not restore what the
# earlier pass had already deleted. Recomputing ``baseline - denied`` has neither
# failure: it is idempotent, order-independent, and reversible.
_baseline: Set[str] = set(BASELINE_SELECTABLE_BACKENDS)
_selectable: Set[str] = set(BASELINE_SELECTABLE_BACKENDS)


def register_selectable_backend(backend: str) -> None:
    """Make *backend* selectable in ``agent.acp_backend``.

    Called from an edition's ``ProviderRegistry.register_acp_backends`` alongside
    the provider registration itself — registering the provider without this
    leaves the harness runnable but unreachable, which is exactly the state a
    hard-coded list produced: an option absent from the dashboard on a build that
    could run it.

    Writes the BASELINE and the effective set together, so an edition that
    registers after a policy pass has already run is still visible to the next
    recompute rather than being silently dropped by it.

    Idempotent, so a re-entrant bootstrap costs nothing. Rejects an id outside
    ``ACP_BACKENDS_KNOWN``: provider construction would raise on it later, and a
    dashboard option that cannot start a session is worse than an absent one.

    ALSO rejects a harness whose routing is :attr:`Routing.UNVERIFIED`, and that
    second refusal is the one worth reading. ``ACP_BACKENDS_KNOWN`` membership is
    not a safety property: it says a build can SPELL the id, which is what lets a
    governance rule deny it. Selectability is a different claim, because a
    selectable harness starts sessions — and for an ``UNVERIFIED`` one nothing
    establishes that its tool calls reach ``HookManager.on_tool_call``, its
    routing verdict is INDETERMINATE so nothing refuses the session, and
    ``tool_gate.is_enforced`` is False so the spawn path applies no compensating
    credential mask. One call from an out-of-repo edition would put a harness in
    exactly that state on the switch, and the only thing standing in its way
    otherwise is a frozen literal plus a test that names it.

    There is deliberately NO opt-out. A keyword flag here would be a documented way
    to put an ungated harness on the switch, and no shipped caller wants one: every
    known backend but one is routed, and the one that is not is deliberately absent
    from the selectable baseline. A harness must have established routing BEFORE it
    can be selectable — an edition that needs otherwise arrives with its own caller
    and its own justification, which is a conversation rather than a flag.
    """
    if backend not in ACP_BACKENDS_KNOWN:
        raise ValueError(
            f"cannot register unknown ACP backend {backend!r}; "
            f"known: {sorted(ACP_BACKENDS_KNOWN)}"
        )
    # ``routing_for`` rather than a direct table read, so this shares the table's
    # own fail-closed default: an id the routing table does not name at all is
    # UNVERIFIED here too, which is the answer that refuses.
    if routing_for(backend) is Routing.UNVERIFIED:
        raise ValueError(
            f"cannot register {backend!r} as selectable: its routing is "
            f"{Routing.UNVERIFIED.value!r}, so nothing establishes that its tool calls "
            "reach the host permission gate, nothing refuses a session that cannot be "
            "gated, and no compensating credential mask is applied. A harness must have "
            "established routing in ACP_BACKEND_ROUTING before it can be selectable."
        )
    _baseline.add(backend)
    _selectable.add(backend)


def selectable_backends() -> FrozenSet[str]:
    """Every backend this deployment may currently select."""
    return frozenset(_selectable)


def registered_backends() -> FrozenSet[str]:
    """Every backend the BUILD can serve, before any policy narrowing.

    The input a policy recompute iterates. Distinct from
    :func:`selectable_backends`, which is the answer AFTER narrowing — asking the
    narrowed set what to narrow is how a one-way ratchet gets built by accident.
    """
    return frozenset(_baseline)


def apply_selectable_denials(denied: Set[str]) -> FrozenSet[str]:
    """Recompute the selectable set as ``baseline - denied``. Returns what was removed.

    The ONE way deployment policy reaches this decision, and the structural
    counterpart to :func:`register_selectable_backend`: rather than adding a second
    gate somewhere downstream, the ``agent_backend`` governance scope narrows this
    registry (``agent_backend_governance.narrow_selectable_backends``, driven from
    ``bootstrap_context`` at boot AND from ``policy_distribution.apply_ceiling``
    whenever a ceiling is installed at runtime). Everything downstream —
    ``resolve_selected_backend``, the PATCH allowlist, ``GET /api/config/schema``,
    the provider factory — then reads the narrowed answer with no code of its own,
    which is what keeps selectability at exactly one gate (harness-parity H4) and
    the Kiro construction path free of an adapter-driven conditional (H13).

    ASSIGNS rather than subtracts, so calling it again with a smaller ``denied``
    RESTORES what a previous call removed. That is the property a runtime ceiling
    swap needs and a destructive remove cannot provide.

    :data:`GOVERNANCE_FLOOR_BACKEND` is force-kept even if named in ``denied``. That
    is not defence against the governance caller, which never submits the floor to
    the scope — it is so that no caller of this function can empty the set and leave
    the install with no startable harness, a state the dashboard cannot repair
    because the trust-root policy is the one file it may not write.
    """
    keep = {b for b in _baseline if b not in denied}
    if GOVERNANCE_FLOOR_BACKEND in _baseline:
        keep.add(GOVERNANCE_FLOOR_BACKEND)
    removed = frozenset(_baseline - keep)
    _selectable.clear()
    _selectable.update(keep)
    return removed


def selectable_backend_values() -> list[str]:
    """:func:`selectable_backends` as a sorted list.

    The form every operator-facing surface wants: a stable option order in the
    dashboard and a stable ``must be one of [...]`` refusal message. Kept here so
    the PATCH allowlist and the schema endpoint share one answer instead of each
    sorting its own.
    """
    return sorted(selectable_backends())


def resolve_selected_backend(value: object) -> str:
    """Coerce a persisted ``agent.acp_backend`` to a backend this build can serve.

    THE single gate, in the one place the pre-registry code already gated: called
    from ``_normalize_acp_backend`` on the way out of ``config.json``. What changed
    is only what it reads — the registry instead of a frozen literal — so the
    coercion behaviour is unchanged from before the registry existed. The Kiro
    construction path deliberately gains no second check: harness-parity H13 keeps
    that path free of conditionals added in service of an adapter, and a check there
    could not fire anyway, since ``AgentConfig`` is built in exactly one place and
    its ``acp_backend`` is never reassigned.

    Runs inside ``KiroCrewConfig.load()``, so it must stay free of anything that
    reads the platform context: ``current_context()``'s lazy branch loads config,
    so a lookup here re-enters the very load that called it and recurses until the
    stack ends — and a broad ``except`` around it does not save you, it converts
    the crash into a silent wrong answer. Reading only the registry keeps it safe.

    An unselectable or unrecognized value — a backend this build did not register, a
    typo, or the non-string shapes a hand-edited ``config.json`` can hold — degrades
    to the default with the reason in the log rather than propagating: ``AcpProvider``
    rejects an unknown backend by raising, and startup refusing with a reason is the
    contract (harness-parity H3).

    An edition that registers a backend must do so before the first config load; the
    registry is read here, not cached, so ordering is the edition's to get right.
    """
    selectable = selectable_backends()
    if isinstance(value, str) and value in selectable:
        return value
    if value not in (None, ACP_BACKEND_KIRO):
        logger.warning(
            "Ignoring agent.acp_backend %r (not selectable in this build); using "
            "the default backend. Selectable values: %s",
            value,
            ", ".join(repr(b) for b in sorted(selectable)),
        )
    return ACP_BACKEND_KIRO


# ── Capability membership (harness-parity H6, H7) ──
# Every capability a backend may claim is an OPT-IN set here, never a negation at
# the call site. ``not is_claude_backend`` reads correctly with two backends and
# then silently hands the capability to the third, so a harness that has never
# demonstrated the capability inherits it — and the operator who never opted into
# that harness is the one who finds out. Adding a member is a deliberate edit
# with evidence; inheriting a default is not a decision. See
# docs/system-specs/modules/harness-parity.md.

# Backends whose single process can host N concurrent ACP sessions (AcpRuntime
# demux) AND whose shared subagent session can be CONTINUED after the conversation
# that spawned it ends. claude-agent-acp runs through AcpClient (one process per
# session) and is not a member.
#
# What survives is the PERSISTED THREAD, never a resident session. Teardown closes:
# a subagent session left alive on the shared process after its parent ends is a
# memory leak, and on codex an expensive one -- each resident session carries the MCP
# fleet ``codex`` starts from its own config, measured at roughly 44 processes and
# 2751 MB for one session. So a member's teardown disposes the in-memory session, and
# ``spawn_continue`` re-reaches the conversation by loading the record the host kept.
# Membership therefore asks one question: after this backend's teardown verb, can a
# ``session/load`` still restore the thread?
#
# kiro-cli answers yes with a transcript Crew holds under ``<kiro home>/sessions/cli``
# that ``_kiro.dev/session/terminate`` leaves on disk.
#
# codex-acp answers yes with a thread ``codex`` persists under ``CODEX_HOME``, and
# the answer is MEASURED rather than argued -- codex-acp 1.11.0 against codex
# 0.154.0, one real adapter:
#
#   * ``session/close`` (a request) evicts: the sessionId stops answering. That is
#     the same fact ``ACP_BACKENDS_SESSION_EVICTION`` records, and it is unchanged.
#   * ``session/load`` on that closed id SUCCEEDS, replays the conversation, and the
#     session then answers a question about the first turn.
#   * the same load succeeds from a RESTARTED adapter process over the same
#     ``CODEX_HOME`` -- the shape a continuation actually takes, since the runtime
#     that served the subagent is usually gone by then. Token accounting confirms
#     the context came from the thread rather than the prompt: the recall turn spent
#     330 input tokens against 9786 cached-read.
#   * ``session/delete`` archives the thread and a load then refuses, so release has
#     a verb that genuinely disposes.
#
# ``ACP_BACKENDS_HARNESS_OWNED_SESSIONS`` is what carries that restore: codex
# resolves a load from the sessionId alone, with no Crew-side transcript to check.
# What is measured where, because the halves have different reach.
# ``test_real_codex_acp_session_close_evicts`` carries the ``real_adapter`` marker, so
# the contract lane runs it on a pinned adapter and an adapter bump that stops
# ``session/close`` evicting goes red there. The RESTORE half has no such lane cover:
# ``session/load`` on a thread the lane's fabricated credential created is refused --
# "no rollout found for thread id", measured -- because a session that never ran a real
# turn has nothing to reload, so the property cannot be asserted without a credential.
# ``test_real_codex_acp_load_after_close_restores`` is where it IS asserted, and that
# test is opt-in: it prompts, so it runs only where a host sets
# ``KIROCREW_LIVE_CODEX_PROMPT_TESTS`` and holds a codex credential, never in CI.
# So an adapter bump does not re-measure the restore this membership rests on, and a
# release that made ``close`` destroy the record would reach the field before any lane
# went red. Re-measuring it is a credentialled run of that opt-in test, which
# ``test/real_adapter_gate.py`` names as part of a codex-acp bump.
#
# KAS answers NO, and that is the whole of its exclusion: its teardown maps to
# ``_kiro/session/delete``, which REMOVES the persisted record, so there is nothing
# for a load to restore and a shared subagent would strand ``spawn_continue`` on
# ``conversation_gone``. A different gap from anything codex had, owned by whoever
# gives KAS a non-destroying teardown; until then its subagents get dedicated
# sessions, which is working behaviour rather than a degraded one.
#
# opencode is not a member: one binary serves one session over its own stdio pipe,
# so there is no second session to share.
# pi is not a member: Crew spawns one ``pi-acp`` process per session over its own
# stdio pipe, so there is no second session to share.
#
# deepseek is not a member, and here the limit is CREW's driver rather than the
# harness. The harness multiplexes: one connection carries several independent
# sessions, ``session/close`` disposes only the addressed one, and a closed session
# stays listable and resumable. What it is not served by is ``AcpRuntime``, the only
# demux Crew has -- see ``ACP_BACKENDS_ACP_RUNTIME`` -- so Crew opens one process per
# session and there is no shared session to persist. A harness capability Crew cannot
# reach is recorded here rather than claimed.
ACP_BACKENDS_SESSION_SHARING = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_CODEX})

# Backends that can load an enrolled member's full saved agent spec at spawn.
# Separate from session sharing and per-session dispatch (harness-parity H6):
# support for either does not establish full-spec loading. Only kiro-cli has
# demonstrated it; the provider still requires a live dedicated runtime and a
# confirmed active template before reporting that the saved spec is loaded.
ACP_BACKENDS_MEMBER_CAPABILITIES = frozenset({ACP_BACKEND_KIRO})

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
# One restriction the mount must NOT step over, and this harness is the only member it
# binds: switching off a tool of the dashboard server is honoured here by withholding
# the whole server (``registry.PerToolDeny.WHOLE_SERVER`` -- no deny slot on the
# element, no file of Crew's, and no structured identity on a tool call to refuse by).
# So ``AcpClient._append_member_dispatch_server`` withholds the mount for a member
# whose dashboard server is narrowed, and that thread runs as plain chat rather than
# reaching a tool the operator switched off. codex and claude keep their mounts there:
# both hold a second channel that still refuses the call.
#
# Switching that server off WHOLE (``disabled``) is a stronger rule and carries no
# backend condition, because the form has no per-call spelling for any harness to
# refuse by (``acp.session_mcp.session_mcp_disabled_servers``). It binds on BOTH paths
# that compose a session's array -- ``AcpClient``'s, which opencode and claude take,
# and ``AcpRuntime``'s create and resume paths, which codex and KAS take -- so the
# operator's switch-off reaches a member session whichever one runs.
#
# pi is excluded on the evidence in ``ACP_BACKENDS_SESSION_MCP_ARRAY``: the array is
# accepted and never forwarded to the agent, so a member dispatch mounted through it
# would be inert.
# deepseek is excluded, and it fails a HARDER test than pi above. It
# does have the mount -- it is a member of ``ACP_BACKENDS_SESSION_MCP_ARRAY`` -- so
# codex's first precondition holds. Codex's second does not: its routing is
# ``Routing.UNVERIFIED``, outside ``tool_gate.ENFORCED_ROUTINGS``, so a session that
# cannot be gated is never refused because nothing gates it. Mounting session control
# into such a session would hand Crew's own control plane to a harness whose tool
# calls Crew does not decide. A member session on it stays plain chat.
#
# WHERE the mount happens differs by backend, and opencode's is the client's.
# ``AcpClient._append_member_dispatch_server`` serves the backends whose array the
# CLIENT composes -- claude's and opencode's -- while codex and KAS are served by
# ``AcpRuntime``: their arrays come from ``AcpRuntime._mirrored_session_mcp`` and
# ``create_session`` / ``load_session`` append the entry themselves, on a non-empty
# ``member_session_key``. That key is where membership is read
# (``providers/acp.py`` ``_member_session_key``), which is why adding a backend here
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
ACP_BACKENDS_MEMBER_DISPATCH = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
    }
)

# Backends implementing the ``_session/steer`` extension (mid-turn steer).
# claude-agent-acp does not implement it, so a steer sent there is answered with
# method-not-found rather than reaching the turn.
# codex-acp (1.11.0) has a steering channel, but not this one and not usable for
# what membership buys. Measured against a real adapter: it is a different method
# (``_session/steering``, ``{sessionId, prompt: [ContentBlock]}``, answered with
# ``{outcome: injected|startedNewTurn|failed}``, advertised as
# ``initialize._meta.steering.supported``) with no ``steering_consumed`` echo --
# and the one thing membership is for, handing a deny reason to the model INSIDE
# the turn that was denied, cannot happen on codex at all: its command approval
# advertises ``cancel`` as the ONLY reject option (no ``decline``), and answering
# it aborts the turn with ``stopReason: "cancelled"`` before the model is called
# again. A steer injected while the permission request is pending returns
# ``injected`` and is then discarded with the turn. So codex stays a non-member
# and takes the refusal-recovery continuation (see
# ``dashboard.state.should_queue_refusal_recovery``), which is the only channel
# that reaches its model.
# opencode is not a member either: its ``initialize`` result advertises
# ``sessionCapabilities`` of close, fork, list and resume, and nothing else.
# pi is not a member: pi-acp's ``initialize`` result advertises ``loadSession`` and
# ``sessionCapabilities`` of list and delete, and no steering extension.
# deepseek is not a member: it advertises close, list and resume only, and permits
# one in-flight prompt per session, so a mid-turn steer has no verb to travel on.
ACP_BACKENDS_STEER = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends that can serve a MANUAL ``/compact`` (the user-typed slash command).
# Every member acts on the ``/compact`` prompt that ``AcpProvider.compact()``
# sends. They do not all answer it the same way, and that split is
# ``ACP_BACKENDS_INLINE_COMPACTION``: kiro-cli ACKs the prompt then emits
# ``_kiro.dev/compaction/status``, which ``wait_for_compaction()`` picks up, while
# claude-agent-acp, codex-acp, opencode, pi-acp and goose finish the whole
# compaction inside the ``session/prompt`` turn.
#
# opencode is a member on a LIVE capture against opencode 1.18.30, and it joins
# despite advertising nothing: its ``available_commands_update`` lists only
# ``customize-opencode``, ``init`` and ``review``. That silence was read here as "no
# compaction capability of any kind", and it is not one -- the harness serves
# ``/compact`` out of its PROMPT handler rather than out of its command list, so the
# command list cannot answer the question. Driven over four turns the session's
# ``usage_update.used`` climbed 14863 -> 15727 -> 16614 -> 17478. A ``/compact``
# prompt then returned ``stopReason: end_turn`` with no status frame of any kind,
# and the next ORDINARY turn read 14577 -- below the pre-compact peak -- with the
# model answering out of a summary of the turns that were dropped. So the context
# really shrank, and the turn's own terminal frame is the only done signal there is.
# The single-turn slice of that drive is committed as evidence rather than quoted:
# ``test/fixtures/acp_frames/opencode/compact-live.jsonl`` carries the ordinary
# turn, the ``/compact`` turn's ``used: 514``, and the ``end_turn`` with nothing
# after it -- which is the ABSENCE this membership rests on.
#
# codex is a member on a capture rather than on its documentation. codex-acp 1.11.0
# driven over stdio advertises ``compact`` in its ``available_commands_update``
# ("Summarize conversation to avoid hitting the context limit"), intercepts the
# ``/compact`` prompt as that command, and answers the same ``session/prompt``
# request once the compaction is done. The frames are a MARKED tool-call pair --
# ``_meta.contextCompaction`` -- which ``_dispatch.parse_codex_compaction_update``
# translates into the compaction status every consumer already reads, so the waiter
# is satisfied from inside the turn rather than stranded after it. Its native
# auto-compaction is real but CONDITIONAL: with ``model_auto_compact_token_limit``
# set it fires on its own and emits the same marked pair, and with the limit absent a
# session held at 50k tokens across three turns compacted not once. So the old
# "manages compaction automatically" promise was true only for an operator who had
# configured it.
#
# pi and goose are NOT members, and the reason is the evidence CLASS rather than the
# feature. Both advertise a ``compact`` built-in and both dispatch it before any
# model turn -- pi-acp 0.0.33 intercepts it in ``prompt()``, awaits
# ``session.proc.compact(...)`` and returns ``{ stopReason: "end_turn" }``; goose
# 1.50.1 routes it through ``Agent::reply`` -> ``execute_command`` ->
# ``handle_compact_command``, and its own ``command_starts_turn("/compact")`` is
# false. So the source says inline in both cases.
#
# What neither has is a driven capture, and this set asks for one: the bar opencode
# met is a live session whose ``usage_update.used`` was seen to fall. Source says
# what the code WOULD do; a capture says what the harness DID. For a membership whose
# wrong answer makes ``wait_for_compaction`` report a completion that did not happen,
# the second is the bar, and holding both to it is what keeps this set's memberships
# comparable to each other. Neither could be driven where this was written -- pi
# answers ``Authentication required``, goose
# ``Failed to resolve provider: GOOSE_PROVIDER`` -- so they wait for someone who can
# drive them rather than entering on the weaker class.
#
# Until then both are unclassified, which is a better position than the one they
# held: they take the ``COMPACT_ARM_UNCLASSIFIED`` refusal, which promises nothing,
# and the gate logs a WARNING naming the memberships they lack, instead of being
# told their harness manages compaction itself on no evidence at all.
#
# kas and deepseek are the other two non-members, and none of the four is the same
# case.
# :data:`ACP_BACKENDS_HARNESS_MANAGED_COMPACTION` carries the difference and the
# consequence: KAS compacts on its own initiative AND says so on the wire, so
# declining its ``/compact`` costs nothing, while deepseek says nothing at all, so a
# decline leaves its context unbounded.
ACP_BACKENDS_COMPACT = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
    }
)

# Backends that compact on their OWN initiative and report it on their ACP surface,
# so Crew's context meter falls back below the threshold without Crew acting.
#
# This is the set that makes a decline HONEST. A backend outside
# :data:`ACP_BACKENDS_COMPACT` cannot be handed a ``/compact`` prompt, and the
# question that remains is what happens instead. A member answers it: KAS runs
# auto-summarization and emits ``summarization_started`` /
# ``summarization_completed``, which ``acp.kas_wire`` maps to a compaction status and
# which calls ``reset_after_compaction()`` on the meter
# (``acp/session_handle.py``) -- so the reading that crossed
# ``session.autocompact_pct`` drops on its own and the user-facing "manages
# compaction automatically" is a description rather than a hope. A backend outside
# BOTH this set and ``ACP_BACKENDS_COMPACT`` has no compaction path Crew can see on
# any surface, so skipping it is not a decline but a leak: nothing bounds the context
# and nothing tells the user. What Crew does about that is decided by a THIRD
# membership, :data:`ACP_BACKENDS_CONTEXT_RECYCLE` -- a member of that set is
# recycled at the threshold, and a backend in none of the three is declined and
# logged at WARNING, because ending a conversation is not something a harness earns
# by never having been classified.
#
# deepseek is the standing non-member and the reason this set exists, and the frames
# that establish it are in the corpus rather than quoted here:
# ``test/fixtures/acp_frames/deepseek/handshake-live.jsonl`` and
# ``turn-live.jsonl``. What those frames establish is an ACP surface with no
# compaction on it: no ``available_commands_update`` is emitted at all,
# ``session/load`` answers ``"Method not found"``, and the advertised capabilities are
# ``mcpCapabilities`` / ``promptCapabilities`` / ``sessionCapabilities`` -- no
# compaction anything. Whether the harness summarizes for ITSELF behind that surface
# is not established here, and is deliberately claimed in neither direction: the
# capture stops at ``used`` 7695 of ``size`` 8192, so it is evidence about 94% of the
# window and says nothing about the wall. Driving deepseek across its own window is
# tracked separately. The membership rests on the observable half, which is the half
# Crew acts on: there is no status to wait for and no command to send, so a reading
# that crossed ``session.autocompact_pct`` would be answered by nothing here however
# the harness behaves at the wall. Meanwhile its ``usage_update`` reports a real
# meter, climbing ``used`` 7554 -> 7695 of ``size`` 8192 across ONE captured turn: a
# reading Crew can act on with nowhere to act. Those two files are the whole basis of this membership, which is an ABSENCE --
# an inventory question, unlike ``ACP_BACKENDS_COMPACT``'s positive capability claim,
# which is why that set needs a harness DRIVEN and this one does not.
#
# A harness that joins NEITHER this set nor ``ACP_BACKENDS_COMPACT`` is DECLINED at
# the threshold, not recycled. The recycle is its own membership,
# :data:`ACP_BACKENDS_CONTEXT_RECYCLE`, because granting the one session-ending arm
# by exclusion would be the same unproven claim this set exists to remove, only
# louder. What such a harness gets instead is a WARNING from the gate naming both
# memberships it lacks, and the refusal arm that promises nothing -- so the leak is
# reported rather than either denied or answered by ending the conversation. pi and
# goose are that case today.
ACP_BACKENDS_HARNESS_MANAGED_COMPACTION = frozenset({ACP_BACKEND_KAS})

# Backends whose FULL context is answered by recycling the session, because no
# compaction reaches them from either side.
#
# The third of three answers to "what happens when this context fills", and the
# only destructive one, which is why it is a membership rather than the leftover.
# A member is a harness Crew cannot hand ``/compact`` to
# (:data:`ACP_BACKENDS_COMPACT`) AND that reports no compaction of its own
# (:data:`ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`), so its context grows until the
# harness's own window ends the conversation for it. Recycling at
# ``session.autocompact_pct`` bounds it, at the cost of what the agent remembered
# — the same cost the window exacts anyway, taken while the session is still
# usable.
#
# deepseek is the one member. Its ACP surface emits no
# ``available_commands_update`` at all (``session/load`` is already
# ``Method not found``) and its ``session/update`` vocabulary carries no compaction
# status, while its ``usage_update`` reports a real meter (``used`` 7554 -> 7695 of
# ``size`` 8192 in one captured turn) — a reading with nowhere to go.
#
# A harness in NONE of the three sets is deliberately not a member here. Granting
# this by exclusion would hand a session-destroying behaviour to every harness
# added later without anyone deciding it, which is the same defect as claiming
# self-management on no evidence — only louder, because this arm ends
# conversations. Such a harness declines like a harness-managed one and is told so
# in its own words (:func:`compact_unsupported_reply` has a third sentence for
# exactly this case), and the gate logs a WARNING naming the gap, so the condition
# is reported rather than silent while somebody decides which set it belongs in.
ACP_BACKENDS_CONTEXT_RECYCLE = frozenset({ACP_BACKEND_DEEPSEEK})

# Backends that finish a manual ``/compact`` INSIDE the ``session/prompt`` turn,
# so the turn's terminal frame is the done signal and there is no asynchronous
# compaction status to await. A NON-member emits the result separately and the
# caller must wait for it (``AcpProvider.wait_for_compaction``); awaiting a
# member instead strands the waiter for the full timeout, and telling a
# non-member it is done leaves the user's ``/compact`` silently unacknowledged.
#
# A STRICT SUBSET of ``ACP_BACKENDS_COMPACT``, which answers the earlier question
# "is a manual /compact offered at all". kas and deepseek are in neither. kiro-cli
# is in ``ACP_BACKENDS_COMPACT`` but not here: it ACKs the prompt and then emits
# ``_kiro.dev/compaction/status``, which is exactly the asynchronous result this
# set says a non-member has. codex-acp IS a member, and its evidence is the same
# capture: the ``tool_call_update`` carrying ``status: "completed"`` and
# ``_meta.contextCompaction`` arrives BEFORE the ``session/prompt`` response, and
# that response is a plain ``stopReason: "end_turn"`` with no compaction status
# following it. So the turn's terminal is the done signal, exactly as it is for
# claude.
#
# Named as a set rather than spelled as an ``is_claude_backend`` check, because an
# identity check hands the synchronous arm to every harness that is claude and
# withholds it from every harness that is not, with neither being a decision anyone
# recorded (harness-parity H6). Membership is exactly the set of harnesses that
# demonstrate the capability.
#
# opencode is a member on the evidence recorded on ``ACP_BACKENDS_COMPACT``, and it
# demonstrates the capability in the one way this set is about -- where the done
# signal lands. Its ``/compact`` turn ends with ``stopReason: end_turn`` and emits no
# status frame at all, so the turn's terminal frame is the ONLY signal there is,
# which makes awaiting one a strand rather than a wait.
#
# pi and goose are absent for the reason recorded on ``ACP_BACKENDS_COMPACT``: their
# source says inline, no capture confirms it, and the two memberships move together
# when one does.
ACP_BACKENDS_INLINE_COMPACTION = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
    }
)

# Backends carrying their OWN internal OS sandbox, which on macOS cannot nest
# inside Kiro Crew's seatbelt (kernel EPERM) — so ``sandbox.wrap_argv`` skips
# Crew's own layer for them. This is the one membership test that fails OPEN:
# claiming it for a harness with no internal sandbox hands isolation to a layer
# that never starts and leaves the agent process unconfined. Only kiro-cli
# qualifies; a Node or Python harness does not, however it is spawned.
#
# KAS is NOT a member even though Crew now spawns it as ``kiro-cli acp
# --agent-engine v3`` and the process on the end of the argv IS kiro-cli. The
# relay spawns the KAS server without an ``--sandbox`` argument, and KAS's
# sandbox factory resolves an absent config to its no-op backend, so no OS
# sandbox starts inside — adding KAS here would skip Crew's seatbelt in favour of
# a layer that does not exist. See :mod:`kiro_crew.acp.kas_transport`.
#
# codex-acp is excluded on the same rule: it is a Node adapter, so Crew's own layer
# is the only OS confinement a codex session gets. The Codex sandbox modes the
# adapter can apply are in-process policy, not an OS sandbox that Crew's would
# nest inside.
#
# opencode is excluded, and here the exclusion is load-bearing rather than
# conservative: Crew's own sandbox layer carries the credential mask that is the
# compensating control for this harness's passive reads, so skipping that layer
# would remove the control. It carries no OS sandbox of its own to replace it.
#
# pi is excluded on the same load-bearing ground, and its own documentation says
# so in as many words: pi has no built-in sandbox and runs tools with the
# permissions of its process. Crew's layer is the only one there is.
# deepseek is excluded on that same load-bearing ground, and the thing that makes it
# tempting is exactly what makes it wrong: its composition DOES mount a sandbox of
# its own, and Crew even pins that sandbox's mode. But it confines the harness's own
# tool executors from inside the same process -- it is not an OS sandbox that Crew's
# seatbelt would nest inside, and it does not carry Crew's credential mask. Skipping
# Crew's layer for it would drop the compensating control for its passive reads.
ACP_BACKENDS_INTERNAL_SANDBOX = frozenset({ACP_BACKEND_KIRO})

# Backends whose pod-spawned child has its ambient ``HOME`` relocated onto the
# pod's own tree, so the MCP OAuth grant artifacts the harness derives from
# ``$HOME`` stay pod-scoped (``acp.client._apply_pod_home_remap``).
#
# Deliberately its OWN set rather than a reuse of
# ``ACP_BACKENDS_INTERNAL_SANDBOX``, even though the membership is identical
# today. The two answer different questions -- "does this harness carry its own
# OS sandbox?" versus "does relocating this harness's HOME move its credential
# store?" -- and conflating them means a harness added to the sandbox set for
# sandbox reasons silently inherits credential-relocation semantics it never
# opted into. That is the capability conflation harness-parity H6 exists to
# prevent, so each membership stays an explicit decision.
#
# Only kiro-cli qualifies: it derives its OAuth artifact directory from ``$HOME``
# with no env override for that subtree alone, which is what makes the remap the
# only reachable lever. A harness that stores credentials elsewhere gains
# nothing from the remap and would only lose its real-home state, so it must not
# be added without checking where it actually reads credentials from.
#
# opencode is excluded because the lever it needs already exists: its credential
# home follows ``XDG_DATA_HOME``, which its auth declaration names, so the
# credential floor re-anchors the declared leaf under the override and no ``$HOME``
# relocation is required to reach it.
#
# pi is excluded for the same reason with a different variable: its whole agent
# directory, credential file included, follows ``PI_CODING_AGENT_DIR``, which its
# auth declaration names.
# deepseek is excluded on the same grounds: its whole home is ``DSH_HOME``, which its
# auth declaration names, so the floor re-anchors the declared leaf under the
# override and a ``$HOME`` relocation reaches nothing the override does not.
ACP_BACKENDS_POD_HOME_REMAP = frozenset({ACP_BACKEND_KIRO})

# Backends served by AcpRuntime + AcpSessionHandle — the kiro-agent family
# (kiro-cli and KAS) whose single process hosts N sessions via demux.
# claude-agent-acp runs one AcpClient per session and is NOT a
# member. Membership drives the shared runtime start path and the kiro-family
# spawn conventions: members read the cli.json effort/tool-search overlay and
# receive effort at spawn, whereas claude applies it via a live push after the
# session is ready. Stated as opt-in membership (harness-parity H5/H6) so the
# four sites that mean "kiro or kas" say so positively rather than as
# ``not is_claude_backend`` — an inference that silently captures every harness
# added later. This is a SUPERSET of ACP_BACKENDS_SESSION_SHARING: running on
# AcpRuntime is necessary for session sharing but not sufficient (KAS runs here
# yet is excluded from sharing until keep-aware teardown lands).
# opencode is not a member: it is spawned per session and reads none of the
# kiro-family cli.json overlay, so it takes the AcpClient path.
# pi is not a member for the same reason: one ``pi-acp`` process per session, no
# cli.json overlay, the AcpClient path.
#
# deepseek is not a member, and it is the first harness excluded here whose own
# protocol would support membership: one of its connections carries several
# independent sessions. ``AcpRuntime`` is not a general multiplexer, though -- it
# carries the kiro-family spawn argv, the cli.json effort and Tool Search overlay,
# and the ``_kiro/*`` auth and delete verbs -- so serving this harness from it means
# a demux that is not kiro-shaped, which is its own work. It takes the AcpClient
# path, one process per session, until that exists.
#
# ``ACP_BACKEND_CODEX`` IS a member, and it is the first one that is not
# kiro-shaped. It earns membership on the two facts a shared process needs, both
# captured off a live ``codex-acp`` 1.11.0 against a real ``codex`` build:
#
# * one adapter process serves N sessions and keeps them apart. Two
#   ``session/new`` calls on one connection return distinct ids, and across a
#   prompt on one of them every ``session/update`` notification carries that
#   session's id -- none leaks to the other.
# * the adapter itself does not grow with sessions: 103 MB at zero sessions and
#   85 MB at eight. The growth is in ``codex app-server``, and it is gentle
#   (+14 MB per session on a minimal config, +37 MB with a host MCP registry).
#
# What it does NOT bring is the kiro-family spawn convention: it reads no
# cli.json overlay and takes effort through ``session/set_config_option``. That
# is why membership here is a statement about the TRANSPORT and nothing else --
# every kiro-family convention is its own set, and codex is absent from each.
ACP_BACKENDS_ACP_RUNTIME = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX})

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
# on-disk form is Crew's to parse; codex-acp, opencode and pi are not members
# because none of them reads ``~/.kiro/agents`` at all.
ACP_BACKENDS_MARKDOWN_AGENT_SPECS = frozenset({ACP_BACKEND_KAS})

# Backends whose agent spec comes from the USER-LEVEL directory alone, so a
# checkout's same-named spec is not the agent their session is running.
#
# Every other host resolves the nearer layer too: kiro-cli reads
# ``<project>/.kiro/agents/`` itself for ``--agent``, and a MIRRORED host receives
# the array ``acp/session_mcp.py`` translates, which is project-nearest-first. KAS
# is the exception -- ``acp/kas_agents.load_agent_spec`` is handed
# ``paths.kiro_agents_dir()`` and reads nothing else, which
# ``agent_discovery.project_agent_files`` already states, so a project-only agent
# selected on a KAS session is refused at session start rather than projected.
#
# What membership decides is the SCOPE of the broker-overlay lookup
# (``mcp_gateway.session_servers``). That overlay is keyed by agent name and is
# also written from the user-level directory, so for every OTHER host a
# checkout-declared name means the overlay holds no stubs for this session. For a
# member the reverse holds: the user-level agent IS the one running, so scoping
# its lookup would suppress the stubs for servers the session really has and run
# them outside the pool, outside caller-identity attribution and outside broker
# governance. Read through the runtime's own scope helper, never as "is KAS": a
# host added later that reads the user level alone joins here.
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
    reads the user level alone and leaves above, so the term would have no caller
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
# ``tools`` refs against the servers the session actually receives, and for an
# array-backed host that is the wire array: what is not in it is not mounted.
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

# Backends whose teardown verb actually EVICTS the session from the adapter's own
# session map, freeing what it held.
#
# Running on a shared process (the set above) is not that claim. A harness can
# multiplex perfectly and still have no verb that disposes one session, and the
# difference only shows on a process that outlives many sessions: every
# non-evicting teardown leaves its session addressable and its context resident,
# so the adapter grows without bound at whatever rate sessions are created.
#
# kiro-cli and KAS are members: their teardown verbs (``_kiro.dev/session/terminate``
# and ``_kiro/session/delete``) remove the session from the process.
#
# codex is a member on captured evidence. Crew's teardown verb for it is the
# standard ``session/close``, sent as a request; measured live against codex-acp
# 1.11.0, the adapter answers it with ``{}`` and afterwards the same sessionId
# stops answering ``session/set_config_option`` -- the session is gone from the
# process, while the Codex thread's own record survives (evict, not delete). The
# contrast on the same wire is ``session/cancel``: after it the same oracle keeps
# answering and a further ``session/prompt``'s ``cachedReadTokens`` shows the
# context resident, so ``cancel`` interrupts a turn and is not a teardown, and a
# harness sending it as one is correctly excluded here. The harness docstring on
# ``CodexHarness.teardown`` carries the full measurement, and a gated live test
# repeats it on every install that has the adapter.
#
# Read by every path that creates and destroys sessions on a shared process, and
# that is why membership is one fact rather than one gate per caller:
# :func:`kiro_crew.session._bg_runtime_backends` (background handles -- title
# generation, suggestions, folders, nav -- each taking an ephemeral sessionId many
# times per conversation), ``AcpSessionProvider.new_conversation`` (warm pooled
# reuse, resetting at the rate a workflow's steps run), and the entitlement probe
# in ``AcpRuntime`` (a throwaway session per unavailable-model pick). A harness
# whose teardown does not evict leaks one session on every one of those paths, at
# rates the operator never controls; a harness in this set frees it on each.
#
# An opt-in set rather than a subtraction at any call site, for the reason every
# set in this section is opt-in (harness-parity H6/H7): a harness added later and
# spelled as "not <some host>" would inherit an eviction guarantee it has never
# demonstrated, and the operator who never opted into it is the one who finds the
# adapter growing. Membership is earned by a measured teardown, not by default.
ACP_BACKENDS_SESSION_EVICTION = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX})

# ``ACP_BACKENDS_KIRO_IDENTITY_STORE`` is gone, and it has no replacement HERE.
# Whether a ``kiro-cli logout`` may retire a running child is a fact about how the
# harness SIGNS IN, and it was the third hand-maintained copy of that fact -- beside
# the credential floor and the sandbox mask, each of which had to agree with it. It is
# now ``host_auth.backends_retired_by_host_logout()``, projected from the harness's own
# declaration, which is where the same declaration also supplies the leaf the floor
# fences and the leaf the mask spares.
#
# It could not become a projected SET in this module: this module supplies the backend
# ids that table is keyed by, so importing ``host_auth`` from here would close a cycle.
# And it must not be a projected set in ``host_auth`` either -- an ``ACP_BACKENDS_*``
# name is vocabulary, whose home this module is, and the harness-parity gate enforces
# that. A derived answer is not vocabulary, so it stays a function and the question of
# which module owns the set does not arise.

# Backends that switch models through ``session/set_config_option("model", ...)``
# rather than the kiro-native ``session/set_model`` request. Opt-in for the same
# reason as every set above: a switch sent down a channel the adapter does not
# implement is answered with method-not-found, and the session keeps serving turns
# on the model the operator thought they had just left.
#
# ``ACP_BACKEND_OPENCODE`` is a member on captured evidence: its ``session/new``
# result advertises a ``model`` select whose ``currentValue`` is the configured
# ``provider/model`` id, and that select is the channel a switch travels down.
#
# ``ACP_BACKEND_PI`` is a member on the same kind of evidence: pi-acp's
# ``session/new`` result advertises a ``model`` select whose ``currentValue`` is the
# ``provider/model`` id pi resolved from its own ``models.json``
# (``test/fixtures/acp_frames/pi/session-live.jsonl``).
#
# ``ACP_BACKEND_DEEPSEEK`` is a member on the same kind of evidence, with one shape
# to respect: its ``model`` select carries OPAQUE values. Each is a JSON-encoded
# ``[provider, model]`` pair -- ``["deepseek-official", "deepseek-v4-flash"]`` -- so
# the value is passed through exactly as advertised rather than parsed. That is what
# ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` membership is for.
ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    }
)

# Backends that take a reasoning-effort change through
# ``session/set_config_option("effort", ...)``. A SEPARATE set from the model
# channel above despite identical membership today: the two config options are
# advertised independently, and ``AcpClient.supports_config_option`` exists
# precisely because adapter builds ship one without the other. Collapsing them
# would make an adapter that gained model-switching inherit an effort channel it
# never advertised.
#
# opencode is NOT a member, which is exactly the split this separate set exists
# for: the same ``session/new`` result that advertises its ``model`` select
# advertises a ``mode`` select beside it and no ``effort`` option at all.
#
# pi is NOT a member for the same kind of reason: the option beside its ``model``
# select is ``thought_level`` (off ... xhigh), a different id with a different
# vocabulary, and this set names the harnesses whose option is ``effort``.
#
# deepseek IS a member: the same ``session/new`` result carries both selects, the
# effort one offering off, low, high and max. It advertises that option under its own
# id, ``reasoning_effort``, which ``EFFORT_CONFIG_OPTION_IDS`` below records --
# membership says the channel exists, the table says what to call it.
ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION = frozenset(
    {ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_DEEPSEEK}
)

# Backends whose ADVERTISED model ids are ``<model>[<effort>]`` pairs that the
# ``model`` config option does not accept whole. codex-acp is the member: its
# ``models.availableModels`` is one entry per model x reasoning effort (the
# legacy ``session/set_model`` vocabulary, and what the picker shows), while its
# ``model`` select takes only the bare model and the effort travels down the
# separate ``reasoning_effort`` option. A member's exhausted spelling ladder falls
# through to that two-write split; a non-member's refused bracketed id stays
# refused. Opt-in (harness-parity H13): claude-agent-acp's ``[1m]`` suffix is a
# context window and must reach the wire intact, and opencode's ``provider/model``
# ids carry no suffix at all -- neither may inherit a split it never advertised.
ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS = frozenset({ACP_BACKEND_CODEX})

# The ``configId`` each backend spells its reasoning-effort option with. One home
# for a fact that is per-harness vocabulary, not a constant: claude-agent-acp
# advertises ``effort`` and codex-acp advertises ``reasoning_effort``, and a
# session that writes the other one's spelling is answered with "unknown config
# option" and silently keeps whatever effort it already had.
#
# Opt-in by exception (harness-parity H13): the default is the ``effort`` spelling
# every existing member of ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` runs through,
# and a backend only appears here to name a different one.
#
# The table and the default are read ONLY by :func:`effort_config_option_id`, and
# neither crosses a facade: a consumer indexing the mapping gets a ``KeyError`` for
# every backend without a row, which is the whole failure the resolver exists to
# prevent. The function is the export.
EFFORT_CONFIG_OPTION_IDS: Mapping[str, str] = {
    ACP_BACKEND_CODEX: "reasoning_effort",
    ACP_BACKEND_DEEPSEEK: "reasoning_effort",
}

#: The spelling used by every backend without a row in
#: ``EFFORT_CONFIG_OPTION_IDS``.
DEFAULT_EFFORT_CONFIG_OPTION_ID = "effort"


def effort_config_option_id(backend: str) -> str:
    """The ``configId`` *backend* exposes its reasoning effort under.

    Every effort site -- the dashboard's live change, the startup application of
    a persisted slot level, the knowledge pool's apply, the level reader that
    fills the dropdown, and the effort half of a ``<model>[<effort>]`` pick --
    resolves the id here. Two spellings of the same option in one tree diverge
    silently: a write to the wrong id draws "unknown config option", which every
    one of those callers treats as "this adapter has no effort selector" and
    skips, so the session runs an effort the UI does not report.
    """
    return EFFORT_CONFIG_OPTION_IDS.get(backend, DEFAULT_EFFORT_CONFIG_OPTION_ID)


# Backends that resolve the WIRE model id from the provider's OWN advertised list
# (captured from ``session/new`` and cached across sessions) rather than trusting
# the stored id verbatim. Needed where the spelling a backend SERVES differs from
# the one Crew stored: claude-agent-acp advertises versioned ``…[1m]`` ids whose
# bare form collapses to the base (200K) context window. A member both FEEDS the
# advertised-model cache on capture and FOLDS the id onto it — at spawn and on a
# warm-pool ``set_model`` — so a switched model lands on the served spelling.
# Opt-in (harness-parity H6): a future adapter with the same spelling gap joins
# here; one whose wire ids are already exact (kiro-cli serves its ids verbatim and
# gets windows from the ``--list-models`` cache) never needs to.
#
# ``ACP_BACKEND_CODEX`` is a member for the OTHER half of what membership buys:
# the capture. codex-acp advertises its model list only as a ``configOptions``
# ``model`` select on ``session/new``, and that list is the ONLY source of ids
# ``session/set_config_option("model", ...)`` accepts -- the static registry has no
# codex namespace, and kiro-cli's ``--list-models`` catalog names models codex
# refuses with a bare ``-32602``. Without membership the capture skipped the
# select, the picker showed kiro's catalog, and a pick from it killed the session
# at startup. Its spelling fold is a no-op (codex serves its ids verbatim), which
# is fine: the cache it feeds is what the picker reads back.
#
# ``ACP_BACKEND_OPENCODE`` is a member for the capture half as well. Its ids are
# ``provider/model`` pairs it resolves from its own config -- ``ollama/qwen3:8b``
# for a local model, ``opencode/…`` for its hosted ones -- so the advertised select
# is the only vocabulary its ``session/set_config_option`` accepts, and the static
# registry names none of them.
#
# ``ACP_BACKEND_PI`` is a member for the capture half too, and its ids come from
# the same place opencode's do: ``provider/model`` pairs out of the operator's own
# ``models.json`` (``ollama/llama3.2:3b`` for a local model), which no static
# registry names.
#
# ``ACP_BACKEND_DEEPSEEK`` is a member for the capture half, and its ids make the
# capture the ONLY workable source rather than merely the best one: the select's
# values are JSON-encoded ``[provider, model]`` pairs drawn from the harness's live
# service catalog. Nothing can spell one of those from a stored bare model name, so
# a pick that did not come from the capture is a pick the session refuses.
ACP_BACKENDS_ADVERTISED_MODEL_SELECTION = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    }
)

# Backends that seed a per-session settings file — claude-agent-acp's
# ``settings.local.json`` — to lock the model + permission surface. The file is
# written once at spawn, but a warm-pool claim switches model on a process that
# has ALREADY read it, so a member must RE-SEED it on ``set_model``: the
# spawn-time write alone leaves a stale allowlist/model behind, which is what let
# a switched model collapse to its base window on a claimed pool runtime. Opt-in
# for the same reason as every set here — a harness with no such file is not a
# member and takes no re-seed.
#
# opencode is NOT a member, and the reason is not that nothing is supplied to it:
# this set drives the RE-SEED on ``set_model``, because claude's
# ``settings.local.json`` pins the model. What opencode is handed is inline config in
# its own environment carrying ``permission`` alone, and its model travels as a
# config option, so a claim that switches model leaves nothing stale to re-seed --
# and there is no file of Crew's in the work dir at all.
#
# deepseek is NOT a member, and for it the answer is simpler than for either
# harness above: Crew writes no file for it at all. What it supplies to the child is
# one pinned environment variable, and its model travels as a config option, so a
# warm-pool claim that switches model leaves nothing anywhere to re-seed.
ACP_BACKENDS_SEED_LOCAL_SETTINGS = frozenset({ACP_BACKEND_CLAUDE})

#: Operator lever for the permission mode a seeded session runs under, read by
#: :func:`resolve_cc_permission_mode`. One name and one resolver, so a per-session
#: lever and an operator-wide one cannot disagree about the same session.
CC_PERMISSION_MODE_ENV = "KIROCREW_CC_PERMISSION_MODE"


def resolve_cc_permission_mode(explicit: str | None, backend: str) -> str | None:
    """The ``permissions.defaultMode`` a session seeds, or ``None`` to seed nothing.

    Two opt-ins, one answer: the caller's own per-session request (the dashboard
    slot's Auto intent) first, then :data:`CC_PERMISSION_MODE_ENV` for an operator
    who wants every session on the backend's classifier. ``None`` leaves the
    seeded ``settings.local.json`` without a ``defaultMode`` key at all, which is
    the backend's own per-tool default -- so with nothing asking, nothing widens.

    Fail-closed in both axes. A backend that seeds no per-session settings file
    gets ``None``, asked of :data:`ACP_BACKENDS_SEED_LOCAL_SETTINGS` rather than of
    the backend id, so a future seeding adapter joins the set instead of editing
    this. And ``auto`` EXACTLY is the only value that resolves: a typo, a stray
    space, a stale value or an inherited ``bypassPermissions`` resolves to ``None``
    rather than to a wider surface than the one it names.

    An unrecognised value is not silently dropped, because that is the failure this
    whole path was reported for: a lever an operator believes is set, doing nothing,
    with every other signal reading healthy. It warns and names what it read.
    """
    # Function-scope import, not module-scope: this module is on the load path of
    # ``KiroCrewConfig.load()`` and stays free of ``kiro_crew.acp`` there (see the
    # import-light note in ``kiro_crew.acp_backends``; ``test_acp_capability_sets_leaf``
    # pins it in a subprocess).
    from kiro_crew.acp.types import CC_PERMISSION_MODE_AUTO

    if backend not in ACP_BACKENDS_SEED_LOCAL_SETTINGS:
        return None
    requested = explicit or os.environ.get(CC_PERMISSION_MODE_ENV) or ""
    if requested == CC_PERMISSION_MODE_AUTO:
        return CC_PERMISSION_MODE_AUTO
    if requested:
        logger.warning(
            "permission mode %r is not recognised (only %r is); this session runs on "
            "the backend's own per-tool default",
            requested,
            CC_PERMISSION_MODE_AUTO,
        )
    return None


# Which model-registry NAMESPACE a backend's ids live in. This is a registry index
# key, NOT a provider-identity check (see agent_sdk.provider_identity, note 3): a
# context window is a property of the MODEL, so the same model reached via two
# backends shares one namespace. Two consumers read it, which is why every known
# backend is mapped rather than only the members of one set: the wire-id fold for an
# ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` member picks its registry index here,
# and :func:`kiro_crew.agent_sdk.capabilities.capabilities_for` reads it for EVERY
# backend to fill ``SessionCapabilities.model_id_namespace``. Defaults to the
# ``acp`` (kiro) namespace, where
# every non-claude id the registry carries lives today. The literals are the
# model_registry's own provider keys, spelled here rather than imported to keep
# this load-path leaf free of a ``kiro_crew.model_registry`` dependency.
#
# ``ACP_BACKEND_CODEX`` gets its OWN key. The same key also selects the bucket of
# the cross-session advertised-model cache (``model_registry.advertised_models``),
# and codex's served ids (``gpt-5.4``, ``gpt-5.4-codex``, ...) are a different
# vocabulary from the kiro ids that live under ``acp``. Sharing the bucket would
# let a codex session overwrite what the picker offers for a kiro-family harness.
# The static registry has no ``codex`` provider, so the translation into this
# namespace is a passthrough -- which is exactly right for ids the backend itself
# advertised.
_MODEL_REGISTRY_NAMESPACE_BY_BACKEND: dict = {
    ACP_BACKEND_CLAUDE: "claude_code",
    ACP_BACKEND_KIRO: "acp",
    ACP_BACKEND_KAS: "acp",
    ACP_BACKEND_CODEX: "codex",
    # opencode gets its own key for the same reason codex does: its ids are
    # ``provider/model`` pairs drawn from the operator's own provider list, so
    # sharing the ``acp`` bucket would let one harness overwrite what the picker
    # offers for another.
    ACP_BACKEND_OPENCODE: "opencode",
    # pi likewise: ``provider/model`` pairs from the operator's own models.json.
    ACP_BACKEND_PI: "pi",
    # goose likewise, and it is the sharpest case: its ``session/new`` advertises the
    # whole provider catalog it could reach, so sharing the ``acp`` bucket would not
    # merely mix two harnesses' ids -- it would replace kiro-cli's advertised catalog
    # with a list of every provider goose knows about.
    ACP_BACKEND_GOOSE: "goose",
    # deepseek gets its own key on the strongest form of the same reason: its ids are
    # JSON-encoded ``[provider, model]`` pairs from its own catalog, a vocabulary no
    # other harness spells, so a shared bucket would offer the picker ids that only
    # one backend can accept.
    ACP_BACKEND_DEEPSEEK: "deepseek",
}


def model_registry_namespace(backend: str) -> str:
    """The model-registry namespace key for *backend* (default ``acp``)."""
    return _MODEL_REGISTRY_NAMESPACE_BY_BACKEND.get(backend, "acp")


# Backends implementing ``_kiro.dev/commands/execute`` — the kiro extension that
# runs a slash command as an RPC. Non-members have no equivalent verb, so their
# slash commands go through ``session/prompt`` and are interpreted by the adapter
# (or degrade to prompt text) instead of returning -32601 for the whole call.
#
# The same membership decides who reads the workspace ``cli.json`` overlay for
# EFFORT: the kiro-family harnesses take ``chat.modelDefaults`` from that file at
# spawn, and writing it for a harness that never reads it leaves a stale file in
# the user's workspace that no later clear can reach. Tool Search has its own,
# narrower set below -- the two hosts read that setting from different places.
# opencode is not a member: it has no ``_kiro.dev`` verb, and it publishes its own
# command list as an ``available_commands_update`` on ``session/update`` instead.
# pi is not a member for the same reason: pi-acp publishes its built-ins the same
# way and has no ``_kiro.dev`` verb.
# deepseek is not a member and publishes no command list either: it carries commands
# internally and its ACP surface rejects them, so it exposes none over the wire.
ACP_BACKENDS_KIRO_SLASH_COMMANDS = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends that read the MCP Tool Search setting from the workspace ``cli.json``
# overlay (``toolSearch.*`` keys). Only kiro-cli's Rust engine does. KAS shares
# the slash-command dialect above but never opens that file: on the relay path
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
ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD = frozenset({ACP_BACKEND_KIRO})

# Backends on which a Side Chat turn may EXECUTE read-only tools under
# ``ToolApprovalPolicy.READ_ONLY``. The allowance rests on a kiro-cli agent-spec
# mechanism: the side session is bound to a derived ``<agent>--readonly`` spec
# (``dashboard/side_readonly_spec``) whose emptied grants make every tool call
# raise a permission request the host gate judges. Another harness has its own
# pre-approval surface — claude-agent-acp's ``permissions.allow`` /
# ``bypassPermissions``, KAS ``permissions`` rules read from its own store — that
# neither the derived spec nor the gate can see, so a call it pre-approves would
# run with no READ_ONLY decision and no SEL row. Off this set the side turn runs
# ``REJECT_ALL``, the pre-allowance posture, and its footer says tools are
# unavailable there. A harness joins by demonstrating that every tool call it
# serves reaches ``session/request_permission`` under the derived spec.
#
# opencode is NOT a member. Its tool calls do reach ``session/request_permission``
# (captured live), but that is by way of its own ``permission`` setting, not the
# derived ``<agent>--readonly`` spec this allowance is built on -- the harness reads
# no kiro agent spec at all. A side turn on it therefore runs ``REJECT_ALL`` until a
# read-only posture is expressed in the harness's own permission vocabulary.
#
# pi is NOT a member either. Every one of its tool calls does reach
# ``session/request_permission``, but through Crew's gate extension rather than the
# derived spec, and the extension expresses no read-only posture -- it asks about
# everything and lets the host decide.
# deepseek is NOT a member, and the tempting part is that its permission vocabulary
# HAS a read-only posture Crew can select with ``DSH_PERMISSION_MODE``. Selecting it
# would confine the harness and still not produce a READ_ONLY DECISION anyone can
# see: that posture is enforced by the harness's own sandbox, which denies rather
# than asks, so no call reaches the host gate and no SEL row is written. A side turn
# on it runs ``REJECT_ALL``.
ACP_BACKENDS_SIDE_READONLY = frozenset({ACP_BACKEND_KIRO})

# Backends whose model-side REFUSAL arrives with a structured reason, not just a
# stop reason. When the Kiro service's content filter declines a turn, kiro-cli
# (and KAS, its relay) emit a ``_kiro.dev/metadata`` notification carrying
# ``stopReason: CONTENT_FILTERED`` plus a ``refusal`` object -- ``category``
# (``CYBER``, ...), the service's canned ``explanation``, and an optional
# ``recommendedModel`` -- milliseconds BEFORE the turn's terminal frame. Nothing
# in the terminal itself says why the turn stopped: the canned explanation is
# streamed as ordinary assistant text, and the ``session/prompt`` result may
# read ``end_turn`` or come back as a bare ``-32603 Internal error``.
#
# Membership decides whether :func:`kiro_crew.acp._dispatch.parse_refusal` is
# consulted on that notification. Every harness still lands on the SAME
# :class:`kiro_crew.acp.types.RefusalInfo` -- claude-agent-acp only reports
# Anthropic's ``stopReason: "refusal"`` with no reason attached, codex-acp
# likewise -- so a non-member is not "unsupported": its refusal card simply has
# no category line. The set exists so a future harness that carries its own
# reason payload is added HERE with a parser, rather than by widening the
# metadata reader to guess at every notification's shape.
# opencode is not a member: it carries no reason payload of its own, so its
# refusal card has no category line.
# pi is not a member: same.
# deepseek is not a member: it maps a turn ending onto a plain ACP ``stopReason`` and
# keeps provider-specific detail off the wire, so its refusal card has no category
# line either.
ACP_BACKENDS_STRUCTURED_REFUSAL = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

# Backends whose child may ask THIS host for an access token over the
# ``_kiro/auth/getAccessToken`` connection-level request, to be answered from Kiro
# Crew's own credential vault (:mod:`kiro_crew.auth`). KAS is the member: when Crew
# holds a signed-in identity of its own it spawns the relay WITHOUT
# ``--auth-method cli`` (see :func:`kiro_crew.acp.kas_transport.build_kas_argv`),
# which leaves the engine's credential callback on the wire for Crew to answer.
# Membership is what authorizes the runtime to hand a credential to a child at
# all; a request with that method from any non-member is answered
# method-not-found like every other ownerless request, never with a token.
# Positive membership rather than ``== ACP_BACKEND_KAS`` in the shared runtime
# (harness-parity H5). Distinct from ``host_auth.backends_retired_by_host_logout()``
# on purpose:
# "may be handed Crew's credential" and "is invalidated by a kiro-cli logout" are
# different properties, and a member here that is spawned in cli-owned mode (no
# Crew identity stored) never receives the callback in the first place.
# opencode is not a member: it authenticates from its own credential file, and its
# ``initialize`` result advertises its own ``opencode-login`` auth method, so it
# never asks this host for a token.
# pi is not a member: it authenticates from its own ``auth.json`` and advertises its
# own ``pi_terminal_login`` auth method.
# deepseek is not a member, and it asks for less than opencode does: its
# ``initialize`` result advertises ``authMethods: []`` and its ``authenticate``
# returns immediate success, so the ACP layer authenticates nothing at all and the
# provider key it needs is resolved inside the harness from its own credential store.
ACP_BACKENDS_HOST_AUTH_CALLBACK = frozenset({ACP_BACKEND_KAS})

# Backends that keep their OWN session records and resolve a resume from the
# ``sessionId`` alone. For a member there is no Crew-side transcript to check
# before ``session/load`` and no ``_kiro.dev/session_file`` to send with it; a
# non-member is the kiro family, whose transcript Crew holds under
# ``<kiro home>/sessions/cli`` and pre-checks so a missing file falls back to a
# fresh session rather than a failed load. A SET rather than a chain of identity
# tests on the resume path: that path is shared with kiro-cli, and harness-parity
# H13 keeps it free of conditionals added in service of an adapter -- a harness
# added later is one member here, not one more ``elif`` there.
#
# pi is a member: pi-acp advertises ``loadSession`` and resolves a ``session/load``
# from the id alone through its own session map, replaying the conversation as
# ``user_message_chunk`` / ``agent_message_chunk`` updates before answering
# (``test/fixtures/acp_frames/pi/session-load-live.jsonl``).
ACP_BACKENDS_HARNESS_OWNED_SESSIONS = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
    }
)

# Backends whose SUCCESSFUL ``session/load`` result carries no ``modes`` block.
# The kiro family and claude return one, and its absence on those harnesses is how
# a load that did not really take is told apart from one that did. OpenCode's
# successful ``session/load`` result carries ``configOptions`` and no ``modes`` --
# OBSERVED, in ``test/fixtures/acp_frames/opencode/session-load-live.jsonl``, where a
# second process loaded a session the first had created and the harness replayed the
# conversation before answering. For a member a response that is not an error IS the
# successful load. Without membership a reopened session would load, fail the
# ``modes`` check, fall through to ``session/new`` and discard the conversation the
# harness had just restored.
# pi is NOT a member: its successful ``session/load`` result carries a ``modes``
# block (pi-acp maps its thinking levels onto ACP modes), observed in the same
# capture, so the ordinary gate applies.
#
# deepseek is a member on its schema rather than on a capture of a populated block:
# the response to the verb it DOES serve declares ``modes`` optional, and the harness
# rejects modes across its whole surface, so it can never return one. Membership is
# what keeps a restored conversation from being discarded by a check for a block this
# harness has nothing to put in.
ACP_BACKENDS_LOAD_WITHOUT_MODES = frozenset({ACP_BACKEND_OPENCODE, ACP_BACKEND_DEEPSEEK})

# Backends that restore a session with ``session/resume`` instead of
# ``session/load``, and advertise it under ``sessionCapabilities.resume`` instead of
# the ``loadSession`` flag.
#
# Both are STANDARD ACP v1. The schema says so of the verb this set names: it
# "resumes an existing session without returning previous messages (unlike
# ``session/load``)", and is "useful for agents that can resume sessions but don't
# implement full session loading". So a member is not a harness with a quirk to
# accommodate -- it is a harness serving the optional method the specification
# provides for exactly this case.
#
# A SET plus a method constant rather than a driver method, because the two calls
# have the same contract: ``ResumeSessionRequest`` carries the same fields as
# ``LoadSessionRequest`` (session id, cwd, MCP servers, additional directories,
# ``_meta``), and ``ResumeSessionResponse`` carries the same fields as
# ``LoadSessionResponse`` (optional modes, optional config options, ``_meta``). Only
# two reads differ -- which capability advertises the verb, and which verb is sent --
# so a per-harness method would be a function whose one difference is a string, and
# harness-parity H13 keeps the shared restore path free of a conditional added in
# service of one adapter.
#
# Membership decides BOTH reads together on purpose. A harness that advertises the
# capability under one key and serves the other verb does not exist, and splitting
# them into two sets would invite an entry in one and not the other -- which reads as
# "cannot restore" and silently starts every reopened session fresh.
#
# The REJECTED alternative, named so it is not proposed as a simplification: sniffing
# the handshake instead of declaring membership -- read ``sessionCapabilities.resume``,
# and if it is there send ``session/resume``. It looks like it removes this set, and it
# removes the DECISION instead. A harness may advertise a capability it serves badly,
# or advertise both and mean one; sniffing hands the choice to whatever the agent said
# on the day, with no record of what Crew verified and no place to write down why. The
# set is the record, and adding a harness to it is a deliberate edit with a capture
# behind it (harness-parity H6, the same reason every capability here is opt-in).
ACP_BACKENDS_RESUME_WITHOUT_LOAD = frozenset({ACP_BACKEND_DEEPSEEK})


# ── How a harness is made to ask ──
# Kiro Crew's PreToolUse gate -- the bundled denied-command rules, the
# sensitive-path block, the governance ceiling -- runs from exactly ONE place,
# ``HookManager.on_tool_call``, reached only from the permission-request branch of
# the dispatch parser. A harness that does not send ``session/request_permission``
# per tool call is a harness where none of those controls execute. So "how is this
# one made to ask?" is a security property, not a compatibility note, and it is
# named here rather than assumed at each call site.


class Routing(str, Enum):
    """The mechanism that makes a harness ask before it runs a tool.

    ``AGENT_SPEC`` -- the spawn names an agent, so the harness asks by
    construction and there is nothing to probe or apply.

    ``SESSION_CONFIG`` -- the ACP v1 session advertises a config option whose
    enforced value makes privileged tools ask. Kiro Crew verifies the option is
    advertised and applies it before the first prompt.

    ``SEEDED_SETTINGS`` -- the harness is made to ask by a settings file Kiro
    Crew writes, so the precondition would be confirmable by reading back what was
    written. **Declared but not enforced by this core**, because that read-back
    does not exist. ``AcpClient._write_claude_local_settings`` does seed
    ``permissions.defaultMode``, but it is a CONDITIONAL write: it touches only the
    file Crew owns (created this session, bytes still Crew's) and otherwise leaves
    the path alone, and nothing confirms the adapter honoured the mode afterwards.
    So a ``bypassPermissions`` already present in a user's own
    ``settings.local.json`` or ``~/.claude`` is neither detected nor stripped, and
    the guarantee cannot be asserted. Recorded as a known gap rather than papered
    over with a ``ROUTED`` this core cannot earn -- see
    ``docs/system-specs/modules/harness-onboarding.md``.

    ``VERIFIED_SEEDED_SETTINGS`` -- the harness is made to ask by a setting Kiro
    Crew supplies as the session starts, AND the precondition is confirmed by
    reading the harness's OWN RESOLVED configuration back before the first prompt.
    The read-back is the whole difference from ``SEEDED_SETTINGS``, and it is what
    this mechanism can assert that the other cannot: a value the harness's own
    config already carried, or a seed that did not take effect, is OBSERVED rather
    than assumed. Where the read-back cannot confirm the required value the verdict
    is INDETERMINATE and the session is refused, so a harness whose own default is
    permissive cannot present a gate that gates nothing.

    Where the setting TRAVELS is the driver's business, not this vocabulary's, and
    it is not necessarily a file: the one member today passes it as inline config in
    the child's environment, which that harness resolves above its own project
    config file -- so a session establishes the guarantee without writing anything
    into a checked-out repository.

    ``VERIFIED_GATE_EXTENSION`` -- the harness has NO gate of its own to configure,
    so Kiro Crew loads one INTO it: an extension Crew ships, passed on the
    harness's own command line at spawn, that intercepts every tool call and
    raises the harness's confirm dialog, which the adapter forwards as
    ``session/request_permission``. The precondition -- that the harness loaded
    Crew's extension, from Crew's file -- is VERIFIED before the first prompt by
    asking the harness's own command registry: the extension registers a probe
    command, and the read-back requires that command to be present and to name
    the shipped file as its source. A harness that did not load it, or loaded a
    different file under the same name, is refused. Like the member above, this
    proves the precondition and not the per-call emission; the frame corpus
    carries the observation of the latter.

    The extension is Crew's own code executing inside a third-party process with
    that process's permissions. That is a new trust boundary and it is stated
    here rather than assumed: the file is read-only package data, the harness is
    told its absolute path, and nothing the agent says can change which file is
    named.

    ``UNVERIFIED`` -- Kiro Crew has NOT established how, or whether, this harness
    can be made to ask. This member exists so "we do not know" is a state a
    caller must handle rather than an absent case that falls through to a
    permissive branch. It always resolves INDETERMINATE, which refuses.
    """

    AGENT_SPEC = "agent_spec"
    SESSION_CONFIG = "session_config"
    SEEDED_SETTINGS = "seeded_settings"
    VERIFIED_SEEDED_SETTINGS = "verified_seeded_settings"
    VERIFIED_GATE_EXTENSION = "verified_gate_extension"
    UNVERIFIED = "unverified"


#: Harness id -> its routing mechanism.
#:
#: A ``.get(backend, Routing.UNVERIFIED)`` read is deliberate: an id this table
#: does not name fails closed rather than inheriting a neighbour's mechanism.
ACP_BACKEND_ROUTING: dict = {
    ACP_BACKEND_KIRO: Routing.AGENT_SPEC,
    ACP_BACKEND_KAS: Routing.AGENT_SPEC,
    ACP_BACKEND_CLAUDE: Routing.SEEDED_SETTINGS,
    ACP_BACKEND_CODEX: Routing.SESSION_CONFIG,
    ACP_BACKEND_OPENCODE: Routing.VERIFIED_SEEDED_SETTINGS,
    ACP_BACKEND_PI: Routing.VERIFIED_GATE_EXTENSION,
    ACP_BACKEND_GOOSE: Routing.VERIFIED_SEEDED_SETTINGS,
    # deepseek is ``UNVERIFIED`` on OBSERVATION, not for want of looking, and the
    # distinction matters because its approval setting looks like a seed-and-verify
    # case and is not one. Its ACP surface does advertise
    # ``session/request_permission``, and its composition does carry an approval
    # service whose policy Crew can pin and read back. What the wire shows is that
    # neither decides a tool call: the sandbox permits an in-policy action silently
    # and DENIES an out-of-policy one with the denial in the tool result, and the
    # permission request carries only a MODEL-INITIATED ask to escalate past that
    # sandbox -- refused outright when the model omits its justification. Four live
    # captures across the confined and read-only postures raised no permission
    # request at all. So a ``VERIFIED_SEEDED_SETTINGS`` entry here would read back a
    # setting that governs escalations and assert a routing guarantee nothing
    # performs, which is the one thing this table exists to prevent.
    ACP_BACKEND_DEEPSEEK: Routing.UNVERIFIED,
}


#: Harness id -> the ``(option_id, required_value)`` its SESSION_CONFIG routing
#: needs, as advertised by ``session/new`` and applied through
#: ``session/set_config_option``.
#:
#: codex-acp's default ``agent`` mode permits writes inside the workspace without
#: asking. Its ACP v1 ``mode`` selector is the enforceable boundary: ``read-only``
#: still permits passive READS -- ACP v1 has no way to require a prompt for those
#: -- but commands and changes request approval. That residual read gap does not
#: close and this option cannot close it; what makes it survivable is the
#: OS-boundary mask in ``acp_tool_gate.adapter_hidden_credential_dirs``, which
#: denies the child everything on the read-gate floor except the harness's own
#: token store.
ACP_BACKEND_PERMISSION_CONFIG: dict = {
    ACP_BACKEND_CODEX: ("mode", "read-only"),
}


#: Harness id -> the ``(setting_key, required_value)`` its
#: ``VERIFIED_SEEDED_SETTINGS`` routing needs in the configuration Crew supplies at
#: session start and then reads back out of the harness.
#:
#: OpenCode asks per tool call only while its ``permission`` setting is ``ask``. Its
#: own default is permissive, so a session that supplied nothing would never ask and
#: the PreToolUse gate would run for nothing -- which is why the required value is
#: data here rather than a literal at the seeding site: the same pair names what is
#: supplied, what is read back, and what the refusal reports.
# goose asks per tool call only in its ``approve`` mode, and its own default is
# ``auto``, which auto-approves -- so the same reasoning puts the required value here
# as data. The KEY is an environment variable rather than a config field because that
# is where goose resolves this setting from, and it resolves it ABOVE its own config
# file: an operator's ``GOOSE_MODE: auto`` cannot defeat the seed, and the resulting
# mode is reported in the ``session/new`` result itself.
#: deepseek has no entry, and it is the case that shows why membership here is not
#: the same question as "does this harness have a permission setting". It has one,
#: Crew can pin it, and a read-back can confirm it -- and it would still be a setting
#: about model-initiated escalations rather than about tool calls, so naming it here
#: would produce a ROUTED verdict for a harness that never asks. Its routing is
#: ``UNVERIFIED`` instead.
ACP_BACKEND_PERMISSION_SETTING: dict = {
    ACP_BACKEND_OPENCODE: ("permission", "ask"),
    ACP_BACKEND_GOOSE: ("GOOSE_MODE", "approve"),
}


#: Harness id -> the probe command its ``VERIFIED_GATE_EXTENSION`` routing reads
#: back out of the harness's command registry before the first prompt.
#:
#: The name is data here for the same reason the seeded setting is: it is what the
#: extension registers, what the read-back looks for, and what the refusal names,
#: and three sites spelling it independently would drift. The extension file itself
#: is package data resolved by the driver, not by this leaf.
ACP_BACKEND_GATE_PROBE_COMMAND: dict = {
    ACP_BACKEND_PI: "kiro-crew-gate",
}


@dataclass(frozen=True)
class SelfServedLaunch:
    """The launch facts of one harness that serves ACP from its own binary.

    ``binary`` is the name searched for on PATH, ``acp_args`` is what follows it on
    the argv, ``bin_env_var`` is the operator override read before the search,
    ``install_command`` is what an absent verdict tells the operator to run, ``label``
    is the harness's display name, and ``protocol_version`` is the handshake dialect.

    ``missing_hint`` is the one field that is prose rather than a value, and it earns
    its place: what an operator would OTHERWISE try to install differs per harness (an
    npm adapter that does not exist; a plugin package that is not the host), and that
    sentence is what stops the wrong install. It is appended to the shared not-found
    message rather than replacing it.

    Frozen because the table is module state every spawn reads: a mutation from one
    session would follow every session after it.
    """

    label: str
    binary: str
    acp_args: tuple
    bin_env_var: str
    install_command: str
    protocol_version: int
    missing_hint: str

    @property
    def spawn_label(self) -> str:
        """What the spawn is logged under: the binary name and its own args."""
        return " ".join((self.binary, *self.acp_args)).strip()


#: Harness id -> the fixed facts of LAUNCHING it, for the harnesses whose own binary
#: serves ACP.
#:
#: One row replaces what was a separate site per harness in six places: the binary
#: name, the argv tail, the override variable, the install command, the protocol
#: version and its row in the version table (all ``acp/client.py``), plus the three
#: resolver seams in ``agent_sdk/drivers/acp.py``, the install probe in
#: ``agent_sdk/backend_install.py`` and the display label in
#: ``agent_sdk/tool_gate.py``. Every field is the SAME KIND of thing for all members;
#: a harness whose launch needs a decision rather than a value is not one.
#:
#: It lives in this leaf, beside the routing and permission tables, because H8 keeps
#: harness vocabulary in one module and because the install probe reads it without
#: importing ``kiro_crew.acp``.
#:
#: Membership is deliberately narrow. claude-agent-acp, codex-acp and pi-acp are Node
#: adapters resolved through a different ladder -- ``node_modules`` rungs, a vendored
#: entry, and two independently-absent components for pi -- and kiro-cli's argv
#: carries the agent spec, so none of the four is a member and none of their spawn
#: arms reads this table. What a member's arm still owns for itself is its ROUTING:
#: opencode's config read-back, goose's mode seed and deepseek's absence of either
#: are not launch facts and are not here.
ACP_BACKEND_LAUNCH: Mapping[str, SelfServedLaunch] = {
    ACP_BACKEND_OPENCODE: SelfServedLaunch(
        label="OpenCode",
        binary="opencode",
        acp_args=("acp",),
        bin_env_var="OPENCODE_BIN",
        install_command="npm i -g opencode-ai",
        protocol_version=1,
        missing_hint="No adapter package is needed: this harness serves ACP itself.",
    ),
    ACP_BACKEND_GOOSE: SelfServedLaunch(
        label="goose",
        binary="goose",
        acp_args=("acp",),
        bin_env_var="GOOSE_BIN",
        install_command=(
            "curl -fsSL https://raw.githubusercontent.com/block/goose/main/"
            "download_cli.sh | bash"
        ),
        protocol_version=1,
        missing_hint="No adapter package is needed: this harness serves ACP itself.",
    ),
    ACP_BACKEND_DEEPSEEK: SelfServedLaunch(
        label="DeepSeek Harness",
        binary="dsh",
        acp_args=("--profile", "acp"),
        bin_env_var="DSH_BIN",
        install_command="npm i -g @deepseek-ai/dsh",
        protocol_version=1,
        missing_hint=(
            "The ACP plugin package alone does not serve ACP: it is a plugin, and "
            "this binary is the host that boots the profile it lives in."
        ),
    ),
}

#: The harnesses whose whole launch is described by :data:`ACP_BACKEND_LAUNCH`.
#:
#: A driver-internal membership, not a consumer-facing capability: it answers "is this
#: harness's argv a value the table already holds?", which only the spawn path, the
#: install probe and the driver seams ask. Derived from the table's keys rather than
#: written a second time, so the two cannot disagree.
ACP_BACKENDS_SELF_SERVED_ACP: FrozenSet[str] = frozenset(ACP_BACKEND_LAUNCH)

#: The argv0 basename each harness's child process runs as.
#:
#: Read by the PID-file reclaim in :mod:`kiro_crew.session_pid`, which asks one
#: question of a tracked PID it is about to signal: does this PID still name the kind
#: of process the tracking entry described? That is a recycle guard, and it needs a
#: name per harness rather than a capability.
#:
#: Declared HERE rather than in the reclaim, for the reason every per-backend fact is
#: declared here: a harness added to :data:`ACP_BACKENDS_KNOWN` and not to this table
#: is a harness whose orphans the reclaim cannot recognise, and the reclaim's failure
#: mode for an unrecognised orphan is to drop its tracking entry and spare the
#: process — so nothing later can find it. ``test_pid_lifecycle`` ratchets the
#: coverage, so the omission is a red test rather than a leaked process.
#:
#: The three self-served harnesses read their own ``ACP_BACKEND_LAUNCH`` row so the
#: two tables cannot disagree. The rest are spelled out because their launch is
#: bespoke: kiro-cli serves both the kiro and KAS backends (KAS is kiro-cli's relay),
#: and the claude, codex and pi adapters are Node entry scripts whose basenames live
#: with their resolvers in the ACP layer, which this module must not import.
ACP_BACKEND_PROCESS_NAMES: Mapping[str, str] = {
    ACP_BACKEND_KIRO: "kiro-cli",
    ACP_BACKEND_KAS: "kiro-cli",
    ACP_BACKEND_CLAUDE: "claude-agent-acp",
    ACP_BACKEND_CODEX: "codex-acp",
    ACP_BACKEND_PI: "pi-acp",
    **{backend: record.binary for backend, record in sorted(ACP_BACKEND_LAUNCH.items())},
}


#: The npm package that ships each NODE-HOSTED adapter.
#:
#: Three harnesses are entry scripts Crew hands to ``node``; every other harness is a
#: binary that serves ACP itself. Only these three can appear in a command line as
#: ``node <path>``, so only these three need a path to be recognised by.
#:
#: Declared here for the same reason :data:`ACP_BACKEND_PROCESS_NAMES` is: the resolvers
#: live in the ACP layer, which this module must not import, while the reclaim in
#: :mod:`kiro_crew.session_pid` must not import the ACP layer either. One table both can
#: read is what keeps the resolver's spelling and the reclaim's from drifting -- and a
#: drift there is a launch the reclaim cannot recognise, or a path it recognises that
#: Crew never spawns.
#:
#: ``pi-acp`` is unscoped and the other two are scoped: the values are the real published
#: names, not a pattern, because a pattern is what would let an unrelated package satisfy
#: it.
ACP_BACKEND_NODE_ADAPTER_PACKAGES: Mapping[str, str] = {
    ACP_BACKEND_CLAUDE: "@agentclientprotocol/claude-agent-acp",
    ACP_BACKEND_CODEX: "@agentclientprotocol/codex-acp",
    ACP_BACKEND_PI: "pi-acp",
}

#: The entry script inside such a package, as every resolver builds it.
NODE_ADAPTER_ENTRY_SEGMENTS: tuple[str, ...] = ("dist", "index.js")


def node_adapter_entry_relpaths() -> tuple[str, ...]:
    """Every ``<package>/dist/index.js`` tail Crew launches a Node adapter with.

    The IDENTITY of an interpreter-hosted adapter, for a consumer that has a command
    line and must decide whether Crew spawned it. Matching a package DIRECTORY NAME
    against the harness names instead makes "what a process may call itself" an open
    axis: an unrelated npm application at ``node /srv/goose/dist/index.js`` carries a
    directory named after a harness Crew never launches through Node at all. These
    relative paths are a closed set this repository owns, so the axis closes with it.

    Returned as POSIX-separated relative paths, sorted for a stable value.
    """
    return tuple(
        sorted(
            "/".join((package, *NODE_ADAPTER_ENTRY_SEGMENTS))
            for package in ACP_BACKEND_NODE_ADAPTER_PACKAGES.values()
        )
    )


def agent_process_markers() -> tuple[str, ...]:
    """Every harness argv0 basename, sorted and de-duplicated.

    A tuple of substrings for a cmdline match, which is what
    ``platform_compat.process_matches`` takes. Sorted so the value is stable to read
    in a log, de-duplicated because kiro and KAS share ``kiro-cli``.
    """
    return tuple(sorted(set(ACP_BACKEND_PROCESS_NAMES.values())))


def launch_for(backend: str) -> SelfServedLaunch:
    """The launch record for *backend*, raising ``KeyError`` when it has none.

    Raising rather than answering a default is the point: a caller that reaches here
    for a Node adapter or for kiro-cli has taken the wrong arm, and a stand-in record
    would spawn the wrong binary instead of saying so.
    """
    return ACP_BACKEND_LAUNCH[backend]


def routing_for(backend: str) -> "Routing":
    """The routing mechanism for *backend*, failing closed on an unknown id."""
    return ACP_BACKEND_ROUTING.get(backend, Routing.UNVERIFIED)


def permission_config_for(backend: str) -> tuple:
    """The ``(option_id, value)`` *backend* needs, or ``("", "")`` when it needs none."""
    return ACP_BACKEND_PERMISSION_CONFIG.get(backend, ("", ""))


def permission_setting_for(backend: str) -> tuple:
    """The ``(setting_key, value)`` *backend* seeds, or ``("", "")`` when it seeds none."""
    return ACP_BACKEND_PERMISSION_SETTING.get(backend, ("", ""))


def gate_probe_command_for(backend: str) -> str:
    """The probe command *backend*'s gate extension registers, or ``""`` when none."""
    return ACP_BACKEND_GATE_PROBE_COMMAND.get(backend, "")
