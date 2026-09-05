from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks
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
    app.router.add_get("/api/taskrunner/{task_id}/review-fix", fix_tasks.handle_get_fix_task)
    app.router.add_post("/api/taskrunner/{task_id}/review-fix/actions", fix_tasks.handle_fix_action)
    app.router.add_post(
        "/api/apps/code-review-sage/fix-tasks/{task_id}/review-again",
        lambda request: fix_tasks.handle_review_again(request, None),
    )
    return app


async def _runner_for(tmp_path, *, state: ReviewFixState, pr_url: str = "") -> TaskRunner:
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(
            state=state,
            pr_url=pr_url,
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
    run.review_fix.state = state
    run.review_fix.revision = 0
    run.revision = 0
    return runner


def _sage_finding() -> dict[str, object]:
    return {
        "headline": "Fix target",
        "severity": "red",
        "observation": "The target is inconsistent.",
        "consequence": "The review can miss the intended behavior.",
        "file": "src/example.py",
        "line": 12,
        "end_line": 14,
        "fingerprint": "finding-fingerprint",
        "suggestion": "Align the target behavior.",
    }


def _sage_report(
    *,
    band: str = "red",
    finding: dict[str, object] | None = None,
    url: str = "https://github.com/example/repo/pull/42",
) -> dict[str, object]:
    return {
        "rows": [
            {
                "url": url,
                "change_id": "change-1",
                "band": band,
                "title": "Example pull request",
                "findings": [finding or _sage_finding()],
            }
        ]
    }


def _sage_snapshot(
    finding: dict[str, object],
    *,
    key: str = "change-1:finding:0",
) -> dict[str, object]:
    return {
        "key": key,
        "title": finding["headline"],
        "severity": finding["severity"],
        "body": f"{finding['observation']}\n\n{finding['consequence']}",
        "file_path": finding["file"],
        "line": finding["line"],
        "end_line": finding["end_line"],
        "fingerprint": finding["fingerprint"],
        "suggested_fix": finding["suggestion"],
    }


@pytest.mark.asyncio
async def test_sage_validator_rejects_unavailable_and_foreign_reports(monkeypatch):
    finding = _sage_finding()
    snapshot = _sage_snapshot(finding)
    monkeypatch.setattr(fix_tasks, "_read_sage_report", lambda _run_id: None)

    with pytest.raises(fix_tasks.ReviewFixPlanError) as unavailable:
        await fix_tasks._validate_sage_findings(
            "foreign-run",
            "https://github.com/example/repo/pull/42",
            [snapshot],
        )
    assert unavailable.value.code == "review_report_unavailable"

    monkeypatch.setattr(
        fix_tasks,
        "_read_sage_report",
        lambda _run_id: _sage_report(url="https://github.com/example/repo/pull/99"),
    )
    with pytest.raises(fix_tasks.ReviewFixPlanError) as foreign:
        await fix_tasks._validate_sage_findings(
            "sage-run",
            "https://github.com/example/repo/pull/42",
            [snapshot],
        )
    assert foreign.value.code == "finding_not_owned"


@pytest.mark.asyncio
async def test_sage_validator_rejects_green_rows(monkeypatch):
    finding = _sage_finding()
    monkeypatch.setattr(
        fix_tasks,
        "_read_sage_report",
        lambda _run_id: _sage_report(band="green", finding=finding),
    )

    with pytest.raises(fix_tasks.ReviewFixPlanError) as exc_info:
        await fix_tasks._validate_sage_findings(
            "sage-run",
            "https://github.com/example/repo/pull/42",
            [_sage_snapshot(finding)],
        )
    assert exc_info.value.code == "finding_not_eligible"


@pytest.mark.asyncio
@pytest.mark.parametrize("finding_patch", [{"kind": "design"}, {"policy_only": True}])
async def test_sage_validator_rejects_design_and_policy_findings(
    monkeypatch,
    finding_patch: dict[str, object],
):
    finding = _sage_finding()
    finding.update(finding_patch)
    monkeypatch.setattr(
        fix_tasks,
        "_read_sage_report",
        lambda _run_id: _sage_report(finding=finding),
    )

    with pytest.raises(fix_tasks.ReviewFixPlanError) as exc_info:
        await fix_tasks._validate_sage_findings(
            "sage-run",
            "https://github.com/example/repo/pull/42",
            [_sage_snapshot(finding)],
        )
    assert exc_info.value.code == "finding_not_eligible"


@pytest.mark.asyncio
async def test_sage_validator_rejects_snapshot_mismatch(monkeypatch):
    finding = _sage_finding()
    monkeypatch.setattr(
        fix_tasks,
        "_read_sage_report",
        lambda _run_id: _sage_report(finding=finding),
    )
    snapshot = _sage_snapshot(finding)
    snapshot["title"] = "tampered title"

    with pytest.raises(fix_tasks.ReviewFixPlanError) as exc_info:
        await fix_tasks._validate_sage_findings(
            "sage-run",
            "https://github.com/example/repo/pull/42",
            [snapshot],
        )
    assert exc_info.value.code == "finding_snapshot_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("band", ["red", "yellow"])
async def test_sage_validator_accepts_red_and_yellow_findings(monkeypatch, band):
    finding = _sage_finding()
    finding["severity"] = band
    monkeypatch.setattr(
        fix_tasks,
        "_read_sage_report",
        lambda _run_id: _sage_report(band=band, finding=finding),
    )

    await fix_tasks._validate_sage_findings(
        "sage-run",
        "https://github.com/example/repo/pull/42",
        [_sage_snapshot(finding)],
    )


@pytest.mark.asyncio
async def test_status_and_actions_require_confirmation_and_current_cas(tmp_path):
    runner = await _runner_for(
        tmp_path,
        state=ReviewFixState.AWAITING_GROUP_CONFIRMATION,
    )
    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.get("/api/taskrunner/review-fix-http/review-fix")
        assert response.status == 200
        payload = await response.json()
        assert payload["state"] == ReviewFixState.AWAITING_GROUP_CONFIRMATION.value
        assert payload["revision"] == 0

        missing_confirmation = await client.post(
            "/api/taskrunner/review-fix-http/review-fix/actions",
            json={
                "action": "confirm_grouping",
                "expected_revision": 0,
                "target_fingerprint": "target-fingerprint",
            },
        )
        assert missing_confirmation.status == 409
        assert (await missing_confirmation.json())["code"] == "review_fix_action_rejected"

        stale = await client.post(
            "/api/taskrunner/review-fix-http/review-fix/actions",
            json={
                "action": "confirm_grouping",
                "expected_revision": 7,
                "target_fingerprint": "target-fingerprint",
                "confirmation_id": "confirm-1",
            },
        )
        assert stale.status == 409
        assert (await stale.json())["code"] == "stale_task_state"

        wrong_target = await client.post(
            "/api/taskrunner/review-fix-http/review-fix/actions",
            json={
                "action": "confirm_grouping",
                "expected_revision": 0,
                "target_fingerprint": "different-target",
                "confirmation_id": "confirm-1",
            },
        )
        assert wrong_target.status == 409
        assert (await wrong_target.json())["code"] == "stale_task_state"

        confirmed = await client.post(
            "/api/taskrunner/review-fix-http/review-fix/actions",
            json={
                "action": "confirm_grouping",
                "expected_revision": 0,
                "target_fingerprint": "target-fingerprint",
                "confirmation_id": "confirm-1",
            },
        )
        assert confirmed.status == 200
        confirmed_payload = await confirmed.json()
        assert confirmed_payload["revision"] == 1
        assert confirmed_payload["review_fix"]["groups"][0]["state"] == "confirmed"


@pytest.mark.asyncio
async def test_model_resolution_action_requires_advertised_concrete_model(tmp_path):
    runner = await _runner_for(tmp_path, state=ReviewFixState.BLOCKED_MODEL_RESOLUTION)
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    run.review_fix.model = ReviewFixModelResolution(provider="acp")

    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post(
            "/api/taskrunner/review-fix-http/review-fix/actions",
            json={
                "action": "resolve_model",
                "expected_revision": 0,
                "target_fingerprint": "target-fingerprint",
                "confirmation_id": "confirm-model",
                "model": "served-model",
                "advertised_model_ids": ["served-model"],
            },
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["state"] == ReviewFixState.AWAITING_GROUP_CONFIRMATION.value
        assert payload["review_fix"]["model"]["resolved_model_id"] == "served-model"


@pytest.mark.asyncio
async def test_review_again_is_explicit_and_forwards_parsed_body(tmp_path):
    runner = await _runner_for(
        tmp_path,
        state=ReviewFixState.PUSHED,
        pr_url="https://github.com/example/repo/pull/42",
    )
    captured: dict = {}

    async def review_again_handler(_request, body):
        captured.update(body)
        return web.json_response({"run_id": "review-2", "changes": body["changes"]})

    app = web.Application()
    app["state"] = SimpleNamespace(task_runner=runner, sessions=_Sessions())
    app.router.add_post(
        "/api/apps/code-review-sage/fix-tasks/{task_id}/review-again",
        lambda request: fix_tasks.handle_review_again(request, review_again_handler),
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/apps/code-review-sage/fix-tasks/review-fix-http/review-again",
            json={
                "expected_revision": 0,
                "target_fingerprint": "target-fingerprint",
                "confirmation_id": "review-again-1",
            },
        )
        assert response.status == 200
        assert (await response.json())["run_id"] == "review-2"

    assert captured["changes"] == ["https://github.com/example/repo/pull/42"]
    assert "_body" not in captured
    review_fix = runner.get_review_fix("review-fix-http").review_fix
    assert review_fix is not None
    assert review_fix.state is ReviewFixState.REREVIEWING


@pytest.mark.asyncio
async def test_apply_commit_and_push_are_state_gated(tmp_path):
    runner = await _runner_for(
        tmp_path,
        state=ReviewFixState.AWAITING_GROUP_CONFIRMATION,
    )
    async with TestClient(TestServer(_app(runner))) as client:
        for action in ("apply_group", "commit_group", "push_preview", "push"):
            response = await client.post(
                "/api/taskrunner/review-fix-http/review-fix/actions",
                json={
                    "action": action,
                    "expected_revision": 0,
                    "target_fingerprint": "target-fingerprint",
                    "confirmation_id": f"confirm-{action}",
                    "group_id": "group-1",
                    "commit_message": "fix review finding",
                },
            )
            assert response.status == 409
            assert (await response.json())["code"] == "review_fix_action_rejected"
