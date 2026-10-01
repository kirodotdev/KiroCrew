"""The ``/model`` and ``/agent`` inline-keyboard pickers.

Telegram caps ``callback_data`` at 64 bytes and model and agent ids routinely exceed
it, so a button carries an index into a retained picker table
(``TelegramDispatcher._model_pickers`` / ``_agent_pickers``), bounded by a TTL and a
retention cap. A press is consumed before it is applied, so a double press cannot
apply twice. The keyboards offer only what this session's backend advertised
(models) and what this machine can load (agents, minus Kiro Crew's own and
app-installed ones). ``dispatch/callbacks.py`` routes the press here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.agent_discovery import AgentInfo
    from kiro_crew.telegram.client import TelegramCallback
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")

# How long a /model picker stays pressable, and how many pickers are retained.
# Both are bounds on unbounded growth (one entry per press-less /model), not UX
# knobs: an expired or evicted picker answers "reopen /model" rather than acting
# on a stale list.
_MODEL_PICKER_TTL_SECS = 300.0


_MODEL_PICKER_MAX = 50


#: Buttons a picker shows. Telegram renders a one-per-row keyboard fine at this
#: size, and the list is the account's own model set, not a catalogue.
#: Shared by both pickers, like the TTL and the retention cap above: the lists
#: are this account's own models and this machine's own agent specs, not
#: catalogues, so one bound fits both.
_PICKER_LIMIT = 24


#: An agent kept out of the channel ``/agent`` picker BY DEFAULT: the picker
#: offers the agents a person driving from Telegram actually chooses between,
#: not Kiro Crew's own machinery. Two disjoint signals, both authoritative on
#: their own field rather than on the display name:
#:
#: * ``kirocrew_owned`` — the Kiro Crew-generated internal agents (the chat
#:   agent, the background/heartbeat/conductor/worker/knowledge specs). This is
#:   the ``name in OWNED_KIRO_AGENT_FILES`` flag ``list_agents`` already sets, so
#:   a user's OWN hand-authored ``kirocrew-custom.json`` — which merely shares
#:   the ``kirocrew`` name prefix and is not owned — is NOT hidden. The prefix
#:   would over-match it; the ownership flag is exactly the set to hide.
#: * an app-installed agent, whose spec is materialised under the
#:   ``<app>--<agent>.json`` link filename (see ``apps.bridges``/``apps.execution``).
#:   These belong to an installed app, not to the person picking an agent, so
#:   they are hidden alongside the internals. The double-dash is a filename
#:   convention Kiro Crew writes, not a name a user types, so matching the
#:   FILENAME (not the possibly-bare declared name) is what identifies them.
_APP_AGENT_LINK_SEP = "--"


def _agent_is_internal(info: AgentInfo) -> bool:
    """Whether *info* is a system/internal agent hidden from the channel picker.

    See :data:`_APP_AGENT_LINK_SEP`. Purely a function of the roster row, so the
    picker and any test agree on one definition.
    """
    if info.kirocrew_owned or info.source == "kirocrew":
        return True
    return _APP_AGENT_LINK_SEP in info.filename


@dataclass
class _Picker:
    """A posted keyboard, resolving a button index back to the value it names.

    One record type for ``/model`` and ``/agent``: both cap ``callback_data`` at
    64 bytes, both routinely carry ids longer than that, and both therefore send
    an INDEX into a retained table instead of the id itself.
    """

    route: tuple[str, str]
    created_at: float
    #: ``(value, label)`` in button order. A ``""`` value is the row that means
    #: "no explicit pick" — Auto for a model, the configured default for an agent.
    choices: tuple[tuple[str, str], ...]
    # Exact session the picker may mutate; revalidated on press.
    session_key: str = ""
    # Native Telegram model choices persist across /new; resumed-session choices
    # switch only the selected dashboard session.
    store_route_preference: bool = False


def _model_choices(self: TelegramDispatcher, session_key: str) -> tuple[tuple[str, str], ...]:
    """``(model_id, label)`` rows to offer for this session.

    The ONLY source is what this session's backend advertised at
    ``session/new`` — the set THIS account may actually use, carrying the
    backend's own ids. That is deliberate on both counts: a static catalogue
    would offer models the account cannot reach (a refusal mid-conversation),
    and its display keys would need per-backend translation before the wire,
    whereas an advertised id is what ``set_model`` accepts verbatim.

    Returns just the Auto row when nothing is advertised (no live session
    yet), which the caller reads as "there is nothing to pick".
    """
    rows: list[tuple[str, str]] = [("", "Auto (let the backend choose)")]
    provider = self.sessions.get_provider(session_key)
    advertised = getattr(provider, "available_models", None)
    if not callable(advertised):
        return tuple(rows)
    try:
        entries = [m for m in advertised() if isinstance(m, dict)]
    except Exception:  # pragma: no cover - defensive
        logger.warning("telegram /model: available_models failed", exc_info=True)
        return tuple(rows)
    for entry in entries:
        model_id = str(entry.get("modelId") or "").strip()
        # "auto" is already offered as the first row; listing it twice would
        # give the same choice two buttons.
        if not model_id or model_id == "auto":
            continue
        rows.append((model_id, str(entry.get("name") or model_id)))
    return tuple(rows[:_PICKER_LIMIT])


def _prune_pickers(table: dict[str, _Picker], now: float) -> None:
    """Drop expired pickers, then the oldest ones past the retention cap.

    Both bounds exist because every press-less picker leaves an entry behind;
    an expired or evicted one answers "reopen the command" rather than acting
    on a stale list.
    """
    for token, picker in list(table.items()):
        if now - picker.created_at > _MODEL_PICKER_TTL_SECS:
            table.pop(token, None)
    while len(table) > _MODEL_PICKER_MAX:
        table.pop(min(table, key=lambda t: table[t].created_at), None)


async def _consume_picker(
    self: TelegramDispatcher,
    cb: "TelegramCallback",
    data: str,
    table: dict[str, _Picker],
    *,
    noun: str,
    command: str,
) -> tuple[_Picker, str, str] | None:
    """Resolve a picker press to ``(picker, value, label)``, or None on a miss.

    Consumes the picker BEFORE the caller applies it: the apply takes a
    round-trip, and a second press in that window would otherwise apply twice.
    A miss covers expired, evicted and already-consumed alike — deliberately
    one wording, because "expired" would be wrong for the picker a double-press
    merely used, which is the case a user actually hits.
    """
    assert self.client is not None
    token = f"{cb.chat_id}:{cb.message_id}"
    picker = table.get(token)
    expired = picker is not None and (time.time() - picker.created_at > _MODEL_PICKER_TTL_SECS)
    try:
        index = int(data.partition(":")[2])
    except ValueError:
        index = -1
    if picker is None or expired or not (0 <= index < len(picker.choices)):
        table.pop(token, None)
        await self.client.edit_message(
            cb.chat_id,
            cb.message_id,
            f"⌛ This {noun} list is no longer active — send {command} again.",
            reply_markup={"inline_keyboard": []},
        )
        return None
    table.pop(token, None)
    value, label = picker.choices[index]
    return picker, value, label


async def _handle_model(
    self: TelegramDispatcher,
    route: tuple[str, str],
    chat_id: int,
    arg: str,
    *,
    session_key: str | None = None,
) -> None:
    """Post the model keyboard (or report the current pick for a bare arg).

    Deliberately button-only: a free-text model id means guessing at names
    the user has no way to enumerate, and a typo lands as a rejected
    ``set_model`` mid-conversation. Any argument is treated as "show me the
    list" rather than parsed.
    """
    assert self.client is not None
    target_key = session_key or self._session_key(route)
    thread = self._route_thread(route)
    choices = self._model_choices(target_key)
    if len(choices) <= 1:
        await self._reply(
            chat_id,
            "No model list available yet — send a message first, then /model.",
            thread=thread,
        )
        return

    current = self._model_pref.get(route, "")
    current_label = next(
        (label for mid, label in choices if mid == current),
        current or "Auto",
    )
    header = f"Current model: {current_label}\nPick one:"
    if arg.strip():
        # An argument is not an id to apply — say so once, then show the
        # list anyway so the message is still a step forward.
        header = f"/model takes no argument — pick from the list.\n\n{header}"
    keyboard = [
        [{"text": f"{'• ' if mid == current else ''}{label}", "callback_data": f"m:{index}"}]
        for index, (mid, label) in enumerate(choices)
    ]
    message_id = await self._reply(
        chat_id,
        header,
        thread=thread,
        reply_markup={"inline_keyboard": keyboard},
    )
    if message_id is None:
        return
    now = time.time()
    self._prune_pickers(self._model_pickers, now)
    self._model_pickers[f"{chat_id}:{message_id}"] = _Picker(
        route=route,
        created_at=now,
        choices=choices,
        session_key=target_key,
        store_route_preference=session_key is None,
    )


async def _apply_model(
    self: TelegramDispatcher,
    route: tuple[str, str],
    model_id: str,
    session_key: str | None = None,
    *,
    store_route_preference: bool | None = None,
) -> str:
    """Record *model_id* for *route* and push it to the live session.

    *model_id* comes verbatim from the session's advertised list, so it is
    already the id this backend accepts — no canonical translation, which
    would differ per backend and could mangle an id that was correct.

    The preference is stored unconditionally so it reaches the NEXT session
    even when there is nothing live to switch (the common case right after
    ``/new``). When a session does exist, the switch is attempted in place —
    ``session/set_model`` carries the conversation across — and the semaphore
    is taken atomically so the switch cannot interleave JSON-RPC with a turn
    on the same stdio channel.

    Returns the user-facing outcome line.
    """
    label = model_id or "Auto"
    if store_route_preference is None:
        store_route_preference = session_key is None
    if store_route_preference:
        self._model_pref[route] = model_id
    target_key = session_key or self._session_key(route)
    live = self.sessions.has_session(target_key)
    # Two different promises, because the preference reaches a session only
    # at creation: ``get_or_create`` returns a reused session from its fast
    # path before it consults ``model=``. With nothing live the next message
    # starts the session, so it genuinely lands then; with a session already
    # up, only a fresh conversation picks it up.
    deferred = f"✅ Model set to {label} — it applies to your next message."
    next_new = (
        f"✅ Model set to {label} — this conversation keeps its current "
        f"model; the switch applies to your next one (/new)."
    )
    # Auto has no ACP id meaning "let the backend choose", so it can only be
    # recorded; the next session start resolves it from config. Claiming a
    # live switch here would be a lie.
    if not model_id:
        if not store_route_preference:
            return (
                "⚠️ Auto can only be selected when starting a new Telegram "
                "conversation; the resumed session was not changed."
            )
        return next_new if live else deferred
    if not live:
        return deferred
    if not await self.sessions.try_acquire(target_key):
        return (
            f"✅ Model set to {label}, but a reply is still running — this "
            f"conversation keeps its current model; the switch applies to "
            f"your next one (/new)."
        )
    try:
        provider = self.sessions.get_provider(target_key)
        set_model = getattr(getattr(provider, "client", None), "set_model", None)
        if set_model is None:
            return next_new
        await set_model(model_id)
    except Exception as exc:
        logger.warning(
            "telegram /model: live set_model failed for %s: %s",
            target_key,
            type(exc).__name__,
            exc_info=True,
        )
        # A native-route preference still stands; a resumed session has no
        # deferred Telegram preference to apply later.
        suffix = (
            "it applies to your next conversation (/new)."
            if store_route_preference
            else "the resumed session was not changed."
        )
        return (
            f"⚠️ Couldn't switch this conversation to {label} " f"({type(exc).__name__}) — {suffix}"
        )
    finally:
        self.sessions.release(target_key)
    return f"✅ Now using {label}."


def _installed_agent_names() -> list[str]:
    """Selectable agent names for the picker, user-level scope, sorted.

    System and internal agents are excluded (:func:`_agent_is_internal`):
    the picker offers the agents a person chooses between, so Kiro Crew's own
    machinery and app-installed agents do not occupy its slots and crowd out
    the user's own agents. The classification is on the roster row's fields,
    not the display name, so a user's own ``kirocrew``-prefixed agent stays.

    ``list_agents`` caches on a directory signature but still reads and
    parses each JSON on a miss, so callers run it off the loop.
    """
    from kiro_crew.telegram import transport_dispatch as facade

    return sorted(
        {info.name for info in facade.list_agents() if info.name and not _agent_is_internal(info)}
    )


def _agent_choices(self: TelegramDispatcher, names: list[str]) -> tuple[tuple[str, str], ...]:
    """``(agent_id, label)`` rows to offer, "" first for the configured default.

    Sourced from the installed agent specs, so the list is what this machine
    can actually load — a static catalogue would offer an agent whose spec is
    absent and fail at the next session start, which is exactly the failure
    mode the ``/model`` picker avoids by listing only advertised ids.

    Only SELECTABLE rows are returned, and this is the exact set stored in
    the picker's resolution table: cut to :data:`_PICKER_LIMIT` so the count
    fits Telegram's keyboard. The count hidden by that cut is surfaced by the
    caller as a separate, non-selectable keyboard row — it must not live here,
    or a press would resolve its index back to an agent.
    """
    configured = self._configured_agent()
    rows: list[tuple[str, str]] = [("", f"Default ({configured})")]
    rows.extend((name, name) for name in names[:_PICKER_LIMIT])
    return tuple(rows)


async def _handle_agent(
    self: TelegramDispatcher, route: tuple[str, str], chat_id: int, arg: str
) -> None:
    """Post the agent keyboard, or report the current pick when there is none.

    Button-only, for the same reason ``/model`` is: a free-text agent name is
    a guess at something the user cannot enumerate, and a typo lands as a
    cold-start failure on the next message rather than an error here.
    """
    assert self.client is not None
    thread = self._route_thread(route)
    try:
        names = await asyncio.to_thread(self._installed_agent_names)
    except Exception:
        logger.warning("telegram /agent: agent discovery failed", exc_info=True)
        names = []
    choices = self._agent_choices(names)
    if len(choices) <= 1:
        await self._reply(chat_id, "No agent list available on this machine.", thread=thread)
        return
    current = self._agent_pref.get(route, "")
    current_label = next((label for aid, label in choices if aid == current), current or "Default")
    header = f"Current agent: {current_label}\nPick one:"
    if arg.strip():
        header = f"/agent takes no argument — pick from the list.\n\n{header}"
    keyboard = [
        [{"text": f"{'• ' if aid == current else ''}{label}", "callback_data": f"g:{index}"}]
        for index, (aid, label) in enumerate(choices)
    ]
    # Make truncation VISIBLE instead of silent (the reported harm). More
    # selectable agents than fit are cut in ``_agent_choices``; this trailing
    # row names how many were dropped. Its ``callback_data`` matches no picker
    # prefix, so a press is inert — the callback is already acked, and it is
    # deliberately NOT a ``g:`` index that would resolve back to an agent.
    hidden = len(names) - _PICKER_LIMIT
    if hidden > 0:
        keyboard.append([{"text": f"… and {hidden} more not shown", "callback_data": "noop"}])
    message_id = await self._reply(
        chat_id, header, thread=thread, reply_markup={"inline_keyboard": keyboard}
    )
    if message_id is None:
        return
    now = time.time()
    self._prune_pickers(self._agent_pickers, now)
    self._agent_pickers[f"{chat_id}:{message_id}"] = _Picker(
        route=route, created_at=now, choices=choices
    )


async def _apply_agent(self: TelegramDispatcher, route: tuple[str, str], agent_id: str) -> str:
    """Record *agent_id* for *route* and report what it changed.

    The agent is part of the session key, so a pick necessarily opens a FRESH
    conversation — there is no in-place swap to claim, and the message says so
    rather than implying the running conversation changed spec. The previous
    conversation is not destroyed: switching back reaches the same key again.

    REFUSED while a reply is streaming, for the same reason ``/compact``
    refuses: everything about a running turn is keyed on the session key —
    ``_active_renderers`` for the steer chip, the queue receipt, ``/stop``'s
    provider lookup — so moving the key out from under it would leave that
    turn running with no route back to it, and a ``/stop`` would report
    nothing running while the answer kept arriving.
    """
    label = agent_id or f"Default ({self._configured_agent()})"
    key = self._session_key(route)
    if self.sessions.is_busy(key):
        return (
            f"⏳ Still working on your last message — send /agent again once "
            f"it finishes to switch to {label}."
        )
    had = self.sessions.has_session(key)
    if agent_id:
        self._agent_pref[route] = agent_id
    else:
        self._agent_pref.pop(route, None)
    if not had:
        return f"✅ Agent set to {label}."
    return (
        f"✅ Agent set to {label} — this starts a fresh conversation. "
        f"Switch back to return to the previous one."
    )
