"""Regression: the dashboard cron create handler must persist a new job in a
single locked ``add_job_async`` transaction — never mutate the returned job and
then call a bare, UNLOCKED ``state.crons._save()``.

Audit finding (GPT 5.6 HIGH, BLOCK-MERGE, ``dashboard/handlers/cron.py``):
    ``job = await state.crons.add_job_async(...)`` followed by an unlocked
    ``state.crons._save()`` lets two concurrent create requests interleave at
    the ``await``. The unlocked save re-serializes the whole in-memory job list
    outside the store lock, so it can overwrite the second request's freshly
    added job (or its fields) → persistent cron data loss.

Fix: all optional presentation/routing fields (agent, model, silent, timezone,
strict_schedule, hide_in_chat) are now passed into the single locked
``add_job_async`` build+persist, and the handler performs NO follow-up
``_save()``.

These tests fail against the pre-fix handler (which issued a second, unlocked
save when any optional field was set) and pass against the remediation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_crons_create


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


class TestCreateSingleLockedSave:
    """A create with optional fields must persist via ONE locked save."""

    @pytest.mark.asyncio
    async def test_create_with_fields_does_not_do_unlocked_second_save(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        save_calls = {"n": 0}
        real_save = crons._save

        def _counting_save() -> None:
            save_calls["n"] += 1
            real_save()

        crons._save = _counting_save  # type: ignore[method-assign]

        request = _create_request(
            {
                "name": "digest",
                "message": "summarize",
                "every": 3600,
                "agent": "researcher",
                "model": "claude-opus-4.8",
                "silent": True,
                "strict_schedule": True,
                "hide_in_chat": True,
            },
            crons,
        )
        resp = await api_crons_create(request)
        assert resp.status == 200

        # Exactly one save — the one inside the locked _persist_add_locked. The
        # pre-fix handler did a SECOND, unlocked save to flush the mutated
        # fields (save_calls["n"] would be 2), which is the data-loss window.
        assert save_calls["n"] == 1

        # And the job is persisted fully-formed (fields set under the lock).
        jobs = crons.list_jobs(include_disabled=True)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.agent_id == "researcher"
        assert job.model == "claude-opus-4.8"
        assert job.silent is True
        assert job.strict_schedule is True
        assert job.hide_in_chat is True

    @pytest.mark.asyncio
    async def test_create_without_optional_fields_still_one_save(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        save_calls = {"n": 0}
        real_save = crons._save

        def _counting_save() -> None:
            save_calls["n"] += 1
            real_save()

        crons._save = _counting_save  # type: ignore[method-assign]

        request = _create_request(
            {"name": "plain", "message": "ping", "every": 3600}, crons
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        assert save_calls["n"] == 1
        assert len(crons.list_jobs()) == 1


class TestConcurrentCreatesNoDataLoss:
    """Two concurrent creates must both survive — neither clobbers the other."""

    @pytest.mark.asyncio
    async def test_two_concurrent_creates_keep_both_jobs(self, tmp_path):
        crons = CronService(base_dir=tmp_path)

        req_a = _create_request(
            {
                "name": "job-a",
                "message": "a",
                "every": 3600,
                "agent": "agent-a",
                "model": "model-a",
            },
            crons,
        )
        req_b = _create_request(
            {
                "name": "job-b",
                "message": "b",
                "every": 7200,
                "agent": "agent-b",
                "model": "model-b",
            },
            crons,
        )

        resp_a, resp_b = await asyncio.gather(
            api_crons_create(req_a), api_crons_create(req_b)
        )
        assert resp_a.status == 200
        assert resp_b.status == 200

        jobs = crons.list_jobs(include_disabled=True)
        # Both jobs persisted — the unlocked-save race would drop one.
        assert len(jobs) == 2
        by_name = {j.name: j for j in jobs}
        assert set(by_name) == {"job-a", "job-b"}
        # Each job keeps its own distinct fields (no cross-clobber).
        assert by_name["job-a"].agent_id == "agent-a"
        assert by_name["job-a"].model == "model-a"
        assert by_name["job-b"].agent_id == "agent-b"
        assert by_name["job-b"].model == "model-b"

        # Round-trip through a fresh load proves the persisted store (not just
        # the in-memory list) holds both jobs.
        reloaded = CronService(base_dir=tmp_path)
        assert {j.name for j in reloaded.list_jobs(include_disabled=True)} == {
            "job-a",
            "job-b",
        }
