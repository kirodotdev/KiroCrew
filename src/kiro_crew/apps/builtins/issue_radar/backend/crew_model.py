"""Which model a crew's next turn runs on: its triage model or its work model.

A crew record carries one optional ``triage_model``. Unset, nothing here touches
the crew's session and every turn runs on the crew's ``model``, exactly as a crew
with no triage model always has.

Set, the choice is made from the LEDGER and from nothing else. A crew none of whose
open work items sits in an acting phase (:data:`ACTING_PHASES`) is only reading its
queue and checking whether anything moved, and that turn runs on the triage model.
Once any open item is in an acting phase the crew is doing the work, and every turn
runs on its work model until no item is acting again. The crew moves an item into
an acting phase itself, through the ledger write, so the cheap turn's one decision
-- does anything here need action? -- is what escalates to the work model.

THE SWITCH LANDS BETWEEN TURNS, never inside one. :func:`apply_turn_model` writes
``slot.model`` and arms the slot's deferred session reset, which the next turn
consumes before it acquires a session. The deferral checks and the reset arming
are ``session_control``'s own (``model_switch_deferred``,
``arm_model_switch_reset``), the ones a queued ``session_set_model`` pick goes
through, so the conversation resumes on the new model rather than starting over.
A running turn keeps the model it started on.

A PERSON'S PICK WINS. The crew changes only a model it chose itself. Once someone
picks a model in the crew's session -- the model picker, or a ``session_set_model``
pick -- that pick holds: the crew stops switching the session's model, the switch
back to the work model included, until an edit changes the crew's ``model`` or
``triage_model``, or the person resumes switching from the crew page (a patch of
``model_held: false``). The two are told apart by the slot's pick generation, which
every deliberate pick bumps (:data:`_written`), and the hold is kept on the crew
record (``model_held``) so a restart keeps it too. The crew page names the held
model, so the hold is never silent.

AN UNUSABLE TRIAGE MODEL FALLS BACK, VISIBLY. The record's id is shape-checked on
write; whether the account can run it is answered by the session that starts on
it, which records the backend's verdict on the slot (``slot.model_withheld``). A
withheld triage model is remembered per crew, every later turn runs on the work
model, and :func:`unavailable_triage_model` names it for the crew page. Changing the
setting clears the verdict, because the verdict is about one id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from kiro_crew.dashboard.session_control import arm_model_switch_reset, model_switch_deferred

from . import crew_store

logger = logging.getLogger("kirocrew.app.issue-radar")

#: The phases in which the crew is the one doing the work: an item it has chosen
#: and not yet parked, from ``selected`` through ``addressing-review``. Built from
#: the store's own sets so a phase added to either is acting here too. The waiting
#: phases (``awaiting-ci``, ``awaiting-merge``, ``awaiting-reply``) are not in it:
#: a crew holding only those is checking whether something moved, which is the
#: triage model's job.
ACTING_PHASES: frozenset[str] = frozenset(
    {"selected"} | set(crew_store.TTL_ACTIVE_PHASES) | set(crew_store.EDITING_PHASES)
)

#: crew id -> the triage model id a session withheld for that crew. Process
#: memory: a restart forgets it and the next session that starts on the id
#: records the verdict again.
_withheld: dict[str, str] = {}

#: slot key -> (the model this module last pointed the slot at, the slot's pick
#: generation right after that write). The generation is what tells this module's
#: own write apart from a person's: the model picker, the bulk picker and a
#: committed ``session_set_model`` pick all bump ``_model_pick_gen``, and the
#: runtime's own rewrites of a pin (filling an agent-default pin with the served
#: id, folding an alias) do not. The one runtime write that does bump it, the
#: provider switch clearing every slot's model, stamps the generation it produced
#: (``_model_runtime_pick_gen``) and is read as no pick. So a moved generation means someone chose a model
#: since, and an unchanged one means the slot still holds this module's choice,
#: however the runtime spelled it. The third member is the slot OBJECT's identity:
#: a slot closed and resumed, or archived and rehydrated, is a new object whose
#: generation starts again at 0, and that reset is nobody's pick.
_written: dict[str, tuple[str, int | None, int]] = {}

#: crew id -> the crew's (work model, triage model) when a person's pick was seen
#: on its slot. Process memory that covers the time until the crew record carries
#: ``model_held`` (:func:`hold_to_record`). Keyed to the settings, and dropped by
#: :func:`note_triage_change` when an edit changes either one, so an edit releases
#: it here exactly as :func:`crew_store.update_crew` releases it on the record, and
#: by an explicit ``model_held: false`` patch whether or not the record had it yet.
#: The crew page shows only a RECORDED hold, so it never offers to release one a
#: restart would forget.
_held: dict[str, tuple[str, str]] = {}

#: crew id -> the crew's hold release count (:func:`crew_store.hold_releases`) when
#: the hold in :data:`_held` was seen. Set and dropped with it. The record write
#: carries it, so a release that lands while the write is queued wins.
_hold_seen_at: dict[str, int] = {}


def triage_model(crew: Mapping[str, Any]) -> str:
    """The crew's configured triage model id, ``""`` when it has none."""
    value = crew.get("triage_model")
    return value.strip() if isinstance(value, str) else ""


def work_model(crew: Mapping[str, Any]) -> str:
    """The crew's work model; ``""`` is the agent's own default."""
    value = crew.get("model")
    return value if isinstance(value, str) else ""


def has_acting_item(items: Iterable[Mapping[str, Any]]) -> bool:
    """Whether any open work item sits in an acting phase."""
    return any(str(item.get("phase") or "") in ACTING_PHASES for item in items)


def is_managed(slot: Any) -> bool:
    """Whether this module last chose *slot*'s model, so switching off has work to do."""
    return str(getattr(slot, "key", "") or "") in _written


def _pick_gen(slot: Any) -> int | None:
    """The slot's pick generation, ``None`` when it carries no integer one."""
    gen = getattr(slot, "_model_pick_gen", None)
    return gen if isinstance(gen, int) and not isinstance(gen, bool) else None


def _runtime_wrote_since(slot: Any, slot_key: str) -> bool:
    """Whether the runtime itself, not a person, last moved *slot*'s pick generation.

    The gateway's provider switch clears every slot's model and bumps the
    generation, for the model-fallback probe's sake, and stamps the generation it
    produced on ``_model_runtime_pick_gen``. Nobody picked that model, so it must
    not read as a hold: the slot is taken on as it stands, the way a rebuilt slot
    is.
    """
    _model, gen, ident = _written[slot_key]
    now = _pick_gen(slot)
    stamp = getattr(slot, "_model_runtime_pick_gen", None)
    return ident == id(slot) and now is not None and now != gen and now == stamp


def _picked_since(slot: Any, slot_key: str) -> bool:
    """Whether a person chose *slot*'s model since this module last wrote it.

    By pick generation (see :data:`_written`). A generation the runtime stamped as
    its own write (:func:`_runtime_wrote_since`) is no pick. A slot with no
    generation to read, or a rebuilt slot object whose generation restarted, is
    judged by its model alone.
    """
    model, gen, ident = _written[slot_key]
    now = _pick_gen(slot)
    if ident == id(slot) and gen is not None and now is not None:
        return now != gen and not _runtime_wrote_since(slot, slot_key)
    return (getattr(slot, "model", "") or "") != model


def is_held(crew: Mapping[str, Any]) -> bool:
    """Whether a person's pick holds the crew session's model, so the crew does not switch it.

    Held from the moment the pick is seen until an edit changes the crew's
    ``model`` or ``triage_model``: on the record once :func:`hold_to_record` has
    written it, and in process memory until then.
    """
    if crew.get("model_held") is True:
        return True
    return _held.get(str(crew.get("id") or "")) == crew_store.model_settings(crew)


def hold_to_record(crew: Mapping[str, Any]) -> bool:
    """Whether a hold seen in this process is not on the crew record yet.

    The caller writes it with :func:`crew_store.set_crew_model_held`, off the loop,
    so a gateway restart keeps the person's pick.
    """
    return crew.get("model_held") is not True and is_held(crew)


def hold_seen_at(crew: Mapping[str, Any]) -> int | None:
    """The release count the crew's in-process hold was seen under, if one was seen.

    What :func:`crew_store.set_crew_model_held` compares, so the write lands only if
    no release came after the pick was seen.
    """
    return _hold_seen_at.get(str(crew.get("id") or ""))


def triage_in_effect(crew: Mapping[str, Any]) -> bool:
    """Whether the crew switches to its triage model at all right now.

    False with no triage model, one equal to the work model, one the account
    cannot run, or a person's pick holding the session: in each, every turn runs
    on the model the session already has, so nothing tells a turn to stop short.
    """
    triage = triage_model(crew)
    return (
        bool(triage)
        and triage != work_model(crew)
        and not unavailable_triage_model(crew)
        and not is_held(crew)
    )


def unavailable_triage_model(crew: Mapping[str, Any]) -> str:
    """The configured triage model when a session withheld it, else ``""``."""
    triage = triage_model(crew)
    if not triage:
        return ""
    return triage if _withheld.get(str(crew.get("id") or "")) == triage else ""


def turn_model(crew: Mapping[str, Any], items: Iterable[Mapping[str, Any]]) -> str:
    """The model the crew's next turn runs on.

    The work model unless a triage model is set, usable, and no open item is in an
    acting phase.
    """
    work = work_model(crew)
    triage = triage_model(crew)
    if not triage or triage == work:
        return work
    if unavailable_triage_model(crew) or has_acting_item(items):
        return work
    return triage


def _note_session_verdict(crew: Mapping[str, Any], slot: Any) -> None:
    """Read the session's verdict on the triage model off the slot, if it has one."""
    triage = triage_model(crew)
    crew_id = str(crew.get("id") or "")
    if not triage or (getattr(slot, "model", "") or "") != triage:
        return
    verdict = getattr(slot, "model_withheld", None)
    if verdict is True and _withheld.get(crew_id) != triage:
        _withheld[crew_id] = triage
        logger.warning(
            "issue-radar crew %s: triage model %s is not available to this account; "
            "running every turn on the work model",
            crew_id,
            triage,
        )
    elif verdict is False:
        _withheld.pop(crew_id, None)


def apply_turn_model(
    state: Any,
    slot: Any,
    crew: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
) -> bool:
    """Point *slot* at the model its next turn should run on. Returns whether it moved.

    SYNCHRONOUS on purpose: the running check and the writes happen with no
    suspension between them, so a turn cannot start in the middle. A running slot
    is left alone, and so is every case ``model_switch_deferred`` names -- the
    deferrals a queued ``session_set_model`` pick takes, through the same helper,
    as the reset is armed through the same ``arm_model_switch_reset``. The next
    call decides again.

    A PERSON'S PICK WINS. This module changes only a slot whose model it chose
    itself: once someone picks a model in the crew's session (the model picker, or
    a ``session_set_model`` pick) the slot is :func:`is_held` and left alone, the
    switch back to the work model included, until an edit changes the crew's
    ``model`` or ``triage_model``. A slot this process has not written yet (a new
    crew, a restart) is taken on as it stands unless the record says it is held.
    """
    if slot is None or getattr(slot, "running", False):
        return False
    if model_switch_deferred(state, slot):
        return False
    if getattr(slot, "_pending_model_pick", None) is not None:
        # A ``session_set_model`` pick is queued for the next turn start. It is
        # someone's choice, and a switch here would bump the pick generation it was
        # queued against, so the commit would drop it as overtaken. Let it commit;
        # the pass after sees it as a pick and holds it.
        return False
    slot_key = str(getattr(slot, "key", "") or "")
    current = getattr(slot, "model", "") or ""
    written = _written.get(slot_key)
    if triage_model(crew):
        if is_held(crew):
            return False
        if written is not None and written[2] != id(slot):
            # The slot was rebuilt (closed and resumed, or archived and
            # rehydrated). Its generation restarted at 0, which is no pick, so
            # take it on as it stands rather than reading the reset as one.
            del _written[slot_key]
            written = None
        if written is not None and _runtime_wrote_since(slot, slot_key):
            # The provider switch cleared the slot's model. Its own write, not a
            # pick, and not this module's choice any more either: compare with
            # the model the slot holds now, so the crew's model goes back on.
            del _written[slot_key]
            written = None
        if written is not None and _picked_since(slot, slot_key):
            del _written[slot_key]
            crew_id = str(crew.get("id") or "")
            _held[crew_id] = crew_store.model_settings(crew)
            _hold_seen_at[crew_id] = crew_store.hold_releases(crew_id)
            logger.info(
                "issue-radar crew %s: model %s picked by hand; not switching it until "
                "the crew's model settings change",
                crew.get("id"),
                current or "(agent default)",
            )
            return False
        _note_session_verdict(crew, slot)
        target = turn_model(crew, items)
        # Compared with what this module wrote rather than with ``slot.model``: the
        # runtime may have respelled the pin since (an agent-default pin filled with
        # the served id), and that is still this module's choice.
        if (written[0] if written is not None else current) == target:
            _written[slot_key] = (target, _pick_gen(slot), id(slot))
            return False
    elif written is not None:
        # Switched off. Put the slot back on the work model, but only if it still
        # holds the model this module put there: a model someone picked since is
        # theirs, and a crew this module never managed is not in the map at all.
        picked = _picked_since(slot, slot_key)
        del _written[slot_key]
        target = work_model(crew)
        if picked or written[0] == target or current == target:
            return False
    else:
        return False
    slot.model = target
    if isinstance(getattr(slot, "_model_pick_gen", None), int):
        # Counted as a deliberate pick so the model-fallback restore does not
        # undo it. :data:`_written` records the generation this bump produced, so
        # a person's later pick still reads as theirs.
        slot._model_pick_gen += 1
    if triage_model(crew):
        _written[slot_key] = (target, _pick_gen(slot), id(slot))
    arm_model_switch_reset(state, slot)
    slot._dirty = True
    logger.info(
        "issue-radar crew %s: next turn runs on %s",
        crew.get("id"),
        target or "the agent default",
    )
    return True


def note_triage_change(
    slot: Any,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    released: bool = False,
) -> None:
    """Bring this module's memory in line with a crew edit.

    A changed triage model clears the crew's withheld verdict, because the verdict
    is about one id: switching the setting off and back on is how a person asks the
    crew to try a model again. A changed ``model`` or ``triage_model`` releases a
    hand-picked model hold, as :func:`crew_store.update_crew` does on the record,
    and so does *released*: the edit carried an explicit ``model_held: false``
    ("Switch back to triage model"), which releases a hold this process holds
    whether or not the record had it yet.

    A gateway restart empties :data:`_written`, so a triage model switched off
    before this process ever applied one would otherwise stay on the slot. Marking
    it here lets the next :func:`apply_turn_model` put the work model back -- unless
    a person's pick holds the slot, which only that person's pick put there.
    """
    crew_id = str(after.get("id") or before.get("id") or "")
    old = triage_model(before)
    if old != triage_model(after):
        _withheld.pop(crew_id, None)
    held_before = is_held(before)
    if (
        released
        or crew_store.model_settings(before) != crew_store.model_settings(after)
        or (before.get("model_held") is True and after.get("model_held") is not True)
    ):
        # Released: by a change to either model, or by "Switch back to triage
        # model" on the crew page. The next pass takes the slot on as it stands
        # and switches it again.
        _held.pop(crew_id, None)
        _hold_seen_at.pop(crew_id, None)
    if not old or triage_model(after) or slot is None:
        return
    slot_key = str(getattr(slot, "key", "") or "")
    if slot_key in _written or held_before:
        return
    if (getattr(slot, "model", "") or "") == old:
        _written[slot_key] = (old, _pick_gen(slot), id(slot))
