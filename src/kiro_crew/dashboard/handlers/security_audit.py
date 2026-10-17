"""The SEL audit and the write-path pieces the Settings > Security write endpoints share.

``_audit`` records each request's outcome in the Security Event Log through
``_sel``, which resolves the log at call time; ``ConfigCorruptError`` is the
refusal a writer raises instead of overwriting a store it cannot read; and
``_run_off_loop`` moves an endpoint's blocking file I/O off the gateway's event
loop. The facade's denied-commands routes and the trusted-apps and trusted-registries
owners import them from here.

Served through :mod:`kiro_crew.dashboard.handlers.security`, which callers and
tests import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger("kiro_crew.dashboard.handlers.security")


def _sel():
    """Late-binding ``_sel()`` for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


def _audit(request: web.Request, *, operation: str, outcome: str, resources: str = "") -> None:
    """Best-effort SEL audit; a logging failure never breaks the request."""
    try:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


class ConfigCorruptError(Exception):
    """denied_commands.json exists but is unreadable/not-a-dict — refuse to mutate.

    A mutation that read a corrupt file as ``{}`` and wrote it back would
    silently reset the opt-out state. The write path raises this so the handler
    returns 500 instead of clobbering a populated-but-unparseable file.
    """


async def _run_off_loop(fn):
    """Run a blocking (filesystem) callable in the default thread executor.

    The denied-command lookups read ``denied_commands.json`` and walk the
    governance profile store — blocking I/O that must not run on aiohttp's sole
    event loop (a slow/stalled FS would freeze every request + heartbeat).
    """
    return await asyncio.get_running_loop().run_in_executor(None, fn)


# Named by the path callers import it from: the facade that serves it.
ConfigCorruptError.__module__ = "kiro_crew.dashboard.handlers.security"
