"""The reading half of a crew's webview: the panel record and the drawer route that serves it.

Which slot a crew's panel and board card are stored on, the record the drawer reads (the
published record, with a pipeline board's numbers taken from the crew log), the board card
derived from that record, the keys a panel read serves and withholds, and
``GET /api/members/{slug}/panel``. The publish route composes the same record, so the card
it derives is the one the drawer reads.

:mod:`kiro_crew.dashboard.handlers.agent_panel` is this module's facade: callers and tests
import and patch every name through it, and a write there -- ``monkeypatch.setattr``,
``mock.patch`` -- reaches this module too (:mod:`kiro_crew._facade_mirror`). This module
never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final, cast

from aiohttp import web

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew import pipeline_board_contract
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import projection
from kiro_crew.crew_log.entry_types import PANEL_FOLD_NAME
from kiro_crew.dashboard.handlers.members import _deny_app_caller, _member_thread_slot
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.members import MemberSlugError, is_readable_member_name
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.work_vocab import WORK_FOLD_NAME, WorkBoardView

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")


def _panel_slot(cfg: KiroCrewConfig, member: str, slug: str) -> str:
    """The DM slot a crew's panel fold lives on.

    The same derivation ``api_member_thread`` uses to CREATE that thread, so it names
    the slot the publishing session actually runs under -- a member's DM session is
    the only place the panel tool is mounted. Derived rather than looked up so a crew
    whose thread is not running still reads its last panel.

    The fallback is the V1 key, taken when the memory-store resolution refuses: an
    unknown member, or a member identity whose V2 store record is missing or
    degraded. Blanking a published panel because a store record is unreadable would
    make an unrelated degradation look like the crew never published, and the
    fallback cannot serve another crew's record -- the slug is part of both keys, and
    the ownership digest is re-checked on whatever is read.
    """
    try:
        slot, _store = _member_thread_slot(cfg, member, slug)
    except UnknownMemoryStore:
        return members_mod.member_slot_key(slug)
    return slot


#: Slugs already warned about, so the shared-key refusal is reported once rather than on every
#: panel read.
#:
#: BOUNDED TWICE, because one bound was an inference and inferences are what this file keeps
#: getting wrong. ``slug`` arrives on the REQUEST PATH, so "bounded by the roster" was only true of
#: the slugs a configured crew actually claims -- a caller asking about slugs nobody owns grew this
#: set for free. So: nothing is recorded unless two configured crews really do collide (below), and
#: the set is CLEARED at a named ceiling in case a long-lived process sees many rosters through hot
#: config edits.
_SHARED_KEY_WARN_CAP: Final[int] = 256


_SHARED_KEY_LOGGED: set[str] = set()


def _card_slot(cfg: KiroCrewConfig, member: str, slug: str) -> str:
    """The slot a crew's board card may be stored on, or ``""`` when no slot names it.

    The panel slot is not always enough. A V2 member's key carries its private store
    generation, which is unique to that member, so the key names exactly one crew. The V1
    key is the slug alone, and slugification is many-to-one -- ``Oncall`` and ``oncall``
    reach one slug, and two agents may even carry one ``member_id`` -- so on V1 a single
    key is every colliding crew's key at once.

    The FOLD survives that, keeping a record per ownership digest, and the panel routes
    survive it by checking the digest on whatever they read. The card store cannot: it is
    keyed on the slot, and the slot carries no crew of its own, only a per-session
    identity. So a card built from one crew's record, stored on a shared key, is served by
    the owner-only card route as the OTHER crew's board -- one crew's state read under
    another's name, which is the defect :func:`kiro_crew.agent_panel.publish` exists to
    prevent, reappearing on the surface that has no digest to check.

    Answered by asking which configured crews reach this key, rather than by reading a
    binding off the slot, because there is no such binding to read. Exactly one is the
    only safe answer: two means the key is shared, and none means no crew in this config
    claims it, so nothing here can say whose board it would be.

    Refusing costs the CARD and nothing else -- the drawer keeps its own slot and both
    crews keep their panels -- and the ambiguity is a configuration a person can end by
    renaming, which is the remedy ``member`` collisions already carry elsewhere.
    """
    key = _panel_slot(cfg, member, slug)
    if not key:
        return ""
    seen = 0
    for name in getattr(cfg, "agents", {}) or {}:
        try:
            other = _panel_slot(cfg, name, members_mod.member_slug(name, cfg))
        except Exception:
            # A crew whose identity cannot be resolved claims no key. Skipped rather than
            # counted: counting it would refuse every card in a config holding one
            # degraded member, and treating a failure as a claim on THIS key is a guess
            # about the very thing that could not be read.
            continue
        if other == key:
            seen += 1
    # ONE decision, in one place. An early exit on the second claimant was here and no test
    # could tell it from this line, because it answered the same question a loop iteration
    # sooner: a crew roster is small, so it bought nothing a reader had to reason about.
    if seen == 1:
        return key
    # LOGGED ONLY FOR A REAL COLLISION, and that is the memory bound as much as it is the right
    # message. Two or more configured crews reaching one key is the case the remedy fits: rename
    # one. ZERO claimants is a different thing -- a slug this config does not hold, which every
    # caller can ask about freely -- so advising a rename there would be wrong AND would let a
    # request path decide what this set retains.
    #
    # Once per slug rather than per call, because a panel read happens on every drawer open and a
    # warning per read is a log nobody reads. The remedy travels in the message, since whoever
    # meets the line has no reason to come looking for this docstring.
    if seen > 1 and slug not in _SHARED_KEY_LOGGED:
        if len(_SHARED_KEY_LOGGED) >= _SHARED_KEY_WARN_CAP:
            # CLEARED, not evicted by age. There is no order worth maintaining here and the cost of
            # clearing is one repeated warning, which is the harmless direction; the alternative is
            # either dropping warnings forever or keeping a list that grows.
            _SHARED_KEY_LOGGED.clear()
        _SHARED_KEY_LOGGED.add(slug)
        logger.warning(
            "no board card for member slug %s: %d configured crews resolve to one slot key, so "
            "the key cannot say whose board a card on it would be. Rename one crew, or give it a "
            "private memory store, to separate them.",
            slug,
            seen,
        )
    return ""


#: How long a pipeline board's newest work entry may be before the header calls it
#: stale. A HOST value: the fold cannot know it and the publisher must not decide when
#: its own board stops counting as current.
#:
#: About TEN patrol intervals -- the conductor skill arms its patrol near 90 seconds --
#: rather than one plus slack. One interval would call a board stale the moment a single
#: cycle did no work, which is the normal quiet cycle and not news; ten means the log has
#: been silent across many cycles, and a fleet nobody has heard from in a quarter of an
#: hour is the thing a reader needs told.
BOARD_STALE_AFTER_SECONDS: Final[int] = 900


def _panel_record(slot: str, slug: str, owner_key: str) -> dict[str, Any] | None:
    """The crew's panel record, with a contract template's NUMBERS taken from the log.

    Two steps: pick the record (:func:`_published_record`, whose selection rules are
    their own story), then, for the one template that has a declared contract, replace
    its data with :func:`~kiro_crew.pipeline_board_contract.build_pipeline_board`'s
    output. Everything else is served exactly as published.

    Done HERE rather than in the drawer because both surfaces read this one record: the
    composed document carries it in its data island, and the docked native summary is
    rendered from the same ``data`` object travelling beside it. A frontend fix would
    have to be made twice and could not be made at all in the document -- its srcdoc
    runs on a null origin under ``connect-src 'none'``, so it can never fetch anything.
    """
    record = _published_record(slot, slug, owner_key)
    if record is None:
        return record
    if str(record.get("template") or "") != pipeline_board_contract.BOARD_TEMPLATE_ID:
        return record
    return _with_board_numbers(slot, record)


def _with_board_numbers(slot: str, record: dict[str, Any]) -> dict[str, Any]:
    """*record* with its data rebuilt from the ``work`` fold, or *record* unchanged.

    UNCHANGED is the answer for a board that is not there. A crew whose work fold is
    absent, empty or unreadable must not be handed a complete board of zeros: zero
    items is a fact about a board that exists, and "no board" is a different one. Left
    as published, the template's three-state renderer reads the missing sections as
    ABSENT and says "not said", which is the true statement.

    The fold is read through the ordinary slot-keyed projection -- the same warm kernel
    the panel fold above uses, so a second drawer open folds no entry again and this
    route keeps no cache of its own. It is the member's OWN DM slot both times: a
    conductor's board binds to the slot its conductor entries name, which for a crew
    publishing its own panel is that same DM slot.

    A publisher that wrote the free shape is not an error to the reader -- the author
    is gone and the payload is already on disk -- so its unusable keys are dropped, the
    derived numbers are rendered, and ``contract_replaced`` names what was dropped. A
    publisher quietly overriding the log is the lie this whole contract exists to stop,
    so being overridden has to leave a mark.
    """
    try:
        view = projection.read_slot_projection(slot, WORK_FOLD_NAME).value
    except Exception:
        # Same totality contract as the panel fold above: a damaged log reads as
        # "nothing folded" rather than as a 500, and WARNING because a panel whose
        # numbers silently stopped updating is a crew-visible symptom with no other
        # trace, reproduced on every read until an operator repairs the log.
        logger.warning("work fold unreadable for slot %s", slot, exc_info=True)
        # MARKED, because "I could not derive the board" and "there is no board" are different
        # facts that the record alone cannot separate: both leave the published data in place.
        # For the drawer they are the same answer -- the three-state renderer says "not said",
        # which is true of both -- but for the CARD they are opposite instructions. An absence is
        # authoritative and evicts; a failure must leave the last good card alone, or one
        # unreadable fold deletes a valid card, broadcasts its removal, and does it again on
        # every read until an operator repairs the log.
        #
        # Withheld from the response by ``_PANEL_WITHHELD_KEYS``, like ``board`` and ``card``.
        unreadable = dict(record)
        unreadable["board_unreadable"] = True
        return unreadable
    if not _is_work_board(view):
        return record
    try:
        judgment = pipeline_board_contract.validate_judgment(record.get("data"))
        replaced: list[str] = []
    except pipeline_board_contract.JudgmentError as exc:
        judgment = pipeline_board_contract.EMPTY_JUDGMENT
        replaced = sorted(record.get("data") or {}) if isinstance(record.get("data"), dict) else []
        logger.warning(
            "panel data for slot %s is not a board judgment (%s); rendering the log's "
            "own numbers and dropping the published keys %s",
            slot,
            exc,
            replaced,
        )
    panel = pipeline_board_contract.build_pipeline_board(
        # THE CAST'S LIMIT, stated rather than left to be assumed. ``Projection.value``
        # is ``Any``, so this asserts the shape instead of checking it -- mypy proves
        # the PROVIDER reads only fields ``WorkBoardView`` declares, and that
        # ``_work_render`` writes exactly them, but nothing type-checks that this
        # value came from that renderer. ``_is_work_board`` above is the runtime half
        # that makes the assertion safe in the direction that bites: a value that is
        # not a board at all is refused before it reaches here.
        cast("WorkBoardView", view),
        judgment,
        name=str(record.get("crew") or ""),
        captured_at=str(record.get("published_at") or ""),
        stale_after_seconds=BOARD_STALE_AFTER_SECONDS,
        now_epoch=time.time(),
    )
    out = dict(record)
    # THE CARD, built here because this is the one place the board exists: the fold has
    # been read, the publisher's judgment validated and the provider run, so the only
    # thing left is the flattening -- and doing it anywhere else would mean reading the
    # fold a second time.
    #
    # WITHHELD from the drawer response like ``board`` is, and handed to the dynamic-card
    # store by the async caller rather than from here: this function runs in a worker
    # thread and the store is loop-owned, so the hop belongs at the boundary that already
    # exists. No model is called on this path and none can be: ``panel_card_data`` is
    # arithmetic over a TypedDict.
    try:
        out["card"] = {
            "html": _card_page(),
            "data": pipeline_board_contract.panel_card_data(panel),
        }
    except OSError:
        # A page the build did not ship, or one that cannot be read. The CARD is optional and
        # the PANEL is what this route owes its caller, so this degrades to no card rather than
        # to a 500 on somebody's drawer -- the record is returned without a ``card`` key, which
        # the publisher reads as "evict", the same as any other board that is not there.
        #
        # Caught HERE rather than left to the caller: ``_panel_record`` runs outside
        # ``_read_and_compose``'s own ``try``, and the route catches only ``MemberSlugError``
        # and ``ValueError``, so an OSError from the page read would have escaped as a 500 --
        # which is what ``_card_page``'s docstring already claimed does not happen.
        logger.warning("the board card page could not be read for slot %s", slot, exc_info=True)
    # A SIBLING key, and ``data`` is left exactly as published.
    #
    # The two surfaces want different things from this record. The document renders the
    # contract, so the composer reads ``board``. The drawer's DOCKED card is native
    # React that walks ``data``'s own key order and prints the first entries as headline
    # tiles -- so putting the derived board in ``data`` made a conductor's compact card
    # lead with "contract version 1" and "omitted 0", which is the least interesting
    # pair of numbers on it. The publisher's judgment is what belongs in a one-line
    # card: it is the sentence a person wrote.
    out["board"] = pipeline_board_contract.panel_payload(panel)
    if replaced:
        # On the RECORD, and deliberately not on the read route's JSON response: the
        # override has to leave a mark a reader of this record can find, but a response
        # field nothing renders is a field with no reader. The warning above is what
        # reaches the operator, who is the party that can act on it.
        out["contract_replaced"] = replaced
    return out


def _publish_derived_card(
    state: Any,
    slot_key: str,
    record: Mapping[str, Any] | None,
    expected_owner: str,
    authoritative: bool = False,
) -> None:
    """Hand the board's card to the dynamic-card store, if this record carries one.

    *expected_owner* is the ownership digest of the crew this slot's card may describe, and it
    is REQUIRED rather than defaulted so a future caller cannot omit it and reopen the hole
    below. ``_published_record`` answers with the slug-keyed file, unfiltered, when the fold
    holds nothing under the reading digest -- and one slug can carry two crews. The panel READ
    route is safe by ordering, refusing a foreign record before it reaches here; the publish
    route had no such refusal, so a colliding crew's board could be stored on the live slot and
    then served by the owner-only card route. Checked here rather than at one call site because
    the refusal belongs with the write it protects.

    A mismatch stores nothing AND evicts nothing: evicting on a foreign record would be the
    same bug facing the other way, letting a colliding crew delete the owner's card.

    *authoritative* says this caller holds the record it just wrote, rather than a snapshot of
    unknown age. Only the publish route does, and the store uses it to break a tie between two
    records that share one second-granularity ``published_at``.

    An EMPTY *slot_key* means no slot names this crew alone -- see :func:`_card_slot` --
    and nothing is written or removed. Taken before the absent-record branch, because a
    retirement is a write too: on a shared key, one crew's missing panel would drop the
    other crew's card.

    ON THE EVENT LOOP, after the worker hop: the store is loop-owned and
    :func:`_with_board_numbers` runs in a thread, so this is the boundary that already
    exists rather than a new threadsafe call added inside it.

    TOTAL. A card that cannot be stored -- no store bound yet, a slot that is not running,
    a payload the host refuses -- must not turn somebody's drawer read into a 500: the
    panel response is what this route owes the caller, and the card is a side effect of
    having built the board anyway.
    """
    if not slot_key:
        # NO SLOT NAMES THIS CREW ALONE. Neither store nor evict: the store is keyed on the
        # slot and the slot carries no crew, so a write here would land on whatever crew the
        # shared key currently presents, and a removal would drop that crew's card. Above
        # the ownership check below rather than beside it, because that check reads the
        # RECORD's owner -- it answers whether the record is ours, never whether the slot is.
        return
    if getattr(state, "_slots", {}).get(slot_key) is None:
        # THE SLOT IS GONE, so neither a store write nor a RETIREMENT belongs here. This runs a
        # worker hop after the read that produced *record*, and ``push_slot_removed`` can clear
        # the slot in that gap: it already evicts the slot's derived card AND drops its
        # retirement stamp as the one place that knows the removal is definitive. A retirement
        # written here after that close has no later publish to clear it -- ``forget_retired``
        # only fires on a subsequent publish, and a gone slot has none -- so each closed slot
        # that lost this race would leave one orphan ``(key, stamp)`` pair in ``_retired`` for
        # the life of the process. Refusing before BOTH retirement branches (absent-record and
        # no-card) and the store write keeps every retained field bounded: a slot that is gone
        # has no card to serve and nothing to order a late write against.
        return
    if record is None:
        # NO RECORD AT ALL is the board being gone, not a record about somebody else -- the crew
        # never published, or its panel has been removed. Taken BEFORE the ownership check
        # below, because that check reads a crew key off the record and a record that is not
        # there has none: routed through it, an absent panel matched nothing, returned early,
        # and left the previous card served as `status: published` -- a board that is gone,
        # presented as current.
        #
        # Retired with NO revision, which the store reads as "do not order this": there is no
        # stamp to order by, and inventing one would let an absent panel outrank a real board.
        #
        # ORDERED BY WHO IS ASKING, because with no stamp nothing else can order it. The publish
        # route is AUTHORITATIVE here: it just wrote this record, so an absent one means the crew
        # really retracted its board, and the retirement stands. The panel-read REFRESHER is not:
        # its record is a fresh read, but the retirement runs a worker hop later on the event
        # loop, so a publish can store a newer board in between -- and a stampless removal would
        # then drop that live board, presenting a gone card the reader cannot tell from current.
        # So a non-authoritative absent read leaves a held card ALONE; the next authoritative
        # publish (or an owner change) retires it, and nothing live is dropped on a race. A board
        # genuinely gone with no publish behind it is retired by the record-present path below,
        # which carries the record's own stamp and orders correctly.
        cards = getattr(state, "_dynamic_cards", None)
        if cards is not None and (authoritative or cards.derived.get(slot_key) is None):
            cards.retire_derived(slot_key, "", authoritative=authoritative)
        return
    if not expected_owner or str(record.get("crew_key") or "") != expected_owner:
        # BEFORE anything reads the card or touches the store: see the docstring. Neither store
        # nor evict, because this record is not about this slot's crew at all.
        return
    if record.get("board_unreadable"):
        # The fold could not be READ, which is not the board being gone. Leave the stored card
        # exactly as it is: a board that cannot be derived for a moment is not a board that
        # changed, and the same reasoning already governs a payload the host refuses.
        return
    card = record.get("card")
    cards = getattr(state, "_dynamic_cards", None)
    make_store = getattr(state, "ensure_dynamic_card_store", None)
    if cards is None and callable(make_store):
        # THE STORE IS CREATED FOR A DERIVED CARD TOO, and this is what makes the "a derived
        # card is free, so the cost opt-in does not gate it" claim actually true. The store
        # itself was only ever constructed on the first `set_dynamic_cards_enabled(True)`, so
        # availability depended on TOGGLE HISTORY: an owner who never enabled model-written
        # cards got no board either, and one who enabled them once and turned them off kept
        # getting one. Same feature, opposite answers, decided by a switch neither answer is
        # about.
        #
        # ``enabled=False`` is the whole point: this constructs the container and starts no
        # worker, spends no attempt and calls no model. The model path stays exactly as
        # opt-in as it was.
        #
        # BUILT FOR A RETIREMENT AS WELL AS A CARD, and not as a convenience: a retirement
        # records the stamp that orders everything arriving after it, so with no store there is
        # nowhere to put it and the retirement is simply lost. A delayed read still holding the
        # pre-retirement board then creates the store itself and publishes that board as
        # current. Deciding whether a store is needed from the record's CONTENT is what left
        # that gap -- ordering is a property of the record stream, not of the payload -- so the
        # store is resolved once, for both outcomes, before either is taken.
        cards = make_store()
    if not isinstance(card, dict):
        # NO CARD MEANS EVICT, never "leave things as they are". This record is the
        # authoritative answer for the slot, so a record without a board says the board is
        # gone -- the crew republished to another template, or its work fold holds no board
        # one. Returning early kept the PREVIOUS card in the store and the card route went on
        # serving it as `status: published`, which is a stale board presented as current: the
        # one state a status surface must never reach, because a reader cannot tell it from a
        # live one.
        if cards is not None:
            # ORDERED, like a publication: this removal is a statement about ONE record, so a
            # delayed read that snapshotted a no-board record must not drop a card some later
            # publish stored. Unordered, the removal path took any arrival and inverted the
            # order the write path had just been taught to keep.
            cards.retire_derived(
                slot_key,
                str((record or {}).get("published_at") or ""),
                authoritative=authoritative,
            )
        return
    if cards is None:
        # No store exists and none can be built: ``ensure_dynamic_card_store`` is the
        # constructor, and the branch above takes it whenever this state has one. Reaching here
        # means it does not -- an older state object, or a test double -- which is a state with
        # no card surface at all, so nothing can be served from it either. Compatibility
        # fallback, and the card waits until a store exists.
        return
    # LIVE by the top guard: ``_slots`` was checked the moment this function began and nothing
    # between there and here awaits, so the slot cannot have been closed in the gap -- the
    # lookup only re-fetches the object the store write needs, it is not a second existence test.
    slot = state._slots[slot_key]
    try:
        # The record's own publish stamp travels with the card so the store can ORDER two writes
        # built from different records. This runs after a worker hop, so a panel read that
        # snapshotted an older record can arrive AFTER a publish stored a newer card, and with
        # no stamp the store would simply take the late arrival and serve the older board.
        cards.publish_derived(
            slot,
            card,
            str((record or {}).get("published_at") or ""),
            authoritative=authoritative,
        )
    except Exception:
        logger.warning(
            "could not store the derived board card for slot %s", slot_key, exc_info=True
        )


@lru_cache(maxsize=1)
def _card_page() -> str:
    """The card page's markup, read once per process.

    CACHED because it is shipped package data that cannot change under a running gateway,
    and this runs on every drawer read. Unlike the crew webview templates there is no
    operator override directory for a card page, so there is nothing a cache could hide:
    the one thing an override would need -- a re-read -- is the thing that does not exist
    here.

    A page missing from the build raises ``OSError``, which :func:`_with_board_numbers`
    catches so the record comes back with no card. Deliberately NOT left to the route: that
    catches only ``MemberSlugError`` and ``ValueError``, so an escaping ``OSError`` would be
    a 500 on a drawer read -- a packaging fault taking out the panel along with the card.
    """
    return pipeline_board_contract.card_template_path().read_text(encoding="utf-8")


def _is_work_board(view: Any) -> bool:
    """Whether *view* is a board that EXISTS, as opposed to an unbound empty fold.

    ``entries``, not the item count: a conductor that recorded its goal and nothing
    else has a real board with no items yet, and that board's zeros are true. A fold
    over a slot that never carried a work entry has none, and its zeros are not.
    """
    if not isinstance(view, dict):
        return False
    conductor = view.get("conductor")
    if not isinstance(conductor, dict):
        return False
    try:
        return int(conductor.get("entries") or 0) > 0
    except (TypeError, ValueError):
        return False


def _published_record(slot: str, slug: str, owner_key: str) -> dict[str, Any] | None:
    """The crew's panel record: the folded one, else the stored file.

    THE FILE DECIDES THE PANEL when this owner has one, and the fold supplies the
    history. The file cannot be staler: the publish route writes it BEFORE it
    appends, and returns without appending if that write fails, so every publish is
    in the file while only the ones whose append landed are in the fold. That write
    order is the invariant this selection rests on -- a future writer that appends
    without writing the file would break it, and this comment is the contract it
    would be breaking.

    The fold is read for *slot* -- the member's own DM slot, which is the only slot a
    publish can append under -- through the ordinary slot-keyed projection, so it is
    served by the same warm kernel every other slot fold uses and this route keeps no
    cache of its own. It answers alone when the file has nothing to say: a crew whose
    file was never written, or was removed, or cannot be parsed.

    *owner_key* selects WHICH record, because one slot can carry two crews: the slot
    is the member slug's, and a crew whose persisted ``member_id`` is another crew's
    name-derived slug lands on the same one. The fold keeps a record per ownership
    digest, so each crew is answered with its own rather than with whichever of them
    published last -- which the single file cannot do. The file is checked against the
    same digest before it is used at all, since it is keyed by slug alone, so a
    collision cannot let one crew's publish displace the other's reading.

    The file is also what makes a panel outlive its session. It answers for a crew
    that published with the crew log off, for one that published before this entry
    type existed, and for one whose session unit has since been collected by
    retention -- a fold-only read would blank a webview that is still on disk.
    Nothing here writes it: this is a GET, and the store owns that write.

    An empty ``template`` is how the fold says "nothing published": the store refuses
    a publish naming no template, so no real record has one.
    """
    try:
        folded = projection.read_slot_projection(slot, PANEL_FOLD_NAME).value
    except Exception:
        # A damaged or unreadable log reads as "nothing folded" rather than as a 500,
        # matching the store's own totality contract: this route renders somebody's
        # drawer, and the fallback below may still have a panel to show.
        #
        # WARNING, not debug: the crew-visible symptom is a panel that went blank
        # with nothing saying why, and a log that stays damaged produces it on every
        # read. The operator is the only party who can repair it, so the trace has to
        # be at a level they will actually see.
        logger.warning("panel fold unreadable for slot %s", slot, exc_info=True)
        folded = {}
    owners = folded.get("owners") if isinstance(folded, dict) else None
    mine = owners.get(owner_key) if isinstance(owners, dict) else None
    if not (isinstance(mine, dict) and str(mine.get("template") or "")):
        return agent_panel.read(slug)

    stored = agent_panel.read(slug)
    # The file is keyed by SLUG alone, so on a slug two crews resolve to it may hold
    # the other crew's panel. Only this owner's own file may be used, or a collision
    # would let one crew's publish displace the other's reading.
    if stored is None or str(stored.get("crew_key") or "") != owner_key:
        return mine

    # THE FILE DECIDES THE PANEL, because it cannot be staler than the fold: the
    # publish route writes it BEFORE it appends, and returns without appending if
    # that write fails. So every publish is in the file, while only the ones whose
    # append landed are in the fold -- the crew log may be off for a cycle, or the
    # entry may exceed the log's whole-LINE ceiling while its data is under the
    # store's own cap.
    #
    # Preferring the fold instead pinned the drawer to the last LOGGED cycle and kept
    # serving it while the route answered the crew ok, which is the one failure a
    # published panel must not have: a viewer cannot tell a stale dashboard from a
    # current one. Comparing the two ``published_at`` stamps does not fix it either,
    # because both are second-granularity and two publishes in one second tie.
    #
    # The history is the fold's alone, and those rows stay true of the cycles that
    # were logged, so they ride along rather than being lost with it. They count
    # LOGGED publishes, which is what the fold can see.
    newest = dict(stored)
    for carried in ("history", "publishes", "history_omitted"):
        if carried in mine:
            newest[carried] = mine[carried]
    return newest


def _owned_by(record: Mapping[str, Any], owner_key: str) -> bool:
    """Whether *record* belongs to *owner_key*. An unowned record belongs to nobody."""
    return bool(owner_key) and str(record.get("crew_key") or "") == owner_key


def owned_panel_record(slot: str, slug: str, owner_key: str) -> dict[str, Any] | None:
    """*owner_key*'s own published record, by the read route's rule, or ``None``.

    :func:`_published_record` picks the record, the file first when it is this
    owner's, and :func:`_owned_by` is the ownership check the read route makes on
    it. The dashboard page reads through here, so it shows what the route shows.
    """
    if not owner_key:
        return None
    record = _published_record(slot, slug, owner_key)
    if record is None or not _owned_by(record, owner_key):
        return None
    return record


def _read_and_compose(
    slot: str,
    slug: str,
    owner_key: str,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """One record read, and the document composed from that same record.

    Both halves of the response come from a single snapshot, so a publish landing
    mid-request cannot pair one version's HTML with another version's summary.
    Runs in a worker thread: the slot fold (or the fallback file read) plus the
    template read plus the compose. The final flag distinguishes composition failure
    from an absent record without exposing an unowned record before ownership is
    checked.
    """
    record = _panel_record(slot, slug, owner_key)
    try:
        return record, agent_panel.render_record(record), False
    except (agent_panel.PanelError, TypeError, ValueError):
        return record, None, True


# The keys a panel record carries by the time the drawer serializes it, split into
# the two disjoint sets the read makes of each: SERVED reaches the client, WITHHELD
# is for the server's own use and stays server-side. Most come from
# ``projection._panel_owner_record``; the last two are added by this route's own
# provider step. Every key the record carries is in exactly one of these, and
# ``_panel_meta`` reddens on a key in neither, so a field added at either layer and
# classified in neither set fails loud -- ``test_the_drawer_serializer_classifies_
# every_record_key`` for the fold's keys, ``test_the_drawer_serializer_accepts_every_
# key_the_provider_adds`` for this route's -- rather than being silently served or
# silently dropped. An allow-list keyed on the record's OWN keys is what keeps the
# record's shape and the drawer's shape from diverging silently.
_PANEL_SERVED_KEYS = frozenset(
    {
        "template",
        "title",
        "crew",
        "data",
        "published_at",
        # The fold computes and bounds these on every publish, so serving them costs
        # nothing and gives the drawer the crew's publish history the server keeps.
        "history",
        "publishes",
        "history_omitted",
    }
)


# WITHHELD, each for its own reason:
#  * ``crew_key`` is a digest of the exact crew name, which may itself be
#    credential-shaped; a sibling route test pins that it never reaches a client.
#  * ``schema`` is the record's internal version tag, meaningful only to the fold.
#  * ``board`` is the log-derived pipeline board ``_with_board_numbers`` puts on a
#    contract template's record; the composer renders it into the document's data
#    island, and the docked card walks ``data``, so no client reads it off the meta.
#  * ``contract_replaced`` is the mark left on the record when a free-shape payload
#    was overridden by the log's numbers; it has no renderer, so the operator's
#    warning is what reaches a person, not a response field nothing reads.
#  * ``card`` is the dynamic dashboard card ``_with_board_numbers`` builds from the same
#    board. It goes to the CARD STORE, which the dashboard reads on its own route, so
#    serving it here too would publish one board through two contracts and let a viewer
#    see the two disagree.
_PANEL_WITHHELD_KEYS = frozenset(
    {"crew_key", "schema", "board", "card", "contract_replaced", "board_unreadable"}
)


def _panel_meta(record: Mapping[str, Any]) -> dict[str, Any]:
    """The drawer's metadata, as an allow-list over the record's own keys.

    Iterates the keys the record carries and serves exactly those in
    ``_PANEL_SERVED_KEYS``, so a key the fold does not produce is simply not served
    and a served key tracks the field rather than a hand-listed name. A key in
    NEITHER set is a programming error -- a field on ``_panel_owner_record`` whose
    drawer stance no one has decided -- and raises rather than defaulting either way,
    which keeps a computed-but-unserved field from slipping through.

    ``data`` is coerced to a dict because the store refuses a record whose data is
    not an object, so this handles only the rejected case, not a shape the store
    allows.
    """
    served: dict[str, Any] = {}
    for key in record:
        if key in _PANEL_WITHHELD_KEYS:
            continue
        if key not in _PANEL_SERVED_KEYS:
            raise KeyError(
                f"panel record key {key!r} is classified neither served nor withheld; "
                "add it to _PANEL_SERVED_KEYS or _PANEL_WITHHELD_KEYS in agent_panel.py"
            )
        served[key] = record[key]
    # Coerced to the exact shapes the drawer's client type promises, so the served
    # payload does not depend on which record form (fold, file, or the merge of the
    # two) reached us. The four text fields carry the ``str(... or "")``
    # normalisation; ``publishes``/``history_omitted`` are counts and ``history`` is
    # a list of ``{at,title,template}`` rows.
    out: dict[str, Any] = {
        "template": str(served.get("template") or ""),
        "title": str(served.get("title") or ""),
        "crew": str(served.get("crew") or ""),
        "published_at": str(served.get("published_at") or ""),
        # ``read`` already refuses a record whose data is not an object, so this is a
        # dict or the record was rejected; the guard is for the rejected case rather
        # than for a shape the store allows.
        "data": served["data"] if isinstance(served.get("data"), dict) else {},
    }
    # A raw legacy FILE record carries none of these three, so they are served only
    # when the record has them: a file-only panel reads without empty history keys.
    if "history" in served:
        rows = served["history"]
        out["history"] = [dict(r) for r in rows] if isinstance(rows, list) else []
    if "publishes" in served:
        out["publishes"] = projection._as_int(served["publishes"])
    if "history_omitted" in served:
        out["history_omitted"] = projection._as_int(served["history_omitted"])
    return out


async def api_member_panel(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/panel — the crew's composed webview document.

    Returned as a JSON string rather than a ``text/html`` body: the drawer feeds
    it to the same srcdoc builder the artifact frames use, which adds the strict
    CSP and the theme variables. Serving it as HTML here would invite loading it
    directly, outside the sandbox that makes it safe to render at all.

    The raw ``data`` object travels alongside the document, and the drawer's
    DOCKED summary is rendered from it natively rather than from the document.
    That is the whole reason it is here: a dashboard needs a full page to be
    legible (four tiles across, multi-column grids), and the panel hosting the
    docked view starts at 320px wide, which cannot host one without pushing the
    crew's most important line below the fold. Reading the summary from the data
    lets the panel show the few fields that matter, in the order the crew
    published them, as ordinary escaped text.

    Duplicating the data (it is also inside the document's island) is deliberate
    and bounded: ``publish`` caps it, and the alternative -- parsing it back out
    of the composed HTML -- would make the drawer a consumer of the template's
    markup. Insertion order survives because ``json.dumps`` does not sort keys
    and ``JSON.parse`` preserves the order of non-numeric keys.

    An app token scoped to ``/api/members`` reaches this route by PREFIX -- it is a
    child of that parent -- so app callers are denied explicitly. Apps are isolated
    from member surfaces generally (``handlers/members.py`` denies its three routes
    the same way); a panel is a crew's own published state and a rendered document,
    which is squarely inside what that isolation exists to withhold.
    """
    # ``await``: this guard is a coroutine (it offloads its SEL audit off the
    # event loop). Calling it without awaiting returns a truthy coroutine that
    # never runs, so the deny path silently stops denying -- the rebase that
    # made it async produced no conflict here, only a dead guard.
    denied = await _deny_app_caller(request, "members.panel")
    if denied is not None:
        return denied
    # The READ is served to any dashboard caller: a non-owner dashboard subject may read
    # a crew's published record, and gating the whole route would turn that read into a
    # 403. Only the derived-card WRITE below is owner-gated, because that write mutates
    # the owner-facing dynamic-card store; see its guard.
    slug = request.match_info.get("slug", "")
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    # ``member`` (query, REQUIRED) is the exact crew name, exactly as
    # ``api_member_activity`` requires it and for the same reason: slugification is
    # lossy, so ``Oncall`` and ``oncall`` reach one slug and therefore one slot. The
    # fold keeps a record per ownership digest, so both crews' panels survive there
    # -- but a read keyed on the slug alone still hands whichever published last to
    # both of them. Verifying the stored ownership claim here is what picks the
    # asking crew's own record, and making the parameter required makes the mixed
    # read impossible by construction rather than a caller obligation.
    member = request.query.get("member", "")
    if not is_readable_member_name(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )
    # ONE read, both halves. `render` + `read` as separate calls let a publish land
    # between them and returned the old document beside the new summary, so the
    # docked chip and the expanded view could disagree. Composed inside the same
    # worker hop, which also keeps the template resolution off the event loop.
    #
    # The SLOT comes from the crew name plus the slug, through the same derivation
    # the members page uses for the DM thread: the fold lives on that slot, because
    # that is the only session a publish can append from. Derived rather than looked
    # up so a member whose thread is not running still reads its last panel.

    # Derived from the crew name alone, so it is known BEFORE the read and can select
    # which of a shared slot's records to fold out. The post-read comparison below is
    # the same value re-checked against what was actually read: it is what guards the
    # legacy file, which is keyed on the slug only and therefore cannot be selected.
    mine = agent_panel.crew_key(member)

    def _resolve_and_read() -> tuple[str, dict[str, Any] | None, str | None, bool]:
        cfg = KiroCrewConfig.load()
        # The slot key comes BACK from the worker hop, because the card store is keyed on
        # it and resolving it a second time out here would load the config twice and could
        # answer differently if it changed between the two reads.
        #
        # TWO keys, deliberately. The fold is read off the panel slot, which is shared by
        # every crew whose name reaches this slug; the card is stored only on a slot that
        # names one crew, so the key returned for the store is empty when this one is
        # shared. Both come from the same load, so they cannot disagree about the config.
        slot = _panel_slot(cfg, member, slug)
        return (_card_slot(cfg, member, slug), *_read_and_compose(slot, slug, mine))

    try:
        slot_key, record, html, render_failed = await asyncio.to_thread(_resolve_and_read)
    except (MemberSlugError, ValueError):
        # The crew name has no addressable member space, so it has no DM slot and
        # therefore no panel. Reported as the empty state rather than a refusal: from
        # this caller's point of view there is nothing published, and the slug it
        # asked about is not evidence of anything else.
        return web.json_response({"panel": None, "html": None})
    # Compared on the DIGEST of the exact name. The stored ``crew`` is the REDACTED
    # display text, so a credential-shaped crew name would never equal the exact
    # name it was redacted from -- that crew could not read its own panel, and two
    # different such crews would look like the same owner.
    #
    # An UNOWNED record is refused, not served. Treating an empty ``crew_key`` as
    # "nothing to compare" would be a fail-OPEN default on the one guard that keeps
    # a crew's drawer its own, and nothing legitimate produces such a record: this
    # schema ships with the ownership field, the publish route refuses a session
    # with no crew binding (``no_crew``), and :func:`publish` rejects an empty crew
    # outright. A forgery is the only thing left that can write one, which is
    # exactly what must not render.
    if record is not None and not _owned_by(record, mine):
        # Another crew owns this slug's record. Reported as "nothing published"
        # rather than as a refusal: from this crew's point of view it HAS no panel,
        # and naming the other crew would disclose a colliding name the viewer of
        # this drawer has no other way to learn.
        return web.json_response({"panel": None, "html": None})
    # AFTER the ownership refusal above, never beside the read.
    #
    # ``_published_record`` answers with ``agent_panel.read(slug)`` unfiltered when the
    # fold holds nothing for this owner, and that file is keyed on the SLUG alone -- so a
    # colliding crew's record, or an unowned one, reaches this far. ``_panel_record``
    # gates only on the template id, so such a record still builds a card out of its
    # ``lede``, its per-item action sentences and its crew name. Stored before the check
    # above, that card is then served by the owner-only dashboard-card route, and the text
    # THIS route just refused to disclose is read off the other surface instead. The
    # refusal is what decides whether this record may be seen at all, so nothing derived
    # from it may leave the request before it.
    #
    # Deliberately BEFORE the render/compose branches below: a card is built from the fold
    # and the record, not from the drawer template, so a crew whose template is broken
    # still has a board worth showing.
    #
    # OWNER-GATED, and only here. The read above is open to any dashboard caller (the crew
    # webview depends on it), but this write mutates the owner-facing dynamic-card store and
    # broadcasts to owners -- the member-subject class the sibling routes guard. So a
    # non-owner `!dashboard` subject still READS its panel but triggers no store write: the
    # narrow gate the design review asked for, closing the non-owner-write hole without the
    # 403 a route-level gate put on the webview.
    if is_owner_dashboard_request(request):
        _publish_derived_card(request.app["state"], slot_key, record, mine)
    if render_failed:
        # A published panel that cannot be composed is temporarily unavailable,
        # matching the write path's 503 and giving the drawer a retryable error.
        return web.json_response(
            {"error": "could not render the panel", "code": "panel_render_failed"},
            status=503,
        )
    if html is None:
        # Only a record the store treats as absent reaches this empty state. A
        # readable published record with a broken template returns the error above.
        return web.json_response({"panel": None, "html": None})
    try:
        panel = _panel_meta(record) if record is not None else None
    except KeyError:
        # A record carrying a key the serializer classifies as neither served nor
        # withheld is a programming error the test suite is meant to catch, but a
        # live drawer must not 500 on somebody's panel: log it for the operator and
        # show the empty state, the same failure mode every other read defect here
        # degrades to.
        logger.warning("panel record has an unclassified key for slug %s", slug, exc_info=True)
        return web.json_response({"panel": None, "html": None})
    if panel is not None:
        # The template's own opt-in to render in the docked card, read from the
        # document served beside it. It is derived from ``html`` at response time,
        # not a record field, so it rides on the served panel rather than through
        # the record-keyed allow-list. ``None`` keeps the native summary, which
        # costs the drawer no mint.
        panel["docked_height"] = agent_panel.docked_height(html)
    return web.json_response(
        {
            "panel": panel,
            "html": html,
        }
    )
