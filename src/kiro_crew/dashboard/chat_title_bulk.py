from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_USER,
    _TITLE_PROMPT_ROLES,
    _is_member_thread,
    _model_manual_title,
    apply_regenerated_title,
)
from kiro_crew.dashboard.chat_utils_display import _redact_for_display
from kiro_crew.dashboard.create_rate_limit import GENERATE_TITLE, allow_create, has_create_budget
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import load_window_off_loop
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

BULK_TITLE_CONCURRENCY = 2

_run_ids = itertools.count(1)

_RETITLED = "retitled"
_UNCHANGED = "unchanged"
_FAILED = "failed"


@dataclass
class _BulkTitleRun:
    id: int
    total: int
    retitled: int = 0
    unchanged: int = 0
    failed: int = 0
    failed_sessions: list[dict[str, str]] = field(default_factory=list)

    def record_failure(self, slot: Any) -> None:
        self.failed += 1
        title = _redact_for_display(slot.title or "")
        self.failed_sessions.append({"key": slot.key, "title": title})

    @property
    def done(self) -> int:
        return self.retitled + self.unchanged + self.failed

    @property
    def finished(self) -> bool:
        return self.done >= self.total

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "total": self.total,
            "done": self.done,
            "retitled": self.retitled,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "failed_sessions": list(self.failed_sessions),
            "finished": self.finished,
        }


def _current_run(state: DashboardState) -> _BulkTitleRun | None:
    run = getattr(state, "_bulk_title_run", None)
    return run if isinstance(run, _BulkTitleRun) else None


@dataclass
class _BulkTitlePlan:
    queued: list[Any] = field(default_factory=list)
    skipped_running: list[str] = field(default_factory=list)
    skipped_renamed: list[str] = field(default_factory=list)
    skipped_recent: list[str] = field(default_factory=list)
    skipped_empty: list[str] = field(default_factory=list)
    renamed: int = 0

    def to_json(self, *, dry_run: bool, run: _BulkTitleRun | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "dry_run": dry_run,
            "run_id": run.id if run is not None else None,
            "queued": [slot.key for slot in self.queued],
            "skipped_running": self.skipped_running,
            "skipped_renamed": self.skipped_renamed,
            "skipped_recent": self.skipped_recent,
            "skipped_empty": self.skipped_empty,
            "renamed": self.renamed,
        }


def _is_busy(slot: Any) -> bool:
    return bool(slot.running or slot.linked_turn_running)


def _has_conversation(slot: Any) -> bool:
    return any(m.get("role") in _TITLE_PROMPT_ROLES for m in slot.sidebar_messages())


def plan_bulk_titles(state: DashboardState, *, include_renamed: bool) -> _BulkTitlePlan:
    plan = _BulkTitlePlan()
    under_construction = _under_construction(state)
    for key, slot in list(state._slots.items()):
        if slot.is_closing or key in under_construction or _is_member_thread(slot):
            continue
        renamed = slot._title_origin == _TITLE_ORIGIN_USER
        if _is_busy(slot):
            plan.skipped_running.append(key)
            continue
        if renamed:
            plan.renamed += 1
            if not include_renamed:
                plan.skipped_renamed.append(key)
                continue
        if not _has_conversation(slot):
            plan.skipped_empty.append(key)
        elif not has_create_budget(GENERATE_TITLE, key):
            plan.skipped_recent.append(key)
        else:
            plan.queued.append(slot)
    return plan


def _under_construction(state: DashboardState) -> Any:
    return getattr(state, "_slots_under_construction", None) or ()


def _still_current(state: DashboardState, slot: Any) -> bool:
    return (
        state._slots.get(slot.key) is slot
        and not slot.is_closing
        and slot.key not in _under_construction(state)
    )


async def _retitle_one(
    state: DashboardState,
    slot: Any,
    gate: asyncio.Semaphore,
    *,
    epoch: int,
    include_renamed: bool,
) -> str:
    async with gate:
        if slot._title_epoch != epoch or not _still_current(state, slot) or _is_busy(slot):
            return _UNCHANGED
        if _is_member_thread(slot):
            return _UNCHANGED
        if not include_renamed and slot._title_origin == _TITLE_ORIGIN_USER:
            return _UNCHANGED
        await load_window_off_loop(slot)
        if slot._title_epoch != epoch or not _still_current(state, slot):
            return _UNCHANGED
        title = await _model_manual_title(state, slot)
        if not title or slot._title_epoch != epoch or not _still_current(state, slot):
            return _UNCHANGED
        previous = (slot.title, slot._titled, slot._title_origin, slot._title_low_signal)
        applied_epoch = slot._title_epoch + 1
        saved = await apply_regenerated_title(
            state, slot, title, still_current=lambda: _still_current(state, slot)
        )
        if saved:
            return _RETITLED
        if slot._title_epoch == applied_epoch and state._slots.get(slot.key) is slot:
            slot.title, slot._titled, slot._title_origin, slot._title_low_signal = previous
            slot._title_epoch += 1
            state.push_slot_title(slot.key, slot.title)
        return _FAILED


async def _tracked(
    state: DashboardState,
    run: _BulkTitleRun,
    slot: Any,
    gate: asyncio.Semaphore,
    *,
    epoch: int,
    include_renamed: bool,
) -> None:
    try:
        outcome = await _retitle_one(
            state, slot, gate, epoch=epoch, include_renamed=include_renamed
        )
    except asyncio.CancelledError:
        run.record_failure(slot)
        raise
    except Exception:
        run.record_failure(slot)
        logger.warning("Bulk title regeneration failed for slot %s", slot.key, exc_info=True)
        return
    if outcome == _RETITLED:
        run.retitled += 1
    elif outcome == _FAILED:
        run.record_failure(slot)
    else:
        run.unchanged += 1


async def run_bulk_titles(
    state: DashboardState,
    slots: list[Any],
    *,
    include_renamed: bool,
    run: _BulkTitleRun | None = None,
    epochs: dict[str, int] | None = None,
) -> None:
    if run is None:
        run = _BulkTitleRun(id=next(_run_ids), total=len(slots))
    if epochs is None:
        epochs = {slot.key: slot._title_epoch for slot in slots}
    gate = asyncio.Semaphore(BULK_TITLE_CONCURRENCY)
    await asyncio.gather(
        *(
            _tracked(
                state, run, slot, gate, epoch=epochs[slot.key], include_renamed=include_renamed
            )
            for slot in slots
        ),
        return_exceptions=True,
    )


def _refusal(request: web.Request, operation: str) -> web.Response:
    sel().log_api_access(
        caller=str(request.get("app") or "unknown"),
        operation=operation,
        outcome="denied",
        source="app_isolation",
        resources="bulk_titles",
        error="only a dashboard user can regenerate titles",
    )
    return web.json_response({"error": "unauthorized", "code": "forbidden"}, status=403)


async def api_chat_slots_generate_titles_status(request: web.Request) -> web.Response:
    if request.get("app") != "":
        return _refusal(request, "chat.slots_generate_titles_status")
    run = _current_run(request.app["state"])
    return web.json_response({"ok": True, "run": run.to_json() if run is not None else None})


async def api_chat_slots_generate_titles(request: web.Request) -> web.Response:
    if request.get("app") != "":
        return _refusal(request, "chat.slots_generate_titles")
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    include_renamed = body.get("include_renamed", False)
    dry_run = body.get("dry_run", False)
    if not isinstance(include_renamed, bool):
        return web.json_response(
            {"error": "include_renamed must be a boolean", "code": "invalid_include_renamed"},
            status=400,
        )
    if not isinstance(dry_run, bool):
        return web.json_response(
            {"error": "dry_run must be a boolean", "code": "invalid_dry_run"}, status=400
        )

    state: DashboardState = request.app["state"]
    active = _current_run(state)
    if not dry_run and active is not None and not active.finished:
        return web.json_response(
            {
                "error": "bulk_titles_in_progress",
                "code": "bulk_titles_in_progress",
                "run": active.to_json(),
            },
            status=409,
        )
    plan = plan_bulk_titles(state, include_renamed=include_renamed)
    run: _BulkTitleRun | None = None
    if not dry_run:
        admitted = []
        for slot in plan.queued:
            if allow_create(GENERATE_TITLE, slot.key):
                admitted.append(slot)
            else:
                plan.skipped_recent.append(slot.key)
        plan.queued = admitted
        if admitted:
            logger.info("Bulk title regeneration queued for %d sessions", len(admitted))
            sel().log_api_access(
                caller=str(request.get("user") or "dashboard"),
                operation="chat.slots_generate_titles",
                outcome="success",
                source="dashboard",
                resources=f"bulk_titles:{len(admitted)}",
            )
            run = _BulkTitleRun(id=next(_run_ids), total=len(admitted))
            state._bulk_title_run = run
            epochs = {slot.key: slot._title_epoch for slot in admitted}
            task = asyncio.create_task(
                run_bulk_titles(
                    state, admitted, include_renamed=include_renamed, run=run, epochs=epochs
                )
            )
            state._background_tasks.add(task)
            task.add_done_callback(state._background_tasks.discard)
    return web.json_response(plan.to_json(dry_run=dry_run, run=run))
