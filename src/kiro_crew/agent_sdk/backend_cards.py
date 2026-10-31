"""What each harness can do, projected from the memberships it already declared.

The Settings > Agent Harness switch asks one question this module answers: what
does an operator lose or gain by picking this harness rather than that one? Every
answer here is a PROJECTION over :mod:`kiro_crew.agent_sdk.backends` -- the
capability sets, :data:`~kiro_crew.agent_sdk.backends.ACP_BACKEND_ROUTING`, and
each harness's auth declaration in :mod:`kiro_crew.agent_sdk.host_auth`. Nothing
here is authored per harness, and there is no ``if backend ==`` anywhere in the
file: a harness that joins a set gains its line, and a harness this build has
never heard of renders a complete card the moment its id is in
``ACP_BACKENDS_KNOWN``.

Three levels, and the third one is DECLARED rather than projected
----------------------------------------------------------------
A card line is AVAILABLE, NOT AVAILABLE, or NOT MEASURED. The first two are the
projection, and a ``frozenset`` is why they are only two: it carries one bit -- in,
or out -- so nothing about a non-member is legible in the source data, and three
reasons a reader wants told apart collapse into one absence: cannot do it, does it
differently, and nobody has measured it. ``ACP_BACKENDS_MEMBER_DISPATCH`` shows the
collapse: kiro is a non-member because it has no per-session channel at all, and
KAS is a member on a captured mount.

The third level is NOT derived from the sets, because it is not in them. It is
:data:`DECLARED_UNMEASURED`, a per-harness per-line table carrying a reason per
entry, and it is the one thing in this module authored per harness. That is a
deliberate exception to the rule the rest of the file keeps, taken for the one fact
the rule cannot reach: "nobody has driven this yet" is a statement about Crew's own
EVIDENCE rather than about the harness, it exists today only as prose in a set's
comment, and no bit anywhere carries it. So the alternative to declaring it is not
deriving it: it is a two-level card, where a set that excludes a harness for want
of a driven capture wears the same mark as a harness with no such surface at all. Those two answers are not one answer, and a reader of two levels cannot see
which cell a measurement would pay for.

Five things keep the exception from growing into the per-harness table this
projection exists to remove:

* an entry is admissible ONLY where the deciding set's own comment in its ``backends_*`` owner
  says the gap is evidence. ``test_backend_cards`` reads that comment and fails an
  entry the vocabulary does not support, so the table cannot drift away from the
  prose and the threshold is a gate rather than a convention to remember;
* "a decision is missing" is not this state. A harness no decision has admitted to
  a set does not have the feature -- H6 carries nothing over from another member --
  so NOT AVAILABLE is the true mark. Unmeasured is
  for a line whose answer is unknown, never for one whose answer is no;
* an entry may not contradict the projection. A member has demonstrated the
  capability, so :func:`_card_lines` applies an entry only to a line that is already
  not available, and a test fails an entry that names a member rather than letting a
  table overrule a membership;
* it is fail-closed on the wire and counts as neither on the card: ``available`` stays
  a bool and answers False for an unmeasured line, so a consumer reading that field
  alone -- anything predating ``measured`` -- gets the honest not-available answer and
  never a promise;
* a new harness still costs no edit here. Every line of every harness this table does
  not name is measured, so onboarding one renders a complete card exactly as before.

``ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD`` is the near miss worth naming, and it stays
off-card: its answer is KNOWN and version-gated per process, so a pre-session
projection that holds no version cannot answer it in any of the three levels.
Unmeasured would say nobody looked, and somebody did.

The ONE fact that is genuinely graded already carries its own grade:
:class:`~kiro_crew.agent_sdk.backends.Routing` names five mechanisms and one
"not established", each with a documented meaning and a fail-closed default. It is
rendered as data (:attr:`BackendCard.tool_approval`) rather than folded into a
boolean.

Which memberships reach a card, and how
--------------------------------------
Five buckets, and every ``ACP_BACKENDS_*`` set in the vocabulary module reaches at
least one of them (``test_backend_cards`` fails when a new set reaches none, so
adding a set forces the decision rather than defaulting it):

* **user-facing** -- switching harness changes what the user can DO: a control
  appears or disappears, a command is refused, tools are absent from a session, a
  chat cannot be continued. These are :data:`USER_FACING_LINES`, and each is a
  capability whose ABSENCE is a loss, because that is what an available /
  not-available mark means to a reader. It is a CURATED core of seven, the only
  lines the card's "supports N of M" counts, so the set stays small enough to
  hold in one's head and a count compares like with like.
* **detail** -- a real capability whose absence is a loss, but not one a user
  picks a harness by: how a saved spec loads, an agent-side orchestration verb,
  an agent-file format. These are :data:`DETAIL_LINES`, carried on the wire as
  ``details``, folded behind a disclosure on the panel and outside the count.
* **security** -- it changes which layer confines the agent, whether Crew hands
  its own credential to the child, or how an unclassifiable approval is answered.
  These are :data:`SECURITY_LINES`. They are stated only when they HOLD and are
  rendered OUTSIDE any disclosure, next to the tool-approval line: "Crew's sandbox
  is not confining this child" is at least as material as how the harness is made
  to ask, and a fact behind a closed disclosure is a fact an operator comparing
  harnesses does not see.
* **operator** -- it says where something LIVES: whose disk holds the transcript,
  which side supplies the model list, which channel carries a command. These are
  :data:`OPERATOR_LINES`, also stated only when they hold, because a caveat is
  either raised or absent -- "this harness does not relocate its home on a pod" is
  not information anyone reads.
* **off-card** -- the only difference the membership makes is which code path
  runs, with the same observable product behaviour when both paths are correct.
  The test for this bucket: if the membership were wrong, would the user see a
  missing feature, or a bug? A wrong ``ACP_BACKENDS_SESSION_SHARING`` costs a user
  ``spawn_continue``. A wrong ``ACP_BACKENDS_SEED_LOCAL_SETTINGS`` is a stale
  model, i.e. a defect. Defects are not capabilities, so they stay off the card --
  see :data:`OFF_CARD_SETS`, which carries the reason per set.

``ACP_BACKENDS_KNOWN`` is in none of the four: it is the membership floor, and a
card existing at all already says it.

A membership whose two states are BOTH fine is a note, never a line. Three sets
are that kind, and each would otherwise put a red cross beside a harness that
loses nothing:

* which side owns the transcript -- Crew holds the kiro family's under its own
  sessions tree, so a reopened chat comes back either way;
* where the model list comes from -- Crew serves the kiro family's ids from its own
  registry;
* which channel carries a slash command -- ``ACP_BACKENDS_KIRO_SLASH_COMMANDS``
  names the ``_kiro.dev/commands/execute`` RPC, and a non-member is not
  command-less: opencode and pi both publish their own built-ins as an
  ``available_commands_update``. Read as "your slash commands stop working" it
  would be false for exactly those two, so the card states the CHANNEL as a note
  and claims nothing about a harness that carries its own.

A set may also inform a line it does not decide alone. Two lines are unions,
because no single set answers the question a user asks; a set that is a union
INPUT is not thereby classified, and no set decides more than one line on its own.

Why the sets are named as STRINGS here
--------------------------------------
Each line names its inputs by set NAME and resolves them through
:func:`_membership`. Two reasons, and the second is a live hazard rather than a
preference:

* it is what lets the completeness test compare what the vocabulary DEFINES
  against what this file classifies. Comparing the frozensets themselves cannot
  do it -- seven of them are equal to ``frozenset({""})`` today, so equality
  proves nothing about which name was meant.
* a value bound at import time does not follow a test that injects a synthetic
  harness by patching the DEFINING module (``test_member_memory_auth`` does
  exactly that). The patch would land on an attribute nothing reads, and the card
  would answer from production membership while the test believed otherwise.

The MCP half, which is a projection over another source
-------------------------------------------------------
One question an operator asks is not answerable from a membership set: what
happens to their AGENT SPEC on the way to this harness. That is declared per
backend in ``providers/mirrors`` -- the projection KIND, the reach of a per-tool
MCP restriction, and a disposition per spec concern -- and
:mod:`kiro_crew.agent_sdk.backend_mcp_ability` projects it. :func:`card_payload`
carries it as its own key so the panel and ``kirocrew doctor`` read one card, while
each projection stays a projection over ONE source.

Why this is not ``SessionCapabilities``, and not in ``backends.py``
------------------------------------------------------------------
:class:`~kiro_crew.agent_sdk.capabilities.SessionCapabilities` answers what one
LIVE session's harness can do, for consumers that must not branch on a harness id.
This module answers what an operator is choosing BETWEEN, before any session
exists, and it is read by exactly one consumer on a request path
(``dashboard/handlers/acp_backend_status``). Keeping them apart is why neither
grows the other's fields.

It cannot live in ``backends.py`` either: the ``vocabulary-home`` ratchet
(``scripts/check_harness_parity.py``, H8) reserves the ``ACP_BACKENDS_*`` spelling
for that module and its owners, so a projection over those sets is a function in a module of its
own -- the same shape, and for the same reason, as
``host_auth.backends_retired_by_host_logout()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

from kiro_crew.agent_sdk import backends
from kiro_crew.agent_sdk.backend_mcp_ability import ability_payload

# ── Card line ids ──
# Stable machine keys. The LABEL for each is the dashboard's, keyed off the id, so
# a line is phrased once per CAPABILITY and every harness reuses that phrasing --
# which is the whole reason a new harness costs no locale edit.

#: Crew's own MCP tools are reachable from a session on this harness.
LINE_CREW_TOOLS = "crew_tools"
#: A crew-member chat gets the session-control tools mounted.
LINE_MEMBER_THREAD_TOOLS = "member_thread_tools"
#: An enrolled member's saved spec is loaded or projected at spawn.
LINE_MEMBER_SAVED_AGENT = "member_saved_agent"
#: A Side Chat turn may execute read-only tools.
LINE_SIDE_CHAT_TOOLS = "side_chat_tools"
#: A subagent chat survives teardown, so it can be continued later.
LINE_SUBAGENT_CONTINUATION = "subagent_continuation"
#: A message can be added to a turn that is already running.
LINE_MID_TURN_STEER = "mid_turn_steer"
#: The manual ``/compact`` command is offered.
LINE_MANUAL_COMPACT = "manual_compact"
#: Reasoning effort can be changed on a live session.
LINE_REASONING_EFFORT = "reasoning_effort"
#: The model can be switched on a live session.
LINE_MODEL_SWITCH = "model_switch"
#: An agent written as one markdown file is loaded by this harness.
LINE_MARKDOWN_AGENTS = "markdown_agents"

#: Crew's own sandbox stands down for this harness's child process.
NOTE_CREW_SANDBOX_STANDS_DOWN = "crew_sandbox_stands_down"
#: An approval this harness cannot classify is refused rather than answered.
NOTE_REFUSES_UNCLASSIFIED_TOOLS = "refuses_unclassified_tools"
#: Crew answers this harness's child from its own credential vault.
NOTE_HOST_CREDENTIAL_TO_CHILD = "host_credential_to_child"
#: On a pod, this harness's ``$HOME`` is relocated onto the pod tree.
NOTE_POD_HOME_RELOCATED = "pod_home_relocated"
#: The harness holds its own entitlement, so it signs in separately.
NOTE_OWN_CREDENTIAL_STORE = "own_credential_store"
#: The harness keeps its own chat record, and a reopen restores from it.
#: Off the card, in ``kirocrew doctor`` and ``providers/mirrors/README.md`` only:
#: whose disk the transcript sits on. Crew holds a non-member's transcript under its
#: own sessions tree and a reopened chat restores from there, so the conversation
#: comes back either way -- no feature lost, no risk taken, no setting of the
#: reader's stopped working.
NOTE_KEEPS_OWN_CHAT_RECORD = "keeps_own_chat_record"
#: The model list comes from the harness's own advertised select.
#: Off the card, on the same terms: which registry fills the model picker. A
#: non-member's list comes from Crew's own registry, which is a SOURCE rather than a
#: shortfall -- the same models are offered and the same switch works. The agent
#: file's own ``availableModels`` is a different question and IS on the card, as an
#: ineffective setting where it holds.
NOTE_HARNESS_MODEL_LIST = "harness_model_list"
#: Slash commands travel on Crew's own command channel.
#: Off the card, on the same terms: which channel carries a slash command. A
#: non-member is not command-less -- opencode and pi publish their own built-ins as
#: an ``available_commands_update`` -- so the difference is the channel rather than
#: the feature. Its input set stays a capability-line input (the reasoning-effort
#: line's slash-command channel), which is why the decision is recorded here rather
#: than in :data:`OFF_CARD_SETS`.
NOTE_CREW_COMMAND_CHANNEL = "crew_command_channel"

#: The auth declaration, rather than a membership set, decides a line.
_FROM_AUTH_DECLARATION = "auth_declaration"


@dataclass(frozen=True)
class _LineSpec:
    """One card line and the memberships that decide it.

    ``sets`` is a UNION: a line is available when the harness is in any of them.
    Two lines need one, and the union is a human judgement rather than a
    mechanical join, so it is written down here with its reason on the entry.
    """

    id: str
    sets: Tuple[str, ...]


#: The CORE lines: the curated set an operator choosing a harness reads, in the
#: order the card renders, and the only lines its "supports N of M" counts.
#:
#: Small on purpose, so a reader can hold the whole set in their head: does Crew
#: work here (its tools, a crewmate's chat), what can a chat do (Side Chat tools,
#: steer a running turn, /compact), and what can be tuned (effort, model). A
#: capability that is real but not one a user picks a harness by belongs in
#: :data:`DETAIL_LINES` instead. Server-owned order, so a new line appears in the
#: right place with no frontend edit.
USER_FACING_LINES: Tuple[_LineSpec, ...] = (
    # Two channels carry Crew's tools, and a harness needs only one of them.
    # Members of the per-session array receive the server list on ``session/new``;
    # the kiro family is handed ``--agent`` and loads Crew's spec (its MCP servers
    # included) itself, which is exactly why they are absent from that array. A
    # harness in NEITHER has none of Crew's tools in its sessions, which is the
    # single most consequential thing this card says.
    #
    # ``ACP_BACKENDS_KIRO_FAMILY`` names that family here. The runtime set does
    # NOT: codex is served by the shared runtime for TRANSPORT reasons while reading
    # none of Crew's agent spec, so a line keyed on it would hand codex this claim
    # by the wrong route.
    _LineSpec(LINE_CREW_TOOLS, ("ACP_BACKENDS_SESSION_MCP_ARRAY", "ACP_BACKENDS_KIRO_FAMILY")),
    _LineSpec(LINE_MEMBER_THREAD_TOOLS, ("ACP_BACKENDS_MEMBER_DISPATCH",)),
    _LineSpec(LINE_SIDE_CHAT_TOOLS, ("ACP_BACKENDS_SIDE_READONLY",)),
    # Three verbs carry a user's mid-turn message: kiro-cli's ``_session/steer``,
    # codex-acp's ``_session/steering``, and opencode's second ``session/prompt``
    # that joins the running turn. A user asks "can I add to a running turn?", and
    # any of them answers yes, so the line is their union.
    _LineSpec(
        LINE_MID_TURN_STEER,
        (
            "ACP_BACKENDS_STEER",
            "ACP_BACKENDS_STEERING_REQUEST",
            "ACP_BACKENDS_STEER_CONCURRENT_PROMPT",
        ),
    ),
    _LineSpec(LINE_MANUAL_COMPACT, ("ACP_BACKENDS_COMPACT",)),
    # Effort travels down one of two channels, and neither set alone answers the
    # question a user asks. The config-option members advertise an ``effort``
    # option; kiro-cli changes effort by the ``/effort`` slash command and is absent
    # from that set entirely, so reading the config set alone would report the
    # default harness as unable to do what it has always done. KAS takes effort as
    # a config option, so it answers through the first set.
    _LineSpec(
        LINE_REASONING_EFFORT,
        ("ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"),
    ),
    # Same shape for the model: the config-option members take a switch as
    # ``session/set_config_option``, and the kiro family takes it as the native
    # set-model request -- so the kiro-family set stands for it, as on the first
    # line above.
    _LineSpec(
        LINE_MODEL_SWITCH,
        ("ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION", "ACP_BACKENDS_KIRO_FAMILY"),
    ),
)

#: Capabilities a harness may differ on that are NOT on the curated card.
#:
#: Each is a real capability -- a membership whose absence is a loss -- but not one a
#: user weighs when picking a harness: it is a detail of how a saved spec loads, an
#: agent-side orchestration verb, or an agent-file format. Kept as lines so the
#: panel can show them behind a folded disclosure, and kept OUT of the card's
#: count so "supports N of M" is a number over the core set alone.
DETAIL_LINES: Tuple[_LineSpec, ...] = (
    # Whether an enrolled member's saved spec is loaded (kiro-cli, KAS) or its
    # projection confirmed (Claude, Codex, DeepSeek) at spawn. The label says
    # "saved agent", not "whole": a projecting backend reports some fields as
    # gaps, and the card's "will not take effect" list names them. The question
    # a user asks -- does a crewmate get Crew's tools here? -- is the
    # member-thread line on the card; this one says how the spec arrives.
    _LineSpec(LINE_MEMBER_SAVED_AGENT, ("ACP_BACKENDS_MEMBER_CAPABILITIES",)),
    # ``spawn_continue`` is a verb the AGENT calls, not a control the user
    # presses, and a non-member's subagents get dedicated sessions, which the
    # set's own comment calls working behaviour rather than a degraded one.
    _LineSpec(LINE_SUBAGENT_CONTINUATION, ("ACP_BACKENDS_SESSION_SHARING",)),
    # An agent-file FORMAT: every harness loads the JSON spec Crew writes.
    _LineSpec(LINE_MARKDOWN_AGENTS, ("ACP_BACKENDS_MARKDOWN_AGENT_SPECS",)),
)

#: The facts that change what confines the agent or who holds a credential.
#:
#: Rendered outside any disclosure. Each is stated only when it holds, and each
#: names a control the operator is choosing rather than a feature they gain: two
#: of the four (the sandbox stand-down, and Crew handing over its credential)
#: describe a boundary moving, and the sandbox one fails OPEN by design.
SECURITY_LINES: Tuple[_LineSpec, ...] = (
    # A union: KAS's default relay route is the kiro-cli process and stands Crew's
    # sandbox down exactly as kiro-cli does. Its bundle route does not, and a card
    # cannot show a per-route fact, so the line errs toward naming the boundary
    # that can move rather than hiding it on the route most KAS sessions take.
    _LineSpec(
        NOTE_CREW_SANDBOX_STANDS_DOWN,
        ("ACP_BACKENDS_INTERNAL_SANDBOX", "ACP_BACKENDS_RELAY_INTERNAL_SANDBOX"),
    ),
    _LineSpec(NOTE_REFUSES_UNCLASSIFIED_TOOLS, ("ACP_BACKENDS_META_IDENTITY",)),
    _LineSpec(NOTE_HOST_CREDENTIAL_TO_CHILD, ("ACP_BACKENDS_HOST_AUTH_CALLBACK",)),
    _LineSpec(NOTE_POD_HOME_RELOCATED, ("ACP_BACKENDS_POD_HOME_REMAP",)),
)

#: The facts that say where something LIVES.
#:
#: Every one of these is a membership whose two states are both correct
#: behaviour, which is exactly why none of them is a capability line: a
#: not-available mark would report a loss where there is none.
OPERATOR_LINES: Tuple[_LineSpec, ...] = (
    # Credentials are the one where-it-lives fact that is also the reader's: whose
    # secret store a harness signs in against is a RISK they carry, not a route Crew
    # happens to take.
    _LineSpec(NOTE_OWN_CREDENTIAL_STORE, (_FROM_AUTH_DECLARATION,)),
)

#: Sets that reach no card line, each with the reason it does not.
#:
#: Read by the completeness test, so a set here is a recorded decision rather than
#: an omission. Every entry fails the same test: a wrong membership produces a
#: DEFECT rather than an absent feature, and a card that listed defect classes in
#: front of someone choosing a harness would be worse than one line shorter.
OFF_CARD_SETS: Mapping[str, str] = {
    "ACP_BACKENDS_MODEL_AT_LAUNCH": (
        "which channel carries a model pick: a launch argument instead of a wire "
        "request. The model-switch line already says whether a running session can move."
    ),
    "ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN": (
        "whether a server the user approved for a template Crew does not own reaches "
        "that template's sessions here. The approval card asks the same question for "
        "the session it is raised in and refuses with its own reason, so a card line "
        "would repeat a refusal the user already meets where it applies"
    ),
    "ACP_BACKENDS_MATE_WORKER_KIT": (
        "whether a crewmate's worker may run here with its kit. A worker always runs "
        "on its crewmate's backend, so the choice it depends on is the member "
        "backend, and a non-member refuses the hand-off with an error that names "
        "the backends that can carry it. Nothing runs without its kit, so a card "
        "line would describe a refusal the user meets with its own remedy"
    ),
    "ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT": (
        "which channel carries a crewmate worker's prompt: the context block or the "
        "wire spec. The worker gets the same prompt either way, so there is nothing "
        "for a user choosing a harness to weigh"
    ),
    "ACP_BACKENDS_STEER_RETRACT": (
        "whether a pending steer can be taken back (the dashboard's Unsend, Edit and "
        "Queue instead). The steer itself is the mid-turn-steer line's claim, and these "
        "controls are drawn only on a steer the running harness can take back, so no "
        "reader is promised an undo the harness lacks. A wrong membership offers a "
        "control whose clear names nothing, which leaves the steer to run, and is a "
        "defect"
    ),
    "ACP_BACKENDS_SERIAL_SESSION_STARTS": (
        "which backend's process answers session starts and mode switches one at a "
        "time. It decides only what a timed-out start's error says about the "
        "requests ahead of it; every request is sent the same way on every harness, "
        "so a reader choosing a harness loses nothing. A wrong membership makes that "
        "error claim a queue that was not there, or omit one that was, which is a "
        "defect"
    ),
    "ACP_BACKENDS_INDEPENDENT_SETUP": (
        "whether first-run setup may proceed without Kiro CLI. The onboarding gate "
        "already presents this choice before a session or Settings card exists; a "
        "wrong membership would send someone through a setup path that cannot work"
    ),
    "ACP_BACKENDS_MEMBER_PANEL": (
        "whether a member DM session may mount its own webview. Its membership is the "
        "same as ACP_BACKENDS_MEMBER_DISPATCH's, and the member-thread-tools line "
        "already answers for that one, so a card line here would repeat a claim the "
        "reader has read one line earlier and tell them nothing a harness differs on. "
        "The set exists so the DECISION is opted into per capability (harness-parity "
        "H6) rather than inherited from session control; if the two memberships ever "
        "diverge, that divergence is what earns a line"
    ),
    "ACP_BACKENDS_NATIVE_TODOS": (
        "whether the dashboard may forward the harness-native /todos command. The "
        "dashboard still renders agent-authored task state for every harness, so this "
        "membership selects a command-dispatch path rather than a task capability a "
        "reader loses by choosing another harness. A wrong membership forwards a "
        "command the acquired provider cannot serve, which is a defect"
    ),
    "ACP_BACKENDS_LOCAL_CLEAR": (
        "whether the dashboard answers /clear itself or hands it to the harness. "
        "/clear drops the conversation on every harness either way, so a reader "
        "choosing a harness loses nothing. A wrong membership either sends /clear "
        "to the model as text, or answers a harness that would have run it, which "
        "is a defect"
    ),
    "ACP_BACKENDS_LOCAL_HARNESS_COMMANDS": (
        "whether the dashboard answers every harness command itself and leaves the "
        "ones it cannot serve out of the / menu. It names a harness with no command "
        "channel rather than a feature a reader gains, and the commands that matter "
        "for choosing a harness already have lines of their own. A wrong membership "
        "either hides commands a harness would have run, or offers rows that only "
        "ever reach the model as text, which is a defect"
    ),
    "ACP_BACKENDS_HOOKS_LIST": (
        "which backend's agent may ask its client for the hooks matching a trigger, and "
        "have the client run one. Nothing a reader choosing a harness can act on: Crew "
        "does not announce the capability that makes an agent use the channel, so a "
        "member asks nothing and a non-member loses nothing. A wrong membership would "
        "answer a backend that never defined the channel, which is a defect rather "
        "than a shortfall"
    ),
    "ACP_BACKENDS_OPEN_EXTERNAL_URL": (
        "which backend's engine sends an MCP sign-in link to its client instead of "
        "running the OAuth flow itself. Both paths end at the same Authorize banner, "
        "so a reader choosing a harness loses nothing; a wrong membership would answer "
        "a request the backend never sends, or leave a KAS server unable to sign in, "
        "which is a defect"
    ),
    "ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS": (
        "who runs the agent spec's own hooks: the harness, or Crew's turn loop for a "
        "harness that never receives them. The hooks run either way, so a reader "
        "choosing a harness loses nothing; a wrong membership runs every hook twice "
        "or not at all, which is a defect"
    ),
    "ACP_BACKENDS_HARNESS_OWNED_SESSIONS": (
        "whose disk the transcript sits on. Crew holds a non-member's transcript under "
        "its own sessions tree and a reopened chat restores from there, so the "
        "conversation comes back either way -- the reader loses no feature, carries no "
        "new risk, and no setting of theirs stops working. Where it lives is in "
        "`kirocrew doctor` and `providers/mirrors/README.md`, for the reader diagnosing "
        "a session rather than choosing a harness"
    ),
    "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION": (
        "which registry fills the model picker. A non-member's list comes from Crew's "
        "own registry, which is a SOURCE rather than a shortfall: the same models are "
        "offered and the same switch works. The agent file's own `availableModels` is a "
        "different question, and it is on the card as an ineffective setting where it "
        "holds"
    ),
    "ACP_BACKENDS_ACP_RUNTIME": (
        "which transport starts a session: one shared process demuxed by AcpRuntime, or "
        "one process per session. The user gets a session either way; a wrong membership "
        "is a spawn that fails, which is a defect"
    ),
    "ACP_BACKENDS_SESSION_EVICTION": (
        "whether the teardown verb Crew sends disposes a session on the shared process. "
        "Invisible when right, a memory leak when wrong, which is a defect"
    ),
    "ACP_BACKENDS_MEMBER_TOOL_LIMITS": (
        "whether a saved member spec's per-tool tool switch-off is honoured. A "
        "non-member refuses such a spec before spawn with an error naming the fix, "
        "so nothing runs wider than it was saved; the card's saved-agent line already "
        "says whether the spec loads at all"
    ),
    "ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS": (
        "whether a crewmate worker's single-tool switch-offs are honoured. A worker "
        "with such limits on a non-member is refused before spawn with an error naming "
        "the fix, so it never runs wider than its crewmate saved; a worker without "
        "them runs, and the card's saved-agent line already says whether it loads"
    ),
    "ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE": (
        "which channel carries the agent spec's own servers to the session, read by the "
        "unresolved-ref detector. The servers arrive either way; a wrong membership is a "
        "false diagnostic, which is a defect"
    ),
    "ACP_BACKENDS_INLINE_COMPACTION": (
        "whether a manual /compact is awaited or immediate. The user sees /compact "
        "finish either way; a wrong membership is a hung wait, which is a defect"
    ),
    "ACP_BACKENDS_CONTEXT_RECYCLE": (
        "the other half of that split: whether a full context is answered by "
        "restarting the session. Same reasoning as its partner set, and the same "
        "defect in either direction -- a wrong membership either recycles a session "
        "that did not need it or leaves one growing into its own window. The card "
        "reports whether /compact works, which is what a reader choosing a harness "
        "acts on"
    ),
    "ACP_BACKENDS_UNMARKED_COMPACTION_PAIR": (
        "which frame shape the compaction parser reads; the compaction itself is "
        "the manual_compact line"
    ),
    "ACP_BACKENDS_HARNESS_MANAGED_COMPACTION": (
        "what answers a full context on a harness Crew cannot hand /compact to. The "
        "card already reports whether /compact works, which is the part a reader "
        "choosing a harness acts on, and both states of THIS set are correct "
        "behaviour for the harness they describe. A wrong membership is a defect in "
        "either direction -- claiming it leaves the context unbounded, withholding "
        "it recycles a session that did not need it -- and neither is an absent "
        "feature a card could mark"
    ),
    "ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION": (
        "which fact answers whether a session takes an effort level -- the option the "
        "harness advertised, or Crew's model registry. The card already reports whether "
        "effort can be changed on this harness at all, which is the part a reader "
        "choosing one acts on, and both states of THIS set are correct behaviour for the "
        "harness they describe. A wrong membership is a defect in either direction: "
        "asking the registry about a harness whose model ids it does not carry hides a "
        "control that works, and asking the option on a harness whose level rides the "
        "model offers one the model will refuse"
    ),
    "ACP_BACKENDS_AGENT_STARTED_TURNS": (
        "whether a turn the agent starts on its own between prompts is read when it "
        "happens. It is a transport detail of one adapter, not a choice a reader "
        "makes: a member missing from it shows that turn under the next message, and "
        "a harness added without the end-of-turn marker holds its slot until the "
        "stale-turn gate or the prompt timeout, so either wrong membership is a defect"
    ),
    "ACP_BACKENDS_SEED_LOCAL_SETTINGS": (
        "whether a settings file is re-seeded on a model switch. Invisible when "
        "right, a stale model when wrong"
    ),
    "ACP_BACKENDS_EFFORT_OVERLAY": (
        "which channel carries a reasoning-effort level to the engine at spawn -- the "
        "workspace cli.json overlay for this set, a config option or a slash command "
        "for the rest. The card's effort line already says whether effort can be "
        "changed, and every channel applies the level the user chose. A wrong "
        "membership is a defect: the level is written where the engine never reads "
        "it, or a session is started at a level another session left in that file"
    ),
    "ACP_BACKENDS_TOOL_SEARCH_OVERLAY": (
        "which channel carries the Tool Search setting to the engine -- the "
        "workspace cli.json overlay for this set, the initialize handshake for "
        "ACP_BACKENDS_CLIENT_META_SETTINGS. Both members honour the setting the "
        "user chose, so a non-member is not Tool-Search-less and an available mark "
        "would put a cross beside a harness that loses nothing. A wrong membership "
        "writes the value where the engine never reads it -- the dashboard shows "
        "the setting on while the engine runs with it off, which is a defect"
    ),
    "ACP_BACKENDS_CLIENT_META_SETTINGS": (
        "the other half of that split: taking feature settings from the ACP "
        "initialize request rather than from the overlay file. Same reasoning, and "
        "the two sets are complements over the same user-visible setting rather "
        "than two capabilities"
    ),
    "ACP_BACKENDS_LOAD_WITHOUT_MODES": (
        "tolerating a restore result that carries no modes block. Pure "
        "response-shape handling behind a restore that either works or does not"
    ),
    "ACP_BACKENDS_RESUME_WITHOUT_LOAD": (
        "which verb restores a session, and which capability advertises it. The "
        "user reopens a chat either way"
    ),
    "ACP_BACKENDS_FORK_AT_MESSAGE": (
        "whether an edited message's rewind forks the native session or replays "
        "the transcript as text. The edit runs either way"
    ),
    "ACP_BACKENDS_RELOAD_DISPOSED_SESSION": (
        "whether Crew reads one harness's own error for a session it disposed in a "
        "live process, and reloads that session in a fresh one before the next "
        "prompt. Recovery from a harness's failure mode, not a feature: a harness "
        "without that failure mode loses nothing by being outside the set"
    ),
    "ACP_BACKENDS_SELF_SERVED_ACP": (
        "whether a harness's whole launch is a row of ACP_BACKEND_LAUNCH, so the "
        "spawn path, the install probe and the driver seams resolve argv from that "
        "table. Which code path builds the command; the session starts either way, "
        "and a wrong membership is a launch that fails, which is a defect"
    ),
    "ACP_BACKENDS_STRUCTURED_REFUSAL": (
        "whether a refusal card gains a category line. Visible, and nothing a "
        "reader can act on or would pick a harness for"
    ),
    "ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR": (
        "how a harness that writes a turn's provider failure as message text is "
        "made to report it as a failure. Every harness ends such a turn on an error "
        "row once this is right, so a reader choosing a harness loses nothing; a "
        "wrong membership either shows a failure as an answer or an answer as a "
        "failure, which is a defect"
    ),
    "ACP_BACKENDS_REPORT_USAGE": (
        "whether a turn that reports no usage billed nothing, read by memory consolidation "
        "to decide whether an empty reply spends one of its attempts. The chat surfaces "
        "already show what each harness reports, so a reader choosing a harness loses "
        "nothing; a wrong membership either charges a throttled turn or retries a span "
        "without end, which is a defect"
    ),
    "ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL": (
        "whether a kindless permission frame takes its tool_call's kind so the "
        "gate's kind-keyed tiers see the harness's classification. Both states are "
        "the gate working as measured for that harness; a wrong membership is a "
        "vetoed read or an unrouted delete, which is a defect, not a feature the "
        "reader would choose a harness by"
    ),
    "ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS": (
        "whether an advertised <model>[<effort>] id is applied as two writes. The "
        "model switch either lands or is refused, which its own line already says"
    ),
    "ACP_BACKENDS_SPEC_FILE_GRANTS": (
        "whether a start on a crew-bound spec Kiro Crew does not own is refused "
        "while that file's grants exceed the ceiling, because the host reads them. "
        "Both states hold the governance ceiling, so it is no feature a reader "
        "would choose a harness for"
    ),
    "ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY": (
        "which directory a host resolves its agent specs from, and therefore "
        "whether the session's project checkout scopes the broker-overlay lookup. "
        "Both states are correct behaviour for the host that holds them, and "
        "neither is a feature a reader would choose a harness for: a member's "
        "sessions keep their brokered servers, a non-member's project agent gets "
        "the servers it actually declared. A wrong membership is a defect either "
        "way -- a project agent running the user-level agent's servers, or a "
        "member's servers dropping out of pool and caller-identity attribution "
        "while the operator has the gateway switched on"
    ),
    "ACP_BACKENDS_ACP_CLIENT_SPAWNABLE": (
        "which spawn path a pool that builds AcpClient itself may use for this "
        "backend, rather than AcpRuntime. A session starts either way when the "
        "caller asks the right path; a wrong membership is a spawn under the "
        "WRONG IDENTITY -- kas or codex under a bare kiro-cli spawn -- which is a "
        "defect, not an absent feature a reader choosing a harness acts on"
    ),
    "ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP": (
        "whether a session/new stdio element whose command cannot be found is left "
        "out and reported as a failed server, rather than handed to a harness that "
        "drops it silently or fails the whole session over it. A repair of a "
        "harness hazard on the start path, not a capability a reader choosing a "
        "harness gains or loses: the server is unusable either way, and the report "
        "already names it on every backend that sends the array"
    ),
    "ACP_BACKENDS_HONOR_ZERO_TOOL_BAN": (
        "whether an agent spec's `\"tools\": []` holds against this backend's "
        "NATIVE tools too, or only narrows the MCP allowlist. Read by the "
        "knowledge/research pool to pick a backend it can trust with a zero-tool "
        "worker. `tool_approval` already reports the reader-facing half -- whether "
        "and how this harness is made to ask before it runs a tool at all -- so a "
        "reader choosing a harness for that reason has already seen it; this set "
        "answers a narrower question the pool asks about, and a wrong membership "
        "admits a backend whose native tools stay live under a spec that meant to "
        "deny them, which is a defect"
    ),
    "ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD": (
        "whether a freshly installed MCP server reaches a RUNNING session. The one "
        "set whose membership this card cannot honestly project: it is version-gated "
        "per process by mcp_hot_reload_supported, and a pre-session projection holds "
        "no version -- so an available mark would promise a session the runtime "
        "still resets on a build below that floor, and two levels cannot say "
        "on a recent enough release"
    ),
    "ACP_BACKENDS_MCP_WIRE_REINJECT": (
        "the OTHER half of the same fact -- whether a freshly installed MCP server "
        "reaches a RUNNING session, here by Crew re-sending the projection rather "
        "than by the harness watching a file. Its membership is not version-gated, "
        "but a card that stated this half for KAS beside an off-card hot-reload set "
        "would read as kiro-cli lacking what it has; one fact needs one line, and "
        "that line cannot be projected while the other half cannot"
    ),
}

#: The set that is the membership floor rather than a capability.
#:
#: A card exists for every member, so its own membership is already stated by the
#: card being there at all.
MEMBERSHIP_FLOOR_SET = "ACP_BACKENDS_KNOWN"

#: The set that marks the kiro family, for the union lines that stand on it.
#:
#: Two lines read it as "the family that loads Crew's agent spec and takes the
#: native set-model request". The runtime set cannot stand in for it: it is a
#: TRANSPORT property, and codex sits in it while reading no agent spec of Crew's.
#: Nor can the command-verb set: KAS refuses that verb but is in the family. A test
#: holds both lines to reading THIS set.
KIRO_FAMILY_MARKER_SET = "ACP_BACKENDS_KIRO_FAMILY"

#: A capability the harness's own source says it serves, which no live run has
#: confirmed -- the evidence CLASS the deciding set holds its members to.
#:
#: The panel keys a label off this code, which is why it is a code and not the
#: sentence: the prose on the entry below is English source commentary, and the
#: reader's wording belongs in the locale catalogs like every other card string.
REASON_NO_DRIVEN_CAPTURE = "no_driven_capture"


@dataclass(frozen=True)
class UnmeasuredLine:
    """Why one harness's one line has no answer yet, and who says so."""

    #: The machine reason code the panel translates. One of the ``REASON_*`` above.
    reason: str

    #: The ``ACP_BACKENDS_*`` set whose own comment establishes the gap. Named as a
    #: string for the reasons the module docstring gives, and read by the test that
    #: holds every entry to that comment.
    declared_by: str

    #: What that comment says, short enough to check against it. Not sent on the
    #: wire: it is the admissibility evidence for the entry, for a reader here.
    citation: str


#: The cells where Crew has no ANSWER, declared per harness with a reason each.
#:
#: Keyed by (harness constant NAME, card line id). The harness is named rather than
#: valued for the same two reasons the sets are: ``ACP_BACKEND_KIRO`` is the empty
#: string so a literal id is illegible, and a renamed constant raises here instead of
#: quietly reading as a harness nobody has.
#:
#: The table is empty: every core line of every known harness has an answer. An
#: entry is added only when a deciding set's own comment names the harness and says
#: the gap is missing evidence, never missing capability.
#:
#: USER-FACING lines only, which is not an omission: the security and operator notes
#: are stated only when they HOLD, so an unmeasured one is already absent rather than
#: crossed out, and a third level would have nothing to correct.
DECLARED_UNMEASURED: Mapping[Tuple[str, str], UnmeasuredLine] = {}

#: Card lines the runtime also gates on a field the adapter advertises, with that
#: field. For these lines a membership set is not the whole answer: the runtime
#: refuses the line when the session's handshake lacks the field.
#:
#: ``test_backend_cards_advertised`` counts the runtime sites that gate one of these
#: lines on a deciding set instead, against a shrink-only allowlist, so a new site
#: reads the field rather than adding another set test.
ADVERTISED_FIELD_BY_LINE: Mapping[str, str] = {
    # claude only, in ``acp.client_turn_control._steering_request_ready``.
    LINE_MID_TURN_STEER: "initialize._meta.steering.supported",
    # KAS only, in ``acp.session_handle_compaction._kas_compact_advertised``.
    LINE_MANUAL_COMPACT: "initialize.agentCapabilities._meta.kiro.extensionMethods",
    # The effort option's values, in ``acp.client_model_selection.get_valid_effort_levels``.
    LINE_REASONING_EFFORT: "session.configOptions[effort option].options",
    # The model select, in ``acp.session_handle_model_selection.model_select_envelope``.
    LINE_MODEL_SWITCH: "session.configOptions[model] or session.models.availableModels",
}

#: The (harness, line) cells whose runtime gate is the line's ``initialize`` field.
#:
#: Keyed by harness constant NAME, as :data:`DECLARED_UNMEASURED` is and for the same
#: reasons. Only the two ``_session/steering`` speakers: claude through
#: ``AcpClient._steering_request_ready``, codex through ``AcpSessionHandle``. Both
#: refuse the steer when their handshake lacks the field, so :func:`card_for`, given
#: that handshake, says the same. kiro-cli, KAS, goose, pi and opencode steer on a
#: verb that advertises nothing, so their membership stays the whole answer.
ADVERTISED_BY_HARNESS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("ACP_BACKEND_CLAUDE", LINE_MID_TURN_STEER),
        ("ACP_BACKEND_CODEX", LINE_MID_TURN_STEER),
    }
)


@dataclass(frozen=True)
class CardLine:
    """One user-facing line: what it is, and whether this harness has it."""

    id: str
    available: bool

    #: Whether an answer EXISTS for this harness on this line. False only where
    #: :data:`DECLARED_UNMEASURED` says so, never inferred: a plain non-member is
    #: measured, and its absence is a real absence.
    #:
    #: :attr:`available` is False whenever this is False, so the pair has no state
    #: that reads as a promise and a consumer of the bool alone is not misled.
    measured: bool = True

    #: The reason code for an unmeasured line, ``""`` otherwise.
    unmeasured_reason: str = ""


@dataclass(frozen=True)
class BackendCard:
    """Everything the switch can say about one harness without authoring prose."""

    #: The ``agent.acp_backend`` id. ``""`` is kiro-cli.
    backend: str

    #: The policy-facing spelling, which is also the panel's name fallback.
    policy_id: str

    #: Every core line, in server order, available or not -- or, for the
    #: cells :data:`DECLARED_UNMEASURED` names, not measured.
    capabilities: Tuple[CardLine, ...]

    #: Every :data:`DETAIL_LINES` line, same three states, never in the count.
    details: Tuple[CardLine, ...]

    #: The ids of the SECURITY notes that hold, in server order. Rendered beside
    #: :attr:`tool_approval` rather than behind a disclosure.
    security_notes: Tuple[str, ...]

    #: The ids of the where-it-lives notes that hold, in server order.
    operator_notes: Tuple[str, ...]

    #: How this harness is made to ask before it runs a tool, as
    #: :class:`~kiro_crew.agent_sdk.backends.Routing`'s own value. The one graded
    #: line on the card, and the most security-relevant single fact on it.
    tool_approval: str

    #: Whether the BUILD offers this harness as a choice at all, before any
    #: deployment policy narrows the set.
    #:
    #: A known id outside the baseline is one this build can SPELL -- so a
    #: governance rule can name it -- and will not start a session with. That is a
    #: different state from an id a deployment policy denied, and the panel needs
    #: the difference: a policy denial is not the reader's to fix, while a build
    #: exclusion is a standing fact about the harness.
    #:
    #: It does NOT carry the reason, because the reason is not one thing. A harness
    #: can be outside the baseline because ``register_selectable_backend`` refuses
    #: an ``UNVERIFIED`` routing -- in which case :attr:`tool_approval` is the
    #: reason and says so -- or because no edition ever registered it, which
    #: nothing here can distinguish from the first case.
    offered_by_build: bool


def _membership(name: str) -> FrozenSet[str]:
    """The vocabulary module's set called *name*.

    Resolved by name, and read through the module on every call, for the two
    reasons the module docstring gives. A name no set answers to raises here
    rather than quietly reading as "no harness has this".
    """
    return frozenset(getattr(backends, name))


def _policy_id(backend: str) -> str:
    """*backend*'s policy-facing spelling, falling back to the id itself."""
    return backends.POLICY_ID_BY_BACKEND.get(backend, backend)


def _holds(backend: str, spec: _LineSpec) -> bool:
    """Whether *spec* is true of *backend*: membership in ANY of its sets."""
    for name in spec.sets:
        if name == _FROM_AUTH_DECLARATION:
            # Imported in the function body, not at module scope. ``host_auth`` is
            # read during the credential floor's own construction, and a leaf that
            # security code builds from is one to add no import edges to from a
            # display projection.
            from kiro_crew.agent_sdk.host_auth import (
                UNKNOWN_AGENT_AUTH,
                declaration_for,
                signs_in_separately,
            )

            # The sentinel check is what keeps this note evidence-based, and it is
            # not redundant. ``declaration_for`` answers with UNKNOWN_AGENT_AUTH for
            # an id it cannot name, and that sentinel's entitlement source is the
            # own-credential-file one -- so ``signs_in_separately`` alone reads True
            # for a harness nothing has been declared about, and the card would
            # assert where it signs in on no evidence at all. Identity against the
            # sentinel rather than its ``backend`` field, which is the empty string
            # and therefore indistinguishable from kiro-cli's own id.
            #
            # Reachable during a real onboarding rather than only in a test: a
            # harness joins ``ACP_BACKENDS_KNOWN`` at Stage 1 and gets its auth
            # declaration at Stage 5, and every card in between is served.
            if declaration_for(backend) is UNKNOWN_AGENT_AUTH:
                continue
            if signs_in_separately(backend):
                return True
            continue
        if backend in _membership(name):
            return True
    return False


def _unmeasured_lines(backend: str) -> Dict[str, UnmeasuredLine]:
    """The declared-unmeasured entries that apply to *backend*, by line id.

    A dict built per call and indexed by harness id, rather than a comparison
    against one: the projection may not test WHICH harness it is describing (a test
    reads this file's AST for exactly that), and an index answers the question
    without the shape that would make the next harness an edit here.

    Each harness NAME is resolved through the vocabulary module on every call, as
    :func:`_membership` resolves a set name and for the same two reasons -- a
    renamed constant raises rather than silently naming nobody, and a test that
    injects a synthetic harness by patching the defining module is followed.
    """
    by_backend: Dict[str, Dict[str, UnmeasuredLine]] = {}
    for (harness_name, line_id), entry in DECLARED_UNMEASURED.items():
        by_backend.setdefault(getattr(backends, harness_name), {})[line_id] = entry
    return by_backend.get(backend, {})


def _advertised(init: Mapping[str, object], wire_path: str) -> bool:
    """Whether *init* carries a literal ``true`` at *wire_path*.

    *wire_path* is an ``initialize.``-rooted dotted path from
    :data:`ADVERTISED_FIELD_BY_LINE`. Any missing step, non-object step or value
    other than ``True`` reads False, the runtime's own fail-closed reading.
    """
    node: object = init
    for key in wire_path.split(".")[1:]:
        node = node.get(key) if isinstance(node, dict) else None
    return node is True


def _gated_lines(backend: str) -> FrozenSet[str]:
    """The line ids :data:`ADVERTISED_BY_HARNESS` gates for *backend*.

    Resolved by name on every call, as :func:`_unmeasured_lines` resolves its keys.
    """
    by_backend: Dict[str, set] = {}
    for harness_name, line_id in ADVERTISED_BY_HARNESS:
        by_backend.setdefault(getattr(backends, harness_name), set()).add(line_id)
    return frozenset(by_backend.get(backend, ()))


def _card_lines(
    backend: str,
    specs: Tuple[_LineSpec, ...],
    init: Optional[Mapping[str, object]] = None,
) -> Tuple[CardLine, ...]:
    """Every line of *specs* for *backend*, in server order.

    Membership decides first and the declared table only ever SOFTENS a negative:
    an entry reaches a line that is already not available, so a harness that has
    demonstrated the capability keeps its available mark whatever the table says.
    That ordering is what stops a stale entry from withdrawing a real capability;
    the entry being stale at all is separately a test failure.

    With *init*, a member line in :data:`ADVERTISED_BY_HARNESS` also needs its
    handshake field. A line that field withdraws is measured: the session's own
    handshake is the measurement.
    """
    declared = _unmeasured_lines(backend)
    gated = _gated_lines(backend) if init is not None else frozenset()
    produced = []
    for spec in specs:
        held = _holds(backend, spec)
        available = held
        if held and spec.id in gated:
            available = _advertised(init or {}, ADVERTISED_FIELD_BY_LINE[spec.id])
        entry = None if held else declared.get(spec.id)
        produced.append(
            CardLine(
                id=spec.id,
                available=available,
                measured=entry is None,
                unmeasured_reason="" if entry is None else entry.reason,
            )
        )
    return tuple(produced)


def card_for(backend: str, init: Optional[Mapping[str, object]] = None) -> BackendCard:
    """The card for *backend*, complete for any id -- known to this build or not.

    *init* is a live session's ``initialize`` result. Given one, a line the runtime
    gates on a handshake field (:data:`ADVERTISED_BY_HARNESS`) is available only
    when membership says so AND that handshake carries the field. Without one the
    card is membership alone, which is what ``GET /api/acp-backends`` serves.

    Total: no raise, no I/O, no dependence on a live session. An id outside every
    set answers False on every capability line and ``unverified`` on tool
    approval, which is the fail-closed reading of "this build establishes nothing
    about it" rather than an absent case. It is also MEASURED on every line, which
    is the same reading: an id nothing has been declared about has no evidence gap
    recorded either, and unmeasured is a claim about Crew's own measurements rather
    than a synonym for "unknown harness".

    Every field but one is a pure read of frozen membership.
    :attr:`BackendCard.offered_by_build` is not: it reads the selectable registry,
    which an edition writes at bootstrap, so a card built before that runs reports
    an edition's harness as not offered.
    """
    return BackendCard(
        backend=backend,
        policy_id=_policy_id(backend),
        capabilities=_card_lines(backend, USER_FACING_LINES, init),
        details=_card_lines(backend, DETAIL_LINES, init),
        security_notes=tuple(spec.id for spec in SECURITY_LINES if _holds(backend, spec)),
        operator_notes=tuple(spec.id for spec in OPERATOR_LINES if _holds(backend, spec)),
        tool_approval=backends.routing_for(backend).value,
        offered_by_build=backend in backends.registered_backends(),
    )


def _line_payload(line: CardLine) -> Dict[str, object]:
    """One card line as JSON."""
    return {
        "id": line.id,
        # Still a BOOL, and still first. An unmeasured line answers False here,
        # so a reader that predates the two fields beside it -- an older panel
        # against a newer gateway -- gets the fail-closed not-available answer
        # rather than inheriting a third state it has no way to render.
        "available": line.available,
        "measured": line.measured,
        "unmeasured_reason": line.unmeasured_reason,
    }


def card_payload(backend: str) -> Dict[str, object]:
    """*backend*'s card as the JSON shape ``GET /api/acp-backends`` sends.

    Built here rather than in the handler so the projection owns its own wire
    shape: the classification into capabilities, security notes and operator notes
    is this module's judgement, and a handler assembling it field by field would be
    a second place that could disagree about which bucket a line is in -- which for
    the security notes decides whether a reader has to open a disclosure to see
    them.
    """
    card = card_for(backend)
    return {
        "capabilities": [_line_payload(line) for line in card.capabilities],
        # Same shape as ``capabilities`` and outside its count: the panel folds
        # these behind a disclosure, and a gateway that predates them sends none.
        "details": [_line_payload(line) for line in card.details],
        "security_notes": list(card.security_notes),
        "operator_notes": list(card.operator_notes),
        # The routing value itself, not a record around it. A second reading of the
        # routing (which mechanisms this core ENFORCES) would be a second FIELD when
        # it has a consumer; wrapping this one against that day ships a shape no
        # reader needs and a shipped gateway cannot withdraw.
        "tool_approval": card.tool_approval,
        "offered_by_build": card.offered_by_build,
        # Its own GROUP rather than these further flat keys: the answer one
        # question together (how the agent spec reaches this harness), they come
        # from one source, and a reader on an older gateway gets an absent object
        # it can test once instead of four fields it has to test apart.
        "mcp": ability_payload(backend),
    }
