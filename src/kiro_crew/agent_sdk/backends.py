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
   * - ``ACP_BACKENDS_INDEPENDENT_SETUP``
     - pre-session registry query (which first-run harnesses can complete setup
       without Kiro CLI before any session exists)
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
   * - ``ACP_BACKENDS_MEMBER_TOOL_LIMITS``
     - pre-session registry query (whether a saved spec's per-tool ``disabledTools``
       is honoured, or the spec is refused before spawn)
   * - ``ACP_BACKENDS_MATE_WORKER_KIT``
     - pre-session registry query (whether a crewmate's worker runs with its kit,
       or is refused before spawn)
   * - ``ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT``
     - context assembly (whether a crewmate worker's principles and its crewmate's
       prompt ride the ``[AGENT SYSTEM PROMPT]`` block)
   * - ``ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS``
     - pre-session registry query (whether a crewmate worker's single-tool limits
       are honoured, or a worker with limits is refused before spawn)
   * - ``ACP_BACKENDS_MEMBER_DISPATCH``
     - driver-internal (whether a per-session tool set can be mounted)
   * - ``ACP_BACKENDS_MEMBER_PANEL``
     - driver-internal (whether a member's own webview can be mounted)
   * - ``ACP_BACKENDS_STEER``
     - pre-session registry query (whether ``_session/steer`` exists)
   * - ``ACP_BACKENDS_STEERING_REQUEST``
     - pre-session registry query (whether a user steer travels on an awaited
       request instead: the ``_session/steering`` request of codex-acp and
       claude-agent-acp, or goose's ``_goose/unstable/session/steer``)
   * - ``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``
     - pre-session registry query (whether a user steer travels as a second
       ``session/prompt`` that joins the running turn)
   * - ``ACP_BACKENDS_STEER_RETRACT``
     - driver-internal (whether a pending steer can be taken back with
       ``_session/steer/clear``)
   * - ``ACP_BACKENDS_COMPACT``
     - pre-session registry query (whether manual ``/compact`` is offered at all)
   * - ``ACP_BACKENDS_INLINE_COMPACTION``
     - semantic question (``SessionCapabilities.compacts_inline``)
   * - ``ACP_BACKENDS_UNMARKED_COMPACTION_PAIR``
     - frame translation (whether a compaction pair with no marker is read)
   * - ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION``
     - semantic question (whether a skipped autocompact is answered by the harness)
   * - ``ACP_BACKENDS_CONTEXT_RECYCLE``
     - semantic question (whether a full context is answered by recycling)
   * - ``ACP_BACKENDS_INTERNAL_SANDBOX``
     - driver-internal (whether Crew's seatbelt is skipped at spawn)
   * - ``ACP_BACKENDS_RELAY_INTERNAL_SANDBOX``
     - driver-internal (whether Crew's seatbelt is skipped on the kiro-cli relay route)
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
   * - ``ACP_BACKENDS_MODEL_AT_LAUNCH``
     - driver-internal (whether the model is a launch argument, so the startup
       apply sends nothing and a mid-session switch is refused)
   * - ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION``
     - semantic question (``SessionCapabilities.effort_via_config_option``)
   * - ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS``
     - driver-internal (whether an advertised ``<model>[<effort>]`` id is applied
       as two config-option writes)
   * - ``effort_config_option_id``
     - driver-internal (which ``configId`` carries the reasoning effort)
   * - ``effort_config_option_value``
     - driver-internal (which VALUE that option spells a Crew effort level with)
   * - ``effort_config_option_levels``
     - driver-internal (which advertised values are levels, not "no override")
   * - ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION``
     - driver-internal (whether the ADVERTISED option, rather than Crew's model
       registry, answers that this session takes an effort level and which ones)
   * - ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``
     - semantic question (``SessionCapabilities.resolves_model_from_advertised_list``)
   * - ``ACP_BACKENDS_SEED_LOCAL_SETTINGS``
     - driver-internal (whether ``settings.local.json`` is re-seeded on switch)
   * - ``ACP_BACKENDS_AGENT_STARTED_TURNS``
     - driver-internal (whether ``AcpClient`` reads stdout between turns and hands a
       turn the agent started on its own to its ``on_agent_turn`` callback)
   * - ``ACP_BACKENDS_KIRO_SLASH_COMMANDS``
     - semantic question (``SessionCapabilities.effort_via_slash_command``), and
       driver-internal everywhere else (whether ``_kiro.dev/commands/execute`` exists)
   * - ``ACP_BACKENDS_KIRO_FAMILY``
     - driver-internal (whether the effort live path and the overlay gate take
       the kiro-cli / KAS branch), and the card's kiro-family marker
   * - ``ACP_BACKENDS_NATIVE_TODOS``
     - semantic question (``SessionCapabilities.supports_native_todos``)
   * - ``ACP_BACKENDS_LOCAL_CLEAR``
     - pre-session registry query (whether the dashboard answers ``/clear``
       itself rather than handing it to the harness as prompt text)
   * - ``ACP_BACKENDS_LOCAL_HARNESS_COMMANDS``
     - pre-session registry query (whether the dashboard answers every harness
       command itself, and which commands the ``/`` menu lists)
   * - ``ACP_BACKENDS_EFFORT_OVERLAY``
     - driver-internal (whether the workspace ``cli.json`` effort keys are written,
       read back at construction and cleared)
   * - ``ACP_BACKENDS_TOOL_SEARCH_OVERLAY``
     - driver-internal (whether the workspace ``cli.json`` Tool Search keys are written)
   * - ``ACP_BACKENDS_CLIENT_META_SETTINGS``
     - driver-internal (whether ``initialize`` carries ``_meta.kiro.settings``)
   * - ``ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD``
     - pre-session registry query (whether the dashboard may skip a session reset)
   * - ``ACP_BACKENDS_MCP_WIRE_REINJECT``
     - pre-session registry query (whether the dashboard may skip a session reset
       because Crew re-sends the projection at the next turn boundary)
   * - ``ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN``
     - pre-session registry query (whether a per-template MCP addition reaches a
       session of a template Crew does not own)
   * - ``ACP_BACKENDS_SPEC_FILE_GRANTS``
     - pre-session registry query (whether a start on a crew-bound spec Crew does
       not own is refused while the file's grants exceed the ceiling)
   * - ``ACP_BACKENDS_STRUCTURED_REFUSAL``
     - driver-internal (whether the metadata refusal parser is consulted)
   * - ``ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR``
     - driver-internal (whether an adapter-authored text chunk that ends a turn
       is the turn's provider failure rather than an answer)
   * - ``ACP_BACKENDS_REPORT_USAGE``
     - semantic question (whether a turn that reports no usage billed nothing,
       read through the provider's ``reports_turn_usage``)
   * - ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL``
     - driver-internal (whether a kindless permission frame takes the preceding
       tool_call's ``kind``; read per frame by the session handle's wire accessor)
   * - ``ACP_BACKENDS_HOOKS_LIST``
     - driver-internal (whether this harness's agent asks its client for the hooks
       matching a trigger and to run one, read by the session dispatch loop that
       answers the three hook methods; no consumer above the boundary asks it)
   * - ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``
     - semantic question (``SessionCapabilities.crew_fires_spec_hooks``)
   * - ``ACP_BACKENDS_HOST_AUTH_CALLBACK``
     - driver-internal (whether the reader loop may answer the engine's
       ``_kiro/auth/getAccessToken`` from Crew's own vault)
   * - ``ACP_BACKENDS_OPEN_EXTERNAL_URL``
     - driver-internal (whether the reader loop answers ``_kiro/openExternalUrl``
       and a session starts MCP sign-ins with ``_kiro/mcp/resetServer``)
   * - ``ACP_BACKENDS_SIDE_READONLY``
     - pre-session registry query (whether a side-chat turn may execute
       read-only tools under the derived ``<agent>--readonly`` spec; asked
       about the configured backend id before the side session is created)
   * - ``ACP_BACKENDS_SERIAL_SESSION_STARTS``
     - driver-internal (whether a timed-out session start names the unanswered
       starts and mode switches it was sent behind on the same process)
   * - ``ACP_BACKENDS_HARNESS_OWNED_SESSIONS``
     - driver-internal (whether ``session/load`` is gated on a Crew-side transcript)
   * - ``ACP_BACKENDS_LOAD_WITHOUT_MODES``
     - driver-internal (whether a successful session-restore result carries no
       ``modes`` block)
   * - ``ACP_BACKENDS_RESUME_WITHOUT_LOAD``
     - driver-internal (which ACP verb restores a session, and which capability
       key advertises it)
   * - ``ACP_BACKENDS_FORK_AT_MESSAGE``
     - driver-internal (whether ``session/fork`` honours a fork point, so a
       rewind keeps the retained native history)
   * - ``ACP_BACKENDS_RELOAD_DISPOSED_SESSION``
     - driver-internal (whether a session whose backend the harness disposed in a
       live process is answered as cancelled or failed once, then reloaded in a
       fresh process before the next prompt)
   * - ``ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY``
     - driver-internal (whether the session's project checkout scopes the broker
       overlay lookup, read only through :func:`overlay_project_scope` while
       ``kiro_crew.acp`` composes the ``session/new`` MCP array). Deliberately not
       a semantic question: it describes where a HOST reads agent specs from, and
       no consumer above the boundary asks it -- what a consumer would ask about
       is the resulting server list, which it already receives
   * - ``ACP_BACKENDS_HONOR_ZERO_TOOL_BAN``
     - semantic question (``SessionCapabilities.honors_zero_tool_ban``)
   * - ``ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP``
     - driver-internal (whether a ``session/new`` stdio element whose command
       cannot be found is left out and reported as failed before the request)
   * - ``ACP_BACKENDS_ACP_CLIENT_SPAWNABLE``
     - semantic question (``SessionCapabilities.acp_client_spawnable``)

The two non-set tables ``SessionCapabilities`` also translates are
:func:`model_registry_namespace` (the model-id namespace) and
:func:`kiro_crew.agent_sdk.backend_identity.is_claude_backend_name` (the provider
seam). Both already existed; neither gained a member here.
"""

from __future__ import annotations

import logging
import threading
from typing import FrozenSet, Set

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
# GitHub Copilot CLI: a single binary that serves ACP itself (``copilot --acp
# --stdio``), so its install probe names ONE component, like opencode and goose. Its
# permission route is an ACP v1 session config option (``allow_all``), the shape
# codex has -- see :data:`Routing.SESSION_CONFIG`.
#
# VERIFIED RANGE: Copilot CLI 1.0.100 is the recorded binary
# (``test/fixtures/acp_frames/copilot/``). Its ``allow_all`` option and its
# ``session/request_permission`` per tool call are wire facts of that release.
ACP_BACKEND_COPILOT = "copilot"
# Strands: the official Strands CLI (``@strands-agents/cli``) serving a Strands Python
# project over ACP (``strands --agent <project> --acp-server``). One component to
# install, a Node floor (the CLI's own ``engines.node``) and a Python environment for
# the project. In that mode the CLI sends ``session/request_permission`` only for a
# Strands interrupt, so Crew loads the project through its own gate module, which
# raises one for every tool call -- see :data:`Routing.VERIFIED_GATE_EXTENSION`.
#
# VERIFIED RANGE: @strands-agents/cli 0.2.3 over strands-harness 0.3.3 /
# strands-agents 1.63.0 (``test/fixtures/acp_frames/strands/``).
ACP_BACKEND_STRANDS = "strands"
# The kiro-cli backend is spelled as the empty string throughout, so name it
# rather than leaving every call site to infer it from "not claude".
ACP_BACKEND_KIRO = ""

# How config.json spells a Kiro CLI choice. The runtime id stays ``""``; only the
# config boundary (:func:`backend_from_stored` / :func:`backend_to_stored`)
# translates, so ``agent.acp_backend`` absent or ``""`` can mean "no choice" while
# a stored ``"kiro"`` means the user picked Kiro CLI. Same word as the governance
# policy id (``POLICY_ID_KIRO``), so one name for Kiro CLI reaches every file a
# person edits.
STORED_BACKEND_KIRO = "kiro"

# The harness an install runs when ``agent.acp_backend`` holds no choice (absent
# or ``""``) and KAS is selectable.
DEFAULT_ACP_BACKEND = ACP_BACKEND_KAS

# The harness an explicit Kiro CLI choice runs on once this host's kiro-cli is known
# to offer its v3 (KAS) engine. kiro-cli serves ``acp`` on v3 by default and is
# retiring v2, and a bare ``kiro-cli acp`` on v3 reads none of what Crew writes for
# the v2 harness (the per-spawn skill-view aliases, the ``cli.json`` effort overlay,
# the re-filtered agent specs). So :func:`resolve_selected_backend` hands the pick
# to Crew's KAS path, which drives that engine with KAS's launch, projection,
# permissions and compaction -- but only after the gateway's ``acp --help`` probe
# has CONFIRMED v3 (:func:`confirm_kiro_cli_offers_kas`) and while KAS is
# selectable. Without that evidence (another process, an unknown verdict, the
# bundle route, a policy that denies KAS) the pick keeps the v2 launch. The stored
# value is never rewritten: ``config.json`` still says ``"kiro"`` until the user
# saves KAS (the a-kiro-cli-choice-runs-on-kas entry in docs/decisions).
KIRO_SELECTION_BACKEND = ACP_BACKEND_KAS

# Membership gate for the ``acp_backend`` kwarg. An unrecognized value would
# otherwise fall through every ``_is_<backend>`` check and silently spawn
# kiro-cli, so provider construction rejects it instead.
ACP_BACKENDS_KNOWN: FrozenSet[str] = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_STRANDS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
    }
)

# First-run setup can complete without Kiro CLI only for harnesses explicitly
# known to launch independently. New adapters opt in after their install and
# sandbox path is verified; a KAS-like backend cannot inherit the bypass merely
# because its id is neither Kiro nor KAS.
ACP_BACKENDS_INDEPENDENT_SETUP: FrozenSet[str] = frozenset(
    {
        ACP_BACKEND_STRANDS,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
    }
)

# ── The selectable registry ──

#: What the public edition ships.
#:
#: ``ACP_BACKEND_CLAUDE`` is included because the public build can genuinely serve a
#: session with it: ``acp/client_start.py`` owns the whole spawn path (the ``_is_claude``
#: branch, ``_resolve_claude_acp_bin``, ``_resolve_claude_code_executable``) and the
#: adapter it needs is a PUBLIC npm package (``CLAUDE_ACP_NPM_PKG``). Nothing about it
#: is edition-private. Leaving it out would render the option as permanently
#: unavailable on exactly the builds that can run it.
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
#: * ``backend_install`` probes for the ``dsh`` binary, one component, because the
#:   harness's own release ships the executable that serves ACP from its ``acp``
#:   profile.
#: * its tool calls are ROUTED and the routing is VERIFIED. Its sandbox decides an
#:   action itself and its own ``session/request_permission`` carries only a
#:   model-initiated escalation -- so
#:   the gate is not a setting Crew can seed but a PLUGIN Crew composes, through the
#:   per-launch patch its own launcher documents. The plugin answers the harness's
#:   ``tools/pre-execute`` waterfall with ``ask``, which its tools core resolves
#:   through ``ctx.approval`` and its ACP bridge answers by emitting
#:   ``session/request_permission`` per call. The read-back is the gate's own load
#:   marker, keyed to this session's nonce and to Crew's sealed file, read before the
#:   first prompt. See :data:`Routing.VERIFIED_GATE_EXTENSION` and
#:   :data:`Readback.LOAD_MARKER`.
#:
#: ``ACP_BACKEND_COPILOT`` is included on the same two conditions, met by Codex's
#: mechanism:
#:
#: * ``backend_install`` probes for the ``copilot`` binary, one component, because
#:   the harness's own package ships the executable that serves ACP.
#: * its tool calls are ROUTED. Copilot CLI advertises an ``allow_all`` session
#:   config option on every ``session/new`` and ``session/load``; Crew verifies the
#:   ``off`` value is advertised and applies it before the first prompt, refusing
#:   the session otherwise, so a user's own allow-all default cannot reach a Crew
#:   session. See :data:`Routing.SESSION_CONFIG`.
#:
#: ``ACP_BACKEND_STRANDS`` is included on the same two conditions, met by pi's and
#: deepseek's mechanism:
#:
#: * ``backend_install`` probes for the ``strands`` binary, its Node floor and the
#:   Python environment the project runs in, and names whichever is absent.
#: * its tool calls are ROUTED and the routing is VERIFIED. The CLI's Python-project
#:   ACP mode asks only when a Strands interrupt is raised, so Crew starts it on its
#:   own sealed gate module, which attaches an interrupt hook to every Strands
#:   ``Agent`` the project builds, and a read-back boot proves the gate loaded from
#:   Crew's file under this session's nonce before the session starts. See
#:   :data:`Routing.VERIFIED_GATE_EXTENSION` and :data:`Readback.LOAD_MARKER`.
BASELINE_SELECTABLE_BACKENDS: FrozenSet[str] = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_STRANDS,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
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
    ACP_BACKEND_STRANDS: ACP_BACKEND_STRANDS,
    ACP_BACKEND_CLAUDE: ACP_BACKEND_CLAUDE,
    # Every known id needs an entry: a policy author has to be able to name — and so
    # to deny — any id this build can spell, and the mapping is what makes the id
    # nameable in a rule at all.
    ACP_BACKEND_CODEX: ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE: ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI: ACP_BACKEND_PI,
    ACP_BACKEND_GOOSE: ACP_BACKEND_GOOSE,
    ACP_BACKEND_DEEPSEEK: ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_COPILOT: ACP_BACKEND_COPILOT,
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
# Keeping them apart is what makes the policy re-appliable in BOTH directions. With
# one set and a destructive ``deny_selectable_backend``, a ceiling installed at
# runtime (``policy_distribution.apply_ceiling`` replaces
# ``current_context().governance`` mid-process) could never be re-evaluated, so a
# TIGHTENED fleet policy would stay inert until every gateway restarted and a
# LOOSENED one could not restore what an earlier application had already deleted.
# Recomputing ``baseline - denied`` has neither failure: it is idempotent,
# order-independent, and reversible.
_baseline: Set[str] = set(BASELINE_SELECTABLE_BACKENDS)
_selectable: Set[str] = set(BASELINE_SELECTABLE_BACKENDS)

# Backends THIS HOST cannot serve, keyed to the reason, e.g. KAS on a kiro-cli whose
# ``acp`` subcommand cannot select the KAS engine. A third input beside the two sets
# above: it is a fact about the machine, not the build or the policy, so every policy
# recompute subtracts it again rather than restoring what the host cannot run.
_host_unservable: dict[str, str] = {}

# Whether THIS process has positive evidence that the host's kiro-cli offers its v3
# (KAS) engine: set by the gateway's KAS floor when ``acp --help`` ran and names the
# engine. The one condition under which a Kiro CLI pick runs on
# :data:`KIRO_SELECTION_BACKEND`; an unknown verdict leaves it unset.
_kiro_cli_offers_kas: bool = False

# Whether :func:`ensure_kiro_cli_kas_verdict` has already run its one bounded probe
# in THIS process. The probe spawns ``kiro-cli acp --help`` once; a process that
# never got the gateway floor's verdict (the CLI, an eval run, a peer server) uses
# it to establish the same evidence before its first spawn, and a gap or unknown
# verdict is remembered here so it is not re-probed on every later config load.
_kiro_cli_kas_probed: bool = False

# Serialises :func:`ensure_kiro_cli_kas_verdict`. The CLI entries that call it are
# single-threaded (one call before the first spawn), but a peer server or an app
# could reach it from more than one thread; the lock makes the read-probe-set
# sequence atomic so the bounded spawn runs exactly once and the two module flags
# are never written by two threads at the same moment.
_kiro_cli_kas_probe_lock = threading.Lock()


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
    keep = {b for b in _baseline if b not in denied and b not in _host_unservable}
    if GOVERNANCE_FLOOR_BACKEND in _baseline:
        keep.add(GOVERNANCE_FLOOR_BACKEND)
    # Only what *denied* removed: a host mark is reported by whoever placed it, so
    # a policy recompute never logs the host's removal as its own.
    removed = frozenset(b for b in _baseline - keep if b in denied)
    _selectable.clear()
    _selectable.update(keep)
    return removed


def mark_backend_unservable(backend: str, reason: str) -> None:
    """Record that this host cannot serve *backend*, and drop it from the selectable set.

    Survives every later :func:`apply_selectable_denials` recompute, so a policy
    refresh cannot put back a harness the machine cannot start. The floor is never
    marked: it is what keeps the install startable.
    """
    if backend == GOVERNANCE_FLOOR_BACKEND:
        return
    _host_unservable[backend] = reason
    _selectable.discard(backend)


def confirm_kiro_cli_offers_kas() -> None:
    """Record that this host's kiro-cli offers its v3 (KAS) engine.

    Called by the gateway's KAS floor only on a probe that RAN and names the
    engine. From then on a Kiro CLI pick resolves to :data:`KIRO_SELECTION_BACKEND`
    while KAS is selectable.
    """
    global _kiro_cli_offers_kas
    _kiro_cli_offers_kas = True


def kiro_cli_offers_kas() -> bool:
    """Whether :func:`confirm_kiro_cli_offers_kas` ran in this process."""
    return _kiro_cli_offers_kas


def ensure_kiro_cli_kas_verdict() -> None:
    """Establish this process's Kiro-CLI-offers-v3 evidence, probing once if needed.

    The gateway's KAS floor records that evidence at boot
    (:func:`confirm_kiro_cli_offers_kas`); a process that never runs the floor --
    ``kirocrew chat``, an eval run, a peer server -- otherwise reads a stored Kiro
    CLI pick (``agent.acp_backend: "kiro"``) as the v2 kiro-cli launch even where
    kiro-cli already serves a bare ``acp`` on its v3 (KAS) engine, and once kiro-cli
    serves v3 by default that bare launch is driven as v2. So such a process reaches
    the same verdict the gateway does.

    Probes ONLY when the stored ``agent.acp_backend`` is the Kiro CLI pick and the
    bundle route is off: two cheap document reads
    (:func:`acp_backend_stored_as_kiro`, :func:`acp_backend_bundle_route`, no spawn)
    gate it, so a default / ``kas`` / ``claude`` / ``codex`` backend -- a user who
    never asked -- pays nothing and keeps its unchanged start, and a bundle-route
    pick stays on kiro-cli exactly as the gateway floor keeps it (KAS there is
    Crew's bundled engine, which kiro-cli's own v3 support says nothing about). On a
    Kiro CLI pick it runs the same one bounded ``acp --help`` probe the floor uses
    (:func:`kas_engine_verdict`) and, on POSITIVE evidence only, records it -- the
    identical promotion rule: a gap, an unknown (no pinned kiro-cli, a failed spawn)
    or an unselectable KAS leaves the pick on the v2 launch. Call it before the
    process builds its provider factory, so the config load that follows reads the
    stored pick as KAS (:func:`kiro_cli_pick_backend`).

    Idempotent and probe-once: it does nothing once evidence is already recorded
    (the gateway floor ran, or a prior call probed), and it spawns at most once per
    process even when the verdict was a gap or unknown. The read-probe-set sequence
    is under :data:`_kiro_cli_kas_probe_lock`, so two threads reaching it at once
    still probe exactly once and never race the module flags. Blocking (one bounded
    spawn) on its first call in a process whose pick is Kiro CLI; never raises.
    """
    global _kiro_cli_kas_probed
    if _kiro_cli_offers_kas or _kiro_cli_kas_probed:
        return
    with _kiro_cli_kas_probe_lock:
        # Re-read inside the lock: another thread may have probed while this one
        # waited, so the first winner's verdict stands and no second spawn runs.
        if _kiro_cli_offers_kas or _kiro_cli_kas_probed:
            return
        try:
            from kiro_crew.config.loader_document import (
                acp_backend_bundle_route,
                acp_backend_stored_as_kiro,
            )

            if not acp_backend_stored_as_kiro() or acp_backend_bundle_route():
                # Not a Kiro CLI pick, or the bundle route (where the pick stays on
                # kiro-cli, matching the gateway floor): nothing to promote. Do NOT
                # spend a spawn or mark the process probed, so a later load for a
                # different config (a peer server re-reading) can re-check cheaply.
                return
        except Exception:  # noqa: BLE001 - an unreadable document reads as no pick
            return
        _kiro_cli_kas_probed = True
        if KIRO_SELECTION_BACKEND not in selectable_backends():
            # KAS is not selectable here (a policy denies it, or this host was
            # marked unable to serve it), so the pick keeps the v2 launch
            # regardless of the probe -- do not spend the spawn.
            return
        try:
            from kiro_crew.agent_sdk import kas_engine_verdict

            offers_kas, _reason = kas_engine_verdict()
        except Exception:  # noqa: BLE001 - an unanswerable probe keeps the pick on v2
            return
        if offers_kas:
            confirm_kiro_cli_offers_kas()


def host_unservable_reason(backend: str) -> str | None:
    """Why this host cannot serve *backend*, or ``None`` when nothing was recorded."""
    return _host_unservable.get(backend)


def selectable_backend_values() -> list[str]:
    """:func:`selectable_backends` as a sorted list.

    The form every operator-facing surface wants: a stable option order in the
    dashboard and a stable ``must be one of [...]`` refusal message. Kept here so
    the PATCH allowlist and the schema endpoint share one answer instead of each
    sorting its own.
    """
    return sorted(selectable_backends())


def backend_from_stored(value: object) -> object:
    """Translate a stored ``agent.acp_backend`` value into the gate's input.

    The config-file vocabulary differs from the runtime one in exactly two ways:
    absent (``None``) and ``""`` both mean "no choice" and become ``None``, which
    :func:`resolve_selected_backend` answers with the default and no warning; and
    :data:`STORED_BACKEND_KIRO` becomes :data:`ACP_BACKEND_KIRO`. Every other
    value, including a non-string, passes through so the gate can judge it.
    """
    if value is None or value == "":
        return None
    if value == STORED_BACKEND_KIRO:
        return ACP_BACKEND_KIRO
    return value


def backend_to_stored(backend: str) -> str:
    """The config-file spelling of a runtime backend id (Kiro CLI is ``"kiro"``)."""
    return STORED_BACKEND_KIRO if backend == ACP_BACKEND_KIRO else backend


def spawn_delegates_to_internal_sandbox(backend: str, *, kiro_cli_internal_sandbox: bool) -> bool:
    """Whether this spawn's isolation is delegated to kiro-cli's internal sandbox.

    The one verdict every launch site passes to ``sandbox.wrap_argv`` as
    ``is_kiro_cli`` (harness-parity H7). A member of
    ``ACP_BACKENDS_INTERNAL_SANDBOX`` delegates on every route; a member of
    ``ACP_BACKENDS_RELAY_INTERNAL_SANDBOX`` delegates only when its plan says
    kiro-cli's sandbox confines the spawn. Both tests are positive, so a harness in
    neither set keeps Crew's layer whatever its plan says.

    Defined on this facade rather than beside the sets in ``backends_launch``: tests
    rebind ``ACP_BACKENDS_INTERNAL_SANDBOX`` here, and a function in the owner would
    read the owner's binding instead.
    """
    return backend in ACP_BACKENDS_INTERNAL_SANDBOX or (
        kiro_cli_internal_sandbox and backend in ACP_BACKENDS_RELAY_INTERNAL_SANDBOX
    )


def selectable_stored_backend_values() -> list[str]:
    """:func:`selectable_backend_values` in the config-file vocabulary, sorted.

    What a config surface offers and accepts for ``agent.acp_backend``: the
    dashboard PATCH allowlist and the schema endpoint's ``enumValues``.
    """
    return sorted(backend_to_stored(b) for b in selectable_backends())


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

    Takes RUNTIME ids. A value read from a config file goes through
    :func:`backend_from_stored` first, which turns "no choice" (absent or ``""``)
    into ``None`` and the stored ``"kiro"`` into :data:`ACP_BACKEND_KIRO`.

    :data:`ACP_BACKEND_KIRO` resolves to itself here. Moving a stored Kiro CLI pick
    onto KAS happens once, at config load (:func:`kiro_cli_pick_backend`, called by
    ``_read_backend_choice``), so a value this gate already answered -- the floor a
    degraded member backend took, say -- is never moved again by a later call.

    Only ``None`` (no choice) selects :data:`DEFAULT_ACP_BACKEND`, and only
    while it is selectable; otherwise it takes the floor, silently. An
    explicit value that is unselectable or unrecognized — a backend this build did
    not register, one policy denied, one this host cannot serve, a typo, or the
    non-string shapes a hand-edited ``config.json`` can hold — degrades to
    :data:`GOVERNANCE_FLOOR_BACKEND` with the reason in the log rather than
    propagating: ``AcpProvider`` rejects an unknown backend by raising, and startup
    refusing with a reason is the contract (harness-parity H3).

    An edition that registers a backend must do so before the first config load; the
    registry is read here, not cached, so ordering is the edition's to get right.
    """
    selectable = selectable_backends()
    if value is None:
        return (
            DEFAULT_ACP_BACKEND if DEFAULT_ACP_BACKEND in selectable else GOVERNANCE_FLOOR_BACKEND
        )
    if isinstance(value, str) and value in selectable:
        return value
    # A host mark was announced once by whoever placed it; repeating it on every
    # config load would bury that notice under copies of a vaguer one.
    if isinstance(value, str) and value in _host_unservable:
        return GOVERNANCE_FLOOR_BACKEND
    logger.warning(
        "Ignoring agent.acp_backend %r (not selectable in this build); using "
        "the Kiro CLI floor. Selectable values: %s",
        value,
        ", ".join(repr(b) for b in selectable_stored_backend_values()),
    )
    return GOVERNANCE_FLOOR_BACKEND


def kiro_cli_pick_backend() -> str:
    """The runtime id a STORED Kiro CLI pick in ``agent.acp_backend`` loads as.

    A Kiro CLI pick launches a bare ``kiro-cli acp``, which kiro-cli now serves on
    its v3 (KAS) engine by default, and only Crew's KAS path drives that engine. The
    pick moves there only on positive evidence (:func:`confirm_kiro_cli_offers_kas`)
    and while KAS is selectable; otherwise it keeps the v2 launch. See
    :data:`KIRO_SELECTION_BACKEND`.

    Called only where the stored value is read (``_read_backend_choice``), never
    from :func:`resolve_selected_backend`: an already-resolved Kiro CLI id may be
    a degraded value's floor (harness-parity H3), not a pick.
    """
    if _kiro_cli_offers_kas and KIRO_SELECTION_BACKEND in selectable_backends():
        return KIRO_SELECTION_BACKEND
    return ACP_BACKEND_KIRO


# ── Capability membership (harness-parity H6, H7) ──
# Every capability a backend may claim is an OPT-IN set in this vocabulary, never a negation at
# the call site. ``not is_claude_backend`` reads correctly with two backends and
# then silently hands the capability to the third, so a harness that has never
# demonstrated the capability inherits it — and the operator who never opted into
# that harness is the one who finds out. Adding a member is a deliberate edit
# with evidence; inheriting a default is not a decision. See
# docs/system-specs/modules/harness-parity.md.


# ── Where each name is defined ──
# Callers import every name from this module, and it defines the core of the vocabulary:
# the backend identifiers, ``ACP_BACKENDS_KNOWN``, the first-run set and the selectable
# registry. Every other name is defined by the owner for its question and re-exported here:
#
# * ``backends_mcp`` -- MCP servers, agent specs and the tool surface
# * ``backends_members`` -- what a crew member's session may load and mount
# * ``backends_sessions`` -- which process serves a session, and sharing, eviction, restore
#   and fork
# * ``backends_turns`` -- steering, compaction, slash commands and how a turn ends
# * ``backends_models`` -- model and reasoning-effort switching
# * ``backends_routing`` -- how a harness is made to ask before a tool runs, and which of
#   its requests Crew answers
# * ``backends_launch`` -- launching, confining and recognising a harness's process
#
# A new set goes to the owner whose question it answers. ``scripts/check_harness_parity.py``
# names this file and each owner in ``VOCABULARY_PATHS``, so a set defined anywhere else
# still fails the gate. Every owner keeps this module's leaf property and imports only the
# standard library and the identifiers above, which is why the owners are imported last.
# The ``backends_*`` files are this vocabulary; ``backend_cards``, ``backend_install`` and
# ``backend_identity`` beside them are consumers that read it through this module.


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
# name is vocabulary, homed in this module and its owners, and the harness-parity gate enforces
# that. A derived answer is not vocabulary, so it stays a function and the question of
# which module owns the set does not arise.

from kiro_crew.agent_sdk.backends_launch import (  # noqa: E402,F401 - re-exported vocabulary
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_POD_HOME_REMAP,
    ACP_BACKENDS_RELAY_INTERNAL_SANDBOX,
    ACP_BACKENDS_SELF_SERVED_ACP,
    COPILOT_VERIFIED_VERSION,
    NODE_ADAPTER_ENTRY_SEGMENTS,
    STRANDS_NODE_FLOOR,
    STRANDS_PYTHON_REQUIREMENT,
    STRANDS_VERIFIED_VERSION,
    SelfServedLaunch,
    agent_process_markers,
    launch_for,
    node_adapter_entry_relpaths,
)
from kiro_crew.agent_sdk.backends_mcp import (  # noqa: E402,F401 - re-exported vocabulary
    ACP_BACKENDS_CLIENT_META_SETTINGS,
    ACP_BACKENDS_EFFORT_OVERLAY,
    ACP_BACKENDS_HONOR_ZERO_TOOL_BAN,
    ACP_BACKENDS_MARKDOWN_AGENT_SPECS,
    ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
    ACP_BACKENDS_MCP_WIRE_REINJECT,
    ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_SPEC_FILE_GRANTS,
    ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE,
    ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN,
    ACP_BACKENDS_TOOL_SEARCH_OVERLAY,
    ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY,
    overlay_project_scope,
)
from kiro_crew.agent_sdk.backends_members import (  # noqa: E402,F401 - re-exported vocabulary
    ACP_BACKENDS_MATE_WORKER_KIT,
    ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT,
    ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_MEMBER_TOOL_LIMITS,
)
from kiro_crew.agent_sdk.backends_models import (  # noqa: E402,F401 - re-exported vocabulary
    _MODEL_REFUSAL_PHRASE_BY_BACKEND,
    _MODEL_REGISTRY_NAMESPACE_BY_BACKEND,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
    CC_PERMISSION_MODE_ENV,
    DEFAULT_EFFORT_CONFIG_OPTION_ID,
    EFFORT_CONFIG_OPTION_ABSENT_FOLDS,
    EFFORT_CONFIG_OPTION_IDS,
    EFFORT_CONFIG_OPTION_NO_OVERRIDE_VALUES,
    EFFORT_CONFIG_OPTION_VALUES,
    effort_config_option_id,
    effort_config_option_levels,
    effort_config_option_value,
    model_refusal_phrase,
    model_registry_namespace,
    resolve_cc_permission_mode,
)
from kiro_crew.agent_sdk.backends_routing import (  # noqa: E402,F401 - re-exported vocabulary
    _PROMPT_COMMAND_NAME,
    _PROMPT_LEADING_FILLER,
    ACP_BACKEND_FORBIDDEN_MODES,
    ACP_BACKEND_GATE_PROBE_COMMAND,
    ACP_BACKEND_GATE_READBACK,
    ACP_BACKEND_PERMISSION_CONFIG,
    ACP_BACKEND_PERMISSION_SETTING,
    ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST,
    ACP_BACKEND_ROUTING,
    ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
    ACP_BACKENDS_HOOKS_LIST,
    ACP_BACKENDS_HOST_AUTH_CALLBACK,
    ACP_BACKENDS_META_IDENTITY,
    ACP_BACKENDS_OPEN_EXTERNAL_URL,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    ACP_BACKENDS_SIDE_READONLY,
    Readback,
    Routing,
    gate_probe_command_for,
    gate_readback_for,
    permission_config_for,
    permission_setting_for,
    prompt_command_issue,
    routing_for,
    session_config_drift_issue,
)
from kiro_crew.agent_sdk.backends_sessions import (  # noqa: E402,F401 - re-exported vocabulary
    ACP_BACKENDS_ACP_CLIENT_SPAWNABLE,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_FORK_AT_MESSAGE,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_RELOAD_DISPOSED_SESSION,
    ACP_BACKENDS_RESUME_WITHOUT_LOAD,
    ACP_BACKENDS_SERIAL_SESSION_STARTS,
    ACP_BACKENDS_SESSION_EVICTION,
    ACP_BACKENDS_SESSION_SHARING,
)
from kiro_crew.agent_sdk.backends_turns import (  # noqa: E402,F401 - re-exported vocabulary
    ACP_BACKENDS_AGENT_STARTED_TURNS,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_CONTEXT_RECYCLE,
    ACP_BACKENDS_HARNESS_MANAGED_COMPACTION,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_KIRO_FAMILY,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_LOCAL_CLEAR,
    ACP_BACKENDS_LOCAL_HARNESS_COMMANDS,
    ACP_BACKENDS_NATIVE_TODOS,
    ACP_BACKENDS_REPORT_USAGE,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEER_CONCURRENT_PROMPT,
    ACP_BACKENDS_STEER_RETRACT,
    ACP_BACKENDS_STEERING_REQUEST,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR,
    ACP_BACKENDS_UNMARKED_COMPACTION_PAIR,
    escape_user_message_markup,
    wrap_user_message,
)
