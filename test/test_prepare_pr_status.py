"""Regression tests for the prepare-pr aggregate readiness policy."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "scripts" / "pr_status.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_pr_status", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pr_payload(checks: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "number": 42,
            "title": "fix: keep the change focused",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "REVIEW_REQUIRED",
            "url": "https://github.com/example/repo/pull/42",
            "headRefName": "fix/focused",
            "statusCheckRollup": checks,
        }
    )


def _install_fake_gh(module: ModuleType, payload: str) -> None:
    def fake_run(args: list[str]) -> tuple[int, str, str]:
        if args[:3] == ["gh", "auth", "status"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            return 0, payload, ""
        raise AssertionError("unexpected command: {}".format(args))

    module.run = fake_run
    module.unresolved_thread_count = lambda _number: 3


def test_passed_aggregate_overrides_old_failures_and_advisory_threads() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "old duplicate check", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


def test_passed_aggregate_overrides_an_old_pending_check() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "old duplicate check", "status": "IN_PROGRESS", "conclusion": ""},
            {"context": "PR Readiness", "state": "SUCCESS"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 0


def test_legacy_pull_request_without_aggregate_still_fails_closed() -> None:
    module = _load_script()
    payload = _pr_payload(
        [{"name": "Backend Tests", "status": "COMPLETED", "conclusion": "FAILURE"}]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20


def test_check_run_named_pr_readiness_cannot_mask_a_failure() -> None:
    module = _load_script()
    payload = _pr_payload(
        [
            {"name": "PR Readiness", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "Backend Tests", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
    )
    _install_fake_gh(module, payload)

    assert module.main(["pr_status.py", "42"]) == 20
