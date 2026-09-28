"""The crew kind's type registry: the two dispatch contracts and their validator.

``test_crew_log_types.py`` covers the SESSION table. This file covers the CREW
table, and it is deliberately a separate file because the two answer different
questions: the session table is pinned equal to the set of types the session
emitter writes, while the crew table declares exactly the two contracts the crew
kind's spec states as contracts and leaves the other six domains open.

Three things are asserted here that nothing else can:

* the declared fields match ``docs/reference/crew-log/crew-types.md`` field for
  field, so the page and the registry cannot drift;
* ``validate_data`` refuses a malformed crew payload on the real append path,
  leaving the file byte-identical;
* ownership refuses a crew type in a session's log AND a session type in a
  crew's log, which is the hole a single shared registry would open.

The ownership assertions carry a MUTATION check: each direction is re-run against
a registry whose crew entry has been removed, and the test proves the refusal
comes from ``TYPE_OWNERSHIP`` rather than from a type simply being undeclared --
otherwise a registry that lost its crew table would still pass.
"""

from __future__ import annotations

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, CrewLogError, Ref
from kiro_crew.crew_log.entry_types import (
    CREW_ENTRY_TYPES,
    CREW_REPORT_STATUSES,
    CREW_TARGET_KINDS,
    ENTRY_TYPES,
    SESSION_ENTRY_TYPES,
    declaration_for,
    render_markdown,
    validate_data,
)
from kiro_crew.crew_log.schema import KIND_CREW, KIND_SESSION, TYPE_OWNERSHIP, check_ownership
from kiro_crew.work_vocab import WORK_WORKER_STATUSES

CREW = "qa"
SESSION = "s-7f3a"

DISPATCH = "crew/dispatch"
REPORT = "crew/report"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _crew(unit_id: str = CREW) -> CrewLog:
    return CrewLog.create(KIND_CREW, unit_id)


def _session(unit_id: str = SESSION) -> CrewLog:
    return CrewLog.create(KIND_SESSION, unit_id, owner=CREW, agent="kirocrew")


def _code(excinfo) -> str:
    return excinfo.value.code


def _evidence() -> Ref:
    return Ref(KIND_SESSION, SESSION, 1)


#: One dispatch payload per target form, both legal.
SESSION_TARGET = {"item": "WI-4", "target": {"kind": "session", "slot": "dashboard:7"}}
CREW_TARGET = {"item": "WI-4", "target": {"kind": "crew", "name": "docs"}}
DONE_REPORT = {"item": "WI-4", "status": "done"}


# --- the table is the spec -------------------------------------------------


def test_the_crew_kind_declares_exactly_the_two_contract_types():
    """Two, because two is how many crew types carry a stated contract.

    Asserted as an exact set rather than a count: a count says a table has the
    right size, and the failure worth catching is a table that has the right size
    and the wrong members.
    """
    assert set(CREW_ENTRY_TYPES) == {DISPATCH, REPORT}


def test_the_registry_answers_per_kind_and_the_two_tables_are_distinct():
    assert set(ENTRY_TYPES) == {KIND_CREW, KIND_SESSION}
    assert ENTRY_TYPES[KIND_CREW] is CREW_ENTRY_TYPES
    assert ENTRY_TYPES[KIND_SESSION] is SESSION_ENTRY_TYPES
    # No type is declared under both kinds. ``message`` is owned by both, so a
    # shared declaration would be the plausible mistake: the two carry different
    # ``data`` and one table for both would validate a crew's payload against a
    # session's shape.
    assert set(CREW_ENTRY_TYPES).isdisjoint(SESSION_ENTRY_TYPES)


def test_a_declaration_is_selected_by_kind_not_by_type_alone():
    assert declaration_for(KIND_CREW, DISPATCH) is CREW_ENTRY_TYPES[DISPATCH]
    # The same type name against the other kind answers None, which is what makes
    # ``validate_data`` unable to check one kind's payload against the other's.
    assert declaration_for(KIND_SESSION, DISPATCH) is None
    assert declaration_for(KIND_CREW, "session/opened") is None


#: The field tables from ``docs/reference/crew-log/crew-types.md``, transcribed:
#: ``(name, required)`` per declared key, members flattened with a dotted path.
#: Required here is the DECLARATION's required, which is why the two conditional
#: members of ``target`` read as optional -- a conditional requirement has no
#: spelling in the registry and is the writer's obligation instead.
SPEC_FIELDS: dict[str, tuple[tuple[str, bool], ...]] = {
    DISPATCH: (
        ("item", True),
        ("target", True),
        ("target.kind", True),
        ("target.slot", False),
        ("target.name", False),
        ("brief", False),
    ),
    REPORT: (
        ("item", True),
        ("status", True),
        ("credits", False),
        ("summary", False),
    ),
}


def _flatten(fields, prefix: str = "") -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for spec in fields:
        out.append((f"{prefix}{spec.name}", spec.required))
        out.extend(_flatten(spec.fields, f"{prefix}{spec.name}."))
    return out


@pytest.mark.parametrize("entry_type", sorted(SPEC_FIELDS))
def test_each_declaration_matches_the_reference_page_field_for_field(entry_type):
    assert _flatten(CREW_ENTRY_TYPES[entry_type].fields) == list(SPEC_FIELDS[entry_type])


def test_the_report_status_enum_covers_its_one_writer(monkeypatch):
    """Derived from the work board's tuple, so the writer cannot outgrow it.

    The failure this prevents is a status the board gains and this enum does not:
    a CLOSED enum then refuses the entry, the refusal is permanent, and the record
    is destroyed rather than the mistake caught.
    """
    assert set(WORK_WORKER_STATUSES) <= set(CREW_REPORT_STATUSES)
    # The spec's own four are present too, including the one no writer produces
    # yet, because the page states them as the vocabulary.
    assert {"done", "blocked", "failed", "progress"} <= set(CREW_REPORT_STATUSES)
    # ``question`` survives under its own name. Folding it into ``blocked`` would
    # erase which party has to act, which is the only thing the two differ by.
    assert "question" in CREW_REPORT_STATUSES
    assert len(CREW_REPORT_STATUSES) == len(set(CREW_REPORT_STATUSES))


def test_the_two_closed_enums_are_the_ones_the_writer_clamps():
    target = next(spec for spec in CREW_ENTRY_TYPES[DISPATCH].fields if spec.name == "target")
    target_kind = next(spec for spec in target.fields if spec.name == "kind")
    status = next(spec for spec in CREW_ENTRY_TYPES[REPORT].fields if spec.name == "status")
    assert target_kind.enum_closed and target_kind.enum == CREW_TARGET_KINDS
    assert status.enum_closed and status.enum == CREW_REPORT_STATUSES


def test_render_markdown_renders_the_crew_table():
    rendered = render_markdown(KIND_CREW)
    assert f"# Declared `{KIND_CREW}` crew log entry types" in rendered
    for spec in CREW_ENTRY_TYPES.values():
        assert f"## `{spec.type}`" in rendered
        assert spec.summary in rendered
    # The nested members are rendered under their dotted path, so the generated
    # table states the same keys the page's field column does.
    for dotted in ("target.kind", "target.slot", "target.name"):
        assert f"| `{dotted}` |" in rendered
    # And it is the CREW table: no session type leaks into it.
    assert "## `session/opened`" not in rendered


# --- the validator accepts what the spec calls valid ----------------------


@pytest.mark.parametrize("payload", [SESSION_TARGET, CREW_TARGET])
def test_both_target_forms_are_accepted(payload):
    validate_data(KIND_CREW, DISPATCH, payload)
    assert _crew().append(DISPATCH, payload, src="crew:conductor").seq == 1


def test_a_dispatch_may_carry_a_brief():
    payload = {**SESSION_TARGET, "brief": "drive it green"}
    validate_data(KIND_CREW, DISPATCH, payload)


@pytest.mark.parametrize("status", CREW_REPORT_STATUSES)
def test_every_declared_status_is_accepted(status):
    validate_data(KIND_CREW, REPORT, {"item": "WI-4", "status": status})


def test_a_report_may_carry_credits_as_an_int_or_a_float():
    for credits in (0, 1, 0.21):
        validate_data(KIND_CREW, REPORT, {**DONE_REPORT, "credits": credits})


# --- the validator refuses what the spec calls invalid --------------------


#: One refusal per rule the declaration states, each naming the path it refuses.
REFUSALS: tuple[tuple[str, dict, str], ...] = (
    # A dispatch with no target names nobody.
    (DISPATCH, {"item": "WI-4"}, "data.target"),
    (DISPATCH, {"target": {"kind": "crew", "name": "docs"}}, "data.item"),
    # The target's own kind is required and closed.
    (DISPATCH, {"item": "WI-4", "target": {"slot": "dashboard:7"}}, "data.target.kind"),
    (DISPATCH, {"item": "WI-4", "target": {"kind": "app"}}, "data.target.kind"),
    # Wrong JSON types, at the top level and inside the object.
    (DISPATCH, {"item": 4, "target": {"kind": "crew", "name": "docs"}}, "data.item"),
    (DISPATCH, {"item": "WI-4", "target": "dashboard:7"}, "data.target"),
    (DISPATCH, {"item": "WI-4", "target": {"kind": "session", "slot": 7}}, "data.target.slot"),
    # An undeclared key is refused rather than silently dropped.
    (DISPATCH, {**SESSION_TARGET, "targets": {}}, "data.targets"),
    (
        DISPATCH,
        {"item": "WI-4", "target": {"kind": "crew", "name": "d", "slug": "d"}},
        "data.target.slug",
    ),
    # The report's own two required fields, and its closed status.
    (REPORT, {"status": "done"}, "data.item"),
    (REPORT, {"item": "WI-4"}, "data.status"),
    (REPORT, {"item": "WI-4", "status": "finished"}, "data.status"),
    (REPORT, {**DONE_REPORT, "credits": "0.21"}, "data.credits"),
    (REPORT, {**DONE_REPORT, "summary": ["done"]}, "data.summary"),
    (REPORT, {**DONE_REPORT, "typo": 1}, "data.typo"),
)


@pytest.mark.parametrize(("entry_type", "payload", "field"), REFUSALS)
def test_a_malformed_crew_payload_is_refused_naming_its_field(entry_type, payload, field):
    with pytest.raises(CrewLogError) as caught:
        validate_data(KIND_CREW, entry_type, payload)
    assert _code(caught) == lg.CODE_BAD_DATA_FIELD
    assert caught.value.field == field


@pytest.mark.parametrize(("entry_type", "payload", "field"), REFUSALS)
def test_a_refused_append_leaves_the_crew_log_byte_identical(entry_type, payload, field):
    """The refusal happens before any byte is written, on the real append path.

    Asserted through ``append`` rather than through ``validate_data`` alone: the
    declaration only protects the record if the store consults it, and the bytes
    are what proves the refusal came before the write rather than after it.
    """
    crew = _crew()
    path = lg.crew_log_path(KIND_CREW, CREW)
    before = path.read_bytes()
    with pytest.raises(CrewLogError) as caught:
        crew.append(entry_type, payload, src="crew:conductor", ref=_evidence())
    assert _code(caught) == lg.CODE_BAD_DATA_FIELD
    assert path.read_bytes() == before
    assert crew.last_seq == 0


# --- ownership, both directions, with the mutation check ------------------


@pytest.mark.parametrize("entry_type", [DISPATCH, REPORT])
def test_a_crew_type_is_refused_in_a_session_log(entry_type):
    session = _session()
    path = lg.crew_log_path(KIND_SESSION, SESSION)
    before = path.read_bytes()
    with pytest.raises(CrewLogError) as caught:
        session.append(entry_type, DONE_REPORT, src="gateway")
    assert _code(caught) == lg.CODE_EVENT_TYPE_NOT_OWNED
    assert caught.value.field == "type"
    assert path.read_bytes() == before


@pytest.mark.parametrize("entry_type", ["session/opened", "turn/started", "work/recorded"])
def test_a_session_type_is_refused_in_a_crew_log(entry_type):
    crew = _crew()
    path = lg.crew_log_path(KIND_CREW, CREW)
    before = path.read_bytes()
    with pytest.raises(CrewLogError) as caught:
        crew.append(entry_type, {}, src="gateway")
    assert _code(caught) == lg.CODE_EVENT_TYPE_NOT_OWNED
    assert caught.value.field == "type"
    assert path.read_bytes() == before


@pytest.mark.parametrize("entry_type", [DISPATCH, REPORT])
def test_the_ownership_refusal_survives_a_registry_that_lost_its_crew_table(
    entry_type, monkeypatch
):
    """The mutation check: drop the crew declarations and the refusal must hold.

    ``check_ownership`` and ``validate_data`` refuse different things, and the
    session-log direction could be read as either -- a crew type in a session log
    is both unowned and undeclared there. Removing the crew table proves which
    one answers: ownership does, so the refusal keeps its own code and the entry
    is still refused when the declaration is gone.
    """
    monkeypatch.setitem(ENTRY_TYPES, KIND_CREW, {})
    assert declaration_for(KIND_CREW, entry_type) is None
    with pytest.raises(CrewLogError) as caught:
        check_ownership(KIND_SESSION, entry_type, "gateway")
    assert _code(caught) == lg.CODE_EVENT_TYPE_NOT_OWNED
    # And the crew's own log still accepts it, now unvalidated -- which is the
    # state this change replaced, so the two layers are demonstrably separate.
    check_ownership(KIND_CREW, entry_type, "crew:conductor")
    validate_data(KIND_CREW, entry_type, {"anything": 1})


def test_removing_the_declaration_is_what_stops_the_payload_refusal(monkeypatch):
    """The other half of the mutation check, on the validator rather than ownership.

    With the table present a missing ``target`` is refused; with it removed the
    same payload passes. That is the discriminating evidence that the refusal is
    the declaration's and not some other check that happens to reject the entry.
    """
    with pytest.raises(CrewLogError):
        validate_data(KIND_CREW, DISPATCH, {"item": "WI-4"})
    monkeypatch.setitem(ENTRY_TYPES, KIND_CREW, {})
    validate_data(KIND_CREW, DISPATCH, {"item": "WI-4"})


def test_the_owned_domains_are_the_spec_s_eight_and_the_two_types_sit_inside_them():
    assert TYPE_OWNERSHIP[KIND_CREW] == frozenset(
        {"member", "activity", "slot", "patrol", "message", "crew", "item", "memory"}
    )
    # Prefix-based ownership is why a new action under ``crew`` needs no registry
    # change: the two declared types are owned by a domain that was already there.
    for entry_type in (DISPATCH, REPORT):
        assert entry_type.split("/", 1)[0] in TYPE_OWNERSHIP[KIND_CREW]
