"""A module-local ``asyncio`` whose ``sleep`` a test supplies.

A module that does ``import asyncio`` holds the stdlib module itself, so
``monkeypatch.setattr(mod.asyncio, "sleep", fake)`` rebinds ``asyncio.sleep`` for
the whole worker: the event loop, pytest-asyncio and every other task in flight
then sleep through the fake too. Rebinding the owning module's ``asyncio`` NAME to
this namespace changes what that one module calls and leaves the stdlib module
alone::

    monkeypatch.setattr(git_coord, "asyncio", asyncio_with_sleep(AsyncMock()))
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable


class _AsyncioWithSleep:
    """Every attribute is the stdlib ``asyncio``'s, except ``sleep``."""

    def __init__(self, sleep: Callable[..., Any]) -> None:
        self.sleep = sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


def asyncio_with_sleep(sleep: Callable[..., Any]) -> Any:
    """A stand-in for one module's ``asyncio`` that calls ``sleep`` for its sleeps."""
    return _AsyncioWithSleep(sleep)
