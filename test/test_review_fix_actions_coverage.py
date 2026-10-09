from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Importing ``unsandboxed_git`` registers its autouse fixture for this module;
# only the real-git tests can run without the sandbox backend.
from review_fix_helpers import unsandboxed_git, with_owner_dashboard  # noqa: F401

from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks
from kiro_crew.review_fix import ReviewFixPlanError, capture_group_patch
from kiro_crew.review_fix_git import ReviewFixPatch
from kiro_crew.task_models import (
    Project,
    ReviewFixDependencyGroup,
    ReviewFixFindingSnapshot,
    ReviewFixGroupState,
    ReviewFixMetadata,
    ReviewFixModelResolution,
    ReviewFixState,
    ReviewFixTargetSnapshot,
)
from kiro_crew.taskrunner import TaskRunner


class _Sessions:
    _sessions: dict = {}


@pytest.fixture(autouse=True)
def _sage_app_enabled(_floor_monkeypatch):
    # These tests exercise the handlers' own logic; the enablement gate has its
    # own tests in test_review_fix_routes.py.
    _floor_monkeypatch.setattr(fix_tasks, "is_app_enabled", lambda name: True)


def _app(runner: TaskRunner | None) -> web.Application:
    app = with_owner_dashboard(web.Application())
    app["state"] = SimpleNamespace(task_runner=runner, sessions=_Sessions())
    app.router.add_get("/rf/{task_id}", fix_tasks.handle_get_fix_task)
    app.router.add_post("/rf/{task_id}/actions", fix_tasks.handle_fix_action)
    app.router.add_post("/create", fix_tasks.handle_create_fix_task)
    app.router.add_post(
        "/rf/{task_id}/review-again",
        lambda request: fix_tasks.handle_review_again(request, None),
    )
    return with_owner_dashboard(app)


async def _runner_for(
    tmp_path, *, state: ReviewFixState, task_id: str = "review-fix-http"
) -> TaskRunner:
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "work" / task_id)
    await runner.create_review_fix(
        ReviewFixMetadata(
            state=state,
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
            finding_snapshots=(ReviewFixFindingSnapshot(key="finding-1", title="Fix target"),),
            groups=[ReviewFixDependencyGroup(group_id="group-1", finding_keys=["finding-1"])],
        ),
        task_id=task_id,
    )
    run = runner.get_review_fix(task_id)
    assert run.review_fix is not None
    run.review_fix.state = state
    run.review_fix.revision = 0
    run.revision = 0
    return runner


def _action_body(action: str, revision: int = 0, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "action": action,
        "expected_revision": revision,
        "target_fingerprint": "target-fingerprint",
        "confirmation_id": f"confirm-{action}",
    }
    body.update(extra)
    return body


def test_payload_redacts_untrusted_status_and_metadata_text():
    """Model-written status and metadata cannot leak through responses."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    leak = f"issue {secret} https://evil.example/?x={secret}"
    metadata = ReviewFixMetadata(
        state=ReviewFixState.BLOCKED_VALIDATION,
        blocked_reason=leak,
        target=ReviewFixTargetSnapshot(dirty_fingerprint="fp"),
        groups=[ReviewFixDependencyGroup(group_id="group-1")],
    )
    run = Project(
        task_id="review-fix-leak", spec_path="x", spec_content="x", error=leak, review_fix=metadata
    )

    serialized = str(fix_tasks._payload(run))

    assert secret not in serialized
    assert leak not in serialized
    assert "[REDACTED: suspicious URL" in serialized


def _create_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "target_path": "/repo",
        "findings": [{"key": "red", "title": "Fix target", "path": "target.txt"}],
        "review_run_id": "sage-run",
        "pr_url": "https://github.com/example/repo/pull/42",
        "advertised_model_ids": ["served-model"],
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_create_endpoint_validates_request_and_reports_blocked_states(tmp_path, monkeypatch):
    async with TestClient(TestServer(_app(None))) as client:
        response = await client.post("/create", json={})
        assert response.status == 503
        assert (await response.json())["code"] == "task_runner_unavailable"

    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)

    async def ok_validator(*_args, **_kwargs):
        return None

    monkeypatch.setattr(fix_tasks, "_validate_sage_findings", ok_validator)

    async with TestClient(TestServer(_app(runner))) as client:
        bad_json = await client.post(
            "/create", data="{not json", headers={"Content-Type": "application/json"}
        )
        assert bad_json.status == 400
        assert (await bad_json.json())["code"] == "invalid_json"

        for payload, code in (
            ({"target_path": "   "}, "target_required"),
            (_create_body(target_path=None), "target_required"),
            (_create_body(findings=[]), "findings_required"),
            (_create_body(review_run_id=""), "review_run_required"),
            (_create_body(pr_url=None), "review_pr_required"),
        ):
            response = await client.post("/create", json=payload)
            assert response.status == 400, payload
            assert (await response.json())["code"] == code

        async def plan_error(*_args, **_kwargs):
            raise fix_tasks.ReviewFixPlanError("nope")

        monkeypatch.setattr(fix_tasks, "_validate_sage_findings", plan_error)
        rejected = await client.post("/create", json=_create_body())
        assert rejected.status == 400
        assert (await rejected.json())["code"] == "invalid_review_fix"

        monkeypatch.setattr(fix_tasks, "_validate_sage_findings", ok_validator)

        async def model_error(_runner, **_kwargs):
            raise fix_tasks.ReviewFixModelResolutionError("unserved")

        monkeypatch.setattr(fix_tasks, "create_review_fix_task", model_error)
        failed = await client.post("/create", json=_create_body())
        assert failed.status == 400
        assert (await failed.json())["code"] == "blocked_model_resolution"

        async def boom(_runner, **_kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(fix_tasks, "create_review_fix_task", boom)
        crashed = await client.post("/create", json=_create_body())
        assert crashed.status == 400
        assert (await crashed.json())["code"] == "review_fix_creation_failed"

        created: dict = {}

        async def realish_create(_runner, **kwargs):
            created.update(kwargs)
            run = runner.get_review_fix("review-fix-http")
            run.revision = 0
            run.review_fix.revision = 0
            runs = created.setdefault("calls", 0) + 1
            created["calls"] = runs
            run.review_fix.state = (
                ReviewFixState.AWAITING_GROUP_CONFIRMATION
                if runs == 1
                else ReviewFixState.BLOCKED_DIRTY_OVERLAP
            )
            return run

        monkeypatch.setattr(fix_tasks, "create_review_fix_task", realish_create)
        ok = await client.post("/create", json=_create_body(name="fix it"))
        assert ok.status == 201
        assert created["name"] == "fix it"
        assert created["advertised_model_ids"] == ["served-model"]
        assert created["candidate_root"] is None

        accepted = await client.post("/create", json=_create_body(candidate_root="/tmp/c"))
        assert accepted.status == 202
        assert (await accepted.json())["state"] == ReviewFixState.BLOCKED_DIRTY_OVERLAP.value


# Local-review validation reads the persisted session, not the request body.


def _local_create_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "target_path": "/repo",
        "findings": [{"id": "finding-1", "fingerprint": "fp-1"}],
        "local_session_id": "local-sess-1",
        "advertised_model_ids": ["served-model"],
    }
    body.update(overrides)
    return body


def _local_session(**overrides: object) -> dict:
    session: dict[str, object] = {
        "id": "local-sess-1",
        "repository": "/repo",
        "findings": [
            {
                "id": "finding-1",
                "fingerprint": "fp-1",
                "title": "Server title",
                "severity": "error",
                "message": "Server message",
                "file": "src/example.py",
                "line": 12,
                "end_line": 14,
                "suggestion": "Server suggestion",
            }
        ],
    }
    session.update(overrides)
    return session


@pytest.mark.asyncio
async def test_create_endpoint_accepts_a_valid_local_selection(tmp_path, monkeypatch):
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    monkeypatch.setattr(fix_tasks, "_read_local_session", lambda sid: _local_session())

    captured: dict = {}

    async def realish_create(_runner, **kwargs):
        captured.update(kwargs)
        run = runner.get_review_fix("review-fix-http")
        run.revision = 0
        run.review_fix.revision = 0
        run.review_fix.state = ReviewFixState.AWAITING_GROUP_CONFIRMATION
        return run

    monkeypatch.setattr(fix_tasks, "create_review_fix_task", realish_create)

    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post("/create", json=_local_create_body())

    assert response.status == 201
    # The persisted session copy is authoritative.
    (finding,) = captured["findings"]
    assert finding["key"] == "finding-1"
    assert finding["title"] == "Server title"
    assert finding["body"] == "Server message"
    assert finding["file_path"] == "src/example.py"
    assert finding["suggested_fix"] == "Server suggestion"


@pytest.mark.asyncio
async def test_create_endpoint_ignores_a_tampered_finding_body_in_favor_of_the_server_copy(
    tmp_path, monkeypatch
):
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    monkeypatch.setattr(fix_tasks, "_read_local_session", lambda sid: _local_session())

    captured: dict = {}

    async def realish_create(_runner, **kwargs):
        captured.update(kwargs)
        run = runner.get_review_fix("review-fix-http")
        run.revision = 0
        run.review_fix.revision = 0
        run.review_fix.state = ReviewFixState.AWAITING_GROUP_CONFIRMATION
        return run

    monkeypatch.setattr(fix_tasks, "create_review_fix_task", realish_create)

    tampered = _local_create_body(
        findings=[
            {
                "id": "finding-1",
                "fingerprint": "fp-1",
                "title": "attacker title",
                "severity": "info",
                "body": "attacker body",
                "file_path": "attacker.py",
                "suggested_fix": "attacker fix",
            }
        ]
    )

    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post("/create", json=tampered)

    assert response.status == 201
    (finding,) = captured["findings"]
    assert finding["title"] == "Server title"
    assert finding["file_path"] == "src/example.py"
    assert "attacker" not in str(finding)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_factory,body_overrides,expected_code",
    [
        (
            lambda: _local_session(),
            {"findings": [{"id": "not-in-session"}]},
            "finding_not_owned",
        ),
        (
            lambda: _local_session(repository="/somewhere-else"),
            {},
            "target_repository_mismatch",
        ),
        (lambda: None, {}, "local_session_not_found"),
    ],
    ids=["unknown-finding-id", "repository-mismatch", "missing-session"],
)
async def test_create_endpoint_rejects_an_invalid_local_session(
    tmp_path, monkeypatch, session_factory, body_overrides, expected_code
):
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    monkeypatch.setattr(fix_tasks, "_read_local_session", lambda sid: session_factory())

    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post("/create", json=_local_create_body(**body_overrides))
        assert response.status == 400
        assert (await response.json())["code"] == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"review_run_id": "sage-run-1"},
        {"pr_url": "https://github.com/example/repo/pull/1"},
        {"review_run_id": "sage-run-1", "pr_url": "https://github.com/example/repo/pull/1"},
    ],
)
async def test_create_endpoint_rejects_local_session_id_combined_with_review_run(
    tmp_path, monkeypatch, overrides
):
    """Reject mixed sources before either validation path can create a task."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    monkeypatch.setattr(fix_tasks, "_read_local_session", lambda sid: _local_session())

    called = False

    async def unexpected_create(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("create_review_fix_task must not be called")

    monkeypatch.setattr(fix_tasks, "create_review_fix_task", unexpected_create)

    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post("/create", json=_local_create_body(**overrides))
        assert response.status == 400
        assert (await response.json())["code"] == "local_and_review_run_conflict"
    assert called is False


@pytest.mark.asyncio
async def test_create_endpoint_local_path_passes_empty_review_run_id_and_pr_url(
    tmp_path, monkeypatch
):
    """The local path must not let a stray request field leak into task metadata."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    monkeypatch.setattr(fix_tasks, "_read_local_session", lambda sid: _local_session())

    captured: dict = {}

    async def realish_create(_runner, **kwargs):
        captured.update(kwargs)
        run = runner.get_review_fix("review-fix-http")
        run.revision = 0
        run.review_fix.revision = 0
        run.review_fix.state = ReviewFixState.AWAITING_GROUP_CONFIRMATION
        return run

    monkeypatch.setattr(fix_tasks, "create_review_fix_task", realish_create)

    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post("/create", json=_local_create_body())

    assert response.status == 201
    assert captured["review_run_id"] == ""
    assert captured["pr_url"] == ""


def _sage_report(finding: dict) -> dict:
    return {
        "rows": [
            {
                "url": "https://github.com/example/repo/pull/1",
                "change_id": "change-1",
                "band": "red",
                "title": "row title",
                "findings": [finding],
            }
        ]
    }


@pytest.mark.asyncio
async def test_validate_sage_findings_rejects_an_alias_key_that_disagrees_with_canonical(
    monkeypatch,
):
    """A matching canonical field cannot hide a differing untrusted alias."""
    report = _sage_report(
        {
            "severity": "red",
            "headline": "Finding headline",
            "observation": "",
            "consequence": "",
            "file": "target.py",
            "line": 10,
            "fingerprint": "fp-1",
            "suggestion": "",
        }
    )
    monkeypatch.setattr(fix_tasks, "_read_sage_report", lambda _run_id: report)

    tampered = {
        "key": "change-1:finding:0",
        "title": "Finding headline",
        "severity": "red",
        "body": "",
        "file_path": "target.py",
        "line": 10,
        "end_line": None,
        "fingerprint": "fp-1",
        "suggested_fix": "",
        "fix": "rm -rf /",  # the alias _finding_snapshot would otherwise fall back to
    }

    with pytest.raises(ReviewFixPlanError) as excinfo:
        await fix_tasks._validate_sage_findings(
            "run-1", "https://github.com/example/repo/pull/1", [tampered]
        )
    assert excinfo.value.code == "finding_snapshot_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observation,consequence,file_path,suggestion,expected_body",
    [
        # Matching aliases and intentional blank optional fields must still pass.
        ("root cause", "impact", "target.py", "do the fix", "root cause\n\nimpact"),
        ("", "", "", "", ""),
    ],
    ids=["matching-canonical-snapshot", "empty-optional-fields"],
)
async def test_validate_sage_findings_accepts_honest_snapshots(
    monkeypatch, observation, consequence, file_path, suggestion, expected_body
):
    report = _sage_report(
        {
            "severity": "red",
            "headline": "Finding headline",
            "observation": observation,
            "consequence": consequence,
            "file": file_path,
            "line": 10,
            "fingerprint": "fp-1",
            "suggestion": suggestion,
        }
    )
    monkeypatch.setattr(fix_tasks, "_read_sage_report", lambda _run_id: report)

    honest = {
        "key": "change-1:finding:0",
        "title": "Finding headline",
        "severity": "red",
        "body": expected_body,
        "file_path": file_path,
        "line": 10,
        "end_line": None,
        "fingerprint": "fp-1",
        "suggested_fix": suggestion,
    }

    await fix_tasks._validate_sage_findings(
        "run-1", "https://github.com/example/repo/pull/1", [honest]
    )


@pytest.mark.asyncio
async def test_apply_then_commit_group_happy_path(tmp_path, monkeypatch):
    from review_fix_helpers import _repo as make_repo

    repo = make_repo(tmp_path)
    runner = await _runner_for(tmp_path, state=ReviewFixState.READY_TO_APPLY)
    metadata = runner.get_review_fix("review-fix-http").review_fix
    assert metadata is not None
    metadata.target.target_path = str(repo)
    metadata.target.repo_root = str(repo)
    metadata.target.branch_name = "feature/fix"
    metadata.target.head_sha = "0" * 40
    metadata.git.candidate_worktree_path = str(tmp_path / "candidate")
    Path(metadata.git.candidate_worktree_path).mkdir(exist_ok=True)
    metadata.groups[0].state = ReviewFixGroupState.READY_TO_APPLY
    metadata.groups[0].affected_files = ("target.txt",)

    async def same_target(*_args, **_kwargs):
        return metadata.target

    async def fake_candidate_patch(*_args, **_kwargs):
        return ReviewFixPatch(
            patch_id="p1", patch_text="diff --git a/target.txt\n", paths=("target.txt",)
        )

    async def fake_apply(*_args, **_kwargs):
        return None

    monkeypatch.setattr(fix_tasks.review_fix_git, "inspect_target", same_target)
    monkeypatch.setattr(fix_tasks.review_fix_git, "candidate_patch", fake_candidate_patch)
    monkeypatch.setattr(fix_tasks.review_fix_git, "apply_patch", fake_apply)

    # Apply requires a captured patch: pin the id first (the capture bumps the
    # task revision to 1).
    await capture_group_patch(
        runner, "review-fix-http", "group-1", expected_revision=0, expected_group_revision=0
    )

    async with TestClient(TestServer(_app(runner))) as client:
        applied = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body("apply_group", revision=1, group_id="group-1"),
        )
        assert applied.status == 200
        payload = await applied.json()
        assert payload["state"] == ReviewFixState.AWAITING_COMMIT.value
        assert payload["review_fix"]["groups"][0]["state"] == ReviewFixGroupState.APPLIED.value
        assert payload["review_fix"]["groups"][0]["patch_path"]
        # The captured id survives apply untouched.
        assert payload["review_fix"]["groups"][0]["candidate_patch_id"] == "p1"

        async def fake_commit(*_args, **_kwargs):
            return "abc1234"

        monkeypatch.setattr(fix_tasks.review_fix_git, "commit_group", fake_commit)
        committed = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body(
                "commit_group",
                revision=payload["revision"],
                group_id="group-1",
                commit_message="fix: align target",
            ),
        )
        assert committed.status == 200
        commit_payload = await committed.json()
        assert commit_payload["state"] == ReviewFixState.COMMITTED.value
        assert commit_payload["review_fix"]["groups"][0]["commit_hash"] == "abc1234"


async def _real_candidate_harness(tmp_path, *, state: ReviewFixState, bind: str):
    """Real repo/task/candidate harness shared by git-mutation tests."""
    from review_fix_helpers import _repo as make_repo

    repo = make_repo(tmp_path)
    runner = await _runner_for(tmp_path, state=state)
    metadata = runner.get_review_fix("review-fix-http").review_fix
    assert metadata is not None
    target = await fix_tasks.review_fix_git.inspect_target(repo)
    candidate = await fix_tasks.review_fix_git.create_candidate(
        target, tmp_path / "candidate", f"kirocrew/review-fix/{bind}"
    )
    metadata.target = target
    metadata.git = candidate
    return repo, runner, metadata, target, candidate


@pytest.mark.asyncio
async def test_apply_rejects_a_candidate_that_changed_after_validation(tmp_path):
    """Apply must reject candidate drift since the validated capture."""
    repo, runner, metadata, target, _candidate = await _real_candidate_harness(
        tmp_path, state=ReviewFixState.READY_TO_APPLY, bind="bind-1"
    )
    metadata.groups[0].state = ReviewFixGroupState.READY_TO_APPLY
    metadata.groups[0].affected_files = ["target.txt"]

    candidate_file = tmp_path / "candidate" / "target.txt"
    candidate_file.write_text("validated\n", encoding="utf-8")
    await capture_group_patch(
        runner,
        "review-fix-http",
        "group-1",
        expected_revision=0,
        expected_group_revision=0,
    )
    # mutate_review_fix works on a deep copy, so the bound id lives on the
    # run's current metadata, not on the object captured above.
    metadata = runner.get_review_fix("review-fix-http").review_fix
    assert metadata is not None
    validated_patch_id = metadata.groups[0].candidate_patch_id
    assert validated_patch_id

    # The drift the review-fix flow exists to catch: the candidate changes
    # between the validated capture and the operator's Apply click.
    candidate_file.write_text("tampered after validation\n", encoding="utf-8")
    captured_run = runner.get_review_fix("review-fix-http")
    assert captured_run.revision == 1  # capture bumped the task revision

    async with TestClient(TestServer(_app(runner))) as client:
        applied = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body(
                "apply_group",
                revision=1,
                group_id="group-1",
                target_fingerprint=target.dirty_fingerprint,
            ),
        )
        assert applied.status == 409, await applied.text()
        body = await applied.json()
        assert body["code"] == "review_fix_action_rejected"
        assert "candidate changed after validation" in body["error"]
        # Nothing reached the target: the rejected apply must be side-effect free.
        assert (repo / "target.txt").read_text(encoding="utf-8") == "before\n"
        stored = runner.get_review_fix("review-fix-http").review_fix
        assert stored is not None
        assert stored.groups[0].candidate_patch_id == validated_patch_id


@pytest.mark.asyncio
async def test_apply_requires_a_captured_patch_id(tmp_path, monkeypatch):
    """An empty candidate patch id must be refused at Apply."""
    repo, runner, metadata, target, _candidate = await _real_candidate_harness(
        tmp_path, state=ReviewFixState.READY_TO_APPLY, bind="bind-2"
    )
    metadata.groups[0].state = ReviewFixGroupState.READY_TO_APPLY
    metadata.groups[0].affected_files = ["target.txt"]

    async def fake_candidate_patch(*_args, **_kwargs):
        return ReviewFixPatch(
            patch_id="disk-id", patch_text="diff --git a/target.txt\n", paths=("target.txt",)
        )

    monkeypatch.setattr(fix_tasks.review_fix_git, "candidate_patch", fake_candidate_patch)

    async with TestClient(TestServer(_app(runner))) as client:
        applied = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body(
                "apply_group",
                group_id="group-1",
                target_fingerprint=target.dirty_fingerprint,
            ),
        )
        assert applied.status == 409, await applied.text()
        body = await applied.json()
        assert body["code"] == "review_fix_action_rejected"
        assert "group was never captured" in body["error"]
        # Nothing was applied and the group id remains unset.
        assert (repo / "target.txt").read_text(encoding="utf-8") == "before\n"
        stored = runner.get_review_fix("review-fix-http").review_fix
        assert stored is not None
        assert stored.groups[0].candidate_patch_id == ""


@pytest.mark.asyncio
async def test_apply_of_captured_group_no_longer_rebinds_the_id(tmp_path, monkeypatch):
    """Apply verifies the captured id and never rebinds it."""
    from review_fix_helpers import _repo as make_repo

    repo = make_repo(tmp_path)
    runner = await _runner_for(tmp_path, state=ReviewFixState.READY_TO_APPLY)
    metadata = runner.get_review_fix("review-fix-http").review_fix
    assert metadata is not None
    metadata.target.target_path = str(repo)
    metadata.target.repo_root = str(repo)
    metadata.target.head_sha = "0" * 40
    metadata.git.candidate_worktree_path = str(tmp_path / "candidate")
    Path(metadata.git.candidate_worktree_path).mkdir(exist_ok=True)
    metadata.groups[0].state = ReviewFixGroupState.READY_TO_APPLY
    metadata.groups[0].affected_files = ["target.txt"]
    metadata.groups[0].candidate_patch_id = "captured-id"

    async def same_target(*_args, **_kwargs):
        return metadata.target

    async def fake_candidate_patch(*_args, **_kwargs):
        return ReviewFixPatch(
            patch_id="disk-id", patch_text="diff --git a/target.txt\n", paths=("target.txt",)
        )

    async def fake_apply(*_args, **_kwargs):
        return None

    monkeypatch.setattr(fix_tasks.review_fix_git, "inspect_target", same_target)
    monkeypatch.setattr(fix_tasks.review_fix_git, "candidate_patch", fake_candidate_patch)
    monkeypatch.setattr(fix_tasks.review_fix_git, "apply_patch", fake_apply)

    async with TestClient(TestServer(_app(runner))) as client:
        applied = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body("apply_group", group_id="group-1"),
        )
        assert applied.status == 409, await applied.text()
        body = await applied.json()
        assert "candidate changed after validation" in body["error"]
        stored = runner.get_review_fix("review-fix-http").review_fix
        assert stored is not None
        # The mismatch was REFUSED, not absorbed by overwriting the stored id.
        assert stored.groups[0].candidate_patch_id == "captured-id"


@pytest.mark.asyncio
async def test_capture_refuses_a_group_with_no_owned_files(tmp_path):
    """A fileless group would capture an unscoped whole-worktree patch."""
    _repo, runner, metadata, _target, _candidate = await _real_candidate_harness(
        tmp_path, state=ReviewFixState.AWAITING_VALIDATION, bind="bind-3"
    )
    metadata.groups[0].affected_files = []  # the fileless group

    with pytest.raises(ReviewFixPlanError, match="owns no files") as excinfo:
        await capture_group_patch(
            runner,
            "review-fix-http",
            "group-1",
            expected_revision=0,
            expected_group_revision=0,
        )
    assert excinfo.value.code == "fileless_group"


@pytest.mark.asyncio
@pytest.mark.parametrize("omitted_build", [{}, {"build_command": None}, {"build_command": []}])
async def test_validate_group_route_accepts_a_test_only_request(
    tmp_path, monkeypatch, omitted_build
):
    """Any omitted build command becomes an explicit None."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_VALIDATION)

    received: dict[str, object] = {}

    async def fake_validate(_runner, _task_id, _group_id, **kwargs):
        received.update(kwargs)
        return {"validated": True}, True

    monkeypatch.setattr(fix_tasks, "validate_group", fake_validate)
    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body(
                "validate_group",
                group_id="group-1",
                expected_group_revision=0,
                test_command=["pytest", "-q"],
                **omitted_build,
            ),
        )
        assert response.status == 200, await response.text()

    assert received["build_command"] is None
    assert received["test_command"] == ["pytest", "-q"]


@pytest.mark.asyncio
async def test_validate_group_route_rejects_a_malformed_build_command(tmp_path):
    """A present malformed build command is a request error, not a skip."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_VALIDATION)
    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body(
                "validate_group",
                group_id="group-1",
                expected_group_revision=0,
                test_command=["pytest", "-q"],
                build_command=["make", 1],
            ),
        )
        assert response.status == 409, await response.text()
        assert "build_command" in (await response.json())["error"]


def _preview(commits: tuple[str, ...] = ("abc1234 fix: align target",)) -> dict[str, object]:
    return {
        "remote": "origin",
        "branch": "feature/fix",
        "upstream": "origin/feature/fix",
        "commits": list(commits),
        "files": ["target.txt"],
        "diverged": False,
    }


@pytest.mark.asyncio
async def test_push_preview_push_and_discard_actions(tmp_path, monkeypatch):
    runner = await _runner_for(tmp_path, state=ReviewFixState.COMMITTED)
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    run.review_fix.groups[0].state = ReviewFixGroupState.COMMITTED

    async def fake_preview(*_args, **_kwargs):
        return _preview()

    async def fake_push(*_args, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr(fix_tasks.review_fix_git, "push_preview", fake_preview)
    monkeypatch.setattr(fix_tasks.review_fix_git, "push", fake_push)

    async with TestClient(TestServer(_app(runner))) as client:
        preview = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("push_preview")
        )
        assert preview.status == 200, await preview.text()
        assert (await preview.json())["state"] == ReviewFixState.AWAITING_PUSH.value

        pushed = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("push", revision=1)
        )
        assert pushed.status == 200, await pushed.text()
        assert (await pushed.json())["state"] == ReviewFixState.PUSHED.value


@pytest.mark.asyncio
async def test_push_rejects_when_head_advanced_after_the_approved_preview(tmp_path, monkeypatch):
    """Push must reject branch drift since the stored approved preview."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.COMMITTED)
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    run.review_fix.groups[0].state = ReviewFixGroupState.COMMITTED
    previews: list[dict[str, object]] = [_preview()]
    pushes: list[tuple[object, ...]] = []

    async def moving_preview(*_args, **_kwargs):
        return previews[-1]

    async def recording_push(*_args, **_kwargs):
        pushes.append(_args)
        return {"remote": _args[1], "branch": _args[2], "pushed": True}

    monkeypatch.setattr(fix_tasks.review_fix_git, "push_preview", moving_preview)
    monkeypatch.setattr(fix_tasks.review_fix_git, "push", recording_push)

    async with TestClient(TestServer(_app(runner))) as client:
        preview = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("push_preview")
        )
        assert preview.status == 200, await preview.text()

        previews.append(_preview(commits=("abc1234 fix: align target", "deadbee unreviewed")))
        pushed = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("push", revision=1)
        )
        assert pushed.status == 409, await pushed.text()
        body = await pushed.json()
        assert body["error"] == "push preview is stale; request a new push preview"
        assert pushes == []

        # Re-approving with the current state unblocks the push.
        run = runner.get_review_fix("review-fix-http")
        run.revision = 1
        run.review_fix.revision = 1
        approved = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("push_preview", revision=1)
        )
        assert approved.status == 200, await approved.text()
        pushed_now = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("push", revision=2)
        )
        assert pushed_now.status == 200, await pushed_now.text()
        assert (await pushed_now.json())["state"] == ReviewFixState.PUSHED.value
        assert len(pushes) == 1


@pytest.mark.asyncio
async def test_push_revalidates_state_under_the_lock_and_rejects_a_stale_call(
    tmp_path, monkeypatch
):
    """Push revalidates under the lock and never calls Git when stale."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.COMMITTED)
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    run.review_fix.groups[0].state = ReviewFixGroupState.COMMITTED

    push_calls: list[object] = []

    async def fake_preview(*_args, **_kwargs):
        return _preview()

    async def recording_push(*args, **_kwargs):
        push_calls.append(args)
        return {"pushed": True}

    monkeypatch.setattr(fix_tasks.review_fix_git, "push_preview", fake_preview)
    monkeypatch.setattr(fix_tasks.review_fix_git, "push", recording_push)

    with pytest.raises(fix_tasks.ReviewFixConflict):
        await fix_tasks._push(
            runner, "review-fix-http", expected=99, fingerprint="target-fingerprint"
        )

    assert push_calls == []


@pytest.mark.asyncio
async def test_discard_candidate_revalidates_state_under_the_lock(tmp_path, monkeypatch):
    """Discard revalidates under the lock and never touches a stale candidate."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_COMMIT)
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    run.review_fix.git.candidate_worktree_path = str(tmp_path / "candidate")

    discard_calls: list[object] = []

    async def recording_discard(*args, **_kwargs):
        discard_calls.append(args)

    monkeypatch.setattr(fix_tasks.review_fix_git, "discard_candidate", recording_discard)

    with pytest.raises(fix_tasks.ReviewFixConflict):
        await fix_tasks._discard_candidate(
            runner, "review-fix-http", expected=99, fingerprint="target-fingerprint"
        )

    assert discard_calls == []
    # State never transitioned either: the reject happened before mutate_review_fix.
    reloaded = runner.get_review_fix("review-fix-http")
    assert reloaded.review_fix.state is ReviewFixState.AWAITING_COMMIT


@pytest.mark.asyncio
async def test_discard_from_awaiting_commit_removes_the_worktree(tmp_path):
    """Discard transitions before destroying the candidate worktree."""
    from review_fix_helpers import _repo as make_repo

    repo = make_repo(tmp_path)
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_COMMIT)
    metadata = runner.get_review_fix("review-fix-http").review_fix
    assert metadata is not None
    target = await fix_tasks.review_fix_git.inspect_target(repo)
    candidate = await fix_tasks.review_fix_git.create_candidate(
        target, tmp_path / "candidate", "kirocrew/review-fix/discard-1"
    )
    metadata.target = target
    metadata.git = candidate

    async with TestClient(TestServer(_app(runner))) as client:
        discarded = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body("discard_candidate", target_fingerprint=target.dirty_fingerprint),
        )
        assert discarded.status == 200, await discarded.text()
        payload = await discarded.json()
        assert payload["state"] == ReviewFixState.DONE.value
        assert payload["review_fix"]["git"]["candidate_worktree_path"] == ""
        assert not (tmp_path / "candidate").exists()


@pytest.mark.asyncio
async def test_discard_survives_worktree_removal_failure(tmp_path, monkeypatch):
    """A removal failure leaves the DONE run recoverable, not bricked."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_COMMIT)
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    run.review_fix.git.candidate_worktree_path = str(tmp_path / "candidate")

    async def exploding_remove(*_args, **_kwargs):
        raise RuntimeError("worktree busy: SECRET-INTERNAL-DETAIL")

    monkeypatch.setattr(fix_tasks.review_fix_git, "discard_candidate", exploding_remove)

    async with TestClient(TestServer(_app(runner))) as client:
        discarded = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("discard_candidate")
        )
        assert discarded.status == 200, await discarded.text()
        payload = await discarded.json()
        assert payload["state"] == ReviewFixState.DONE.value
        logs = "\n".join(payload["review_fix"]["logs"])
        assert "candidate worktree could not be removed" in logs
        assert "worktree busy" in logs


@pytest.mark.asyncio
async def test_get_and_create_edge_guards(tmp_path):
    async with TestClient(TestServer(_app(None))) as client:
        missing_runner = await client.get("/rf/any-task")
        assert missing_runner.status == 503

    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    async with TestClient(TestServer(_app(runner))) as client:
        unknown = await client.get("/rf/nope")
        assert unknown.status == 404
        assert (await unknown.json())["code"] == "not_found"

        denied = await client.post(
            "/create",
            json=_create_body(target_path=str(Path.home() / ".aws")),
        )
        assert denied.status == 403
        assert (await denied.json())["code"] == "target_denied"

        bad_review_json = await client.post(
            "/rf/review-fix-http/review-again",
            data="{nope",
            headers={"Content-Type": "application/json"},
        )
        assert bad_review_json.status == 400
        assert (await bad_review_json.json())["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_retry_capture_and_validate_dispatches(tmp_path, monkeypatch):
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    run = runner.get_review_fix("review-fix-http")

    run.review_fix.state = ReviewFixState.BLOCKED_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

    async def fake_execute(_task_id, **_kwargs):
        return "execution-task-1"

    monkeypatch.setattr(runner, "execute_review_fix", fake_execute)
    async with TestClient(TestServer(_app(runner))) as client:
        retried = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("retry", revision=0)
        )
        assert retried.status == 202, await retried.text()
        assert (await retried.json())["execution_task_id"] == "execution-task-1"

    run.revision = 1
    run.review_fix.revision = 1
    with monkeypatch.context() as mp:
        mp.setattr(fix_tasks, "is_app_enabled", lambda name: True)

    async def fake_capture(_runner, _task_id, _group_id, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr(fix_tasks, "capture_group_patch", fake_capture)
    async with TestClient(TestServer(_app(runner))) as client:
        captured = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body(
                "capture_group_patch",
                revision=1,
                group_id="group-1",
                expected_group_revision=0,
            ),
        )
        assert captured.status == 200, await captured.text()

    run.revision = 2
    run.review_fix.revision = 2
    with monkeypatch.context() as mp:
        mp.setattr(fix_tasks, "is_app_enabled", lambda name: True)

    async def fake_validate(_runner, _task_id, _group_id, **_kwargs):
        return {"validated": True}, True

    monkeypatch.setattr(fix_tasks, "validate_group", fake_validate)
    async with TestClient(TestServer(_app(runner))) as client:
        validated = await client.post(
            "/rf/review-fix-http/actions",
            json=_action_body(
                "validate_group",
                revision=2,
                group_id="group-1",
                expected_group_revision=0,
                test_command=["python", "-m", "pytest"],
                build_command=["make", "build"],
            ),
        )
        assert validated.status == 200, await validated.text()
        assert (await validated.json())["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,from_state",
    [("resume", ReviewFixState.PAUSED), ("retry", ReviewFixState.BLOCKED_VALIDATION)],
)
async def test_resume_and_retry_emit_a_success_audit_event_on_the_202_path(
    tmp_path, monkeypatch, action, from_state
):
    """A 202 resume/retry launch is audited before the handler returns."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    run = runner.get_review_fix("review-fix-http")
    run.review_fix.state = from_state
    run.review_fix.revision = 0
    run.revision = 0

    async def fake_execute(_task_id, **_kwargs):
        return "execution-task-1"

    monkeypatch.setattr(runner, "execute_review_fix", fake_execute)

    audit_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        fix_tasks,
        "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kwargs: audit_calls.append(kwargs)),
    )

    async with TestClient(TestServer(_app(runner))) as client:
        response = await client.post(
            "/rf/review-fix-http/actions", json=_action_body(action, revision=0)
        )
        assert response.status == 202, await response.text()
        assert (await response.json())["execution_task_id"] == "execution-task-1"

    assert len(audit_calls) == 1
    assert audit_calls[0]["tool_name"] == action
    assert audit_calls[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_action_context_guards_and_unknown_tasks(tmp_path):
    runner = await _runner_for(tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION)
    async with TestClient(TestServer(_app(runner))) as client:
        unconfirmed = await client.post(
            "/rf/review-fix-http/actions",
            json={
                "action": "apply_group",
                "expected_revision": 0,
                "target_fingerprint": "target-fingerprint",
                "group_id": "group-1",
            },
        )
        assert unconfirmed.status == 409

        bool_revision = await client.post(
            "/rf/review-fix-http/actions",
            json={
                "action": "confirm_grouping",
                "expected_revision": True,
                "target_fingerprint": "target-fingerprint",
                "confirmation_id": "c",
            },
        )
        assert bool_revision.status == 409

        unknown = await client.post("/rf/nope/actions", json=_action_body("pause"))
        assert unknown.status == 404

        unsupported = await client.post(
            "/rf/review-fix-http/actions",
            json={
                "action": "detonate",
                "expected_revision": 0,
                "target_fingerprint": "target-fingerprint",
                "confirmation_id": "c",
            },
        )
        assert unsupported.status == 409
        assert (await unsupported.json())["code"] == "review_fix_action_rejected"


def _embedded_app(runner: TaskRunner | None) -> web.Application:
    """Routes mounted with an app token, as token auth would do."""

    @web.middleware
    async def mark_embedded(request: web.Request, handler):
        request["app"] = "sage-app"
        return await handler(request)

    app = web.Application(middlewares=[mark_embedded])
    app["state"] = SimpleNamespace(task_runner=runner, sessions=_Sessions())
    app.router.add_post("/api/taskrunner/{task_id}/review-fix/actions", fix_tasks.handle_fix_action)
    return app


@pytest.mark.asyncio
async def test_auto_approve_requires_dashboard_provenance(tmp_path, monkeypatch):
    """Only an owner dashboard request may mint per-run trust."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.BLOCKED_VALIDATION)
    run = runner.get_review_fix("review-fix-http")
    assert run.review_fix is not None
    granted: list[bool] = []

    async def fake_execute(_task_id, **kwargs):
        granted.append(kwargs["auto_approve"])
        return "execution-task-1"

    monkeypatch.setattr(runner, "execute_review_fix", fake_execute)
    audit = MagicMock()
    audit.log_tool_invocation.return_value = None
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.taskrunner._sel", lambda: audit, raising=False
    )

    async with TestClient(TestServer(_embedded_app(runner))) as client:
        denied = await client.post(
            "/api/taskrunner/review-fix-http/review-fix/actions",
            json=_action_body("retry", auto_approve=True),
        )
        assert denied.status == 403, await denied.text()
        assert (await denied.json())["code"] == "owner_only"
    assert granted == []

    # Same request from the dashboard context (no calling app) is honored.
    async with TestClient(TestServer(_app(runner))) as client:
        allowed = await client.post(
            "/rf/review-fix-http/actions", json=_action_body("retry", auto_approve=True)
        )
        assert allowed.status == 202, await allowed.text()
    assert granted == [True]


@pytest.mark.asyncio
async def test_review_again_handler_failure_restores_pushed(tmp_path):
    """A failed review-again launch must not strand REREVIEWING."""
    runner = await _runner_for(tmp_path, state=ReviewFixState.PUSHED, task_id="rf-rereview")

    async def exploding_handler(_request, _body):
        raise RuntimeError("sage review backend unavailable")

    app = with_owner_dashboard(web.Application())
    app["state"] = SimpleNamespace(task_runner=runner, sessions=_Sessions())
    app.router.add_post(
        "/rf/{task_id}/review-again",
        lambda request: fix_tasks.handle_review_again(request, exploding_handler),
    )

    async with TestClient(TestServer(app)) as client:
        failed = await client.post(
            "/rf/rf-rereview/review-again",
            json={"expected_revision": 0, "target_fingerprint": "target-fingerprint"},
        )
        assert failed.status == 500
        restored = runner.get_review_fix("rf-rereview").review_fix
        assert restored is not None
        assert restored.state is ReviewFixState.PUSHED
        assert restored.audit_log[-1].action == "review_again_rolled_back"


@pytest.mark.asyncio
async def test_review_again_guard_ladder(tmp_path):
    async with TestClient(TestServer(_app(None))) as client:
        unavailable = await client.post("/rf/t/review-again", json={})
        assert unavailable.status == 503

    awaiting = await _runner_for(
        tmp_path, state=ReviewFixState.AWAITING_GROUP_CONFIRMATION, task_id="rf-awaiting"
    )
    async with TestClient(TestServer(_app(awaiting))) as client:
        not_ready = await client.post(
            "/rf/rf-awaiting/review-again",
            json={"expected_revision": 0, "target_fingerprint": "target-fingerprint"},
        )
        assert not_ready.status == 409
        assert (await not_ready.json())["code"] == "review_again_not_ready"

        incomplete = await client.post("/rf/rf-awaiting/review-again", json={})
        assert incomplete.status == 400
        assert (await incomplete.json())["code"] == "review_again_context_required"

    pushed = await _runner_for(tmp_path, state=ReviewFixState.PUSHED, task_id="rf-pushed")
    async with TestClient(TestServer(_app(pushed))) as client:
        stale = await client.post(
            "/rf/rf-pushed/review-again",
            json={"expected_revision": 9, "target_fingerprint": "target-fingerprint"},
        )
        assert stale.status == 409
        assert (await stale.json())["code"] == "stale_task_state"

        restarted = await client.post(
            "/rf/rf-pushed/review-again",
            json={"expected_revision": 0, "target_fingerprint": "target-fingerprint"},
        )
        assert restarted.status == 202
        payload = await restarted.json()
        assert payload["state"] == ReviewFixState.REREVIEWING.value
        assert payload["review_run_id"] == ""
