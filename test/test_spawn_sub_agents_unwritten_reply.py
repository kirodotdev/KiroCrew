"""A result is settled only once a written reply carried it, for every parent kind.

``spawn_sub_agents`` returns its members' results inline, and settling a member
suppresses its completion event, which is the only other way that result
reaches the parent. Every member is held from the moment its id is minted, so
for a cron, channel or task parent (which a dashboard tab's queue never held)
a member settled without reaching the parent is a result nobody delivers. So
the call's one commit is sent only after the dispatcher wrote the reply frame,
and names only members whose whole block that frame carries. These tests drive
the real tool through the real stdio loop and check, for a dashboard tab and a
cron parent alike, that no member is settled that the parent did not receive:
when the reply's write tears, when a cancel lands after the step that built the
reply returned, and when the reply is cut at the response cap.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

import pytest
from test_mcp_shared import _LoopHarness, _tools_call

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew import mcp_core, platform_compat
from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS
from kiro_crew.mcp_tools import spawn as spawn_tools

PARENTS = ["dashboard:tab1", "cron:job1:run1"]


def _settles(body: dict[str, Any]) -> list[str]:
    """The ids a mark-collected body settles as collected: the body is the
    commit, the single mark main also sends as the call returns."""
    return list(body.get("ids") or [])


class _Gateway:
    """The two gateway routes the tool calls, recording every mark."""

    def __init__(self, ids: list[str], result_of=lambda aid: f"{aid} finished") -> None:
        self._spawned = iter(ids)
        self._result_of = result_of
        self.marks: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.ended = threading.Event()

    def post(self, path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        if path == "/api/spawn":
            return {"id": next(self._spawned)}
        if path == "/api/spawn/mark-collected":
            with self.lock:
                self.marks.append(dict(body))
            self.ended.set()
        return {}

    def get(self, path: str, **_kw: Any) -> dict[str, Any]:
        aid = path.rsplit("/", 1)[1]
        return {"done": True, "agent": aid, "result": self._result_of(aid)}

    def bodies(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.marks)


def _drive(
    monkeypatch, gateway: _Gateway, parent: str, members: int, *, until, respond=None
) -> _LoopHarness:
    harness = _LoopHarness(monkeypatch, mcp_core._call_tool_deferrable)
    if respond is not None:
        monkeypatch.setattr(mcp_shared, "respond", respond)
    call = _tools_call(1, "spawn_sub_agents")
    call["params"]["arguments"] = {"agents": [{"prompt": f"t{i}"} for i in range(members)]}
    with (
        patch.object(mcp_core, "_post", side_effect=gateway.post),
        patch.object(mcp_core, "_get", side_effect=gateway.get),
        patch.object(mcp_core, "sel"),
        patch.dict("os.environ", {"KIROCREW_SESSION_KEY": parent}),
    ):
        try:
            harness.send(call)
            until(harness)
        finally:
            harness.close()
    return harness


@pytest.mark.parametrize("parent", PARENTS)
@pytest.mark.parametrize("reader", ["platform", "pump"])
def test_a_torn_reply_marks_no_member_collected_and_releases_each(monkeypatch, reader, parent):
    if reader == "pump":
        # Windows reads stdin on the pump thread; the loop is the same.
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    gateway = _Gateway(["a1", "a2"])
    responses: list[Any] = []

    def _torn(req_id: Any, result: Any, error: dict | None = None) -> bool:
        responses.append(req_id)
        return False  # the frame tore: the client holds no complete reply

    def _until(_harness: _LoopHarness) -> None:
        assert gateway.ended.wait(10.0), f"the call never ended its collection: {gateway.marks}"

    _drive(monkeypatch, gateway, parent, 2, until=_until, respond=_torn)
    assert responses == [1]  # the reply was attempted once
    bodies = gateway.bodies()
    collected = [aid for body in bodies for aid in _settles(body)]
    assert not collected, f"marked collected though the reply carrying them tore: {bodies}"
    released = {aid for body in bodies for aid in body.get("released", [])}
    assert released == {"a1", "a2"}, f"not released for ordinary delivery: {bodies}"


@pytest.mark.parametrize("parent", PARENTS)
def test_cancel_after_the_settling_step_settles_nothing(monkeypatch, parent):
    """The step that builds the reply returns its text on a pool thread; a
    cancel that lands before the dispatcher harvests it drops the reply. Every
    member then reaches the parent as an ordinary completion, so none may be
    settled by anything the call sent."""
    gateway = _Gateway(["a1", "a2"])
    built = threading.Event()
    go_on = threading.Event()
    real_collect = spawn_tools._SubAgentsStep._collect

    def _collect(self):
        text = real_collect(self)
        built.set()
        assert go_on.wait(10.0)  # the cancel lands while the text is in hand
        return text

    monkeypatch.setattr(spawn_tools._SubAgentsStep, "_collect", _collect)

    def _until(harness: _LoopHarness) -> None:
        assert built.wait(10.0), "the reply was never built"
        harness.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        # Frames are read in order: once the ping is answered, the cancel was read.
        harness.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
        go_on.set()
        assert gateway.ended.wait(10.0), "the cancel ended nothing"

    harness = _drive(monkeypatch, gateway, parent, 2, until=_until)
    assert [r[0] for r in harness.responses] == [2]  # the cancelled call is never answered
    bodies = gateway.bodies()
    assert not [aid for b in bodies for aid in _settles(b)], f"settled though dropped: {bodies}"
    assert {aid for b in bodies for aid in b.get("released", [])} == {"a1", "a2"}


@pytest.mark.parametrize("parent", PARENTS)
def test_truncation_cut_member_is_not_settled(monkeypatch, parent):
    """Seven ordinary CJK results overflow ``MAX_RESPONSE_LEN``: the reply the
    client receives ends inside the last block, so that member's result reached
    nobody and must be delivered as an ordinary completion."""
    # Under the inline summary threshold, so each is carried whole; JSON escapes
    # each character to six, so six blocks fit the cap and a seventh does not.
    text = "結果" * 1300
    assert len(text) <= COMPLETION_KEEP_DEFAULT_CHARS
    gateway = _Gateway([f"a{i}" for i in range(1, 8)], result_of=lambda aid: text)

    def _until(harness: _LoopHarness) -> None:
        assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        assert gateway.ended.wait(10.0), "the call never committed"

    _drive(monkeypatch, gateway, parent, 7, until=_until)
    bodies = gateway.bodies()
    settled = {aid for b in bodies for aid in _settles(b)}
    assert settled == {f"a{i}" for i in range(1, 7)}, f"the cut member was settled: {bodies}"
    assert {aid for b in bodies for aid in b.get("released", [])} >= {"a7"}
