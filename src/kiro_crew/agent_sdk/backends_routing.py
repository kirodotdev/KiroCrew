"""How a harness is made to ask before a tool runs, and which of its requests Kiro
Crew answers.

The routing mechanism per harness and the data each mechanism needs (the session
config option, the seeded setting, the gate extension's probe and read-back), the
lookups that fail closed on an unknown id, and the memberships on the same
permission path: Side Chat's read-only tools, the fail-closed classification of a
tool call, a kindless permission frame, and the requests the reader loop answers
for the child -- an access token, a consent URL, the hook calls -- with the spec
hooks Crew fires itself.

One of the owners of the harness vocabulary in :mod:`kiro_crew.agent_sdk.backends`,
which re-exports every name defined here and is the import path callers use. Like that
module it is a leaf: nothing here imports ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.platform`` at module scope. ``scripts/check_harness_parity.py`` lists this file
in ``VOCABULARY_PATHS``, which is what lets its ``ACP_BACKEND*`` names be defined here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum
from typing import FrozenSet

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
)

# Backends on which a Side Chat turn may EXECUTE read-only tools under
# ``ToolApprovalPolicy.READ_ONLY``. A harness has its own pre-approval surface
# that the host gate cannot see, so a call it pre-approves would run with no
# READ_ONLY decision and no SEL row. A member starts its side session in a shape
# where no such pre-approval can reach a write, and every call it does not refuse
# itself asks the host gate:
#
# * kiro-cli: the session is bound to a derived ``<agent>--readonly`` spec
#   (``dashboard/side_readonly_spec``) whose emptied grants make every tool call
#   kiro-cli does not run natively raise a permission request. Captured on kiro-cli 2.34.0
#   (``test/fixtures/acp_frames/kiro/side-read-only-live.jsonl``): ``execute_bash``
#   and ``fs_write`` each asked and a rejected write left no file; ``fs_read`` and
#   ``grep`` inside the session cwd ran natively, as the spec module describes.
# * claude-agent-acp: its settings cannot be emptied from outside, so the session
#   is started with only the read built-ins, no MCP server, settings hooks
#   disabled, an ``ask`` rule on each read built-in and ``deny`` rules on the
#   host's sensitive paths, then pinned to ``default`` mode
#   (``AcpClient._claude_session_meta``, ``_pin_claude_starting_mode``). No write
#   tool exists there for an allow rule to pre-approve. ``default`` mode runs
#   Read, Grep and Glob inside the session's cwd without asking; the ``ask``
#   rules override that, and a denied sensitive path is refused by claude
#   before it asks. Captured on claude-agent-acp 0.94.0
#   (``test/fixtures/acp_frames/claude/side-read-no-ask-live.jsonl`` and
#   ``test/fixtures/acp_frames/claude/side-read-ask-live.jsonl``): without the
#   file-read rules four in-cwd reads ran with no permission request, and with
#   them all five reads asked. claude's routing (``Routing.SEEDED_SETTINGS``) is declared but not
#   enforced by this core.
# * goose: its pre-approval store is ``permission.yaml`` in its config directory,
#   which in ``approve`` mode runs a ``user.always_allow`` tool without asking.
#   The side session is started with a goose home of its own under its scratch
#   window (``GOOSE_PATH_ROOT``, ``acp.harness.goose.build_side_goose_root``):
#   the user's config entries linked in except ``permission.yaml``,
#   ``config.yaml`` copied without ``extensions``, and no MCP server on the
#   array. A read is proved from the (extension, tool) pair goose stamps, as
#   written: ``("developer", "tree")`` and ``("developer", "read_image")`` only
#   (``acp.harness_tool_names.GOOSE_READ_ONLY_BUILTINS``). POSIX hosts only
#   (``acp.harness.goose.side_home_supported``); elsewhere the turn runs
#   ``REJECT_ALL``.
# * deepseek uses Crew's verified gate plugin, which turns stock dsh tool allows
#   into asks. The owner proof below limits the read grant to stock dsh tools.
#
# Off this set the side turn runs ``REJECT_ALL``, the pre-allowance posture, and
# its footer says tools are unavailable there.
#
# KAS is a member on measurement. It takes the derived spec over the wire
# (``acp.kas_agents`` projects ``<agent>--readonly`` into ``customAgents``), and
# the derivation empties its ``permissions`` block with the other grants, so the
# projected agent carries no allow rule. Captured LIVE on KAS 0.66.43 (kiro-cli
# 2.32.0) and committed as ``test/fixtures/acp_frames/kas/side-read-only-live.jsonl``:
# with a base spec whose ``allowedTools`` pre-approves all four, ``read_file``,
# ``grep_search``, ``run_command`` and ``fs_write`` each raised
# ``session/request_permission`` under the derived spec, and a rejected write left
# no file. First measured on kiro-cli 2.29.0. An ``allow_always`` answer does not
# outlive its session: on
# one process, after a normal session's ``run_command`` and ``fs_write`` were
# answered ``allow_always``, the same calls from a ``--readonly`` side session, from
# a second normal session, and from a fresh process each asked again. What the
# READ_ONLY gate can then PROVE on KAS is what it
# proves on kiro-cli: a shell command is judged from the ``_meta.kiro.command`` the
# engine states, and a file read or search from the engine's ``_meta.kiro.toolId``
# read under its kiro-cli policy name (``read_file``/``list_directory`` ->
# ``fs_read``, ``grep_search`` -> ``grep``, ``file_search`` -> ``glob``), so a
# read-only one is approved and anything else is refused.
# The ``allow_always`` probe is quoted, not committed. No lane re-runs either, so a
# kiro-cli release after 2.32.0 is not re-measured.
#
# codex is NOT a member. No side session on it is measured here to raise a
# permission request for every call it does not run as a read, so a side turn on
# it runs ``REJECT_ALL``.
#
# opencode is NOT a member. Its tool calls do reach ``session/request_permission``
# (captured live), but that is by way of its own ``permission`` setting, not the
# derived ``<agent>--readonly`` spec this allowance is built on -- the harness reads
# no kiro agent spec at all. A side turn on it therefore runs ``REJECT_ALL`` until a
# read-only posture is expressed in the harness's own permission vocabulary.
#
# goose is a member on measurement, measured on goose 1.50.1. With the user's own
# ``permission.yaml`` listing ``shell``, ``tree``, ``analyze`` and ``read_image``
# under ``always_allow``, a session on the separate home raised
# ``session/request_permission`` for every tool the model called (``tree``,
# ``read_image``, ``analyze``, ``shell``, ``write``, ``todo__todo_write``,
# ``apps__create_app``, ``load``, ``delegate``, ``extensionmanager__manage_extensions``),
# and a rejected ``shell`` write left no file; on the user's own home the same
# ``shell`` call ran without asking. A server or config extension named
# ``developer`` that serves ``tree`` is stated ``developer__tree``, so it is not
# proven. goose has no tool that returns a file's text apart from its shell, and the
# shell stays refused, so a goose side turn lists folders and views images.
#
# pi is NOT a member either. Every one of its tool calls does reach
# ``session/request_permission``, but through Crew's gate extension rather than the
# derived spec, and the extension expresses no read-only posture -- it asks about
# everything and lets the host decide.
#
# deepseek is a member on measurement, and not through ``DSH_PERMISSION_MODE``:
# that sandbox posture denies instead of asking, so it would produce no READ_ONLY
# decision and no SEL row. Crew's verified gate plugin instead turns every stock
# dsh ``tools/pre-execute`` allow into ``ask`` and keeps only harness denies.
# Captured on dsh 0.2.0-rc.2 with the composed plugin: ``read``, ``grep``,
# ``glob``, ``write``, ``bash`` and an unknown ``mcp__probe__read`` all asked;
# rejected writes and commands left no file. The gate may approve only stock
# dsh-owned ``read``, ``grep`` and ``glob`` calls with absolute targets. Unknown,
# shadowed, MCP and command tools fail closed. The capture is
# ``test/fixtures/acp_frames/deepseek/side-read-only-live.jsonl``.
ACP_BACKENDS_SIDE_READONLY = frozenset(
    {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE, ACP_BACKEND_DEEPSEEK}
)

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

# Backends whose ``session/request_permission`` names no ``kind`` and whose
# tool_call ``kind`` is SAFE to carry onto the permission event in its place,
# so the gate's write-plane routing for ``edit``/``delete`` sees the
# classification the harness gave the call. Only the write-plane kinds are
# carried (``_dispatch.build_permission_event`` withholds every other cached
# kind), so membership decides whether a member's edits and deletes are
# routed by kind at all; the read-only proof is never fed a carried kind.
# KAS is the member: its permission ``toolCall`` is id, status and title only
# (``test/fixtures/acp_frames/kas/session.jsonl``), and it is the harness that
# has ``delete_file``. The kind it stamps on every built-in is recorded in
# ``test/fixtures/kas_builtin_tool_ids.json`` (``tool_call_kind``, from the
# engine's ``mapActionTypeToToolKind``): ``search`` on ``grep_search``,
# ``file_search`` AND ``list_directory``, ``other`` on ``code``/``tool_search``,
# ``read``/``edit``/``delete``/``execute``/``fetch`` on the rest -- which is
# why the carry is confined to the write plane rather than "every kind KAS
# stamps is one the gate reads".
# kiro-cli is NOT a member although its permission frames omit ``kind`` as
# well (``test/fixtures/acp_frames/kiro/session.jsonl``): its gates already
# route edits by the diff content block and it has no delete tool, and a
# carried ``edit`` would change what its kindless governance classification
# sees (the additive read pairs), so it needs nothing carried (H13).
# claude-agent-acp, codex-acp and the rest are not members: unmeasured, and a
# wrong kind on a permission event can only narrow or veto.
# Membership decides whether ``AcpSessionHandle`` hands its ``tool_kind_cache``
# (allocated for every backend) to ``_dispatch.build_permission_event``; a
# non-member's parser receives none and the frame's own ``kind`` stands.
ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL: FrozenSet[str] = frozenset({ACP_BACKEND_KAS})

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
# Captured live on KAS 0.66.70 (kiro-cli 2.35.1 spawned WITHOUT ``--auth-method
# cli``): ``test/fixtures/acp_frames/kas/host-auth-callback-live.jsonl``. On connect
# the engine sent ``_kiro/auth/getAccessToken`` with empty params for the client to
# answer from Crew's vault; the request carries no secret and the answer is
# client-to-agent, off the capture.
ACP_BACKENDS_HOST_AUTH_CALLBACK = frozenset({ACP_BACKEND_KAS})

#: Backends whose engine sends an MCP OAuth consent URL to its client as a
#: ``_kiro/openExternalUrl`` request, after the client starts a sign-in with
#: ``_kiro/mcp/resetServer``. Membership makes the handshake-declared channel
#: live: the reader loop answers that request and the session starts sign-ins.
#: KAS is the only member; it declares ``openExternalUrl`` in
#: ``KAS_CLIENT_CAPABILITIES``. KAS 0.66.70 (kiro-cli 2.36.0) live: after
#: ``_kiro/mcp/resetServer`` with ``startOAuth`` on an OAuth-gated http server, the
#: engine sent the consent URL as ``_kiro/openExternalUrl`` {url}
#: (acp_frames/kas/open-external-url-live.jsonl).
#: kiro-cli is not a member: its engine runs the OAuth flow itself and reports the
#: URL as ``_kiro.dev/mcp/oauth_request``.
#: claude, codex, opencode, pi, goose and deepseek are not members: none of them
#: defines either method.
ACP_BACKENDS_OPEN_EXTERNAL_URL = frozenset({ACP_BACKEND_KAS})

#: Backends whose agent asks its CLIENT for the hooks matching a trigger, and to
#: run one, over ``_kiro/hooks/list``, ``_kiro/hooks/sessionStart`` and
#: ``_kiro/hooks/executeHook``. Only KAS defines that channel, and the answers
#: carry -- and the last one runs -- operator-authored hook commands, so the route
#: that serves them is gated on membership rather than on the method name alone:
#: the dispatch loop it lives in is shared by every backend served by the shared
#: runtime, and a non-member sending any of them is answered ``-32601`` like any
#: other method it does not serve.
#:
#: Membership authorizes the ROUTE only. The handshake does not announce the
#: channel (``KAS_CLIENT_CAPABILITIES``), so a member asks nothing yet.
#:
#: kiro-cli is not a member: it runs the spec's hooks itself
#: (see ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``, which excludes it) and sends no
#: ``_kiro/hooks/*`` request.
ACP_BACKENDS_HOOKS_LIST = frozenset({ACP_BACKEND_KAS})

#: Backends whose session never runs the agent spec's own ``hooks``, so Crew's turn
#: loop fires them instead
#: (:mod:`kiro_crew.agent_sdk.spec_hooks`). KAS takes its agent over
#: the wire and the ``customAgents`` schema has no slot for them
#: (``acp.kas_agents.UNSUPPORTED_SPEC_KEYS``). kiro-cli is NOT a member and must
#: never become one: it reads the spec off disk and runs the field itself, so
#: membership there would run every spec hook twice. A harness added later stays
#: out until it is shown to drop the field.
#:
#: goose and opencode are members: their session/new element set has no hooks
#: field either, and both send a permission request for every tool call (goose
#: under the ``approve`` mode Crew seeds and reads back, opencode under the
#: ``ask`` permission Crew seeds and reads back), which is where the turn loop runs
#: a PreToolUse hook. Their tool matchers meet the harness's own tool names
#: through :mod:`kiro_crew.acp.harness_tool_names`. claude and codex are NOT: each
#: approves some calls inside the harness without asking, so a PreToolUse hook
#: would be skipped on exactly those calls until Crew makes them ask.
ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS = frozenset(
    {ACP_BACKEND_KAS, ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE}
)


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
    # Declared, not enforced: nothing reads back whether the seeded settings took
    # effect, and ``SEEDED_SETTINGS`` is outside ``tool_gate.ENFORCED_ROUTINGS``, so
    # a claude session is not refused for it.
    ACP_BACKEND_CLAUDE: Routing.SEEDED_SETTINGS,
    ACP_BACKEND_CODEX: Routing.SESSION_CONFIG,
    ACP_BACKEND_OPENCODE: Routing.VERIFIED_SEEDED_SETTINGS,
    ACP_BACKEND_PI: Routing.VERIFIED_GATE_EXTENSION,
    ACP_BACKEND_GOOSE: Routing.VERIFIED_SEEDED_SETTINGS,
    # deepseek reaches the same member as pi, and by the same reasoning: the harness
    # has no gate of its own that decides a TOOL CALL, so Crew loads one into it.
    # Its sandbox permits an in-policy
    # action silently, DENIES an out-of-policy one with the denial in the tool
    # result, and its own ``session/request_permission`` carries only a
    # MODEL-INITIATED ask to escalate past that sandbox, so its approval POLICY is
    # real, readable, and routes nothing. That is why it is not
    # ``VERIFIED_SEEDED_SETTINGS``. What it does answer is its own
    # ``tools/pre-execute`` waterfall: a plugin returning ``{kind: 'ask'}`` makes its
    # tools core resolve the call through ``ctx.approval``, which its ACP bridge
    # answers by emitting ``session/request_permission`` per call. So the gate is an
    # extension Crew composes, which is this member, and the frame corpus carries the
    # live capture (``test/fixtures/acp_frames/deepseek/permission-request-live``).
    ACP_BACKEND_DEEPSEEK: Routing.VERIFIED_GATE_EXTENSION,
    # Copilot CLI asks per tool call unless its own allow-all is on, and it states
    # that switch as an ACP v1 session config option (``allow_all``) on every
    # session/new and session/load -- Codex's mechanism, applied the same way.
    ACP_BACKEND_COPILOT: Routing.SESSION_CONFIG,
    # The Strands CLI's Python-project ACP mode forwards a Strands interrupt as
    # ``session/request_permission`` and raises none for a tool call itself, so Crew
    # loads the project through its own gate module, which raises one per call --
    # pi's and deepseek's member. Live capture:
    # ``test/fixtures/acp_frames/strands/permission-deny-allow-live.jsonl``.
    ACP_BACKEND_STRANDS: Routing.VERIFIED_GATE_EXTENSION,
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
    # Copilot CLI's ``allow_all`` option: ``off`` is "Require approval for tool,
    # path, and URL requests", its own default -- but a user default (the
    # ``COPILOT_ALLOW_ALL`` environment variable, ``--allow-all`` in a wrapper, a
    # restored session left on) can turn it on, so Crew writes ``off`` rather than
    # trusting the default. Measured on Copilot CLI 1.0.100. Passive reads inside
    # the session's directory run without asking, which is the same residual Codex
    # has and the same OS credential mask compensates for.
    ACP_BACKEND_COPILOT: ("allow_all", "off"),
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
    # deepseek's token is a PLUGIN name rather than a command name, because its
    # composition has no command registry to register into -- see
    # ``ACP_BACKEND_GATE_READBACK`` below for which read-back looks for it.
    ACP_BACKEND_DEEPSEEK: "kiro-crew-tool-gate",
    # The gate MODULE's own name: like deepseek's, a composition with no command
    # registry, so the gate reports itself through a load marker.
    ACP_BACKEND_STRANDS: "kiro-crew-strands-gate",
}


class Readback(str, Enum):
    """HOW a ``VERIFIED_GATE_EXTENSION`` harness is asked whether the gate loaded.

    The mechanism is one member of :class:`Routing` because the GUARANTEE is one
    thing -- Kiro Crew's own gate, loaded into a harness that has none, confirmed
    present before the first prompt -- but the QUESTION has to be asked in the
    harness's own terms, and two harnesses do not answer the same way. This enum
    is that difference, kept as data beside the routing table so a third harness
    is a row rather than a branch in the verdict.

    ``COMMAND_REGISTRY`` -- ask the harness. The extension registers a command and
    the harness's own registry reports it with the file it was loaded from, so the
    WITNESS is the harness: Crew reads back an answer it did not author. This is
    the stronger of the two and is preferred wherever a harness offers it.

    ``LOAD_MARKER`` -- ask Crew's own code, because the harness offers nothing to
    ask. The plugin writes one file at a path Crew names in the child's
    environment, carrying the probe name, the per-session nonce Crew issued, and
    its own resolved module URL. Present-with-this-nonce-and-this-module proves
    the plugin Crew shipped was composed, from Crew's file, in THIS session --
    which is the precondition. It is weaker than the member above in one specific
    way, stated rather than glossed: the witness is the gate itself, so it
    attests that Crew's code ran and not that the harness reports it running. A
    harness whose ACP surface adds no method, capability or ``_meta`` field -- which
    is the dsh ACP profile's own declared invariant -- leaves no third option, and
    a marker that is merely ABSENT refuses the session, so the failure direction
    is closed either way.
    """

    COMMAND_REGISTRY = "command_registry"
    LOAD_MARKER = "load_marker"


#: Harness id -> how its ``VERIFIED_GATE_EXTENSION`` read-back is performed.
#:
#: A ``.get(backend, Readback.COMMAND_REGISTRY)`` read would be wrong here: a
#: harness added to the routing table without a row would silently inherit a
#: read-back its driver never implements and report ROUTED for it. So the lookup
#: fails closed instead -- see :func:`gate_readback_for`.
ACP_BACKEND_GATE_READBACK: dict = {
    ACP_BACKEND_PI: Readback.COMMAND_REGISTRY,
    ACP_BACKEND_DEEPSEEK: Readback.LOAD_MARKER,
    ACP_BACKEND_STRANDS: Readback.LOAD_MARKER,
}


#: Harness id -> the slash commands Crew lets reach it as the start of a prompt.
#:
#: A member's prompt handler RUNS a prompt that begins with ``/<name>`` as its own
#: command instead of sending it to the model, and some of those commands move the
#: harness's permission state with no frame Crew can act on in time. Copilot CLI
#: 1.0.100 is the member, measured: a prompt of ``/allow-all on``, ``/yolo`` (an
#: alias its ``available_commands_update`` does not even list) or ``/permissions
#: allow-all`` turned its ``allow_all`` option on, and the ``config_option_update``
#: saying so arrived only on the NEXT config write, after tools would already have
#: run unasked. Its parser strips leading whitespace (space, tab, newline, NBSP,
#: ideographic space all measured), folds case (``/YOLO``), takes the token up to
#: the next whitespace as the name, and does NOT run a token carrying other
#: punctuation (``/yolo.``, ``/yolo/x``, ``//yolo``, ``/ yolo`` all reached the
#: model). A prompt, though, is text anyone upstream of the harness can author --
#: a channel message, a sub-agent task another model wrote, a cron body -- so the
#: command a prompt may start with is an ALLOWLIST: the ones that only read or
#: compact. ``/plan`` and ``/research`` are left out: plan mode ends in an offer to
#: continue in autopilot, which this build has not measured over ACP. Every other command-shaped first token is refused before the wire, so a
#: command this build has never seen fails closed rather than open. The
#: ``allow_all`` write at session start (``ACP_BACKEND_PERMISSION_CONFIG``) is the
#: other half: it is what undoes a value a restored session was left in.
ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST: dict = {
    ACP_BACKEND_COPILOT: frozenset(
        {
            "compact",
            "context",
            "env",
            "init",
            "list-dirs",
            "rename",
            "review",
            "security-review",
            "usage",
        }
    ),
}

#: Harness id -> the session modes that a SESSION_CONFIG member must never be seen in.
#:
#: The routing write proves the permission option's value when a session opens, and
#: the prompt allowlist keeps Crew's own prompts from moving it. Neither holds a
#: session there if the HARNESS moves itself, which is why a member here is watched
#: for the whole session (``AcpClient._tripwire_session_config``): a
#: ``config_option_update`` reporting the permission option off its required value,
#: or a ``current_mode_update`` naming one of these modes, stops the harness. Copilot
#: CLI 1.0.100's ``autopilot`` mode is described by the harness as one that "enables
#: allow-all and runs until task completion without user interaction", and a
#: ``mode`` write of it was measured turning ``allow_all`` on. Crew never writes a
#: mode to this harness, so seeing it means something other than Crew moved it.
ACP_BACKEND_FORBIDDEN_MODES: dict = {
    ACP_BACKEND_COPILOT: frozenset(
        {"https://agentclientprotocol.com/protocol/session-modes#autopilot"}
    ),
}


def session_config_drift_issue(backend: str, update: object) -> str:
    """Why a ``session/update`` *update* shows *backend* leaving its routed state, or ``""``.

    Pure. A backend with no :data:`ACP_BACKEND_FORBIDDEN_MODES` row is never refused,
    so this reads nothing for a harness outside the table (harness-parity H13).
    """
    forbidden = ACP_BACKEND_FORBIDDEN_MODES.get(backend)
    if forbidden is None or not isinstance(update, dict):
        return ""
    kind = update.get("sessionUpdate")
    if kind == "current_mode_update":
        mode = update.get("currentModeId")
        if isinstance(mode, str) and mode in forbidden:
            return f"the harness switched itself to mode {mode!r}, which approves tools on its own"
        return ""
    if kind != "config_option_update":
        return ""
    option_id, required = ACP_BACKEND_PERMISSION_CONFIG.get(backend, ("", ""))
    options = update.get("configOptions")
    if not option_id or not isinstance(options, list):
        return ""
    for option in options:
        if not isinstance(option, dict):
            continue
        if option.get("id") == option_id and option.get("currentValue") != required:
            return (
                f"the harness reported {option_id}={option.get('currentValue')!r} mid-session; "
                f"Kiro Crew requires {required!r}"
            )
        if option.get("category") == "mode" and option.get("currentValue") in forbidden:
            return (
                f"the harness reported mode {option.get('currentValue')!r} mid-session, "
                "which approves tools on its own"
            )
    return ""


#: The command-name grammar a member's parser runs: a word of letters, digits, ``-``
#: and ``_``. A first token outside it (a path such as ``/etc/hosts``, ``/yolo.``) is
#: not run as a command, so it is not refused either.
_PROMPT_COMMAND_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
#: What may precede the slash and still leave a command for the harness to run. Its
#: parser strips Unicode whitespace; the zero-width format characters are stripped
#: here too although a measured ``\u200b/allow-all on`` did NOT run, because the
#: cost of reading one as a command is a refused prompt and the cost of the
#: opposite mistake is a gate turned off.
_PROMPT_LEADING_FILLER = re.compile(r"^[\s\u200b\u200c\u200d\u2060\ufeff]*")


def prompt_command_issue(backend: str, texts: Iterable[str]) -> str:
    """Why *backend* must not be sent a prompt made of *texts*, or ``""``.

    Pure and harness-neutral: it reads :data:`ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST`,
    so a backend without a row is never refused. Every text block is checked, not
    only the first, so the order a prompt builder lays blocks out in cannot decide
    which one the harness parses.
    """
    allowed = ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST.get(backend)
    if allowed is None:
        return ""
    for text in texts:
        if not isinstance(text, str):
            continue
        body = _PROMPT_LEADING_FILLER.sub("", text, count=1)
        if not body.startswith("/"):
            continue
        rest = body[1:]
        if not rest or rest[0].isspace():
            continue
        if not rest[0].isascii():
            # A character right after the slash that the two runtimes may not agree
            # on (``/\ufeffyolo``): the harness may strip it and run the command.
            return (
                f"'/' is followed by {rest[0]!r}, which the harness may strip before "
                f"reading a command name, so the prompt could run as the harness's own "
                f"command rather than reach the model. To send the text to the model, "
                f"start it without the slash."
            )
        # The name is read the way the harness reads it: the longest run of name
        # characters. What follows decides whether that run is a command. Nothing, or
        # whitespace -- U+FEFF included, which JavaScript's ``\s`` matches and
        # Python's ``split()`` does not -- ends a command name; an ASCII character
        # that is not whitespace (``/yolo.``, ``/etc/hosts``) makes it a path, never
        # run; any other character is one the two runtimes may disagree about, so the
        # prompt is refused rather than guessed at.
        match = _PROMPT_COMMAND_NAME.match(rest)
        if match is None:
            continue
        token = match.group(0)
        following = rest[match.end() : match.end() + 1]
        if following and not (following.isspace() or following == "\ufeff"):
            if following.isascii():
                continue
            return (
                f"'/{token}' is followed by {following!r}, a character the harness may "
                f"read as the end of a command name, so it could run as the harness's "
                f"own command rather than reach the model. Kiro Crew forwards only these "
                f"commands to it: {', '.join('/' + name for name in sorted(allowed))}. "
                f"To send the text to the model, start it without the slash."
            )
        if token.casefold() in allowed:
            continue
        return (
            f"'/{token}' would run as the harness's own command rather than reach the "
            f"model, and Kiro Crew forwards only these commands to it: "
            f"{', '.join('/' + name for name in sorted(allowed))}. Permissions are set "
            f"with Kiro Crew's own approval mode. To send the text to the model, "
            f"start it without the slash."
        )
    return ""


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


def gate_readback_for(backend: str) -> Readback | None:
    """How *backend*'s gate-extension read-back is performed, or ``None``.

    ``None`` means this harness declares no read-back style, and every caller
    treats that as a refusal rather than picking one: a harness routed through
    :data:`Routing.VERIFIED_GATE_EXTENSION` whose style is unknown would otherwise
    report ROUTED for a check its driver never runs.
    """
    return ACP_BACKEND_GATE_READBACK.get(backend)


# These classes keep the facade as their module: it is the path every caller
# imports them from, so a pickled or logged reference names that path.
Routing.__module__ = "kiro_crew.agent_sdk.backends"
Readback.__module__ = "kiro_crew.agent_sdk.backends"
