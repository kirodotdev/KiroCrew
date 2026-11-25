"""The ``!model`` picker: which models a session may switch to, the posted button set that
resolves a press back to one, and applying the pick.

``_model_choices`` offers only what the session's backend advertised, ``_handle_model``
posts the buttons and records a ``_ModelPicker`` per message, ``_prune_model_pickers``
bounds that table, and ``_apply_model`` stores the preference and switches a live
session in place. The press itself is resolved by ``on_interaction``
(``transport_dispatch_interactions``); the tables stay on the dispatcher.

Each function here whose first parameter is ``self`` is a ``DiscordDispatcher``
method: the class binds it by name, so it reads and writes the dispatcher's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields read (a call on one included): ``_allowed_threads``, ``_model_pickers``,
``_model_pref``, ``client``, ``sessions``.

Instance fields assigned, or whose mapping is changed: ``_model_pickers``,
``_model_pref``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.discord.renderer import build_model_components
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher, ReplyFn

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.discord.transport_dispatch")


# How long a !model picker stays pressable, and how many pickers are retained.
# Both bound unbounded growth (one entry per press-less !model), they are not UX
# knobs: an expired or evicted picker answers "reopen !model" rather than acting
# on a stale list. Mirrors the Telegram dispatcher.
_MODEL_PICKER_TTL_SECS = 300.0
_MODEL_PICKER_MAX = 50
#: Buttons a picker shows. Discord allows 5 buttons per action row and 5 rows,
#: so 25 is the platform ceiling; 24 leaves the Auto row inside it.
_MODEL_PICKER_LIMIT = 24


@dataclass
class _ModelPicker:
    """A posted !model button set, resolving a button index back to a model id."""

    scope_id: str
    channel_id: str
    message_id: str
    created_at: float
    #: ``(model_id, label)`` in button order. ``model_id`` "" is the Auto row.
    choices: tuple[tuple[str, str], ...]


def _model_choices(self: DiscordDispatcher, session_key: str) -> tuple[tuple[str, str], ...]:
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
    except Exception:
        logger.warning("discord !model: available_models failed", exc_info=True)
        return tuple(rows)
    for entry in entries:
        model_id = str(entry.get("modelId") or "").strip()
        # "auto" is already the first row; listing it twice would give the
        # same choice two buttons.
        if not model_id or model_id == "auto":
            continue
        rows.append((model_id, str(entry.get("name") or model_id)))
    return tuple(rows[:_MODEL_PICKER_LIMIT])


#: The argument that names the Auto row, whose model id is "".
AUTO_MODEL_ARG = "auto"


def _named_model(choices: tuple[tuple[str, str], ...], arg: str) -> tuple[str, str] | None:
    """The ``(model_id, label)`` row *arg* names exactly, or None."""
    wanted = " ".join(arg.split()).casefold()
    if not wanted:
        return None
    for model_id, label in choices:
        if wanted == (model_id or AUTO_MODEL_ARG).casefold() or wanted == label.casefold():
            return model_id, label
    return None


def _prune_model_pickers(self: DiscordDispatcher, now: float) -> None:
    """Drop expired pickers, then the oldest ones past the retention cap."""
    for token, picker in list(self._model_pickers.items()):
        if now - picker.created_at > _MODEL_PICKER_TTL_SECS:
            self._model_pickers.pop(token, None)
    while len(self._model_pickers) > _MODEL_PICKER_MAX:
        oldest = min(self._model_pickers, key=lambda t: self._model_pickers[t].created_at)
        self._model_pickers.pop(oldest, None)


async def _handle_model(
    self: DiscordDispatcher,
    channel_id: str,
    scope_id: str,
    session_key: str,
    arg: str,
    *,
    reply: ReplyFn | None = None,
    user_id: str = "",
) -> None:
    """Switch to the model *arg* names, or post the model buttons.

    *arg* is applied only when it names a row of this session's advertised list
    exactly (its id or its label, case aside; ``auto`` for the Auto row), which
    is what a `/model` type-ahead pick sends. Anything else is a guess at a name
    the user had no way to enumerate, so it shows the list instead of reaching
    ``set_model`` as a rejected id mid-conversation.

    ``reply`` is the slash command's private sink, so `/model` posts a picker
    only its caller sees. A press edits either kind through its own
    interaction token (see ``on_interaction``), which is the one route that
    reaches an ephemeral message.
    """
    say = reply or self._channel_reply(channel_id)
    choices = self._model_choices(session_key)
    if len(choices) <= 1:
        await say("No model list available yet — send a message first, then `!model`.")
        return

    picked = _named_model(choices, arg)
    if picked is not None:
        model_id, label = picked
        # Admission ran before routing's awaits, so it is read again before the
        # switch, in the button press's order: the ceiling, then the rosters
        # LAST with no await between them and the switch.
        if not await channel_inbound_permitted("discord") or (
            not self._authorized(user_id)
            or (scope_id.startswith("thread:") and channel_id not in self._allowed_threads)
        ):
            logger.info("discord !model switch dropped: authorization withdrawn")
            sel().log_api_access(
                caller=user_id or "unknown",
                operation="discord.set_model",
                outcome="denied",
                source="discord",
                resources=f"channel={channel_id};roster=withdrawn",
            )
            return
        outcome = await self._apply_model(scope_id, session_key, model_id)
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="discord.set_model",
            outcome="allowed",
            source="discord",
            resources=f"model={label}",
        )
        await say(outcome)
        return

    current = self._model_pref.get(scope_id, "")
    current_label = next(
        (label for mid, label in choices if mid == current),
        current or "Auto",
    )
    header = f"Current model: **{current_label}**\nPick one:"
    if arg.strip():
        # Not a model this session offers — say so once, then show the list
        # anyway so the message is still a step forward. The argument is not
        # echoed: it is free text, and the list names every valid one.
        header = f"That is not a model this conversation offers — pick from the list.\n\n{header}"
    message_id = await say(header, components=build_model_components(choices, current))
    if message_id is None:
        return
    now = time.time()
    self._prune_model_pickers(now)
    self._model_pickers[f"{channel_id}:{message_id}"] = _ModelPicker(
        scope_id=scope_id,
        channel_id=channel_id,
        message_id=message_id,
        created_at=now,
        choices=choices,
    )


async def _apply_model(
    self: DiscordDispatcher, scope_id: str, session_key: str, model_id: str
) -> str:
    """Record *model_id* for this conversation and push it to the live session.

    *model_id* comes verbatim from the backend's advertised list, so it is not
    translated (a canonical mapping could mangle a correct id). The preference is
    always stored so it reaches the NEXT session; a live session is switched in
    place via ``session/set_model`` under its semaphore, so the switch cannot
    interleave JSON-RPC with a turn. Returns the user-facing outcome line.
    """
    label = model_id or "Auto"
    self._model_pref[scope_id] = model_id
    live = self.sessions.has_session(session_key)
    # Two different promises, because the preference reaches a session only
    # at creation: ``get_or_create`` returns a reused session from its fast
    # path before it consults ``model=``. With nothing live the next message
    # starts the session, so it genuinely lands then; with a session already
    # up, only a fresh conversation picks it up.
    deferred = f"✅ Model set to {label} — it applies to your next message."
    next_new = (
        f"✅ Model set to {label} — this conversation keeps its current "
        f"model; the switch applies to your next one (`!new`)."
    )
    # Auto has no ACP id meaning "let the backend choose", so it can only be
    # recorded; the next session start resolves it from config. Claiming a
    # live switch here would be a lie.
    if not model_id:
        return next_new if live else deferred
    if not live:
        return deferred
    if not await self.sessions.try_acquire(session_key):
        # The acquire's Kiro account check retires a session that holds
        # another account, and then nothing is live: the next message starts
        # a fresh one. A session the check only flagged (an idle parent it
        # spared for its running sub-agents) stays registered but is refused
        # the same way, and the next message's claim retires it.
        if not self.sessions.has_session(session_key) or self.sessions.is_retiring(session_key):
            return deferred
        return (
            f"✅ Model set to {label}, but a reply is still running — this "
            f"conversation keeps its current model; the switch applies to "
            f"your next one (`!new`)."
        )
    try:
        provider = self.sessions.get_provider(session_key)
        set_model = getattr(getattr(provider, "client", None), "set_model", None)
        if set_model is None:
            return next_new
        await set_model(model_id)
    except Exception as exc:
        logger.warning(
            "discord !model: live set_model failed for %s: %s",
            session_key,
            type(exc).__name__,
            exc_info=True,
        )
        # The stored preference still stands, so the next session gets it,
        # but do not claim the running conversation switched when it did not.
        return (
            f"⚠️ Couldn't switch this conversation to {label} "
            f"({type(exc).__name__}) — it applies to your next "
            f"conversation (`!new`)."
        )
    finally:
        self.sessions.release(session_key)
    return f"✅ Now using {label}."
