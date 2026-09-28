"""The MCP half of the ability card is a PROJECTION, and these tests keep it one.

The capability half already had its guards (``test_backend_cards``). This half
reads a different source -- the declarations in ``providers/mirrors`` -- so it has
its own failure modes, and each test below is one cost the card exists to stop
paying:

1. **Completeness under a new harness.** A selectable backend with no section is
   the invisible-difference failure the mirror folder exists to prevent, so the
   card must answer for every one of them. Not by GUESSING: an undeclared id
   answers ``""``, and the test asserts that too.
2. **Completeness under a new spec concern.** ``Concern`` is closed and a mirror
   must rule on every member, so a member added there must be either ON the card
   or recorded in :data:`~kiro_crew.agent_sdk.backend_mcp_ability.OFF_CARD_CONCERNS`
   with the reason it is not. A concern in neither is one nobody decided about.
3. **Completeness under a new kind or reach.** Every ``ProjectionKind`` and every
   ``PerToolDeny`` member must reach the wire as its own value, and doctor must
   have a phrase for it -- otherwise a harness declaring the new one renders a card
   that silently says nothing about the thing that makes it different.
4. **No per-harness prose.** The module may not name a harness id: the moment it
   does, onboarding costs an edit here and the projection has become a table.
5. **The declaration is what reaches the reader.** The shipped values are asserted
   end to end, because a declaration nothing renders is not a feature.

It DECLARES. Nothing here asserts that any backend enforces a per-tool MCP deny,
because two of them cannot and are not asked to.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Set

from kiro_crew.acp_backends import selectable_backend_values
from kiro_crew.agent_sdk import backend_mcp_ability as mcp_mod
from kiro_crew.agent_sdk.backend_cards import card_payload
from kiro_crew.providers.mirrors import (
    PROJECTIONS,
    Concern,
    Disposition,
    PerToolDeny,
    ProjectionKind,
)

MODULE = Path(mcp_mod.__file__)

#: A backend id no declaration names, standing in for the next harness onboarded.
STRANGER = "a-harness-nobody-declared"


# ── 1. every selectable harness answers, and a stranger answers nothing ─────


def test_every_selectable_backend_has_a_card() -> None:
    """The acceptance condition: a harness reaches the switch with a card.

    ``PROJECTIONS`` is what the mirror parity test already holds every selectable
    backend to; this asserts the card RENDERS that declaration rather than the
    declaration merely existing, which is the gap the issue reported.
    """
    for backend in selectable_backend_values():
        ability = mcp_mod.ability_for(backend)
        assert ability.projection, f"{backend!r} renders no projection kind"
        assert ability.projection in {kind.value for kind in ProjectionKind}, backend


def test_a_harness_with_no_declaration_claims_nothing() -> None:
    """Fail-closed, and loudly empty rather than quietly reassuring.

    A card that guessed would be the invisible-difference failure ``providers/mirrors``
    exists to stop: an operator would read a complete-looking section about a harness
    nothing has been declared about. Total, because it is served on a request path.
    """
    ability = mcp_mod.ability_for(STRANGER)
    assert ability.projection == ""
    assert ability.per_tool_deny == ""
    assert ability.withheld == ()
    assert ability.no_channel == ()


def test_a_new_harness_needs_no_edit_to_this_module() -> None:
    """Onboarding costs a PROJECTIONS entry and nothing here.

    Driven through the real registry rather than a stub: a kind and a reach already
    shipped by some harness are read back on an id this module has never seen, so
    the projection is shown to be keyed on the DECLARATION and not on the id.
    """
    borrowed = next(
        backend
        for backend, declared in PROJECTIONS.items()
        if declared.kind is ProjectionKind.MIRROR
    )
    ability = mcp_mod.ability_for(borrowed)
    # The same declaration, reached only through the id -- no branch in this module
    # names the harness, which the AST test below proves structurally.
    assert ability.projection == ProjectionKind.MIRROR.value
    assert ability.per_tool_deny == PROJECTIONS[borrowed].per_tool_deny.value


# ── 2. every spec concern is classified ─────────────────────────────────────


def test_every_concern_is_classified() -> None:
    """A concern in neither bucket is a concern nobody decided about.

    ``Concern`` is closed and every mirror must rule on every member, so adding one
    there obliges this file to answer the reader's half of the same question: is
    this something an operator choosing a harness needs to see?
    """
    classified: Set[str] = {*mcp_mod.ON_CARD_CONCERNS, *mcp_mod.OFF_CARD_CONCERNS}
    defined = {member.name for member in Concern}
    missing = sorted(defined - classified)
    assert not missing, (
        f"these spec concerns are not classified in agent_sdk/backend_mcp_ability.py: "
        f"{missing}. Put each in ON_CARD_CONCERNS (a reader choosing a harness can act "
        f"on how it is ruled) or in OFF_CARD_CONCERNS with the reason it reaches no "
        f"card line."
    )


def test_the_module_classifies_no_concern_that_does_not_exist() -> None:
    """The other direction, which a coverage count cannot see.

    A renamed or deleted concern left behind here would read as classified while
    naming nothing, and the line built on it would answer "withholds nothing" for
    every harness forever.
    """
    classified: Set[str] = {*mcp_mod.ON_CARD_CONCERNS, *mcp_mod.OFF_CARD_CONCERNS}
    defined = {member.name for member in Concern}
    stale = sorted(classified - defined)
    assert not stale, f"these names answer to no Concern member: {stale}"


def test_every_off_card_concern_carries_its_reason() -> None:
    """An omission with no reason is indistinguishable from a decision.

    The same rule ``McpProjection`` enforces on its own kinds, and the reason this
    is a mapping rather than a set.
    """
    for concern, reason in mcp_mod.OFF_CARD_CONCERNS.items():
        assert reason.strip(), f"{concern} is off the card with no reason"


def test_the_two_off_card_concerns_are_the_context_text_ones() -> None:
    """Pinned by name, because leaving them ON would print a falsehood.

    Every mirror rules PROMPT and RESOURCES withheld, and both reach the harness
    anyway -- as ordinary context text rather than through a projection. Rendering
    the withhold verbatim would tell a reader their agent never receives its own
    instructions, which is the opposite of what happens. A third entry here is a
    deliberate change to this assertion.
    """
    assert set(mcp_mod.OFF_CARD_CONCERNS) == {"PROMPT", "RESOURCES"}
    for backend, declared in PROJECTIONS.items():
        if declared.kind is not ProjectionKind.MIRROR:
            continue
        ruled = mcp_mod._dispositions(backend)
        for name in ("PROMPT", "RESOURCES"):
            cid = Concern[name].name.lower()
            assert ruled.get(cid) == Disposition.WITHHELD.value, (backend, name)


# ── 3. every kind and every reach reaches a reader ──────────────────────────


def test_every_projection_kind_reaches_the_wire_as_its_own_value() -> None:
    """The card renders the KIND, so a new kind must be distinguishable on it.

    Read through a real declaration per kind rather than asserted over the enum
    alone: the wire carries the value, and a projection that collapsed two kinds
    onto one string would still pass an enum-only check.
    """
    seen = {mcp_mod.ability_for(backend).projection for backend in PROJECTIONS}
    declared = {declared.kind.value for declared in PROJECTIONS.values()}
    assert seen == declared


def test_the_doctor_report_prints_the_declarations_own_values() -> None:
    """Doctor states the kind and the reach as DECLARED, so no table can fall behind.

    This replaces a pair of tests that held two English phrase tables to the two
    vocabularies. The phrase tables are gone: the panel owns the prose in thirteen
    languages, and a rival wording in the report was one declaration speaking with two
    voices -- of which only one could be translated, and only one could drift.

    What is asserted now is the property that made those tables unnecessary: every
    member of both vocabularies reaches the report as its own value, so a new member is
    printable the moment it is declared and there is nothing to keep in step.
    """
    from kiro_crew import cli_doctor

    assert not hasattr(cli_doctor, "_PROJECTION_PHRASE"), "the prose table is back"
    assert not hasattr(cli_doctor, "_DENY_PHRASE"), "the prose table is back"

    # Scoped to the SELECTABLE set, which is what the section can report on. A kind
    # declared only by a harness this build does not offer (deepseek's ``broker-only``)
    # is unreachable by construction, and asserting it would assert a row nobody can get.
    # Driven once per harness IN USE, because that is the card the report prints: the
    # property is that every declared value is printABLE, not that all of them print at
    # once -- a table of six on every run was the noise the section shed.
    selectable = set(selectable_backend_values())
    reachable = {b: p for b, p in PROJECTIONS.items() if b in selectable}
    assert reachable, "no selectable backend carries a declaration"
    for backend, projection in reachable.items():
        printed = _doctor_ability_output(backend)
        assert projection.kind.value in printed, projection.kind
        if projection.per_tool_deny is not None:
            assert projection.per_tool_deny.value in printed, projection.per_tool_deny


def _doctor_ability_output(backend: str = "") -> str:
    """The ability section's own stdout for *backend* IN USE, captured without a fixture.

    A helper rather than a fixture because two tests here want the text and neither is
    about capture plumbing. It takes the harness in use because the section reports the
    card of THAT harness: the full per-harness table was the dashboard's job all along,
    and a row apiece on every terminal run was the noise that bought it.
    """
    import io
    from contextlib import redirect_stdout
    from types import SimpleNamespace

    from kiro_crew import cli_doctor

    buffer = io.StringIO()
    cfg = SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))
    with redirect_stdout(buffer):
        cli_doctor._doctor_backend_ability_cards(cfg)
    return buffer.getvalue()


def test_a_concern_id_is_the_member_name_and_not_the_dotted_spec_key() -> None:
    """The id keys a translated label, so it may not be a path into a payload.

    ``permissions.defaultMode`` is the concern's VALUE and the right spelling in the
    registry; as a card id it reads as a path. The spec key is offered separately,
    for the terminal reader who is holding the file.
    """
    assert mcp_mod.concern_id(Concern.PERMISSION_MODE) == "permission_mode"
    assert mcp_mod.spec_keys()["permission_mode"] == "permissions.defaultMode"
    assert set(mcp_mod.spec_keys()) == {member.name.lower() for member in Concern}


# ── 4. no per-harness prose ─────────────────────────────────────────────────


def test_the_module_names_no_harness() -> None:
    """The projection may not branch on WHICH harness it is describing.

    Read from the AST rather than by grepping, so a comparison cannot hide behind
    formatting. Two shapes are refused, the same two ``test_backend_cards`` refuses:
    naming an ``ACP_BACKEND_*`` identifier, and testing ``backend`` for equality.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("ACP_BACKEND_"):
            offenders.append(f"{node.id} at line {node.lineno}")
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            equality = any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
            if node.left.id == "backend" and equality:
                offenders.append(f"equality on `backend` at line {node.lineno}")
    assert not offenders, (
        "the MCP card must be a projection over the declaration, so it may not name "
        f"a harness id or compare one for equality: {offenders}"
    )


def test_no_card_id_names_a_harness() -> None:
    """A card id is a CONCERN, so it may not carry a harness's name.

    A per-harness id would mean a per-harness label, which is a per-harness edit to
    thirteen locale files -- the cost this projection exists to remove.
    """
    harness_words = ("kiro", "claude", "kas", "codex", "opencode", "goose", "deepseek", "pi_")
    for member in Concern:
        for word in harness_words:
            assert word not in mcp_mod.concern_id(member), f"{member.name} names a harness"


# ── 3b. an unreadable half is not a reassuring answer ───────────────────────


def test_a_mirror_whose_rulings_raise_answers_nothing_established(monkeypatch) -> None:
    """Half a card is worse than none, because the missing half reads as good news.

    ``ability_for`` reads two things: the declaration, and the mirror's per-concern
    rulings. A registered mirror this build can spell but cannot run -- a plugin or an
    edition registration -- can answer the first and raise on the second, and a card
    built from that would print the kind and the reach with EMPTY concern lists. Empty
    means "withholds nothing" on every surface that renders it, so the reader would be
    told their agent file arrives whole on the strength of a read that failed.

    The whole card goes back to "nothing is established" instead, which is the same
    answer an undeclared harness gets and the one no surface renders as reassurance.

    Driven through the real failure: the mirror is registered and its ``rulings()``
    raises, which is what a half-built mirror does.
    """
    import kiro_crew.providers.mirrors as mirrors_pkg

    declared_mirror = next(b for b, d in PROJECTIONS.items() if d.kind is ProjectionKind.MIRROR)
    intact = mcp_mod.ability_for(declared_mirror)
    assert intact.projection == "mirror", "fixture harness no longer projects as a mirror"
    assert intact.withheld, "fixture harness withholds nothing, so the test proves nothing"

    class _HalfBuiltMirror:
        def rulings(self):
            raise RuntimeError("this mirror cannot be run by this build")

    monkeypatch.setattr(mirrors_pkg, "mirror_for", lambda _b: _HalfBuiltMirror())

    assert mcp_mod._dispositions(declared_mirror) is None
    blind = mcp_mod.ability_for(declared_mirror)
    assert blind.projection == "", blind
    assert blind.per_tool_deny == "", blind
    assert blind.withheld == () and blind.no_channel == (), blind
    assert blind.costs_whole_server is False
    assert blind.costs_control_plane is False
    payload = mcp_mod.ability_payload(declared_mirror)
    assert payload == {
        "per_tool_deny": "",
        "costs_whole_server": False,
        "ineffective": [],
    }, payload


def test_an_unreadable_rulings_read_is_told_apart_from_a_kind_with_no_rulings() -> None:
    """``None`` and ``{}`` are different answers, and the difference is the whole fix.

    A ``native`` or ``external`` harness has no mirror, so it has no rulings and its
    empty map is a FACT. A mirror whose rulings raise has an unknown, and collapsing the
    two would put the reassuring reading back.
    """
    for backend, declared in PROJECTIONS.items():
        if declared.kind is ProjectionKind.MIRROR:
            continue
        assert mcp_mod._dispositions(backend) == {}, backend
        # ... and the card still renders, because nothing about it is unknown.
        assert mcp_mod.ability_for(backend).projection == declared.kind.value, backend


# ── 4b. the reach classification is complete, and it is the server's ────────


def test_every_reach_is_classified_as_costly_or_not() -> None:
    """A new ``PerToolDeny`` member is a failing test, not a harmless default.

    Which reaches cost a WHOLE server is a judgement, and both renderers now read it
    off this module instead of spelling a comparison each. That only holds if the
    classification is total: a member in neither tuple would answer ``False``, which
    reads as "switching a tool off here costs the tool" -- the claim the card exists
    to stop being made by default.
    """
    classified = {*mcp_mod.COSTS_WHOLE_SERVER, *mcp_mod.COSTS_ONE_TOOL}
    declared = {member.value for member in PerToolDeny}
    assert classified == declared, (
        "every PerToolDeny value must be in COSTS_WHOLE_SERVER (a tool-off can cost a "
        "whole server) or in COSTS_ONE_TOOL (it costs the tool it names), and neither "
        f"tuple may name a value the vocabulary dropped: {classified ^ declared}"
    )
    assert not set(mcp_mod.COSTS_WHOLE_SERVER) & set(mcp_mod.COSTS_ONE_TOOL)


def test_every_reach_is_told_apart_by_both_costs() -> None:
    """Two questions, not one asked twice, and the difference is the control plane.

    ``per-call`` costs a whole server -- any server that is not Crew's own -- but Crew
    refuses the call itself on ``kirocrew-core``, so the session can still report back.
    ``whole-server`` has no per-call identity to match and takes the control plane with
    it. The terminal report words the second consequence, so the second answer has to be
    strictly narrower: collapsing them would put "cannot report back" in front of a
    reader whose session can.

    Driven from the vocabulary rather than from a table, so a member added to
    ``PerToolDeny`` is ruled on by ``costs_control_plane`` here.
    """
    from kiro_crew.agent_sdk.backend_mcp_ability import McpAbility

    def ability(reach: str) -> McpAbility:
        return McpAbility(projection="mirror", per_tool_deny=reach, withheld=(), no_channel=())

    control_plane = {m.value for m in PerToolDeny if ability(m.value).costs_control_plane}
    costly = {m.value for m in PerToolDeny if ability(m.value).costs_whole_server}
    assert control_plane == {PerToolDeny.WHOLE_SERVER.value}
    assert control_plane < costly, (control_plane, costly)
    assert not ability("").costs_control_plane
    for backend, declared in PROJECTIONS.items():
        got = mcp_mod.ability_for(backend)
        assert got.costs_control_plane is (
            declared.per_tool_deny is PerToolDeny.WHOLE_SERVER
        ), backend
        if got.costs_control_plane:
            assert got.costs_whole_server, backend
    assert mcp_mod.ability_for(STRANGER).costs_control_plane is False


def test_the_control_plane_cost_stays_off_the_wire() -> None:
    """Read by the terminal report alone, so the payload does not carry it.

    The panel phrases both costly reaches in thirteen languages and needs the wider
    flag; a second boolean on the wire would be a field with no reader, and a field
    with no reader is one that drifts.
    """
    for backend in sorted(PROJECTIONS):
        assert "costs_control_plane" not in mcp_mod.ability_payload(backend)


def test_the_two_reaches_that_cost_a_whole_server_are_the_shipped_ones() -> None:
    """The classification asserted against the vocabulary's own semantics.

    ``whole-server`` has no per-call identity to match, so the only faithful action is
    withholding the server entire. ``per-call`` refuses per tool on Crew's OWN servers
    and withholds any other server whole, which is the same accident for a reader with
    a third-party server. ``settings-file`` reaches the harness as a rule it reads, so
    the narrowed server stays mounted and the cost is the tool.
    """
    assert set(mcp_mod.COSTS_WHOLE_SERVER) == {
        PerToolDeny.WHOLE_SERVER.value,
        PerToolDeny.PER_CALL.value,
    }
    assert set(mcp_mod.COSTS_ONE_TOOL) == {PerToolDeny.SETTINGS_FILE.value}


def test_a_harness_with_no_reach_is_not_called_costly() -> None:
    """``""`` is "nothing is established", and it may not read as a warning either.

    The flag is the prominent slot on the panel, so a harness that declares no reach
    at all -- a native harness, or an id no declaration names -- must not take it.
    """
    for backend, declared in PROJECTIONS.items():
        if declared.per_tool_deny is None:
            assert mcp_mod.ability_for(backend).costs_whole_server is False, backend
    assert mcp_mod.ability_for(STRANGER).costs_whole_server is False
    assert mcp_mod.ability_payload(STRANGER)["costs_whole_server"] is False


def test_the_classification_reaches_the_wire_for_every_shipped_harness() -> None:
    """End to end: the payload's flag agrees with the reach it travels beside.

    The point of shipping it is that no renderer re-derives it, so the value a
    renderer reads is asserted here against the declaration rather than against the
    tuple alone.
    """
    for backend in sorted(PROJECTIONS):
        payload = mcp_mod.ability_payload(backend)
        expected = payload["per_tool_deny"] in mcp_mod.COSTS_WHOLE_SERVER
        assert payload["costs_whole_server"] is expected, backend
        assert isinstance(payload["costs_whole_server"], bool), backend


# ── 5. the shipped declarations, end to end ─────────────────────────────────


def test_the_shipped_whole_server_harnesses_say_so_on_their_card() -> None:
    """The row the maintainer's ruling turned into a card line.

    Per-tool ``mcp.deny`` is NOT a hard requirement on every provider. A harness
    without a per-call deny channel withholds the whole server instead, and what it
    owes a reader is to DECLARE that before a session runs. This asserts the
    declaration reaches the card for every harness that carries it -- by set rather
    than by one id, so a harness that gains or loses the reach is caught.
    """
    declared = {
        backend
        for backend, projection in PROJECTIONS.items()
        if projection.per_tool_deny is PerToolDeny.WHOLE_SERVER
    }
    assert declared, "no harness declares the whole-server reach any more"
    for backend in declared:
        assert mcp_mod.ability_for(backend).per_tool_deny == PerToolDeny.WHOLE_SERVER.value


def test_a_withhold_and_a_no_channel_are_separate_lists() -> None:
    """The split IS the meaning, so it is asserted rather than assumed.

    A withhold is a decision with a reason; a no-channel is a gap the transport
    cannot carry today and has an address recorded for it. Collapsing them would
    tell a reader a settled ruling and an open gap are the same answer -- which is
    the exact conflation ``Disposition`` was introduced to end.
    """
    mirrored = [b for b, d in PROJECTIONS.items() if d.kind is ProjectionKind.MIRROR]
    assert mirrored
    for backend in mirrored:
        ability = mcp_mod.ability_for(backend)
        assert not set(ability.withheld) & set(ability.no_channel), backend
        ruled = mcp_mod._dispositions(backend)
        for cid in ability.withheld:
            assert ruled[cid] == Disposition.WITHHELD.value, (backend, cid)
        for cid in ability.no_channel:
            assert ruled[cid] == Disposition.NO_CHANNEL.value, (backend, cid)


def test_a_kind_with_no_mirror_carries_no_per_concern_ruling() -> None:
    """Only a mirror can answer per concern, so only a mirror does.

    A ``native`` harness reads the spec itself and an ``external`` projection lives
    outside this folder; neither has rulings to read, and inventing a list for them
    would be the card claiming a loss where there is none.
    """
    for backend, declared in PROJECTIONS.items():
        if declared.kind is ProjectionKind.MIRROR:
            continue
        ability = mcp_mod.ability_for(backend)
        assert ability.withheld == (), backend
        assert ability.no_channel == (), backend
        assert ability.per_tool_deny == "", backend


def test_the_card_render_order_is_the_servers() -> None:
    """A LIST in server order, so a new concern lands in the right place.

    The frontend holds a label per id and no order of its own, which is what keeps
    a new line from costing a frontend edit.
    """
    ability = mcp_mod.ability_for(
        next(b for b, d in PROJECTIONS.items() if d.kind is ProjectionKind.MIRROR)
    )
    on_card = [Concern[name].name.lower() for name in mcp_mod.ON_CARD_CONCERNS]
    for listed in (ability.withheld, ability.no_channel):
        assert list(listed) == [cid for cid in on_card if cid in listed]


# ── 6. the wire shape ───────────────────────────────────────────────────────


def test_the_payload_carries_every_field_the_panel_reads() -> None:
    """The projection owns its own wire shape, so the shape is pinned here."""
    payload = mcp_mod.ability_payload(
        next(b for b, d in PROJECTIONS.items() if d.kind is ProjectionKind.MIRROR)
    )
    assert set(payload) == {"per_tool_deny", "costs_whole_server", "ineffective"}
    assert isinstance(payload["ineffective"], list)


def test_the_card_endpoint_carries_the_mcp_group() -> None:
    """One card on the wire, so the panel reads one object rather than two calls."""
    for backend in selectable_backend_values():
        payload = card_payload(backend)
        assert set(payload["mcp"]) == {  # type: ignore[arg-type]
            "per_tool_deny",
            "costs_whole_server",
            "ineffective",
        }, backend


def test_the_wire_carries_only_what_costs_the_reader_something() -> None:
    """The card's admission rule, asserted rather than left to a reviewer's eye.

    A line reaches the reader iff switching to this harness costs them a feature, adds
    a risk, or makes one of their own agent-file settings ineffective. The reach is a
    risk; the ineffective settings are the third case. Everything else the projection
    knows -- WHICH route Crew takes to this harness, where the gap is addressed, what
    the gap's tracking pointer is -- answers a question the reader did not ask, and
    saying it would make the card longer without making a decision easier.

    Pinned as an exact key set, because the cost of a field is that it can never be
    taken back off a shipped wire.
    """
    for backend in sorted(PROJECTIONS):
        payload = mcp_mod.ability_payload(backend)
        assert set(payload) == {"per_tool_deny", "costs_whole_server", "ineffective"}, backend


def test_the_projection_kind_is_known_and_not_sent() -> None:
    """``native`` / ``mirror`` / ``external`` is a ROUTE, so the card does not carry it.

    It stays on :class:`McpAbility` because ``kirocrew doctor`` states it: that reader
    is diagnosing the route. A reader choosing a harness cannot act on it -- no feature
    changes, no risk appears, no setting of theirs stops working -- so it reaches no
    card line, as its own or folded into another.
    """
    for backend, declared in PROJECTIONS.items():
        assert mcp_mod.ability_for(backend).projection == declared.kind.value, backend
        blob = json.dumps(mcp_mod.ability_payload(backend))
        assert declared.kind.value not in blob, backend


def test_an_ineffective_setting_is_one_list_however_it_was_ruled() -> None:
    """Withheld and no-channel are one group on the wire, and the reason is the reader.

    The split is real and the module keeps it -- a withhold is a settled decision, a
    no-channel is an open gap with an address -- but both answer the reader's one
    question the same way: the thing you wrote in your file does not happen here. Two
    headings made them look like two kinds of problem to act on differently.
    """
    for backend in sorted(PROJECTIONS):
        ability = mcp_mod.ability_for(backend)
        listed = mcp_mod.ability_payload(backend)["ineffective"]
        assert listed == [*ability.withheld, *ability.no_channel], backend
        # order still comes from the card's own render order, so a new concern lands
        # where the vocabulary put it rather than where a frontend sorts it
        on_card = [Concern[name].name.lower() for name in mcp_mod.ON_CARD_CONCERNS]
        assert list(listed) == [cid for cid in on_card if cid in listed], backend


def test_the_payload_is_json_native() -> None:
    """``web.json_response`` refuses a frozenset or a dataclass.

    The failure would be a 500 on the panel's own poll rather than a missing line.
    """
    for backend in sorted(PROJECTIONS):
        json.dumps(mcp_mod.ability_payload(backend))
    json.dumps(mcp_mod.ability_payload(STRANGER))


def test_the_reason_prose_never_reaches_the_wire() -> None:
    """Deliberately not projected, and the omission is load-bearing.

    ``McpProjection.reason`` and ``Ruling.reason`` are written for the reader of the
    registry, at registry length and in a maintainer's register -- one of them names
    an upstream Rust function. If a user-facing reason is ever wanted, it is a new
    FIELD every mirror fills in, not this one re-registered.
    """
    for backend, declared in PROJECTIONS.items():
        blob = json.dumps(mcp_mod.ability_payload(backend))
        assert declared.reason[:40] not in blob, backend
