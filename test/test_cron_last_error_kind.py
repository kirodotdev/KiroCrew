"""A failed cron run records which model failure it was, not only its text.

``last_error_kind`` sits next to ``last_error`` in the job's ``crons.json`` entry and
on the run's history row: ``throttle`` or ``capacity`` from the tags the ACP
transport set from the raw error frame, empty for any other failure. The entry
writes the key only when it is set. These tests drive a real run through
``CronService.run_job`` with an error the transport itself built, and read both
records off disk.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpError, _raise_acp_error
from kiro_crew.cron import CronJob, CronService, _RunClaim
from kiro_crew.testing.wait import await_until

_ADVERTISED = ["claude-haiku-5.5", "gpt-5.6-terra"]
_THROTTLE_FRAME = {
    "code": -32603,
    "message": "Prompt failed",
    "data": {
        "message": "Too many requests, please wait before trying again.",
        "errorType": "ClientThrottleError",
        "retryErrorType": "THROTTLING",
    },
}
_CAPACITY_FRAME = {
    "code": -32603,
    "message": "Prompt failed",
    "data": "The model 'claude-haiku-5.5' is not available right now.",
}


def _transport_error(frame: dict) -> AcpError:
    with pytest.raises(AcpError) as raised:
        _raise_acp_error(frame, _ADVERTISED)
    return raised.value


def _raising(exc: Exception):
    async def callback(job: CronJob) -> None:
        raise exc

    return callback


async def _run(svc: CronService, job_id: str) -> None:
    assert await await_until(asyncio.ensure_future(svc.run_job(job_id)), what="run")


def _stored_entry(tmp_path: Path, job_id: str) -> dict:
    jobs = json.loads((tmp_path / "crons.json").read_text(encoding="utf-8"))["jobs"]
    (entry,) = [j for j in jobs if j["id"] == job_id]
    return entry


def _history_rows(tmp_path: Path, job_id: str) -> list[dict]:
    path = tmp_path / "cron-history" / f"{job_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "kind"),
    [(_THROTTLE_FRAME, "throttle"), (_CAPACITY_FRAME, "capacity")],
    ids=["throttle", "capacity"],
)
async def test_a_model_failure_stores_its_kind_in_the_registry_and_history(
    tmp_path: Path, frame: dict, kind: str
) -> None:
    svc = CronService(base_dir=tmp_path, on_job=_raising(_transport_error(frame)))
    job = svc.add_job(name="digest", message="summarise", every_secs=600)

    await _run(svc, job.id)

    entry = _stored_entry(tmp_path, job.id)
    assert entry["last_status"] == "error"
    assert entry.get("last_error_kind") == kind
    (row,) = _history_rows(tmp_path, job.id)
    assert row["status"] == "failure"
    assert row.get("last_error_kind") == kind


@pytest.mark.asyncio
async def test_a_failure_that_only_quotes_a_throttle_message_stores_no_kind(
    tmp_path: Path,
) -> None:
    worded = str(_transport_error(_THROTTLE_FRAME))
    assert "throttling requests" in worded
    quoted = AssertionError(f"banner mismatch: expected {worded!r}")
    svc = CronService(base_dir=tmp_path, on_job=_raising(quoted))
    job = svc.add_job(name="banner-check", message="check", every_secs=600)

    await _run(svc, job.id)

    entry = _stored_entry(tmp_path, job.id)
    assert entry["last_status"] == "error"
    assert "throttling requests" in entry["last_error"]
    assert entry.get("last_error_kind", "") == ""
    (row,) = _history_rows(tmp_path, job.id)
    assert row.get("last_error_kind") == ""


@pytest.mark.asyncio
async def test_a_success_after_a_throttle_clears_the_kind(tmp_path: Path) -> None:
    throttled = _transport_error(_THROTTLE_FRAME)
    calls = 0

    async def throttled_then_ok(job: CronJob) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise throttled

    svc = CronService(base_dir=tmp_path, on_job=throttled_then_ok)
    job = svc.add_job(name="digest", message="summarise", every_secs=600)

    await _run(svc, job.id)
    assert _stored_entry(tmp_path, job.id).get("last_error_kind") == "throttle"
    await _run(svc, job.id)

    entry = _stored_entry(tmp_path, job.id)
    assert entry["last_status"] == "ok"
    assert entry.get("last_error_kind", "") == ""
    rows = _history_rows(tmp_path, job.id)
    assert [r.get("last_error_kind") for r in rows] == ["throttle", ""]


@pytest.mark.asyncio
async def test_a_cancel_after_a_throttle_stores_no_kind(tmp_path: Path) -> None:
    svc = CronService(base_dir=tmp_path, on_job=_raising(_transport_error(_THROTTLE_FRAME)))
    job = svc.add_job(name="digest", message="summarise", every_secs=600)
    await _run(svc, job.id)
    assert _stored_entry(tmp_path, job.id).get("last_error_kind") == "throttle"

    # The next run is in flight when the user cancels it.
    svc._sessions = MagicMock(
        reset=AsyncMock(), _sessions={}, tearing_down=MagicMock(return_value=[])
    )
    svc._claims[job.id] = _RunClaim(
        trigger="manual",
        claimed_at=time.time() - 5,
        task=MagicMock(done=MagicMock(return_value=False)),
    )
    with patch("kiro_crew.sel.sel"):
        assert await svc.cancel(job.id) is True

    entry = _stored_entry(tmp_path, job.id)
    assert "Cancelled by user" in entry["last_error"]
    assert entry.get("last_error_kind", "") == ""
