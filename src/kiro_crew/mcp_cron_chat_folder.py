"""The sidebar chat folder ``cron_add`` and ``cron_update`` file a job's chat in.

The Schedule form's save rules, applied to the tools: the shape and existence check,
the Bot-home refusal, the existence re-check and the job write inside one hold of the
folder-store lock, and the save-time move of the job's existing tab. Every rule is the
form's own helper in :mod:`kiro_crew.dashboard.handlers.cron_jobs`, after the refusal
every agent sidebar write gets from a session a channel can drive
(``channel_reachable_refusal``, asked of the caller's verified session); this module
only carries the call there.

That carry is needed because the tool body runs in a worker thread of
``POST /api/crons/tools`` while the folder tree, its lock and the job's tab belong to
the route's event loop. The route names itself to the call it dispatches through
:data:`_ROUTE_CALL`, a ``ContextVar`` set inside that one dispatch, and each check is
run on the route's loop. The route runs the call on its own pool
(:mod:`kiro_crew.dashboard.cron_tools_pool`), so a wait here for the route's loop
never holds a worker the loop's own work needs. A call that reaches the store without
the route (the attended CLI writing the host store directly) has no folder tree to
check against, so it refuses a chat folder rather than saving one unchecked.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.cron import CronJob

_T = TypeVar("_T")

#: The tools route's request and event loop, for the one tool call it dispatches.
_ROUTE_CALL: ContextVar[tuple[web.Request, asyncio.AbstractEventLoop] | None] = ContextVar(
    "kirocrew_cron_tools_route_call", default=None
)

_NEEDS_GATEWAY = (
    "Error: chat_folder_id needs the running gateway, which checks that the folder "
    "exists. Start the gateway and retry, or leave chat_folder_id out."
)


def set_route_call(
    request: web.Request, loop: asyncio.AbstractEventLoop
) -> contextvars.Token[tuple[web.Request, asyncio.AbstractEventLoop] | None]:
    """Mark the current tool call as dispatched by ``POST /api/crons/tools``."""
    return _ROUTE_CALL.set((request, loop))


def reset_route_call(
    token: contextvars.Token[tuple[web.Request, asyncio.AbstractEventLoop] | None],
) -> None:
    _ROUTE_CALL.reset(token)


def _on_route_loop(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, _T]) -> _T:
    """Run *coro* on the route's loop and wait for it from this worker thread."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        coro.close()
        raise RuntimeError("a cron tool's chat folder check must run off the route's loop")
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def _error_text(response: web.Response) -> str:
    """The ``error`` message of one of the form's refusal responses."""
    return str(json.loads(response.text or "{}").get("error", ""))


def resolve_chat_folder(
    value: object,
    *,
    session_key: str = "",
    agent_id: str = "",
    member_id: str = "",
    job: CronJob | None = None,
) -> tuple[str, str | None]:
    """Check a tool's ``chat_folder_id``: ``(folder id, None)`` or ``("", "Error: ...")``.

    *job* is the job a ``cron_update`` edits (its store and current folder decide
    the Bot-home refusal); a ``cron_add`` passes the *session_key*, *agent_id* and
    *member_id* it creates the job with, and the refusal uses the store the job
    will bind to. A new job that cannot bind raises the add's own ``ValueError``.
    ``None`` or ``""`` is "not filed".
    """
    route = _ROUTE_CALL.get()
    blank = value is None or (isinstance(value, str) and not value.strip())
    # A new job left unfiled has nothing to check; clearing an existing job's
    # folder still asks whether its tab may leave a Bot's home.
    if blank and (job is None or route is None):
        return "", None
    if route is None:
        return "", _NEEDS_GATEWAY
    request, loop = route
    if job is None:
        from kiro_crew.cron_service.identity import cron_capture_execution

        execution = cron_capture_execution(session_key, "", agent_id, member_id)
        memory_store, current, operation = execution.store.legacy_name, "", "cron.create"
    else:
        memory_store = str(getattr(job, "memory_store", "") or "")
        current = str(getattr(job, "chat_folder_id", "") or "")
        operation = "cron.update"

    async def _check() -> tuple[str, str | None]:
        from kiro_crew.dashboard.chat_folders_records import (
            _caller_reaches_a_channel,
            channel_reachable_refusal,
        )
        from kiro_crew.dashboard.handlers import cron_jobs
        from kiro_crew.dashboard.token_auth import KNOWN_INTERNAL_CALLERS

        # Filing or moving the job's tab reshapes the person's sidebar, which no
        # agent may do from a session a channel can drive. Every caller of this
        # route is an agent, admitted by the secret or by an agent-socket
        # capability, so its verified session is asked, not the audit origin,
        # and its row names the listed X-Internal-Caller either way.
        if _caller_reaches_a_channel(request, request.headers.get("X-Session-Key", "")):
            named = (request.headers.get("X-Internal-Caller") or "").strip()
            contained = channel_reachable_refusal(
                request,
                operation,
                f"chat_folder_id={value}",
                caller=named if named in KNOWN_INTERNAL_CALLERS else "unknown-internal",
            )
            return "", f"Error: {_error_text(contained)}"
        state = request.app["state"]
        folder_id, refused = cron_jobs._resolve_chat_folder_id(state, value)
        if refused is None:
            refused = cron_jobs._refuse_mate_home_chat_folder(
                request,
                state,
                folder_id,
                memory_store,
                operation,
                current,
                caller="mcp",
                source="mcp",
            )
        if refused is not None:
            return "", f"Error: {_error_text(refused)}"
        return folder_id, None

    return _on_route_loop(loop, _check())


def write_holding_chat_folder(folder_id: str, write: Callable[[], _T]) -> _T:
    """Run the job *write* inside the form's hold of the folder-store lock.

    The hold re-checks that *folder_id* still exists, so a folder deleted since
    :func:`resolve_chat_folder` refuses the save with the form's
    ``unknown_chat_folder`` message (a ``ValueError``, as the store's own
    refusals are). Without a folder nothing is held.

    The write runs on THIS thread, under the caller's own context, while the
    route's loop holds the lock and waits for it: handing it to another worker
    would need a second executor thread per call inside the hold, and calls
    blocked here holding every worker would then wait on each other forever.
    This thread is a cron tools pool worker, never a default-executor one, so
    the lock's other holders and :func:`move_job_tab`'s slot save, which run
    default-executor jobs while it waits, always find a worker.
    """
    route = _ROUTE_CALL.get()
    if not folder_id or route is None:
        return write()
    request, loop = route
    entered: concurrent.futures.Future[asyncio.Future[_T]] = concurrent.futures.Future()

    async def _held() -> _T:
        from kiro_crew.dashboard.handlers import cron_jobs

        async def _persist() -> _T:
            finished: asyncio.Future[_T] = loop.create_future()
            entered.set_result(finished)
            return await finished

        try:
            return await cron_jobs._persist_holding_folder(
                request.app["state"], folder_id, _persist
            )
        except cron_jobs._ChatFolderGone:
            raise ValueError(_error_text(cron_jobs._unknown_chat_folder_response())) from None

    held = asyncio.run_coroutine_threadsafe(_held(), loop)
    # The hold either lets the write in, or ends first (the folder is gone).
    waiting: tuple[concurrent.futures.Future[Any], ...] = (entered, held)
    concurrent.futures.wait(waiting, return_when=concurrent.futures.FIRST_COMPLETED)
    if entered.done():
        finished = entered.result()
        try:
            result = write()
        except BaseException as exc:
            loop.call_soon_threadsafe(finished.set_exception, exc)
        else:
            loop.call_soon_threadsafe(finished.set_result, result)
    return held.result()


def move_job_tab(job: CronJob, transition: dict[str, str]) -> None:
    """Move the job's existing tab as the form's save does, after the store committed.

    *transition* is the store's ``chat_folder_transition_out`` sink: it names the
    prior folder only when the update changed ``chat_folder_id``.
    """
    route = _ROUTE_CALL.get()
    if route is None or "chat_folder_was" not in transition:
        return
    request, loop = route
    from kiro_crew.dashboard.handlers import cron_jobs

    _on_route_loop(
        loop,
        cron_jobs.move_cron_job_tab(
            request.app["state"],
            job,
            transition["chat_folder_was"],
            str(getattr(job, "chat_folder_id", "") or ""),
        ),
    )
