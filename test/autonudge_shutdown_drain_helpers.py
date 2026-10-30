"""Shared hang-guard helper for the autonudge shutdown-drain topic modules.

Split out of ``test_autonudge_shutdown_drain.py`` so each sibling topic module
imports the same lost-run ceiling and wait helper instead of carrying its own copy.
"""

from __future__ import annotations

import asyncio

import pytest

_LOST_RUN_SECS = 30.0


async def _wait_until(predicate, what: str) -> None:
    """Wait until *predicate* is true, failing by name if the run is wedged."""

    async def _spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_spin(), timeout=_LOST_RUN_SECS)
    except asyncio.TimeoutError:
        pytest.fail(f"{what} did not happen within the lost-run ceiling")
