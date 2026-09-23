"""Native IPv6 routing preserves nodes, coverage and fail-closed verdicts."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from coverage import CoverageData

from conftest import _find_posix_test_shell
from scripts.ci_file_shards import file_shard

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("test/test_meetings_providers.py", "test/test_platform_compat.py")
DNS = TARGETS[0] + "::TestDnsRebindingIsRefused::"
PEER = TARGETS[1] + "::test_native_tcp_peer_identifies_client_process_not_server"
WINDOWS_NAME = "test_windows_finds_real_ipv6_loopback_listener"
EXPECTED = {
    DNS + "test_the_fetch_lands_on_the_vetted_address_not_the_rebound_one",
    DNS + "test_an_unpinned_connector_would_have_been_rebound",
    DNS + "test_a_redirect_hop_is_pinned_to_its_own_vetted_address",
    PEER + f"[{int(socket.AF_INET6)}-::1]",
    TARGETS[1] + "::TestFindListeningPidsErrors::" + WINDOWS_NAME,
}


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]


def _step(jobs, job, prefix):
    return next(s for s in jobs[job]["steps"] if s.get("name", "").startswith(prefix))


def _run(argv, cwd, env=None, **kwargs):
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        **kwargs,
    )


def _env(root):
    env = dict(os.environ)
    for key in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "COV_CORE_SOURCE",
        "COV_CORE_DATAFILE",
        "COV_CORE_CONFIG",
        "COVERAGE_PROCESS_START",
        "KIROCREW_HOME",
    ):
        env.pop(key, None)
    env.update(
        PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT))),
        PYTHONDONTWRITEBYTECODE="1",
        COVERAGE_FILE=str(root / ".coverage"),
        TMPDIR=str(root),
        TMP=str(root),
        TEMP=str(root),
    )
    return env


def _shell(body, cwd, env):
    shell = _find_posix_test_shell() if os.name == "nt" else shutil.which("bash")
    assert shell, "CI routing tests require native Git Bash on Windows or Bash on POSIX"
    return _run([shell, "-eo", "pipefail", "-c", body], cwd, env)


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("available", [True, False])
def test_shell_requires_bash_with_native_windows_resolver(monkeypatch, platform, available):
    module = sys.modules[__name__]
    bash = "/usr/bin/bash" if platform == "posix" else "C:/Program Files/Git/bin/bash.exe"
    expected = bash if available else None
    calls = []

    def which(name):
        assert platform == "posix", "Windows must not select a PATH/WSL Bash"
        assert name == "bash", "sh may be dash and reject pipefail"
        return expected

    def native_shell():
        # The shared POSIX resolver legitimately returns sh, not necessarily Bash.
        return "/usr/bin/sh" if platform == "posix" else expected

    monkeypatch.setattr(module, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(module, "shutil", SimpleNamespace(which=which))
    monkeypatch.setattr(module, "_find_posix_test_shell", native_shell)
    monkeypatch.setattr(module, "_run", lambda *args: calls.append(args))
    if available:
        _shell("false | true", ROOT, {})
        assert calls == [([bash, "-eo", "pipefail", "-c", "false | true"], ROOT, {})]
    else:
        with pytest.raises(AssertionError, match="Bash"):
            _shell("false | true", ROOT, {})
        assert not calls


def test_real_collected_nodes_are_the_disjoint_fleet_and_hosted_union(tmp_path):
    def collect(*args):
        result = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-n0",
                "--no-cov",
                "--collect-only",
                "-qq",
                "--color=no",
                "-p",
                "no:cacheprovider",
                *args,
                *TARGETS,
            ],
            ROOT,
            _env(tmp_path),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        nodes = [line for line in result.stdout.splitlines() if line.startswith("test/")]
        assert nodes and len(nodes) == len(set(nodes))
        return set(nodes)

    original = collect()
    fleet = collect("-m", "not ipv6_required")
    hosted = collect("-m", "ipv6_required")
    assert hosted == EXPECTED
    assert not fleet & hosted
    assert original == fleet | hosted
    assert PEER + f"[{int(socket.AF_INET)}-127.0.0.1]" in fleet
    assert DNS + "test_a_normal_public_host_still_fetches" in fleet
    assert DNS + "test_resolution_stays_off_the_event_loop" in fleet
    # Only the owners of these two files can collect their ordinary items.
    owners = {file_shard(ROOT / target, ROOT, 8) for target in TARGETS}
    shards = [
        collect(
            "-p",
            "scripts.ci_file_shards",
            "--file-shards=8",
            f"--file-shard={owner}",
            "-m",
            "not ipv6_required",
        )
        for owner in owners
    ]
    assert sum(map(len, shards)) == len(fleet)
    assert set().union(*shards) == fleet


def test_workflow_routes_every_scope_without_changing_local_defaults(jobs):
    config = configparser.ConfigParser()
    config.read(ROOT / "setup.cfg", encoding="utf-8")
    assert "ipv6_required:" in config["tool:pytest"]["markers"]
    assert "ipv6_required" not in config["tool:pytest"]["addopts"]
    assert config["tool:pytest"]["testpaths"] == "test src/kiro_crew/apps/builtins"
    for name, expected_count in (("backend-test", 4), ("backend-test-windows", 1)):
        body = _step(jobs, name, "Run tests")["run"].replace("\\\n", " ")
        commands = re.findall(r"^\s*(?:python -m )?pytest .+$", body, re.MULTILINE)
        assert len(commands) == expected_count
        assert all('-m "not ipv6_required"' in command for command in commands)
        assert jobs[name]["strategy"]["matrix"]["group"] == list(range(1, 9))
    hosted = jobs["backend-test-ipv6"]
    assert hosted["needs"] == ["changes", "await-fast-gate"]
    assert hosted["runs-on"] == "${{ matrix.os }}"
    assert hosted["strategy"] == {
        "fail-fast": False,
        "matrix": {"os": ["ubuntu-latest", "windows-latest"]},
    }
    assert "if" not in hosted and "continue-on-error" not in hosted
    assert hosted["defaults"]["run"]["shell"] == "bash"
    run = _step(jobs, "backend-test-ipv6", "Run required")["run"]
    assert "-m ipv6_required" in run and "--file-shard" not in run
    assert all(target in run for target in TARGETS)
    assert 'if [ "$LEAF_TESTS" = "true" ]; then REPEAT=3; fi' in run
    assert "COVERAGE=(--no-cov)" in run
    assert '[ "$RUNNER_OS" = "Linux" ]' in run
    for flag in ("ONLY_FRONTEND", "LEAF_TESTS"):
        assert f'[ "${flag}" != "true" ]' in run
    ordinary = _step(jobs, "backend-test", "Run tests")["run"]
    assert re.findall(r"--cov=\S+", run.replace(")", "")) == re.findall(r"--cov=\S+", ordinary)
    for step in hosted["steps"]:
        assert not step.get("continue-on-error")
    upload = _step(jobs, "backend-test-ipv6", "Upload IPv6")
    assert upload["with"] == {
        "name": "coverage-ipv6",
        "path": ".coverage.ipv6",
        "include-hidden-files": True,
        "if-no-files-found": "error",
    }
    assert "runner.os == 'Linux'" in upload["if"]
    assert "only_frontend != 'true'" in upload["if"]
    assert "leaf_tests != 'true'" in upload["if"]
    combine = jobs["coverage-combine"]
    assert "backend-test-ipv6" in combine["needs"]
    download = _step(jobs, "coverage-combine", "Download required IPv6")
    assert download["with"] == {"name": "coverage-ipv6"}
    gate = jobs["coverage-gate"]
    assert "backend-test-ipv6" in gate["needs"] and gate["if"] == "${{ always() }}"
    guard = _step(jobs, "coverage-gate", "Require upstream")
    assert guard["env"]["IPV6"] == "${{ needs.backend-test-ipv6.result }}"


@pytest.mark.parametrize("mode", ["full", "frontend", "leaf"])
@pytest.mark.parametrize("status", ["success", "failure", "cancelled", "skipped", ""])
def test_gate_executes_fail_closed_for_ipv6_in_every_scope(jobs, tmp_path, mode, status):
    env = dict(
        _env(tmp_path),
        IPV6=status,
        KERNEL_LOCK_OWNER="success",
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
        assert "backend-test-ipv6=" in result.stdout


@pytest.mark.parametrize("platform", ["Linux", "Windows"])
@pytest.mark.parametrize("fault", [None, "empty", "missing", "skip", "failure"])
def test_hosted_report_requires_nonempty_complete_native_execution(jobs, tmp_path, platform, fault):
    names = [node.split("::")[-1] for node in sorted(EXPECTED)]
    if fault == "empty":
        names = []
    elif fault == "missing":
        names = names[1:]
    cases = []
    for index, name in enumerate(names):
        status = "<skipped/>" if platform == "Linux" and name == WINDOWS_NAME else ""
        if index == 0 and fault in {"skip", "failure"}:
            status += "<skipped/>" if fault == "skip" else "<failure/>"
        cases.append(f'<testcase name="{escape(name, quote=True)}">{status}</testcase>')
    (tmp_path / "ipv6.xml").write_text(
        "<testsuite>" + "".join(cases) + "</testsuite>", encoding="utf-8"
    )
    body = _step(jobs, "backend-test-ipv6", "Run required")["run"]
    check = body.split("python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    result = _run(
        [sys.executable, "-"],
        tmp_path,
        dict(_env(tmp_path), RUNNER_OS=platform, RUNNER_TEMP=str(tmp_path)),
        input=check,
    )
    assert (result.returncode == 0) == (fault is None), result.stdout + result.stderr


@pytest.fixture
def coverage_root():
    # The real omit policy excludes pytest-of-* trees, not source checkouts.
    with tempfile.TemporaryDirectory(prefix="ipv6-coverage-") as directory:
        yield Path(directory)


def test_real_coverage_combine_preserves_both_routes_and_requires_ipv6(jobs, coverage_root):
    tmp_path = coverage_root

    def write(name, content):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    sources = [
        "src/kiro_crew",
        "src/kiro_crew/apps/builtins/code_review_sage/sage_lib",
        "src/kiro_crew/apps/builtins/aws_control/crew/packaging",
        "src/kiro_crew/apps/builtins/code_review_sage/tests",
    ]
    for source in sources:
        directory = Path(source)
        while directory != Path("src"):
            write(str(directory / "__init__.py"), "")
            directory = directory.parent
        write(
            source + "/probe.py",
            "def choose(value):\n    if value:\n        return 1\n    return 0\n",
        )
    write("setup.cfg", (ROOT / "setup.cfg").read_text(encoding="utf-8"))
    write("pytest.ini", "[pytest]\nmarkers =\n    ipv6_required: native IPv6\n")
    write(
        "test_probe.py",
        "import pytest\nfrom kiro_crew import probe as core\n"
        "from sage_lib import probe as sage\n"
        "from kiro_crew.apps.builtins.aws_control.crew.packaging import probe as packaging\n"
        "from kiro_crew.apps.builtins.code_review_sage.tests import probe as fixtures\n"
        "@pytest.mark.parametrize('value', [0, pytest.param(1, marks=pytest.mark.ipv6_required)])\n"
        "def test_branch(value):\n"
        "    for module in (core, sage, packaging, fixtures):\n"
        "        assert module.choose(value) == value\n",
    )
    env = _env(tmp_path)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        (str(tmp_path / "src"), str(tmp_path / sources[1]).rsplit(os.sep, 1)[0])
    )
    staged = tmp_path / "staged"
    staged.mkdir()
    for route, marker in (
        ("baseline", ""),
        ("fleet", "not ipv6_required"),
        ("ipv6", "ipv6_required"),
    ):
        body = _step(
            jobs,
            "backend-test-ipv6" if route == "ipv6" else "backend-test",
            "Run required" if route == "ipv6" else "Run tests",
        )["run"]
        selectors = re.findall(r"--cov=\S+", body.replace(")", ""))
        result = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "pytest_cov.plugin",
                "-q",
                "--cov-report=",
                *selectors,
                "-m",
                marker,
            ],
            tmp_path,
            env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        (tmp_path / ".coverage").rename(staged / route)
    baseline = CoverageData(basename=str(staged / "baseline"))
    baseline.read()
    shutil.copyfile(staged / "fleet", tmp_path / ".coverage.1")
    body = _step(jobs, "coverage-combine", "Combine into")["run"]
    result = _shell(body, tmp_path, env)
    assert result.returncode != 0 and "required IPv6 coverage data missing" in result.stdout
    assert not (tmp_path / "coverage.xml").exists()
    shutil.copyfile(staged / "ipv6", tmp_path / ".coverage.ipv6")
    # Supply the independent required lane without filling any IPv6-only arcs.
    shutil.copyfile(staged / "fleet", tmp_path / ".coverage.kernel-lock-owner")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    result = _shell(body, tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    combined = CoverageData(basename=str(tmp_path / ".coverage"))
    combined.read()
    assert combined.measured_files() == baseline.measured_files()
    for name in baseline.measured_files():
        assert sorted(combined.arcs(name)) == sorted(baseline.arcs(name)), name
    recorded = {}
    for key in combined.measured_files():
        normalized = key.replace("\\", "/")
        assert normalized not in recorded, "duplicate source identity"
        recorded[normalized] = key
    for source in sources:
        name = source + "/probe.py"
        assert name in recorded
        assert sorted(combined.lines(recorded[name])) == [1, 2, 3, 4]
    assert (tmp_path / "coverage.xml").is_file()
