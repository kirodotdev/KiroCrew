"""Turn timing markers: the per-turn ``turn_timing`` record and its log line."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream, _slot
from chat_test_helpers import _make_app
from test_chat_send_echo_scope import _SLOT, echo_state  # noqa: F401 -- fixture

import kiro_crew.memory_startup as memory_startup_mod
from kiro_crew import start_priority, turn_timing
from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.dashboard import token_auth
from kiro_crew.providers.base import LLMEvent
from kiro_crew.testing.wait import finished

_FIELDS = (
    "handler_ms",
    "task_start_ms",
    "pre_admit_ms",
    "memory_wait_ms",
    "admit_prelude_ms",
    "pre_claim_ms",
    "claim_ms",
    "build_prompt_ms",
    "dispatch_gates_ms",
    "first_event_ms",
    "first_visible_ms",
    "cold_start_wait_ms",
    "spawn_admission_wait_ms",
    "session_new_wait_ms",
    "spawn_init_ms",
    "session_new_ms",
    "set_model_ms",
    "exec_q0",
    "exec_q_claim",
    "is_new",
    "resumed",
    "priority",
)

# Names that would mean a wait duration or a gate depth reached a frame.
_LEAK_MARKERS = ("wait_ms", "exec_q", "start_queue", "queue_wait")


@pytest.fixture
def timing_on(monkeypatch, caplog):
    monkeypatch.setattr(turn_timing, "ENABLED", True)
    caplog.set_level(logging.INFO, logger="kiro_crew.turn_timing")
    return caplog


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "kiro_crew.turn_timing"]


def _fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split()[1:])


def _import_level(flag: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_TURN_TIMING"}
    if flag is not None:
        env["KIROCREW_TURN_TIMING"] = flag
    code = (
        "import logging; from kiro_crew import turn_timing as t; "
        "print(t.ENABLED, logging.getLogger('kiro_crew.turn_timing').level)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_flag_off_leaves_logger_level_alone_and_flag_on_sets_info():
    assert _import_level(None) == f"False {logging.NOTSET}"
    assert _import_level("1") == f"True {logging.INFO}"


def test_flag_off_logs_no_marker_line(monkeypatch, caplog):
    monkeypatch.setattr(turn_timing, "ENABLED", False)
    caplog.set_level(logging.INFO, logger="kiro_crew.turn_timing")

    async def turn():
        turn_timing.begin("s", "sse")
        turn_timing.mark("dispatched")
        turn_timing.ensure("s", "runner")
        turn_timing.emit("visible")
        turn_timing.sse_first_frame("s")

    asyncio.run(turn())
    assert _lines(caplog) == []


@pytest.mark.asyncio
async def test_runner_turn_logs_one_line_with_every_field(tmp_path, timing_on, monkeypatch):
    import kiro_crew.memory_startup as memory_startup

    state, client = _runner_state(tmp_path)
    published: list[tuple[str, object]] = []
    state.broadcast_ws = MagicMock(side_effect=lambda kind, data: published.append((kind, data)))
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
    # The record's clock moves only while the turn sits in the memory wait, so the
    # span it reports is exact whatever the host's timer resolution or load.
    clock = {"now": 1000.0}
    monkeypatch.setattr(turn_timing, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    real_wait = memory_startup.wait_for_memory_preparation

    async def held_wait(task):
        clock["now"] += 0.25
        return await real_wait(task)

    monkeypatch.setattr(memory_startup, "wait_for_memory_preparation", held_wait)
    slot = _slot()

    await _drive(state, slot)

    lines = [line for line in _lines(timing_on) if "outcome=" in line]
    assert len(lines) == 1, lines
    fields = _fields(lines[0])
    assert fields["outcome"] == "visible"
    for name in _FIELDS:
        assert name in fields, name
    assert fields["memory_wait_ms"] == "250.0"
    assert fields["is_new"] == "True"
    # Nothing the turn put on a frame carries a wait or a depth.
    wire = json.dumps([published, slot.messages], default=str)
    assert not any(marker in wire for marker in _LEAK_MARKERS)


@pytest.mark.asyncio
async def test_a_nested_prompts_get_turn_records_nothing_into_the_outer_line(
    tmp_path, timing_on, monkeypatch
):
    # ``/prompts get`` runs the expanded prompt as a nested turn (depth 1) in the
    # outer turn's task and context; its admission must not write the outer line.
    state, client = _runner_state(tmp_path)
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._resolve_prompt_mention",
        lambda message, project_dir: ("expanded prompt body", "ok", ""),
    )
    log = logging.getLogger("test")
    real_wait = memory_startup_mod.wait_for_memory_preparation

    async def nested_admission_wait(task):
        start_priority.notify_start_queue(log, None, 5000.0, start_priority.START_QUEUE_ADMISSION)
        turn_timing.phase("spawn_init", 6000.0)
        return await real_wait(task)

    monkeypatch.setattr(memory_startup_mod, "wait_for_memory_preparation", nested_admission_wait)

    await _drive(state, _slot(), "/prompts get demo")

    assert client.stream.call_count == 1, "the nested turn never reached the provider"
    lines = [line for line in _lines(timing_on) if "outcome=" in line]
    assert len(lines) == 1, lines
    fields = _fields(lines[0])
    assert fields["outcome"] == "ended_at_slash_gate"
    assert fields["spawn_admission_wait_ms"] == "0.0"
    assert fields["spawn_init_ms"] == "-"
    assert fields["memory_wait_ms"] == "-"
    assert fields["claim_ms"] == "-"
    assert fields["is_new"] == "-"


@pytest.mark.asyncio
async def test_gate_waits_and_phases_land_on_the_turn(timing_on):
    turn_timing.ensure("s", "runner")
    log = logging.getLogger("test")
    start_priority.notify_start_queue(log, None, start_priority.START_QUEUE_COLD_START)
    start_priority.notify_start_queue(log, None, 120.0, start_priority.START_QUEUE_COLD_START)
    start_priority.notify_start_queue(log, None, 30.0, start_priority.START_QUEUE_ADMISSION)
    start_priority.notify_start_queue(log, None, 7.5, start_priority.START_QUEUE_SESSION_NEW)
    turn_timing.phase("spawn_init", 400.0)
    turn_timing.phase("session_new", 90.0)
    turn_timing.emit("no_visible")
    fields = _fields(_lines(timing_on)[0])
    assert fields["cold_start_wait_ms"] == "120.0"
    assert fields["spawn_admission_wait_ms"] == "30.0"
    assert fields["session_new_wait_ms"] == "7.5"
    assert fields["spawn_init_ms"] == "400.0"
    assert fields["session_new_ms"] == "90.0"
    assert fields["set_model_ms"] == "-"


@pytest.mark.asyncio
async def test_a_second_provider_start_sums_its_phases_like_its_waits(timing_on):
    turn_timing.ensure("s", "runner")
    log = logging.getLogger("test")
    start_priority.notify_start_queue(log, None, 5000.0, start_priority.START_QUEUE_ADMISSION)
    turn_timing.phase("spawn_init", 6000.0)
    start_priority.notify_start_queue(log, None, 100.0, start_priority.START_QUEUE_ADMISSION)
    turn_timing.phase("spawn_init", 500.0)
    turn_timing.emit("no_visible")
    fields = _fields(_lines(timing_on)[0])
    assert fields["spawn_init_ms"] == "6500.0"
    assert fields["spawn_admission_wait_ms"] == "5100.0"


@pytest.mark.asyncio
async def test_a_task_the_turn_starts_does_not_record_into_the_turn(timing_on):
    turn_timing.ensure("s", "runner")
    log = logging.getLogger("test")

    async def refill():
        start_priority.notify_start_queue(log, None, 5000.0, start_priority.START_QUEUE_ADMISSION)
        turn_timing.phase("spawn_init", 6000.0)

    await asyncio.create_task(refill())
    start_priority.notify_start_queue(log, None, 100.0, start_priority.START_QUEUE_ADMISSION)
    turn_timing.emit("no_visible")
    fields = _fields(_lines(timing_on)[0])
    assert fields["spawn_admission_wait_ms"] == "100.0"
    assert fields["spawn_init_ms"] == "-"


@pytest.mark.parametrize("enabled", [False, True])
def test_a_gate_acquire_never_reaches_the_metrics_recorder(monkeypatch, enabled):
    # The first get_recorder() of a process builds the recorder on the calling
    # thread; on the spawn path that thread is the event loop.
    import kiro_crew.metrics.provider as provider

    def refuse():
        raise AssertionError("gate acquire reached the metrics recorder")

    monkeypatch.setattr(provider, "get_recorder", refuse)
    monkeypatch.setattr(turn_timing, "ENABLED", enabled)
    log = logging.getLogger("test")
    for queue in (
        start_priority.START_QUEUE_COLD_START,
        start_priority.START_QUEUE_ADMISSION,
        start_priority.START_QUEUE_SESSION_NEW,
    ):
        start_priority.notify_start_queue(log, None, queue)
        start_priority.notify_start_queue(log, None, 12.0, queue)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_post_api_chat_streams_with_the_flag_on_and_off(
    echo_state, monkeypatch, caplog, enabled  # noqa: F811
):
    monkeypatch.setattr(turn_timing, "ENABLED", enabled)
    caplog.set_level(logging.INFO, logger="kiro_crew.turn_timing")
    app = _make_app(echo_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat",
            params={"token": token_auth.generate_token("local-app")},
            json={"slot": _SLOT, "message": "hello", "meta": {"sendId": "s-timing"}},
        )
        assert response.status == 200
        body = await finished(response.text(), "response.text", timeout=5)
    rows = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    assert rows[-1] == "[DONE]"
    assert [json.loads(row)["type"] for row in rows[:-1]] == ["assistant"]
    assert not any(marker in body for marker in _LEAK_MARKERS)
    sse_lines = [line for line in _lines(caplog) if "stage=sse_first_frame" in line]
    assert len(sse_lines) == (1 if enabled else 0)
