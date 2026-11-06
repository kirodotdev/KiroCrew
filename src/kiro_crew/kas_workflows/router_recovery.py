"""The KAS workflow router's run ledger, reconnects and boot recovery.

Each function here whose first parameter is ``self`` is a
:class:`~kiro_crew.kas_workflows.router.KasWorkflowRouter` method: the class binds it
by name (``recover_on_boot = _r_recovery.recover_on_boot``), so it reads and writes the
router's own state, and a patch of the method on the class or on an instance reaches
every caller. Nothing here redacts: ``reattach``, whose lines name runs and KAS's
errors, stays in ``router.py``, the module the security posture registry names as the
sink.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import weakref
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.router_base import (
    BOOT_RECOVERY_CONCURRENCY,
    BOOT_RECOVERY_MAX_CHATS,
    LAUNCH_TTL_SECS,
    LEDGER_MAX_RUNS,
    MAX_PENDING_LAUNCHES,
    RECONNECT_INTERVAL_SECS,
    RECONNECT_MAX_TRIES,
    KasWorkflowError,
    _alive,
    session_binding,
)

if TYPE_CHECKING:
    from kiro_crew.kas_workflows.router import KasWorkflowRouter

#: The router's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.kas_workflows.router")


async def ensure_ledger(self: KasWorkflowRouter) -> None:
    """Read the run ledger once, off the loop."""
    if self._ledger_loaded:
        return
    async with self._ledger_lock:
        if self._ledger_loaded:
            return
        path = self._ledger_path
        loaded: dict[str, dict[str, Any]] = {}
        if path is not None:
            try:
                raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                data = json.loads(raw)
                runs = data.get("runs") if isinstance(data, dict) else None
                for wid, entry in (runs or {}).items():
                    if (
                        isinstance(wid, str)
                        and isinstance(entry, dict)
                        and isinstance(entry.get("slot"), str)
                    ):
                        loaded[wid] = {
                            "slot": entry["slot"],
                            "cwd": str(entry.get("cwd") or ""),
                            "createdAt": float(entry.get("createdAt") or 0),
                            "ended": bool(entry.get("ended")),
                            "workspace": str(entry.get("workspace") or ""),
                        }
            except FileNotFoundError:
                pass
            except Exception:  # noqa: BLE001 -- a bad ledger loses links, not runs
                logger.warning("KAS workflow run ledger unreadable; starting empty")
        # Entries written since startup win over what disk held.
        self._ledger = {**loaded, **self._ledger}
        self._ledger_loaded = True


async def _save_ledger(self: KasWorkflowRouter) -> None:
    path = self._ledger_path
    if path is None:
        return
    await self.ensure_ledger()
    from kiro_crew.atomic_write import atomic_write

    async with self._ledger_save_lock:
        if len(self._ledger) > LEDGER_MAX_RUNS:
            # Oldest finished runs first, then oldest of all.
            order = sorted(
                self._ledger,
                key=lambda w: (not self._ledger[w].get("ended"), self._ledger[w]["createdAt"]),
            )
            for wid in order[: len(self._ledger) - LEDGER_MAX_RUNS]:
                self._ledger.pop(wid, None)
        # Snapshot INSIDE the lock: a save that waited here writes the ledger
        # as it is now, so the last write to finish is the newest state.
        payload = json.dumps({"version": 1, "runs": self._ledger}, indent=1, sort_keys=True)
        try:
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(atomic_write, path, payload, mode=0o600)
        except Exception:  # noqa: BLE001 -- the in-memory ledger still serves
            logger.warning("KAS workflow run ledger not saved", exc_info=True)


def _undriven_live(self: KasWorkflowRouter, slot_key: str | None = None) -> list[str]:
    """Ledger runs not known finished and with no live driving process."""
    out = []
    for wid, entry in self._ledger.items():
        if entry.get("ended") or (slot_key is not None and entry["slot"] != slot_key):
            continue
        if self.mirror.status(wid) in wire.TERMINAL_RUN_STATUSES:
            continue
        if _alive(self._run_runtime.get(wid)) is None:
            out.append(wid)
    return out


def prepare_reconnect(self: KasWorkflowRouter, slot_key: str) -> str:
    """A single-use reconnect message for *slot_key*'s next turn."""
    now = self._clock()
    for token in [t for t, (_s, at) in self._reconnects.items() if at < now - LAUNCH_TTL_SECS]:
        self._reconnects.pop(token, None)
    while len(self._reconnects) >= MAX_PENDING_LAUNCHES:
        self._reconnects.pop(next(iter(self._reconnects)))
    token = secrets.token_hex(16)
    self._reconnects[token] = (slot_key, now)
    return f"Reconnect workflow runs (reconnect {token})"


def _take_reconnect(self: KasWorkflowRouter, slot_key: str, token: str) -> bool:
    held = self._reconnects.get(token)
    if held is None or held[0] != slot_key:
        return False
    self._reconnects.pop(token, None)
    return True


def _step_cwd(self: KasWorkflowRouter, workflow_id: str, runtime: Any) -> str:
    """The directory a step session is loaded in: its run's project, else the process's."""
    return str((self._ledger.get(workflow_id) or {}).get("cwd") or "") or str(
        getattr(runtime, "_work_dir", "") or ""
    )


async def _reask_plans(
    self: KasWorkflowRouter, runtime: Any, workflow_id: str, sessions: list[str]
) -> bool:
    """Load each step session holding a kept plan approval, so KAS asks it again.

    The step's runtime is recorded first: the re-asked request arrives right
    after the load answers and must reach this router, not the runtime's
    auto-reject. True when at least one load was accepted.
    """
    cwd = self._step_cwd(workflow_id, runtime)
    if not cwd:
        return False
    ref = weakref.ref(runtime)
    asked = False
    for session_id in sessions:
        self._route_step(session_id, ref)
        try:
            await self._call(
                runtime,
                wire.METHOD_SESSION_LOAD,
                {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
            )
        except KasWorkflowError:
            logger.info("plan approval step session did not load for %s", workflow_id)
            continue
        asked = True
    return asked


def schedule_reattach(self: KasWorkflowRouter, slot_key: str, provider: Any) -> None:
    """On an ordinary chat turn, re-attach that chat's undriven runs in the background."""
    if not self._ledger_loaded or not self._undriven_live(slot_key):
        return
    binding = session_binding(provider)
    if binding is None:
        return
    runtime, session_id = binding

    async def _go() -> None:
        try:
            await self.reattach(slot_key, runtime, session_id)
        except Exception:  # noqa: BLE001 -- best effort; the dashboard retries
            logger.info("background workflow reattach failed for %s", slot_key)

    self._spawn(_go())


def due_reconnects(self: KasWorkflowRouter) -> list[str]:
    """Chats whose undriven runs are due an automatic reconnect turn, marked tried."""
    now = self._clock()
    due = []
    # A chat no ledger run names any more has no reconnect left to count.
    slots = {entry["slot"] for entry in self._ledger.values()}
    for slot_key in [s for s in self._reconnect_tries if s not in slots]:
        self._reconnect_tries.pop(slot_key, None)
    for slot_key in sorted({self._ledger[w]["slot"] for w in self._undriven_live()}):
        tries, last = self._reconnect_tries.get(slot_key, (0, 0.0))
        if tries >= RECONNECT_MAX_TRIES or now - last < RECONNECT_INTERVAL_SECS:
            continue
        self._reconnect_tries[slot_key] = (tries + 1, now)
        due.append(slot_key)
    return due


def reset_reconnect_tries(self: KasWorkflowRouter, slot_key: str) -> None:
    self._reconnect_tries.pop(slot_key, None)


async def _owner_verdict(self: KasWorkflowRouter, workflow_id: str) -> str:
    """KAS's own word on whether *workflow_id* has a live owner.

    ``_kiro/workflow/load`` on a process that does not hold the run serves
    it from disk, read-only (kiro-agent ``workflow-handlers.ts``: "read
    paths reconcile stale-running state but never register or claim
    ownership"): a run whose owner stopped beating past KAS's window comes
    back ``paused`` with :data:`STALE_RUNNING_PAUSE_REASON`, one a live
    owner drives stays ``running``. Returns ``recover`` (paused: nobody
    drives it), ``live``, ``finished``, ``other:<status>`` or ``unreadable``.
    """
    try:
        loaded = await self._call(
            await self._reader(), wire.METHOD_LOAD, {"workflowId": workflow_id}
        )
    except KasWorkflowError:
        return "unreadable"
    if self.mirror.apply_load(loaded) is not None:
        self.revision += 1
    state = wire.as_mapping(loaded.get("state")) or {}
    status = wire.as_str(state.get("status")) or ""
    if status in wire.TERMINAL_RUN_STATUSES:
        self._note_ended(workflow_id)
        return "finished"
    if status == wire.STATUS_PAUSED:
        return "recover"
    if status == wire.STATUS_RUNNING:
        return "live"
    return f"other:{status or 'unknown'}"


async def recover_on_boot(
    self: KasWorkflowRouter,
    chat_workspace: Callable[[str], str | None],
    reconnect: Callable[[str], Awaitable[bool]],
    *,
    max_chats: int = BOOT_RECOVERY_MAX_CHATS,
    concurrency: int = BOOT_RECOVERY_CONCURRENCY,
) -> list[tuple[str, str, str]]:
    """Reattach this gateway's own interrupted runs, once, at startup.

    Only runs in Crew's own ledger are considered (a run another client
    started is never in it), only those not known finished and not driven
    by a live process here, and only those KAS itself reports ownerless
    (:meth:`_owner_verdict`): a run another live owner holds is left alone.
    KAS's ``_kiro/workflow/resumeAll`` is deliberately NOT used: it resumes
    every interrupted run in the process's scope, IDE and other clients'
    runs included, which would take over work that is not Crew's.

    Runs are grouped by chat and each chat gets ONE *reconnect* (the
    reconnect turn a chat's own poll already sends; it dispatches no model
    prompt), at most *concurrency* at a time and *max_chats* per boot, most
    recent first. *chat_workspace* names a chat's workspace, or None when
    the chat does not exist any more; a chat that is gone, or whose workspace is
    not the one its run was started in, is skipped, never recreated.

    Returns ``(workflowId, slot, outcome)`` per run considered; each is
    also logged.
    """
    await self.ensure_ledger()
    outcomes: list[tuple[str, str, str]] = []

    def note(wid: str, slot_key: str, outcome: str) -> None:
        outcomes.append((wid, slot_key, outcome))
        logger.info("KAS workflow boot recovery: run %s (chat %s) %s", wid, slot_key, outcome)

    by_chat: dict[str, list[str]] = {}
    for wid in self._undriven_live():
        slot_key = self._ledger[wid]["slot"]
        verdict = await self._owner_verdict(wid)
        if verdict == "recover":
            by_chat.setdefault(slot_key, []).append(wid)
        elif verdict == "live":
            note(wid, slot_key, "skipped: a live owner still holds it")
        elif verdict == "finished":
            note(wid, slot_key, "skipped: already finished")
        elif verdict == "unreadable":
            note(wid, slot_key, "skipped: Kiro Agent could not read it")
        else:
            note(wid, slot_key, f"skipped: not paused ({verdict.split(':', 1)[1]})")

    def newest(slot_key: str) -> float:
        return max(self._ledger[w]["createdAt"] for w in by_chat[slot_key])

    chats = sorted(by_chat, key=newest, reverse=True)
    for slot_key in chats[max_chats:]:
        for wid in by_chat[slot_key]:
            note(wid, slot_key, f"skipped: over the {max_chats}-chat boot cap")
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(slot_key: str) -> None:
        runs = by_chat[slot_key]
        workspace = chat_workspace(slot_key)
        if workspace is None:
            for wid in runs:
                note(wid, slot_key, "skipped: its chat no longer exists")
            return
        mine = [w for w in runs if self._ledger[w].get("workspace") in ("", workspace)]
        for wid in runs:
            if wid not in mine:
                note(wid, slot_key, "skipped: started in another workspace")
        if not mine:
            return
        async with gate:
            # Counts as this chat's first automatic try, so the dashboard
            # poll does not send a second reconnect on top of this one.
            self._reconnect_tries[slot_key] = (1, self._clock())
            try:
                sent = await reconnect(slot_key)
            except Exception:  # noqa: BLE001 -- one chat must not stop the rest
                logger.info("KAS workflow boot reconnect failed for %s", slot_key)
                sent = False
        outcome = "reattached" if sent else "skipped: its chat could not take the turn"
        for wid in mine:
            note(wid, slot_key, outcome)

    await asyncio.gather(*(one(s) for s in chats[:max_chats]))
    return outcomes
