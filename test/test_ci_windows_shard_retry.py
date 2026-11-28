"""The shard retry is loaded by the Windows backend shards, and by no other job.

``scripts/ci_shard_retry.py`` retries a shard's few failed tests once. These tests
read ``.github/workflows/ci.yml`` and pin how the ``backend-test-windows`` job
turns it on: the step's variables, the plugin on every pytest invocation, the
summary step and the record's upload. The plugin's own rules are pinned by
``test/test_ci_shard_retry.py``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github/workflows/ci.yml"
_PLUGIN = "-p scripts.ci_shard_retry"
_RECORD = "${{ runner.temp }}/ci-shard-retry.jsonl"


def _retry_module():
    spec = importlib.util.spec_from_file_location(
        "ci_shard_retry_for_wiring", _REPO_ROOT / "scripts/ci_shard_retry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


@pytest.fixture(scope="module")
def steps(jobs) -> list[dict]:
    return jobs["backend-test-windows"]["steps"]


@pytest.fixture(scope="module")
def run_tests(steps) -> dict:
    return next(s for s in steps if s.get("name", "").startswith("Run tests (Windows shard"))


def _step(steps: list[dict], name: str) -> dict:
    return next(s for s in steps if s.get("name") == name)


def test_the_step_sets_the_variables_the_plugin_reads(run_tests) -> None:
    retry = _retry_module()
    env = run_tests["env"]
    assert env[retry.ENV_ON] == "1"
    # The plugin retries on `pull_request` alone, so the event name is what
    # keeps push and merge-queue runs showing every failure.
    assert env[retry.ENV_EVENT] == "${{ github.event_name }}"
    assert env[retry.ENV_RECORD] == _RECORD
    assert env[retry.ENV_SHARD] == "windows-${{ matrix.group }}"
    assert env[retry.ENV_HEAD_SHA] == "${{ github.event.pull_request.head.sha }}"
    assert retry.enabled({retry.ENV_ON: env[retry.ENV_ON], retry.ENV_EVENT: "pull_request"})
    for event in ("push", "merge_group", "schedule", "workflow_dispatch"):
        assert not retry.enabled({retry.ENV_ON: env[retry.ENV_ON], retry.ENV_EVENT: event})


def test_every_pytest_invocation_of_the_step_loads_the_plugin(run_tests) -> None:
    script = run_tests["run"].replace("\\\n", " ")
    invocations = [line for line in script.splitlines() if "python -m pytest" in line]
    assert len(invocations) >= 4
    # The leaf and reduced arms take their plugins from the PROGRESS array.
    assert f"PROGRESS+=({_PLUGIN})" in script
    for line in invocations:
        assert _PLUGIN in line or '"${PROGRESS[@]}"' in line, line


def test_the_summary_runs_on_a_red_shard_too(steps) -> None:
    step = _step(steps, "Summarize the shard retry")
    assert step["if"] == "${{ always() }}"
    assert step["shell"] == "bash"
    assert step["run"] == "python scripts/ci_shard_retry.py summary"
    assert step["env"] == {_retry_module().ENV_RECORD: _RECORD}


def test_the_record_is_uploaded_per_shard_from_a_red_shard_too(steps) -> None:
    step = _step(steps, "Upload the shard retry record")
    assert step["if"] == "${{ always() }}"
    assert step["uses"].startswith("actions/upload-artifact@")
    assert step["with"]["name"] == "windows-shard-retry-${{ matrix.group }}"
    assert step["with"]["path"] == _RECORD
    # A shard that retried nothing wrote no record; that is not a failure.
    assert step["with"]["if-no-files-found"] == "ignore"


def test_the_summary_and_the_upload_come_after_the_tests(steps) -> None:
    names = [s.get("name", "") for s in steps]
    ran = next(i for i, name in enumerate(names) if name.startswith("Run tests (Windows shard"))
    assert (
        ran
        < names.index("Summarize the shard retry")
        < names.index("Upload the shard retry record")
    )


def test_no_other_job_turns_the_retry_on(jobs) -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert len(re.findall(r"^\s+CI_SHARD_RETRY:", text, flags=re.M)) == 1
    loaders = [
        name
        for name, job in jobs.items()
        if any("scripts.ci_shard_retry" in str(step.get("run", "")) for step in job["steps"])
    ]
    assert loaders == ["backend-test-windows"]
