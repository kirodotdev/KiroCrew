"""Workflow steps sharing one KAS process, measured on the real relay.

``workflows/shared_nodes.py`` opens a run's steps as sessions on the launching
chat's own ``kiro-cli acp --agent-engine v3`` process. This lane drives that shape
on the installed relay with a raw ACP client (the harness of
``test_kas_subagent_sharing_real.py``): a parent chat session, an analyse step,
then a fan-out of N steps prompted at the same time, all on one process.

1. The fan-out adds no engine process: the relay's own process tree, fake MCP
   servers aside, is the same size with N step sessions as with the parent alone,
   and the user server runs once while each step gets its own per-session one.
2. A step that is cancelled mid-turn ends alone: its siblings and the parent
   still answer on the same process.
3. A finished step's session closes and the next run's steps open fresh sessions
   on the same, still-running process.

Opt-in, as that file: ``KIROCREW_KAS_LIVE=1`` and a signed-in kiro-cli; the tests
that make model calls also need ``KIROCREW_KAS_LIVE_MODEL=1``. Each relay runs with
``HOME`` and cwd under ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
from test_kas_subagent_sharing_real import TIMEOUT_S, _Acp, _Lane, _require_kas, _require_model

from kiro_crew.testing.wait import wait_until

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc"),
]

#: Steps in the fan-out.
FAN_OUT = 3


@pytest.fixture
def kas_lane(tmp_path):
    value = _Lane(tmp_path, _require_kas())
    try:
        yield value
    finally:
        value.cleanup()


class _ConcurrentAcp(_Acp):
    """``_Acp`` whose requests may be issued from several threads at once."""

    def __init__(self, *args, **kwargs) -> None:
        self._ids = threading.Lock()
        super().__init__(*args, **kwargs)

    def call(self, method: str, params: dict) -> dict:
        with self._ids:
            mid = self._next
            self._next += 1
            slot: list = [threading.Event(), None]
            self._pending[mid] = slot
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        assert slot[0].wait(TIMEOUT_S), f"{method} unanswered after {TIMEOUT_S}s"
        msg = slot[1]
        assert "error" not in msg, f"{method}: {msg['error']}"
        return msg.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})


def _start(kas_lane) -> _ConcurrentAcp:
    acp = _ConcurrentAcp(
        kas_lane.root / f"stderr-{len(kas_lane._procs)}.log", kas_lane.home, kas_lane.work
    )
    kas_lane._procs.append(acp)
    return acp


def _engine_tree(root: int, fake_server: Path) -> list[int]:
    """Descendants of *root*, the lane's fake MCP servers left out."""
    kids: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if str(fake_server) in cmd:
            continue
        kids.setdefault(int(stat.rsplit(")", 1)[1].split()[1]), []).append(int(entry.name))
    out, stack = [], [root]
    while stack:
        for child in kids.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


def _prompt_all(acp: _ConcurrentAcp, sids: dict[str, str], text) -> dict[str, object]:
    """Prompt every session at once; each value is the reply or the raised error."""
    out: dict[str, object] = {}

    def _one(mark: str, sid: str) -> None:
        try:
            out[mark] = acp.reply(sid, text(mark))
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            out[mark] = exc

    threads = [threading.Thread(target=_one, args=item) for item in sids.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT_S)
    return out


def test_a_fan_out_adds_no_engine_process(kas_lane):
    """Parent + analyse + N steps in one folder: one engine tree, one user server."""
    acp = _start(kas_lane)
    kas_lane.new(acp, "parent")
    wait_until(
        lambda: _engine_tree(acp.proc.pid, kas_lane.server),
        describe=lambda: "the relay started no engine",
    )
    alone = len(_engine_tree(acp.proc.pid, kas_lane.server))
    kas_lane.new(acp, "analyse")
    steps = [kas_lane.new(acp, f"step{i}") for i in range(FAN_OUT)]
    assert len(_engine_tree(acp.proc.pid, kas_lane.server)) == alone
    assert kas_lane.live("user") == ["-"]
    assert kas_lane.live("crew") == sorted(
        ["analyse", "parent", *(f"step{i}" for i in range(FAN_OUT))]
    )
    for sid in steps:
        acp.call("session/close", {"sessionId": sid})
    kas_lane.settle(
        lambda: kas_lane.live("crew") == ["analyse", "parent"], "closed steps' servers stop"
    )
    assert acp.proc.poll() is None


def test_the_next_runs_steps_open_on_the_same_process(kas_lane):
    acp = _start(kas_lane)
    kas_lane.new(acp, "parent")
    first = kas_lane.new(acp, "run1-step")
    acp.call("session/close", {"sessionId": first})
    kas_lane.settle(lambda: kas_lane.live("crew") == ["parent"], "the first run's step ends")
    kas_lane.new(acp, "run2-step")
    assert kas_lane.live("crew") == ["parent", "run2-step"]
    assert kas_lane.live("user") == ["-"]


def test_parallel_steps_answer_on_one_process(kas_lane):
    _require_model()
    acp = _start(kas_lane)
    kas_lane.new(acp, "parent")
    sids = {f"s{i}": kas_lane.new(acp, f"s{i}") for i in range(FAN_OUT)}
    alone = len(_engine_tree(acp.proc.pid, kas_lane.server))
    out = _prompt_all(acp, sids, lambda m: f"Reply with exactly: WORD-{m.upper()}")
    assert {m: f"WORD-{m.upper()}" in str(r) for m, r in out.items()} == {m: True for m in sids}
    assert len(_engine_tree(acp.proc.pid, kas_lane.server)) == alone


def test_a_cancelled_step_ends_alone(kas_lane):
    """A step cancelled mid-turn does not take its siblings or the parent with it."""
    _require_model()
    acp = _start(kas_lane)
    parent = kas_lane.new(acp, "parent")
    sids = {f"s{i}": kas_lane.new(acp, f"s{i}") for i in range(FAN_OUT)}
    victim = sids["s0"]
    started = threading.Event()
    stop_reason: dict[str, object] = {}

    def _long() -> None:
        try:
            started.set()
            result = acp.call(
                "session/prompt",
                {
                    "sessionId": victim,
                    "prompt": [
                        {"type": "text", "text": "Count from 1 to 400, one number per line."}
                    ],
                },
            )
            stop_reason["value"] = result.get("stopReason")
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            stop_reason["value"] = exc

    thread = threading.Thread(target=_long)
    thread.start()
    started.wait(TIMEOUT_S)
    wait_until(
        lambda: any(
            u.get("params", {}).get("sessionId") == victim
            and u.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk"
            for u in list(acp.updates)
        ),
        describe=lambda: "the long step never streamed",
    )
    acp.notify("session/cancel", {"sessionId": victim})
    thread.join(TIMEOUT_S)
    assert stop_reason.get("value") == "cancelled", stop_reason
    rest = {m: s for m, s in sids.items() if s != victim}
    out = _prompt_all(acp, rest, lambda m: f"Reply with exactly: OK-{m.upper()}")
    assert {m: f"OK-{m.upper()}" in str(r) for m, r in out.items()} == {m: True for m in rest}
    assert "OK-PARENT" in acp.reply(parent, "Reply with exactly: OK-PARENT")
    assert acp.proc.poll() is None
    assert json.dumps(sorted(kas_lane.live("crew"))) == json.dumps(sorted(["parent", *sids]))
