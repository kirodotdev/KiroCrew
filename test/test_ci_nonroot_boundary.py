"""The measured non-root boundary and the complete Linux migration contract."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import _find_posix_test_shell

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ACTION_REF = "./.github/actions/run-as-runner"
_CI_SHELL = "/usr/local/bin/ci-shell {0}"
_LARGE = "${{ needs.changes.outputs.linux_runner_large || 'ubuntu-latest' }}"


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))[
        "jobs"
    ]


@pytest.fixture(scope="module")
def action():
    return yaml.safe_load(
        (_REPO_ROOT / ".github/actions/run-as-runner/action.yml").read_text(encoding="utf-8")
    )["runs"]["steps"]


@pytest.mark.parametrize("name", ["backend-test", "e2e-boot-matrix"])
def test_the_boundary_follows_setup_and_covers_every_test_and_coverage_step(jobs, name):
    steps = jobs[name]["steps"]
    provision = next(i for i, step in enumerate(steps) if step.get("uses") == _ACTION_REF)
    setups = [
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith(("actions/setup-", "astral-sh/setup-"))
    ]
    assert setups and provision > max(setups)
    consumers = [
        (i, step)
        for i, step in enumerate(steps)
        if any(token in step.get("run", "") for token in ("pytest ", "coverage combine"))
    ]
    assert consumers
    for index, step in consumers:
        assert provision < index
        if name == "backend-test":
            assert step["shell"] == _CI_SHELL
        else:
            from test_ci_additional_fleet_routes import _evaluate

            # Only job defaults permit expressions; step shells are literal.
            assert "shell" not in step
            for os_name in ("ubuntu-latest", "windows-latest", "macos-15"):
                assert _evaluate(
                    jobs[name]["defaults"]["run"]["shell"], {"matrix.os": os_name}
                ) == (_CI_SHELL if os_name == "ubuntu-latest" else "bash")
    if name == "e2e-boot-matrix":
        for step in steps[:provision]:
            if "run" in step:
                assert step["shell"] == "bash"
    for job in jobs.values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("shell", ""), "Step shells cannot use expressions"
    # The selector writes GITHUB_OUTPUT and must remain under the runner identity.
    for step in steps:
        if step.get("id") == "scope":
            assert step.get("shell", "bash") == "bash"


def test_all_linux_shards_and_memory_heavy_jobs_use_large(jobs):
    for name in ("backend-test", "backend-lint", "bundle-size"):
        assert jobs[name]["runs-on"] == _LARGE
    backend = jobs["backend-test"]
    assert backend["strategy"] == {
        "fail-fast": False,
        "matrix": {"python-version": ["3.12"], "group": list(range(1, 9))},
    }
    assert backend["env"]["SHARD_COUNT"] == 8
    assert backend["timeout-minutes"] == 60
    lint = next(
        s
        for s in jobs["backend-lint"]["steps"]
        if s.get("name") == "Check formatting (black, baselined)"
    )
    assert "BLACK_NUM_WORKERS" not in lint.get("env", {})
    assert lint["run"] == (
        "python3 scripts/ci_black_diagnostics.py scripts/check_black_formatting.py"
    )
    assert "if" not in lint
    assert "continue-on-error" not in lint
    for command in ("isort --check-only", "flake8 ", "mypy "):
        assert any(
            s.get("run", "").startswith(command) and not s.get("continue-on-error")
            for s in jobs["backend-lint"]["steps"]
        )
    assert any(
        "--max-old-space-size=6144" in s.get("run", "") for s in jobs["bundle-size"]["steps"]
    )


def test_privilege_transition_only_goes_down_and_temp_stays_root_owned(action):
    # Preserved from the source boundary tests: executable statements, not prose.
    code = "\n".join(
        line for line in action[0]["run"].splitlines() if not line.lstrip().startswith("#")
    )
    assert "/etc/sudoers" not in code and "NOPASSWD" not in code
    assert all(
        "777" not in line or line.strip() == 'chmod 1777 "$RUNNER_TEMP"'
        for line in code.splitlines()
        if "chmod " in line
    )
    assert [line.strip() for line in code.splitlines() if "chown " in line] == [
        'chown -R runner:runner "$GITHUB_WORKSPACE"'
    ]
    assert 'chmod 1777 "$RUNNER_TEMP"' in code
    assert 'if [ "$(id -u)" != "0" ]' in code
    assert 'exec bash --noprofile --norc -eo pipefail "$1"' in code
    assert "exec runuser -m -u runner -- env --default-signal=INT" in code
    assert "HOME=/home/runner USER=runner LOGNAME=runner KIROCREW_LOCK_TEST_ROOT=/dev/shm" in code
    assert 'test "$(stat -f -c %T /dev/shm)" = tmpfs' in code
    assert "ldconfig" in code and "--no-install-recommends lsof" in code
    assert code.count("sha256sum -c -") == code.count("curl ") == 1
    assert action[0]["env"]["JQ_VERSION"] == "1.7.1"
    assert (
        action[0]["env"]["JQ_SHA256"]
        == "5942c9b0934e510ee61eb3e30273f1b3fe2590df93933a93d7c58b81d19c8ff5"
    )
    boundary = action[1]
    assert boundary["shell"] == _CI_SHELL
    assert boundary["if"] == "runner.environment == 'self-hosted'"
    for expected in (
        'test "$(id -u)" -ne 0',
        'test "$(stat -c %U "$RUNNER_TEMP")" = root',
        'test -k "$RUNNER_TEMP"',
        'test ! -w "$(dirname "$GITHUB_ENV")"',
        'test ! -w "$file"',
        "env -u LD_LIBRARY_PATH python",
    ):
        assert expected in boundary["run"]
    for name in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY"):
        assert f'"${name}"' in boundary["run"]


def test_action_shell_parses_without_running_host_mutations(action, tmp_path):
    shell = _find_posix_test_shell()
    assert shell
    for step in action:
        result = subprocess.run(
            [shell, "-n"],
            input=step["run"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "workflow,name",
    [
        ("ci.yml", "backend-test-sandbox"),
        ("ci.yml", "e2e"),
        ("release.yml", "release-candidate-tests"),
        ("gui-user-test.yml", "gui-user-test"),
        ("ci.yml", "pod-boot-windows"),
    ],
)
def test_unproved_namespace_gui_and_task_scheduler_jobs_stay_hosted(workflow, name):
    job = yaml.safe_load((_REPO_ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8"))[
        "jobs"
    ][name]
    assert "codebuild" not in job["runs-on"]
    assert "linux_runner" not in job["runs-on"] and "windows_runner" not in job["runs-on"]


@pytest.mark.parametrize(
    "line",
    [
        'chmod 777 "$GITHUB_WORKSPACE"',
        'chmod -R 777 "$GITHUB_WORKSPACE"',
        'chmod 2777 "$GITHUB_WORKSPACE"',
        'chmod 1777 "$GITHUB_WORKSPACE"',
        'chmod 1777 "$RUNNER_TEMP" "$GITHUB_WORKSPACE"',
    ],
)
def test_boundary_ratchet_rejects_world_writable_chmod(action, line):
    changed = [dict(step) for step in action]
    changed[0]["run"] += "\n" + line
    with pytest.raises(AssertionError):
        test_privilege_transition_only_goes_down_and_temp_stays_root_owned(changed)
