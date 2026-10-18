"""kas steering and kiro compaction status, replayed from live captures.

``kas/steering-live.jsonl`` and ``kiro/compaction-live.jsonl`` are the evidence
``ACP_BACKENDS_STEER`` (kas) and ``ACP_BACKENDS_COMPACT`` (kiro) cite. The frame
replay snapshot does not reach either translation -- KAS's steer kinds ride a
``session_info_update`` that only ``AcpSessionHandle`` reads, and kiro's terminal
status is consumed by ``wait_for_compaction`` -- so these tests drive the real
handlers with the captured frames.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_STEER_CLEARED,
    EVENT_STEER_CONSUMED,
    EVENT_STEER_QUEUED,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_STEER,
)

_CORPUS = Path(__file__).parent / "fixtures" / "acp_frames"


def _frames(rel: str) -> list[dict[str, Any]]:
    lines = (_CORPUS / rel).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["_meta"]["recorded"] == "live"
    return [json.loads(line) for line in lines[1:]]


def _kind(frame: dict[str, Any]) -> str | None:
    update = (frame.get("params") or {}).get("update") or {}
    return ((update.get("_meta") or {}).get("kiro") or {}).get("kind")


def test_kas_steer_is_consumed_inside_the_turn_it_was_sent_into() -> None:
    assert ACP_BACKEND_KAS in ACP_BACKENDS_STEER
    frames = _frames("kas/steering-live.jsonl")
    rt, _reader, _ = _make_runtime()
    handle = AcpSessionHandle("kas-steer-session-1", _register(rt, "s")["s"], rt)
    events = [
        ev
        for frame in frames
        if _kind(frame) in ("steering_queued", "steering_injected", "steering_cleared")
        for ev in handle._handle_kas_session_info(frame["params"]["update"])
    ]
    assert [ev.kind for ev in events] == [
        EVENT_STEER_QUEUED,
        EVENT_STEER_CONSUMED,
        EVENT_STEER_CLEARED,
    ]
    assert "pineapple" in events[1].text

    injected = next(i for i, f in enumerate(frames) if _kind(f) == "steering_injected")
    turn_end = next(i for i, f in enumerate(frames) if (f.get("result") or {}).get("stopReason"))
    answer = "".join(
        f["params"]["update"]["content"]["text"]
        for f in frames[injected:turn_end]
        if f.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk"
    )
    # The model read the steer in the same turn, before that turn's stopReason.
    assert "pineapple" in answer


@pytest.mark.asyncio
async def test_kiro_compaction_status_reaches_wait_for_compaction() -> None:
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_COMPACT
    frames = _frames("kiro/compaction-live.jsonl")
    statuses = [f for f in frames if f.get("method") == "_kiro.dev/compaction/status"]
    assert [f["params"]["status"]["type"] for f in statuses] == ["started", "completed"]

    rt, _reader, _ = _make_runtime()
    queue = _register(rt, "kiro-compact-session-1")["kiro-compact-session-1"]
    handle = AcpSessionHandle("kiro-compact-session-1", queue, rt)
    for frame in statuses:
        queue.put_nowait(JsonRpcMessage.from_dict(frame))
    queue.put_nowait(None)  # end the post-compaction metadata grace drain at once
    result = await handle.wait_for_compaction(timeout=3.0)
    assert result["type"] == "completed"
    assert "cobalt" in result["summary"]
