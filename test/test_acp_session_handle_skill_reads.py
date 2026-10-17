"""Skill reads on the shared ACP runtime reach the usage ledger.

``AcpSessionHandle`` serves the kiro-cli, KAS and codex backends on one shared
``AcpRuntime`` process; ``AcpClient`` serves the others one process per
session. The ledger must credit a direct ``SKILL.md`` read the same way on
both, so these tests drive the handle's REAL dispatch loop -- a ``prompt()``
turn on a fake runtime process fed the frames a successful read produces --
with a recording observer installed. Calling the tracker directly would pass
on a loop that never wired it, which is the whole defect.
"""

from __future__ import annotations

import asyncio

import pytest
from test_acp_runtime import (
    _await_routed,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import METHOD_SESSION_UPDATE
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.skill_usage import get_global_skill_read_observer, set_global_skill_read_observer
from kiro_crew.testing.wait import await_until

SKILL = "/x/skills/demo/SKILL.md"
SHARED_RUNTIME_BACKENDS = (ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX)


class _Observer:
    """Records both phases so a test can tell resolution from crediting."""

    def __init__(self, keys=("demo",)):
        self._keys = list(keys)
        self.resolved: list[tuple[str, dict | None, str | None]] = []
        self.credited: list[list[str]] = []

    def resolve_tool_read_keys(self, tool_name="", raw_params=None, command=None):
        self.resolved.append((tool_name, raw_params, command))
        return list(self._keys)

    def credit_skill_reads(self, keys):
        self.credited.append(list(keys))


@pytest.fixture
def observer():
    prior = get_global_skill_read_observer()
    obs = _Observer()
    set_global_skill_read_observer(obs)
    try:
        yield obs
    finally:
        set_global_skill_read_observer(prior)


def _update(sid: str, update: dict) -> dict:
    return {"method": METHOD_SESSION_UPDATE, "params": {"sessionId": sid, "update": update}}


def _read_call(raw_input: dict | None, tool_call_id: str = "tc-1") -> dict:
    """kiro-cli's batched ``read`` of one file, as the live frame spells it."""
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": "Reading SKILL.md",
        "kind": "read",
        "status": "pending",
        "_meta": {"kiro": {"toolName": "read"}},
    }
    if raw_input is not None:
        update["rawInput"] = raw_input
    return _update("sA", update)


def _read_result(status: str, tool_call_id: str = "tc-1", **extra) -> dict:
    update = {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "status": status}
    if status == "completed":
        update["content"] = [{"type": "content", "content": {"type": "text", "text": "# demo"}}]
    update.update(extra)
    return _update("sA", update)


READ_ARGS = {"operations": [{"mode": "Line", "path": SKILL}]}


async def _run_turn(backend: str, frames: list[dict]) -> None:
    """One prompt turn on the shared runtime, fed *frames* then end_turn."""
    rt, reader, _ = _make_runtime()
    rt._acp_backend = backend
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:

        async def drive():
            async for _ in handle.prompt("hi", timeout=5.0):
                pass

        driver = asyncio.ensure_future(drive())
        req_id = (await _await_routed(rt, "sA"))["sA"]
        for frame in frames:
            _feed(reader, frame)
        _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
        await await_until(driver, what="the driver finishing its turn")
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", SHARED_RUNTIME_BACKENDS)
async def test_a_completed_read_on_the_shared_runtime_is_credited_once(observer, backend):
    await _run_turn(backend, [_read_call(READ_ARGS), _read_result("completed")])

    assert observer.resolved == [("read", READ_ARGS, None)]
    assert observer.credited == [["demo"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_read_that_did_not_complete_is_not_credited(observer, status):
    # Resolution happens at the call; the credit waits for ``completed``. A
    # denied or failed read must leave no delivery behind.
    await _run_turn(ACP_BACKEND_KIRO, [_read_call(READ_ARGS), _read_result(status)])

    assert len(observer.resolved) == 1
    assert observer.credited == []


@pytest.mark.asyncio
async def test_arguments_arriving_on_the_refinement_still_credit_once(observer):
    # Some harnesses send the initial tool_call with no arguments and the real
    # input on a later tool_call_update; the handle observes both, and one read
    # resolves once.
    await _run_turn(
        ACP_BACKEND_KIRO,
        [
            _read_call(None),
            _update(
                "sA",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-1",
                    "title": "Reading SKILL.md",
                    "kind": "read",
                    "rawInput": READ_ARGS,
                },
            ),
            _read_result("completed"),
        ],
    )

    assert len(observer.resolved) == 1
    assert observer.credited == [["demo"]]


@pytest.mark.asyncio
async def test_a_shell_cat_of_a_skill_is_observed_with_its_command(observer):
    command = f"cat {SKILL}"
    await _run_turn(
        ACP_BACKEND_KIRO,
        [
            _update(
                "sA",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": f"Running: {command}",
                    "kind": "execute",
                    "status": "pending",
                    "rawInput": {"command": command},
                    "_meta": {"kiro": {"toolName": "shell"}},
                },
            ),
            _read_result("completed"),
        ],
    )

    assert len(observer.resolved) == 1
    assert observer.resolved[0][2] == command
    assert observer.credited == [["demo"]]


@pytest.mark.asyncio
async def test_a_tool_call_naming_no_skill_never_reaches_the_observer(observer):
    await _run_turn(
        ACP_BACKEND_KIRO,
        [
            _read_call({"operations": [{"mode": "Line", "path": "/etc/hosts"}]}),
            _read_result("completed"),
        ],
    )

    assert observer.resolved == []
    assert observer.credited == []


@pytest.mark.asyncio
async def test_a_new_turn_forgets_the_previous_turns_reads(observer):
    # The tracker shares the per-turn lifecycle of the sibling tool-call caches:
    # a completion frame for a call noted in a PREVIOUS turn credits nothing.
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    task = await _start_reader(rt)
    try:
        for frames in ([_read_call(READ_ARGS)], [_read_result("completed")]):

            async def drive():
                async for _ in handle.prompt("hi", timeout=5.0):
                    pass

            driver = asyncio.ensure_future(drive())
            req_id = (await _await_routed(rt, "sA"))["sA"]
            for frame in frames:
                _feed(reader, frame)
            _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
            await await_until(driver, what="the driver finishing its turn")
    finally:
        await _stop_reader(task)

    assert len(observer.resolved) == 1
    assert observer.credited == []
