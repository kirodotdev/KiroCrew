"""The live-routes fixture shared by the ``test_pptx_maker_routes*.py`` modules.

``_RoutesFixture`` serves the real ``register_routes`` behind a middleware that
stamps the dashboard owner, over a temp deck root, so every module drives the
same app the gateway mounts.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from kiro_crew.apps.builtins.pptx_maker.backend import paths, routes
from kiro_crew.testing.wait import async_wait_until

_OWNER = "owner-user"


@web.middleware
async def _as_owner(request: web.Request, handler):
    """Stamp the dashboard owner's identity, as the auth middleware would.

    The mutating routes are owner only, so every request in these tests arrives
    as the configured owner (``user == owner_id`` with an empty app claim).
    """
    request["user"] = _OWNER
    request["app"] = ""
    return await handler(request)


def _enabled(value: bool):
    """Patch the app-enablement gate the ``_require_enabled`` wrapper consults."""
    return mock.patch.object(routes, "is_app_enabled", return_value=value)


class _RoutesFixture(AioHTTPTestCase):
    """A live aiohttp app with the real routes and a temp deck root."""

    async def get_application(self) -> web.Application:
        app = web.Application(middlewares=[_as_owner])
        app["state"] = SimpleNamespace(owner_id=_OWNER)
        routes.register_routes(app)
        return app

    async def asyncSetUp(self) -> None:
        # Cleanups, not teardown: a setup step that raises skips asyncTearDown,
        # and the temp tree and the deck-root override must still be undone.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "decks"
        self.deck = self.root / "20260101-demo"
        (self.deck / "specs").mkdir(parents=True)
        (self.deck / "specs" / "brief.md").write_text("the brief", encoding="utf-8")
        (self.deck / "compose").mkdir()
        (self.deck / "compose" / "intro_1.json").write_text("{}", encoding="utf-8")
        (self.deck / "output.pptx").write_bytes(b"PK\x03\x04")
        deck_root = mock.patch.dict(os.environ, {paths.DECK_ROOT_ENV: str(self.root)})
        deck_root.start()
        self.addCleanup(deck_root.stop)
        await super().asyncSetUp()

    def url(self, path: str) -> str:
        return f"{routes.API_PREFIX}{path}"


async def _await_detached(predicate: Callable[[], bool]) -> None:
    """Wait for detached work that runs in a THREAD, not just on the loop.

    ``asyncio.sleep(0)`` only yields to the event loop, so it cannot wait for anything
    handed to ``run_in_executor``: the executor thread may not have been scheduled at
    all yet. The polled wait yields real time between reads, so the thread gets
    somewhere to run, and a genuinely broken detach fails by name rather than hanging.
    """
    await async_wait_until(predicate)
