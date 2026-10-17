"""The manager keeps working limits when a reload hands it input it cannot use.

``kiro_crew/subagent_parts/config_surface.py`` holds ``SubagentManager.reconfigure``,
``apply_limits`` and the cap-raise hook. A reload that cannot size the auto cap keeps the
cap in force, a limit whose value does not parse keeps the limit already in force, and a
broken cap-raise hook cannot block the raise it rings.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.subagent as subagent
from kiro_crew.config.loader import KiroCrewConfig

pytestmark = pytest.mark.usefixtures("close_subagent_managers")

_LIMIT_FIELDS = {
    "subagent_max_turns": "_default_turn_limit",
    "subagent_timeout_secs": "_default_timeout",
    "subagent_stall_idle_secs": "_stall_idle_secs",
    "subagent_spawn_stagger_secs": "_spawn_stagger_secs",
    "subagent_result_ttl_secs": "_result_ttl_secs",
    "subagent_queue_max_wait_secs": "_subagent_queue_max_wait_secs",
}


def _manager(max_concurrent: int) -> subagent.SubagentManager:
    mgr = subagent.SubagentManager(
        sessions=MagicMock(), ctx_builder=MagicMock(), max_concurrent=max_concurrent
    )
    mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
    return mgr


def _unsizable(_cfg: object) -> int:
    raise OSError("host memory unreadable")


@pytest.mark.asyncio
async def test_a_reload_that_cannot_size_the_cap_keeps_the_cap_in_force(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mgr = _manager(4)
    monkeypatch.setattr(subagent, "resolve_max_subagents", _unsizable)
    with caplog.at_level(logging.WARNING, logger=subagent.__name__):
        await mgr.reconfigure(KiroCrewConfig())
    assert mgr.user_max_concurrent == 4
    assert "keeping the current cap" in caplog.text


def test_applying_limits_without_a_cap_keeps_the_cap_when_sizing_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mgr = _manager(5)
    monkeypatch.setattr(subagent, "resolve_max_subagents", _unsizable)
    with caplog.at_level(logging.WARNING, logger=subagent.__name__):
        mgr.apply_limits(KiroCrewConfig())
    assert mgr.user_max_concurrent == 5
    assert "keeping max_concurrent=5" in caplog.text


@pytest.mark.parametrize("bad", ["not a number", None], ids=["value-error", "type-error"])
def test_a_limit_that_does_not_parse_keeps_the_limit_in_force(bad: object) -> None:
    mgr = _manager(4)
    before = {attr: getattr(mgr, attr) for attr in _LIMIT_FIELDS.values()}
    real = KiroCrewConfig().agent
    agent = SimpleNamespace(
        completion_keep=real.completion_keep,
        completion_keep_chars=real.completion_keep_chars,
        **dict.fromkeys(_LIMIT_FIELDS, bad),
    )
    mgr.apply_limits(SimpleNamespace(agent=agent), max_concurrent=4)  # type: ignore[arg-type]
    assert {attr: getattr(mgr, attr) for attr in _LIMIT_FIELDS.values()} == before


def test_a_broken_cap_raise_hook_does_not_block_the_raise() -> None:
    mgr = _manager(6)
    mgr.set_effective_cap(2)
    rung: list[int] = []

    def hook() -> None:
        rung.append(mgr.max_concurrent)
        raise RuntimeError("hook broke")

    mgr.set_cap_raise_listener(hook)
    assert mgr.set_effective_cap(3) == 3
    assert rung == [3]
    assert mgr.max_concurrent == 3
