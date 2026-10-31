"""A completed run whose kill failed keeps ``leaked_process`` on every delivery route.

A completed run whose teardown could not end its process keeps its clean record
(``outcome`` ``completed``) and is flagged ``SubagentInfo._leaked_process``. When
its completion reaches the parent directly, the report writes the ``delivered``
tombstone with the flag. On a dashboard parent the completion is QUEUED, held for
a wave digest, or left as an undelivered notice instead, and the gateway's ledger
owes the ``delivered`` mark: each owed ``SubagentDelivery`` carries the flag so the
drain's tombstone keeps it and the next start still ends the survivor
(``list_leaked_processes``). Without it the tombstone ends the run and nothing
reaches the process.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_gateway_subagent_wiring import _Done, harness, make_info

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.slack.gateway import GatewayOrchestrator, _compose_subagent_completion
from kiro_crew.subagent import SubagentDelivery, SubagentManager
from kiro_crew.subagent_persistence import (
    create_agent_folder,
    list_leaked_processes,
    settle_delivered_batch,
    write_result_chunk,
)


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """Point the sub-agent registry at a temp directory."""
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


def _finished_run(agent_id: str) -> None:
    """A completed run's folder: state + a non-empty result.txt, no tombstone."""
    create_agent_folder(agent_id, task="t", parent_session="dashboard:main")
    write_result_chunk(agent_id, "the full transcript\n")


def _leaked(agent_id: str, **fields: Any):
    """A completed run whose teardown kill failed: clean record, flagged survivor."""
    info = make_info(agent_id, **fields)
    info._leaked_process = True
    assert info.outcome == "completed" and not info.error
    return info


def _tombstone(root, agent_id: str) -> dict[str, Any]:
    return json.loads((root / agent_id / "tombstone.json").read_text())


def _assert_flagged_on_disk(root, agent_id: str) -> None:
    tombstone = _tombstone(root, agent_id)
    assert tombstone["cause"] == "delivered"
    assert (
        tombstone.get("leaked_process") is True
    ), "the delivered tombstone dropped the flag: the next start never ends the survivor"
    assert tombstone.get("outcome") == "completed"
    assert agent_id in [state["id"] for state in list_leaked_processes()]


class TestTheQueuedRouteKeepsTheFlag:
    @pytest.mark.asyncio
    async def test_a_queued_leaked_completion_settles_a_flagged_tombstone(self, agent_root) -> None:
        slot = _ChatSlot("s1")
        info = _leaked("a1")
        _finished_run(info.id)
        announce = _compose_subagent_completion(info, _flush_only=False).announce

        GatewayOrchestrator._defer_queued_delivery(slot, announce, info, flush_only=False)
        owed = slot.take_pending_subagent_deliveries([announce])
        assert [(d.agent_id, d.leaked_process) for d in owed] == [("a1", True)]

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        await manager.settle_queued_delivery(owed)

        _assert_flagged_on_disk(agent_root, "a1")
        # What the parent was told: a completion carrying its result.
        assert "completed" in announce and "the result" in announce
        assert "kill failed" not in announce

    @pytest.mark.asyncio
    async def test_a_queued_clean_completion_settles_an_unflagged_tombstone(
        self, agent_root
    ) -> None:
        """Control: only the flagged run's tombstone carries ``leaked_process``."""
        slot = _ChatSlot("s1")
        info = make_info("a2")
        _finished_run(info.id)
        announce = _compose_subagent_completion(info, _flush_only=False).announce

        GatewayOrchestrator._defer_queued_delivery(slot, announce, info, flush_only=False)
        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        await manager.settle_queued_delivery(slot.take_pending_subagent_deliveries([announce]))

        tombstone = _tombstone(agent_root, "a2")
        assert tombstone["cause"] == "delivered"
        assert "leaked_process" not in tombstone
        assert list_leaked_processes() == []


class TestTheDigestHeldRouteKeepsTheFlag:
    @pytest.mark.asyncio
    async def test_a_held_leaked_member_settles_a_flagged_tombstone(
        self, done: _Done, agent_root
    ) -> None:
        slot = done.tab("main")
        slot.running = True
        _finished_run("w0")

        await done.done(_leaked("w0", batch_id="wl", batch_total=2))
        await done.done(make_info("w1", batch_id="wl", batch_total=2))

        owed = [delivery for _content, deliveries in slot.owed for delivery in deliveries]
        held = [d for d in owed if d.agent_id == "w0"]
        assert [d.leaked_process for d in held] == [True], owed

        settle_delivered_batch(held)

        _assert_flagged_on_disk(agent_root, "w0")


class TestTheUndeliveredNoticeKeepsTheFlag:
    @pytest.mark.asyncio
    async def test_an_undelivered_leaked_completion_settles_a_flagged_tombstone(
        self, done: _Done, agent_root
    ) -> None:
        loaded = MagicMock(name="loaded tab", _pending_subagent_failures=[], window_pending=False)
        done.state.get_slot = MagicMock(return_value=loaded)
        _finished_run("a3")

        await done.manager_kwargs["on_event"](
            "subagent_injection_failed",
            _leaked("a3"),
            {"error": "timed out", "failure_msg": "report"},
        )

        (_content, deliveries), _kw = loaded.note_pending_subagent_delivery.call_args
        assert [(d.agent_id, d.leaked_process) for d in deliveries] == [("a3", True)]

        settle_delivered_batch(deliveries)

        _assert_flagged_on_disk(agent_root, "a3")


def test_an_unflagged_delivery_settles_with_no_extra_fields() -> None:
    """The batch settle passes nothing new for an ordinary completion."""
    writer = MagicMock()

    settle_delivered_batch([SubagentDelivery("plain", 1.0, 0.5)], writer=writer)

    writer.assert_called_once_with("plain", elapsed=1.0, credits=0.5)
