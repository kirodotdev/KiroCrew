"""A Slack allowlist or tracking click and the opposite click on the same id end as saved.

An add reaches the running set only after its ``config.json`` save; a remove applies at
once and again after its own save, and until that save returns the tracked-channel gate
refuses the id. Here each click's save is held open in its worker thread on its own
event, so the test reads the state with the first save returned and the second still
held, in both orders, for a user and for a channel. The later click wins in the file and
in memory.

Config writes land in the per-test ``KIROCREW_HOME`` that ``conftest.py`` pins.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest
from slack_interactions_coverage_helpers import _mock_aiohttp_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _restore_module_globals  # noqa: F401
from slack_interactions_coverage_helpers import orch_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _payload

from kiro_crew.config.loader import config_path
from kiro_crew.dashboard.handlers.agents import _get_config_lock
from kiro_crew.slack import handler as sh
from kiro_crew.slack import interactions as ix
from kiro_crew.slack.allowlist import (
    ACTION_ALLOWLIST_APPROVE,
    ACTION_ALLOWLIST_DENY,
    ACTION_TRACK_APPROVE,
    ACTION_TRACK_DENY,
)
from kiro_crew.testing.wait import async_wait_until, finished, until_parked

KINDS = {
    "user": ("persist_allowed_user", "allowed_users", "slack_id"),
    "channel": ("persist_tracking_channel", "tracking_channels", "channel_id"),
}


@pytest.mark.parametrize("first", ["add", "remove"])
@pytest.mark.parametrize("kind", ["user", "channel"])
@pytest.mark.asyncio
async def test_the_later_click_wins_in_memory_and_on_disk(
    kind: str, first: str, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, section, field = KINDS[kind]
    if kind == "user":
        handler, add, remove = ix._handle_allowlist, ACTION_ALLOWLIST_APPROVE, ACTION_ALLOWLIST_DENY
    else:
        handler, add, remove = ix._handle_track_channel, ACTION_TRACK_APPROVE, ACTION_TRACK_DENY

    def live() -> set[str]:
        return orch._allowed_users if kind == "user" else orch._tracking_channels

    def saved() -> set[str]:
        return {e[field] for e in json.loads(config_path().read_text("utf-8"))["slack"][section]}

    real = getattr(ix, writer)
    if first == "remove":
        real("X1")
        live().add("X1")
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    def held(*args: object, **kwargs: object) -> None:
        turn = 0 if not entered[0].is_set() else 1
        entered[turn].set()
        release[turn].wait(60)
        real(*args, **kwargs)

    monkeypatch.setattr(ix, writer, held)
    clicks = (add, remove) if first == "add" else (remove, add)
    started: list[asyncio.Task[None]] = []
    try:
        one = asyncio.create_task(handler(_payload(), {"value": "X1:x"}, clicks[0], "", "", "U1"))
        started.append(one)
        await async_wait_until(entered[0].is_set)
        two = asyncio.create_task(handler(_payload(), {"value": "X1:x"}, clicks[1], "", "", "U1"))
        started.append(two)
        await until_parked(_get_config_lock())
        release[0].set()
        await finished(one, "the first click")
        await async_wait_until(entered[1].is_set)

        # First save returned, second held: neither click has put X1 in the running set.
        assert ("X1" in live(), "X1" in saved()) == (False, first == "add")
        if kind == "channel" and first == "add":
            # A config reload of the saved file puts it back, and the gate still refuses it.
            live().update(saved())
            assert "X1" in live() and not sh.is_tracked_channel("X1")
    finally:
        release[0].set()
        release[1].set()
        # Drain both clicks even after a failed assert: one left inside its save at
        # teardown hangs the worker instead of failing by name.
        await finished(asyncio.gather(*started, return_exceptions=True), "both clicks")
    await finished(two, "the second click")

    wanted = first == "remove"
    assert ("X1" in live(), "X1" in saved()) == (wanted, wanted)
    assert sh._removes_in_flight == {}
    if kind == "channel":
        assert sh.is_tracked_channel("X1") is wanted
