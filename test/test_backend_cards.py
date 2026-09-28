"""The capability card is a PROJECTION, and these tests are what keeps it one.

Four properties, and each one is a cost the card exists to avoid paying again:

1. **Completeness under a new harness.** A backend id that joins
   ``ACP_BACKENDS_KNOWN`` must render a whole card with NO edit to any card file.
   That is the acceptance condition for the whole design, so it is a test rather
   than a claim: ``test_a_new_backend_renders_a_complete_card_with_no_card_edit``
   injects a synthetic id and reads its card back.
2. **Completeness under a new capability set.** Every ``ACP_BACKENDS_*`` set the
   vocabulary module defines has to be classified -- user-facing, security note,
   operator note, off-card, or the membership floor. A set in none of them is a set
   nobody decided about, which is the same failure the disposition-row test in
   ``test_agent_sdk_capabilities`` catches one layer down.
3. **No per-harness prose, and no per-harness branch.** The card file may not
   compare against a backend id: the moment it does, a new harness costs an edit
   here and the projection has become a table. Membership is the allowed shape --
   ``backend in <set>`` is the whole mechanism -- so the check is scoped to
   EQUALITY against an id, which is the shape that captures one harness.
4. **The third card state is DECLARED, so the declaration is gated.** Available and
   not-available are projected; "not measured" cannot be, because no bit carries it
   -- it is ``DECLARED_UNMEASURED``, the one per-harness table in the card module.
   Three tests are what keep that exception from becoming the per-harness card this
   design exists to remove: an entry must be supported by the deciding set's OWN
   comment in the vocabulary module, it may not name a MEMBER of that set, and a
   harness the table does not name must be measured on every line.
5. **The two unions rest on a coincidence, so the coincidence is pinned.** Two
   lines read ``ACP_BACKENDS_ACP_RUNTIME`` as a stand-in for the kiro family. That
   set's own meaning is narrower (served by the shared runtime, reads the
   kiro-family ``cli.json`` overlay), and its docstring names the harness that
   would break the alignment: a multiplexer served by a demux that is not
   kiro-shaped. ``test_the_kiro_family_stand_in_still_stands_for_the_kiro_family``
   fails on that day, before the card starts claiming two capabilities such a
   harness does not have.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Set, Tuple

import pytest

from kiro_crew.agent_sdk import backend_cards as cards_mod
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    Routing,
)

VOCABULARY_MODULE = Path(sdk_backends.__file__)

#: Words a set comment uses when it is declining on EVIDENCE rather than on a
#: capability. One of these has to appear for a declared-unmeasured entry to be
#: admissible, which is what keeps the table from outrunning the vocabulary.
EVIDENCE_GAP_WORDS = ("unclassified", "driven capture", "no capture", "not been measured")

CARD_MODULE = Path(cards_mod.__file__)

#: A backend id no set names, standing in for the next harness onboarded.
STRANGER = "a-harness-nobody-registered"

#: Every line the card carries, in every bucket.
ALL_SPECS: Tuple[cards_mod._LineSpec, ...] = (
    *cards_mod.USER_FACING_LINES,
    *cards_mod.SECURITY_LINES,
    *cards_mod.OPERATOR_LINES,
)


def _every_card() -> Tuple[cards_mod.BackendCard, ...]:
    """One card per known backend, by policy id.

    The projection exports no all-backends helper: nothing in production wants
    one, because the endpoint walks ``probe_backends()`` and asks for a card per
    row. So the tests build the set they are about.
    """
    produced = tuple(cards_mod.card_for(b) for b in sdk_backends.ACP_BACKENDS_KNOWN)
    return tuple(sorted(produced, key=lambda card: card.policy_id))


def _classified_sets() -> Set[str]:
    """Every set name the card module places in one of its buckets."""
    named: Set[str] = {cards_mod.MEMBERSHIP_FLOOR_SET, *cards_mod.OFF_CARD_SETS}
    for spec in ALL_SPECS:
        named.update(name for name in spec.sets if name.startswith("ACP_BACKENDS_"))
    return named


# ── 1. a new harness costs no card edit ────────────────────────────────────


def test_every_known_backend_renders_a_complete_card() -> None:
    """One card per known id, every user-facing line present on each.

    A line missing from one harness's card would make the panel's rows
    incomparable -- the reader would be looking at two different questionnaires
    and would have no way to tell.
    """
    expected = tuple(spec.id for spec in cards_mod.USER_FACING_LINES)
    produced = {card.backend for card in _every_card()}
    assert produced == set(sdk_backends.ACP_BACKENDS_KNOWN)
    for card in _every_card():
        assert tuple(line.id for line in card.capabilities) == expected, card.backend
        assert card.tool_approval, card.backend


def test_a_new_backend_renders_a_complete_card_with_no_card_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE acceptance condition: onboarding a harness touches no card file.

    The synthetic id joins the membership floor and two capability sets, exactly
    as a real Stage-1/Stage-2 onboarding would, and nothing in
    ``backend_cards.py`` is edited. Its card must come back whole: every line
    present, the two joined lines available, the rest not, and the fail-closed
    routing for a harness the routing table does not name.

    Patched on the DEFINING module rather than on the ``acp_backends`` shim,
    because the projection resolves every set through that module by name -- a
    patch on the shim would land on an attribute nothing reads and this test
    would pass while measuring production membership.
    """
    monkeypatch.setattr(
        sdk_backends,
        "ACP_BACKENDS_KNOWN",
        frozenset({*sdk_backends.ACP_BACKENDS_KNOWN, STRANGER}),
    )
    monkeypatch.setattr(
        sdk_backends,
        "ACP_BACKENDS_STEER",
        frozenset({*sdk_backends.ACP_BACKENDS_STEER, STRANGER}),
    )
    monkeypatch.setattr(
        sdk_backends,
        "ACP_BACKENDS_INTERNAL_SANDBOX",
        frozenset({*sdk_backends.ACP_BACKENDS_INTERNAL_SANDBOX, STRANGER}),
    )

    by_id = {card.backend: card for card in _every_card()}
    assert STRANGER in by_id, "a new known backend must get a row with no card edit"
    card = by_id[STRANGER]

    assert tuple(line.id for line in card.capabilities) == tuple(
        spec.id for spec in cards_mod.USER_FACING_LINES
    )
    available = {line.id for line in card.capabilities if line.available}
    assert available == {cards_mod.LINE_MID_TURN_STEER}
    assert cards_mod.NOTE_CREW_SANDBOX_STANDS_DOWN in card.security_notes
    # The onboarding order is Stage 1 (this membership) then Stage 5 (the auth
    # declaration), so a card served in between must not claim where a harness
    # signs in. Nothing is declared for the synthetic id, so the note is absent.
    assert cards_mod.NOTE_OWN_CREDENTIAL_STORE not in card.operator_notes
    # Fail closed on both derived facts: nothing establishes how this harness is
    # made to ask, and the build has not registered it as a choice.
    assert card.tool_approval == Routing.UNVERIFIED.value
    assert card.offered_by_build is False


def test_an_unknown_backend_is_withheld_every_capability() -> None:
    """A stranger's card reads as a stranger's, not as a neighbour's.

    ``card_for`` is total on purpose: the panel builds its row list from a union
    of server answers, so an id this projection has never seen must produce a
    complete card of negatives rather than a raise on a request path.
    """
    card = cards_mod.card_for(STRANGER)
    assert [line.available for line in card.capabilities] == [False] * len(card.capabilities)
    assert card.tool_approval == Routing.UNVERIFIED.value
    assert card.offered_by_build is False
    assert card.security_notes == ()
    # And NO note either, which the auth line makes a real hazard rather than a
    # formality: ``declaration_for`` answers an unknown id with a sentinel whose
    # entitlement source is the own-credential-file one, so a bare
    # ``signs_in_separately`` read would assert where this harness signs in on no
    # evidence at all.
    assert card.operator_notes == ()


# ── 2. a new capability set has to be classified ───────────────────────────


def test_every_capability_set_is_classified_by_the_card() -> None:
    """A set in no bucket is a set nobody decided about.

    Four buckets plus the membership floor. Adding a capability set therefore
    forces the same question the disposition table forces one layer down: is this
    something an operator choosing a harness needs to read?
    """
    defined = {name for name in vars(sdk_backends) if name.startswith("ACP_BACKENDS_")}
    missing = sorted(defined - _classified_sets())
    assert not missing, (
        f"these capability sets are not classified in agent_sdk/backend_cards.py: "
        f"{missing}. Put each in USER_FACING_LINES (its absence is a loss the reader "
        f"can act on), SECURITY_LINES (it moves a confinement or credential "
        f"boundary), OPERATOR_LINES (it says where something lives, and both states "
        f"are correct behaviour), or OFF_CARD_SETS with the reason it reaches no card."
    )


def test_the_card_classifies_no_set_that_does_not_exist() -> None:
    """The other direction, which a coverage count cannot see.

    A renamed or deleted set left behind here would read as classified while
    naming nothing, and the line built on it would silently answer False for
    every harness forever.
    """
    defined = {name for name in vars(sdk_backends) if name.startswith("ACP_BACKENDS_")}
    stale = sorted(_classified_sets() - defined)
    assert not stale, f"these names are classified but no set answers to them: {stale}"


def test_no_set_decides_two_lines_on_its_own() -> None:
    """One membership, one claim.

    A set that alone decided both a capability line and a note would be answering
    one question twice, in two registers. A set may still INFORM a second line as
    part of a union -- that is what the two unions are -- so the rule is about sole
    authorship rather than about appearing once.
    """
    sole: dict = {}
    for spec in ALL_SPECS:
        if len(spec.sets) != 1:
            continue
        name = spec.sets[0]
        sole.setdefault(name, []).append(spec.id)
    doubled = {name: ids for name, ids in sole.items() if len(ids) > 1}
    assert not doubled, f"these sets each decide more than one line by themselves: {doubled}"


def test_the_off_card_sets_reach_no_line() -> None:
    """An off-card set must not also be a line's input, union or not."""
    used = {name for spec in ALL_SPECS for name in spec.sets}
    assert not used & set(cards_mod.OFF_CARD_SETS)
    assert cards_mod.MEMBERSHIP_FLOOR_SET not in used


def test_every_off_card_set_carries_its_reason() -> None:
    """An entry with no reason is an omission wearing a decision's clothes."""
    for name, reason in cards_mod.OFF_CARD_SETS.items():
        assert len(reason.split()) >= 8, f"{name} needs a reason, not a label"


# ── 3. no per-harness branch, no per-harness prose ─────────────────────────


def test_the_card_module_compares_against_no_backend_id() -> None:
    """The projection may not branch on WHICH harness it is describing.

    Read from the AST rather than by grepping the text, so a comparison cannot
    hide behind formatting. Two shapes are refused: naming an ``ACP_BACKEND_*``
    identifier at all -- the module imports none -- and testing ``backend`` for
    EQUALITY. ``backend in <set>`` is deliberately allowed and is the entire
    mechanism: membership describes a capability, while equality captures one
    harness and makes the next one an edit here.
    """
    tree = ast.parse(CARD_MODULE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("ACP_BACKEND_"):
            offenders.append(f"{node.id} at line {node.lineno}")
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            equality = any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
            if node.left.id == "backend" and equality:
                offenders.append(f"equality on `backend` at line {node.lineno}")
    assert not offenders, (
        "the card must be a projection over membership, so it may not name a "
        f"harness id or compare one for equality: {offenders}"
    )


def test_the_card_module_names_no_harness_in_a_line_id() -> None:
    """A line id is a CAPABILITY, so it may not carry a harness's name.

    A per-harness id would mean a per-harness label, which is a per-harness edit
    to thirteen locale files -- the cost this projection exists to remove. The
    command-channel note is the near miss worth naming: its SET is spelled for the
    kiro protocol extension it describes, and the note's own id says ``crew``.
    """
    harness_words = ("kiro", "claude", "kas", "codex", "opencode", "goose", "deepseek", "pi_")
    for spec in ALL_SPECS:
        for word in harness_words:
            assert word not in spec.id, f"{spec.id} names a harness"


# ── 4. the union stand-in, pinned ──────────────────────────────────────────


def test_the_kiro_family_lines_read_the_marker_not_the_runtime_set() -> None:
    """Two lines mean "the kiro family". They must read the set that says so.

    ``ACP_BACKENDS_ACP_RUNTIME`` is a TRANSPORT property and codex is in it while
    reading none of Crew's agent spec, so a line keyed on it would hand codex
    "loads Crew's agent spec" and "takes the native set-model request" by the
    wrong route. Pinned on the specs themselves, so a substitution back to the
    runtime set is a red rather than a comment.
    """
    by_id = {spec.id: spec for spec in cards_mod.USER_FACING_LINES}
    for line in (cards_mod.LINE_CREW_TOOLS, cards_mod.LINE_MODEL_SWITCH):
        sets = by_id[line].sets
        assert cards_mod.KIRO_FAMILY_MARKER_SET in sets, (line, sets)
        assert "ACP_BACKENDS_ACP_RUNTIME" not in sets, (
            f"{line} reads the runtime set as the kiro family; codex is a runtime member "
            "that reads no kiro agent spec"
        )


# ── 5. the lines say what the memberships say ──────────────────────────────


def test_the_default_harness_reads_as_the_most_capable() -> None:
    """A coherence check on the whole projection, not on one line.

    kiro-cli is the harness every capability set was written against, so a card
    that did not show it ahead of every adapter would mean a line was inverted --
    the failure mode a two-level card is most exposed to.
    """
    counts = {
        card.backend: sum(1 for line in card.capabilities if line.available)
        for card in _every_card()
    }
    best = max(counts.values())
    assert counts[ACP_BACKEND_KIRO] == best
    assert counts[ACP_BACKEND_KIRO] > counts[ACP_BACKEND_KAS]


def test_crew_tools_are_available_on_both_channels_and_absent_on_neither() -> None:
    """The union that carries Crew's own tools, checked from both sides.

    The kiro family loads Crew's agent spec itself and the array members are
    handed the server list per session; a harness in neither has none of Crew's
    tools, which is the line this card exists to make visible. Pi is that harness
    today: it ACCEPTS the array and never forwards it.
    """

    def crew_tools(backend: str) -> bool:
        card = cards_mod.card_for(backend)
        line = next(x for x in card.capabilities if x.id == cards_mod.LINE_CREW_TOOLS)
        return line.available

    assert crew_tools(ACP_BACKEND_KIRO) is True
    assert crew_tools(ACP_BACKEND_DEEPSEEK) is True
    assert crew_tools(ACP_BACKEND_PI) is False


def test_effort_reads_available_on_the_harness_that_uses_a_slash_command() -> None:
    """The other union, and the reason it is not one set.

    kiro-cli changes reasoning effort by slash command and is absent from the
    config-option set entirely, so a card built on that set alone would report
    the default harness as unable to do what it has always done.
    """
    card = cards_mod.card_for(ACP_BACKEND_KIRO)
    line = next(x for x in card.capabilities if x.id == cards_mod.LINE_REASONING_EFFORT)
    assert line.available is True
    assert ACP_BACKEND_KIRO not in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION


def test_the_command_channel_reaches_no_card_line_at_all() -> None:
    """A harness off the kiro RPC is not command-less, so the card says nothing.

    opencode and pi publish their own built-ins as an ``available_commands_update``,
    so "slash commands stop working" would be false for exactly them -- and stating
    WHICH channel carries a command instead answers a question the reader did not
    ask: they lose no feature, carry no new risk, and no setting of theirs stops
    working. It is recorded off the card with that reason, and the channel itself is
    in ``kirocrew doctor`` and ``providers/mirrors/README.md``.
    """
    line_ids = {spec.id for spec in cards_mod.USER_FACING_LINES}
    assert cards_mod.NOTE_CREW_COMMAND_CHANNEL not in line_ids
    note_ids = {spec.id for spec in cards_mod.OPERATOR_LINES}
    assert cards_mod.NOTE_CREW_COMMAND_CHANNEL not in note_ids
    for backend in (ACP_BACKEND_OPENCODE, ACP_BACKEND_PI, ACP_BACKEND_KIRO):
        assert cards_mod.NOTE_CREW_COMMAND_CHANNEL not in cards_mod.card_for(backend).operator_notes


def test_the_only_where_it_lives_line_left_is_the_credential_store() -> None:
    """The card carries a where-it-lives fact only where it is also the reader's.

    Whose secret store a harness signs in against is a risk the reader takes on.
    Whose disk holds the transcript, and which registry fills the model picker, are
    routes Crew takes: the conversation comes back either way and the same models are
    offered, so neither costs a feature, adds a risk, or stops a setting of theirs
    from working. Both are recorded in ``OFF_CARD_SETS`` with that reason.
    """
    assert {spec.id for spec in cards_mod.OPERATOR_LINES} == {cards_mod.NOTE_OWN_CREDENTIAL_STORE}
    for name in (
        "ACP_BACKENDS_HARNESS_OWNED_SESSIONS",
        "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION",
    ):
        assert name in cards_mod.OFF_CARD_SETS, name


def test_every_known_harness_is_offered_and_carries_its_routing() -> None:
    """Known, selectable, and the card carries the routing as data.

    deepseek was the one harness this test existed for: in ``ACP_BACKENDS_KNOWN`` so
    a governance rule could name it, and outside the selectable baseline because
    nothing established that its tool calls reach the host gate. Its gate plugin
    closes the second half, so the row is now offered and its routing is what the
    panel shows -- still with no prose written per harness anywhere.
    """
    card = cards_mod.card_for(ACP_BACKEND_DEEPSEEK)
    assert card.offered_by_build is True
    assert card.tool_approval == Routing.VERIFIED_GATE_EXTENSION.value
    assert ACP_BACKEND_DEEPSEEK in sdk_backends.ACP_BACKENDS_KNOWN


def test_security_notes_are_stated_only_when_they_hold() -> None:
    """A caveat is raised or absent, never raised-and-negated.

    Crew's seatbelt stands down for kiro-cli and for nothing else, so exactly one
    card carries that note. A note on every card, negated, would be a line nobody
    reads.
    """
    carrying = [
        card.backend
        for card in _every_card()
        if cards_mod.NOTE_CREW_SANDBOX_STANDS_DOWN in card.security_notes
    ]
    assert carrying == [ACP_BACKEND_KIRO]
    kas = cards_mod.card_for(ACP_BACKEND_KAS)
    assert cards_mod.NOTE_HOST_CREDENTIAL_TO_CHILD in kas.security_notes
    assert cards_mod.NOTE_OWN_CREDENTIAL_STORE not in kas.operator_notes


def test_the_security_notes_are_the_confinement_and_credential_facts() -> None:
    """The split from the operator notes is what keeps them out of a disclosure.

    The panel renders these beside the tool-approval line and the others behind a
    closed one, so a note landing in the wrong bucket is a security fact a reader
    has to click to find.
    """
    assert tuple(spec.id for spec in cards_mod.SECURITY_LINES) == (
        cards_mod.NOTE_CREW_SANDBOX_STANDS_DOWN,
        cards_mod.NOTE_REFUSES_UNCLASSIFIED_TOOLS,
        cards_mod.NOTE_HOST_CREDENTIAL_TO_CHILD,
        cards_mod.NOTE_POD_HOME_RELOCATED,
    )


# ── 6. the wire shape ──────────────────────────────────────────────────────


def test_the_payload_carries_every_field_the_panel_reads() -> None:
    """The projection owns its own wire shape, so the shape is pinned here."""
    payload = cards_mod.card_payload(ACP_BACKEND_KIRO)
    assert set(payload) == {
        "capabilities",
        "security_notes",
        "operator_notes",
        "tool_approval",
        "offered_by_build",
        # The MCP half, projected from the mirror declarations rather than from a
        # membership set, and carried as its own GROUP: its fields answer one question
        # together, and a panel on an older gateway tests one absent object instead of
        # several absent fields. Its own shape is pinned in
        # ``test_backend_mcp_ability``.
        "mcp",
    }
    capabilities = payload["capabilities"]
    assert isinstance(capabilities, list)
    assert all(
        set(entry) == {"id", "available", "measured", "unmeasured_reason"} for entry in capabilities
    )
    # A LIST and not a map: the server owns the order, so a new line lands in the
    # right place with no frontend edit.
    assert [entry["id"] for entry in capabilities] == [
        spec.id for spec in cards_mod.USER_FACING_LINES
    ]
    # The routing value itself. A record around one string would be a shape no
    # reader needs, and a shipped gateway cannot take a wire field back.
    assert payload["tool_approval"] == Routing.AGENT_SPEC.value
    assert payload["offered_by_build"] is True
    # The two note lists are separate ON THE WIRE, because the panel renders them
    # in two places: one outside every disclosure, one behind it.
    assert cards_mod.NOTE_CREW_SANDBOX_STANDS_DOWN in payload["security_notes"]
    # One where-it-lives note is left on the card, and it is the reader's own risk:
    # whose secret store the harness signs in against. The three that named a route
    # Crew takes -- transcript disk, model registry, command channel -- are off it.
    assert payload["operator_notes"] == []
    signs_in_itself = cards_mod.card_payload("codex")
    assert cards_mod.NOTE_OWN_CREDENTIAL_STORE in signs_in_itself["operator_notes"]
    for backend in sorted(sdk_backends.ACP_BACKENDS_KNOWN):
        notes = cards_mod.card_payload(backend)["operator_notes"]
        assert cards_mod.NOTE_CREW_COMMAND_CHANNEL not in notes, backend
        assert cards_mod.NOTE_KEEPS_OWN_CHAT_RECORD not in notes, backend
        assert cards_mod.NOTE_HARNESS_MODEL_LIST not in notes, backend


# ── 7. the third state, which is declared rather than projected ────────────


def _comment_block_above(name: str) -> str:
    """The contiguous ``#`` comment block directly above set *name*'s definition.

    Read out of the vocabulary module's own SOURCE rather than out of a docstring,
    because that is where the reason for a non-membership is actually written: these
    sets carry their evidence in the comment above the assignment, and the rule this
    gate enforces is about that text.
    """
    lines = VOCABULARY_MODULE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"{name} ") and not line.startswith(f"{name}:"):
            continue
        block: list = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
            block.append(lines[cursor])
            cursor -= 1
        return "\n".join(reversed(block))
    raise AssertionError(f"no assignment of {name} found in {VOCABULARY_MODULE.name}")


def test_every_declared_unmeasured_entry_names_a_real_cell() -> None:
    """A stale entry is a cell that reads unmeasured about nothing.

    The harness is keyed by CONSTANT NAME, so a rename raises rather than quietly
    naming nobody -- this asserts the resolved id is a harness this build knows and
    the line is one the card actually renders, which a ``getattr`` alone cannot.
    """
    for (harness_name, line_id), entry in cards_mod.DECLARED_UNMEASURED.items():
        harness = getattr(sdk_backends, harness_name)
        assert harness in sdk_backends.ACP_BACKENDS_KNOWN, harness_name
        assert line_id in {spec.id for spec in cards_mod.USER_FACING_LINES}, line_id
        assert entry.reason, (harness_name, line_id)
        assert entry.declared_by in vars(sdk_backends), entry.declared_by
        # The citation is the admissibility evidence, so a label is not one.
        assert len(entry.citation.split()) >= 12, (harness_name, line_id)


def test_every_declared_unmeasured_entry_is_supported_by_the_set_comment() -> None:
    """THE threshold: the vocabulary has to say the gap is evidence.

    This is what stops the third state from becoming a second opinion. An entry is
    admissible only where the deciding set's own comment names the harness AND says
    what is missing is a measurement -- "unclassified", "no driven capture". A
    reading of the harness's capability that only this table holds would be exactly
    the per-harness prose the card exists to remove, and a reviewer could not tell
    the two apart by looking.
    """
    for (harness_name, line_id), entry in cards_mod.DECLARED_UNMEASURED.items():
        block = _comment_block_above(entry.declared_by).lower()
        word = harness_name.replace("ACP_BACKEND_", "").lower()
        assert re.search(rf"\b{re.escape(word)}\b", block), (
            f"{entry.declared_by}'s comment does not name {word}, so the vocabulary "
            f"does not support the unmeasured entry for ({word}, {line_id})"
        )
        assert any(phrase in block for phrase in EVIDENCE_GAP_WORDS), (
            f"{entry.declared_by}'s comment does not say the gap is EVIDENCE "
            f"({EVIDENCE_GAP_WORDS}), so ({word}, {line_id}) is a not-available cell "
            "rather than an unmeasured one"
        )


def test_no_declared_unmeasured_entry_names_a_member() -> None:
    """The table may soften a negative, never overrule a membership.

    A member has demonstrated the capability, so an entry naming one would be prose
    withdrawing a measured fact. Checked against the line's own union rather than
    against one set, because a union line is available on ANY of its inputs.
    """
    by_id = {spec.id: spec for spec in cards_mod.USER_FACING_LINES}
    for (harness_name, line_id), _entry in cards_mod.DECLARED_UNMEASURED.items():
        harness = getattr(sdk_backends, harness_name)
        line = cards_mod.card_for(harness)
        rendered = next(item for item in line.capabilities if item.id == line_id)
        assert rendered.available is False, (
            f"{harness_name} is a member of one of {by_id[line_id].sets}, so this cell "
            "is measured and the entry is stale"
        )
        assert rendered.measured is False


def test_the_compact_capture_gap_is_the_only_unmeasured_thing_on_any_card() -> None:
    """The two cells, named, and every other cell on every card measured.

    pi and goose both dispatch ``/compact`` before a model turn in their own source
    and neither has been driven, which ``ACP_BACKENDS_COMPACT`` records as
    "unclassified". A plain cross means something else: deepseek has no compaction on
    its ACP surface at all -- in the set's own words it emits no
    ``available_commands_update`` and answers ``session/load`` with "Method not
    found". Those two answers are not the same answer, and this test is where the
    difference is pinned.
    """
    unmeasured = {
        (card.backend, line.id)
        for card in _every_card()
        for line in card.capabilities
        if not line.measured
    }
    assert unmeasured == {
        (ACP_BACKEND_PI, cards_mod.LINE_MANUAL_COMPACT),
        (ACP_BACKEND_GOOSE, cards_mod.LINE_MANUAL_COMPACT),
    }
    # The harness with no compaction surface keeps the cross: its answer is known.
    deepseek = cards_mod.card_for(ACP_BACKEND_DEEPSEEK)
    compact = next(
        line for line in deepseek.capabilities if line.id == cards_mod.LINE_MANUAL_COMPACT
    )
    assert (compact.available, compact.measured) == (False, True)
    assert compact.unmeasured_reason == ""


def test_an_unmeasured_line_is_never_counted_as_available() -> None:
    """Fail-closed, on the object and on the wire.

    The one reading that would be worse than the two-level card is an unmeasured
    line that some consumer reads as a capability. ``available`` stays a bool and
    stays False, so the panel's own "supports N of M" count, ``kirocrew doctor`` and
    anything predating ``measured`` all answer not-available.
    """
    for backend in sorted(sdk_backends.ACP_BACKENDS_KNOWN):
        for entry in cards_mod.card_payload(backend)["capabilities"]:
            assert isinstance(entry["available"], bool)
            if not entry["measured"]:
                assert entry["available"] is False, (backend, entry)
                assert entry["unmeasured_reason"], (backend, entry)
            else:
                # A measured line carries no reason, so a reader cannot render one
                # beside a cross it does not belong to.
                assert entry["unmeasured_reason"] == "", (backend, entry)


def test_a_harness_the_table_does_not_name_is_measured_on_every_line() -> None:
    """Onboarding still costs no edit here, and claims no evidence gap either.

    The acceptance condition for the whole design survives the third state: a new
    harness renders a complete card, and every line of it is measured -- unmeasured
    is a statement about Crew's own measurements, not a synonym for an id nothing
    has been declared about.
    """
    card = cards_mod.card_for(STRANGER)
    assert all(line.measured for line in card.capabilities)
    assert all(line.unmeasured_reason == "" for line in card.capabilities)


def test_the_payload_is_json_native() -> None:
    """Nothing in the payload needs a custom encoder.

    ``web.json_response`` refuses a frozenset or a dataclass, and the failure
    would be a 500 on the panel's own poll rather than a missing line.
    """
    import json

    for backend in sorted(sdk_backends.ACP_BACKENDS_KNOWN):
        json.dumps(cards_mod.card_payload(backend))
