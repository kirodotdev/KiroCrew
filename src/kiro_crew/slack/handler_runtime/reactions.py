"""The status reaction a native Slack turn wears on the message it answers.

At most one reaction, and only where it says something nothing else does:

* Where Slack shows the bot's native thread status
  (``assistant.threads.setStatus`` accepted the "is working on your request"
  line), that status IS the progress signal, so the message gets no reaction
  while the turn runs.
* Where it does not, the message gets the ``queued`` ack (👀) while the turn
  runs, so the user can see it was picked up.
* When the reply lands the ack is removed and nothing replaces it: the reply is
  the done signal. A failed turn gets the ``error`` reaction instead, on either
  surface, so a failure is never silent.

The phase table itself is built at import in :mod:`kiro_crew.slack.handler`, where
``slack.reactions`` overrides are read.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _PHASE_EMOJIS,
        SlackClientOps,
        _build_phase_emojis,
    )


def phase_emojis() -> dict[str, str | None]:
    """The phase -> emoji table currently in force (follows ``slack.reactions``).

    The one read path for every reaction site. Returns the live table object,
    which :func:`refresh_phase_emojis` rebuilds in place when the config changes
    -- so a caller that resolves a phase now sees the operator's latest
    overrides, not the ones captured when this module was imported.
    """
    return _PHASE_EMOJIS


def refresh_phase_emojis(overrides: dict[str, str | None] | None) -> list[str]:
    """Rebuild the live phase-emoji table from *overrides*, in place.

    Called by the gateway's Slack config applier with the reloaded
    ``slack.reactions``. In place, because ``StatusReactionController``
    instances and the reaction sites hold the table object, not a copy. Returns
    the unknown keys so the caller can warn about them, as the import-time build
    does.
    """
    built, unknown = _build_phase_emojis(overrides or {})
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(built)
    return unknown


class StatusReactionController:
    """The one reaction a Slack turn wears on the message it answers.

    :meth:`start` adds the ``queued`` ack unless Slack is already showing the
    native thread status; :meth:`finalize` takes the ack down when the turn
    ends and, only on failure, adds the ``error`` reaction. A failure found
    after a clean finalize (the reply itself failed to post) still upgrades to
    the error reaction, so an error is never silent.
    """

    def __init__(
        self, slack: SlackClientOps, channel: str, ts: str, *, enabled: bool = True
    ) -> None:
        self._enabled = enabled
        self._slack = slack
        self._channel = channel
        self._ts = ts
        self._current_emoji: str | None = None
        # Serializes the add/remove calls: the ack's add must reach Slack before
        # the reply-time remove, or a fast reply leaves the ack on the message.
        self._lock = asyncio.Lock()
        self._started = False
        self._finalized = False
        self._errored = False
        # Strong references: the loop holds tasks weakly, and a collected task
        # would drop the ack's removal or the error reaction.
        self._tasks: set[asyncio.Future[None]] = set()

    # ── public API ──────────────────────────────────────────────────

    def start(self, *, native_status: bool) -> None:
        """Acknowledge the message, unless Slack already shows native status.

        *native_status* is whether ``assistant.threads.setStatus`` accepted the
        working line for this turn's thread. Idempotent.
        """
        if self._started or self._finalized or not self._enabled:
            return
        self._started = True
        if native_status:
            return
        self._spawn(phase_emojis().get("queued"))

    def finalize(self, error: bool = False) -> None:
        """Take the ack down; on *error*, add the error reaction.

        Idempotent, except that ``error=True`` after a clean finalize upgrades
        to the error reaction (the turn's reply failed after the turn ended).
        """
        if not self._enabled or (self._finalized and (self._errored or not error)):
            return
        self._finalized = True
        self._errored = error
        self._spawn(phase_emojis().get("error") if error else None)

    # ── internal ────────────────────────────────────────────────────

    def _spawn(self, emoji: str | None) -> None:
        task = asyncio.ensure_future(self._swap_emoji(emoji))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _swap_emoji(self, new_emoji: str | None) -> None:
        """Remove the current reaction and add *new_emoji* (skip if same).

        ``new_emoji=None`` removes any applied reaction and adds nothing: the
        ack coming down at reply time, or a reaction suppressed by config.
        """
        async with self._lock:
            if new_emoji == self._current_emoji:
                return
            old = self._current_emoji
            self._current_emoji = new_emoji
            if old:
                try:
                    await self._slack.remove_reaction(self._channel, self._ts, old)
                except Exception:
                    pass
            if new_emoji is None:
                return
            try:
                await self._slack.add_reaction(self._channel, self._ts, new_emoji)
            except Exception:
                pass
