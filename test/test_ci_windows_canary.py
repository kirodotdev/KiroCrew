"""Full Windows fleet migration: routing, unchanged tests and hard parity probes."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from test_ci_additional_fleet_routes import _evaluate as _expression

from conftest import _find_posix_test_shell

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO = "kirodotdev/KiroCrew"
_ACTION = _REPO_ROOT / ".github/actions/setup-windows-tests"


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))[
        "jobs"
    ]


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("ci_windows_probe", _ACTION / "probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "repository,head",
    [(_REPO, _REPO), (_REPO, "fork/repo"), ("fork/repo", "fork/repo"), (_REPO, "")],
)
@pytest.mark.parametrize(
    "event", ["push", "pull_request", "schedule", "issue_comment", "workflow_dispatch"]
)
@pytest.mark.parametrize(
    "actors,actor", [("", "123"), ('["123"]', "123"), ('["123"]', "999"), ("[]", "123")]
)
@pytest.mark.parametrize(
    "action",
    ["opened", "synchronize", "edited", "reopened", "labeled", "unlabeled", "ready_for_review", ""],
)
def test_all_actual_routes_require_repository_event_and_admitted_actor(
    jobs, repository, head, event, actors, actor, action
):
    context = {
        "github.repository": repository,
        "github.event_name": event,
        "github.event.action": action,
        "github.event.pull_request.head.repo.full_name": head,
        "vars.CODEBUILD_ACTOR_IDS": actors,
        "github.actor_id": actor,
        "github.run_id": "100",
        "github.run_attempt": "2",
    }
    expected = (
        repository == _REPO
        and actor in json.loads(actors or "[]")
        and (
            event == "push"
            or (event == "pull_request" and head == _REPO and action in {"opened", "synchronize"})
        )
    )
    for step_id in ("runner", "windows-runner"):
        step = next(s for s in jobs["changes"]["steps"] if s.get("id") == step_id)
        assert _expression(step["env"]["ELIGIBLE"], context) == expected
    for name in ("changes", "await-fast-gate"):
        assert _expression(jobs[name]["runs-on"], context) == (
            "codebuild-kirocrew-gha-linux-100-2" if expected else "ubuntu-latest"
        )


@pytest.mark.parametrize("eligible", [False, True])
@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("step_id,platform", [("runner", "linux"), ("windows-runner", "windows")])
def test_real_resolver_shell_emits_large_and_hosted_fallback(
    jobs, tmp_path, eligible, attempt, step_id, platform
):
    step = next(s for s in jobs["changes"]["steps"] if s.get("id") == step_id)
    # The helper rejects Windows' WSL launcher. Assert rather than skip: routing
    # must be checked even when a developer's native shell installation is broken.
    shell = _find_posix_test_shell()
    assert shell, "Routing contract requires a native POSIX test shell"
    label = (
        step["env"]["LABEL"]
        .replace("${{ github.run_id }}", "100")
        .replace("${{ github.run_attempt }}", str(attempt))
    )
    assert label == f"codebuild-kirocrew-gha-{platform}-100-{attempt}"
    output = tmp_path / "output"
    result = subprocess.run(
        [shell, "-e", "-c", step["run"]],
        cwd=tmp_path,
        env=dict(
            os.environ, ELIGIBLE=str(eligible).lower(), LABEL=label, GITHUB_OUTPUT=output.as_posix()
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    actual = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    fallback = "ubuntu-latest" if platform == "linux" else "windows-latest"
    expected = {
        f"{platform}_runner": (
            (label + (" instance-size:large" if platform == "windows" else ""))
            if eligible
            else fallback
        )
    }
    if platform == "linux":
        expected["linux_runner_large"] = label + " instance-size:large" if eligible else fallback
    assert actual == expected
    for key in expected:
        assert jobs["changes"]["outputs"][key] == "${{ steps." + step_id + ".outputs." + key + " }}"


@pytest.mark.parametrize(
    "resolved", ["", "windows-latest", "codebuild-windows instance-size:large"]
)
def test_all_windows_consumers_keep_output_fallback_and_matrix_names(jobs, resolved):
    consumers = {
        name
        for name, job in jobs.items()
        if "outputs.windows_runner" in str(job.get("runs-on", ""))
    }
    assert consumers == {
        "backend-test-windows",
        "backend-test-windows-fail-closed",
        "e2e-boot-matrix",
    }
    for name in consumers:
        assert "changes" in jobs[name]["needs"]
        for group in range(1, 9):
            context = {
                "matrix.group": group,
                "matrix.os": "windows-latest",
                "needs.changes.outputs.windows_runner": resolved,
                "needs.changes.outputs.linux_runner_large": "linux-large",
            }
            assert _expression(jobs[name]["runs-on"], context) == (resolved or "windows-latest")


def test_shard_selection_and_checks_stay_intact(jobs):
    job = jobs["backend-test-windows"]
    assert job["strategy"] == {"fail-fast": False, "matrix": {"group": list(range(1, 9))}}
    assert job["env"]["SHARD_COUNT"] == 8
    assert job["env"]["MDNB_GIT_TIMEOUT_SEC"] == 120
    assert job["timeout-minutes"] == 60
    assert "if" not in job and "continue-on-error" not in job
    step = next(s for s in job["steps"] if s.get("name", "").startswith("Run tests (Windows shard"))
    command = re.sub(
        r"\s+", " ", step["run"].split("python -m pytest", 1)[1].replace("\\\n", " ")
    ).strip()
    assert command == (
        "-p scripts.ci_pytest_progress -p scripts.ci_file_shards "
        '--ci-progress-dir "$RUNNER_TEMP/pytest-progress" '
        '-q -n auto --timeout=180 --no-cov --max-worker-restart=0 -m "not ipv6_required" '
        '--file-shards "$SHARD_COUNT" --file-shard ${{ matrix.group }}'
    )
    assert step["shell"] == "bash"
    upload = next(s for s in job["steps"] if s.get("name") == "Upload Windows pytest progress")
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["name"] == "windows-pytest-progress-${{ matrix.group }}"
    assert upload["with"]["path"] == "${{ runner.temp }}/pytest-progress/*.jsonl"
    assert jobs["pod-boot-windows"]["runs-on"] == "windows-latest"


@pytest.mark.parametrize(
    "name", ["backend-test-windows", "backend-test-windows-fail-closed", "e2e-boot-matrix"]
)
def test_inventory_precedes_setup_and_hard_probe_precedes_tests(jobs, name):
    steps = jobs[name]["steps"]
    inventory = next(
        i for i, s in enumerate(steps) if s.get("uses") == "./.github/actions/setup-windows-tests"
    )
    setup = next(
        i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/setup-python@")
    )
    probe = next(
        i for i, s in enumerate(steps) if s.get("name") == "Verify Windows test prerequisites"
    )
    tests = next(i for i, s in enumerate(steps) if "pytest " in s.get("run", ""))
    assert inventory < setup < probe < tests
    assert "runner.environment == 'self-hosted'" in steps[probe]["if"]
    assert "matrix.group" not in steps[probe]["if"]
    assert "continue-on-error" not in steps[probe]
    assert steps[probe]["run"] == "python .github/actions/setup-windows-tests/probe.py"
    body = yaml.safe_load((_ACTION / "action.yml").read_text(encoding="utf-8"))["runs"]["steps"][0]
    assert body["shell"] == "powershell"
    assert body["if"] == "runner.os == 'Windows' && runner.environment == 'self-hosted'"
    for required in (
        "Get-CimInstance",
        "Get-Command",
        "GITHUB_PATH",
        "bin\\bash.exe",
        "-cnotmatch '^(MINGW|MSYS)'",
        "$LASTEXITCODE",
    ):
        assert required in body["run"]
    for install in ("choco install", "winget install", "Invoke-WebRequest"):
        assert install not in body["run"]


@pytest.mark.parametrize("fault", [None, "sid", "owner", "acl", "symlink"])
def test_probe_executes_owned_file_operations_and_cleans_on_failure(
    probe, tmp_path, monkeypatch, fault
):
    pc = SimpleNamespace(
        current_user_sid=lambda: None if fault == "sid" else "test-user", restrict_to_owner=Mock()
    )
    acl = SimpleNamespace(
        describe=Mock(
            return_value=SimpleNamespace(owner_sid="other" if fault == "owner" else "test-user")
        )
    )
    if fault == "acl":
        pc.restrict_to_owner.side_effect = OSError("ACL unavailable")
    symlink = Mock(side_effect=lambda link, target: shutil.copyfile(target, link))
    if fault == "symlink":
        symlink.side_effect = OSError("symlink unavailable")
    monkeypatch.setattr(Path, "symlink_to", lambda link, target: symlink(link, target))
    for key in ("GITHUB_WORKSPACE", "RUNNER_TEMP"):
        root = tmp_path / key
        root.mkdir()
        monkeypatch.setenv(key, str(root))
    monkeypatch.setattr(probe, "platform_compat", pc)
    monkeypatch.setattr(probe, "windows_acl", acl)
    monkeypatch.setattr(
        probe,
        "ctypes",
        SimpleNamespace(windll=SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: 1))),
    )
    if fault:
        with pytest.raises((AssertionError, OSError)) as error:
            probe.check_paths()
        if fault == "owner":
            assert "owner_sid='other', token_sid='test-user'" in str(error.value)
    else:
        probe.check_paths()
        assert pc.restrict_to_owner.call_count == acl.describe.call_count == symlink.call_count == 2
    assert all(not list(root.iterdir()) for root in tmp_path.iterdir())


@pytest.mark.parametrize("os_name", ["ubuntu-latest", "windows-latest", "macos-15"])
@pytest.mark.parametrize("available", [False, True])
def test_boot_matrix_maps_only_runner_and_retains_os_fallback(jobs, os_name, available):
    context = {
        "matrix.os": os_name,
        "needs.changes.outputs.linux_runner_large": "linux-large" if available else "",
        "needs.changes.outputs.windows_runner": "windows-large" if available else "",
    }
    expected = (
        {
            "ubuntu-latest": "linux-large",
            "windows-latest": "windows-large",
            "macos-15": "macos-15",
        }[os_name]
        if available
        else os_name
    )
    assert _expression(jobs["e2e-boot-matrix"]["runs-on"], context) == expected


@pytest.mark.parametrize("fault", [None, "platform", "python", "tool", "node-floor"])
def test_probe_main_fails_on_missing_native_runtime_or_tools(probe, monkeypatch, fault):
    run = Mock()
    if fault == "tool":
        run.side_effect = FileNotFoundError("tool missing")
    elif fault == "node-floor":
        run.side_effect = [None] * 4 + [subprocess.CalledProcessError(1, ["node"])]
    check_paths = Mock()
    monkeypatch.setattr(
        probe,
        "sys",
        SimpleNamespace(
            platform="linux" if fault == "platform" else "win32",
            version_info=(3, 11) if fault == "python" else (3, 12),
            version="3.12.0",
            getwindowsversion=lambda: SimpleNamespace(build=20348),
        ),
    )
    monkeypatch.setattr(probe, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(probe, "check_paths", check_paths)
    if fault:
        with pytest.raises((AssertionError, FileNotFoundError, subprocess.CalledProcessError)):
            probe.main()
        check_paths.assert_not_called()
    else:
        probe.main()
        assert [call.args[0][0] for call in run.call_args_list] == [
            "git",
            "uv",
            "jq",
            "node",
            "node",
        ]
        assert all(call.kwargs == {"check": True, "timeout": 30} for call in run.call_args_list)
        check_paths.assert_called_once_with()
