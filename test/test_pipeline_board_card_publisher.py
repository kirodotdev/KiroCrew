"""The board's card is published WITHOUT a model call, and only for the right session.

Two halves of one claim, and the first is the load-bearing one. Every other dynamic
dashboard card is written by a model reading a transcript: it costs a call, spends an
attempt from one hourly budget shared across the whole gateway, and is therefore gated on
the owner's opt-in to that cost. The conductor's board is assembled from a fold the product
already keeps, so it costs nothing -- and a test that only checked the card APPEARS would
pass equally over an implementation that quietly asked a model to write it, which would put
the one free card on the machine behind a paid switch and let a model restate numbers the
log already answers for.

So the generator is replaced by one that RAISES. Anything reaching it fails loudly rather
than returning a plausible card.

The second half is who gets it. The derived path deliberately drops the ``_created_by``
exclusion the model path applies -- that exclusion exists to stop a fan-out of workers
spending the shared budget, and a derived card spends none, while a conductor dispatched by
another session is exactly the case that must still get its board. Privacy exclusions stay,
because those are not about cost.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard import card_lifecycle
from kiro_crew.dashboard import state as state_module
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.dynamic_cards import CardBudget
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.pipeline_board_contract import (
    BOARD_TEMPLATE_ID,
    build_pipeline_board,
    card_template_path,
    panel_card_data,
    validate_judgment,
)


class _NoModel:
    """A generator that must never run. Called, it fails the test that called it."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, entry: Any) -> dict | None:
        self.calls += 1
        raise AssertionError(
            "the derived board card asked a model to write it; the board is assembled "
            "from the work fold and must cost no call"
        )


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, _NoModel]:
    class Log:
        @contextmanager
        def publication_hold(self, key: str) -> Any:
            yield

        def session_mtime(self, key: str) -> int:
            return 1

        def rotation_generation(self, key: str) -> int:
            return 0

        def chained_keys(self, key: str) -> list[str]:
            return [key]

        def derive_recent(self, key: str, max_messages: int) -> list[dict]:
            return []

    slot = SimpleNamespace(
        key="chat-1875",
        _dashboard_card_identity="owner-conductor",
        memory_mode="persistent",
        messages=[{"role": "user", "content": "run the board"}],
    )
    state = SimpleNamespace(
        _slots={slot.key: slot},
        conversation_log=Log(),
        flush_slot_now=lambda slot: None,
        sessions=object(),
        _background_tasks=set(),
        broadcast_ws_owners=lambda *args: None,
        _dynamic_cards=None,
    )
    monkeypatch.setattr(
        card_lifecycle.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(resolve_model=lambda role: "auto"),
            dashboard=SimpleNamespace(dynamic_dashboard_cards=True),
        ),
    )
    # ``enabled=False``: the cost opt-in is OFF, which is the state this path must work in.
    lifecycle = card_lifecycle.CardLifecycle(state, enabled=False)
    no_model = _NoModel()
    lifecycle.publisher.generate = no_model
    state._dynamic_cards = lifecycle
    return lifecycle, slot, state, no_model


#: The ownership digest every helper call below claims. ``_publish_derived_card`` refuses a
#: record whose ``crew_key`` is not this, which is the publish route's half of the collision
#: refusal -- so a test record has to carry it to reach the store at all.
OWNER = "owner-digest"


def _owned(**extra: Any) -> dict[str, Any]:
    """A record this slot's crew owns, plus *extra*."""
    return {"crew_key": OWNER, **extra}


def _card() -> dict[str, Any]:
    view = {
        "conductor": {
            "schema": 1,
            "slot_key": "chat-1875",
            "goal": "the board becomes a card",
            "round": 3,
            "goal_version": 1,
            "depth": 0,
            "parent_item": None,
            "created_at": "2026-09-29T13:00:00+00:00",
            "entries": 41,
            "first_entry_at": "2026-09-29T13:00:00+00:00",
            "last_entry_at": "2026-09-29T15:30:00+00:00",
            "generation": "gen-1",
        },
        "items": [
            {
                "schema": 1,
                "item_id": "it_0",
                "title": "t",
                "acceptance": {},
                "state": "open",
                "verdict": None,
                "decision": "",
                "worker_session_key": "chat-2176",
                "round": 3,
                "fails": 0,
                "status": None,
                "summary": "",
                "artifacts": {},
                "pr": 14583,
                "last_report_at": "2026-09-29T15:00:00+00:00",
                "created_at": "2026-09-29T14:00:00+00:00",
                "closed_at": None,
                "events": [],
            }
        ],
        "omitted": 0,
    }
    panel = build_pipeline_board(
        view,  # type: ignore[arg-type]
        validate_judgment({"lede": "One item is open."}),
        name="KiroCrew Pipeline Conductor",  # brand-ok: the crew's own display name
        captured_at="2026-09-29T15:37:00Z",
        stale_after_seconds=900,
        now_epoch=1790696200.0,
    )
    return {
        "html": card_template_path().read_text(encoding="utf-8"),
        "data": panel_card_data(panel),
    }


# ---------------------------------------------------------------------------
# no model call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_board_card_is_published_and_read_with_no_model_call(service: Any) -> None:
    lifecycle, slot, _state, no_model = service
    assert lifecycle.publish_derived(slot, _card()) is True
    read = await lifecycle.read(slot)
    assert read["status"] == "published"
    assert read["card"]["data"]["lede"] == "One item is open."
    assert no_model.calls == 0
    # And nothing was queued: a derived card does not enter the generator's state machine,
    # so it cannot hold the sole permit or spend an attempt from the shared hourly budget.
    assert slot.key not in lifecycle.publisher.entries
    assert not lifecycle.publisher.attempts
    assert lifecycle.publisher.next_delay() is None


@pytest.mark.asyncio
async def test_the_generator_would_be_caught_if_it_ran(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control. The assertion above is only evidence because the stub CAN fail the test.

    Without this, a stub that was never wired in would make ``calls == 0`` true for the
    wrong reason and the no-model claim would rest on nothing.
    """
    from kiro_crew.crew_log import session_tree_projection

    lifecycle, slot, _state, no_model = service
    lifecycle.enabled = True
    lifecycle.publisher.budget = CardBudget(debounce=0)
    # The MODEL path is reached only for an eligible slot: ``_eligible`` requires the crew
    # log on and a ROOT session (``is_root_session``, which reads the seeded session tree).
    # A slot the gate refuses would make ``calls == 0`` true for the wrong reason -- exactly
    # the vacuous control this case exists to rule out -- so make the slot genuinely eligible.
    monkeypatch.setattr(card_lifecycle, "crew_log_enabled", lambda: True)
    session_tree_projection.reset_for_tests()
    session_tree_projection.projection().ensure_seeded()
    try:
        # THE REAL BINDING, from the same helper ``_valid`` compares against. A made-up one is
        # refused before the generator is reached, which makes ``calls == 0`` true for the
        # wrong reason -- exactly the vacuous control this case exists to rule out.
        lifecycle.publisher.notify(
            slot.key, slot._dashboard_card_identity, slot_history_key(slot), "turn"
        )
        await lifecycle.publisher.run_ready()
        assert no_model.calls == 1, "the stub is not the generator being exercised"
    finally:
        session_tree_projection.reset_for_tests()


@pytest.mark.asyncio
async def test_the_cost_opt_in_does_not_hide_the_derived_card(service: Any) -> None:
    """A derived card is free, so the owner's opt-in to the COST of model-written cards
    does not decide whether it is shown -- and turning that cost off must not delete the
    one card on the machine that has none."""
    lifecycle, slot, _state, _no_model = service
    assert lifecycle.enabled is False
    lifecycle.publish_derived(slot, _card())
    assert (await lifecycle.read(slot))["status"] == "published"
    lifecycle.set_enabled(True)
    assert (await lifecycle.read(slot))["status"] == "published"
    lifecycle.set_enabled(False)
    assert (await lifecycle.read(slot))["status"] == "published"


# ---------------------------------------------------------------------------
# who gets it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dispatched_conductor_still_gets_its_board(service: Any) -> None:
    """``_created_by`` excludes a session another session created from the MODEL path,
    because a fan-out of workers would spend the shared hourly budget. A derived card
    spends none, and a conductor dispatched by another session is exactly the case whose
    board a person needs."""
    lifecycle, slot, _state, _no_model = service
    slot._created_by = "chat-1000"
    assert lifecycle._eligible(slot) is False, "the model path still excludes it"
    assert lifecycle.publish_derived(slot, _card()) is True
    assert (await lifecycle.read(slot))["status"] == "published"


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("memory_mode", "incognito"), ("executor", "remote")],
)
def test_privacy_and_remoteness_still_withhold_the_card(
    service: Any, attribute: str, value: Any
) -> None:
    """The exclusions that are NOT about cost stay. An incognito transcript is withheld
    from every derived surface too, and a remote slot's content is not this gateway's to
    publish."""
    lifecycle, slot, _state, _no_model = service
    setattr(slot, attribute, value)
    assert lifecycle.publish_derived(slot, _card()) is False


def test_a_scratch_copy_cannot_publish_over_the_live_slot(service: Any) -> None:
    """A scratch copy shares the live identity and its edits are not committed."""
    lifecycle, slot, _state, _no_model = service
    scratch = SimpleNamespace(**vars(slot))
    assert lifecycle.publish_derived(scratch, _card()) is False


@pytest.mark.asyncio
async def test_a_replacement_session_never_presents_the_retired_crews_board(
    service: Any,
) -> None:
    """The one wrong thing a cached panel can do.

    The board was built for the closed transcript, so it goes with it rather than being
    shown by whatever takes the slot next -- which would attribute one crew's work to
    another with nothing on the card saying so.
    """
    lifecycle, slot, state, _no_model = service
    lifecycle.publish_derived(slot, _card())
    replacement = SimpleNamespace(**vars(slot))
    replacement._dashboard_card_identity = "owner-someone-else"
    state._slots[slot.key] = replacement
    assert (await lifecycle.read(replacement))["card"] is None
    assert slot.key not in lifecycle.derived, "the retired card is dropped, not merely hidden"


def test_the_host_normalizer_still_refuses_a_bad_derived_card(service: Any) -> None:
    """A derived producer is still a producer: its output is refused, not trusted.

    And a refused card leaves the previous one in place -- a board that briefly cannot be
    built is not a board that changed.
    """
    lifecycle, slot, _state, _no_model = service
    assert lifecycle.publish_derived(slot, _card()) is True
    held = dict(lifecycle.derived[slot.key])
    assert lifecycle.publish_derived(slot, {"html": "", "data": {}}) is False
    assert lifecycle.publish_derived(slot, {"html": "<p></p>", "data": {"bad name": "x"}}) is False
    assert lifecycle.derived[slot.key] == held


# ---------------------------------------------------------------------------
# the route hands it over, and never fails the drawer read doing so
# ---------------------------------------------------------------------------


def test_the_route_hands_the_records_card_to_the_store(service: Any) -> None:
    lifecycle, slot, state, no_model = service
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)
    data = lifecycle.derived[slot.key]["card"]["data"]
    # The board's revision IS the conductor's round, so the card carries it once, as the tile.
    assert data["stat_round"] == "3", data["stat_round"]
    assert "meta_revision" not in data, sorted(data)
    assert no_model.calls == 0


@pytest.mark.parametrize(
    ("name", "record"),
    [
        ("no record", None),
        ("no card on the record", {"template": "default"}),
        ("card is not an object", {"card": "nope"}),
    ],
)
def test_a_record_with_no_card_stores_nothing(service: Any, name: str, record: Any) -> None:
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state, slot.key, None if record is None else {**record, "crew_key": OWNER}, OWNER
    )
    assert slot.key not in lifecycle.derived, name


def test_the_route_never_turns_a_store_failure_into_a_failed_drawer_read(service: Any) -> None:
    """The panel response is what this route owes its caller; the card is a side effect of
    having built the board anyway. So a store that raises is logged, not propagated."""
    _lifecycle, slot, state, _no_model = service

    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("the store is unhappy")

    state._dynamic_cards.publish_derived = boom
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)  # must not raise


def test_nothing_is_stored_when_the_slot_is_not_running(service: Any) -> None:
    """A card belongs to a live slot; with none there is nothing to attach it to."""
    lifecycle, slot, state, _no_model = service
    state._slots.clear()
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)  # must not raise
    assert slot.key not in lifecycle.derived


def test_a_late_retirement_on_a_closed_slot_leaves_no_orphan_stamp(service: Any) -> None:
    """A read finishing after ``close_slot`` must not resurrect a retirement for a gone slot.

    ``_publish_derived_card`` runs a worker hop after the read that produced its record, and
    ``push_slot_removed`` can clear the slot in that gap -- it already evicts the derived card
    AND drops the retirement stamp as the one place that knows the removal is definitive. If a
    retirement (an absent or no-card record) still ran after that close, it would write a
    ``(key, stamp)`` pair into ``_retired``, which ``forget_retired`` only clears on a later
    publish -- and a gone slot has none. Each closed slot losing this race would leave one
    orphan pair behind for the life of the process (``_retired`` is unbounded). The live-slot
    guard refuses before both retirement branches, so the gone slot's key never appears.
    """
    lifecycle, slot, state, _no_model = service
    # The slot is closed between the panel read and this worker-hop-later retirement.
    state._slots.clear()
    assert slot.key not in lifecycle._retired
    # Both retirement shapes: an absent record, and a record whose board is gone -- each is a
    # write that would stamp _retired without the guard.
    routes._publish_derived_card(state, slot.key, None, OWNER, authoritative=True)
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(published_at="2026-09-29T15:30:00+00:00"),  # no `card` key -> the no-board branch
        OWNER,
        authoritative=True,
    )
    assert slot.key not in lifecycle._retired, "a late retirement stamped a gone slot"
    assert slot.key not in lifecycle.derived


def test_a_state_that_cannot_make_a_store_is_survived_not_raised(service: Any) -> None:
    """The panel response is what the route owes its caller, so every way the store can be
    unavailable degrades to storing nothing rather than to a 500.

    An absent store is not one of those ways -- a derived card creates one -- so what this
    exercises is a state object that cannot supply one at all.
    """
    _lifecycle, slot, state, _no_model = service
    bare = SimpleNamespace(**{**vars(state), "_dynamic_cards": None})
    assert not hasattr(bare, "ensure_dynamic_card_store"), "the fixture gained the method"
    routes._publish_derived_card(bare, slot.key, _owned(card=_card()), OWNER)  # must not raise


def test_the_card_page_is_read_once_per_process() -> None:
    """It is shipped package data with no operator override directory, so there is nothing
    a cache could hide -- and this runs on every drawer read."""
    routes._card_page.cache_clear()
    first = routes._card_page()
    assert routes._card_page() is first
    assert re.search(r'data-dashboard-field="lede"', first)


# ---------------------------------------------------------------------------
# the card is not a second way out for a record the route refuses
# ---------------------------------------------------------------------------
#
# ``_published_record`` answers with the stored FILE, unfiltered, whenever the fold holds
# nothing for the asking owner -- and that file is keyed on the SLUG alone. Two crews whose
# names slugify alike share it, so a colliding crew's record and an unowned one both reach
# the route, which refuses to serve either. ``_panel_record`` gates only on the template
# id, so such a record still builds a card out of its ``lede``, its per-item action
# sentences and its crew name.
#
# Publishing that card before the refusal made the ownership guard half-applied in the same
# way the READ half once was: the panel response withheld the text and the owner-only
# dashboard-card route handed it over. These cases pin the ORDER, because the refusal is
# what decides whether the record may be seen at all.


def test_the_publish_call_sits_after_the_ownership_refusal() -> None:
    """Textual, and deliberately: the defect IS the order of two statements, and no
    reachable-request test can distinguish "published after the check" from "published
    before it" once both have run. The controls below keep a moved anchor from reading as
    a pass."""
    source = Path(routes.__file__).read_text(encoding="utf-8")
    refusal = source.index("if record is not None and (not owner_key or owner_key != mine):")
    publish = source.index("_publish_derived_card(request.app[")
    # Controls: both anchors exist exactly once, so neither a rename nor a second call
    # site can satisfy this by making an index unfindable.
    assert source.count("if record is not None and (not owner_key or owner_key != mine):") == 1
    assert source.count("_publish_derived_card(request.app[") == 1
    assert refusal < publish, (
        "the derived card is published BEFORE the ownership refusal, so a record this "
        "route refuses to serve still reaches the card store"
    )


def test_the_publish_call_sits_before_the_render_branches() -> None:
    """Control for the pin above, in the other direction.

    Moving the call to the very end of the handler would also satisfy it, and would mean a
    crew whose drawer template is broken loses its board -- which the card does not depend
    on, being built from the fold and the record.
    """
    source = Path(routes.__file__).read_text(encoding="utf-8")
    publish = source.index("_publish_derived_card(request.app[")
    assert source.count('"code": "panel_render_failed"') == 1
    assert publish < source.index('"code": "panel_render_failed"')


def test_only_the_publish_route_claims_to_be_authoritative() -> None:
    """The tie-break is only as good as which side claims it, and wiring the flag backwards
    would leave every ordering test green while inverting the outcome: the panel read's
    snapshot would outrank the record the publish just wrote.

    Textual for the same reason as the two pins above -- the claim is about which call site
    carries the argument, and both call sites store a card either way.
    """
    source = Path(routes.__file__).read_text(encoding="utf-8")
    publish_site = "state, card_slot, card_record, published_owner, authoritative=True"
    read_site = '_publish_derived_card(request.app["state"], slot_key, record, mine)'
    assert source.count(publish_site) == 1, "the publish route does not claim authority"
    assert source.count(read_site) == 1, "the panel read's call site moved or gained the flag"
    # Exactly one claim in the module, so a second authoritative caller cannot appear unnoticed.
    assert source.count("authoritative=True") == 1


def test_the_store_is_told_which_writer_is_calling(service: Any) -> None:
    """Reachable counterpart to the textual pin: the flag has to survive the helper rather
    than be dropped on the way through it."""
    _lifecycle, slot, state, _no_model = service
    seen: list[Any] = []

    class Store:
        derived: dict[str, Any] = {}

        def publish_derived(
            self,
            s: Any,
            payload: Any,
            revision: str = "",
            authoritative: bool = False,
        ) -> bool:
            seen.append(authoritative)
            return True

        def forget_derived(self, key: str) -> None:
            pass

    state._dynamic_cards = Store()
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER, authoritative=True)
    assert seen == [False, True]


@pytest.mark.parametrize(
    ("name", "record"),
    [
        # A colliding crew's record: ``crew_key`` is another crew's digest.
        ("another crew's record", {"crew_key": "someone-else", "crew": "Other Crew"}),
        # An unowned record: only a forgery writes one, and it must not render.
        ("an unowned record", {"crew_key": "", "crew": ""}),
    ],
)
def test_a_refused_record_never_reaches_the_card_store(
    service: Any, name: str, record: dict[str, Any]
) -> None:
    """The helper refuses a record whose crew is not this slot's, on its own.

    It cannot rely on a caller's refusal, because one of its two callers has none. The panel
    READ route refuses a foreign record by ordering, before the store is reached. The PUBLISH
    route has no such refusal: it is the writer. And ``_published_record`` answers with the
    slug-keyed file unfiltered when the fold holds nothing under the reading digest, where one
    slug can carry two crews -- so without a refusal here, a colliding crew's board reaches the
    live slot and is then served by the owner-only card route, leaking exactly the lede and
    action sentences the read route withholds.

    The card carries the record's OWN judgment text, which is why storing one built from a
    foreign record IS the disclosure rather than a step towards it.
    """
    lifecycle, slot, state, _no_model = service
    card = _card()
    card["data"]["lede"] = "planted by another crew"
    routes._publish_derived_card(state, slot.key, {**record, "card": card}, OWNER)
    stored = json.dumps(lifecycle.derived.get(slot.key) or {})
    assert "planted by another crew" not in stored, stored


def test_a_fold_that_cannot_be_read_keeps_the_last_good_card(service: Any) -> None:
    """A DERIVATION FAILURE is not an absent board, and the record alone cannot separate them.

    ``_with_board_numbers`` returns the record unchanged both when the fold holds no board and
    when the fold cannot be read at all, which for the drawer is the same answer -- the
    three-state renderer says "not said", true of both. For the CARD they are opposite: absence
    is authoritative and evicts, a failure must not. Conflated, one unreadable fold deleted a
    valid card, broadcast its removal, and did it again on every read until an operator repaired
    the log.
    """
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)
    assert slot.key in lifecycle.derived
    frames: list[Any] = []
    state.broadcast_ws_owners = lambda *args: frames.append(args)
    routes._publish_derived_card(state, slot.key, _owned(board_unreadable=True), OWNER)
    assert slot.key in lifecycle.derived, "an unreadable fold evicted the last good card"
    assert frames == [], "an unreadable fold announced a removal"


def test_the_derivation_failure_is_marked_where_it_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mark has to be set by the code that catches the exception, or the caller cannot
    tell the two cases apart no matter what it checks."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("the log is damaged")

    monkeypatch.setattr(routes.projection, "read_slot_projection", _boom)
    out = routes._with_board_numbers("chat-1875", {"template": BOARD_TEMPLATE_ID, "data": {}})
    assert out.get("board_unreadable") is True
    assert "card" not in out, "a failed derivation must not also claim to have built a card"


def test_the_failure_mark_never_reaches_the_drawer_response() -> None:
    """It is an instruction to the card store, not a field of the panel. Leaked into the
    response it would become a shape the drawer's own clients could start reading."""
    assert "board_unreadable" in routes._PANEL_WITHHELD_KEYS


def _cfg(*members: tuple[str, str]) -> Any:
    """A config whose agents are *members*, each given as ``(crew name, member_id)``.

    ``member_id`` is what ``member_slug`` prefers over the name, so two crews sharing one
    id is the shortest honest spelling of the collision: two distinct names, one slug, and
    therefore one V1 slot key. An empty id falls back to slugifying the name.
    """
    return SimpleNamespace(
        agents={name: SimpleNamespace(member_id=mid, memory_store="") for name, mid in members},
        memory_stores={},
    )


def test_a_slot_two_crews_share_names_neither_of_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is decided on the KEY, before any record is read.

    The fold keeps a record per ownership digest and both panel routes check that digest, so
    a colliding crew's TEXT never reaches its neighbour through them. The card store has no
    digest to check -- it is keyed on the slot, and a slot carries only a per-session
    identity -- so a card stored on a shared key is served by the owner-only card route as
    whichever crew that key currently presents. One crew's state read under another's name.
    """
    cfg = _cfg(("Oncall", "oncall"), ("oncall", "oncall"))
    assert routes._card_slot(cfg, "Oncall", "oncall") == ""
    # BOTH directions: a guard that refused only the second crew would still let the first
    # one publish onto the shared key, which is the whole disclosure.
    assert routes._card_slot(cfg, "oncall", "oncall") == ""


def test_a_shared_key_leaves_a_trace_naming_its_remedy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusing is correct and it is also INVISIBLE without this.

    The operator sees a crew that simply never gets a card. Nothing names the cause, and the
    remedy lives in a docstring no one has a reason to open. Logged ONCE per slug, because a
    panel read happens on every drawer open and a warning per read is a log nobody reads.
    """
    routes._SHARED_KEY_LOGGED.clear()
    cfg = _cfg(("Oncall", "oncall"), ("oncall", "oncall"))
    with caplog.at_level("WARNING"):
        assert routes._card_slot(cfg, "Oncall", "oncall") == ""
    said = [r.getMessage() for r in caplog.records if "board card" in r.getMessage()]
    assert len(said) == 1, said
    assert "oncall" in said[0] and "Rename" in said[0], said[0]
    # ONCE. A second refusal for the same slug says nothing new.
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert routes._card_slot(cfg, "oncall", "oncall") == ""
    assert [r for r in caplog.records if "board card" in r.getMessage()] == []
    routes._SHARED_KEY_LOGGED.clear()


def test_a_caller_cannot_grow_the_warning_set_with_slugs_nobody_owns() -> None:
    """The set is keyed on a REQUEST PATH value, so what it retains cannot be the caller's choice.

    A slug no configured crew claims is not a collision: nothing collided, the crew is simply not in
    this config, and the rename the warning advises would be wrong advice. Refusing to record those
    is both the right message and the memory bound -- "bounded by the roster" is only true of slugs
    the roster actually claims.
    """
    routes._SHARED_KEY_LOGGED.clear()
    cfg = _cfg(("Releases", "releases"))
    for n in range(50):
        assert routes._card_slot(cfg, f"Ghost{n}", f"ghost{n}") == ""
    assert routes._SHARED_KEY_LOGGED == set(), routes._SHARED_KEY_LOGGED


def test_the_warning_set_is_cleared_at_its_named_ceiling() -> None:
    """A long-lived process can meet many rosters through hot config edits, so the collision-only
    rule is not the whole bound. Cleared rather than evicted by age: there is no order worth
    keeping, and the cost is one repeated warning instead of a set that grows.
    """
    routes._SHARED_KEY_LOGGED.clear()
    routes._SHARED_KEY_LOGGED.update(f"filler-{i}" for i in range(routes._SHARED_KEY_WARN_CAP))
    cfg = _cfg(("Oncall", "oncall"), ("oncall", "oncall"))
    assert routes._card_slot(cfg, "Oncall", "oncall") == ""
    held = routes._SHARED_KEY_LOGGED
    assert held == {"oncall"}, held
    assert len(held) <= routes._SHARED_KEY_WARN_CAP
    routes._SHARED_KEY_LOGGED.clear()


def test_a_slot_one_crew_names_is_still_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control. The collision is the exception; refusing every card would be no feature."""
    cfg = _cfg(("Oncall", "oncall"), ("Releases", "releases"))
    assert routes._card_slot(cfg, "Oncall", "oncall") == routes._panel_slot(cfg, "Oncall", "oncall")
    assert routes._card_slot(cfg, "Oncall", "oncall") != ""


def test_a_crew_this_config_does_not_hold_claims_no_slot() -> None:
    """Zero claimants is not the same answer as one, and must not be read as one.

    A key no configured crew reaches names nobody, so nothing here can say whose board a
    card on it would be. Fail-closed, for the same reason an unowned panel record is refused
    rather than served: content a reader cannot attribute is worse than none.
    """
    assert routes._card_slot(_cfg(("Releases", "releases")), "Oncall", "oncall") == ""


def test_a_member_whose_identity_cannot_be_read_does_not_claim_its_neighbours_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crew whose resolution RAISES is skipped, not counted as a second claimant.

    Counting it would refuse every card in a config that holds one degraded member -- the
    same "an unrelated degradation looks like the crew never published" failure the panel
    slot's own fallback is written to avoid.
    """
    cfg = _cfg(("Oncall", "oncall"), ("Broken", "broken"))
    real = routes._panel_slot

    def _raising(config: Any, member: str, slug: str) -> str:
        if member == "Broken":
            raise ValueError("this member's identity cannot be resolved")
        return real(config, member, slug)

    monkeypatch.setattr(routes, "_panel_slot", _raising)
    assert routes._card_slot(cfg, "Oncall", "oncall") != ""


def test_no_slot_means_the_card_is_neither_stored_nor_the_old_one_dropped(
    service: Any,
) -> None:
    """The guard covers the REMOVAL too, and that is not symmetry for its own sake.

    A retirement is a write whose content is "no board". On a shared key, one crew's missing
    panel would drop the other crew's card -- the same disclosure facing the other way, as a
    deletion.
    """
    lifecycle, slot, state, _no_model = service
    reached: list[str] = []
    lifecycle.publish_derived = lambda *a, **k: reached.append("publish")  # type: ignore[method-assign]
    lifecycle.retire_derived = lambda *a, **k: reached.append("retire")  # type: ignore[method-assign]
    # A card, a record with no board, and no record at all: the three things this helper can
    # be handed, and on an unnamed slot none of them may reach the store.
    routes._publish_derived_card(state, "", _owned(card=_card()), OWNER)
    routes._publish_derived_card(state, "", _owned(template="default"), OWNER)
    routes._publish_derived_card(state, "", None, OWNER, authoritative=True)
    assert reached == [], f"the store was called on a slot no crew names: {reached}"


def test_the_store_is_never_handed_a_slot_the_panel_read_resolved(service: Any) -> None:
    """Source-level, because the two keys are easy to confuse and only one is safe.

    The fold is read off the PANEL slot, which every crew reaching this slug shares. The
    card is stored on the slot that names one crew. Both routes must hand the store the
    second one, so a future edit that passes the panel slot for brevity is caught here.
    """
    source = Path(routes.__file__).read_text(encoding="utf-8")
    assert source.count("_card_slot(cfg, crew_name, slug)") == 1, "the publish route"
    assert source.count("_card_slot(cfg, member, slug)") == 1, "the panel read route"
    # And the panel slot reaches the FOLD read only, never the store call.
    assert "_publish_derived_card(state, _panel_slot(" not in source
    assert '_publish_derived_card(request.app["state"], _panel_slot(' not in source


def test_a_foreign_record_does_not_evict_the_owners_card(service: Any) -> None:
    """The same bug facing the other way. Refusing a foreign record by EVICTING would let a
    colliding crew delete the card belonging to the crew that does own the slot."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)
    assert slot.key in lifecycle.derived
    routes._publish_derived_card(
        state, slot.key, {"crew_key": "someone-else", "crew": "Other Crew"}, OWNER
    )
    assert slot.key in lifecycle.derived, "a foreign record evicted the owner's card"


def test_an_owned_record_still_reaches_the_store(service: Any) -> None:
    """Control for the two above: a guard that refused everything would satisfy both."""
    lifecycle, slot, _state, _no_model = service
    routes._publish_derived_card(_state, slot.key, _owned(card=_card()), OWNER)
    assert slot.key in lifecycle.derived


# ---------------------------------------------------------------------------
# what the publish broadcasts
# ---------------------------------------------------------------------------


def test_a_successful_publish_is_not_broadcast_as_a_removal(service: Any) -> None:
    """``_changed`` derives ``removed`` from the GENERATOR's queue, which a derived card
    never joins -- so routing a successful publish through it announces the card as removed
    and the client answers a removal by resetting the card query it was just handed."""
    lifecycle, slot, state, _no_model = service
    frames: list[Any] = []
    state.broadcast_ws_owners = lambda *args: frames.append(args)
    assert lifecycle.publish_derived(slot, _card()) is True
    assert frames == [("dashboard_card", {"slot": slot.key, "removed": False})]


def test_a_refused_publish_broadcasts_nothing(service: Any) -> None:
    """Nothing changed, so there is nothing to tell anyone -- and a frame here would make a
    refusal indistinguishable from a successful publish."""
    _lifecycle, slot, state, _no_model = service
    frames: list[Any] = []
    state.broadcast_ws_owners = lambda *args: frames.append(args)
    assert _lifecycle.publish_derived(slot, {"html": "", "data": {}}) is False
    assert frames == []


def test_dropping_a_derived_card_IS_broadcast_as_a_removal(service: Any) -> None:
    """The other direction, so the fix above cannot be satisfied by never saying removed.

    ``forget_derived`` keeps ``_changed``, whose queue-derived answer is the correct one
    there: with the derived card gone the read falls through to a queued card if the slot
    has one, and to nothing if it does not.
    """
    lifecycle, slot, state, _no_model = service
    lifecycle.publish_derived(slot, _card())
    frames: list[Any] = []
    state.broadcast_ws_owners = lambda *args: frames.append(args)
    lifecycle.forget_derived(slot.key)
    assert frames == [("dashboard_card", {"slot": slot.key, "removed": True})]


def test_an_unchanged_republish_is_not_broadcast(service: Any) -> None:
    """The panel read republishes the board every time it is served, so an open drawer would
    announce a card event per read and have every client refetch bytes it already holds."""
    lifecycle, slot, state, _no_model = service
    assert lifecycle.publish_derived(slot, _card()) is True
    frames: list[Any] = []
    state.broadcast_ws_owners = lambda *args: frames.append(args)
    # Still a successful publish: the board was rebuilt and stored, it just is not news.
    assert lifecycle.publish_derived(slot, _card()) is True
    assert frames == []


def test_a_changed_republish_is_still_broadcast(service: Any) -> None:
    """Control. A guard that skipped every broadcast would satisfy the test above and leave
    open dashboards frozen on the board's first state."""
    lifecycle, slot, state, _no_model = service
    lifecycle.publish_derived(slot, _stamped("first"))
    frames: list[Any] = []
    state.broadcast_ws_owners = lambda *args: frames.append(args)
    assert lifecycle.publish_derived(slot, _stamped("second")) is True
    assert frames == [("dashboard_card", {"slot": slot.key, "removed": False})]


def test_a_silent_republish_still_advances_the_order(service: Any) -> None:
    """Skipping the BROADCAST must not skip the STORE: the stamp it keeps is what orders the
    next write, so a silent republish that left the revision behind would let a delayed
    read's genuinely older board be accepted afterwards."""
    lifecycle, slot, _state, _no_model = service
    lifecycle.publish_derived(slot, _stamped("same"), "2026-09-29T15:30:00+00:00")
    # Same content, newer record -- the case that produces no frame.
    assert lifecycle.publish_derived(slot, _stamped("same"), "2026-09-29T15:40:00+00:00") is True
    assert lifecycle.derived[slot.key]["revision"] == "2026-09-29T15:40:00+00:00"
    assert lifecycle.publish_derived(slot, _stamped("older"), "2026-09-29T15:35:00+00:00") is False


def test_identical_content_from_a_new_owner_is_broadcast(service: Any) -> None:
    """Unchanged means the content AND the owner. On an owner change ``_derived_for`` withholds
    the held card, so the client was shown nothing -- and the same content arriving under the
    replacement identity is news to it, not a repeat."""
    lifecycle, slot, state, _no_model = service
    lifecycle.publish_derived(slot, _card())
    slot._dashboard_card_identity = "owner-replacement"
    frames: list[Any] = []
    state.broadcast_ws_owners = lambda *args: frames.append(args)
    assert lifecycle.publish_derived(slot, _card()) is True
    assert frames == [("dashboard_card", {"slot": slot.key, "removed": False})]


# ---------------------------------------------------------------------------
# the card's LIFECYCLE: created on a board change, evicted when the board goes
# ---------------------------------------------------------------------------
#
# One invariant seen from three sides. The card was minted only by the drawer READ, so:
# nothing produced it when the board actually changed (and the drawer is the surface this work
# replaces, which would leave the card with no producer at all); nothing evicted it when the
# record stopped carrying a board, so a stale board was served as current; and the store it
# lives in existed only if the model-card cost opt-in had ever been flipped, which made a free
# card's availability depend on toggle history.


def test_no_board_on_the_record_evicts_the_card(service: Any) -> None:
    """A record without a board says the board is GONE, not "leave things as they are".

    The record is the authoritative answer for the slot, so a crew that republishes to another
    template -- or whose work fold holds no board -- must not keep its earlier card
    being served as `status: published`. That is a stale board presented as current, which a
    reader cannot tell from a live one.
    """
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)
    assert slot.key in lifecycle.derived
    routes._publish_derived_card(state, slot.key, _owned(template="default"), OWNER)
    assert slot.key not in lifecycle.derived


def test_a_panel_that_is_gone_retires_the_card(service: Any) -> None:
    """NO RECORD AT ALL is the board being gone, retired by the AUTHORITATIVE publish route.

    The ownership guard reads a crew key off the record, and a record that is not there has
    none -- so an absent panel matched nothing, returned early, and left the previous card
    served as `status: published`: a board that is gone, presented as current. The publish
    route holds the record it just wrote, so an absent one is a real retraction and the card
    goes.
    """
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert slot.key in lifecycle.derived
    routes._publish_derived_card(state, slot.key, None, OWNER, authoritative=True)
    assert slot.key not in lifecycle.derived, "an absent panel left its card published"


def test_a_non_authoritative_absent_read_does_not_drop_a_held_card(service: Any) -> None:
    """The REFRESHER's absent read cannot retire a card, because it may be racing a publish.

    An absent record carries no stamp to order by, and the panel-read refresher's retirement
    runs a worker hop later on the event loop -- so a publish can store a newer board between
    the read and the removal. A stampless non-authoritative retirement would then drop that
    live board and serve a gone card as current. So a non-authoritative absent read leaves a
    held card alone; only the authoritative publish route, which just wrote the record, retires
    it. The control above proves the authoritative path still does.
    """
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert slot.key in lifecycle.derived
    # The refresher reads absent and tries to retire, non-authoritatively.
    routes._publish_derived_card(state, slot.key, None, OWNER)
    assert slot.key in lifecycle.derived, "a non-authoritative absent read dropped a live card"


def test_an_absent_panel_leaves_no_stamp_to_outrank_a_real_board(service: Any) -> None:
    """It carries no `published_at`, so there is nothing to order it by -- and inventing one
    would let an absent panel outrank a board that is really there. A publish racing the
    removal is therefore accepted on its own stamp."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(state, slot.key, None, OWNER)
    assert slot.key not in lifecycle._retired
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert slot.key in lifecycle.derived


def test_a_foreign_record_is_still_not_a_missing_one(service: Any) -> None:
    """Control. The absent case is taken BEFORE the ownership check, so it must not swallow it:
    a colliding crew's record still neither stores nor evicts."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)
    assert slot.key in lifecycle.derived
    routes._publish_derived_card(
        state, slot.key, {"crew_key": "someone-else", "crew": "Other Crew"}, OWNER
    )
    assert slot.key in lifecycle.derived, "a foreign record was read as a missing one"


def test_a_retirement_builds_the_store_it_needs_to_be_remembered_in(service: Any) -> None:
    """A retirement records the stamp that orders everything after it, so with no store there is
    nowhere to put it and the retirement is simply lost.

    Deciding whether a store is needed from the record's CONTENT is what left that gap: a
    boardless record looked like nothing worth building for, and then a delayed read still
    holding the pre-retirement board built the store itself and published that board as current.
    """
    _lifecycle, slot, state, _no_model = service
    state._dynamic_cards = None
    built: list[Any] = []
    real = card_lifecycle.CardLifecycle(state, enabled=False)

    def _make() -> Any:
        built.append(real)
        state._dynamic_cards = real
        return real

    state.ensure_dynamic_card_store = _make
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(template="default", published_at="2026-09-29T15:40:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert built, "a retirement with no store did not build one"
    assert real._retired.get(slot.key) == "2026-09-29T15:40:00+00:00"
    # The delayed read holding the pre-retirement board is now refused, which is the whole point.
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
    )
    assert slot.key not in real.derived, "the retired board came back through a fresh store"


def test_a_delayed_no_board_read_does_not_evict_a_newer_card(service: Any) -> None:
    """A REMOVAL is a write whose content is "no board", so it takes the same order.

    Ordering only the publications leaves the eviction path taking any arrival: a panel read
    that snapshotted a record with no board lands after a publish stored one, and the card is
    dropped even though the newer record has a board. The same inversion the write path refuses,
    reached through the other door.
    """
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:40:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert slot.key in lifecycle.derived
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(template="default", published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
    )
    assert slot.key in lifecycle.derived, "an older no-board read evicted a newer card"


def test_a_newer_no_board_record_still_evicts(service: Any) -> None:
    """Control. An order that refused every removal would satisfy the test above and serve a
    board that is gone as `status: published` forever."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
        authoritative=True,
    )
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(template="default", published_at="2026-09-29T15:40:00+00:00"),
        OWNER,
    )
    assert slot.key not in lifecycle.derived


def test_the_writer_may_retire_its_own_revision(service: Any) -> None:
    """The tie-break reaches retirement too: a publish that wrote a board and then republished
    the same record without one, inside a single second, is stating the board is gone."""
    lifecycle, slot, state, _no_model = service
    stamp = "2026-09-29T15:30:00+00:00"
    routes._publish_derived_card(
        state, slot.key, _owned(card=_card(), published_at=stamp), OWNER, authoritative=True
    )
    routes._publish_derived_card(
        state, slot.key, _owned(template="default", published_at=stamp), OWNER, authoritative=True
    )
    assert slot.key not in lifecycle.derived


def test_a_retirement_still_orders_the_arrival_after_it(service: Any) -> None:
    """Dropping the card drops the stamp that ordered the next write, so the retirement has to
    keep it. Without that the store holds nothing, every revision is accepted, and an in-flight
    read that snapshotted the board record BEFORE the retirement republishes what was retired."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
        authoritative=True,
    )
    # The agent publishes a non-board panel: the board is gone as of T2.
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(template="default", published_at="2026-09-29T15:40:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert slot.key not in lifecycle.derived
    # The delayed drawer read, holding its pre-retirement snapshot, lands now.
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:30:00+00:00"),
        OWNER,
    )
    assert slot.key not in lifecycle.derived, "a retired board came back from a stale read"


def test_a_genuinely_newer_board_comes_back_after_a_retirement(service: Any) -> None:
    """Control. A retirement that blocked every later write would make the card unrecoverable
    for the life of the gateway."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(template="default", published_at="2026-09-29T15:40:00+00:00"),
        OWNER,
        authoritative=True,
    )
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:50:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert slot.key in lifecycle.derived


def test_the_retirement_stamp_goes_when_the_slot_is_definitively_removed(service: Any) -> None:
    """The stamp is kept so a write arriving after a retirement can be ordered against it. A slot
    that is definitively gone has no later write to order, so keeping it retains one string per
    slot the gateway ever hosted -- unbounded, and recoverable only by restarting the process.

    Textual on the removal site as well as behavioural, because the leak is the ABSENCE of a call
    and no reachable test of this store can prove a call somewhere else happens.
    """
    lifecycle, slot, _state, _no_model = service
    lifecycle._retired[slot.key] = "2026-09-29T15:40:00+00:00"
    lifecycle.forget_retired(slot.key)
    assert slot.key not in lifecycle._retired

    source = Path(state_module.__file__).read_text(encoding="utf-8")
    # The definitive-removal path drops the card; it has to drop the stamp beside it.
    assert source.count("self._dynamic_cards.forget_retired(key)") == 1
    drop_card = source.index("self._dynamic_cards.forget_derived(key)")
    drop_stamp = source.index("self._dynamic_cards.forget_retired(key)")
    assert drop_card < drop_stamp, "the stamp is dropped somewhere other than beside the card"


def test_the_retirement_stamp_is_dropped_once_a_card_is_stored_again(service: Any) -> None:
    """It exists only to order what follows the removal, so it must not outlive the removal and
    start ordering against a card that is present."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(template="default", published_at="2026-09-29T15:40:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert lifecycle._retired.get(slot.key) == "2026-09-29T15:40:00+00:00"
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:50:00+00:00"),
        OWNER,
        authoritative=True,
    )
    assert slot.key not in lifecycle._retired


def test_one_order_governs_writes_and_removals(service: Any) -> None:
    """Both paths ask the SAME question, so they cannot drift into two orders -- which is how
    the write path ended up guarded while the removal path was not."""
    lifecycle, _slot, _state, _no_model = service
    source = Path(card_lifecycle.__file__).read_text(encoding="utf-8")
    assert source.count("def _out_of_order(") == 1
    # Exactly two callers: the publication and the retirement.
    assert source.count("self._out_of_order(") == 2


def test_eviction_does_not_need_a_card_to_have_been_there(service: Any) -> None:
    """Control: the evicting branch is total, so a first publish of a non-board record is a
    no-op rather than an error."""
    _lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(state, slot.key, _owned(template="default"), OWNER)
    routes._publish_derived_card(state, slot.key, None, OWNER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute", "value"),
    [("memory_mode", "incognito"), ("executor", "remote")],
)
async def test_a_slot_that_tightens_after_publishing_loses_its_card(
    service: Any, attribute: str, value: Any
) -> None:
    """Privacy and remoteness are properties of the slot AS IT IS NOW.

    Checked only at publish, they say nothing about a card that is already stored: a persistent
    session turned incognito went on serving content the live rules withhold. A published card
    outliving the condition that permitted it is the same defect as never having checked.
    """
    lifecycle, slot, _state, _no_model = service
    assert lifecycle.publish_derived(slot, _card()) is True
    setattr(slot, attribute, value)
    assert (await lifecycle.read(slot))["card"] is None
    assert slot.key not in lifecycle.derived, "withheld content stays in memory"


@pytest.mark.asyncio
async def test_a_slot_that_stays_eligible_keeps_its_card(service: Any) -> None:
    """Control for the three cases above, so the re-check cannot be satisfied by evicting
    every card on every read."""
    lifecycle, slot, _state, _no_model = service
    lifecycle.publish_derived(slot, _card())
    assert (await lifecycle.read(slot))["status"] == "published"
    assert (await lifecycle.read(slot))["status"] == "published"


def test_a_derived_card_gets_a_store_without_the_model_opt_in(service: Any) -> None:
    """The store was built only by `set_dynamic_cards_enabled(True)`, so a free card's
    availability depended on TOGGLE HISTORY: never enabled meant no board at all, while
    enabled-once-then-off left a store behind and the board appeared. Same feature, opposite
    answers, decided by a switch neither answer is about."""
    _lifecycle, slot, state, _no_model = service
    state._dynamic_cards = None
    created: list[Any] = []

    class Store:
        derived: dict[str, Any] = {}

        def publish_derived(
            self,
            s: Any,
            payload: Any,
            revision: str = "",
            authoritative: bool = False,
        ) -> bool:
            created.append((payload, revision, authoritative))
            return True

        def forget_derived(self, key: str) -> None:
            pass

    state.ensure_dynamic_card_store = lambda: Store()
    routes._publish_derived_card(state, slot.key, _owned(card=_card()), OWNER)
    assert created, "no store was created for a derived card"


def test_the_real_state_builds_the_store_with_the_model_path_off() -> None:
    """``ensure_dynamic_card_store`` must construct the container and start no worker -- that
    is what keeps the model path exactly as opt-in as it was."""
    from kiro_crew.dashboard import state as state_mod

    holder = SimpleNamespace(_dynamic_cards=None, _background_tasks=set(), _slots={})
    store = state_mod.DashboardState.ensure_dynamic_card_store(holder)  # type: ignore[arg-type]
    assert store is not None
    assert store.enabled is False, "creating the store must not enable the model path"
    assert store.worker is None, "creating the store must not start a worker"
    assert not store.publisher.attempts, "creating the store must spend no budget"
    assert state_mod.DashboardState.ensure_dynamic_card_store(holder) is store  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# an older build must not overwrite a newer card
# ---------------------------------------------------------------------------
#
# THE INVARIANT, stated once because this span has now been raised twice in different places: a
# derived card write must not be applied out of order with respect to the RECORD it was built
# from. A card is built in one hop and stored in another, so two requests interleave -- a panel
# read snapshots a record, a publish stores a newer card, and the delayed read then republishes
# its older snapshot as current. Nothing in the payload says which board it describes, so the
# store cannot tell a stale write from a fresh one without the source stamp.
#
# Pinning the invariant rather than the two sites is deliberate: the previous round fixed one
# ordering hole in this same span and the next head grew another somewhere else.


def _stamped(lede: str) -> dict[str, Any]:
    card = _card()
    card["data"]["lede"] = lede
    return card


def test_a_card_built_from_an_older_record_cannot_overwrite_a_newer_one(service: Any) -> None:
    lifecycle, slot, _state, _no_model = service
    assert lifecycle.publish_derived(slot, _stamped("newer"), "2026-09-29T15:40:00+00:00") is True
    # The delayed arrival of a read that snapshotted the earlier record.
    assert lifecycle.publish_derived(slot, _stamped("older"), "2026-09-29T15:30:00+00:00") is False
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "newer"


def test_a_newer_record_does_replace_the_card(service: Any) -> None:
    """Control. A guard that refused everything would satisfy the test above and freeze the
    board on its first publish."""
    lifecycle, slot, _state, _no_model = service
    lifecycle.publish_derived(slot, _stamped("first"), "2026-09-29T15:30:00+00:00")
    assert lifecycle.publish_derived(slot, _stamped("second"), "2026-09-29T15:40:00+00:00") is True
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "second"


def test_two_records_in_one_second_do_not_let_the_reader_overwrite_the_writer(
    service: Any,
) -> None:
    """The stamp is the record's `published_at` at SECOND granularity and nothing throttles a
    publish to one per second, so two genuinely different records can share a revision. Ordering
    on the stamp alone has to accept that tie, which is the stale overwrite with a smaller
    window rather than without one."""
    lifecycle, slot, _state, _no_model = service
    stamp = "2026-09-29T15:30:00+00:00"
    # The publish route stores the record it just wrote.
    assert lifecycle.publish_derived(slot, _stamped("newer"), stamp, authoritative=True) is True
    # The delayed panel read arrives holding an OLDER snapshot that shares the second.
    assert lifecycle.publish_derived(slot, _stamped("older snapshot"), stamp) is False
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "newer"


def test_the_writer_still_wins_when_the_reader_got_there_first(service: Any) -> None:
    """The other interleaving of the same second. A refresher's card is not wrong, it is only
    unranked -- so the authoritative write must still replace it rather than be refused as a tie."""
    lifecycle, slot, _state, _no_model = service
    stamp = "2026-09-29T15:30:00+00:00"
    lifecycle.publish_derived(slot, _stamped("read snapshot"), stamp)
    assert lifecycle.publish_derived(slot, _stamped("published"), stamp, authoritative=True) is True
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "published"


def test_the_refresher_still_rehydrates_when_nothing_is_held(service: Any) -> None:
    """Control, and the reason the tie-break is not simply "readers never write". With no card
    stored there is nothing to make stale, so the read path must still fill it -- this is the
    post-restart path, and a guard that refused it would leave an idle conductor with no board."""
    lifecycle, slot, _state, _no_model = service
    stamp = "2026-09-29T15:30:00+00:00"
    assert lifecycle.publish_derived(slot, _stamped("rehydrated"), stamp) is True
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "rehydrated"


def test_the_refresher_still_wins_on_a_newer_record(service: Any) -> None:
    """Second control. A refresher is refused only at an EQUAL revision; when the store is
    behind, the read path is what catches it up."""
    lifecycle, slot, _state, _no_model = service
    lifecycle.publish_derived(
        slot, _stamped("old"), "2026-09-29T15:30:00+00:00", authoritative=True
    )
    assert lifecycle.publish_derived(slot, _stamped("new"), "2026-09-29T15:40:00+00:00") is True
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "new"


def test_an_authoritative_rebuild_of_its_own_record_still_refreshes(service: Any) -> None:
    """The publish route may write the same revision twice -- a retry, or a republish of the same
    record -- and that is a refresh, not a stale write."""
    lifecycle, slot, _state, _no_model = service
    stamp = "2026-09-29T15:30:00+00:00"
    lifecycle.publish_derived(slot, _stamped("first"), stamp, authoritative=True)
    assert lifecycle.publish_derived(slot, _stamped("again"), stamp, authoritative=True) is True
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "again"


def test_a_record_with_no_stamp_is_still_published(service: Any) -> None:
    """A record carrying no ``published_at`` has nothing to order by, and dropping a real board
    over a missing field would be a worse answer than showing it."""
    lifecycle, slot, _state, _no_model = service
    assert lifecycle.publish_derived(slot, _stamped("unstamped"), "") is True
    assert lifecycle.derived[slot.key]["card"]["data"]["lede"] == "unstamped"


def test_an_unstamped_write_does_not_erase_the_order(service: Any) -> None:
    """It must not advance the stored stamp either, or an unstamped write would make a later
    genuine write look older and be refused."""
    lifecycle, slot, _state, _no_model = service
    lifecycle.publish_derived(slot, _stamped("newer"), "2026-09-29T15:40:00+00:00")
    lifecycle.publish_derived(slot, _stamped("unstamped"), "")
    assert lifecycle.derived[slot.key]["revision"] == "2026-09-29T15:40:00+00:00"
    assert lifecycle.publish_derived(slot, _stamped("older"), "2026-09-29T15:30:00+00:00") is False


def test_the_route_passes_the_records_publish_stamp(service: Any) -> None:
    """The stamp has to come from the RECORD, not from the store's own clock: two cards built
    from one record share a revision and differ in store time, and it is the record they
    describe that decides which is newer."""
    lifecycle, slot, state, _no_model = service
    routes._publish_derived_card(
        state,
        slot.key,
        _owned(card=_card(), published_at="2026-09-29T15:37:00+00:00"),
        OWNER,
    )
    assert lifecycle.derived[slot.key]["revision"] == "2026-09-29T15:37:00+00:00"
