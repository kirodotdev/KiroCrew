"""Qoder's stale-friendly catalog refresh for the dashboard model-list endpoint.

The handler owns the dashboard-facing state and policy constants; this owner holds
the one scheduling routine so ``handlers/agents.py`` stays below the repository's
file-line cap. All collaborators are injected by the handler wrapper, so tests that
patch the handler's import surface keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from kiro_crew import model_registry
from kiro_crew.agent_sdk.drivers.acp import model_registry_namespace

logger = logging.getLogger(__name__)


#: How long one GET /api/models waits for a catalog before answering the degraded
#: 503. Unchanged: a remote hub still budgets its whole cold path against this
#: bound (see ``api_models``), so raising it would move
#: ``DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS`` with it.
_LIST_MODELS_SUBPROCESS_TIMEOUT_SECS: float = 10.0

#: Qoder's own catalog is account/release dependent, but its picker can update
#: without a live session. One service-owned probe per interval is enough; the
#: retry gate prevents an unreachable service from being hammered by the UI poll.
_QODER_CATALOG_TTL_SECONDS: float = 600.0
_QODER_CATALOG_RETRY_SECONDS: float = 30.0
_QODER_CATALOG_REFRESH_STATE: dict[str, asyncio.Task[object] | float | None] = {
    "task": None,
    "started_at": 0.0,
}


#: How long the SHARED background catalog fetch may run, decoupled from any one
#: request. On a host where a cold `kiro-cli --list-models` spawn always exceeds
# the request bound, killing it at that bound meant no call ever finished, so
#: every 8s poll started another doomed cold spawn and the picker never left
#: `auto`. The fetch now outlives the request that started it: the request still
#: answers 503 after _LIST_MODELS_SUBPROCESS_TIMEOUT_SECS, but the spawn keeps
#: running under this ceiling and the next poll is served from its result.
_LIST_MODELS_BACKGROUND_TIMEOUT_SECS: float = 90.0
#: A cached catalog younger than this is served straight away; an older one is
#: served straight away too and refreshed behind the reply (stale-while-
#: revalidate). The catalog changes rarely, so a few minutes stale costs nothing
#: the per-request entitlement narrowing does not already correct.
_LIST_MODELS_CATALOG_TTL_SECS: float = 300.0
#: A fetch that spawned kiro-cli and failed (signed out, non-zero exit, timeout,
#: bad output) is remembered this long, and every poll inside the window gets the
#: same 503 without a new spawn or a new WARNING. The dashboard re-polls about
#: every 8s while the list is degraded, so a signed-out CLI cost one spawn and one
#: WARNING per poll (81 of each in 16 minutes on one gateway). 30s holds a failing
#: CLI to two spawns a minute, while a user who has just signed in waits at most
#: 30s for the picker to fill. A success clears the entry at once.
_LIST_MODELS_FAILURE_BACKOFF_SECS: float = 30.0


async def maybe_refresh_qoder_catalog(
    backend: str,
    *,
    state: dict[str, asyncio.Task[object] | float | None],
    ttl_seconds: float,
    retry_seconds: float,
    request_timeout_secs: float,
    refresh_catalog: Callable[[str], Awaitable[list[dict[str, str]]]],
) -> None:
    """Refresh Qoder's picker catalog behind a bounded, stale-friendly read.

    A fresh observed-at stamp means the endpoint can answer immediately. After
    the TTL, one shared task starts; every overlapping request awaits that task
    for at most the ordinary model-list request bound. If the probe times out,
    the shielded task finishes under its own launch bound while this request
    returns the stale snapshot. A recent failure is retried only after the retry
    gate, and an empty answer never replaces the cached catalog.
    """
    observed = model_registry.advertised_models_observed_at(model_registry_namespace(backend))
    if observed and time.time_ns() - observed < ttl_seconds * 1_000_000_000:
        return
    task = state.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=request_timeout_secs)
        except asyncio.TimeoutError:
            logger.debug("qoder catalog refresh did not finish before the read bound")
        except Exception:
            logger.debug("qoder catalog refresh failed before the read bound", exc_info=True)
        return
    now = time.monotonic()
    started_at = state.get("started_at")
    if isinstance(started_at, (int, float)) and started_at > 0 and now - started_at < retry_seconds:
        return
    state["started_at"] = now

    async def _bounded_refresh() -> object:
        rows = await asyncio.wait_for(
            refresh_catalog(backend), timeout=_LIST_MODELS_BACKGROUND_TIMEOUT_SECS
        )
        return bool(rows)

    task = asyncio.ensure_future(_bounded_refresh())
    state["task"] = task
    refreshed: object = None
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=request_timeout_secs)
    except asyncio.TimeoutError:
        logger.info("qoder catalog refresh slower than the model-list read bound; serving stale")
    except Exception:
        logger.debug("qoder catalog refresh failed; serving stale", exc_info=True)
    else:
        # A non-empty refresh is success. Let the next TTL expiry start another
        # probe immediately instead of suppressing it for the failure-retry
        # window. Empty is failure/no evidence and keeps the retry gate armed.
        refreshed = await task
        if refreshed:
            state["started_at"] = 0.0
    finally:

        def _on_done(fut: object) -> None:
            if state.get("task") is task:
                state["task"] = None
            if isinstance(fut, asyncio.Task):
                consume_refresh_exception(fut)

        task.add_done_callback(_on_done)


def consume_refresh_exception(task: "asyncio.Task[list[dict]]") -> None:
    """Retrieve a finished refresh task's exception so it is never 'unhandled'.

    A cached catalog means a degraded background refresh is non-fatal — the
    request path already served the last-good list — so its exception is logged
    at debug and swallowed rather than left for ``Task.__del__`` to escalate.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("api_models: background catalog refresh failed (serving cached)", exc_info=exc)
