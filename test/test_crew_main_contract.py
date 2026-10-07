"""The root card's contract: folded numbers, three model sentences, nothing else.

``mypy src/kiro_crew/`` checks both ends of :func:`build_crew_main` -- the fold renders in,
:class:`~kiro_crew.crew_main_contract.CrewMainDerived` out. These cases cover what a type
checker cannot: that no branch of the provider leaves a field unanswered, that absence is
three-state in WORDS rather than a zero, that no value is a percentage, that a model can
reach no derived field, and that a card whose model-written part carries a number is
refused at the publish seam rather than shown.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from kiro_crew.crew_main_contract import (
    CARD_FIELDS,
    DERIVED_FIELDS,
    FOLD_UNREADABLE,
    JUDGMENT_FIELDS,
    JUDGMENT_TEXT_LIMIT,
    NOT_RECORDED,
    UNREADABLE,
    CrewMainData,
    CrewMainReads,
    build_crew_main,
    card_data_payload,
    merge_crew_main,
)
from kiro_crew.dashboard import card_lifecycle
from kiro_crew.dashboard.card_lifecycle import (
    _ROOT_PROMPT,
    _model_wrote_number,
    _root_card_output,
    is_root_session,
)
from kiro_crew.dashboard.dynamic_cards import (
    MAX_DATA_BYTES,
    MAX_HTML_BYTES,
    CardBudget,
    normalize_card,
)

FOLD_NAMES = ("status", "usage", "approvals", "work")

#: A layout a model might return: CSS with digits in it, every fact bound, three sentences.
LAYOUT = (
    "<style>.c{padding:12px;gap:4px}@media(max-width:320px){.c{padding:2px}}</style>"
    '<section class="c"><h3 data-dashboard-field="lede"></h3>'
    + "".join(
        f'<p>Fact <span data-dashboard-field="{name}"></span></p>'
        for name in sorted(DERIVED_FIELDS)
    )
    + '<p data-dashboard-field="you"></p><p data-dashboard-field="notes"></p></section>'
)


@pytest.fixture(autouse=True)
def _isolate_session_tree(monkeypatch):
    """Drop the process's session-tree projection around every case in this file.

    ``is_root_session`` reads that projection, which is a process singleton keyed to one
    store, so a case here must not leave a fold behind for the next file to read.
    """
    from kiro_crew.crew_log import session_tree_projection

    session_tree_projection.reset_for_tests()
    # Seeded over the case's own empty store: the root gate refuses an unseeded tree.
    session_tree_projection.projection().ensure_seeded()
    # On, because no card is generated with the log off and off is the suite default.
    monkeypatch.setattr(card_lifecycle, "crew_log_enabled", lambda: True)
    yield
    session_tree_projection.reset_for_tests()


class _Slot:
    """The slot fields the card's gates and publisher actually read, and no others."""

    def __init__(self, key: str, **kwargs: object) -> None:
        self.key = key
        self.messages: list[dict] = kwargs.pop("messages", [{"role": "user", "content": "hi"}])  # type: ignore[assignment]
        self._created_by = kwargs.pop("created_by", "")
        self.executor = kwargs.pop("executor", "")
        self.memory_mode = kwargs.pop("memory_mode", "persistent")
        self._dashboard_card_identity = kwargs.pop("identity", "id-" + key)
        self.linked_session_key = ""
        for name, value in kwargs.items():
            setattr(self, name, value)


def _slot(key: str, **kwargs: object) -> _Slot:
    return _Slot(key, **kwargs)


class _Log:
    """The transcript reads ``_generate`` makes."""

    def __init__(self, slot: _Slot) -> None:
        self._slot = slot
        self.generation = 0

    @contextmanager
    def publication_hold(self, key: str):
        yield

    def session_mtime(self, key: str) -> int:
        return 1

    def rotation_generation(self, key: str) -> int:
        return self.generation

    def chained_keys(self, key: str) -> list[str]:
        return [key]

    def derive_recent(self, key: str, max_messages: int, roles: object = None) -> list[dict]:
        return self._slot.messages[-max_messages:]


class _State:
    """Only what CardLifecycle touches: the slot table and a broadcast sink."""

    def __init__(self, *slots: _Slot, log: object = None) -> None:
        self._slots = {slot.key: slot for slot in slots}
        self._background_tasks: set = set()
        self.conversation_log = log
        self.sessions = object()
        self.frames: list[tuple[str, object]] = []

    def flush_slot_now(self, slot: object) -> None:
        return None

    def broadcast_ws_owners(self, kind: str, payload: object) -> None:
        self.frames.append((kind, payload))


def _lifecycle(*slots: _Slot, enabled: bool = False, log: object = None):
    from kiro_crew.dashboard.card_lifecycle import CardLifecycle

    return CardLifecycle(_State(*slots, log=log), enabled=enabled)


def _reads(**overrides: object) -> CrewMainReads:
    """A board where every fold read and every value is present."""
    base: dict[str, object] = {
        "status": {
            "lifecycle": "open",
            "turn_open": True,
            "turns_completed": 12,
            "turns_refused": 1,
            "entries": 480,
            "agent": "kirocrew-conductor",
            "model": "a-model",
            "last_time": int(time.time() * 1000),
        },
        "usage": {
            "credits": 3.5,
            "credits_by_source": {"subagent": {"credits": 1.25, "reported": 4}},
            "tokens": {"total": 1215},
        },
        "approvals": {
            "requested": 9,
            "decided": 7,
            "pending": 2,
            "by_decision": {"allow": 6, "deny": 1},
        },
        "work": {
            "items": [
                {"state": "open", "status": "progress"},
                {"state": "open", "status": "blocked"},
                {"state": "open", "status": "question"},
                {"state": "accepted", "status": "done"},
                {"state": "rejected", "status": None},
            ],
            "omitted": 2,
        },
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


def _reply(**data: str) -> str:
    """A model reply carrying the full layout and the given sentences."""
    return json.dumps({"html": LAYOUT, "data": data})


# --------------------------------------------------------------------------
# the provider is TOTAL: no branch can leave a field unanswered
# --------------------------------------------------------------------------


@pytest.mark.parametrize("unreadable", [(), *[(name,) for name in FOLD_NAMES], FOLD_NAMES])
def test_every_field_is_answered_whatever_could_not_be_read(unreadable: tuple[str, ...]) -> None:
    """Each fold failing alone, all of them failing, and none -- every field present.

    An absent key binds nothing and the card shows an EMPTY cell, which a reader cannot
    tell from a recorded zero.
    """
    derived = build_crew_main(_reads(**{name: FOLD_UNREADABLE for name in unreadable}))
    assert frozenset(derived) == DERIVED_FIELDS
    for field, value in derived.items():
        assert isinstance(value, str) and value.strip(), f"{field} answered nothing"


@pytest.mark.parametrize(
    "reads",
    [
        pytest.param(_reads(**{name: {} for name in FOLD_NAMES}), id="folds-read-but-empty"),
        pytest.param(_reads(work={"items": []}), id="board-with-no-items"),
        pytest.param(_reads(work={}), id="board-with-no-item-list"),
        pytest.param(_reads(usage={"credits_by_source": {}}), id="no-credit-split"),
        pytest.param(_reads(status={"lifecycle": "unknown"}), id="log-no-longer-says"),
    ],
)
def test_every_field_is_answered_for_a_thin_fold(reads: CrewMainReads) -> None:
    derived = build_crew_main(reads)
    assert frozenset(derived) == DERIVED_FIELDS
    for field, value in derived.items():
        assert isinstance(value, str) and value.strip(), f"{field} answered nothing"


def test_a_damaged_value_costs_one_field_and_not_the_card() -> None:
    """Bytes off a file the reader does not control must not raise."""
    derived = build_crew_main(
        _reads(
            status={"lifecycle": "open", "turns_completed": True},
            usage={"credits": float("nan"), "tokens": {"total": -5}},
            approvals={"pending": True, "requested": 9},
        )
    )
    assert derived["turns"] == NOT_RECORDED
    assert derived["tokens"] == NOT_RECORDED
    assert derived["approvals_open"] == NOT_RECORDED
    assert derived["credits"] == UNREADABLE


def test_the_phase_names_a_stop_in_words_never_the_enum() -> None:
    """A reader shown ``end_turn`` is shown a machine word; the field says what happened."""
    finished = build_crew_main(_reads(status={"turn_open": False, "last_stop_reason": "end_turn"}))
    assert finished["phase"] == "the last turn finished"
    novel = build_crew_main(_reads(status={"turn_open": False, "last_stop_reason": "brand_new"}))
    assert novel["phase"] == "the last turn stopped"
    assert "_" not in novel["phase"]


@pytest.mark.parametrize(
    ("lifecycle", "state"),
    [
        pytest.param("open", "session open", id="open"),
        pytest.param("closed", "session closed", id="closed"),
        pytest.param("unknown", "the log no longer says", id="retention-took-it"),
        pytest.param("nonsense", NOT_RECORDED, id="a-word-the-fold-does-not-use"),
    ],
)
def test_the_state_says_which_thing_is_open(lifecycle: str, state: str) -> None:
    """The work fields count items open in the sense of UNRESOLVED, so the state is never
    the bare word "open"."""
    status = {**_reads()["status"], "lifecycle": lifecycle}  # type: ignore[dict-item]
    assert build_crew_main(_reads(status=status))["state"] == state


# --------------------------------------------------------------------------
# absence is three-state, in words
# --------------------------------------------------------------------------


def test_an_unread_fold_and_an_empty_one_read_differently() -> None:
    """The distinction the whole contract rests on: unknown is not the same as none."""
    unread = build_crew_main(_reads(**{name: FOLD_UNREADABLE for name in FOLD_NAMES}))
    empty = build_crew_main(_reads(**{name: {} for name in FOLD_NAMES}))
    assert set(unread.values()) == {UNREADABLE}
    assert UNREADABLE not in set(empty.values())
    for field in DERIVED_FIELDS:
        assert unread[field] != empty[field], field  # type: ignore[literal-required]


def test_no_value_is_ever_a_bare_number_or_a_zero() -> None:
    """A bare count invites the reader to supply the total, and they supply a wrong one."""
    for reads in (_reads(), _reads(**{name: {} for name in FOLD_NAMES})):
        for field, value in build_crew_main(reads).items():
            assert not value.strip().isdigit(), f"{field} is a bare number"
            assert any(ch.isalpha() for ch in value), f"{field} carries no words"


def test_no_value_is_a_percentage() -> None:
    values = list(build_crew_main(_reads()).values())
    assert not [v for v in values if "%" in v or "percent" in v.lower()]


def test_every_count_with_a_denominator_states_it() -> None:
    derived = build_crew_main(_reads())
    for field in (
        "items_open",
        "items_progress",
        "items_blocked",
        "items_done",
        "items_question",
        "approvals_open",
    ):
        assert " of " in derived[field], field  # type: ignore[literal-required]


def test_a_worker_claim_is_not_reported_as_an_acceptance() -> None:
    """``items_done`` counts the conductor's ruling, not the worker's own status."""
    derived = build_crew_main(
        _reads(
            work={
                "items": [
                    {"state": "open", "status": "done"},
                    {"state": "open", "status": "done"},
                    {"state": "accepted", "status": "done"},
                ],
                "omitted": 0,
            }
        )
    )
    assert derived["items_done"] == "1 of 3 items accepted"


def test_dropped_log_entries_are_never_added_into_a_board_total() -> None:
    derived = build_crew_main(_reads(work={"items": [], "omitted": 4}))
    assert "4" in derived["board_omitted"]
    assert "4" not in derived["items_open"]
    clean = build_crew_main(_reads(work={"items": [], "omitted": 0}))
    assert clean["board_omitted"] == "no entries dropped, so these counts cover every log entry"


@pytest.mark.parametrize(
    ("count", "turns", "entries"),
    [(1, "1 turn finished", "1 entry in the log"), (2, "2 turns finished", "2 entries in the log")],
)
def test_a_count_of_one_takes_the_singular(count: int, turns: str, entries: str) -> None:
    status = {"turn_open": False, "turns_completed": count, "entries": count}
    derived = build_crew_main(_reads(status=status))
    assert derived["turns"] == turns
    assert derived["entries"] == entries


@pytest.mark.parametrize(
    ("total", "turns", "shown"),
    [
        pytest.param(0, 4, "no tokens reported", id="zero-after-turns-is-an-absence"),
        pytest.param(0, 0, "0 tokens measured", id="zero-before-any-turn-is-a-zero"),
        pytest.param(512, 4, "512 tokens measured", id="a-real-total"),
    ],
)
def test_a_zero_token_total_after_a_completed_turn_is_not_a_measurement(
    total: int, turns: int, shown: str
) -> None:
    """A completed turn cannot cost zero tokens, so that zero says nothing was reported."""
    derived = build_crew_main(
        _reads(
            status={"turn_open": False, "turns_completed": turns},
            usage={"credits": 1.52, "tokens": {"total": total}},
        )
    )
    assert derived["tokens"] == shown


def test_an_unmetered_subagent_bucket_is_not_a_charge_of_zero() -> None:
    usage = {"credits": 1.0, "credits_by_source": {"subagent": {"credits": 0.0, "reported": 0}}}
    derived = build_crew_main(_reads(usage=usage))
    assert derived["credits_subagents"] == "no sub-agent charge reported"


def test_a_credit_charge_is_shown_to_two_places_and_never_rounded_to_zero() -> None:
    shown = build_crew_main(_reads(usage={"credits": 3.499}))["credits"]
    assert shown == "3.50 credits billed to this crew"
    tiny = build_crew_main(_reads(usage={"credits": 0.00001}))["credits"]
    assert tiny == "less than 0.01 credits billed to this crew"
    assert "e-" not in tiny


# --------------------------------------------------------------------------
# the model cannot reach a number
# --------------------------------------------------------------------------


def test_the_merge_ignores_a_derived_key_even_when_one_reaches_it() -> None:
    """The merge names every field, so a forged judgment carrying a count cannot land."""
    derived = build_crew_main(_reads())
    forged = {"lede": "ok", "you": "", "notes": "", "credits": "999999 credits", "state": "closed"}
    merged = merge_crew_main(derived, forged)  # type: ignore[arg-type]
    for field in DERIVED_FIELDS:
        assert merged[field] == derived[field], field  # type: ignore[literal-required]
    assert merged["lede"] == "ok"
    assert frozenset(merged) == frozenset(CrewMainData.__annotations__) == CARD_FIELDS


def test_a_clean_card_carries_the_folded_numbers_and_the_three_sentences() -> None:
    derived = build_crew_main(_reads())
    reply = _reply(lede="Working through the queue.", you="Rule on the question.", notes="")
    card = _root_card_output(reply, None, derived)
    assert card is not None
    assert card["html"] == LAYOUT
    assert set(card["data"]) == CARD_FIELDS
    for field in DERIVED_FIELDS:
        assert card["data"][field] == derived[field], field  # type: ignore[literal-required]
    assert card["data"]["lede"] == "Working through the queue."
    assert card["data"]["you"] == "Rule on the question."


def _html_reply(html: str, **data: object) -> str:
    return json.dumps({"html": html, "data": data})


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(_reply(lede="Twelve done, 3 to go."), id="digit-in-lede"),
        pytest.param(_reply(lede="Fine.", notes="about 40 turns so far"), id="digit-in-notes"),
        pytest.param(
            _html_reply(LAYOUT.replace("Fact ", "Fact 7 ", 1), lede="Fine."),
            id="digit-in-shown-html",
        ),
        pytest.param(
            _html_reply(LAYOUT.replace("Fact ", "Fact &#55; ", 1), lede="Fine."),
            id="digit-as-a-character-reference",
        ),
        pytest.param(
            _html_reply(LAYOUT, lede="Fine.", credits="many credits"),
            id="model-writes-a-derived-field",
        ),
        pytest.param(_html_reply(LAYOUT, lede="Fine.", you=3), id="count-as-a-json-number"),
        pytest.param(
            _html_reply('<p data-dashboard-field="eta"></p>', lede="Fine."),
            id="binds-a-field-no-fold-fills",
        ),
    ],
)
def test_a_card_whose_model_part_carries_a_number_is_refused(reply: str) -> None:
    """The publish seam: a number the model wrote anywhere it shows refuses the card."""
    assert _root_card_output(reply, None, build_crew_main(_reads())) is None


def test_digits_in_css_are_layout_not_numbers() -> None:
    """``12px`` is not a figure on the card, and refusing it would refuse every layout."""
    own = {"html": LAYOUT, "data": {"lede": "Fine.", "you": "", "notes": ""}}
    assert _model_wrote_number(own) is False


def test_a_data_only_reply_keeps_the_layout_and_refreshes_the_numbers() -> None:
    first = _root_card_output(_reply(lede="Starting."), None, build_crew_main(_reads()))
    assert first is not None
    later = build_crew_main(_reads(work={"items": [{"state": "accepted", "status": "done"}]}))
    second = _root_card_output(json.dumps({"data": {"lede": "Finished."}}), first, later)
    assert second is not None
    assert second["html"] == first["html"]
    assert second["data"]["items_done"] == later["items_done"]
    assert second["data"]["lede"] == "Finished."
    # With no layout to keep, a data-only reply has nothing to publish into.
    assert _root_card_output(json.dumps({"data": {"lede": "Hi."}}), None, later) is None


def test_the_prompt_hands_numbers_over_as_facts_and_forbids_digits() -> None:
    assert "facts" in _ROOT_PROMPT
    assert "NO DIGITS" in _ROOT_PROMPT
    assert "REFUSED" in _ROOT_PROMPT
    for field in JUDGMENT_FIELDS:
        assert field in _ROOT_PROMPT
    assert str(JUDGMENT_TEXT_LIMIT) in _ROOT_PROMPT


def test_the_published_card_fits_the_host_caps_at_their_worst() -> None:
    long = {field: "x" * JUDGMENT_TEXT_LIMIT for field in JUDGMENT_FIELDS}
    for reads in (_reads(), _reads(**{name: FOLD_UNREADABLE for name in FOLD_NAMES})):
        data = card_data_payload(merge_crew_main(build_crew_main(reads), long))  # type: ignore[arg-type]
        assert len(LAYOUT.encode()) <= MAX_HTML_BYTES
        size = sum(len(k.encode()) + len(v.encode()) for k, v in data.items())
        assert size <= MAX_DATA_BYTES, f"{size} data bytes"
        assert normalize_card({"html": LAYOUT, "data": data}) is not None


def test_every_field_name_is_one_the_host_accepts() -> None:
    from kiro_crew.dashboard.dynamic_cards import _FIELD_NAME

    for field in CARD_FIELDS:
        assert _FIELD_NAME.fullmatch(field), field


# --------------------------------------------------------------------------
# whose card this is
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("created_by", "expected"),
    [
        pytest.param("", True, id="a-persons-own-tab"),
        pytest.param("member-bolin", False, id="dispatched-by-a-crew-member"),
        pytest.param("chat-1875-1790253512", False, id="dispatched-by-a-conductor"),
    ],
)
def test_only_a_root_session_takes_a_card(created_by: str, expected: bool) -> None:
    assert is_root_session(type("S", (), {"_created_by": created_by, "key": "s"})()) is expected


def test_a_worker_is_not_eligible_for_a_card_at_all() -> None:
    lifecycle = _lifecycle()
    assert lifecycle._eligible(_slot("root", created_by="")) is True
    assert lifecycle._eligible(_slot("child", created_by="root")) is False


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"executor": "remote"}, id="remote-executor"),
        pytest.param({"memory_mode": "incognito"}, id="incognito"),
    ],
)
def test_a_slot_with_no_local_crew_log_gets_no_card(kwargs: dict) -> None:
    assert _lifecycle()._eligible(_slot("s", **kwargs)) is False


# --------------------------------------------------------------------------
# root means no parent edge, and there are two places an edge can live
# --------------------------------------------------------------------------


class _Node:
    """A session-tree node, as ``fold_tree`` returns it: slot, parent, cycle flag."""

    def __init__(self, slot: str, parent_slot: str | None) -> None:
        self.slot = slot
        self.parent_slot = parent_slot
        self.cycle = False


def _with_tree(monkeypatch, nodes: dict[str, _Node]) -> None:
    """Stand in for the in-memory session-tree projection ``is_root_session`` reads."""
    from kiro_crew.crew_log import session_tree_projection

    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(nodes=lambda: nodes, seeded_for_current_store=True),
    )


def test_an_adopted_slot_is_not_root_even_with_an_empty_created_by(monkeypatch) -> None:
    """The case ``_created_by`` alone cannot see, and the reason the tree is read.

    The adopt verb records a parent edge in the crew log and never touches
    ``_created_by``, so a slot born as a person's own tab and later taken over still
    reads as parentless by that field. Without the tree reading it would be handed a
    panel of its own while its parent already summarises it.
    """
    adopted = _slot("adopted", created_by="")
    _with_tree(monkeypatch, {"adopted": _Node("adopted", "owner")})
    assert is_root_session(adopted) is False
    assert _lifecycle(adopted)._eligible(adopted) is False


def test_a_released_slot_is_root_again(monkeypatch) -> None:
    """A parent edge can be taken away, and the tree is what records that."""
    released = _slot("released", created_by="")
    _with_tree(monkeypatch, {"released": _Node("released", None)})
    assert is_root_session(released) is True


def test_a_dispatched_worker_is_refused_before_the_tree_is_consulted(monkeypatch) -> None:
    """``_created_by`` decides on its own, so a flag-off gateway still refuses a worker.

    The tree is empty here, which is what "the crew log is off" looks like. Read alone it
    would call every slot root, so this pins that the cheap field is required too.
    """
    worker = _slot("worker", created_by="root")
    _with_tree(monkeypatch, {})
    assert is_root_session(worker) is False


def test_an_empty_tree_leaves_an_ordinary_tab_as_root(monkeypatch) -> None:
    """An empty tree is "nothing cites a creator", not "nothing is known"."""
    tab = _slot("tab", created_by="")
    _with_tree(monkeypatch, {})
    assert is_root_session(tab) is True


def test_an_unseeded_tree_is_not_read_as_root_and_asks_for_a_seed(monkeypatch) -> None:
    """Before the first seed an adopted slot's edge is not folded yet, so no card is queued."""
    from kiro_crew.crew_log import session_tree_projection
    from kiro_crew.dashboard import state as dashboard_state

    asks: list[int] = []
    monkeypatch.setattr(dashboard_state, "_request_lineage_seed", lambda: asks.append(1))
    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(nodes=lambda: {}, seeded_for_current_store=False),
    )
    adopted = _slot("adopted", created_by="")
    lifecycle = _lifecycle(adopted, enabled=True)
    assert is_root_session(adopted) is False
    lifecycle.notify(adopted, "done")
    assert "adopted" not in lifecycle.publisher.entries
    assert asks


def test_no_card_is_generated_while_the_crew_log_is_off(monkeypatch) -> None:
    """With no log there is no fold, so a card would be every fact "not recorded"."""
    root = _slot("root", created_by="")
    lifecycle = _lifecycle(root, enabled=True)
    assert lifecycle._eligible(root) is True
    monkeypatch.setattr(card_lifecycle, "crew_log_enabled", lambda: False)
    assert lifecycle._eligible(root) is False
    lifecycle.notify(root, "done")
    assert "root" not in lifecycle.publisher.entries


def test_an_unreadable_tree_falls_back_to_the_cheap_reading(monkeypatch) -> None:
    """A broken projection must cost the wider reading, never the panel or the turn."""
    from kiro_crew.crew_log import session_tree_projection

    def boom():
        raise RuntimeError("tree unavailable")

    monkeypatch.setattr(session_tree_projection, "projection", boom)
    assert is_root_session(_slot("tab", created_by="")) is True
    assert is_root_session(_slot("worker", created_by="root")) is False


def test_the_root_test_does_no_file_io(monkeypatch) -> None:
    """It runs on the gateway serving loop, so a file read here would stall every task.

    Asserted by making ``open`` raise for the duration: the projection's own ``nodes()``
    documents that it never reads a file, and this pins that the predicate around it
    does not either.
    """
    import builtins

    _with_tree(monkeypatch, {"tab": _Node("tab", None)})

    def no_open(*args: object, **kwargs: object):
        raise AssertionError("the root test opened a file")

    monkeypatch.setattr(builtins, "open", no_open)
    assert is_root_session(_slot("tab", created_by="")) is True


# --------------------------------------------------------------------------
# end to end: the generator, the refresher and the worker
# --------------------------------------------------------------------------


def _wire(monkeypatch, reads: dict, replies: list[str]):
    """A root slot and a worker slot, with the fold read and the model both pinned."""
    root = _slot("root")
    worker = _slot("worker", created_by="root")
    lifecycle = _lifecycle(root, worker, enabled=True, log=_Log(root))
    lifecycle.publisher.budget = CardBudget(debounce=0, per_session=0)
    monkeypatch.setattr(card_lifecycle, "_read_card_folds", lambda key: reads["now"])
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(dynamic_dashboard_cards=True, language=""),
        agent=SimpleNamespace(resolve_model=lambda role: "m"),
    )
    monkeypatch.setattr(card_lifecycle.KiroCrewConfig, "load", lambda: cfg)
    prompts: list[str] = []

    async def generate(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(card_lifecycle, "run_bg_oneliner", generate)
    return lifecycle, root, worker, prompts


async def _settle(lifecycle) -> None:
    for task in (lifecycle.worker, lifecycle._derived_worker):
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_the_root_card_shows_the_fold_values_and_a_worker_gets_none(monkeypatch) -> None:
    reads = {"now": _reads()}
    lifecycle, root, worker, prompts = _wire(monkeypatch, reads, [_reply(lede="Reviewing.")])
    lifecycle.notify(root, "done")
    lifecycle.notify(worker, "done")
    await _settle(lifecycle)
    served = await lifecycle.read(root)
    assert served["status"] == "published"
    expected = build_crew_main(reads["now"])
    for field in DERIVED_FIELDS:
        assert served["card"]["data"][field] == expected[field]  # type: ignore[literal-required]
    # The model saw the numbers only as facts, and saw them exactly.
    context = json.loads(prompts[0][len(_ROOT_PROMPT) :])
    assert context["facts"] == dict(expected)
    assert (await lifecycle.read(worker))["card"] is None
    assert worker.key not in lifecycle.publisher.entries
    assert len(prompts) == 1


@pytest.mark.asyncio
async def test_a_refused_card_is_never_published(monkeypatch) -> None:
    reads = {"now": _reads()}
    lifecycle, root, _, _ = _wire(monkeypatch, reads, [_reply(lede="Seven done.", you="3")])
    lifecycle.notify(root, "done")
    await _settle(lifecycle)
    served = await lifecycle.read(root)
    assert served["card"] is None
    assert served["status"] == "failed"


@pytest.mark.asyncio
async def test_numbers_follow_the_log_between_generations_with_no_model_call(monkeypatch) -> None:
    reads = {"now": _reads()}
    lifecycle, root, _, prompts = _wire(monkeypatch, reads, [_reply(lede="Reviewing.")])
    lifecycle.notify(root, "done")
    await _settle(lifecycle)
    # Spend the hourly budget: the next event must refresh the numbers on its own.
    lifecycle.publisher.budget = CardBudget(debounce=0, per_session=0, per_hour=1)
    reads["now"] = _reads(work={"items": [{"state": "accepted", "status": "done"}], "omitted": 0})
    lifecycle.notify(root, "progress")
    # Only the refresher settles: the model queue now waits out the hour.
    await asyncio.wait_for(lifecycle._derived_worker, 2)
    card = (await lifecycle.read(root))["card"]
    assert card["data"]["items_done"] == "1 of 1 items accepted"
    assert card["data"]["lede"] == "Reviewing."
    assert len(prompts) == 1
    await lifecycle.shutdown()


@pytest.mark.asyncio
async def test_shutdown_settles_both_tasks(monkeypatch) -> None:
    reads = {"now": _reads()}
    replies = [_reply(lede="Reviewing."), _reply(lede="Again.")]
    lifecycle, root, _, _ = _wire(monkeypatch, reads, replies)
    lifecycle.notify(root, "done")
    await _settle(lifecycle)
    lifecycle.notify(root, "progress")
    await lifecycle.shutdown()
    assert not lifecycle.state._background_tasks


@pytest.fixture
def _real_log(tmp_path, monkeypatch):
    """A crew log store of this test's own, written by the REAL emitter."""
    from kiro_crew.crew_log import emit

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    yield emit
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


def test_the_session_folds_are_read_from_the_unit_the_slot_writes(_real_log) -> None:
    # The unit id is the ACP session id, never the slot's session key. Folding the key
    # would read a unit that does not exist, which is an EMPTY record -- every count
    # zero -- rather than an error, so the card would print zeros for a busy session.
    emit = _real_log
    emit.on_session_opened("unit-a", slot="root", agent="default", memory="global")
    emit.on_turn_started("unit-a", 1)
    emit.on_turn_completed("unit-a", 1, credits=0.5, stop_reason="end_turn")
    assert emit.flush(10.0)
    reads = card_lifecycle._read_card_folds("root")
    assert reads["status"]["slot"] == "root"
    assert reads["status"]["turns_completed"] == 1
    data = build_crew_main(reads)
    assert data["turns"].startswith("1 ")
    assert data["state"] != NOT_RECORDED


def test_a_slot_with_no_unit_reads_not_recorded_rather_than_zero(_real_log) -> None:
    emit = _real_log
    emit.on_session_opened("unit-b", slot="other", agent="default", memory="global")
    assert emit.flush(10.0)
    data = build_crew_main(card_lifecycle._read_card_folds("root"))
    for field in ("state", "turns", "entries", "credits", "tokens", "approvals_open"):
        assert data[field] == NOT_RECORDED, field


def test_units_the_store_cannot_rank_read_as_unreadable(monkeypatch) -> None:
    from kiro_crew.crew_log import emit

    monkeypatch.setattr(emit, "slot_previous_store", lambda slot: ("", False, False))
    reads = card_lifecycle._read_card_folds("root")
    assert reads["status"] == FOLD_UNREADABLE
    assert reads["usage"] == FOLD_UNREADABLE
    assert reads["approvals"] == FOLD_UNREADABLE
    assert build_crew_main(reads)["turns"] == UNREADABLE


@pytest.mark.asyncio
async def test_the_model_is_told_which_tools_the_log_recorded(_real_log, monkeypatch) -> None:
    # The note once said no stop call was made while the log held one: transcript rows
    # the model reads carry no tool calls. The tool names must come from the fold.
    emit = _real_log
    emit.on_session_opened(
        "unit-t", slot="root", agent="default", memory="global", previous_undecided=False
    )
    emit.on_turn_started("unit-t", 1)
    emit.on_tool_called("unit-t", 1, name="monitor_start", call_id="c1")
    emit.on_tool_completed("unit-t", 1, name="monitor_start", call_id="c1", status="ok")
    emit.on_tool_called("unit-t", 1, name="autonudge_stop", call_id="c2")
    emit.on_tool_completed("unit-t", 1, name="autonudge_stop", call_id="c2", status="ok")
    emit.on_turn_completed("unit-t", 1, stop_reason="end_turn")
    assert emit.flush(10.0)
    reads = {"now": card_lifecycle._read_card_folds("root")}
    # The real log re-roots the session tree; the root gate has its own cases.
    monkeypatch.setattr(card_lifecycle, "is_root_session", lambda slot: True)
    lifecycle, root, _, prompts = _wire(monkeypatch, reads, [_reply(lede="Reviewing.")])
    lifecycle.notify(root, "done")
    await _settle(lifecycle)
    context = json.loads(prompts[0][len(_ROOT_PROMPT) :])
    assert set(context["tools_called"]) == {"monitor_start", "autonudge_stop"}
    assert context["tools_omitted"] == 0
    assert context["tools_whole"] is True
    assert not any("autonudge_stop" in row["text"] for row in context["recent_messages"])
    assert 'Never say a tool was or was not called unless\n"tools_called" says so' in _ROOT_PROMPT
    assert 'unless\n"tools_whole" is true' in _ROOT_PROMPT


def test_a_tool_called_in_an_earlier_unit_of_the_slot_is_listed(_real_log) -> None:
    # A slot that resumed into a successor unit: the stop call is in the OLDER unit,
    # while recent_messages can still show that unit's turns. Folding the newest unit
    # alone gives a complete-looking list with no stop call in it.
    emit = _real_log
    emit.on_session_opened(
        "unit-old", slot="root", agent="default", memory="global", previous_undecided=False
    )
    emit.on_turn_started("unit-old", 1)
    emit.on_tool_called("unit-old", 1, name="autonudge_stop", call_id="c1")
    emit.on_tool_completed("unit-old", 1, name="autonudge_stop", call_id="c1", status="ok")
    emit.on_turn_completed("unit-old", 1, stop_reason="end_turn")
    assert emit.flush(10.0)
    emit.on_session_opened(
        "unit-new", slot="root", agent="default", memory="global", previous_sid="unit-old"
    )
    emit.on_turn_started("unit-new", 1)
    emit.on_tool_called("unit-new", 1, name="monitor_start", call_id="c2")
    emit.on_tool_completed("unit-new", 1, name="monitor_start", call_id="c2", status="ok")
    emit.on_turn_completed("unit-new", 1, stop_reason="end_turn")
    assert emit.flush(10.0)
    reads = card_lifecycle._read_card_folds("root")
    # The card's own numbers still come from the unit the slot writes now.
    assert reads["status"]["previous"] == "unit-old"
    record = card_lifecycle._tool_record(card_lifecycle._read_card_tools("root", reads))
    assert record["tools_called"] == ["monitor_start", "autonudge_stop"]
    assert record["tools_omitted"] == 0
    assert record["tools_whole"] is True


@pytest.mark.parametrize(
    ("ended", "incomplete"),
    [
        ("missing", False),  # retention took the cited unit
        ("foreign", False),  # the cited unit is another slot's
        ("cycle", False),
        ("cap", False),
        ("unknown", False),  # the newest unit answered no record: fold it alone
        ("first", True),  # the scan that walked it did not see the whole store
    ],
)
def test_a_chain_cut_short_never_licenses_a_never_called_claim(
    _real_log, monkeypatch, ended: str, incomplete: bool
) -> None:
    from kiro_crew.crew_log import read
    from kiro_crew.crew_log.session_tree import ChainReading, SlotChain

    emit = _real_log
    emit.on_session_opened("unit-new", slot="root", agent="default", memory="global")
    emit.on_turn_started("unit-new", 1)
    emit.on_tool_called("unit-new", 1, name="monitor_start", call_id="c1")
    emit.on_turn_completed("unit-new", 1, stop_reason="end_turn")
    assert emit.flush(10.0)
    sids = () if ended == "unknown" else ("unit-new",)
    chain = ChainReading(SlotChain("root", sids, ended), incomplete=incomplete)
    monkeypatch.setattr(read, "slot_chain", lambda unit: chain)
    tools = card_lifecycle._read_card_tools("root", {"unit": "unit-new"})
    assert tools["units_whole"] is False
    record = card_lifecycle._tool_record(tools)
    assert record == {"tools_called": ["monitor_start"], "tools_omitted": 0, "tools_whole": False}


def test_a_slot_with_no_unit_has_a_whole_empty_list_and_an_unranked_one_has_none(
    monkeypatch,
) -> None:
    from kiro_crew.crew_log import projection as projections
    from kiro_crew.crew_log import read
    from kiro_crew.crew_log.session_tree import ChainReading, SlotChain

    no_unit = card_lifecycle._read_card_tools("root", {"no_unit": True})
    assert card_lifecycle._tool_record(no_unit)["tools_whole"] is True
    unranked = card_lifecycle._read_card_tools("root", {})
    assert unranked == FOLD_UNREADABLE

    chain = ChainReading(SlotChain("root", ("new", "old"), "first"), incomplete=False)
    monkeypatch.setattr(read, "slot_chain", lambda unit: chain)

    def broken(name, units, *, slot):
        raise OSError("unreadable")

    monkeypatch.setattr(projections, "fold_slot_warm", broken)
    assert card_lifecycle._read_card_tools("root", {"unit": "new"}) == FOLD_UNREADABLE
    monkeypatch.setattr(projections, "fold_slot_warm", lambda name, units, *, slot: None)
    monkeypatch.setattr(
        projections, "projection_of", lambda checkpoint: SimpleNamespace(value=None)
    )
    assert card_lifecycle._read_card_tools("root", {"unit": "new"}) == FOLD_UNREADABLE


def test_a_unit_off_the_chain_is_not_listed_as_this_conversations(_real_log) -> None:
    # Every unit whose header names the slot is not the conversation the card shows:
    # an orphan unit (no edge to or from the chain) is another conversation, and its
    # stop call must not be reported as this one's. The chain decides the units.
    emit = _real_log
    emit.on_session_opened(
        "unit-orphan", slot="root", agent="default", memory="global", previous_undecided=False
    )
    emit.on_turn_started("unit-orphan", 1)
    emit.on_tool_called("unit-orphan", 1, name="autonudge_stop", call_id="c0")
    emit.on_turn_completed("unit-orphan", 1, stop_reason="end_turn")
    assert emit.flush(10.0)
    emit.on_session_opened(
        "unit-a", slot="root", agent="default", memory="global", previous_undecided=False
    )
    assert emit.flush(10.0)
    emit.on_session_opened(
        "unit-b", slot="root", agent="default", memory="global", previous_sid="unit-a"
    )
    emit.on_turn_started("unit-b", 1)
    emit.on_tool_called("unit-b", 1, name="monitor_start", call_id="c1")
    emit.on_turn_completed("unit-b", 1, stop_reason="end_turn")
    assert emit.flush(10.0)
    reads = card_lifecycle._read_card_folds("root")
    assert reads["unit"] == "unit-b"
    record = card_lifecycle._tool_record(card_lifecycle._read_card_tools("root", reads))
    assert record["tools_called"] == ["monitor_start"]
    assert record["tools_whole"] is True


@pytest.mark.parametrize("flags", [{"previous_undecided": True}, {}])
def test_an_oldest_unit_that_does_not_state_it_is_first_is_a_gap(_real_log, flags) -> None:
    # A cold-started successor whose writer could not name its predecessor, or a log
    # from before the edge keys, cites nothing -- but that is not a statement that the
    # conversation began there, so the list may miss the unit before it.
    emit = _real_log
    emit.on_session_opened("unit-g", slot="root", agent="default", memory="global", **flags)
    emit.on_turn_started("unit-g", 1)
    emit.on_tool_called("unit-g", 1, name="monitor_start", call_id="c1")
    emit.on_turn_completed("unit-g", 1, stop_reason="end_turn")
    assert emit.flush(10.0)
    reads = card_lifecycle._read_card_folds("root")
    assert reads["unit"] == "unit-g"
    record = card_lifecycle._tool_record(card_lifecycle._read_card_tools("root", reads))
    assert record["tools_called"] == ["monitor_start"]
    assert record["tools_whole"] is False


def test_the_refresh_path_never_reads_the_tool_chain(_real_log, monkeypatch) -> None:
    # Numbers are re-bound on every log growth with no model call; tool names are
    # for the prompt only, so the chain walk must stay off that path.
    from kiro_crew.crew_log import read

    emit = _real_log
    emit.on_session_opened("unit-r", slot="root", agent="default", memory="global")
    assert emit.flush(10.0)

    def forbidden(unit):
        raise AssertionError("the card fold read walked the slot chain")

    monkeypatch.setattr(read, "slot_chain", forbidden)
    reads = card_lifecycle._read_card_folds("root")
    assert reads["unit"] == "unit-r"
    assert "tools" not in reads


def test_an_unreadable_or_cut_tool_list_never_reads_as_the_whole_one(monkeypatch) -> None:
    assert card_lifecycle._tool_record(FOLD_UNREADABLE) == {
        "tools_called": None,
        "tools_omitted": 0,
        "tools_whole": False,
    }
    rows = {f"tool_{i:03d}": {"calls": 1, "last_time": i} for i in range(300)}
    record = card_lifecycle._tool_record({"by_name": rows, "names_omitted": 4})
    assert record["tools_called"][0] == "tool_299"  # newest first
    assert len(json.dumps(record["tools_called"])) <= card_lifecycle._TOOL_NAMES_CHARS
    shown = len(record["tools_called"])
    assert 0 < shown < 300
    assert record["tools_omitted"] == 300 - shown + 4
    assert record["tools_whole"] is False
    # A name the fold holds with no call (a completion with no call) was not called.
    only = card_lifecycle._tool_record({"by_name": {"x": {"calls": 0, "completed": 1}}})
    assert only == {"tools_called": [], "tools_omitted": 0, "tools_whole": False}
    whole = card_lifecycle._tool_record({"by_name": {}, "units_whole": True})
    assert whole == {"tools_called": [], "tools_omitted": 0, "tools_whole": True}


@pytest.mark.asyncio
async def test_a_log_commit_with_no_transcript_row_re_binds_the_numbers(monkeypatch) -> None:
    # The turn's last entries land AFTER the row that ended it, so nothing but the
    # writer's growth signal says the folds moved. A card bound at that row would
    # otherwise keep saying a turn is running.
    from kiro_crew.crew_log import projection

    reads = {"now": _reads()}
    lifecycle, root, _, prompts = _wire(monkeypatch, reads, [_reply(lede="Reviewing.")])
    lifecycle.state.serving_loop = asyncio.get_running_loop()
    monkeypatch.setattr(projection, "slot_of_session", lambda unit: "root" if unit == "u" else "")
    lifecycle.notify(root, "done")
    await _settle(lifecycle)
    reads["now"] = _reads(work={"items": [{"state": "accepted", "status": "done"}], "omitted": 0})
    await asyncio.to_thread(lifecycle.log_grew, "other")
    await asyncio.to_thread(lifecycle.log_grew, "u")
    for _ in range(50):
        await asyncio.sleep(0.01)
        card = (await lifecycle.read(root))["card"]
        if card["data"]["items_done"] == "1 of 1 items accepted":
            break
    assert card["data"]["items_done"] == "1 of 1 items accepted"
    assert card["data"]["lede"] == "Reviewing."
    assert len(prompts) == 1
    await lifecycle.shutdown()


@pytest.mark.asyncio
async def test_a_workers_report_re_binds_the_conductors_numbers(monkeypatch) -> None:
    # A worker's report lands in the WORKER's log, whose header names the worker's slot,
    # which has no card. The conductor's board folds that log, so it must re-bind.
    from kiro_crew.crew_log import projection

    bound = {"state": "open", "status": "progress", "worker_session_key": "worker"}
    reads = {"now": _reads(work={"items": [dict(bound, status="question")], "omitted": 0})}
    lifecycle, root, _, prompts = _wire(monkeypatch, reads, [_reply(lede="Reviewing.")])
    lifecycle.state.serving_loop = asyncio.get_running_loop()
    monkeypatch.setattr(projection, "slot_of_session", lambda unit: "worker" if unit == "w" else "")
    lifecycle.notify(root, "done")
    await _settle(lifecycle)
    reads["now"] = _reads(work={"items": [bound], "omitted": 0})
    await asyncio.to_thread(lifecycle.log_grew, "w")
    for _ in range(50):
        await asyncio.sleep(0.01)
        card = (await lifecycle.read(root))["card"]
        if card["data"]["items_progress"].startswith("1 "):
            break
    assert card["data"]["items_progress"] == "1 of 1 open items reporting progress"
    assert len(prompts) == 1
    await lifecycle.shutdown()


@pytest.mark.parametrize(
    "html",
    [
        LAYOUT.replace("<style>", '<style data-dashboard-field="credits">').replace(
            '<span data-dashboard-field="credits"></span>', "<span></span>"
        ),
        LAYOUT.replace(
            '<span data-dashboard-field="credits"></span>',
            '<script data-dashboard-field="credits"></script>',
        ),
    ],
    ids=["on-style", "on-stripped-element"],
)
def test_a_binding_the_binder_never_fills_does_not_count(html: str) -> None:
    # The binder fills body elements it keeps. A fact bound anywhere else is a blank cell.
    reply = json.dumps({"html": html, "data": {"lede": "Working.", "you": "", "notes": ""}})
    assert _root_card_output(reply, None, build_crew_main(_reads())) is None


def test_the_growth_signal_reaches_only_the_current_producer(monkeypatch) -> None:
    from kiro_crew.crew_log import emit

    registered: list[object] = []
    monkeypatch.setattr(card_lifecycle, "crew_log_enabled", lambda: True)
    monkeypatch.setattr(card_lifecycle, "_growth_registered", False)
    monkeypatch.setattr(card_lifecycle, "_growth_target", None)
    monkeypatch.setattr(emit, "add_growth_listener", registered.append)
    first = _lifecycle(_slot("root"))
    second = _lifecycle(_slot("root"))
    assert registered == [card_lifecycle._on_log_growth]
    seen: list[str] = []
    monkeypatch.setattr(first, "log_grew", lambda unit: seen.append("first"))
    monkeypatch.setattr(second, "log_grew", lambda unit: seen.append("second"))
    card_lifecycle._on_log_growth("u")
    assert seen == ["second"]


@pytest.mark.parametrize(
    "html",
    [
        '<p data-dashboard-field="lede"></p><p data-dashboard-field="you"></p>'
        '<p data-dashboard-field="notes"></p>',
        LAYOUT.replace('<span data-dashboard-field="credits"></span>', "<span></span>"),
    ],
    ids=["sentences-only", "one-fact-left-out"],
)
def test_a_layout_that_leaves_a_fact_out_is_refused(html: str) -> None:
    # The model owns the layout, so without this a card could drop every number the
    # folds produced and still publish.
    reply = json.dumps({"html": html, "data": {"lede": "Working.", "you": "", "notes": ""}})
    assert _root_card_output(reply, None, build_crew_main(_reads())) is None
    assert _root_card_output(_reply(lede="Working."), None, build_crew_main(_reads())) is not None


def test_a_new_layout_that_drops_a_fact_keeps_the_last_layout_and_takes_the_sentences() -> None:
    first = _root_card_output(_reply(lede="Starting."), None, build_crew_main(_reads()))
    assert first is not None
    thin = '<p data-dashboard-field="lede"></p><p data-dashboard-field="you"></p>'
    reply = json.dumps(
        {"html": thin, "data": {"lede": "Waiting on a date.", "you": "", "notes": ""}}
    )
    second = _root_card_output(reply, first, build_crew_main(_reads()))
    assert second is not None
    assert second["html"] == first["html"]
    assert second["data"]["lede"] == "Waiting on a date."


def test_an_empty_layout_keeps_the_one_on_screen() -> None:
    # Models answer "keep the layout" with "html": "" as often as by omitting the key.
    first = _root_card_output(_reply(lede="Starting."), None, build_crew_main(_reads()))
    assert first is not None
    reply = json.dumps({"html": " ", "data": {"lede": "Waiting.", "you": "", "notes": ""}})
    second = _root_card_output(reply, first, build_crew_main(_reads()))
    assert second is not None
    assert second["html"] == first["html"]
    assert second["data"]["lede"] == "Waiting."
    # With no layout on screen there is nothing to keep.
    assert _root_card_output(reply, None, build_crew_main(_reads())) is None
