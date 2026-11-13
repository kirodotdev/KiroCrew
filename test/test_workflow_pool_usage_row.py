"""Every ``ctx.agent()`` executor writes a per-turn usage row.

All three executors persist through the one shared writer
``agent_exec.persist_step_usage_row``. These tests pin that each writes exactly
one row with surface ``workflow`` on its OWN real session key:

* the warm pool's pooled worker (``_WorkflowSessionWorker.send_message``) → a
  ``wf-pool:`` slot — the default path (``service.pool_agents`` defaults True);
* the pool's unpooled overflow leg (``_run_unpooled``), reached once the
  identity cap is full → a ``wf-unpooled:`` slot;
* the cold-start fallback (``build_agent_fn``) → a ``wf:`` slot (unchanged).

They never touch disk: ``usage._write_token_record`` is patched to capture the
exact record ``_build_token_record`` produced (same technique as
``test_turn_duration_workflow.py``).
"""

from __future__ import annotations

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.workflows.agent_exec as agent_exec
import kiro_crew.workflows.agent_pool as agent_pool
from kiro_crew.workflows.agent_exec import build_agent_fn
from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

pytestmark = pytest.mark.asyncio


class FakeProvider:
    """Bare provider double: no context/agent accessors, so the real
    ``read_context_tokens`` yields ``(0, 0)`` and ``read_effective_agent``
    yields ``""`` (the step's agent fallback is used)."""

    def __init__(self, key: str = "k") -> None:
        self.key = key

    def set_delete_delay(self, *_a, **_k) -> None:
        return None

    async def new_conversation(self) -> None:
        return None

    def is_process_alive(self) -> bool:
        return True


class FakeSessions:
    """Minimal SessionManager double (tolerant ``**kw`` so effort kwargs pass)."""

    def __init__(self) -> None:
        self.released: list[str] = []
        self.destroyed: list[str] = []

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, **kw):
        return FakeProvider(key), True, False

    def release(self, key, *, cleanup=False):
        self.released.append(key)

    async def destroy(self, key):
        self.destroyed.append(key)


@pytest.fixture(autouse=True)
def _fast_stream(_floor_monkeypatch):
    """stream_and_collect → echo the prompt (no real model / kiro-cli), on BOTH
    executor modules (each imports it into its own namespace)."""

    async def fake_stream(provider, message, **kw):
        return f"reply:{message}"

    _floor_monkeypatch.setattr(agent_exec, "stream_and_collect", fake_stream)
    _floor_monkeypatch.setattr(agent_pool, "stream_and_collect", fake_stream)


@pytest.fixture(autouse=True)
def _skip_spawn_gates(_floor_monkeypatch):
    """The spawn gates are not under test here; stub to the resolved cwd."""

    async def fake_vet(**kw):
        return kw.get("cwd")

    _floor_monkeypatch.setattr(agent_exec, "vet_step_spawn", fake_vet)
    _floor_monkeypatch.setattr(agent_pool, "vet_step_spawn", fake_vet)


@pytest.fixture
def captured_rows(monkeypatch):
    """Capture every record handed to the shard writer, without disk I/O.

    ``persist_token_record_async`` resolves ``_write_token_record`` as a
    usage-module global at call time, so patching the attribute captures the
    REAL record ``_build_token_record`` built with zero file writes.
    """
    rows: list[dict] = []
    monkeypatch.setattr(usage_mod, "_write_token_record", lambda record, now: rows.append(record))
    return rows


async def test_pooled_step_writes_one_workflow_row(captured_rows):
    """A pooled ``ctx.agent()`` step writes exactly one usage row,
    surface=workflow, on the pool's real ``wf-pool:`` key."""
    sessions = FakeSessions()
    fn, pool = build_pooled_agent_fn(
        sessions, run_id="wf_pool", default_agent="researcher", default_model="m1"
    )
    try:
        out = await fn("do work", {})
    finally:
        await pool.shutdown()

    assert out == "reply:do work"
    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row["surface"] == "workflow"
    assert str(row["slot"]).startswith("wf-pool:")
    assert row["agent"] == "researcher"  # step fallback (empty read_effective_agent)


async def test_unpooled_overflow_step_writes_one_workflow_row(captured_rows):
    """An unpooled overflow step (identity cap exceeded) writes one usage row,
    surface=workflow, on its real ``wf-unpooled:`` key.

    ``max_identities=1``: the default identity uses the default sub-pool (not an
    identity slot), one distinct override fills the single identity slot, and a
    SECOND distinct override finds no slot left (``len(subpools) >=
    max_identities``) so it runs on the unpooled create-run-destroy leg."""
    sessions = FakeSessions()
    fn, pool = build_pooled_agent_fn(
        sessions,
        run_id="wf_of",
        default_agent="researcher",
        default_model="m1",
        max_identities=1,
    )
    try:
        await fn("default identity", {})  # -> default sub-pool
        await fn("fills the one slot", {"model": "m2"})  # -> identity sub-pool (slot used)
        captured_rows.clear()
        # No identity slot left → None → unpooled leg.
        out = await fn("overflow", {"model": "m3"})
    finally:
        await pool.shutdown()

    assert out == "reply:overflow"
    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row["surface"] == "workflow"
    assert str(row["slot"]).startswith("wf-unpooled:")


async def test_cold_build_agent_fn_row_unchanged(captured_rows):
    """The cold-start fallback writes exactly one ``wf:`` row with
    surface=workflow, keyed ``wf:<run_id>:0``."""
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_cold", default_agent="researcher", default_model="m1")

    out = await fn("do work", {})

    assert out == "reply:do work"
    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row["surface"] == "workflow"
    assert str(row["slot"]).startswith("wf:")
    assert row["slot"] == "wf:wf_cold:0"
    assert row["agent"] == "researcher"


@pytest.fixture
def stopped_stream(monkeypatch):
    """Every turn ends ``cancelled``: a gateway stop cut it off after it spent."""

    async def stream(provider, message, *, on_complete=None, **kw):
        on_complete(type("Done", (), {"stop_reason": "cancelled"})())
        return "fake slow chunk 0"

    monkeypatch.setattr(agent_exec, "stream_and_collect", stream)
    monkeypatch.setattr(agent_pool, "stream_and_collect", stream)


async def test_a_cut_off_pooled_step_still_writes_its_row(captured_rows, stopped_stream):
    fn, pool = build_pooled_agent_fn(FakeSessions(), run_id="wf_cut", default_model="m1")
    try:
        with pytest.raises(Exception, match="stopped before it finished"):
            await fn("slow", {})
    finally:
        await pool.shutdown()

    assert [str(r["slot"])[:8] for r in captured_rows] == ["wf-pool:"]


async def test_a_cut_off_unpooled_step_still_writes_its_row(captured_rows, monkeypatch):
    fn, pool = build_pooled_agent_fn(
        FakeSessions(), run_id="wf_cut_of", default_model="m1", max_identities=1
    )
    try:
        await fn("default identity", {})
        await fn("fills the one slot", {"model": "m2"})
        captured_rows.clear()

        async def stream(provider, message, *, on_complete=None, **kw):
            on_complete(type("Done", (), {"stop_reason": "cancelled"})())
            return "fake slow chunk 0"

        monkeypatch.setattr(agent_pool, "stream_and_collect", stream)
        with pytest.raises(Exception, match="stopped before it finished"):
            await fn("overflow", {"model": "m3"})
    finally:
        await pool.shutdown()

    assert [str(r["slot"])[:12] for r in captured_rows] == ["wf-unpooled:"]


async def test_a_cut_off_cold_step_still_writes_its_row(captured_rows, stopped_stream):
    fn = build_agent_fn(FakeSessions(), run_id="wf_cut_cold", default_model="m1")

    with pytest.raises(Exception, match="stopped before it finished"):
        await fn("slow", {})

    assert [r["slot"] for r in captured_rows] == ["wf:wf_cut_cold:0"]
