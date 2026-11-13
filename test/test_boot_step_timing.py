"""Memory boot logs one INFO line per step with integer wall and thread CPU milliseconds."""

import asyncio
import logging
import re

import pytest
from test_memory_startup import _gateway, _stub_preparation_steps

from kiro_crew.boot_timing import timed_step
from kiro_crew.vector_memory import VectorMemoryStore

_LINE = re.compile(r"^memory-boot step=(\S+) wall_ms=(\d+) cpu_ms=(\d+)$")


def _steps(caplog) -> list[str]:
    found = []
    for record in caplog.records:
        match = _LINE.match(record.getMessage())
        if match:
            found.append(match.group(1))
    return found


@pytest.mark.asyncio
async def test_a_boot_logs_one_line_per_step_with_integer_timings(monkeypatch, tmp_path, caplog):
    gateway = _gateway(monkeypatch)
    _stub_preparation_steps(monkeypatch)
    gateway.vector_memory = VectorMemoryStore(db_path=tmp_path / "memory.db")
    gateway.vector_memory.embed_fn = None
    gateway.ctx_builder.memory.vector_store = gateway.vector_memory
    try:
        with caplog.at_level(logging.INFO, logger="kiro_crew.boot_timing"):
            assert await asyncio.to_thread(gateway._initialize_memory_worker)
    finally:
        gateway.vector_memory.close()
        gateway._memory_startup.stop()
        gateway._memory_startup.release()
    steps = _steps(caplog)
    for name in (
        "reset_memory_caches",
        "repair_legacy_member_stores",
        "migrate_legacy_member_schedules",
        "apply_pending_member_restores",
        "page_prefetch_start",
        "memory_init",
        "vector_init",
        "vector_init.schema",
        "vector_init.record_meta_reconcile",
        "rebuild_index",
        "total",
    ):
        assert steps.count(name) == 1, (name, steps)
    assert steps[-1] == "total"
    assert steps.index("vector_init.record_meta_reconcile") < steps.index("vector_init")


def test_a_raising_step_still_logs_and_re_raises(caplog):
    with caplog.at_level(logging.INFO, logger="kiro_crew.boot_timing"):
        with pytest.raises(ZeroDivisionError):
            with timed_step("boom"):
                1 / 0
    assert _steps(caplog) == ["boom"]
