"""In-process transport for the gateway's own chat tags/slots state.

The health and auto-resume loops run INSIDE the gateway process, started by
``hooks.on_startup``. An earlier revision dialed the gateway's own loopback
HTTP surface (``/api/chat/...``) with an internal shared-secret header — but a
loop authenticating to the very process it runs in is the wrong architecture,
and it broke as the loopback-secret helper's own docstring warns: the loop
read the shared secret file while a different listener generation owned the
port it dialed, so every 60 s cycle was rejected and no health tagging ever
ran (the pod's systemd unit exported none of the bound-port environment
variables either, so port resolution had nothing to anchor on).

This module deletes that hop. It reaches the live :class:`DashboardState`
directly — the same in-memory store the ``/api/chat/*`` handlers mutate — and
carries no shared secret, no port, and no HTTP client at all. It IS the
transport layer, kept out of ``logic.py`` (the portability seam): an
external-app packaging swaps only this file for the cron ``ScriptContext``
HTTP transport, and ``logic.py`` stays byte-identical.

Method surface is identical to the retired loopback client so the sync
orchestration in ``hooks.py`` (``_seed_vocabulary`` / ``_health_pass`` /
``_find_resume_candidates``) is untouched and its tests keep driving a plain
mock. Reads are in-memory dict scans, safe to call from the worker thread the
loops offload onto. WRITES, however, touch a loop-bound tag lock and the
async slot-save path, both owned by the gateway's serving loop — so each write
is marshalled onto that loop with ``run_coroutine_threadsafe`` and awaited.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.apps.builtins.chat_status_tags import logic

logger = logging.getLogger(__name__)


class GatewayUnavailable(RuntimeError):
    """The live gateway state (or its serving loop) is not bound yet; the
    caller should skip this tick rather than act blindly."""


def _state() -> Any:
    """Resolve the live DashboardState in-process, or raise GatewayUnavailable.

    The route registry retains the running aiohttp application (``_app``), and
    the gateway hangs the shared :class:`DashboardState` off ``app["state"]`` —
    the same object every ``/api/chat/*`` handler reads. Any link missing means
    the gateway has not finished wiring (or is a bare test app), which is a
    skip, not an error.
    """
    from kiro_crew.apps.hooks_integration import get_route_registry

    reg = get_route_registry()
    app = getattr(reg, "_app", None) if reg is not None else None
    state = app.get("state") if app is not None else None
    if state is None:
        raise GatewayUnavailable("gateway dashboard state is not bound yet")
    return state


def _run_on_loop(state: Any, coro: Any) -> Any:
    """Run *coro* on the gateway's serving loop from a worker thread and wait.

    Tag writes acquire a :class:`LoopBoundLock` and slot persistence is async —
    both belong to the serving loop. The health/resume loops offload their
    sweep with ``asyncio.to_thread``, so a write issued from that worker thread
    is scheduled back onto the serving loop here and its result awaited.
    """
    loop = getattr(state, "serving_loop", None)
    if loop is None or loop.is_closed():
        raise GatewayUnavailable("gateway serving loop is not available")
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


class TagsStore:
    """In-process facade over the gateway's chat tags/slots state.

    Same synchronous surface the loops (and their tests) expect. Reads hit the
    in-memory store directly; writes marshal onto the serving loop.
    """

    # ── reads (in-memory, worker-thread safe) ─────────────────────────

    def list_tags(self) -> list[dict]:
        return [dict(t) for t in _state()._tags]

    def list_slots(self) -> list[dict]:
        # serialize_slots() yields the exact payload dicts the old
        # GET /api/chat/slots returned (key, running, tags, last_ts,
        # last_activity_ts, queue_depth, …) — what logic.py consumes.
        return _state().serialize_slots()

    def slot_messages(self, key: str, limit: int) -> list[dict]:
        slot = _state()._slots.get(key)
        if slot is None:
            return []
        return [dict(m) for m in slot.messages[-limit:]]

    # ── writes (marshalled onto the serving loop) ─────────────────────

    def create_tag(self, name: str, color: str, *, status: bool) -> dict:
        """Create a tag, idempotent by case-insensitive name (server-assigned id)."""
        state = _state()

        async def _do() -> dict:
            from kiro_crew.dashboard import chat_tags

            return await chat_tags.create_tag_definition_off_loop(state, name, color, status=status)

        return _run_on_loop(state, _do())

    def merge_slot_tags(self, key: str, managed_ids: set[str], want_ids: set[str]) -> bool:
        """Replace only the MANAGED subset of a slot's tags, atomically.

        The merge runs against the slot's LIVE tag list inside the tags write
        lock — not against a snapshot the caller took earlier — so a user edit
        that lands between the sweep's read and this write is preserved rather
        than clobbered. Returns True when the write changed anything.
        """
        state = _state()

        async def _do() -> bool:
            from kiro_crew.dashboard import chat_tags
            from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

            async with chat_tags.tags_write_lock(state):
                slot = state._slots.get(key)
                if not slot:
                    return False
                valid_ids = {t.get("id") for t in state._tags}
                new = logic.merge_tags(
                    list(slot.tags), managed_ids, {t for t in want_ids if t in valid_ids}
                )
                if new == list(slot.tags):
                    return False
                slot.tags = new
                await save_slot_off_loop(state, slot, force=True)
            state.push_slots_update()
            return True

        return bool(_run_on_loop(state, _do()))

    def send_message(self, key: str, message: str) -> None:
        """Inject a user turn into an existing slot (the auto-resume path).

        Mirrors the in-process send in ``session_control``: enqueue-or-run the
        prompt through the standard turn runner. A missing slot is a no-op.
        """
        state = _state()

        async def _do() -> None:
            from kiro_crew.dashboard.chat_runner import _run_chat

            slot = state._slots.get(key)
            if slot is None:
                return
            slot.enqueue_or_run_prompt(message, _run_chat, state)
            state.push_slots_update()

        _run_on_loop(state, _do())
