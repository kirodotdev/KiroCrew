"""What a live session's backend can do — asked instead of which backend it is.

Application code has seven branches that depend on a PROPERTY of the harness:
which model-id namespace it uses, whether its advertised list must be read back,
which channel carries an effort change, whether compaction finishes inline,
whether it serves native todos, and which provider seam serves the session.
Naming the harness instead of the property has one failure mode, always in the
same direction. A fifth backend arrives, nobody
edits the branch, and it silently takes whichever arm "not claude" happens to
select — an arm it never demonstrated it can serve.
``docs/system-specs/modules/harness-parity.md`` calls this H6; RFC PR 3 is where it
stops being possible outside the boundary.

So this module carries one frozen record per session, with one field per question
a consumer OUTSIDE ``kiro_crew.agent_sdk`` actually asks, and consumers read the
field. The identity of the backend is still in :attr:`SessionCapabilities.backend`
because a log line and an error message need to name it — but nothing branches on
it. ``test_agent_sdk_capabilities`` pins that the migrated consumers do not
read the old backend-identity checks.

Every field is a TRANSLATION of a table that already existed in
:mod:`kiro_crew.agent_sdk.backends`, not a new judgement. That is deliberate and
it is the whole reason this can land as a refactor: each field reproduces, for
every backend id including ``codex`` and including an unknown id, exactly the
answer the branch it replaces already computed. The disposition table in
``backends``'s docstring records which set each field translates.

Fail-closed on an unknown id
----------------------------
:func:`capabilities_for` never raises and never guesses. An id this build does not
know resolves to :data:`UNKNOWN_BACKEND_CAPABILITIES` — every boolean False and
both string axes on their default — which is the same answer the old
``== ACP_BACKEND_CLAUDE`` comparisons gave an unrecognized value. False here means
"has not demonstrated the capability", so the arm a stranger takes is the
conservative one rather than the one the previous member happened to leave behind.

Why a record and not a bag of module functions
----------------------------------------------
:func:`capabilities_of` resolves the record from a PROVIDER, and the isinstance
check it performs is load-bearing rather than defensive noise. The predicates this
replaces were ``isinstance(provider, AcpProvider) and provider.is_claude_backend``,
so a shape that was not a provider answered False. A bare
``getattr(provider, "capabilities", None)`` would not preserve that:
``MagicMock(spec=AcpProvider).capabilities`` is a truthy Mock whose every
attribute is also truthy, so a spec'd test double would start reading as a member
of every capability at once. Requiring a real :class:`SessionCapabilities`
instance keeps an unknown shape on the fail-closed default, which is what the
isinstance gate bought.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ACP_CLIENT_SPAWNABLE,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
    ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_HARNESS_OWNED_SLASH_COMMANDS,
    ACP_BACKENDS_HONOR_ZERO_TOOL_BAN,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_MODEL_PER_SESSION,
    ACP_BACKENDS_NATIVE_TODOS,
    ACP_BACKENDS_NATIVE_WORKFLOWS,
    ACP_BACKENDS_PERMISSION_REQUESTS_NAME_CHILD,
    ACP_BACKENDS_PLAN_CHECKLIST,
    ACP_BACKENDS_REMOTE_MCP_OAUTH,
    ACP_BACKENDS_TOOL_TITLE_REFINEMENT,
    model_registry_namespace,
)
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE

#: The model-registry namespace kiro's own ids live in, and the one every backend
#: that has not declared another falls back to. Named so a consumer can ask
#: "is this the native namespace?" without spelling the literal.
MODEL_NAMESPACE_ACP = "acp"

#: The namespace of claude-agent-acp's ids, the only one besides ``acp`` that the
#: model registry carries ids for (``model_registry.json`` ``providers``).
MODEL_NAMESPACE_CLAUDE_CODE = "claude_code"

#: The backends whose transcripts the dashboard's Session Activity parse reads:
#: kiro-cli's ``cli`` session directory and the KAS records. Read only to answer
#: ``SessionCapabilities.writes_crew_readable_transcript``. shortcut: kept private
#: here rather than as a registered ``ACP_BACKENDS_*`` set, because registering one
#: edits ``agent_sdk/backends.py``; move it there once that file is free.
_CREW_READABLE_TRANSCRIPT_BACKENDS = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})


@dataclass(frozen=True)
class SessionCapabilities:
    """What the backend serving one session can do.

    Frozen, and cheap enough to build per read: every field is a set membership or
    a dict lookup over four ids, so there is no cache to invalidate when an edition
    registers a backend mid-process.
    """

    #: The ``agent.acp_backend`` id this record describes. For messages and logs.
    #: Nothing above the boundary may branch on it — that is the whole point of
    #: the fields below.
    backend: str

    #: Which provider SEAM serves the session: :data:`PROVIDER_ACP` or
    #: :data:`PROVIDER_CLAUDE_CODE`.
    #:
    #: The ``agent.provider`` axis, not the ``acp_backend`` axis — see
    #: :mod:`kiro_crew.agent_sdk.provider_identity` for why conflating them draws
    #: the wrong conclusion from either. Consumers use it to LABEL a session (a
    #: billing row's provider) and to route session-file cleanup to the right
    #: home tree (``~/.claude`` vs ``~/.kiro``).
    #:
    #: Known residue, inherited unchanged: KAS reads as :data:`PROVIDER_ACP`. Only
    #: the ACP layer's own ``PROVIDER_LABEL_*`` constants distinguish it, and
    #: promoting that distinction here would change what every KAS turn records.
    #: A widening belongs in its own change with its own reason.
    provider_seam: str

    #: The model-registry namespace this backend's model ids live in.
    #:
    #: A registry index key, NOT a provider-identity check: a context window is a
    #: property of the model, so the same model reached two ways shares one
    #: namespace. :data:`MODEL_NAMESPACE_ACP` means the backend takes kiro's own
    #: ids and expresses "let the provider choose" as the real id ``auto``; any
    #: other value means ids are translated into that provider's namespace and
    #: there is no id meaning "choose for me", so returning to default needs a
    #: session reset rather than a switch.
    model_id_namespace: str

    #: Whether the WIRE model id must be resolved against the list this backend
    #: advertised on ``session/new``, rather than trusting a stored id verbatim.
    #:
    #: True where the spelling the backend SERVES differs from the one Crew stored
    #: (claude-agent-acp advertises versioned ``…[1m]`` ids). Two consumers ride
    #: it: the models picker reads the advertised list from such a session, and the
    #: pinned-model availability verdict SKIPS such a session, because comparing a
    #: stored id against a differently-spelled advertised list manufactures false
    #: "model unavailable" withholds.
    resolves_model_from_advertised_list: bool

    #: Whether one process can run each of its sessions on its own model
    #: (:data:`ACP_BACKENDS_MODEL_PER_SESSION`).
    #:
    #: True where the model request names the session it is for. A sub-agent
    #: started on a model of its own can then take a session on its parent's
    #: process; False sends it to a process of its own, where the model is applied
    #: at that process's start. Unknown and newly registered backends stay False
    #: until they opt in.
    takes_model_per_session: bool

    #: Whether a reasoning-effort change goes through
    #: ``session/set_config_option("effort", …)`` rather than the kiro-native
    #: ``/effort`` slash command.
    #:
    #: A change sent down a channel the adapter does not implement is answered
    #: with method-not-found, and the session keeps serving turns at the level the
    #: operator thought they had just left.
    effort_via_config_option: bool

    #: Whether a reasoning-effort change goes through the kiro-native ``/effort``
    #: slash command (``_kiro.dev/commands/execute``).
    #:
    #: The other channel :attr:`effort_via_config_option` does not describe. A
    #: harness answering False to both has no wire channel for an effort change, and
    #: a caller that sends the slash command anyway is answered with
    #: method-not-found by a harness that never implemented it.
    effort_via_slash_command: bool

    #: Whether a manual ``/compact`` finishes inside the ``session/prompt`` turn.
    #:
    #: True means the turn's terminal frame is the done signal and the caller
    #: acknowledges immediately. False means the result arrives separately and the
    #: caller must await it; awaiting a member instead strands that wait for its
    #: full timeout.
    compacts_inline: bool

    #: Whether Crew's turn loop must fire the agent spec's own ``hooks``.
    #:
    #: True where the harness never receives them (KAS takes its agent over a
    #: wire schema with no slot for the field). False where the harness runs them
    #: itself, and firing them here too would run each one twice.
    crew_fires_spec_hooks: bool

    #: Whether Crew's turn loop must fire the agent spec's hooks on the events that
    #: cannot refuse a tool call (``postToolUse``, ``userPromptSubmit``,
    #: ``agentSpawn``, ``stop``).
    #:
    #: True wherever :attr:`crew_fires_spec_hooks` is, and on claude and codex,
    #: whose harnesses are handed no spec but approve some calls without asking, so
    #: only these events are fired there and ``preToolUse`` runs nowhere. False
    #: where the harness runs the field itself.
    crew_fires_spec_observer_hooks: bool

    #: Whether this harness interprets ``/todos`` as its native task-list command.
    #:
    #: This is deliberately narrower than general slash-command transport: the
    #: kiro-cli has a native slash RPC but does not implement this command.
    #: Unknown and newly registered backends remain False until they opt in.
    supports_native_todos: bool

    #: Whether this harness keeps the agent's checklist itself and sends it as an
    #: ACP ``plan`` (:data:`ACP_BACKENDS_PLAN_CHECKLIST`), rather than through
    #: kiro-cli's ``todo_list`` tool.
    #:
    #: True means the dashboard's checklist sync and recovery blocks, which tell the
    #: agent to call ``todo_list``, are never prepended to its prompt: it has no such
    #: tool. False keeps them, which is right for kiro-cli and harmless elsewhere.
    keeps_own_checklist: bool

    #: Whether this harness runs ANY leading-slash prompt as one of its own commands.
    #:
    #: True where the adapter resolves the name itself (Claude Code's ``/review``,
    #: ``/init``, skills, ``.claude/commands``), so the dashboard forwards every
    #: leading-slash word raw instead of only kiro-cli's command names. Unknown and
    #: newly registered backends remain False until they opt in.
    runs_own_slash_commands: bool

    #: Whether an agent spec's ``"tools": []`` is honoured as a total ban -- no
    #: MCP server AND no harness-native tool -- on this backend.
    #:
    #: True for kiro-cli/KAS (native) and for opencode/goose/deepseek/pi, whose
    #: harness-native tools are refused by ``AcpClient._deny_zero_tools`` at the
    #: permission layer (pi has no mirror; its ``zero_tools`` is read beside its
    #: member mounts from the same spec parse). False for claude, whose routing is
    #: declared but not enforced by this core -- an inherited ``~/.claude``
    #: pre-approval can skip ``session/request_permission`` entirely, so the refusal
    #: never runs. False for codex: its ``AcpSessionHandle`` refuses every permission
    #: request of a zero-tool session (``_deny_zero_tools``), but codex reads files,
    #: searches the web, runs sandboxed commands and calls self-approved tools of the MCP servers in the
    #: operator's ``config.toml`` without asking. A consumer that
    #: spawns a zero-tool agent (the knowledge/research pool) asks this before
    #: trusting a resolved backend, rather than enumerating identities itself.
    honors_zero_tool_ban: bool

    #: Whether ``AcpClient._spawn`` can construct a session for this backend
    #: directly. A POSITIVE membership question (:data:`ACP_BACKENDS_ACP_CLIENT_SPAWNABLE`),
    #: not "every backend except kas/codex": kas and codex need ``AcpRuntime`` and
    #: have no arm in ``_spawn``, so a request to spawn either through
    #: ``AcpClient`` falls through to a bare kiro-cli spawn under the wrong
    #: identity instead of failing loudly -- and a FUTURE backend with no arm
    #: either must report False here too, which only a positive allowlist gives.
    #: A consumer that constructs ``AcpClient`` itself (rather than going through
    #: a provider that already routes by this fact) asks it before passing a
    #: resolved backend on.
    acp_client_spawnable: bool

    #: Whether a remote MCP entry's ``oauth`` block reaches the engine, so a
    #: provider whose OAuth client is pre-registered (no dynamic client
    #: registration) can sign in on this backend.
    #:
    #: False on KAS: its agent schema has no slot for the block, and the engine
    #: falls back to registering a client the provider refuses. The Connections
    #: page reads it to mark those providers kiro-cli only rather than offer a
    #: Connect that cannot finish.
    honors_remote_mcp_oauth: bool

    #: Whether the harness brings its own workflow engine
    #: (:data:`ACP_BACKENDS_NATIVE_WORKFLOWS`), so that workflow definitions, runs
    #: and controls go to it over ``_kiro/workflow/*`` instead of to Crew's
    #: ``ctx``-script engine. False routes to Crew's engine, which serves every
    #: harness, so an unknown backend loses nothing.
    native_workflows: bool

    #: Whether a ``tool_call_update`` carries a call's real title after a first
    #: ``tool_call`` that named it before its input had streamed
    #: (:data:`ACP_BACKENDS_TOOL_TITLE_REFINEMENT`), so a Crew-spawned subagent's
    #: Subagents card takes the refined title for that call's row, and a channel
    #: turn's task card for that call (``messaging.driver``'s ``TOOL_TITLE``).
    #:
    #: True for claude, whose first frame of a call carries the tool's generic title
    #: ("Terminal", "Read File"). False keeps each card on the titles its call (and,
    #: on the Subagents card, its permission request) named, which is right for
    #: kiro-cli: the update that ends its failed call carries the bare tool name, a
    #: worse title than the first.
    refines_tool_call_titles: bool

    #: Whether a permission request raised while a native sub-agent's card is open
    #: is that sub-agent's, not the parent's own
    #: (:data:`ACP_BACKENDS_PERMISSION_REQUESTS_NAME_CHILD`), so the dashboard's
    #: native-crew auto-approve rung may answer it.
    #:
    #: True for kiro-cli and KAS. False everywhere else, an unknown backend
    #: included: there the rung is skipped and the request takes the normal gate.
    permission_requests_name_child: bool

    #: Whether a session of this backend leaves a transcript the dashboard's
    #: Session Activity parse reads: kiro-cli's ``cli`` session directory or the
    #: KAS records (:data:`_CREW_READABLE_TRANSCRIPT_BACKENDS`). A backend that answers False
    #: has its sessions counted from the per-turn usage rows instead, so a new
    #: backend is counted rather than missed.
    writes_crew_readable_transcript: bool

    #: Whether a chat may switch steering files off one by one and have Crew's
    #: steering load leave them out (:mod:`kiro_crew.steering_off`): the context
    #: list offers the switch and the load honours it.
    #:
    #: True for claude, whose chats take their steering through Crew's load and
    #: whose list offers the switch. False everywhere else, an unknown backend
    #: included: there the list offers no switch and the load sends every file.
    honors_chat_steering_off: bool

    #: Whether a session of the default agent gets Kiro Crew's own persona prompt
    #: in its ``[AGENT SYSTEM PROMPT]`` block, written for this harness (the
    #: ``kiro-cli`` transport name respelled), because the harness loads no Crew
    #: agent spec that would carry it.
    #:
    #: True for claude. False everywhere else, an unknown backend included: there
    #: the default agent's block comes from its spec as before.
    crew_sends_persona_prompt: bool

    #: Whether the harness itself picks which conditional steering files
    #: (``manual`` and ``fileMatch`` inclusion) a turn gets, so Crew's context
    #: assembly leaves that choice to it. Read both by a live provider's
    #: ``native_steering`` and, for a chat with no live provider yet, by the
    #: dashboard's steering list, so the two answer alike.
    #:
    #: True for KAS. False everywhere else, an unknown backend included: there
    #: Crew keeps those files reachable itself.
    loads_steering_natively: bool

    #: Whether a prompt the harness accepted stays in its native session even
    #: when the turn never lands (a Stop after a real ``end_turn``, an
    #: empty-response give-up): the Kiro "kept the prompt" contract. A merge
    #: card sent in such a prompt counts as delivered, so it is not sent again.
    #:
    #: True for kiro-cli and KAS (which runs the Kiro CLI pick). False
    #: everywhere else, an unknown backend included: there a card counts only
    #: on a landed turn, so a wrong guess repeats it instead of losing it.
    keeps_prompt_on_unlanded_turn: bool

    #: Whether the harness answers a rejected permission request by ending a
    #: clean ``end_turn`` on the refusal itself, leaving it unanswered, so a
    #: sub-agent turn that ends that way is read as ending on the refusal.
    #:
    #: True for opencode, whose ``reject_once`` ends the turn. False everywhere
    #: else, an unknown backend included: there the harness goes on after a
    #: reject, and a clean ending is not read as one.
    ends_turn_on_permission_reject: bool

    #: Whether the harness names a Crew tool in its tool calls only by a fused
    #: ``<server>_<tool>`` name, so a sub-agent's tool dispatch reads that name as
    #: Crew's only for a server the session handed the harness.
    #:
    #: True for pi and claude. False everywhere else, an unknown backend
    #: included: there no handed server list is read at dispatch.
    names_crew_tools_by_fused_name: bool

    #: Whether an Auto pick moves a live session to ``auto`` in place even when
    #: the slot already holds ``auto``: the harness advertises no model list,
    #: runs ``auto`` on a new session, and can come back on a concrete model (a
    #: reopened chat, a start that inherits kiro-cli's ``chat.defaultModel``), so
    #: the Auto pick is sent as a live ``set_model`` rather than a reset.
    #:
    #: True for KAS. False everywhere else, an unknown backend included: there
    #: Auto goes live only when the backend advertised it, and a slot already on
    #: ``auto`` keeps the same-value shortcut.
    model_auto_switches_in_place: bool

    #: Whether the harness loads the session's agent itself (its persona,
    #: steering, skills and resources), so ``kirocrew chat`` sends the typed text
    #: as it is instead of building Crew's session context into the first message.
    #:
    #: True for kiro-cli and KAS. False everywhere else, an unknown backend
    #: included: there the CLI builds that context itself.
    harness_loads_agent_context: bool

    #: Whether the session's MCP report names, in ``sign_in_suspected``, the remote
    #: servers Crew's credential-free probe found asking for a sign-in, because the
    #: harness sends no sign-in frame of its own. The dashboard shows one "may need
    #: a sign-in" row per chat for that list.
    #:
    #: True for opencode. False everywhere else, an unknown backend included: there
    #: no such list is read and no row is shown.
    reports_mcp_sign_in_suspects: bool

    #: Whether Crew folds the user's standalone ``~/.kiro/hooks/*.json`` files into
    #: the hooks it fires for this harness's turns, and tells the chat when a
    #: checkout's own hook files are withheld.
    #:
    #: True for KAS. False everywhere else, an unknown backend included: goose and
    #: opencode have Crew fire their spec hooks but do not read a user's standalone
    #: files, so none are added.
    crew_merges_user_hook_files: bool

    #: Whether the harness keeps the same native session across its ``/compact``
    #: boundary, so Crew compacts in place: it waits for the turn permit, sends
    #: ``/compact`` and leaves the durable session mapping untouched.
    #:
    #: True for claude. False everywhere else, an unknown backend included: there
    #: the session takes the other compaction arms.
    keeps_session_across_compaction: bool

    #: Whether the harness lends a custom agent kiro-cli's default resources: the
    #: user's global and workspace steering and the default skill trees, unless the
    #: workspace opted out (``chat.disableInheritingDefaultResources``). Context
    #: only; nothing here grants a permission.
    #:
    #: True for kiro-cli. False everywhere else, an unknown backend included: no
    #: other harness lends those trees, so Crew delivers them itself or not at all.
    inherits_kiro_default_resources: bool

    #: Whether the agent on this harness holds kiro-cli's ``todo_list`` built-in, the
    #: tool whose results fill the chat's step pill.
    #:
    #: True for kiro-cli only. KAS registers no such tool, and a harness that keeps
    #: its checklist itself (:attr:`keeps_own_checklist`) sends an ACP ``plan``
    #: instead. The dashboard asks the agent to keep a step list only where this is
    #: True, so no agent is told to call a tool it does not have.
    has_todo_list_tool: bool


def capabilities_for(backend: str) -> SessionCapabilities:
    """The capabilities of *backend*, fail-closed for an id this build cannot name.

    Pure and total: no raise, no I/O, no dependence on whether a session exists.
    That is what lets ``config.loader`` call it from inside
    ``KiroCrewConfig.load()``, where reaching the platform context would re-enter
    the very load that called it.
    """
    return SessionCapabilities(
        backend=backend,
        provider_seam=(PROVIDER_CLAUDE_CODE if is_claude_backend_name(backend) else PROVIDER_ACP),
        model_id_namespace=model_registry_namespace(backend),
        resolves_model_from_advertised_list=backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
        takes_model_per_session=backend in ACP_BACKENDS_MODEL_PER_SESSION,
        effort_via_config_option=backend in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
        # The two effort channels are disjoint sets, so a harness is told to use
        # at most one. KAS is in neither the command set nor this answer: it
        # refuses ``_kiro.dev/commands/execute`` with -32603, and its effort
        # travels only as ``effortLevel``.
        effort_via_slash_command=backend in ACP_BACKENDS_KIRO_SLASH_COMMANDS,
        compacts_inline=backend in ACP_BACKENDS_INLINE_COMPACTION,
        crew_fires_spec_hooks=backend in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
        crew_fires_spec_observer_hooks=backend in ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS,
        supports_native_todos=backend in ACP_BACKENDS_NATIVE_TODOS,
        keeps_own_checklist=backend in ACP_BACKENDS_PLAN_CHECKLIST,
        runs_own_slash_commands=backend in ACP_BACKENDS_HARNESS_OWNED_SLASH_COMMANDS,
        honors_zero_tool_ban=backend in ACP_BACKENDS_HONOR_ZERO_TOOL_BAN,
        acp_client_spawnable=backend in ACP_BACKENDS_ACP_CLIENT_SPAWNABLE,
        honors_remote_mcp_oauth=backend in ACP_BACKENDS_REMOTE_MCP_OAUTH,
        native_workflows=backend in ACP_BACKENDS_NATIVE_WORKFLOWS,
        refines_tool_call_titles=backend in ACP_BACKENDS_TOOL_TITLE_REFINEMENT,
        permission_requests_name_child=backend in ACP_BACKENDS_PERMISSION_REQUESTS_NAME_CHILD,
        writes_crew_readable_transcript=backend in _CREW_READABLE_TRANSCRIPT_BACKENDS,
        honors_chat_steering_off=is_claude_backend_name(backend),
        crew_sends_persona_prompt=is_claude_backend_name(backend),
        loads_steering_natively=backend == ACP_BACKEND_KAS,
        keeps_prompt_on_unlanded_turn=backend in (ACP_BACKEND_KIRO, ACP_BACKEND_KAS),
        ends_turn_on_permission_reject=backend == ACP_BACKEND_OPENCODE,
        names_crew_tools_by_fused_name=backend == ACP_BACKEND_PI or is_claude_backend_name(backend),
        model_auto_switches_in_place=backend == ACP_BACKEND_KAS,
        harness_loads_agent_context=backend in (ACP_BACKEND_KIRO, ACP_BACKEND_KAS),
        reports_mcp_sign_in_suspects=backend == ACP_BACKEND_OPENCODE,
        crew_merges_user_hook_files=backend == ACP_BACKEND_KAS,
        keeps_session_across_compaction=is_claude_backend_name(backend),
        inherits_kiro_default_resources=backend == ACP_BACKEND_KIRO,
        has_todo_list_tool=backend == ACP_BACKEND_KIRO,
    )


#: The answer for a backend id this build does not know, and for a shape that is
#: not a provider at all.
#:
#: Not a sentinel a caller has to test for: it is a real record whose every
#: capability is withheld, so a consumer that never checks still takes the
#: conservative arm. Spelled out rather than derived from a fake id so the
#: fail-closed answer is readable here; ``test_agent_sdk_capabilities`` pins it
#: equal to what :func:`capabilities_for` returns for an unknown id, so the two
#: cannot drift.
#:
#: ``backend`` is the empty string because there is no id to report. That is also
#: the kiro backend's own spelling, so this record is NOT how you tell "unknown"
#: from "kiro" — nothing above the boundary may branch on that field anyway.
UNKNOWN_BACKEND_CAPABILITIES = SessionCapabilities(
    backend="",
    provider_seam=PROVIDER_ACP,
    model_id_namespace=MODEL_NAMESPACE_ACP,
    resolves_model_from_advertised_list=False,
    takes_model_per_session=False,
    effort_via_config_option=False,
    effort_via_slash_command=False,
    compacts_inline=False,
    crew_fires_spec_hooks=False,
    crew_fires_spec_observer_hooks=False,
    supports_native_todos=False,
    keeps_own_checklist=False,
    runs_own_slash_commands=False,
    honors_zero_tool_ban=False,
    acp_client_spawnable=False,
    honors_remote_mcp_oauth=False,
    native_workflows=False,
    refines_tool_call_titles=False,
    permission_requests_name_child=False,
    writes_crew_readable_transcript=False,
    honors_chat_steering_off=False,
    crew_sends_persona_prompt=False,
    loads_steering_natively=False,
    keeps_prompt_on_unlanded_turn=False,
    ends_turn_on_permission_reject=False,
    names_crew_tools_by_fused_name=False,
    model_auto_switches_in_place=False,
    harness_loads_agent_context=False,
    reports_mcp_sign_in_suspects=False,
    crew_merges_user_hook_files=False,
    keeps_session_across_compaction=False,
    inherits_kiro_default_resources=False,
    has_todo_list_tool=False,
)


def capabilities_of(provider: object) -> SessionCapabilities:
    """The capabilities of the backend *provider* is talking to.

    Accepts any shape and answers :data:`UNKNOWN_BACKEND_CAPABILITIES` for one
    that does not carry a real :class:`SessionCapabilities`. That is the calling
    convention the seven predicates this replaces already had — they were
    ``isinstance``-gated and answered False off a foreign shape — and it is what
    keeps a wrapper, an unstarted provider or a test double from reading as a
    member of every capability at once.
    """
    caps = getattr(provider, "capabilities", None)
    return caps if isinstance(caps, SessionCapabilities) else UNKNOWN_BACKEND_CAPABILITIES


def capture_prompt_timeout(provider: object, deadline: float) -> float | None:
    """Resolve a provider-declared transport budget for one outer deadline.

    Capability ownership is read from the provider TYPE, matching the billing
    seam: an instance-only attribute on a loose mock cannot opt in by accident.
    A legacy duck provider declares nothing and returns ``None``, which tells the
    caller to preserve its one-argument ``stream(message)`` invocation.
    """
    resolver = getattr(type(provider), "prompt_timeout_for_deadline", None)
    if not callable(resolver):
        return None
    timeout = resolver(provider, deadline)
    return float(timeout) if timeout is not None else None


__all__ = [
    "MODEL_NAMESPACE_ACP",
    "MODEL_NAMESPACE_CLAUDE_CODE",
    "SessionCapabilities",
    "UNKNOWN_BACKEND_CAPABILITIES",
    "capabilities_for",
    "capabilities_of",
    "capture_prompt_timeout",
]
