"""A ``slack.shared_access`` edit reaches a running gateway with no restart.

The owner lists a Slack member id in ``slack.shared_access.user_ids`` (``kirocrew
config set``, ``config edit`` or an applied hand edit). The live config watcher
reloads ``config.json`` and the orchestrator's Slack applier copies the new
``slack`` section onto the shared config object every Slack read goes through
(``handler.slack_cfg()``). So the next message from a newly listed person is
admitted, and one from a person taken off the list, or with the switch off, is
refused, all in the same process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import cfg_file_fixture  # noqa: F401 -- the cfg_file fixture, by name

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import handler, shared_access
from kiro_crew.slack.gateway import GatewayOrchestrator

OWNER = "UOWNER0001"
FRIEND = "UFRIEND001"
NEWCOMER = "UNEWCOMER1"
TEAM = "T0000TEAM1"


class _FakeWeb:
    """``auth.test`` / ``users.info`` for two real people in the bot's workspace."""

    async def auth_test(self) -> dict[str, Any]:
        return {"team_id": TEAM}

    async def users_info(self, user: str) -> dict[str, Any]:
        return {"user": {"team_id": TEAM, "name": user, "profile": {"display_name": user}}}


def _doc(*, enabled: bool = True, ids: tuple[str, ...]) -> dict:
    return {"slack": {"shared_access": {"enabled": enabled, "user_ids": list(ids)}}}


async def _admitted(user_id: str) -> bool:
    """What the event path asks for a message from *user_id*: check it, then gate it."""
    await shared_access.prime_shared_user(user_id)
    return handler.is_shared_user(user_id)


@pytest.fixture
def gateway(cfg_file: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    """A Slack-less orchestrator subscribed on the process watcher, booted on *cfg_file*."""
    _write(cfg_file, _doc(ids=(FRIEND,)))
    boot = KiroCrewConfig.load()
    with patch.object(boot, "load_credentials", return_value={"KIROCREW_OWNER_ID": OWNER}):
        orch = GatewayOrchestrator(boot, no_dashboard=True, no_crons=True, no_open=True)
    _floor_monkeypatch.setattr(handler, "_owner_id", OWNER)
    _floor_monkeypatch.setattr(handler, "_orch_cfg", orch._cfg)
    live.watch().prime(orch._cfg)
    shared_access.install(_FakeWeb())
    yield orch
    shared_access.reset()


@pytest.mark.asyncio
async def test_an_added_id_is_admitted_after_a_reload_with_no_restart(
    gateway: GatewayOrchestrator, cfg_file: Path
) -> None:
    assert await _admitted(FRIEND) is True
    assert await _admitted(NEWCOMER) is False

    _write(cfg_file, _doc(ids=(FRIEND, NEWCOMER)))
    change = await live.watch().refresh_now()

    assert change is not None and change.touched("slack.shared_access.user_ids")
    assert handler.slack_cfg() is gateway._cfg  # the same object, updated in place
    assert await _admitted(NEWCOMER) is True
    assert await _admitted(FRIEND) is True


@pytest.mark.asyncio
async def test_a_removed_id_is_refused_after_a_reload(
    gateway: GatewayOrchestrator, cfg_file: Path
) -> None:
    assert await _admitted(FRIEND) is True  # confirmed and cached while listed

    _write(cfg_file, _doc(ids=()))
    assert await live.watch().refresh_now() is not None

    assert await _admitted(FRIEND) is False


@pytest.mark.asyncio
async def test_switching_shared_access_off_refuses_everyone_after_a_reload(
    gateway: GatewayOrchestrator, cfg_file: Path
) -> None:
    assert await _admitted(FRIEND) is True

    _write(cfg_file, _doc(enabled=False, ids=(FRIEND,)))
    assert await live.watch().refresh_now() is not None

    assert await _admitted(FRIEND) is False
    assert handler.is_owner(OWNER) is True
