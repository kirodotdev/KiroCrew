"""What each harness can do, projected from the memberships it already declared.

The Developer > Agent Backend switch asks one question this module answers: what
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
collapse: pi is a non-member because a mount there is INERT (the array is accepted
and never forwarded), deepseek is a non-member on a deliberate refusal -- it has the
mount, and its routing sits outside ``tool_gate.ENFORCED_ROUTINGS`` -- and KAS is a
member on a captured mount.

The third level is NOT derived from the sets, because it is not in them. It is
:data:`DECLARED_UNMEASURED`, a per-harness per-line table carrying a reason per
entry, and it is the one thing in this module authored per harness. That is a
deliberate exception to the rule the rest of the file keeps, taken for the one fact
the rule cannot reach: "nobody has driven this yet" is a statement about Crew's own
EVIDENCE rather than about the harness, it exists today only as prose in a set's
comment, and no bit anywhere carries it. So the alternative to declaring it is not
deriving it: it is a two-level card, where ``ACP_BACKENDS_COMPACT`` excludes pi and
goose for want of a driven capture ("both are unclassified", in the set's own words)
and that exclusion wears the same mark as a harness with no compaction surface at
all. Those two answers are not one answer, and a reader of two levels cannot see
which cell a measurement would pay for.

Five things keep the exception from growing into the per-harness table this
projection exists to remove:

* an entry is admissible ONLY where the deciding set's own comment in ``backends.py``
  says the gap is evidence. ``test_backend_cards`` reads that comment and fails an
  entry the vocabulary does not support, so the table cannot drift away from the
  prose and the threshold is a gate rather than a convention to remember;
* "a decision is missing" is not this state. deepseek has no member dispatch because
  mounting Crew's control plane into a session nothing gates is refused, so the
  feature does not work there today and NOT AVAILABLE is the true mark. Unmeasured is
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
Four buckets, and every ``ACP_BACKENDS_*`` set in the vocabulary module reaches at
least one of them (``test_backend_cards`` fails when a new set reaches none, so
adding a set forces the decision rather than defaulting it):

* **user-facing** -- switching harness changes what the user can DO: a control
  appears or disappears, a command is refused, tools are absent from a session, a
  chat cannot be continued. These are :data:`USER_FACING_LINES`, and each is a
  capability whose ABSENCE is a loss, because that is what an available /
  not-available mark means to a reader.
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
for that module, so a projection over those sets is a function in a module of its
own -- the same shape, and for the same reason, as
``host_auth.backends_retired_by_host_logout()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Tuple

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
#: An enrolled member's whole saved agent spec is loaded at spawn.
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
#: the feature. Its input set stays a capability-line input (it is the kiro-family
#: marker), which is why the decision is recorded here rather than in
#: :data:`OFF_CARD_SETS`.
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


#: The lines an operator choosing a harness reads, in the order the card renders.
#:
#: Server-owned order, so a new line appears in the right place with no frontend
#: edit. Grouped: what reaches a session, then what a crew member's session gets,
#: then what a turn can do, then what can be tuned.
USER_FACING_LINES: Tuple[_LineSpec, ...] = (
    # Two channels carry Crew's tools, and a harness needs only one of them.
    # Members of the per-session array receive the server list on ``session/new``;
    # the kiro family is handed ``--agent`` and loads Crew's spec (its MCP servers
    # included) itself, which is exactly why they are absent from that array. A
    # harness in NEITHER has none of Crew's tools in its sessions, which is the
    # single most consequential thing this card says.
    #
    # ``ACP_BACKENDS_KIRO_SLASH_COMMANDS`` is the kiro-family marker and stands for
    # that family here. The runtime set does NOT: codex is served by the shared
    # runtime for TRANSPORT reasons while reading none of Crew's agent spec, so a
    # line keyed on it would hand codex this claim by the wrong route.
    _LineSpec(
        LINE_CREW_TOOLS, ("ACP_BACKENDS_SESSION_MCP_ARRAY", "ACP_BACKENDS_KIRO_SLASH_COMMANDS")
    ),
    _LineSpec(LINE_MEMBER_THREAD_TOOLS, ("ACP_BACKENDS_MEMBER_DISPATCH",)),
    _LineSpec(LINE_MEMBER_SAVED_AGENT, ("ACP_BACKENDS_MEMBER_CAPABILITIES",)),
    _LineSpec(LINE_SIDE_CHAT_TOOLS, ("ACP_BACKENDS_SIDE_READONLY",)),
    _LineSpec(LINE_SUBAGENT_CONTINUATION, ("ACP_BACKENDS_SESSION_SHARING",)),
    _LineSpec(LINE_MID_TURN_STEER, ("ACP_BACKENDS_STEER",)),
    _LineSpec(LINE_MANUAL_COMPACT, ("ACP_BACKENDS_COMPACT",)),
    # Effort travels down one of two channels, and neither set alone answers the
    # question a user asks. The config-option members advertise an ``effort``
    # option; the kiro family changes effort by slash command and is absent from
    # that set entirely, so reading the config set alone would report the default
    # harness as unable to do what it has always done.
    _LineSpec(
        LINE_REASONING_EFFORT,
        ("ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"),
    ),
    # Same shape for the model: the config-option members take a switch as
    # ``session/set_config_option``, and the kiro family takes it as the native
    # set-model request no membership set names positively -- so the kiro-family
    # marker stands for it, as on the first line above.
    _LineSpec(
        LINE_MODEL_SWITCH,
        ("ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"),
    ),
    _LineSpec(LINE_MARKDOWN_AGENTS, ("ACP_BACKENDS_MARKDOWN_AGENT_SPECS",)),
)

#: The facts that change what confines the agent or who holds a credential.
#:
#: Rendered outside any disclosure. Each is stated only when it holds, and each
#: names a control the operator is choosing rather than a feature they gain: two
#: of the four (the sandbox stand-down, and Crew handing over its credential)
#: describe a boundary moving, and the sandbox one fails OPEN by design.
SECURITY_LINES: Tuple[_LineSpec, ...] = (
    _LineSpec(NOTE_CREW_SANDBOX_STANDS_DOWN, ("ACP_BACKENDS_INTERNAL_SANDBOX",)),
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
    "ACP_BACKENDS_SEED_LOCAL_SETTINGS": (
        "whether a settings file is re-seeded on a model switch. Invisible when "
        "right, a stale model when wrong"
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
    "ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS": (
        "whether an advertised <model>[<effort>] id is applied as two writes. The "
        "model switch either lands or is refused, which its own line already says"
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
    "ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD": (
        "whether a freshly installed MCP server reaches a RUNNING session. The one "
        "set whose membership this card cannot honestly project: it is version-gated "
        "per process by mcp_hot_reload_supported, and a pre-session projection holds "
        "no version -- so an available mark would promise a session the runtime "
        "still resets on a build below that floor, and two levels cannot say "
        "on a recent enough release"
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
#: native set-model request", and no set says that positively. The runtime set
#: cannot stand in for it: it is a TRANSPORT property, and codex sits in it while
#: reading no agent spec of Crew's. A test holds both lines to reading THIS set.
KIRO_FAMILY_MARKER_SET = "ACP_BACKENDS_KIRO_SLASH_COMMANDS"

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
#: Both entries rest on the same words in ``ACP_BACKENDS_COMPACT``: pi and goose each
#: dispatch ``/compact`` before any model turn in their own source, neither could be
#: driven where that was written (pi answers ``Authentication required``, goose
#: ``Failed to resolve provider: GOOSE_PROVIDER``), and the set asks for the bar
#: opencode met -- a live session whose ``usage_update.used`` was seen to fall. Until
#: then Crew declines their ``/compact`` with ``COMPACT_ARM_UNCLASSIFIED``, which
#: promises nothing, and this is the card saying the same thing rather than reporting
#: a limit of the harness.
#:
#: USER-FACING lines only, which is not an omission: the security and operator notes
#: are stated only when they HOLD, so an unmeasured one is already absent rather than
#: crossed out, and a third level would have nothing to correct.
DECLARED_UNMEASURED: Mapping[Tuple[str, str], UnmeasuredLine] = {
    ("ACP_BACKEND_PI", LINE_MANUAL_COMPACT): UnmeasuredLine(
        reason=REASON_NO_DRIVEN_CAPTURE,
        declared_by="ACP_BACKENDS_COMPACT",
        citation=(
            "pi-acp 0.0.33 intercepts /compact in prompt() and awaits "
            "session.proc.compact(...), so the source says inline. What it has no "
            "driven capture of is the context actually shrinking, and it could not be "
            "driven where the set was written -- it answers Authentication required. "
            "Until then it is unclassified"
        ),
    ),
    ("ACP_BACKEND_GOOSE", LINE_MANUAL_COMPACT): UnmeasuredLine(
        reason=REASON_NO_DRIVEN_CAPTURE,
        declared_by="ACP_BACKENDS_COMPACT",
        citation=(
            "goose 1.50.1 routes /compact through Agent::reply -> execute_command -> "
            'handle_compact_command and its own command_starts_turn("/compact") is '
            "false, so the source says inline. It has no driven capture either, and "
            "could not be driven where the set was written -- it answers Failed to "
            "resolve provider: GOOSE_PROVIDER. Until then it is unclassified"
        ),
    ),
}


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

    #: Every user-facing line, in server order, available or not -- or, for the
    #: cells :data:`DECLARED_UNMEASURED` names, not measured.
    capabilities: Tuple[CardLine, ...]

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


def _card_lines(backend: str) -> Tuple[CardLine, ...]:
    """Every user-facing line for *backend*, in server order.

    Membership decides first and the declared table only ever SOFTENS a negative:
    an entry reaches a line that is already not available, so a harness that has
    demonstrated the capability keeps its available mark whatever the table says.
    That ordering is what stops a stale entry from withdrawing a real capability;
    the entry being stale at all is separately a test failure.
    """
    declared = _unmeasured_lines(backend)
    produced = []
    for spec in USER_FACING_LINES:
        available = _holds(backend, spec)
        entry = None if available else declared.get(spec.id)
        produced.append(
            CardLine(
                id=spec.id,
                available=available,
                measured=entry is None,
                unmeasured_reason="" if entry is None else entry.reason,
            )
        )
    return tuple(produced)


def card_for(backend: str) -> BackendCard:
    """The card for *backend*, complete for any id -- known to this build or not.

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
        capabilities=_card_lines(backend),
        security_notes=tuple(spec.id for spec in SECURITY_LINES if _holds(backend, spec)),
        operator_notes=tuple(spec.id for spec in OPERATOR_LINES if _holds(backend, spec)),
        tool_approval=backends.routing_for(backend).value,
        offered_by_build=backend in backends.registered_backends(),
    )


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
        "capabilities": [
            {
                "id": line.id,
                # Still a BOOL, and still first. An unmeasured line answers False
                # here, so a reader that predates the two fields beside it -- an
                # older panel against a newer gateway -- gets the fail-closed
                # not-available answer rather than inheriting a third state it has
                # no way to render.
                "available": line.available,
                "measured": line.measured,
                "unmeasured_reason": line.unmeasured_reason,
            }
            for line in card.capabilities
        ],
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
