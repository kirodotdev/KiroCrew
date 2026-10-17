"""The contract, wired: the reader derives the numbers and the writer is refused them.

The types and the parity gate prove the contract is COHERENT. They cannot prove it is
CONNECTED -- ``build_pipeline_board`` type-checks perfectly with no caller at all, which
is exactly the state the previous commit left it in. These are the tests that fail when
the wire is cut.

Two ends, and they fail differently on purpose:

* the WRITER (:func:`kiro_crew.agent_panel.publish`) refuses a payload that is not a
  :class:`PipelineBoardJudgment` and names the key, because the publisher is an agent
  handing over JSON and no type checker is anywhere near it. This is where ``you`` being
  the string ``"Raymond"`` stops, while the author is still present to fix the call.
* the READER (``_panel_record``) replaces the data with the provider's output, so a
  record already on disk in the free shape still renders the log's own numbers -- and
  says it did.

The load-bearing case is :func:`test_an_absent_work_board_is_not_a_board_of_zeros`. Every
other test here fails loudly when the wiring breaks; that one fails SILENTLY in
production, as a board of zeros a reader cannot tell from a real empty fleet.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import agent_panel
from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import agent_panel as panel_routes
from kiro_crew.pipeline_board_contract import (
    BOARD_TEMPLATE_ID,
    CONTRACT_VERSION,
    UNSAID,
    JudgmentError,
    validate_judgment,
)
from kiro_crew.work_vocab import WORK_FOLD_NAME

SLOT = "member-pipeline-conductor"
UNIT = "acp-pipeline-conductor"
CREW = "KiroCrew Pipeline Conductor"  # brand-ok: slugified into a template id, not prose
SLUG = "kirocrew-pipeline-conductor"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no warm fold carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


# ---------------------------------------------------------------------------
# the fold name this reader asks for must be one the kernel registers
# ---------------------------------------------------------------------------


def test_the_work_fold_name_is_one_the_kernel_actually_registers() -> None:
    """``WORK_FOLD_NAME`` names a real fold.

    The reader asks for a projection BY NAME across a package boundary, so a rename in
    the registry cannot be caught by any type. It would make the read raise, which the
    reader's own totality contract turns into a warning and an unchanged record -- a
    blank panel with the numbers silently never arriving. Cheap to pin, invisible
    otherwise.
    """
    assert WORK_FOLD_NAME in crew_log.FOLD_NAMES


# ---------------------------------------------------------------------------
# the writer: the publisher's half is checked at run time
# ---------------------------------------------------------------------------


def test_a_bare_name_under_you_is_refused_and_the_key_is_named() -> None:
    """THE live defect, refused at its source.

    ``you`` arrived as ``"Raymond"`` and rendered on the line that must say what to DO.
    The provider's gate turns such a value into UNSAID on the way out, but that is
    recovery; this is the publisher being told, while it can still fix the call.
    """
    with pytest.raises(JudgmentError) as caught:
        validate_judgment({"you": "Raymond"})
    assert caught.value.key == "you"
    assert "string" in str(caught.value)


def test_a_nested_object_under_you_is_refused_by_its_inner_key() -> None:
    """A dict of dicts is refused, and the refusal names the INNER key.

    A check that only asked "is ``you`` a dict" would pass this and the template would
    render ``[object Object]`` in the action line. The error has to be actionable, so it
    says ``you.open`` rather than ``you``.
    """
    with pytest.raises(JudgmentError) as caught:
        validate_judgment({"you": {"open": {"text": "approve PR 14111"}}})
    assert caught.value.key == "you.open"


def test_a_number_has_nowhere_to_go_and_the_refusal_says_so() -> None:
    """The publisher cannot reach a count, and learns why rather than being ignored.

    Ignoring an unknown key is worse than refusing it: the panel would render without
    the value and look merely incomplete, when the real answer is that the number now
    comes from the log and the publisher has no say in it.
    """
    with pytest.raises(JudgmentError) as caught:
        validate_judgment({"lede": "six items", "total": 6})
    assert caught.value.key == "total"
    assert "provider derives every number" in str(caught.value)


def test_an_omitted_judgment_key_is_the_empty_judgment_not_an_error() -> None:
    """Saying nothing is allowed, and is not the same as saying it wrong.

    A publisher that read no forge has no ``checks``, which is a true statement with a
    named value under this contract. The asymmetry against the provider is deliberate:
    an offer may be silent, a contractual answer may not.
    """
    out = validate_judgment({"lede": "six items open"})
    assert out == {"lede": "six items open", "you": {}, "notes": {}, "checks": {}}


def test_the_empty_judgment_constant_is_not_aliased_by_a_validated_one() -> None:
    """Two validated judgments share no mutable state with each other or the constant.

    ``dict(EMPTY_JUDGMENT)`` copies the mapping and SHARES its three inner dicts, so one
    caller adding an entry to ``you`` would write it into the module constant and hand
    it to every later publisher -- a cross-crew leak with no error anywhere.
    """
    first = validate_judgment({})
    second = validate_judgment({})
    first["you"]["open"] = "leaked into the constant"
    assert second["you"] == {}


def test_every_judgment_field_has_a_runtime_check() -> None:
    """The type and its checks cannot drift.

    Following ``WORK_CONDUCTOR_FIELDS``' own reason for existing: two things that must
    agree read ONE table. A field added to the type with no checker here would be
    accepted unvalidated, which is the hole this whole commit closes.
    """
    from kiro_crew.pipeline_board_contract import (
        _JUDGMENT_CHECKS,
        PipelineBoardJudgment,
    )

    assert set(_JUDGMENT_CHECKS) == set(PipelineBoardJudgment.__annotations__)


def test_publish_refuses_a_free_shape_payload_on_the_contract_template() -> None:
    """``publish`` itself refuses, not only the validator in isolation.

    The validator being correct proves nothing if the store never calls it. This is the
    call, through the real publish path, with the real template id.
    """
    with pytest.raises(agent_panel.PanelError) as caught:
        agent_panel.publish(
            SLUG,
            template=BOARD_TEMPLATE_ID,
            data={"title": "fleet", "fleet": 6, "waiting_on_you": "Raymond"},
            crew=CREW,
        )
    assert caught.value.code == "judgment_rejected"
    assert "title" in str(caught.value) or "fleet" in str(caught.value)


def test_publish_still_accepts_any_shape_on_the_generic_template() -> None:
    """``default`` keeps the free shape. Binding it to this contract is the same
    mistake pointed the other way: every crew would lose its own vocabulary.
    """
    record = agent_panel.publish(
        "some-other-crew",
        template="default",
        data={"cycle": 3, "holding": "two workers", "anything": [1, 2]},
        crew="Some Other Crew",
    )
    assert record["data"] == {"cycle": 3, "holding": "two workers", "anything": [1, 2]}


# ---------------------------------------------------------------------------
# the reader: the numbers come from the fold
# ---------------------------------------------------------------------------


def _unit() -> None:
    CrewLog.create(lg.KIND_SESSION, UNIT, owner="owner", agent=CREW, slot=SLOT)


def _work(*, action: str, **fields: Any) -> None:
    """One ``work/recorded`` entry on this slot's board, appended AND acknowledged.

    The append must LAND: the emitter answers ``False`` on a refused one, and a missing
    entry makes the board smaller -- the direction several of these tests measure, so an
    unchecked append would manufacture the result.
    """
    payload: dict[str, Any] = {"slot": SLOT, "actor": "conductor", "by": SLOT, "action": action}
    payload.update({name: value for name, value in fields.items() if value is not None})
    assert crew_log_emit.on_work_recorded(UNIT, payload, timeout=5.0) is True
    crew_log_emit.flush(timeout=5.0)


def _board(*, items: int = 2, rounds: int = 3) -> list[str]:
    """A goal plus *items* created items, each bound to a worker. Returns their ids."""
    _work(action="goal", goal="ship the board", round=rounds)
    ids: list[str] = []
    for n in range(items):
        item_id = f"it_{n:08x}"
        ids.append(item_id)
        _work(
            action="create",
            item_id=item_id,
            title=f"item {n}",
            acceptance={"kind": "human_approval"},
            round=rounds,
        )
        _work(action="bind", item_id=item_id, worker_session_key=f"chat-99-w{n}")
    return ids


def _record(data: dict[str, Any], *, template: str = BOARD_TEMPLATE_ID) -> dict[str, Any]:
    """A stored panel record, written through the real publish path where it will go."""
    return agent_panel.publish(SLUG, template=template, data=data, crew=CREW)


def _read(record: dict[str, Any], monkeypatch) -> dict[str, Any]:
    """*record* as ``_panel_record`` serves it, with selection pinned to this record.

    Selection is ``_published_record``'s own story and has its own tests; pinning it
    keeps a failure here attributable to the provider step rather than to which of the
    file and the fold won.
    """
    monkeypatch.setattr(panel_routes, "_published_record", lambda *a, **k: dict(record))
    out = panel_routes._panel_record(SLOT, SLUG, str(record["crew_key"]))
    assert out is not None
    return out


def test_the_board_numbers_come_from_the_fold_not_from_the_publisher(monkeypatch) -> None:
    """THE claim: counts and card ids are the log's, and the publisher never typed them.

    Four items in the log against a judgment that holds no numbers at all -- because
    under this contract it structurally cannot -- and the rendered board still counts
    four.
    """
    _unit()
    ids = _board(items=4)
    record = _record({"lede": "the fleet is holding", "you": {ids[0]: "approve PR 14111"}})

    served = _read(record, monkeypatch)
    data = served["board"]
    # ``data`` is left exactly as published: the drawer's native docked card walks it and
    # prints its leading keys, so the derived board travels on its own key instead.
    assert served["data"] == {"lede": "the fleet is holding", "you": {ids[0]: "approve PR 14111"}}

    assert data["progress"]["total"] == 4
    assert data["contract_version"] == CONTRACT_VERSION
    assert data["meta"]["revision"] == 3
    open_column = next(c for c in data["columns"] if c["name"] == "open")
    assert [card["id"] for card in open_column["cards"]] == ids
    assert open_column["cards"][0]["sub"] == "chat-99-w0"
    # The judgment's own two fields survive; they are the half no log can produce.
    assert data["lede"] == "the fleet is holding"
    assert open_column["cards"][0]["you"] == "approve PR 14111"


def test_an_absent_work_board_is_not_a_board_of_zeros(monkeypatch) -> None:
    """A crew with no work log keeps its published data. THE silent failure.

    Every other test here fails loudly when the wiring breaks. This one fails as a
    complete board of zeros that a reader cannot tell from a real empty fleet -- so the
    reader must be handed "not said" instead, which is what leaving the record alone
    produces through the template's three-state renderer.
    """
    _unit()  # a session log exists; no work entry was ever recorded under it
    record = _record({"lede": "nothing started yet"})

    served = _read(record, monkeypatch)

    assert served["data"] == {"lede": "nothing started yet"}
    # No derived board AT ALL, which is what the document composer falls back on: it
    # renders ``data``, whose missing sections the template reads as absent. An empty
    # board object here would be a board of zeros wearing the right key.
    assert "board" not in served


def test_a_board_with_a_goal_and_no_items_does_render_its_zeros(monkeypatch) -> None:
    """The other side of that line: an EXISTING board's zeros are true.

    Without this, "absent means leave it alone" would be satisfied by a reader that
    never renders a board at all, and the distinction being drawn would be untested in
    the direction that makes it a distinction.
    """
    _unit()
    _work(action="goal", goal="about to start", round=1)
    record = _record({"lede": "just opened"})

    data = _read(record, monkeypatch)["board"]

    assert data["progress"]["total"] == 0
    assert all(column["cards"] == [] for column in data["columns"])


def test_an_unreadable_work_fold_warns_and_serves_the_published_record(monkeypatch, caplog) -> None:
    """A damaged log is a warning and an unchanged record, never a 500.

    Matching the panel fold's existing totality contract in the same function: this
    route renders somebody's drawer, and the published record may still have something
    to show. WARNING because a panel whose numbers stopped updating has no other trace
    and only an operator can repair the log.
    """
    _unit()
    _board(items=2)
    record = _record({"lede": "still here"})

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the log is damaged")

    monkeypatch.setattr(panel_routes.projection, "read_slot_projection", _boom)
    with caplog.at_level("WARNING"):
        served = _read(record, monkeypatch)

    assert served["data"] == {"lede": "still here"}
    assert "board" not in served
    assert any("work fold unreadable" in r.message for r in caplog.records)


def test_a_free_shape_record_already_on_disk_is_replaced_and_disclosed(monkeypatch, caplog) -> None:
    """A record predating the contract renders the log's numbers AND says it was
    overridden.

    The writer refuses this shape now, but records written before it are on disk and
    their author is gone, so the reader cannot refuse anything useful. What it must not
    do is override silently: a provider quietly replacing published values is the same
    undisclosed authority this contract took away from the publisher.
    """
    _unit()
    _board(items=2)
    record = _record({"lede": "ok"})
    # The shape publish now refuses, forced past it to stand for a record already stored.
    record["data"] = {"title": "fleet", "fleet": 6, "waiting_on_you": "Raymond"}

    with caplog.at_level("WARNING"):
        served = _read(record, monkeypatch)

    assert served["board"]["progress"]["total"] == 2
    assert served["contract_replaced"] == ["fleet", "title", "waiting_on_you"]
    assert served["board"]["lede"] is None, "a dropped judgment must read as not said"
    assert any("not a board judgment" in r.message for r in caplog.records)


def test_the_drawer_serializer_accepts_every_key_the_provider_adds(monkeypatch) -> None:
    """The provider's record survives the drawer's allow-list, key for key.

    ``_panel_meta`` is an allow-list over the record's OWN keys that RAISES on a key
    classified neither served nor withheld, and the route turns that raise into the
    empty state. So a derived key this provider adds and nobody classifies does not
    show up as a wrong number -- it shows up as every conductor's drawer reading
    "nothing published", with the defect only in a server log. Nothing above notices:
    the provider tests here stop at the record, and the serializer's own test reads
    only the keys the FOLD produces, not the ones this route adds after it.

    Asserted over the record the provider actually returns rather than a hand-listed
    pair of names, so a third derived key added later is covered by this test the day
    it is added.
    """
    _unit()
    _board(items=2)
    record = _record({"lede": "ok"})
    record["data"] = {"title": "fleet", "fleet": 6}  # forces contract_replaced too
    served = _read(record, monkeypatch)
    assert "board" in served and "contract_replaced" in served, "provider added neither key"

    meta = panel_routes._panel_meta(served)

    # Withheld, not served: the composed document renders the board server-side, and
    # the docked card walks ``data``. A response field nothing renders has no reader.
    assert "board" not in meta
    assert "contract_replaced" not in meta
    # The publisher's judgment still reaches the client, which is what the card reads.
    assert meta["data"] == {"title": "fleet", "fleet": 6}


def test_another_template_is_served_exactly_as_published(monkeypatch) -> None:
    """One template, one contract. Every other record passes through untouched.

    The contract is ONE dashboard's input type. A reader that rewrote every crew's data
    would be the general panel dict again, just built on the reader's side.
    """
    _unit()
    _board(items=2)
    record = _record({"cycle": 3, "holding": "two workers"}, template="default")

    served = _read(record, monkeypatch)
    assert served["data"] == {"cycle": 3, "holding": "two workers"}
    assert "board" not in served, "a template with no contract must not be given one"


def test_one_items_action_never_appears_on_another_items_card(monkeypatch) -> None:
    """An action reaches the card it is about, and no other.

    ``you`` keyed by COLUMN stamps one item's sentence onto every card beside it: two
    open items both reading one item's action when only one of them is the item it
    names. That is the same misattribution as an owner name in the cell -- the gate
    removes it by value, and the key has to not reintroduce it by shape.
    """
    _unit()
    ids = _board(items=2)
    record = _record({"you": {ids[0]: "approve PR 14111"}})

    cards = next(c for c in _read(record, monkeypatch)["board"]["columns"] if c["name"] == "open")[
        "cards"
    ]
    by_id = {card["id"]: card["you"] for card in cards}

    assert by_id[ids[0]] == "approve PR 14111"
    assert by_id[ids[1]] is None, "the other card was given an action about a different item"


def test_the_unsaid_sentinel_never_reaches_the_rendered_data(monkeypatch) -> None:
    """``panel_payload`` ran: the island carries ``null``, never ``__unsaid__``.

    The sentinel exists so the provider cannot forget to answer; it is not a value any
    reader should see. Scanning the serialized JSON rather than a few fields, because
    the one that leaks will be the one not named in an assertion.
    """
    _unit()
    _board(items=2)
    record = _record({"lede": "the fleet is holding"})

    data = _read(record, monkeypatch)["board"]

    assert UNSAID not in json.dumps(data)
    # And the absence is a real one: this publisher supplied only a lede, so every
    # per-card judgment is unsaid. A payload with no nulls at all would mean the
    # conversion never ran rather than that nothing needed converting.
    open_cards = next(c for c in data["columns"] if c["name"] == "open")["cards"]
    assert [card["of"] for card in open_cards] == [None, None]
    assert [card["you"] for card in open_cards] == [None, None]


def test_the_document_carries_the_board_and_the_docked_data_stays_published(
    monkeypatch,
) -> None:
    """One record, two surfaces, two keys -- and each surface gets the right one.

    The composed DOCUMENT is what the contract describes, so it must carry the derived
    board. The drawer's DOCKED card is native React that walks ``data``'s own key order
    and prints its leading entries as headline tiles, so a derived board placed there
    made a conductor's compact card lead with "contract version 1" and "omitted 0" --
    the two least useful numbers on it. The publisher's sentence is what belongs in a
    one-line card.

    Asserted through ``render_record``, the real composer, rather than by inspecting the
    record: the record having a ``board`` key proves nothing about which key is rendered.
    """
    _unit()
    _board(items=2)
    record = _record({"lede": "the fleet is holding"})

    served = _read(record, monkeypatch)
    html = agent_panel.render_record(served)
    assert html is not None

    island = json.loads(
        html.split('id="kirocrew-panel-data">', 1)[1]
        .split("</script>", 1)[0]
        .replace("\\u003c", "<")
    )
    assert island["progress"]["total"] == 2, "the document did not get the derived board"
    assert island["contract_version"] == CONTRACT_VERSION
    # And the key the native card walks is still the publisher's own, unexpanded.
    assert served["data"] == {"lede": "the fleet is holding"}


def test_a_record_with_no_board_still_composes_from_its_published_data(monkeypatch) -> None:
    """The fallback every other crew takes. Without it the split would blank them all."""
    _unit()
    record = _record({"cycle": 3, "holding": "two workers"}, template="default")

    served = _read(record, monkeypatch)
    assert "board" not in served
    html = agent_panel.render_record(served)
    assert html is not None
    island = json.loads(
        html.split('id="kirocrew-panel-data">', 1)[1]
        .split("</script>", 1)[0]
        .replace("\\u003c", "<")
    )
    assert island == {"cycle": 3, "holding": "two workers"}
