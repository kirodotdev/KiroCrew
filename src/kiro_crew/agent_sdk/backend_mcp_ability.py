"""How each harness receives Crew's MCP servers, projected from its declaration.

The other half of the ability card. :mod:`kiro_crew.agent_sdk.backend_cards`
projects what a harness can DO from the capability memberships; this projects what
happens to the AGENT SPEC on the way to it, from the declarations
``providers/mirrors`` already carries:

* :class:`~kiro_crew.providers.mirrors.registry.McpProjection` -- the KIND
  (native / mirror / external / no-channel / broker-only), and for a mirror the
  reach of a per-TOOL MCP restriction (:class:`~kiro_crew.providers.mirrors.registry.PerToolDeny`);
* each mirror's :meth:`~kiro_crew.providers.mirrors.base.AgentConfigMirror.rulings`
  -- one :class:`~kiro_crew.providers.mirrors.base.Disposition` per
  :class:`~kiro_crew.providers.mirrors.base.Concern` the spec carries.

Both were written to be read by a maintainer reviewing a projection, and neither
reached a reader choosing a harness. That is the whole gap: a user on codex gets a
permission mode pinned regardless of what their agent file asked for, an
``autoApprove`` block that is not honoured, a model list taken from the adapter
rather than Crew's registry, and hooks that reach no channel -- four deliberate,
defensible rulings, none of them visible before a session runs.

**This declares. It does not enforce.** Per-tool MCP deny is not a requirement on
every provider: a harness with no per-call deny channel withholds the whole server
instead, and what it owes a reader is to SAY so before they pick it. Nothing here
changes what any backend delivers.

Derived, not authored
---------------------
Same rule as the capability card, and for the same reason: a harness onboarded
without a card is a card that says "nothing declared", never a card that is
silently wrong. There is no ``if backend ==`` in this file, no harness id is
named, and a new harness renders a complete section the moment its
``PROJECTIONS`` entry exists -- which the mirror parity test already requires
before it can be selectable.

What is deliberately NOT on it
------------------------------
* **The prose.** ``McpProjection.reason`` and ``Ruling.reason`` are written for the
  reader of the registry, at registry length, in a maintainer's register -- one of
  them names an upstream Rust function. The card renders the DISPOSITION, which is
  the part an operator can act on. A user-facing reason is a NEW field every mirror
  fills in, not this one re-registered.
* **Transports.** Which transports a harness accepts is not a per-backend constant
  and must not be rendered as one: it is read from THIS session's ``initialize``
  answer (``providers.mirrors.codex.drop_unadvertised_transports`` over
  ``agentCapabilities.mcpCapabilities``), precisely so a released adapter that
  gains or drops one is not silently contradicted by a table. A pre-session card
  holds no handshake, so it states the kind and leaves the transport to the
  session that negotiated it.
* **A verdict.** Every disposition here is a declared ruling with a reason behind
  it. The card is advisory: it does not refuse a selection, and no field of it
  gates anything. ``providers/mirrors/README.md`` ("Fill the card") is where that
  rule, and the two surfaces it holds for, are written down once.

The only reader of the declaration inside the boundary
-----------------------------------------------------
``projection_for`` is imported here and nowhere else under ``agent_sdk``, and every
question about the declaration is answered here -- the kind, the per-tool deny reach,
the per-concern dispositions, and the ``no-channel`` gap's own address. That is an
invariant rather than a coincidence, and it is what ``cli_doctor`` depends on: two
projections over one record are two readings that can disagree, and a report that
states the same declared consequence in two voices is a report whose prose drifts
from the record it describes. One reader, one phrasing per fact.

Why a module of its own
-----------------------
``backend_cards`` is a pure projection over ``agent_sdk.backends``, a leaf
``config.loader`` reaches during ``KiroCrewConfig.load()``. The declarations read
here live in ``kiro_crew.providers``, so the import is function-local (``agent_sdk``
is the one tree the agent-sdk-boundary gate exempts, but an import edge added at
module scope would put a providers import on that load path). Keeping it beside
the card rather than inside it is what lets each stay a projection over one source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple


def concern_id(concern: Any) -> str:
    """*concern*'s card id: its :class:`Concern` member NAME, lower-cased.

    Keyed off the name rather than the enum's value, and the difference matters at
    exactly one member. The value is the spec's own JSON spelling
    (``permissions.defaultMode``), which is what makes it right in the registry and
    wrong on a wire the dashboard keys a translated label off: a dotted id reads as
    a path into the payload. The member name is already the stable machine spelling
    of the same concern, and lower-casing it produces the snake_case shape every
    other card id on this endpoint uses.
    """
    return str(concern.name).lower()


#: The concerns an operator choosing a harness reads, in the order the card renders.
#:
#: Named as STRINGS -- the member names of
#: :class:`~kiro_crew.providers.mirrors.base.Concern` -- for the reason
#: ``backend_cards`` names its sets as strings: it is what lets the completeness
#: test compare what the vocabulary DEFINES against what this file classifies,
#: without this module holding an import edge to the enum at module scope.
#:
#: Ordered by what a reader loses first: whether Crew's servers arrive at all, then
#: what narrows them, then what a restriction on them is worth, then the settings
#: the spec asked for and this harness answers differently.
ON_CARD_CONCERNS: Tuple[str, ...] = (
    "MCP_SERVERS",
    "TOOL_ALLOWLIST",
    "DENIED_TOOLS",
    "AUTO_APPROVE",
    "PERMISSION_MODE",
    "MODEL",
    "MODEL_ALLOWLIST",
    "HOOKS",
)

#: Concerns that reach no card line, each with the reason it does not.
#:
#: Read by the completeness test, so an entry here is a recorded decision rather
#: than an omission -- the same arrangement as ``backend_cards.OFF_CARD_SETS``, and
#: it exists because these two would otherwise put a false claim in front of a
#: reader: every mirror rules them WITHHELD, and rendering that verbatim would say
#: the agent never receives its own instructions.
OFF_CARD_CONCERNS: Mapping[str, str] = {
    "PROMPT": (
        "the spec's prompt is withheld from every mirror because it is not a mirror "
        "concern on any backend: it reaches every harness as ordinary prompt text in "
        "the [AGENT SYSTEM PROMPT] context block. Rendering the withhold would tell a "
        "reader their agent's instructions do not arrive, which is the opposite of "
        "what happens"
    ),
    "RESOURCES": (
        "same as PROMPT -- steering files are injected as context text rather than "
        "projected into a backend's config, so the withhold is an implementation "
        "route and not a loss the reader can act on"
    ),
}


#: Per-tool deny reaches whose cost falls on a WHOLE server rather than one tool.
#:
#: Two of the three, and the second one is the reason this is a set rather than a
#: comparison against ``whole-server``. ``whole-server`` has no per-call identity to
#: match, so withholding the server entire is the only faithful answer -- Crew's own
#: control plane included. ``per-call`` stays per tool on Crew's OWN servers and
#: withholds any other server whole, so a reader with a third-party server meets the
#: same accident. ``settings-file`` is the one reach that costs exactly the tool it
#: names, everywhere, which is why it is not here.
#:
#: Named here rather than in each renderer: a consumer that spelled the comparison
#: itself would author "which reaches are dangerous" for its own surface, and a reach
#: added to the vocabulary would render as ordinary on one surface and prominent on
#: another. Held against the vocabulary by the same completeness test that holds
#: :data:`OFF_CARD_CONCERNS`, so a new member is a failing test rather than a value
#: quietly classified as harmless.
COSTS_WHOLE_SERVER: Tuple[str, ...] = ("whole-server", "per-call")

#: Reaches whose cost is the single tool the restriction names, and nothing more.
#:
#: The other half of the classification, spelled out for the completeness test: a
#: reach in NEITHER tuple is one nobody ruled on, which is the state the test fails.
COSTS_ONE_TOOL: Tuple[str, ...] = ("settings-file",)


@dataclass(frozen=True)
class McpAbility:
    """What one harness's declaration says about Crew's MCP servers reaching it."""

    #: ``ProjectionKind``'s own value, or ``""`` for a backend with no declaration.
    #:
    #: ``""`` is the honest answer rather than a default: the mirror parity test
    #: refuses an undeclared SELECTABLE backend, so this can only be a harness the
    #: build can spell and has not finished onboarding, and a card that guessed
    #: would be the invisible-difference failure this whole folder exists to stop.
    projection: str

    #: ``PerToolDeny``'s own value, or ``""`` where the declaration carries none.
    #:
    #: Only a mirror answers: a native harness reads the spec itself, and the other
    #: kinds project nothing to narrow. The one value with a consequence an operator
    #: meets by accident is ``whole-server``, where switching ONE tool off withholds
    #: the whole server -- Crew's own control plane included.
    per_tool_deny: str

    #: Card ids of the concerns this harness's mirror rules WITHHELD: a decision,
    #: with a reason, that the spec's setting is not sent.
    withheld: Tuple[str, ...]

    #: Card ids of the concerns ruled NO_CHANNEL: the harness HAS the capability and
    #: this transport cannot carry it. A gap with an address, not a decision --
    #: which is why it is a separate list rather than folded into the one above.
    no_channel: Tuple[str, ...]

    #: ``no-channel`` only: the delivery path that would carry Crew's servers, as the
    #: declaration wrote it. ``""`` for every other kind.
    #:
    #: The one field on this record that is NOT for the dashboard. A gap is only
    #: addressable if the card can say what would have to exist, and that answer is
    #: maintainer-facing detail at maintainer length -- so it rides here for
    #: ``kirocrew doctor``, which prints it for the SELECTED harness alone, and the
    #: wire payload leaves it out.
    channel: str = ""

    #: ``no-channel`` and ``external`` only: the issue URL or doc anchor that carries
    #: the decision. Same audience and same omission from the wire as
    #: :attr:`channel`.
    tracking: str = ""

    @property
    def costs_control_plane(self) -> bool:
        """Whether a tool-off here can withhold Crew's OWN servers.

        The narrow case of :attr:`costs_whole_server`, and a different fact rather
        than the same one twice. ``per-call`` refuses per tool on Crew's own servers,
        so a restriction there costs a third-party server and the session keeps the
        channel it came from. ``whole-server`` has no per-call identity to match, so a
        restriction on ``kirocrew-core`` withholds ``kirocrew-core`` -- and that is the
        case where a session cannot report back.

        One reach, compared here rather than named in a tuple of its own: a table with
        one member and one reader is a generalization nothing asked for. What matters is
        that the comparison lives beside the declaration it reads instead of in the
        report that words it, and ``test_every_reach_is_told_apart_by_both_costs``
        drives it from the vocabulary, so a member added there is ruled on here.

        Which server a user narrows is the user's own choice, so this says the cost is
        reachable on this harness rather than certain.
        """
        return self.per_tool_deny == "whole-server"

    @property
    def costs_whole_server(self) -> bool:
        """Whether switching ONE tool off here can cost a whole server.

        Derived from :attr:`per_tool_deny` against :data:`COSTS_WHOLE_SERVER`, so
        every surface that gives this fact a prominent slot gives it to the same
        reaches. ``False`` for a harness that declares no reach, which is the honest
        answer: nothing is established about it.
        """
        return self.per_tool_deny in COSTS_WHOLE_SERVER


def _declaration(backend: str) -> Any | None:
    """*backend*'s ``McpProjection``, or ``None`` when it has no entry.

    Never raises: a card is served on a request path, and a build whose registry
    cannot be imported is a broken tree rather than a missing line.
    """
    try:
        # circular import: this module is reached from ``agent_sdk/__init__`` through
        # ``backend_cards``, and ``kiro_crew.providers.mirrors`` reaches back into
        # ``agent_sdk``. At module scope the edge is not a style question -- it breaks
        # ``import kiro_crew.agent_sdk`` with "cannot import name
        # CONTEXT_EVENT_AGENT_CHANGED from partially initialized module" and
        # ``import kiro_crew.config.loader`` with the same class of error, because
        # ``KiroCrewConfig.load()`` reaches the leaf this projection sits beside.
        from kiro_crew.providers.mirrors import projection_for

        return projection_for(backend)
    except Exception:
        return None


def _dispositions(backend: str) -> Mapping[str, str] | None:
    """Card id -> disposition value for every concern *backend*'s mirror rules on.

    Empty for every kind but ``mirror``: a mirror is what performs the projection, so
    it is the only kind that can answer per concern.

    ``None`` is the third answer and it is not the same as ``{}``: the rulings could
    not be READ. An empty map says this harness withholds nothing, which is a claim;
    ``None`` says nothing is established, which is the only honest answer when a
    registered mirror's ``rulings()`` raises. Never raises either way, for the same
    reason as :func:`_declaration`.
    """
    try:
        # circular import -- see :func:`_declaration`.
        from kiro_crew.providers.mirrors import mirror_for

        mirror = mirror_for(backend)
        if mirror is None:
            return {}
        return {
            concern_id(concern): str(ruling.disposition.value)
            for concern, ruling in mirror.rulings().items()
        }
    except Exception:
        return None


def _on_card_ids() -> Tuple[str, ...]:
    """The on-card concern ids, in render order, resolved from the vocabulary.

    Resolved through the enum by NAME rather than spelled here, so a renamed or
    deleted concern fails the completeness test instead of leaving a line that
    matches nothing and reads as "this harness withholds nothing".
    """
    try:
        # circular import -- see :func:`_declaration`.
        from kiro_crew.providers.mirrors import Concern
    except Exception:
        return ()
    by_name = {member.name: member for member in Concern}
    return tuple(concern_id(by_name[name]) for name in ON_CARD_CONCERNS if name in by_name)


def ability_for(backend: str) -> McpAbility:
    """*backend*'s MCP ability, complete for any id -- declared or not.

    Total: no raise and no dependence on a live session, like
    :func:`~kiro_crew.agent_sdk.backend_cards.card_for`. An id with no declaration
    answers ``""`` on both kinds and carries no withhold, which reads as "nothing
    is established about this harness" rather than as "nothing is lost".
    """
    declared = _declaration(backend)
    unknown = McpAbility(
        projection="", per_tool_deny="", withheld=(), no_channel=(), channel="", tracking=""
    )
    if declared is None:
        return unknown
    reach = declared.per_tool_deny
    ruled = _dispositions(backend)
    if ruled is None:
        # The declaration read and the rulings did not, which happens to a mirror this
        # build can spell and cannot run -- a plugin or edition registration. Answering
        # the card with empty concern lists would print "withholds nothing" off a read
        # that failed, so the whole card goes back to "nothing is established": the two
        # halves are one answer, and half of one is the invisible-difference failure this
        # projection exists to stop.
        return unknown
    on_card = _on_card_ids()
    return McpAbility(
        projection=str(declared.kind.value),
        per_tool_deny=str(reach.value) if reach is not None else "",
        withheld=tuple(cid for cid in on_card if ruled.get(cid) == "withheld"),
        no_channel=tuple(cid for cid in on_card if ruled.get(cid) == "no-channel"),
        channel=declared.channel,
        tracking=declared.tracking,
    )


def ability_payload(backend: str) -> Dict[str, object]:
    """*backend*'s MCP ability as the JSON shape ``GET /api/acp-backends`` sends.

    A USER-CONSEQUENCE subset of :func:`ability_for`, not a serialisation of it. A
    line earns the card when switching to this harness costs the reader a feature,
    adds a risk, or makes one of their own agent-file settings ineffective; that
    Crew reaches the harness a different way internally is not a line, however true.
    So the wire carries two things:

    * the per-tool deny reach and whether it can cost a whole server -- a RISK the
      reader meets by accident, since switching a tool off says nothing about
      servers;
    * the spec settings that will not take effect here, as ONE list. The
      withheld/no-channel split is a maintainer's distinction between a settled
      ruling and an open gap; the reader's question is the same either way -- does
      the thing I wrote in my file happen -- and each setting's own sentence says
      what becomes of it.

    The projection KIND is deliberately absent: ``native``/``mirror``/``external``
    names the route Crew takes, which costs the reader nothing and cannot be acted
    on. It stays on :class:`McpAbility` for ``kirocrew doctor``, whose reader is
    diagnosing the route rather than choosing a harness.

    Built here rather than in the card or the handler so this projection owns its own
    wire shape: which concerns reach a reader at all is this module's judgement, and a
    second assembler would be a second place that could disagree about it.
    """
    ability = ability_for(backend)
    return {
        "per_tool_deny": ability.per_tool_deny,
        # The classification travels WITH the reach, rather than being re-derived by
        # whichever surface renders it: which reaches cost a whole server is this
        # module's judgement, held against the vocabulary by its own test, and a
        # renderer comparing values itself would promote a new reach on one surface
        # and bury it on the next.
        "costs_whole_server": ability.costs_whole_server,
        # Stated only where it HOLDS, so a harness that honours the whole file sends an
        # empty list and renders nothing: a reader never reads a row of "delivered"
        # marks that mean "as expected".
        "ineffective": [*ability.withheld, *ability.no_channel],
    }


def spec_keys() -> Dict[str, str]:
    """Card id -> the key the AGENT SPEC spells that concern with.

    The card ids are machine keys a translated label hangs off; a terminal reader
    is holding the spec file instead, and ``permissions.defaultMode`` is what they
    have to go and look at. Derived from the same enum, so the two spellings cannot
    drift and neither is written down twice.

    Empty when the vocabulary cannot be imported, which leaves a caller rendering
    the card ids -- legible, and never a wrong key.
    """
    try:
        # circular import -- see :func:`_declaration`.
        from kiro_crew.providers.mirrors import Concern
    except Exception:
        return {}
    return {concern_id(member): str(member.value) for member in Concern}
