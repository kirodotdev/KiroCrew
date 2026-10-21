"""Helpers shared by the three ``test_history_consolidation_retry*.py`` modules.

``KEY`` is the session key the cases use; ``_seed_log`` seeds a real ``ConversationLog``
under ``tmp_path``, and ``_make_consolidator`` builds a ``HistoryConsolidator`` over a log
with a mocked memory store. ``_total`` reads the transcript's message total, ``_span``
builds the ``AttemptedSpan`` a turn over the current transcript would attempt,
``_FakeRequest`` stands in for the manual consolidate handler's request, and
``_off_loop`` runs a transcript mutation the way an ``async def`` test must.
"""

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar
from unittest.mock import MagicMock

from kiro_crew import history as history_mod
from kiro_crew.history import ConversationLog, HistoryConsolidator

KEY = "dashboard:chat-retry"

_T = TypeVar("_T")


async def _off_loop(write: Callable[[], _T]) -> _T:
    """Run a synchronous transcript mutation off the event loop.

    A metadata rewrite (``update_metadata``, ``mark_consolidated``,
    ``rewrite_session``, ``record_consolidation_failure``) ends in
    ``atomic_write.replace_with_retry``, whose Windows sharing-violation retry
    is a single rename attempt on the loop thread BY DESIGN -- it must never
    block the gateway's sole loop. Driving that write straight from an
    ``async def`` test therefore loses to any handle a scanner holds on
    ``sessions/<key>.jsonl`` for a few milliseconds (``[WinError 5]``). Running
    it on a worker thread, which has no loop of its own, keeps the bounded
    retry -- the offload ``history_offload`` documents for async callers.

    Running off the loop also makes the on-loop-persist discipline guard a
    no-op, so an offloaded write needs no ``allow_on_loop_persist()`` wrapper:
    the sanctioned path and the guard agree.
    """
    return await asyncio.to_thread(write)


def _seed_log(tmp_path, key: str = KEY, count: int = 3) -> ConversationLog:
    """A real transcript with *count* unconsolidated messages."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    # These tests run on the event loop; the mutations under test are the
    # production ones (offloaded inside _consolidate), not this fixture setup.
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"m{i}")
    return log


def _make_consolidator(log: ConversationLog, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _total(log: ConversationLog, key: str = KEY) -> int:
    """The transcript's current message total, as an entry point would supply it."""
    return log.consolidation_counts(key)[0]


def _span(
    log: ConversationLog,
    total: int | None = None,
    key: str = KEY,
    prompted: int | None = None,
) -> history_mod.AttemptedSpan:
    """The span identity a turn over the CURRENT transcript would attempt.

    Mirrors what ``_consolidate`` freezes from its pre-turn snapshot, so a test
    charging a failure by hand stamps the same identity production would.
    *prompted* defaults to the whole tail — the unsplit case, where one prompt
    carried every unconsolidated message.
    """
    meta = log.get_metadata(key)
    extent = _total(log, key) if total is None else total
    return history_mod.AttemptedSpan(
        total=extent,
        generation=int(meta.get("rotation_generation", 0) or 0),
        offset=int(meta.get("last_consolidated", 0) or 0),
        prompted=extent if prompted is None else prompted,
    )


class _FakeRequest(dict):
    """Minimal aiohttp request stand-in for the manual consolidate handler."""

    def __init__(self, state: Any, body: dict) -> None:
        super().__init__()
        self.app = {"state": state}
        # The owner's dashboard claims: the manual trigger is an owner-gated write.
        # The subject is ``state.owner_id`` when one is configured, else the signed
        # local bootstrap subject.
        self["app"] = ""
        self["user"] = str(getattr(state, "owner_id", "") or "") or "local-app"
        # The handler's session-recognition gate refuses a request with no
        # X-Session-Key; the browser UI's static key is the recognised caller.
        self.headers: dict[str, str] = {"X-Session-Key": "dashboard:ui"}
        # ``read_bounded_json`` reads both: whether a body is there, and whether
        # it declares JSON (415 when it does not). A real request always has them.
        self.can_read_body = True
        self.content_type = "application/json"
        self._body = body

    async def json(self) -> dict:
        return self._body
