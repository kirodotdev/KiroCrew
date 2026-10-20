"""Make chats that keep a model of their own follow the default model again.

A chat whose ``slot.model`` is set runs that model on every turn and re-sends it
as ``set_model`` on every resume, so a change to the default never reaches it. An
empty ``slot.model`` means "inherit": the turn resolves the default the chat
inherits, and when the chat's live session was started on another model that
turn queues the same ``skip_if_busy`` reset a model pick queues. Clearing the
field is therefore the whole change; nothing here touches a live session. On
Auto every tier can defer to the backend and the turn's stamp check cannot tell
what the chat inherits, so the clear queues the reset a pick queues for a chat
whose live session started on a model other than the Default Model setting. An
open chat's clear is saved at once and confirmed. A failed save puts an unchanged
slot's model back and fails the confirm, so the next press counts the chat again.

A kept model cannot be told apart from a person's pick, so nothing clears it on
its own. Settings > Chat > Models asks: ``GET /api/chat/slots/follow-default``
counts the chats that keep a model and answers a ``digest`` of exactly that set,
and ``POST`` with that digest clears them. When the set has changed in between (a
new pick, a new chat), the ``POST`` changes nothing and answers 409 with the
fresh count, so the number a person agreed to is the number that changes. Each
chat is cleared only while it still keeps the model the count saw, so a pick made
since is never overwritten. The count also answers what the confirm names: the
saved Default Model (``""`` on Auto), the kept models with how many chats keep
each, and the titles of the first five counted chats, open ones first, each
redacted for display before it is capped. Titles and the Default Model stay out
of the digest, so a rename or a new default does not refuse the confirm.
Kept model names are redacted for display too, with counts grouped by display label.

Only chats a person started qualify (:func:`_line_model`, :func:`_slot_model`).
A chat ``session_create`` made keeps the model its dispatcher gave it (its line
names a ``created_by``), and a crewmate's own thread, app, cron, channel and
remote sessions are left alone. A slot kind a person does not start must set a
non-person origin or a ``created_by``: the origin check is the guard that keeps it
out. A chat that is not loaded is judged on its line;
a loaded chat on its slot and on its line, because a chat reopened from History
comes back without the ``created_by`` and ``app`` its line still names. A loaded
chat whose line cannot be read is not counted, and one with no transcript yet is
judged on its slot alone. A chat on a crewmate whose config pins a model is left
alone as well: that pin outranks the default, and the chat's next turn writes the
model it runs back into ``slot.model`` (:func:`_pinned_crewmates`). Config pins
are read, never written. A loaded chat a throttle or refusal fallback is serving
is not counted until its session is back on its own model, so the press never
leaves a chat on a fallback. A chat whose tab the boot restore has not rebuilt yet
(or could not read) is left for the next press, like one being opened. A line
written before ``created_by`` was recorded cannot be told from a person's chat and
counts as one.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, normalize_agent_model
from kiro_crew.dashboard.channel_slots import slot_close_instant
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import (
    _history_key_for,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.chat_utils_display import _redact_for_display
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.state import NEW_SESSION_TITLE, SlotOrigin
from kiro_crew.history_files import transcript_stem
from kiro_crew.llm_helpers import slot_switch_session_lock

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: Origins a person's chat carries. ``""`` is a line written before origins were
#: recorded; every other origin (app, cron, system) is not a person's.
_PERSON_ORIGINS = frozenset({SlotOrigin.USER, ""})

#: What the preview's digest looks like: a SHA-256 in lowercase hex.
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

_OPERATION = "chat.slots_follow_default"

#: What clearing one counted chat came to (:func:`_clear`).
CHANGED, SKIPPED, FAILED = "changed", "skipped", "failed"

#: No crewmate pins: the rule for a caller that has not read the config.
_NO_PINS: frozenset[str] = frozenset()


#: How many counted chats the count names by title, and the longest title it answers.
_SHOWN_TITLES = 5
_TITLE_CAP = 200


@dataclass(frozen=True)
class _Kept:
    """A chat that keeps a model: the model, and the transcript its line lives in."""

    model: str
    stem: str
    #: The crewmates whose config pinned a model when this chat was counted.
    pinned: frozenset[str] = _NO_PINS
    #: The chat's raw title, ``""`` when untitled. Shown, never part of the digest.
    title: str = ""


def _shown_close(meta: Mapping[str, Any]) -> object:
    """The close a metadata line shows: its ``closed_at`` when it is closed, else ``None``."""
    return meta.get("closed_at") if meta.get("closed") else None


def _archive_pending(close: float | None, shown: object) -> bool:
    """Whether the close recorded in this process still has its save on the way.

    A close save persists the instant ``note_slot_closed`` recorded as the line's
    ``closed_at``, so the save has landed exactly when the line shows that same
    value. Equality, not order: it holds on a coarse clock and across a clock step.
    Two closes of one tab stamped within one clock tick share a value, and this
    cannot tell them apart.
    """
    return close is not None and shown != close


def _pinned_crewmates() -> frozenset[str]:
    """The crewmates whose config pins a model of their own.

    A chat on one of them runs that pin when it keeps no model, and its turn then
    writes the model it runs back into ``slot.model``, so clearing it can never make
    it follow the default. Blocking (a config read): run off the loop.
    """
    agents = KiroCrewConfig.load().agents
    return frozenset(name for name, agent in agents.items() if normalize_agent_model(agent.model))


def _on_pinned_crewmate(agent: object, agent_kind: object, pinned: frozenset[str]) -> bool:
    """Whether a chat runs a crewmate that pins a model: the resolver's crewmate pick."""
    return isinstance(agent, str) and agent in pinned and agent_kind != "template"


def _persons_line(meta: Mapping[str, Any]) -> bool:
    """Whether a metadata line is a chat a person started; an empty line is."""
    if meta.get("created_by") or meta.get("app"):
        return False
    if str(meta.get("origin", "")) not in _PERSON_ORIGINS:
        return False
    if meta.get("mode") == "member":
        return False
    if meta.get("linked_session_key") or meta.get("channel_origin"):
        return False
    return meta.get("executor") != "remote"


def _line_model(meta: Mapping[str, Any], pinned: frozenset[str] = _NO_PINS) -> str:
    """The model a person-started chat's metadata line keeps, or ``""``."""
    model = meta.get("model")
    if not isinstance(model, str) or not model:
        return ""
    if not _persons_line(meta):
        return ""
    if _on_pinned_crewmate(meta.get("agent"), meta.get("agent_kind"), pinned):
        return ""
    return model


def _slot_model(slot: _ChatSlot, pinned: frozenset[str] = _NO_PINS) -> str:
    """The model a person-started live chat keeps, or ``""``; the line rule, on the slot.

    A chat a throttle or refusal fallback is serving keeps ``""`` too: a clear bumps
    the pick generation, and the fallback's restore then reads it as a pick, drops
    its record and leaves the session on the fallback.
    """
    model = slot.model
    if not isinstance(model, str) or not model:
        return ""
    if slot._active_fallback_model or slot._refusal_fallback_primary:
        return ""
    if slot._created_by or slot._app:
        return ""
    if slot._origin not in _PERSON_ORIGINS:
        return ""
    if slot.mode == "member":
        return ""
    if slot.linked_session_key or slot.channel_origin:
        return ""
    if slot.executor == "remote":
        return ""
    if _on_pinned_crewmate(slot.agent, slot.agent_kind, pinned):
        return ""
    return model


def _slot_title(slot: _ChatSlot) -> str:
    """An open chat's title as its tab shows it, ``""`` while it is untitled."""
    title = slot.display_title
    return "" if title == NEW_SESSION_TITLE else title


def _line_title(meta: Mapping[str, Any], stem: str) -> str:
    """A closed chat's title from its line, ``""`` when it has none or it is the key."""
    title = meta.get("title")
    if not isinstance(title, str) or not title:
        return ""
    if title in {stem, _history_key_for(stem), _history_key_for(stem).removeprefix("dashboard:")}:
        return ""
    return title


def _shown_title(title: str) -> str:
    """A title as the count answers it: redacted for display first, then capped."""
    return _redact_for_display(title)[:_TITLE_CAP] if title else ""


def _saved_default_model() -> str:
    """The Default Model setting, ``""`` on Auto. Blocking (a config read): run off the loop."""
    return normalize_agent_model(KiroCrewConfig.load().agent.model)


def _by_model(kept: Mapping[str, _Kept]) -> list[dict[str, Any]]:
    """How many counted chats share each display label, most first, then by label."""
    counts = Counter(_redact_for_display(entry.model) for entry in kept.values())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"model": model, "count": count} for model, count in ordered]


def _closed_lines(
    conversation_log: Any,
    loaded_stems: frozenset[str],
    judged: frozenset[str],
    pinned: frozenset[str],
) -> tuple[list[tuple[str, str, object, str]], set[str]]:
    """The lines a count reads: closed chats that keep a model, and loaded ones withheld.

    The first part is ``(stem, model, shown, title)`` for every dashboard transcript
    that is not loaded and whose line keeps a model, in ``list_sessions`` order;
    ``shown`` is the close the line shows (:func:`_shown_close`). The
    second is the stems in *judged* (the loaded chats whose slot keeps a model)
    whose line is not a person's or cannot be read; a loaded chat with no
    transcript yet reads as an empty line, a person's. Blocking (a directory scan
    and a first-line read per file): run off the loop.
    """
    withheld: set[str] = set()
    for stem in judged:
        meta, readable = conversation_log.get_metadata_status(stem)
        if not readable or not _persons_line(meta):
            withheld.add(stem)
    found: list[tuple[str, str, object, str]] = []
    for row in conversation_log.list_sessions():
        stem = str(row.get("key") or "")
        if not stem.startswith("dashboard_") or stem in loaded_stems:
            continue
        meta, readable = conversation_log.get_metadata_status(stem)
        if not readable:
            continue
        model = _line_model(meta, pinned)
        if model:
            found.append((stem, model, _shown_close(meta), _line_title(meta, stem)))
    return found, withheld


def _constructing(state: DashboardState, name: str) -> bool:
    return name in getattr(state, "_slots_under_construction", ())


async def _kept_models(state: DashboardState) -> dict[str, _Kept]:
    """Every chat that keeps a model, by slot name.

    A loaded chat is judged on its slot, whose next save writes the line, and on
    that line: a History resume restores neither ``created_by`` nor ``app`` to the
    slot, and the line still names them. ``created_by`` stays on the line through
    the slot's saves; ``app`` is slot-owned, so the first full save drops it and
    the chat is a plain one from then on. A chat that is not loaded is judged on
    its line, unless a close recorded in this process has not reached that line or
    ``state.slot_exists`` still reports it open.
    """
    pinned = await asyncio.to_thread(_pinned_crewmates)
    kept: dict[str, _Kept] = {}
    loaded_stems: set[str] = set()
    for name, slot in list(state._slots.items()):
        stem = transcript_stem(slot_history_key(slot))
        loaded_stems.add(stem)
        model = _slot_model(slot, pinned)
        if model and not _constructing(state, name):
            kept[name] = _Kept(model, stem, pinned, _slot_title(slot))
    conversation_log = getattr(state, "conversation_log", None)
    if conversation_log is None:
        return kept
    closed, withheld = await asyncio.to_thread(
        _closed_lines,
        conversation_log,
        frozenset(loaded_stems),
        frozenset(entry.stem for entry in kept.values()),
        pinned,
    )
    kept = {name: entry for name, entry in kept.items() if entry.stem not in withheld}
    for stem, model, shown, title in closed:
        name = _history_key_for(stem).removeprefix("dashboard:")
        if stem in loaded_stems or name in kept or name in state._slots:
            continue
        if state.slot_exists(name):
            continue
        if _archive_pending(slot_close_instant(state, name), shown):
            continue
        kept[name] = _Kept(model, stem, pinned, title)
    return kept


def _digest(kept: Mapping[str, _Kept]) -> str:
    """One value for the set: which chats, and the model each keeps."""
    h = hashlib.sha256()
    for name in sorted(kept):
        h.update(f"{name}\0{kept[name].model}\n".encode())
    return h.hexdigest()


async def _clear_loaded(
    state: DashboardState,
    name: str,
    slot: _ChatSlot,
    model: str,
    pinned: frozenset[str],
    default_model: str | None = None,
) -> str:
    """Clear and save a loaded chat: :data:`CHANGED`, :data:`SKIPPED` or :data:`FAILED`.

    Under the pick lock, so an explicit pick in flight finishes first and wins.
    A chat closed or replaced during the wait, or one a fallback started serving
    (:func:`_slot_model`), is left for the next press.
    The generation bump is an explicit pick's, so a fallback must not be serving.
    The session's switch lock comes first, in the switch handlers' order: an agent
    switch hides the fallback record while it holds that lock and can put it back
    on a refusal, so the check runs on settled state.
    A cleared pin or non-empty session stamp differing from *default_model*
    (the Default Model setting, ``""`` on Auto; ``None`` queues nothing) gets the
    reset a pick queues, run at the next turn. The pin covers a session switched
    in place, which leaves the allocation stamp unchanged.
    Save and confirm outside the locks as a guarded write, so a close waits for it
    or refuses it. A failed save restores an unchanged slot's model, so the confirm
    fails and the next press counts it again. A queued reset stays armed to restart
    on the restored model; another writer may also need it. A replacement or a
    later pick wins; other drift leaves the slot dirty for the flush to retry,
    without rollback.
    """
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import arm_model_switch_reset

    session_key = effective_session_key(slot)
    async with slot_switch_session_lock(session_key), slot._model_pick_lock:
        if state._slots.get(name) is not slot or effective_session_key(slot) != session_key:
            return SKIPPED
        if _slot_model(slot, pinned) != model:
            return SKIPPED
        history_key = slot_history_key(slot)
        old_jev_route = slot.jev_route
        slot.model = ""
        slot.jev_route = False
        slot._model_pick_gen += 1
        cleared_gen = slot._model_pick_gen
        slot._dirty = True
        stamped = state.sessions.allocation_requested_model(session_key)
        if default_model is not None and (
            model != default_model
            or (isinstance(stamped, str) and stamped and stamped != default_model)
        ):
            arm_model_switch_reset(state, slot)
    try:
        saved = await save_slot_off_loop(
            state,
            slot,
            force=True,
            expected_history_key=history_key,
            expected_slot_name=name,
            best_effort=False,
        )
    except Exception:
        logger.warning("Follow default: could not save %s", name, exc_info=True)
        async with slot_switch_session_lock(session_key), slot._model_pick_lock:
            if state._slots.get(name) is not slot or slot._model_pick_gen != cleared_gen:
                return SKIPPED
            if (
                effective_session_key(slot) == session_key
                and slot.model == ""
                and not slot._active_fallback_model
                and not slot._refusal_fallback_primary
            ):
                slot.model = model
                slot.jev_route = old_jev_route
        return FAILED
    return CHANGED if saved else SKIPPED


def _unwritten(conversation_log: Any, kept: _Kept, close: float | None) -> str:
    """Why a counted line was not rewritten: :data:`SKIPPED` or :data:`FAILED`.

    The guard turns away a line that changed since the count (a pick, a deletion)
    and one whose close save is still on its way: a skip, left for the next press. A
    line that still keeps the counted model, or cannot be read, was left as it was
    for another reason (a read that fails under the write lock defers the write), so
    that chat still keeps its model: a failure. Blocking: run off the loop.
    """
    try:
        meta, readable = conversation_log.get_metadata_status(kept.stem)
    except Exception:
        logger.warning("Follow default: could not re-read %s", kept.stem, exc_info=True)
        return FAILED
    if readable and _archive_pending(close, _shown_close(meta)):
        return SKIPPED
    if not readable or _line_model(meta, kept.pinned) == kept.model:
        return FAILED
    return SKIPPED


async def _clear(
    state: DashboardState, name: str, kept: _Kept, default_model: str | None = None
) -> str:
    """Clear one counted chat: :data:`CHANGED`, :data:`SKIPPED` or :data:`FAILED`.

    Skipped is a chat that changed since the count, closed or was replaced during
    the pick-lock wait, or is being opened right now: a resume still building its
    slot may hold the line it read before the rewrite, and its first save would
    write the old model back, so it is left for the next press rather than counted.
    So is a chat whose tab the boot restore has not rebuilt yet or could not read
    (``state.slot_exists``).
    A detached slot's pending archive also skips the chat: a close recorded in memory
    and newer than the one its line shows (judged under the write lock), or one
    recorded while the rewrite ran, may still save the old model.
    After a line rewrite, a loaded chat that already inherits also counts as changed.
    Failed is a clear the gateway could not save.
    """
    if _constructing(state, name):
        return SKIPPED
    slot = state._slots.get(name)
    if slot is not None:
        return await _clear_loaded(state, name, slot, kept.model, kept.pinned, default_model)
    if state.slot_exists(name):
        # A tab the boot restore has not rebuilt yet, or could not read: rebuilding it
        # applies the line it read before the rewrite, so it is left for the next press.
        return SKIPPED
    conversation_log = getattr(state, "conversation_log", None)
    if conversation_log is None:
        return SKIPPED
    # The close recorded before the rewrite, compared by value: a close is recorded
    # before its slot leaves ``_slots``, so a slot already gone has one here.
    close = slot_close_instant(state, name)
    try:
        written = await asyncio.to_thread(
            conversation_log.update_metadata_if,
            kept.stem,
            {"model": ""},
            lambda meta: _line_model(meta, kept.pinned) == kept.model
            and not _archive_pending(close, _shown_close(meta)),
            require_existing=True,
            require_write=True,
        )
    except Exception:
        logger.warning("Follow default: could not update %s", kept.stem, exc_info=True)
        return FAILED
    if not written:
        return await asyncio.to_thread(_unwritten, conversation_log, kept, close)
    if _constructing(state, name) or slot_close_instant(state, name) != close:
        return SKIPPED
    # Opened while the line was rewritten: a resume that read the line first holds
    # the old model in memory, and its next save would write it back.
    slot = state._slots.get(name)
    if slot is not None:
        cleared = await _clear_loaded(state, name, slot, kept.model, kept.pinned, default_model)
        if cleared == FAILED:
            return FAILED
        if cleared == SKIPPED and (state._slots.get(name) is not slot or slot.model != ""):
            return SKIPPED
    return CHANGED


async def api_chat_slots_follow_default(request: web.Request) -> web.Response:
    """``GET`` counts the chats that keep a model; ``POST {digest}`` clears them.

    Owner-only, like the Settings write it sits beside: it changes every chat a
    person started. ``GET`` answers ``{count, digest, default_model, models,
    titles}``: ``models`` is ``[{model, count}]``, most first, and ``titles`` the
    first five counted chats' redacted titles, ``""`` when untitled. ``POST`` answers
    ``{ok, changed}``; 409 ``chats_changed`` with the fresh ``{count, digest}``,
    changing nothing, when the set differs from the one the digest names; and 500
    ``chats_not_saved`` with ``{changed, failed}`` when a line could not be rewritten.
    """
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    if request.method == "GET":
        kept = await _kept_models(state)
        default_model = await asyncio.to_thread(_saved_default_model)
        shown = list(kept.values())[:_SHOWN_TITLES]
        return web.json_response(
            {
                "count": len(kept),
                "digest": _digest(kept),
                "default_model": default_model,
                "models": _by_model(kept),
                "titles": [_shown_title(entry.title) for entry in shown],
            }
        )
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    digest = body.get("digest")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        return web.json_response(
            {"error": "digest must be the preview's digest", "code": "invalid_digest"},
            status=400,
        )
    kept = await _kept_models(state)
    current = _digest(kept)
    if current != digest:
        return web.json_response(
            {
                "error": "the chats changed since they were counted",
                "code": "chats_changed",
                "count": len(kept),
                "digest": current,
            },
            status=409,
        )
    default_model = await asyncio.to_thread(_saved_default_model)
    changed = failed = 0
    for name, entry in kept.items():
        outcome = await _clear(state, name, entry, default_model)
        changed += outcome == CHANGED
        failed += outcome == FAILED
    if changed:
        state.push_slots_update()
    logger.info(
        "Follow default: %d of %d chats now follow the default, %d could not be saved",
        changed,
        len(kept),
        failed,
    )
    if failed:
        # Not a success: those chats still keep their model. A second press counts
        # exactly the ones left, so the remedy is to press again.
        return web.json_response(
            {
                "error": "some chats could not be saved",
                "code": "chats_not_saved",
                "changed": changed,
                "failed": failed,
            },
            status=500,
        )
    return web.json_response({"ok": True, "changed": changed})
