"""The helpers the Knowledge Library routes share: the owner gate on the mutating routes,
the SEL audit, and the app's store, pipeline and task registry.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import (  # noqa: F401 -- the knowledge facade serves both
    require_owner_dashboard_request,
    require_owner_for_dashboard_caller,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")


def _sel_log(tool: str, **kwargs: object) -> None:
    """Emit SEL audit event for knowledge API mutations."""
    sel().log_tool_invocation(
        session_key="dashboard",
        agent="knowledge-api",
        tool_name=f"knowledge.{tool}",
        outcome=str(kwargs.pop("outcome", "completed")),
        resources=str(kwargs) if kwargs else "",
    )


async def _audited_write(fn, *, event: str, fields=None):
    """Run a store write and its audit line in ONE worker take.

    A client disconnect cancels the handler at its ``await``, so an audit line
    written after the worker returned is dropped for a mutation that already
    committed. Emitting it inside the worker closes that window: the thread runs
    to completion regardless of the coroutine's fate, and SEL writes are
    lock-guarded, so writing from a worker is safe. ``delete_item`` and
    ``import_bundle`` use the same shape inline.

    *fn* is the whole call (bind arguments with ``functools.partial``) and
    *fields* is the audit payload. A record naming something the write produced,
    such as a new source id, does not fit that shape: those callers audit from
    their own sync helper instead (``_create_source_audited``). Returns whatever
    *fn* returned.
    """

    def _write_and_audit():
        result = fn()
        _sel_log(event, **(fields or {}))
        return result

    return await asyncio.to_thread(_write_and_audit)


async def _require_knowledge_owner(request: web.Request, operation: str) -> web.Response | None:
    """Owner gate for the knowledge routes, reads and writes alike, or ``None`` to proceed.

    The library is the owner's: its items, sources, graph and exports. A read is
    gated like a write, because a disclosed document cannot be taken back.

    Owner identity is a property of a dashboard-user request: ``app == ""`` is
    the class ``is_owner_dashboard_request`` can rule on at all, so only that
    class is gated here. Every caller whose ``app`` is not ``""`` passes this
    gate and stays under the control that already governs it:

    - an app token (the Notes app declares ``/api/knowledge``) is confined to
      its manifest's declared paths by ``_enforce_app_scope``;
    - an ``X-Internal-Secret`` loopback caller is admitted on the secret
      itself. ``token_auth`` leaves ``app`` absent for a caller acting as the
      person, and sets ``app`` to the owning app's id for a caller it resolves
      to an app; neither arm runs ``_enforce_app_scope``.
    """
    return await require_owner_for_dashboard_caller(request, operation)


def _store(request: web.Request):
    return request.app["state"].knowledge_store


def _pipeline(request: web.Request):
    return request.app.get("knowledge_pipeline")


def _task_registry(app: web.Application, key: str) -> set:  # type: ignore[type-arg]
    """The strong-reference set that keeps detached tasks alive under *key*.

    ``setup_knowledge_routes`` seeds both registries while the app is still
    mutable, so on a running server this is a plain read. The create branch
    exists for an ``Application`` that skipped registration (a handler driven
    directly, before any runner froze it); on a frozen app it would be the
    deprecated post-start write, which is exactly what seeding at registration
    avoids.
    """
    tasks = app.get(key)
    if tasks is None:
        tasks = set()
        app[key] = tasks
    return tasks


def _track_scan_task(app: web.Application, task: asyncio.Task) -> None:  # type: ignore[type-arg]
    """Keep strong reference to scan task and log exceptions."""
    tasks = _task_registry(app, "_scan_tasks")
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(
        lambda t: (
            logger.exception("scan_source failed", exc_info=t.exception())
            if not t.cancelled() and t.exception()
            else None
        )
    )
