"""Mastermind's place in the gateway: the tree's worker, its summarizer, and
the turn hooks the dashboard runner calls for the Mastermind session.

The Mastermind session is the dashboard slot keyed ``slot.MASTERMIND_SLOT_KEY``,
and its turns run as Mastermind while Mastermind is on (``slot.enabled``). They
differ from any other session's turns in three places, and only there:

* before the turn, its history replay is off and the request is prefixed with
  the memory view and the last messages whole (:func:`turn_prefix`);
* when a turn lands, its message and the reply join the tree
  (:func:`turn_landed`), and their summaries are written in the background on
  the selected backend (``run_bg_oneliner``: tool-free, a fresh background
  session per call), on the model of ``agent.role_models.mastermind``
  (:func:`summary_model`);
* at the end of a landed turn its backend conversation is discarded
  (``slot.turn_ended``), so the next turn starts fresh and the view is its only
  memory.

A slot in a private memory mode keeps its privacy: an incognito or temporary
turn is never written to the tree, and a temporary one is not shown it.

The tree lives under ``<data home>/mastermind/``; every read and write of it
runs on the tree's own worker thread (``worker.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.mastermind.constants import KIND_AGENT, KIND_EVENT, KIND_USER
from kiro_crew.mastermind.prompts import VIEW_GUIDE
from kiro_crew.mastermind.slot import SERVICE_ATTR, tree_dir
from kiro_crew.mastermind.summarizer import Summarizer
from kiro_crew.mastermind.tree import view_high_for
from kiro_crew.mastermind.worker import TreeWorker

logger = logging.getLogger(__name__)

#: The ``agent.role_models`` role the summaries run as.
MASTERMIND_ROLE = "mastermind"

#: Seconds one summary call may take before it is given up and retried.
_SUMMARY_TIMEOUT_SECS = 180.0

#: Openings of the messages automation writes into a chat (recorded as events).
_AUTO_NUDGE_PREFIX = "[auto-nudge cycle"


class MastermindService:
    """The tree's worker and summarizer, and what a turn under way owes them."""

    def __init__(self, worker: TreeWorker, summarizer: Summarizer) -> None:
        self.worker = worker
        self.summarizer = summarizer
        # The message of the turn under way, by slot key: (kind, text). Set
        # before the turn and taken when it lands. A turn that does not land
        # keeps it for the recovery that continues it; the next turn the person
        # starts, or a slash command, replaces or drops it.
        self.pending: dict[str, tuple[str, str]] = {}
        # Slots whose turn landed and so owe a conversation discard at its end.
        self.landed: set[str] = set()
        self._writes: set[asyncio.Task[None]] = set()

    def end_turn(self, slot_key: str) -> bool:
        """The turn of *slot_key* ended: whether it landed (and so owes a discard)."""
        if slot_key in self.landed:
            self.landed.discard(slot_key)
            return True
        return False

    async def status(self) -> dict[str, Any]:
        def read(t: Any) -> dict[str, Any]:
            return {
                "messages": t.count,
                "pending_summaries": t.pending(),
                "view_bytes": t.view.size,
                "view_lines": len(t.view.parts),
            }

        return await self.worker.run(read)

    async def settle_writes(self) -> None:
        """Wait for the tree writes scheduled so far (tests and shutdown)."""
        while self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)

    def write(self, records: list[tuple[str, str]]) -> None:
        """Append *records* to the tree in the background, then summarize."""

        async def run() -> None:
            try:
                await self.worker.run(lambda t: [t.append(kind, text) for kind, text in records])
            except Exception:  # noqa: BLE001 -- memory must never break the turn
                logger.warning("mastermind: could not record the turn", exc_info=True)
                return
            self.summarizer.kick()

        task = asyncio.get_running_loop().create_task(run())
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def close(self) -> None:
        await self.summarizer.close()
        await self.settle_writes()
        self.worker.shutdown()


def service_for(state: Any) -> MastermindService:
    """The gateway's Mastermind service, made on first use (its tree loads on
    the worker, at its first call)."""
    service = getattr(state, SERVICE_ATTR, None)
    if service is None:
        worker = TreeWorker(tree_dir())
        service = MastermindService(worker, Summarizer(worker, _summarize_with(state)))
        setattr(state, SERVICE_ATTR, service)
        service.summarizer.kick()
    return service


def summary_model() -> str:
    """The model summaries run on: ``agent.role_models.mastermind``, else
    ``"auto"`` (never the chat's model). Blocking: reads the config."""
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load().agent.resolve_model(MASTERMIND_ROLE)


def _summarize_with(state: Any):
    async def summarize(prompt: str) -> str:
        from kiro_crew.llm_helpers import run_bg_oneliner

        model = await asyncio.to_thread(summary_model)
        return await run_bg_oneliner(
            state.sessions,
            prompt,
            model=model,
            sel_source="mastermind_summary",
            timeout=_SUMMARY_TIMEOUT_SECS,
        )

    return summarize


def message_kind(text: str) -> str:
    """``event`` for a message automation wrote (a cron notification, a subagent
    report, a monitor wake, an auto-nudge), else ``user``."""
    from kiro_crew.dashboard.state import (
        CRON_NOTIFY_PREFIX,
        MONITOR_WAKE_PREFIX,
        SUBAGENT_COMPLETION_PREFIXES,
    )

    prefixes = (
        CRON_NOTIFY_PREFIX,
        MONITOR_WAKE_PREFIX,
        _AUTO_NUDGE_PREFIX,
        *SUBAGENT_COMPLETION_PREFIXES,
    )
    return KIND_EVENT if text.lstrip().startswith(prefixes) else KIND_USER


async def turn_prefix(
    state: Any, slot: Any, text: str, *, synthetic: bool, window_tokens: int | None = None
) -> str:
    """The memory a Mastermind turn starts with, and remember *text* (the turn's
    own message) for :func:`turn_landed`.

    A synthetic turn (a recovery, a synthesis) continues the turn before it, so
    it keeps the message that turn remembered. A temporary slot gets no memory
    and an incognito or temporary one leaves none. The view is sized to the
    model's context window, *window_tokens* (``tree.view_high_for``)."""
    service = service_for(state)
    if not slot.is_restricted and not synthetic:
        service.pending[slot.key] = (message_kind(text), text)
    if slot.blocks_reads:
        return ""
    high = view_high_for(window_tokens)

    def read(t: Any) -> tuple[str, str]:
        t.set_high(high)
        return t.render(budget=high), t.last_messages()

    view, recent = await service.worker.run(read)
    return VIEW_GUIDE + "\n\n" + view + ("\n" + recent if recent else "") + "\n\n"


def turn_landed(state: Any, slot: Any, reply_mids: list[str]) -> None:
    """A Mastermind turn landed: its message and reply join the tree (in the
    background) and its conversation is owed a discard. Never raises."""
    try:
        service = service_for(state)
        service.landed.add(slot.key)
        pending = service.pending.pop(slot.key, None)
        if pending is None:
            return
        records = [pending]
        reply = _reply_text(slot, reply_mids)
        if reply:
            records.append((KIND_AGENT, reply))
        service.write(records)
    except Exception:  # noqa: BLE001 -- memory must never break the turn
        logger.warning("mastermind: could not record the turn", exc_info=True)


def _reply_text(slot: Any, reply_mids: list[str]) -> str:
    """The turn's spoken reply rows, joined in order."""
    from kiro_crew.dashboard.member_message_notes import is_member_message
    from kiro_crew.dashboard.state import row_mid

    mids = set(reply_mids)
    parts = [
        str(row.get("content") or "").strip()
        for row in slot.messages
        if row_mid(row) in mids and is_member_message(row)
    ]
    return "\n\n".join(p for p in parts if p)


async def close(state: Any) -> None:
    service = getattr(state, SERVICE_ATTR, None)
    if service is not None:
        await service.close()
