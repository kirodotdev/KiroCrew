"""Say at start, in the terminal and the bell, that a config file does not parse.

A ``config.json`` or ``config.local.json`` with a JSON syntax error is read as
absent, so the gateway runs on defaults and the person who saved it sees nothing
wrong. After the gateway is ready this prints each error to stderr and the log
(:meth:`ConfigSyntaxError.explain`: the file, the line, the parser's reason and one
fix), and keeps one ``system.agent`` bell note naming them.

The note carries a ``key``, so the bell holds one such note. Whether a stored note
already says the same thing is read from its ``body``, a field the notification
store writes and reloads, so the answer survives a restart. A start that finds the
same errors publishes nothing new to the bell (the terminal line is still printed:
a terminal holds no history). A start that finds the files parse removes the stored
note. Different errors first remove the stored note, so the new one rings instead
of replacing it quietly.

The running gateway's live watcher keeps this slot too (:func:`publish_tear`). A save
that leaves a watched file torn keeps the settings in force, so its note says the
edit is not applied. A process that booted on a torn file runs on defaults, so there
the start report's note stands (an identical body is not rung twice). The watcher
hands over one sentence per torn file (``syntax_report.position_sentence``) and an
empty tuple once a later read adopts the files whole. A whole read settles the note
from the files on disk, because a hand edit the sandbox holds for review reads as
whole to the watcher; it also removes a note an earlier process stored, so a repair
made while no gateway ran does not leave it behind. A removal or a note that is not
stored raises :class:`NoteNotSaved`, and the watcher retries it on its next tick.

The stored notes are read from ``state._notification_log``, the list every other
producer and handler reads them from; the dashboard state has no other accessor.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from kiro_crew.config.syntax_report import ConfigSyntaxError, find_config_syntax_errors
from kiro_crew.notifications.bus import NotificationPayload

logger = logging.getLogger(__name__)

CHANNEL = "system.agent"
SOURCE = "system"
#: Replaces the stored note of the same source, so the bell holds one.
KEY = "config-syntax-error"


def report_to_terminal(errors: list[ConfigSyntaxError]) -> None:
    """Print each error to stderr and log it at WARNING."""
    for err in errors:
        text = err.explain()
        logger.warning("%s", text)
        print(f"WARNING: {text}", file=sys.stderr, flush=True)


def _title(errors: list[ConfigSyntaxError]) -> str:
    names = " and ".join(err.path.name for err in errors)
    has, is_ = ("has", "is") if len(errors) == 1 else ("have", "are")
    return f"{names} {has} a syntax error and {is_} ignored"


def note_body(errors: list[ConfigSyntaxError]) -> str:
    """The bell note's body: one paragraph per line, since the bell renders Markdown."""
    return "\n\n".join(err.explain().replace("\n", "\n\n") for err in errors)


class NoteNotSaved(RuntimeError):
    """The bell change was not stored."""


def _stored(state: Any) -> list[dict]:
    return [
        n
        for n in getattr(state, "_notification_log", ())
        if n.get("source") == SOURCE and n.get("key") == KEY
    ]


async def _remove(state: Any, which: str) -> None:
    _removed, persisted = await state.dismiss_notification(source=SOURCE, key=KEY)
    if not persisted:
        raise NoteNotSaved(f"could not remove the {which} note")


async def _ring(state: Any, *, title: str, body: str) -> bool:
    """Put this note in the bell. False when the stored note already says it."""
    stored = _stored(state)
    if stored and stored[-1].get("body") == body:
        return False
    if stored:
        # A same-key push at the same priority replaces the old note quietly,
        # so a new error would ring nothing. Remove the old note first.
        await _remove(state, "earlier")
    state.notification_bus.push(
        NotificationPayload(
            source=SOURCE,
            channel=CHANNEL,
            # Every setting or edit in the file is ignored until it parses.
            priority="critical",
            title=title,
            body=body,
            key=KEY,
        )
    )
    # Read before any await: a later note replaces the state-wide future.
    persisted = getattr(state, "last_notification_persist", None)
    if persisted is not None and not await persisted:
        raise NoteNotSaved("the note was not stored")
    return True


async def publish(state: Any) -> bool:
    """Report the config syntax errors found now. True when a bell note was pushed."""
    errors = await asyncio.to_thread(find_config_syntax_errors)
    report_to_terminal(errors)
    if state is None:
        return False
    try:
        if not errors:
            if _stored(state):
                await _remove(state, "resolved")
            return False
        return await _ring(state, title=_title(errors), body=note_body(errors))
    except NoteNotSaved as e:
        logger.warning("config syntax note: %s", e)
        return False


def tear_body(tear: tuple[str, ...]) -> str:
    """The live note's body: one paragraph per torn file, then what that means now."""
    return "\n\n".join(
        (
            *tear,
            "Kiro Crew keeps running on the settings it has loaded, so the edit "
            "you saved is not applied yet. The file is left as you saved it.",
            "To fix it: correct the file and save it again. The change applies "
            "within a few seconds, with no restart.",
        )
    )


async def publish_tear(state: Any, tear: tuple[str, ...], booted_on_torn: bool = False) -> bool:
    """Settle the bell for the live watcher's *tear*. True when a note was pushed.

    The note says what holds now. A save that tears a file while earlier settings are
    in force says the edit is not applied (:func:`tear_body`). Where the process
    booted on the torn file (*booted_on_torn*), defaults run and the start report's
    note, built from the files on disk, is the true one, so it is the note rung (the
    same body is not rung twice, a different error replaces it). An empty *tear* means
    the copy the loader serves parses: where the sandbox holds a hand edit for review
    that copy is the endorsed one while the file on disk does not, so the files on
    disk decide there too, and only files that all parse remove the note.

    Raises :class:`NoteNotSaved` when the removal or the note was not stored.
    """
    if tear and not booted_on_torn:
        return await _live_note(state, tear)
    errors = await asyncio.to_thread(find_config_syntax_errors)
    if errors:
        return await _ring(state, title=_title(errors), body=note_body(errors))
    if tear:
        return await _live_note(state, tear)
    # Dismissed even when no note is stored yet: a note whose write is still in
    # flight lands only when the write finishes, and the dismissal waits behind it.
    await _remove(state, "resolved")
    return False


async def _live_note(state: Any, tear: tuple[str, ...]) -> bool:
    return await _ring(
        state,
        title="A config file does not parse; your last edit is not applied",
        body=tear_body(tear),
    )
