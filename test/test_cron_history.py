"""Tests for cron execution history store and cron.py concurrent guard."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cron_history import CronHistoryStore, CronRunRecord

# ── Helpers ──────────────────────────────────────────────────────────────


def _record(job_id: str = "job1", run_id: str = "run1", **kw) -> CronRunRecord:
    return CronRunRecord(
        run_id=run_id,
        job_id=job_id,
        trigger=kw.get("trigger", "scheduled"),
        started_at=kw.get("started_at", time.time()),
        finished_at=kw.get("finished_at", time.time() + 1),
        duration_ms=kw.get("duration_ms", 1000),
        status=kw.get("status", "success"),
        summary=kw.get("summary", "ok"),
        trace=kw.get("trace", "trace data"),
        error=kw.get("error", ""),
    )


@pytest.fixture
def store(tmp_path: Path) -> CronHistoryStore:
    return CronHistoryStore(base_dir=tmp_path)


# ── append ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_writes_job_file_and_index(store: CronHistoryStore, tmp_path: Path) -> None:
    rec = _record()
    await store.append(rec)

    job_file = tmp_path / "cron-history" / "job1.jsonl"
    index_file = tmp_path / "cron-history" / "_index.jsonl"
    assert job_file.exists()
    assert index_file.exists()

    job_data = json.loads(job_file.read_text().strip())
    assert job_data["run_id"] == "run1"
    assert job_data["trace"] == "trace data"

    idx_data = json.loads(index_file.read_text().strip())
    assert idx_data["run_id"] == "run1"
    assert "trace" not in idx_data


@pytest.mark.asyncio
async def test_append_caps_summary_and_trace(store: CronHistoryStore, tmp_path: Path) -> None:
    rec = _record(summary="x" * 500, trace="y" * 60_000)
    await store.append(rec)

    job_file = tmp_path / "cron-history" / "job1.jsonl"
    data = json.loads(job_file.read_text().strip())
    assert len(data["summary"]) == 200
    assert data["trace"].endswith("...[truncated]")


# ── get_job_history ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_history_newest_first(store: CronHistoryStore) -> None:
    for i in range(5):
        await store.append(_record(run_id=f"r{i}"))

    records, total = await store.get_job_history("job1")
    assert total == 5
    assert records[0]["run_id"] == "r4"
    assert records[-1]["run_id"] == "r0"


@pytest.mark.asyncio
async def test_get_job_history_offset_limit(store: CronHistoryStore) -> None:
    for i in range(10):
        await store.append(_record(run_id=f"r{i}"))

    records, total = await store.get_job_history("job1", offset=2, limit=3)
    assert total == 10
    assert len(records) == 3
    assert records[0]["run_id"] == "r7"


@pytest.mark.asyncio
async def test_get_job_history_excludes_trace(store: CronHistoryStore) -> None:
    await store.append(_record(trace="secret trace"))
    records, _ = await store.get_job_history("job1")
    assert "trace" not in records[0]


@pytest.mark.asyncio
async def test_get_job_history_nonexistent_job(store: CronHistoryStore) -> None:
    records, total = await store.get_job_history("nope")
    assert records == []
    assert total == 0


# ── get_run_detail ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_run_detail_includes_trace(store: CronHistoryStore) -> None:
    await store.append(_record(trace="full trace"))
    detail = await store.get_run_detail("job1", "run1")
    assert detail is not None
    assert detail["trace"] == "full trace"


@pytest.mark.asyncio
async def test_get_run_detail_not_found(store: CronHistoryStore) -> None:
    await store.append(_record())
    assert await store.get_run_detail("job1", "nonexistent") is None


@pytest.mark.asyncio
async def test_get_run_detail_missing_job(store: CronHistoryStore) -> None:
    assert await store.get_run_detail("nope", "run1") is None


# ── get_all_history ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_all_history_from_index(store: CronHistoryStore) -> None:
    await store.append(_record(job_id="a", run_id="r1"))
    await store.append(_record(job_id="b", run_id="r2"))

    records, total = await store.get_all_history()
    assert total == 2
    assert records[0]["run_id"] == "r2"


@pytest.mark.asyncio
async def test_get_all_history_filter_by_job_id(store: CronHistoryStore) -> None:
    await store.append(_record(job_id="a", run_id="r1"))
    await store.append(_record(job_id="b", run_id="r2"))
    await store.append(_record(job_id="a", run_id="r3"))

    records, total = await store.get_all_history(job_id="a")
    assert total == 2
    assert all(r["job_id"] == "a" for r in records)


@pytest.mark.asyncio
async def test_get_all_history_no_index(store: CronHistoryStore) -> None:
    records, total = await store.get_all_history()
    assert records == []
    assert total == 0


# ── rotate ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_trims_to_max(store: CronHistoryStore) -> None:
    for i in range(150):
        await store.append(_record(run_id=f"r{i}"))

    await store.rotate("job1")
    records, total = await store.get_job_history("job1", limit=200)
    assert total == 100
    assert records[0]["run_id"] == "r149"


@pytest.mark.asyncio
async def test_rotate_noop_under_limit(store: CronHistoryStore) -> None:
    for i in range(5):
        await store.append(_record(run_id=f"r{i}"))

    await store.rotate("job1")
    _, total = await store.get_job_history("job1")
    assert total == 5


@pytest.mark.asyncio
async def test_rotate_nonexistent_job(store: CronHistoryStore) -> None:
    await store.rotate("nope")  # should not raise


# ── rotate_all ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_all_trims_jobs_and_index(store: CronHistoryStore, tmp_path: Path) -> None:
    for i in range(110):
        await store.append(_record(job_id="big", run_id=f"r{i}"))

    await store.rotate_all()
    _, total = await store.get_job_history("big", limit=200)
    assert total == 100


# ── delete_job_history ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_file_and_cleans_index(store: CronHistoryStore, tmp_path: Path) -> None:
    await store.append(_record(job_id="del_me", run_id="r1"))
    await store.append(_record(job_id="keep", run_id="r2"))

    result = await store.delete_job_history("del_me")
    assert result is True

    job_file = tmp_path / "cron-history" / "del_me.jsonl"
    assert not job_file.exists()

    records, _ = await store.get_all_history()
    assert all(r["job_id"] != "del_me" for r in records)


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(store: CronHistoryStore) -> None:
    assert await store.delete_job_history("nope") is False


# ── path traversal ───────────────────────────────────────────────────────


def test_path_traversal_blocked(store: CronHistoryStore) -> None:
    with pytest.raises(ValueError, match="Path traversal blocked"):
        store._job_path("../etc/passwd")


def test_path_traversal_dotdot_in_name(store: CronHistoryStore) -> None:
    with pytest.raises(ValueError, match="Path traversal blocked"):
        store._job_path("../../secret")


# ── TOCTOU: get after delete ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_history_after_delete(store: CronHistoryStore) -> None:
    await store.append(_record())
    await store.delete_job_history("job1")
    records, total = await store.get_job_history("job1")
    assert records == []
    assert total == 0


# ── atomic rotation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_atomic_no_corruption(store: CronHistoryStore, tmp_path: Path) -> None:
    for i in range(150):
        await store.append(_record(run_id=f"r{i}"))

    await store.rotate("job1")

    job_file = tmp_path / "cron-history" / "job1.jsonl"
    lines = job_file.read_text().strip().splitlines()
    assert len(lines) == 100
    for line in lines:
        json.loads(line)  # all lines valid JSON

    tmp_file = tmp_path / "cron-history" / "job1.tmp"
    assert not tmp_file.exists()


# ── CronRunRecord ────────────────────────────────────────────────────────


def test_record_to_dict_excludes_trace() -> None:
    rec = _record(trace="hidden")
    d = rec.to_dict(include_trace=False)
    assert "trace" not in d


def test_record_from_dict_ignores_extra_keys() -> None:
    data = {"run_id": "x", "job_id": "y", "extra_field": "ignored"}
    rec = CronRunRecord.from_dict(data)
    assert rec.run_id == "x"
    assert rec.job_id == "y"


# ── cron.py: concurrent guard & _job_run_meta ────────────────────────────


@pytest.mark.asyncio
async def test_run_job_returns_false_when_already_executing() -> None:
    from kiro_crew.cron import CronJob, CronService

    svc = CronService.__new__(CronService)
    svc._jobs = [CronJob(id="j1", name="test", schedule="* * * * *", message="hi")]
    svc._executing = {"j1"}
    svc._job_run_meta = {}
    svc._running_tasks = {}
    svc._file = None

    with patch.object(svc, "_sync"):
        result = await svc.run_job("j1")
    assert result is False


@pytest.mark.asyncio
async def test_run_job_stores_manual_trigger_meta() -> None:
    from kiro_crew.cron import CronJob, CronService

    svc = CronService.__new__(CronService)
    svc._jobs = [CronJob(id="j1", name="test", schedule="* * * * *", message="hi")]
    svc._executing = set()
    svc._job_run_meta = {}
    svc._running_tasks = {}
    svc._file = None

    async def fake_run(job):
        pass

    with patch.object(svc, "_sync"), patch.object(svc, "_run_job_isolated", side_effect=fake_run):
        await svc.run_job("j1")

    assert "j1" in svc._job_run_meta
    assert svc._job_run_meta["j1"][1] == "manual"
