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


def note_body(errors: list[ConfigSyntaxError]) -> str:
    """The bell note's body: one paragraph per line, since the bell renders Markdown."""
    return "\n\n".join(err.explain().replace("\n", "\n\n") for err in errors)


async def publish(state: Any) -> bool:
    """Report the config syntax errors found now. True when a bell note was pushed."""
    errors = await asyncio.to_thread(find_config_syntax_errors)
    report_to_terminal(errors)
    if state is None:
        return False
    stored = [
        n
        for n in getattr(state, "_notification_log", ())
        if n.get("source") == SOURCE and n.get("key") == KEY
    ]
    if not errors:
        if stored:
            _removed, persisted = await state.dismiss_notification(source=SOURCE, key=KEY)
            if not persisted:
                logger.warning("config syntax note: could not remove the resolved note")
        return False
    body = note_body(errors)
    if stored and stored[-1].get("body") == body:
        return False
    if stored:
        # A same-key push at the same priority replaces the old note quietly,
        # so a new error would ring nothing. Remove the old note first.
        _removed, persisted = await state.dismiss_notification(source=SOURCE, key=KEY)
        if not persisted:
            logger.warning("config syntax note: could not remove the earlier note")
            return False
    names = " and ".join(err.path.name for err in errors)
    has, is_ = ("has", "is") if len(errors) == 1 else ("have", "are")
    state.notification_bus.push(
        NotificationPayload(
            source=SOURCE,
            channel=CHANNEL,
            # Every setting in the file is ignored until it is fixed.
            priority="critical",
            title=f"{names} {has} a syntax error and {is_} ignored",
            body=body,
            key=KEY,
        )
    )
    # Read before any await: a later note replaces the state-wide future.
    persisted = getattr(state, "last_notification_persist", None)
    if persisted is not None and not await persisted:
        logger.warning("config syntax note: the note was not stored")
        return False
    return True
