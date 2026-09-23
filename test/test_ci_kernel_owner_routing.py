"""The hosted strict kernel-lock-owner lane: exact cases, fail-closed everywhere."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path

import pytest
import yaml
from coverage import CoverageData
from test_ci_ipv6_routing import _env, _run, _shell, _step
from test_gateway_lock_diagnosis import _STRICT_ENV

ROOT = Path(__file__).resolve().parents[1]
JOB = "backend-test-kernel-lock-owner"
ARTIFACT = "coverage-kernel-lock-owner"
DATA_FILE = ".coverage.kernel-lock-owner"
LIVE_HOLDER = "test/test_gateway_lock.py::TestLockHolder::test_live_holder_is_named_and_alive"
ORPHAN = "test/test_gateway_lock_diagnosis.py::test_flock_is_held_by_a_fork_orphan"
NODES = (LIVE_HOLDER, ORPHAN)
JUNIT_IDENTITIES = [
    "test.test_gateway_lock.TestLockHolder::test_live_holder_is_named_and_alive",
    "test.test_gateway_lock_diagnosis::test_flock_is_held_by_a_fork_orphan",
]


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]


@pytest.fixture(scope="module")
def run_body(jobs):
    return _step(jobs, JOB, "Run strict")["run"]


def _junit_check(run_body):
    return run_body.split("python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def test_the_lane_is_a_hosted_vm_running_exactly_the_two_strict_cases(jobs, run_body):
    job = jobs[JOB]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["needs"] == ["changes", "await-fast-gate"]
    for forbidden in ("if", "container", "continue-on-error", "strategy"):
        assert forbidden not in job
    assert job["defaults"]["run"]["shell"] == "bash"
    assert job["timeout-minutes"] <= 15
    for step in job["steps"]:
        assert not step.get("continue-on-error")
        assert "${{" not in step.get("shell", "")
        assert step.get("uses", "") != "./.github/actions/run-as-runner"
    run = _step(jobs, JOB, "Run strict")
    assert run["env"][_STRICT_ENV] == "1"
    assert 'test -z "${KIROCREW_LOCK_TEST_ROOT:-}"' in run_body
    assert 'test "$(stat -f -c %T /proc)" = proc' in run_body
    body = run_body.replace("\\\n", " ")
    commands = re.findall(r"^\s*python -m pytest .+$", body, re.MULTILINE)
    assert len(commands) == 1
    (command,) = commands
    assert " -n0 " in command and "--junitxml=" in command
    marker_free = command.split("pytest", 1)[1]
    for absent in (" -m ", "--file-shard", "-k ", "test/test_gateway_lock.py ", "test/ "):
        assert absent not in marker_free
    assert re.findall(r'"(test/\S+::\S+)"', command) == list(NODES)
    ordinary = _step(jobs, "backend-test", "Run tests")["run"]
    assert re.findall(r"--cov=\S+", run_body.replace(")", "")) == re.findall(r"--cov=\S+", ordinary)
    assert "COVERAGE=(--no-cov)" in run_body
    for flag in ("ONLY_FRONTEND", "LEAF_TESTS"):
        assert f'[ "${flag}" != "true" ]' in run_body
    assert 'if [ "$LEAF_TESTS" = "true" ]; then REPEAT=3; fi' in run_body
    check = _junit_check(run_body)
    for identity in JUNIT_IDENTITIES:
        assert f'"{identity}",' in check
    for outcome in ("skipped", "failure", "error"):
        assert f'"{outcome}"' in check


def test_the_ordinary_shards_still_run_both_cases_unfiltered(jobs):
    for name in ("backend-test", "backend-test-windows"):
        body = _step(jobs, name, "Run tests")["run"]
        assert "gateway_lock" not in body and _STRICT_ENV not in body
    assert "gateway_lock" not in yaml.safe_dump(jobs["backend-test"].get("env", {}))
    config = (ROOT / "setup.cfg").read_text(encoding="utf-8")
    assert "kernel_lock" not in config and _STRICT_ENV not in config


def test_the_coverage_artifact_is_required_by_combine_and_gate(jobs):
    upload = _step(jobs, JOB, "Upload kernel lock owner")
    assert upload["with"] == {
        "name": ARTIFACT,
        "path": DATA_FILE,
        "include-hidden-files": True,
        "if-no-files-found": "error",
    }
    stage = _step(jobs, JOB, "Stage kernel lock owner")
    for step in (upload, stage):
        assert "only_frontend != 'true'" in step["if"]
        assert "leaf_tests != 'true'" in step["if"]
        assert "runner.os" not in step["if"], "the lane has one OS; no per-OS branch to hide in"
    assert f"mv .coverage {DATA_FILE}" in stage["run"]
    combine = jobs["coverage-combine"]
    assert JOB in combine["needs"]
    download = _step(jobs, "coverage-combine", "Download required kernel lock owner")
    assert download["with"] == {"name": ARTIFACT}
    assert f"test -f {DATA_FILE} ||" in _step(jobs, "coverage-combine", "Combine into")["run"]
    gate = jobs["coverage-gate"]
    assert JOB in gate["needs"] and gate["if"] == "${{ always() }}"
    guard = _step(jobs, "coverage-gate", "Require upstream")
    assert guard["env"]["KERNEL_LOCK_OWNER"] == f"${{{{ needs.{JOB}.result }}}}"


@pytest.mark.parametrize("mode", ["full", "frontend", "leaf"])
@pytest.mark.parametrize("status", ["success", "failure", "cancelled", "skipped", ""])
def test_gate_fails_closed_for_the_strict_lane_in_every_scope(jobs, tmp_path, mode, status):
    env = dict(
        _env(tmp_path),
        KERNEL_LOCK_OWNER=status,
        IPV6="success",
        BE="success",
        FE="success",
        FCM="success",
        CC="success" if mode == "full" else "skipped",
        ONLY_BACKEND="false",
        ONLY_FRONTEND=str(mode == "frontend").lower(),
        LEAF_TESTS=str(mode == "leaf").lower(),
    )
    result = _shell(_step(jobs, "coverage-gate", "Require upstream")["run"], tmp_path, env)
    assert (result.returncode == 0) == (status == "success"), result.stdout + result.stderr
    if status != "success":
        assert f"::error::{JOB}={status} -- failing closed." in result.stdout


def _report(cases):
    return "<testsuite>" + "".join(cases) + "</testsuite>"


def _case(identity, status=""):
    classname, name = identity.split("::")
    attrs = f'classname="{escape(classname, quote=True)}" name="{escape(name, quote=True)}"'
    return f"<testcase {attrs}>{status}</testcase>"


@pytest.mark.parametrize(
    "fault",
    [None, "empty", "missing", "skip", "failure", "error", "extra", "renamed", "other-class"],
)
def test_the_junit_check_requires_exactly_both_cases_with_no_skip(run_body, tmp_path, fault):
    cases = [_case(identity) for identity in JUNIT_IDENTITIES]
    if fault == "empty":
        cases = []
    elif fault == "missing":
        cases = cases[1:]
    elif fault in {"skip", "failure", "error"}:
        tag = {"skip": "skipped", "failure": "failure", "error": "error"}[fault]
        cases[1] = _case(JUNIT_IDENTITIES[1], f"<{tag}/>")
    elif fault == "extra":
        cases.append(_case("test.test_gateway_lock::test_release_is_idempotent"))
    elif fault == "renamed":
        cases[1] = _case(JUNIT_IDENTITIES[1] + "_variant")
    elif fault == "other-class":
        cases[0] = _case("test.test_gateway_lock::test_live_holder_is_named_and_alive")
    (tmp_path / "kernel-lock-owner.xml").write_text(_report(cases), encoding="utf-8")
    result = _run(
        [sys.executable, "-"],
        tmp_path,
        dict(_env(tmp_path), RUNNER_TEMP=str(tmp_path)),
        input=_junit_check(run_body),
    )
    assert (result.returncode == 0) == (fault is None), result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="the lane's cases need Linux /proc")
def test_a_real_report_of_the_lane_command_satisfies_the_junit_check(run_body, tmp_path):
    """The pinned classnames are pytest's own, for these node ids, from the repo root.

    Runs the lane's two cases without strict mode: the identities it pins do not
    depend on which kernel branch the orphan case observes, and this host's
    kernel is not the lane's.
    """
    env = _env(tmp_path)
    env.pop(_STRICT_ENV, None)
    env["RUNNER_TEMP"] = str(tmp_path)
    report = tmp_path / "kernel-lock-owner.xml"
    result = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n0",
            "--no-cov",
            "--timeout=120",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            *NODES,
        ],
        ROOT,
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    check = _run([sys.executable, "-"], tmp_path, env, input=_junit_check(run_body))
    assert check.returncode == 0, check.stdout + check.stderr


@pytest.fixture
def coverage_root():
    # The real omit policy excludes pytest-of-* trees, not source checkouts.
    with tempfile.TemporaryDirectory(prefix="kernel-lock-owner-coverage-") as directory:
        yield Path(directory)


def test_combine_requires_the_strict_lane_data_before_producing_a_report(jobs, coverage_root):
    source = coverage_root / "src" / "kiro_crew" / "probe.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\nOTHER = 2\n", encoding="utf-8")
    (coverage_root / "setup.cfg").write_text(
        (ROOT / "setup.cfg").read_text(encoding="utf-8"), encoding="utf-8"
    )
    staged = coverage_root / "staged"
    staged.mkdir()
    for name, lines in ((".coverage.1", {1}), (".coverage.ipv6", {2}), (DATA_FILE, {1, 2})):
        data = CoverageData(basename=str(staged / name))
        data.add_lines({"src/kiro_crew/probe.py": lines})
        data.write()
    env = _env(coverage_root)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    body = _step(jobs, "coverage-combine", "Combine into")["run"]
    for name in (".coverage.1", ".coverage.ipv6"):
        shutil.copyfile(staged / name, coverage_root / name)
    result = _shell(body, coverage_root, env)
    assert result.returncode != 0
    assert "required kernel lock owner coverage data missing" in result.stdout
    assert not (coverage_root / "coverage.xml").exists()
    shutil.copyfile(staged / DATA_FILE, coverage_root / DATA_FILE)
    result = _shell(body, coverage_root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (coverage_root / "coverage.xml").is_file()
    combined = CoverageData(basename=str(coverage_root / ".coverage"))
    combined.read()
    (measured,) = combined.measured_files()
    assert sorted(combined.lines(measured)) == [1, 2]


def test_the_lane_job_parses_as_bash():
    from conftest import _find_posix_test_shell

    shell = _find_posix_test_shell() if os.name == "nt" else shutil.which("bash")
    assert shell, "Native Git Bash on Windows or Bash on POSIX is required"
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    for step in jobs[JOB]["steps"]:
        if "run" not in step:
            continue
        result = subprocess.run(
            [shell, "-n"],
            input=step["run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert result.returncode == 0, result.stderr


def test_lock_module_collects_without_a_posix_only_import():
    """Windows collects this module before applying its POSIX-only skip mark."""
    import ast

    tree = ast.parse((ROOT / "test/test_gateway_lock_diagnosis.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name != "fcntl" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "fcntl"
