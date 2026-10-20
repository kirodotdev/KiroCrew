"""``POST /api/search/segment``: dictionary words for a session search in mcp-core.

The gateway holds the host's one copy of the Chinese dictionary
(:mod:`kiro_crew.history_words`). Each mcp-core process asks here instead of
loading its own, which would cost every process about 45 MB.

Machine-only. The path is in ``server._STRICT_INTERNAL_API_PATHS``, and this
handler re-checks both halves of that grant itself, because neither is
self-enforcing: with the secret header absent the middleware falls through to
cookie auth, and a ``local_only=False`` deployment reclassifies strict paths as
mixed. So a caller must be local (loopback or the gateway's unix socket) AND have
passed the constant-time secret check (``request["internal_auth"]``). A wrong
secret is refused by the middleware before this runs.

Body ``{"runs": [str, ...]}``: 1..``CJK_SEGMENT_MAX_RUNS`` pure-Han runs of
2..``CJK_SEGMENT_MAX_RUN_CHARS`` characters, the same bounds the client applies.
Answers ``{"ok": true, "words": [[str, ...], ...]}`` in run order; ``400`` for any
other body; ``403`` for a caller that is not a local process holding the secret;
``503`` when the dictionary cannot load, which the client reads as "keep the
character path".
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.origin import is_loopback, request_is_unix_socket
from kiro_crew.history_search import CJK_SEGMENT_MAX_RUN_CHARS, CJK_SEGMENT_MAX_RUNS, is_han_run
from kiro_crew.history_words import segment_runs
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def _refuse(request: web.Request, code: str, error: str) -> web.Response:
    """A 403 for a caller outside the machine grant, recorded in the Security Event Log."""
    try:
        sel().log_api_access(
            caller="unknown",
            operation="search.segment",
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error=error,
        )
    except Exception:
        # A failed audit write must not turn the refusal into a 500.
        logger.warning("Failed to log a search-segment denial to SEL", exc_info=True)
    return web.json_response({"ok": False, "code": code, "error": error}, status=403)


def _valid_runs(body: object) -> list[str] | None:
    """The ``runs`` list of *body* when every bound holds, else ``None``."""
    runs = body.get("runs") if isinstance(body, dict) else None
    if not isinstance(runs, list) or not 0 < len(runs) <= CJK_SEGMENT_MAX_RUNS:
        return None
    for run in runs:
        if not isinstance(run, str) or not 1 < len(run) <= CJK_SEGMENT_MAX_RUN_CHARS:
            return None
        if not is_han_run(run):
            return None
    return runs


async def api_search_segment(request: web.Request) -> web.Response:
    """Split the posted pure-Han runs into dictionary words."""
    if not (request_is_unix_socket(request) or is_loopback(request.remote or "")):
        return _refuse(request, "loopback_only", "local callers only")
    if request.get("internal_auth") is not True:
        return _refuse(
            request,
            "internal_secret_required",
            "only a local Kiro Crew process holding the internal secret may call this",
        )
    try:
        body = await request.json()
    except Exception:
        # JSONDecodeError, or a body that is not UTF-8.
        body = None
    runs = _valid_runs(body)
    if runs is None:
        return web.json_response(
            {
                "ok": False,
                "code": "invalid_runs",
                "error": (
                    f"'runs' must be a list of 1..{CJK_SEGMENT_MAX_RUNS} Han-only strings "
                    f"of 2..{CJK_SEGMENT_MAX_RUN_CHARS} characters"
                ),
            },
            status=400,
        )
    try:
        # Off the loop: the first call loads the dictionary (about 0.15 s).
        words = await asyncio.to_thread(segment_runs, runs)
    except Exception:
        logger.warning("search segmenter failed", exc_info=True)
        words = None
    if words is None:
        return web.json_response(
            {"ok": False, "code": "segmenter_unavailable", "error": "no dictionary"},
            status=503,
        )
    return web.json_response({"ok": True, "words": words})
