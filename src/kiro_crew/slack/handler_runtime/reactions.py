"""The status reactions a native Slack turn wears on the message it answers.

The phase ladder (queued, thinking, coding, browsing, tool, done, error) with its
debounce and stall watchdog, the tool-to-phase mapping, and the one-shot reactions the
``!`` commands add. The phase table itself is built at import in
:mod:`kiro_crew.slack.handler`, where ``slack.reactions`` overrides are read.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _CODING_KINDS,
        _CODING_TOOLS,
        _IMMEDIATE_PHASES,
        _PHASE_DEBOUNCE_SECS,
        _PHASE_EMOJIS,
        _STALL_EMOJI_HARD,
        _STALL_EMOJI_SOFT,
        _STALL_HARD_SECS,
        _STALL_SOFT_SECS,
        _TERMINAL_PHASES,
        _WEB_KINDS,
        _WEB_TOOLS,
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


async def _add_phase_reaction(slack: SlackClientOps, channel: str, ts: str, phase: str) -> None:
    """Add the reaction for *phase* if the user hasn't suppressed it.

    Used by one-shot emoji-ack sites outside ``StatusReactionController``
    (e.g. ``!command`` handlers).  Honours ``slack.reactions`` ``null``
    suppression sentinels.
    """
    emoji = phase_emojis().get(phase)
    if emoji is None:
        return
    await slack.add_reaction(channel, ts, emoji)


def _tool_to_phase(tool_name: str, tool_kind: str = "") -> str:
    """Map a tool name/kind to a reaction phase."""
    kind_lower = tool_kind.lower()
    if kind_lower:
        if kind_lower in _CODING_KINDS:
            return "coding"
        if kind_lower in _WEB_KINDS:
            return "browsing"
    # Extract base tool name for MCP tools (mcp__example-mcp__Bash → Bash)
    base = tool_name.split("__")[-1] if "__" in tool_name else tool_name
    if base in _CODING_TOOLS:
        return "coding"
    if base in _WEB_TOOLS:
        return "browsing"
    return "tool"


class StatusReactionController:
    """Phase-aware Slack reaction controller with debounce and stall detection.

    Provides richer emoji feedback than the old binary eyes/lobster pair.
    Intermediate phases are debounced so rapid tool transitions don't spam
    the Slack API.  A stall watchdog adds yawning/fearful reactions when
    the agent appears stuck.
    """

    def __init__(
        self, slack: SlackClientOps, channel: str, ts: str, *, enabled: bool = True
    ) -> None:
        self._enabled = enabled
        self._slack = slack
        self._channel = channel
        self._ts = ts
        self._loop = asyncio.get_running_loop()

        self._current_emoji: str | None = None
        self._pending_phase: str | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._stall_soft_handle: asyncio.TimerHandle | None = None
        self._stall_hard_handle: asyncio.TimerHandle | None = None
        self._stall_emoji: str | None = None
        self._stall_paused = False
        self._finalized = False

    # ── public API ──────────────────────────────────────────────────

    def set_phase(self, phase: str) -> None:
        """Request a phase transition (may be debounced)."""
        if self._finalized or not self._enabled:
            return

        if phase in _TERMINAL_PHASES:
            self.finalize(error=(phase == "error"))
            return

        if phase in _IMMEDIATE_PHASES:
            self._cancel_debounce()
            emoji = phase_emojis().get(phase, phase)
            asyncio.ensure_future(self._swap_emoji(emoji))
            self._reset_stall_watchdog()
            return

        # Intermediate phase — debounce
        self._pending_phase = phase
        self._cancel_debounce()
        self._debounce_handle = self._loop.call_later(_PHASE_DEBOUNCE_SECS, self._fire_debounce)

    def on_progress(self) -> None:
        """Reset stall watchdog — call on any LLM/tool activity."""
        if not self._finalized and not self._stall_paused and self._enabled:
            self._reset_stall_watchdog()

    def pause_stall_watchdog(self) -> None:
        """Pause stall detection (e.g. waiting for user approval)."""
        self._stall_paused = True
        self._cancel_stall_timers()

    def resume_stall_watchdog(self) -> None:
        """Resume stall detection after a pause."""
        self._stall_paused = False
        if not self._finalized and self._enabled:
            self._reset_stall_watchdog()

    def finalize(self, error: bool = False) -> None:
        """Swap to terminal emoji. Idempotent."""
        if self._finalized or not self._enabled:
            return
        self._finalized = True
        self._cancel_debounce()
        self._cancel_stall_timers()
        # Clean up stall emoji before setting terminal
        asyncio.ensure_future(self._do_finalize(error))

    # ── internal ────────────────────────────────────────────────────

    async def _do_finalize(self, error: bool) -> None:
        if self._stall_emoji:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, self._stall_emoji)
            except Exception:
                pass
            self._stall_emoji = None
        terminal = phase_emojis()["error" if error else "done"]
        await self._swap_emoji(terminal)

    def _fire_debounce(self) -> None:
        """Timer callback — bridge to async."""
        asyncio.ensure_future(self._apply_pending())

    async def _apply_pending(self) -> None:
        if self._finalized or self._pending_phase is None:
            return
        emoji = phase_emojis().get(self._pending_phase, self._pending_phase)
        self._pending_phase = None
        await self._swap_emoji(emoji)
        self._reset_stall_watchdog()

    async def _swap_emoji(self, new_emoji: str | None) -> None:
        """Remove old reaction and add new one (skip if same).

        ``new_emoji=None`` means the phase is suppressed by config: remove
        any already-applied reaction but do not add a replacement.
        """
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

    def _cancel_debounce(self) -> None:
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

    def _cancel_stall_timers(self) -> None:
        if self._stall_soft_handle is not None:
            self._stall_soft_handle.cancel()
            self._stall_soft_handle = None
        if self._stall_hard_handle is not None:
            self._stall_hard_handle.cancel()
            self._stall_hard_handle = None

    def _reset_stall_watchdog(self) -> None:
        if not self._enabled:
            return
        self._cancel_stall_timers()
        # Remove existing stall emoji
        if self._stall_emoji:
            emoji_to_remove = self._stall_emoji
            self._stall_emoji = None
            asyncio.ensure_future(self._remove_stall_emoji(emoji_to_remove))
        if self._stall_paused or self._finalized:
            return
        self._stall_soft_handle = self._loop.call_later(_STALL_SOFT_SECS, self._on_stall_soft)
        self._stall_hard_handle = self._loop.call_later(_STALL_HARD_SECS, self._on_stall_hard)

    async def _remove_stall_emoji(self, emoji: str) -> None:
        try:
            await self._slack.remove_reaction(self._channel, self._ts, emoji)
        except Exception:
            pass

    def _on_stall_soft(self) -> None:
        asyncio.ensure_future(self._add_stall_emoji(_STALL_EMOJI_SOFT))

    def _on_stall_hard(self) -> None:
        asyncio.ensure_future(self._add_stall_emoji(_STALL_EMOJI_HARD))

    async def _add_stall_emoji(self, emoji: str) -> None:
        if self._finalized:
            return
        # Remove previous stall emoji if upgrading
        if self._stall_emoji and self._stall_emoji != emoji:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, self._stall_emoji)
            except Exception:
                pass
        self._stall_emoji = emoji
        try:
            await self._slack.add_reaction(self._channel, self._ts, emoji)
        except Exception:
            pass
