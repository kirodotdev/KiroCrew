"""Coverage for the dashboard review-fix adapter handlers.

The adapter lazily exec-loads the Sage ``fix_tasks`` module under a private
alias; these tests drive both wrappers through real HTTP round trips so the
cold-load path, the cached-module path, and both forwarding functions run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import review_fix as adapter
from kiro_crew.task_models import (
    ReviewFixDependencyGroup,
    ReviewFixMetadata,
    ReviewFixModelResolution,
    ReviewFixState,
    ReviewFixTargetSnapshot,
)
from kiro_crew.taskrunner import TaskRunner


class _Sessions:
    _sessions: dict = {}


def _app(runner: TaskRunner) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(task_runner=runner, sessions=_Sessions())
    app.router.add_get("/rf/{task_id}", adapter.api_taskrunner_review_fix)
    app.router.add_post("/rf/{task_id}/actions", adapter.api_taskrunner_review_fix_actions)
    return app


@pytest.mark.asyncio
async def test_adapter_cold_load_then_cached_module_serves_both_wrappers(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "work")
    await runner.create_review_fix(
        ReviewFixMetadata(
            state=ReviewFixState.AWAITING_GROUP_CONFIRMATION,
            target=ReviewFixTargetSnapshot(
                repo_root=str(tmp_path),
                target_path=str(tmp_path),
                dirty_fingerprint="target-fingerprint",
            ),
            model=ReviewFixModelResolution(
                requested_model="served-model",
                provider="acp",
                resolved_model_id="served-model",
                advertised_model_ids=["served-model"],
            ),
            groups=[ReviewFixDependencyGroup(group_id="group-1", finding_keys=["finding-1"])],
        ),
        task_id="review-fix-http",
    )
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_GROUP_CONFIRMATION
    run.review_fix.revision = 0
    run.revision = 0

    async with TestClient(TestServer(_app(runner))) as client:
        status = await client.get("/rf/review-fix-http")
        assert status.status == 200
        status_payload = await status.json()
        assert status_payload["state"] == "awaiting_group_confirmation"
        assert status_payload["revision"] == 0

        confirmed = await client.post(
            "/rf/review-fix-http/actions",
            json={
                "action": "confirm_grouping",
                "expected_revision": 0,
                "target_fingerprint": "target-fingerprint",
                "confirmation_id": "confirm-1",
            },
        )
        assert confirmed.status == 200, await confirmed.text()
        assert (await confirmed.json())["revision"] == 1

        cached_status = await client.get("/rf/review-fix-http")
        assert cached_status.status == 200
