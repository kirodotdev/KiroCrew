"""The Shared Crew release gate: ``KIROCREW_SHARED_CREW_PREVIEW=1``.

Until the owner ships Shared Crew, nothing in it may be switched on from
config by accident. With the variable unset (the default) every
``dashboard.shared_crew`` switch reads as its default-off value whatever
config.json says, so the gateway behaves as it did before Shared Crew. Set
to ``1`` in the gateway's environment, config decides as before. An
environment variable only: no config field, Settings row or change card can
turn it on.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Awaitable, Callable
from typing import Any

#: The gateway environment variable that opts this host into the preview.
PREVIEW_ENV = "KIROCREW_SHARED_CREW_PREVIEW"


def preview_enabled() -> bool:
    """Whether this process runs the Shared Crew preview. Exactly ``1`` turns it on."""
    return os.environ.get(PREVIEW_ENV, "") == "1"


def preview_only(handler: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
    """404 while the Shared Crew preview is off (``KIROCREW_SHARED_CREW_PREVIEW``).

    The route answers as if it did not exist, so nothing here can be used, or
    turned on, by accident before the owner ships Shared Crew. It lives here,
    not with the invite routes, so a boot-path module can gate one route
    without importing a preview-only module.
    """

    @functools.wraps(handler)
    async def gated(request: Any) -> Any:
        if not preview_enabled():
            from aiohttp import web

            raise web.HTTPNotFound()
        return await handler(request)

    return gated


def no_maintenance() -> None:
    """The post-bind maintenance starter a server uses while the preview is off."""
    return None


__all__ = ["PREVIEW_ENV", "no_maintenance", "preview_enabled", "preview_only"]
