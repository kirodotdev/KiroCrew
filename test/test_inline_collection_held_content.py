"""A held result is released with exactly what ordinary delivery would carry.

The parent's turn is blocked inside ``spawn_sub_agents`` while a member is
collected, so a completion handed to the ordinary route is injected into that
turn and then returned inline as well. The registry therefore owns the member
and keeps only its id. The completion stays on the live run, which the hold
marks ``_delivery_queued`` and pins against completed-run eviction, and a
release reads result, error and status back from it, in every branch of the
route.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import subagent_inline_collection as sic
from kiro_crew.slack.gateway import inline_collection_owns
from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_inline_collection import InlineCollections

PARENT = "cron:job-1"


def _manager(delivered: list[Any], busy: bool = False) -> Any:
    mgr = MagicMock()
    mgr._report_owners = {}
    mgr._sessions.is_busy = lambda _p: busy
    # The manager's run table, as ``SubagentManager.get`` reads ``_agents``.
    mgr._agents = {}
    mgr.get = mgr._agents.get

    async def _on_done(ticket: Any) -> None:
        delivered.append(ticket)

    mgr._on_done = _on_done
    mgr.settle_queued_delivery = AsyncMock()
    return mgr


def _registry(delivered: list[Any], busy: bool = False) -> InlineCollections:
    reg = InlineCollections()
    mgr = _manager(delivered, busy)
    mgr.inline_collections = reg
    reg.bind(mgr)
    return reg


def _info(aid: str, result: str, result_path: str = "", **kw: Any) -> SubagentInfo:
    info = SubagentInfo(id=aid, task="t")
    info.parent_session_key = PARENT
    info.done = True
    info.result = result
    info.result_path = result_path
    for k, v in kw.items():
        setattr(info, k, v)
    return info


def _owns(reg: InlineCollections, info: SubagentInfo) -> bool:
    """The gateway's route check on a run the manager holds, as live."""
    reg._manager._agents[info.id] = info
    return inline_collection_owns(reg._manager, None, info)


@pytest.fixture(autouse=True)
def _no_idle_poll(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(sic, "_ORPHAN_IDLE_POLL_SECS", 0)


async def _drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# --- owned, so never injected into the blocked turn --------------------------


@pytest.mark.asyncio
async def test_a_large_completion_while_collecting_is_owned_not_injected() -> None:
    """A completion of any size is owned: answering False would let the route
    inject it into the parent turn blocked inside spawn_sub_agents, and the same
    call returns it inline too."""
    delivered: list[Any] = []
    reg = _registry(delivered, busy=True)
    assert reg.reserve(PARENT, "a1", 60)
    info = _info("a1", "x" * 5_000_000)
    owned = _owns(reg, info)
    assert owned and info._delivery_queued is True
    reg.finish(PARENT, {"a1"}, {"a1"})
    await asyncio.gather(*reg.commit(PARENT, ["a1"], True))
    assert delivered == [], "a result the call returned was routed as well"


@pytest.mark.asyncio
async def test_a_completion_after_the_claim_is_delivered_once() -> None:
    delivered: list[Any] = []
    reg = _registry(delivered, busy=True)
    assert reg.reserve(PARENT, "a1", 60)
    reg.finish(PARENT, {"a1"}, {"a1"})  # claimed: the response names a1
    owned = _owns(reg, _info("a1", "y" * 5000))
    reg.commit(PARENT, ["a1"], True)  # the response was written
    rec = reg._records[PARENT]["a1"]
    # Never a RETURNED record waiting an hour for a completion already out.
    assert owned, "claimed result injected AND returned inline"
    assert rec.state != sic.RETURNED or not reg.has_collected(PARENT)


# --- released whole, in every branch of the route ----------------------------


@pytest.mark.asyncio
async def test_a_released_result_keeps_its_tail(tmp_path) -> None:
    """``info.result`` is already the completion copy ``completion_keep`` trimmed
    (tail, or more than 3000 chars). A release carries all of it, so the final
    answer the ordinary route would send is there."""
    path = tmp_path / "result.txt"
    path.write_text("full transcript")
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    text = "H" * 4000 + "FINAL ANSWER"
    assert _owns(reg, _info("a1", text, str(path)))
    reg.finish(PARENT, {"a1"}, set())  # call ended without returning a1
    await _drain()
    assert len(delivered) == 1
    assert delivered[0].result == text


@pytest.mark.asyncio
async def test_a_user_stopped_partial_is_released_whole(tmp_path) -> None:
    """The route's user_stopped and error+partial branches print info.result
    verbatim, so a released partial is the run's own text, uncut."""
    path = tmp_path / "result.txt"
    path.write_text("full transcript")
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    text = "P" * 4000 + "TAIL"
    assert _owns(reg, _info("a1", text, str(path), user_stopped=True))
    reg.finish(PARENT, {"a1"}, set())
    await _drain()
    assert delivered and delivered[0].result == text and delivered[0].user_stopped


@pytest.mark.asyncio
async def test_a_transcript_removed_after_the_hold_loses_nothing(tmp_path) -> None:
    path = tmp_path / "result.txt"
    path.write_text("full transcript")
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    text = "Z" * 5000
    assert _owns(reg, _info("a1", text, str(path)))
    path.unlink()  # deleted (manual delete, cleanup) while held
    reg.finish(PARENT, {"a1"}, set())
    await _drain()
    assert delivered[0].result == text


@pytest.mark.asyncio
async def test_error_text_is_delivered_whole() -> None:
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    err = "E" * 100_000
    assert _owns(reg, _info("a1", "r", error=err))
    reg.finish(PARENT, {"a1"}, set())
    await _drain()
    assert delivered[0].error == err and delivered[0].outcome == "failed"


@pytest.mark.asyncio
async def test_a_memory_wait_expiry_still_owes_its_report() -> None:
    """A memory-wait expiry fails with an ``error`` and owes the store's report.
    Read from the live run, its commit settles the owed report, never a
    delivered mark."""
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    expiry = _info("a1", "", error="memory wait expired " * 20)
    expiry._report_owed = True
    assert _owns(reg, expiry)
    reg.finish(PARENT, set(), {"a1"})
    await asyncio.gather(*reg.commit(PARENT, ["a1"], True))
    ((batch,),) = [c.args for c in reg._manager.settle_queued_delivery.await_args_list]
    (delivery,) = batch
    assert delivery.agent_id == "a1" and delivery.report_owed is True


# --- the registry keeps no text, over seeded interleavings -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(60))
async def test_every_release_carries_the_live_runs_outcome(seed: int, tmp_path) -> None:
    """Over random paths (hold, close, commit, retire, expiry, consume, discard),
    the registry holds no text of any member, every held run stays pinned until
    its record leaves, and every release carries the live run's own result,
    error and outcome."""
    rng = random.Random(seed)
    now = [0.0]
    delivered: list[Any] = []
    reg = InlineCollections(clock=lambda: now[0])
    mgr = _manager(delivered, busy=False)
    mgr.inline_collections = reg
    reg.bind(mgr)
    path = tmp_path / "r.txt"
    path.write_text("x")
    ids = [f"a{i}" for i in range(12)]
    runs: dict[str, SubagentInfo] = {}
    for _ in range(80):
        aid = rng.choice(ids)
        op = rng.randrange(8)
        if op == 0:
            reg.reserve(PARENT, aid, rng.choice([0, 60]))
        elif op == 1 and aid not in runs:  # one run per id, as the manager mints them
            info = _info(
                aid,
                "q" * rng.choice([0, 10, 4000, 90_000]),
                rng.choice(["", str(path)]),
                error=rng.choice(["", "e", "E" * 9000]),
                user_stopped=rng.random() < 0.2,
            )
            runs[aid] = info
            _owns(reg, info)
        elif op == 1:
            continue
        elif op == 2:
            some = set(rng.sample(ids, 3))
            reg.finish(PARENT, some, {i for i in some if rng.random() < 0.5})
        elif op == 3:
            await asyncio.gather(*reg.commit(PARENT, [aid], rng.random() < 0.5, released=[aid]))
        elif op == 4:
            reg.retire(PARENT)
        elif op == 5:
            now[0] += rng.choice([1, 400, 4000, 8000])
            reg._expire(PARENT)
        elif op == 6:
            reg.consume_collected(PARENT, aid)
        else:
            reg.discard(PARENT, aid)
        await _drain()
        for recs in reg._records.values():
            for rec in recs.values():
                kept = [v for v in vars(rec).values() if isinstance(v, str)]
                # The call id is bounded at entry (``MAX_CALL_ID_CHARS``).
                assert set(kept) <= {rec.aid, rec.parent, rec.state, rec.call}, kept
                if rec.held:
                    assert reg.pins(mgr._agents[rec.aid])
    now[0] += 1e6
    reg._expire(PARENT)
    await _drain()
    for ticket in delivered:
        live = mgr._agents[ticket.id]
        assert ticket is not live
        assert (ticket.result, ticket.error, ticket.outcome) == (
            live.result,
            live.error,
            live.outcome,
        )
    assert not any(reg.pins(info) for info in runs.values())
