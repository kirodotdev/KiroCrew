"""Builders the ``test_slack_gateway_coverage*.py`` suites share.

A ``GatewayOrchestrator`` with mocked credentials, a mocked dashboard state, the
lost-run ceiling for a wait a test must end itself, and the recorded-await lookup.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import gateway as gw

#: Lost-run ceiling for a wait the test itself must end (a turn it parks, a
#: replay it releases). A nudge turn's pre-turn chain makes real executor hops
#: (governance, store, embed pool). Timed wait by wait: at most 0.03 s at ``-n 4``
#: on a loaded 32-CPU host, and at most 4.8 s with every executor hop delayed by
#: 1.2 s (a starved-runner model). 60 s is over ten times that and half the
#: module's ``--timeout=120``, so only a run that never gets there reaches it.
_LOST_RUN_SECS = 60.0


async def _within_lost_run(awaitable, what: str):
    """Await *awaitable* under ``_LOST_RUN_SECS``; on expiry fail naming *what*.

    A bare ``wait_for`` raises an empty ``TimeoutError``. This one says what never
    finished, the ceiling and the elapsed time. A ``TimeoutError`` the awaited code
    raises on its own, before the ceiling, propagates unchanged.
    """
    started = time.monotonic()
    try:
        return await asyncio.wait_for(awaitable, _LOST_RUN_SECS)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        # A loop timer can fire up to one clock tick early (15.6 ms on Windows 3.12).
        # flake-ok: pre-existing lost-run ceiling; elapsed only words the failure of a 60 s wait that already expired; moved verbatim, tracked for a de-flake pass
        if elapsed < _LOST_RUN_SECS - 1.0:
            raise
        pytest.fail(
            f"{what} did not finish within the {_LOST_RUN_SECS:.0f}s lost-run ceiling "
            f"({elapsed:.1f}s elapsed)"
        )


def _awaited(mock: Any) -> Any:
    """The recorded ``Call`` for a single expected await (fails if there was none)."""
    call = mock.await_args
    assert call is not None
    return call


def _make_orchestrator(**kwargs: Any) -> Any:
    """Build a GatewayOrchestrator with mocked credentials (no Slack tokens).

    Returned as ``Any`` on purpose: every test of these suites swaps real collaborators
    (``sessions`` / ``slack`` / ``ctx_builder`` ...) for mocks, which do not
    satisfy the orchestrator's declared attribute types.
    """
    cfg = KiroCrewConfig()
    creds = {"KIROCREW_OWNER_ID": "U_OWNER"}
    with patch.object(cfg, "load_credentials", return_value=creds):
        return gw.GatewayOrchestrator(
            cfg,
            no_dashboard=kwargs.pop("no_dashboard", True),
            no_crons=kwargs.pop("no_crons", True),
            no_open=True,
        )


def _mock_dashboard_state() -> MagicMock:
    ds = MagicMock()
    ds._slots = {}
    ds.last_notification_persist = None
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.broadcast_ws_owners = MagicMock()
    ds.get_slot = MagicMock(return_value=None)
    ds.channel_transports = {}
    return ds


def _loop(key: str = "slack:111.222", **kwargs: Any) -> NudgeLoop:
    return NudgeLoop(
        id=kwargs.pop("id", "loop-1"),
        slot_key=key,
        message=kwargs.pop("message", "keep checking"),
        **kwargs,
    )


_DKEY = "discord:kirocrew:direct:U9"
