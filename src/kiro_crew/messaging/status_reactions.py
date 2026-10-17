"""Channel-neutral turn-status surfacing (``messaging.status_reactions``).

Two things tell a chat user how their turn is going without adding a message to
the conversation: a reaction on their own message that swaps as the agent moves
through the turn, and one low-emphasis line at the end carrying how long the
turn took and how full the context window now is. Both live here because both
are the same shape, per-channel decoration over channel-neutral state, and
because a channel that keeps its own copy of either drifts from the rest.

The ladder owns the phase machine: which phase is current, the debounce that
keeps a rapid tool burst from spending the channel's reaction rate budget on
emoji nobody reads, the stall watchdog that marks a turn which has gone quiet,
and single-flight emoji swapping.

It owns no channel API. The one operation it needs, add or remove ONE emoji on
ONE message, arrives as an injected :class:`ReactionSink`. That is what lets
``kiro_crew.messaging`` stay free of any import from ``kiro_crew.slack`` /
``kiro_crew.discord`` / ``kiro_crew.dashboard`` (the one-way dependency
direction this package is built on) while every channel still runs one ladder
instead of its own.

Emoji are the CHANNEL's vocabulary, never this module's: Slack takes shortcodes
(``eyes``), Discord takes unicode (``👀``). So the phase table is injected too,
and :func:`merge_phase_emojis` applies a user's overrides onto whichever table
the channel owns.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ── Phases ──────────────────────────────────────────────────────────────

PHASE_QUEUED = "queued"
PHASE_THINKING = "thinking"
PHASE_CODING = "coding"
PHASE_BROWSING = "browsing"
PHASE_TOOL = "tool"
PHASE_DONE = "done"
PHASE_ERROR = "error"

#: The ladder in the order a turn walks it. A channel's emoji table is keyed by
#: these names, and only these: an unknown key is a typo in someone's config,
#: which :func:`merge_phase_emojis` reports rather than silently honouring.
PHASES: tuple[str, ...] = (
    PHASE_QUEUED,
    PHASE_THINKING,
    PHASE_CODING,
    PHASE_BROWSING,
    PHASE_TOOL,
    PHASE_DONE,
    PHASE_ERROR,
)

#: Phases that end the turn: they land immediately and nothing follows them.
TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_ERROR})
#: Phases that must be visible at once rather than debounced. ``queued`` is the
#: receipt for "your message arrived", so a delay defeats the point of it.
IMMEDIATE_PHASES = frozenset({PHASE_QUEUED})

# Tool → phase classification. Names are the harness's own tool names; kinds are
# what the ACP stream reports, and win when present because a bring-your-own
# tool can be named anything while still declaring a known kind.
_CODING_TOOLS: frozenset[str] = frozenset(
    {"Bash", "Write", "Edit", "Read", "Glob", "Grep", "NotebookEdit"}
)
_WEB_TOOLS: frozenset[str] = frozenset({"WebFetch", "WebSearch", "Browser"})
_CODING_KINDS: frozenset[str] = frozenset(t.lower() for t in _CODING_TOOLS)
_WEB_KINDS: frozenset[str] = frozenset(t.lower() for t in _WEB_TOOLS)
# kiro-cli's ACP stream titles a command tool "Running: <cmd>" for display.
_TITLE_RUN_PREFIX = "Running: "


def tool_to_phase(tool_name: str, tool_kind: str = "") -> str:
    """Map a tool name/kind onto a ladder phase, defaulting to ``tool``."""
    kind_lower = (tool_kind or "").lower()
    if kind_lower:
        if kind_lower in _CODING_KINDS:
            return PHASE_CODING
        if kind_lower in _WEB_KINDS:
            return PHASE_BROWSING
    # An MCP tool arrives fully qualified (mcp__example-mcp__Bash), and the base
    # name is what the classification knows.
    base = tool_name.split("__")[-1] if "__" in tool_name else tool_name
    if base in _CODING_TOOLS:
        return PHASE_CODING
    if base in _WEB_TOOLS:
        return PHASE_BROWSING
    return PHASE_TOOL


def phase_for_tool_title(title: str, tool_kind: str = "") -> str:
    """:func:`tool_to_phase` for a tool's DISPLAY title.

    Classification works on the tool name, so kiro-cli's ``Running: `` display
    prefix comes off first: a channel that passes the title straight through
    classifies every command as a generic tool.
    """
    return tool_to_phase(title.removeprefix(_TITLE_RUN_PREFIX), tool_kind)


def merge_phase_emojis(
    defaults: Mapping[str, str | None],
    overrides: Mapping[str, str | None] | None = None,
) -> tuple[dict[str, str | None], list[str]]:
    """Return ``(table, unknown_keys)`` with a user's *overrides* applied.

    A value of ``None`` suppresses that phase entirely: no emoji is added for
    it, though a transition into it still clears whatever the previous phase
    left behind. Keys outside *defaults* are collected rather than applied, so
    the caller can tell the user about a typo instead of dropping it silently.
    """
    table: dict[str, str | None] = dict(defaults)
    unknown: list[str] = []
    for key, value in (overrides or {}).items():
        if key in defaults:
            table[key] = value
        else:
            unknown.append(key)
    return table, unknown


# ── Turn timing line ────────────────────────────────────────────────────

#: Context-usage bands, most-used first: the icon is a glance-level read on how
#: close the window is to needing a compaction.
_CTX_BANDS: tuple[tuple[float, str], ...] = ((70.0, "🔴"), (50.0, "🟠"), (30.0, "🟡"))
_CTX_ICON_CLEAR = "🟢"
_SECS_PER_MIN = 60


def format_elapsed(elapsed: float) -> str:
    """Format *elapsed* seconds as ``12s`` or ``3m 7s``."""
    seconds = max(0, int(elapsed))
    if seconds < _SECS_PER_MIN:
        return f"{seconds}s"
    mins, secs = divmod(seconds, _SECS_PER_MIN)
    return f"{mins}m {secs}s"


def context_icon(context_pct: float) -> str:
    """The band icon for a context-window usage percentage."""
    for threshold, icon in _CTX_BANDS:
        if context_pct >= threshold:
            return icon
    return _CTX_ICON_CLEAR


def format_turn_status(elapsed: float, context_pct: float | None = None) -> str:
    """One line of turn-end status, undecorated by any channel's markup.

    Carries the elapsed time always and the context-usage chip only when the
    caller could read a usage figure: an absent one means "unknown", which is
    not the same as 0% and must not render as a reassuring green chip.
    """
    line = f"Finished in {format_elapsed(elapsed)}"
    if context_pct is None:
        return line
    pct = round(context_pct)
    return f"{line} · {context_icon(context_pct)} ctx {pct}%"


# ── Sink seam ───────────────────────────────────────────────────────────


class ReactionSink(Protocol):
    """Add or remove one emoji on one message.

    Implementations are per-channel and are bound to a single message, so the
    ladder never handles a channel id, a message id, or an emoji vocabulary.
    Either call may fail; the ladder treats a failure as non-fatal, because a
    reaction is decoration and the turn behind it is not.
    """

    async def add(self, emoji: str) -> None: ...

    async def remove(self, emoji: str) -> None: ...


class CallableReactionSink:
    """A :class:`ReactionSink` over two coroutine functions of one emoji.

    Lets a channel bind its own client, channel, and message into closures at
    the call site rather than writing a sink class per channel.
    """

    def __init__(
        self,
        add: Callable[[str], Awaitable[Any]],
        remove: Callable[[str], Awaitable[Any]],
    ) -> None:
        self._add = add
        self._remove = remove

    async def add(self, emoji: str) -> None:
        await self._add(emoji)

    async def remove(self, emoji: str) -> None:
        await self._remove(emoji)


# ── Ladder configuration ────────────────────────────────────────────────

#: Hold an intermediate phase this long before it reaches the channel. A turn
#: that fires five tools in a second is one visible transition, not five.
DEFAULT_DEBOUNCE_SECS = 0.7
#: Quiet for this long and the turn is marked as dragging.
DEFAULT_STALL_SOFT_SECS = 15.0
#: Quiet for this long and the mark is upgraded.
DEFAULT_STALL_HARD_SECS = 45.0
#: Longest ``close`` waits for in-flight sink calls before cancelling them. A
#: wedged channel API must not hold a turn's teardown open indefinitely.
DEFAULT_CLOSE_DRAIN_SECS = 5.0


@dataclass(frozen=True)
class LadderTimings:
    """The ladder's four durations, injectable so tests need no real clock."""

    debounce: float = DEFAULT_DEBOUNCE_SECS
    stall_soft: float = DEFAULT_STALL_SOFT_SECS
    stall_hard: float = DEFAULT_STALL_HARD_SECS
    close_drain: float = DEFAULT_CLOSE_DRAIN_SECS


@dataclass(frozen=True)
class StallEmojis:
    """Marks for a turn that has gone quiet, in the channel's vocabulary.

    Either may be ``None``, which suppresses that mark and its timer: a channel
    with no stall vocabulary schedules no watchdog at all.
    """

    soft: str | None = None
    hard: str | None = None


class PhaseReactionLadder:
    """Phase-aware reaction ladder with debounce and stall detection.

    One reaction at a time represents the turn: entering a phase removes the
    previous emoji and adds the new one. Intermediate phases are debounced so a
    burst of tool calls costs one reaction edit rather than one per call, which
    matters because every channel rate-limits reactions. A stall watchdog adds a
    second, additive mark when nothing has happened for a while, so a wedged
    turn is visible without the user asking.

    Construct inside a running event loop: the timers are bound to it.
    """

    def __init__(
        self,
        sink: ReactionSink,
        *,
        emojis: Mapping[str, str | None],
        stall: StallEmojis | None = None,
        timings: LadderTimings | None = None,
        enabled: bool = True,
    ) -> None:
        self._sink = sink
        # Snapshot rather than reference: the table comes from config the
        # channel owns, and a swap mid-turn would have the ladder try to remove
        # an emoji it never added.
        self._emojis: dict[str, str | None] = dict(emojis)
        self._stall = stall or StallEmojis()
        self._timings = timings or LadderTimings()
        self._enabled = enabled
        self._loop = asyncio.get_running_loop()

        self._current_emoji: str | None = None
        self._pending_phase: str | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._stall_soft_handle: asyncio.TimerHandle | None = None
        self._stall_hard_handle: asyncio.TimerHandle | None = None
        self._stall_emoji: str | None = None
        self._stall_paused = False
        self._finalized = False
        self._closed = False
        # Every sink call runs as a task so ``close`` can drain it. Holding the
        # reference is what keeps the loop from collecting one mid-flight.
        self._tasks: set[asyncio.Task[None]] = set()
        # Orders those tasks against each other where it matters: see
        # ``_swap_emoji``.
        self._swap_lock = asyncio.Lock()

    # ── public API ──────────────────────────────────────────────────

    def set_phase(self, phase: str) -> None:
        """Request a phase transition (intermediate phases are debounced)."""
        if self._finalized or self._closed or not self._enabled:
            return

        if phase in TERMINAL_PHASES:
            self.finalize(error=phase == PHASE_ERROR)
            return

        if phase in IMMEDIATE_PHASES:
            self._cancel_debounce()
            self._spawn(self._swap_emoji(self._emoji_for(phase)))
            self._reset_stall_watchdog()
            return

        self._pending_phase = phase
        self._cancel_debounce()
        self._debounce_handle = self._loop.call_later(self._timings.debounce, self._fire_debounce)

    def on_progress(self) -> None:
        """Reset the stall watchdog. Call on any agent or tool activity."""
        if self._finalized or self._closed or self._stall_paused or not self._enabled:
            return
        self._reset_stall_watchdog()

    def pause_stall_watchdog(self) -> None:
        """Stop stall detection while the turn legitimately waits on a human."""
        self._stall_paused = True
        self._cancel_stall_timers()

    def resume_stall_watchdog(self) -> None:
        """Resume stall detection after a pause."""
        self._stall_paused = False
        if not self._finalized and not self._closed and self._enabled:
            self._reset_stall_watchdog()

    def finalize(self, error: bool = False) -> None:
        """Swap to the terminal emoji and stop every timer. Idempotent."""
        if self._finalized or self._closed or not self._enabled:
            return
        self._finalized = True
        self._cancel_debounce()
        self._cancel_stall_timers()
        self._spawn(self._do_finalize(error))

    async def close(self) -> None:
        """Cancel every timer and drain the in-flight sink calls. Idempotent.

        Timers go first so nothing new is scheduled, then the calls already in
        flight get one bounded window to land: they are the turn's last emoji
        and worth waiting for, but a channel API that never answers must not
        hold teardown open, and a task still running afterwards would outlive
        the turn that owns it.
        """
        self._closed = True
        self._cancel_debounce()
        self._cancel_stall_timers()
        pending = [task for task in self._tasks if not task.done()]
        self._tasks.clear()
        if not pending:
            return
        _, unfinished = await asyncio.wait(pending, timeout=self._timings.close_drain)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    # ── internal ────────────────────────────────────────────────────

    def _emoji_for(self, phase: str) -> str | None:
        """The emoji for *phase*: its own name is the fallback, so a channel
        that adds a phase without a table entry still shows something."""
        return self._emojis.get(phase, phase)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a sink call as a tracked task, or drop it once closed."""
        if self._closed:
            coro.close()
            return
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _do_finalize(self, error: bool) -> None:
        # Clear the stall mark first: the terminal emoji is the whole story of
        # the turn, and a leftover mark next to it reads as still-stuck.
        if self._stall_emoji:
            await self._remove_stall_emoji(self._stall_emoji)
            self._stall_emoji = None
        await self._swap_emoji(self._emoji_for(PHASE_ERROR if error else PHASE_DONE))

    def _fire_debounce(self) -> None:
        """Timer callback: bridge into the async swap."""
        self._spawn(self._apply_pending())

    async def _apply_pending(self) -> None:
        if self._finalized or self._closed or self._pending_phase is None:
            return
        emoji = self._emoji_for(self._pending_phase)
        self._pending_phase = None
        await self._swap_emoji(emoji)
        self._reset_stall_watchdog()

    async def _swap_emoji(self, new_emoji: str | None) -> None:
        """Remove the current reaction and add *new_emoji* in its place.

        ``None`` means the phase is suppressed: the previous reaction still goes
        away, nothing replaces it.

        SERIALIZED, because each swap is spawned as its own task and the channel
        I/O between the remove and the add is a real round-trip. Two swaps
        interleaving there apply their edits in whichever order the network
        returns, so a queued-to-terminal transition could add the obsolete emoji
        AFTER the terminal one and leave the turn looking permanently in progress.
        The lock also makes the no-op check meaningful: read outside it, it
        compares against a value another swap is midway through changing.
        """
        async with self._swap_lock:
            if new_emoji == self._current_emoji:
                return
            old = self._current_emoji
            self._current_emoji = new_emoji
            if old:
                await self._sink_call("remove", old)
            if new_emoji is None:
                return
            await self._sink_call("add", new_emoji)

    async def _sink_call(self, action: str, emoji: str) -> None:
        """Apply one emoji edit, swallowing whatever the channel raises.

        A reaction is decoration: a missing message, a revoked scope, or a rate
        limit the channel refused to queue must not surface as a failed turn.
        """
        try:
            if action == "add":
                await self._sink.add(emoji)
            else:
                await self._sink.remove(emoji)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("status reaction %s %r failed", action, emoji, exc_info=True)

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
        if not self._enabled or self._closed:
            return
        self._cancel_stall_timers()
        if self._stall_emoji:
            stale, self._stall_emoji = self._stall_emoji, None
            self._spawn(self._remove_stall_emoji(stale))
        if self._stall_paused or self._finalized:
            return
        if self._stall.soft is not None:
            self._stall_soft_handle = self._loop.call_later(
                self._timings.stall_soft, self._on_stall_soft
            )
        if self._stall.hard is not None:
            self._stall_hard_handle = self._loop.call_later(
                self._timings.stall_hard, self._on_stall_hard
            )

    async def _remove_stall_emoji(self, emoji: str) -> None:
        await self._sink_call("remove", emoji)

    def _on_stall_soft(self) -> None:
        self._spawn(self._add_stall_emoji(self._stall.soft))

    def _on_stall_hard(self) -> None:
        self._spawn(self._add_stall_emoji(self._stall.hard))

    async def _add_stall_emoji(self, emoji: str | None) -> None:
        if emoji is None or self._finalized or self._closed:
            return
        # The hard mark replaces the soft one rather than joining it.
        if self._stall_emoji and self._stall_emoji != emoji:
            await self._sink_call("remove", self._stall_emoji)
        self._stall_emoji = emoji
        await self._sink_call("add", emoji)
