"""A shard retries its few failed tests once and records each flaky pass.

`scripts/ci_shard_retry.py` is a pytest plugin. The cases below run a real
inner pytest session on a scratch tree, so the exit code and the record file
they check are the ones a shard job would see. One module-scoped harness runs
every inner session in turn, on one xdist worker, so no two of them start at
the same time.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.platform_compat_kill import stdlib_run_killing_tree_on_timeout
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts import ci_shard_retry as RETRY

ROOT = Path(__file__).resolve().parents[1]

# Fails the first time it runs in a tree, passes after: a flaky test.
FLAKY = (
    "from pathlib import Path\n"
    "def test_flaky():\n"
    "    seen = Path(__file__).with_name('seen-' + Path(__file__).stem)\n"
    "    if not seen.exists():\n"
    "        seen.write_text('x')\n"
    "        raise AssertionError('first try only\\nsecond line')\n"
)


def grouped(group: str) -> str:
    return "import pytest\n" + FLAKY.replace(
        "def test_flaky", f"@pytest.mark.xdist_group({group!r})\ndef test_flaky"
    )


BROKEN = "def test_broken():\n    raise ValueError('always')\n"
PASSING = "def test_fine():\n    pass\n"
# Passes, and counts its runs in a file beside it.
COUNTED = (
    "from pathlib import Path\n"
    "def test_counted():\n"
    "    runs = Path(__file__).with_name('runs-' + Path(__file__).stem)\n"
    "    runs.write_text((runs.read_text() if runs.exists() else '') + 'x')\n"
)
SETUP_ONCE = (
    "import pytest\nfrom pathlib import Path\n"
    "@pytest.fixture\ndef once():\n"
    "    seen = Path(__file__).with_name('seen-fixture')\n"
    "    if not seen.exists():\n"
    "        seen.write_text('x')\n"
    "        raise RuntimeError('setup broke')\n"
    "def test_uses(once):\n    pass\n"
)
# Makes only the retry pass exit non-zero, after its tests passed.
RETRY_EXITS_3 = (
    "import os\n"
    "def pytest_sessionfinish(session):\n"
    "    if os.environ.get('CI_SHARD_RETRY_RESULTS'):\n"
    "        session.exitstatus = 3\n"
)
# Like the root conftest's residue guard: fails the first session once it is clean.
FIRST_SESSION_GUARD = (
    "import os\n"
    "def pytest_sessionfinish(session):\n"
    "    if not os.environ.get('CI_SHARD_RETRY_RESULTS') and session.exitstatus == 0:\n"
    "        session.exitstatus = 1\n"
)
# Fails the run beside the tests, the way pytest-cov's `--cov-fail-under` does.
FAIL_BESIDE_TESTS = (
    "import pytest\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtestloop(session):\n"
    "    yield\n"
    "    session.testsfailed += 1\n"
)
# Grouped parameters: `test_x[a]@` starts the failing `test_x[a]@b]@g`.
GROUPED_PARAMS = (
    "import pytest\n"
    "@pytest.mark.xdist_group('g')\n"
    "@pytest.mark.parametrize('p', ['a', 'a]@b'], ids=['a', 'a]@b'])\n"
    "def test_x(p):\n"
    "    assert p == 'a'\n"
)
# Fails in the first session and is not collected by the retry pass.
ONLY_FIRST_SESSION = (
    "import os\n"
    "if not os.environ.get('CI_SHARD_RETRY_RESULTS'):\n"
    "    def test_only_first():\n"
    "        raise ValueError('not collected again')\n"
)
# Ends its xdist worker: a crash, which no test phase reports.
CRASHES_WORKER = "import os\ndef test_crash():\n    os._exit(1)\n"
# Fails its call once, then passes it while its teardown fails on the retry pass.
TEARDOWN_FAILS_ON_RETRY = (
    "import os\nimport pytest\n"
    "@pytest.fixture\ndef late():\n"
    "    yield\n"
    "    if os.environ.get('CI_SHARD_RETRY_RESULTS'):\n"
    "        raise RuntimeError('teardown broke on the retry')\n"
) + FLAKY.replace("def test_flaky():", "def test_flaky(late):")
# Calls the repository's own root conftest guard on a session a retry turned green.
ROOT_GUARD = (
    "import importlib.util, sys\n"
    "from types import SimpleNamespace\n"
    "spec = importlib.util.spec_from_file_location('root_conftest', sys.argv[1])\n"
    "guard = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(guard)\n"
    "guard._ROOT_BASELINE = set()\n"
    "guard._root_entries = lambda: {'left-behind'}\n"
    "guard._runner_owned = lambda name: False\n"
    "guard._not_ignored = lambda names: sorted(names)\n"
    "manager = SimpleNamespace(get_plugin=lambda name: None)\n"
    "for status, argument in ((0, 1), (0, 0), (2, 2)):\n"
    "    session = SimpleNamespace(exitstatus=status, config=SimpleNamespace(pluginmanager=manager))\n"
    "    guard.pytest_sessionfinish(session, argument)\n"
    "    print(int(session.exitstatus))\n"
)
SHA = "a" * 40

# name -> (files, pytest arguments, extra environment, opted in)
SCENARIOS: dict[str, tuple[dict[str, str], list[str], dict[str, str], bool]] = {
    "broken": ({"test_a.py": BROKEN + PASSING}, [], {}, True),
    "flaky": (
        {"test_a.py": FLAKY + PASSING},
        [],
        {RETRY.ENV_SHARD: "3", RETRY.ENV_HEAD_SHA: SHA},
        True,
    ),
    "flaky_and_broken": ({"test_a.py": FLAKY + BROKEN}, [], {}, True),
    "over_cap": (
        {
            "test_a.py": FLAKY
            + "".join(f"def test_bad_{n}():\n    assert False\n" for n in range(6))
        },
        [],
        {},
        True,
    ),
    "off": ({"test_a.py": FLAKY}, [], {}, False),
    "event_push": ({"test_a.py": FLAKY}, [], {RETRY.ENV_EVENT: "push"}, True),
    "event_empty": ({"test_a.py": FLAKY}, [], {RETRY.ENV_EVENT: ""}, True),
    "xdist": (
        {"test_a.py": grouped("g") + PASSING},
        ["-p", "xdist", "-n", "2", "--dist", "loadgroup"],
        {},
        True,
    ),
    "xdist_odd_groups": (
        {
            "test_a.py": grouped("team A"),
            "test_b.py": grouped("grüppe ☃"),
            "test_c.py": grouped("g" * 120),
        },
        ["-p", "xdist", "-n", "2", "--dist", "loadgroup"],
        {},
        True,
    ),
    "xdist_prefix_ids": (
        {"test_a.py": GROUPED_PARAMS},
        ["-p", "xdist", "-n", "2", "--dist", "loadgroup"],
        {},
        True,
    ),
    "ini_positional": (
        {"pytest.ini": "[pytest]\naddopts = test_b.py\n", "test_a.py": FLAKY, "test_b.py": COUNTED},
        [],
        {},
        True,
    ),
    "env_positional": (
        {"test_a.py": FLAKY, "test_b.py": COUNTED},
        [],
        {"PYTEST_ADDOPTS": "test_b.py"},
        True,
    ),
    # Only $PYTEST_ADDOPTS loads the plugin: the command line does not name it.
    "env_loads_plugin": (
        {"test_a.py": FLAKY + PASSING},
        [],
        {"PYTEST_ADDOPTS": "-p " + RETRY.PLUGIN},
        True,
    ),
    # The file arguments follow a `--` terminator.
    "terminator": ({"test_a.py": FLAKY + PASSING}, ["--"], {}, True),
    "setup_error": ({"test_a.py": SETUP_ONCE}, [], {}, True),
    "not_rerun": ({"test_a.py": FLAKY + ONLY_FIRST_SESSION}, [], {}, True),
    "worker_crash": (
        {"test_a.py": FLAKY, "test_b.py": CRASHES_WORKER},
        ["-p", "xdist", "-n", "2"],
        {},
        True,
    ),
    "teardown_fails_on_retry": ({"test_a.py": TEARDOWN_FAILS_ON_RETRY}, [], {}, True),
    "fail_fast": ({"test_a.py": FLAKY + BROKEN}, ["-x"], {}, True),
    "retry_exits_nonzero": ({"test_a.py": FLAKY, "conftest.py": RETRY_EXITS_3}, [], {}, True),
    "guard_after_retry": ({"test_a.py": FLAKY, "conftest.py": FIRST_SESSION_GUARD}, [], {}, True),
    "fail_beside_tests": ({"test_a.py": FLAKY, "conftest.py": FAIL_BESIDE_TESTS}, [], {}, True),
    "collection_error": (
        {"test_a.py": FLAKY, "test_b.py": "raise RuntimeError('import broke')\n"},
        ["--continue-on-collection-errors"],
        {},
        True,
    ),
    "ini_report": (
        {"pytest.ini": "[pytest]\naddopts = --junitxml=report.xml\n", "test_a.py": FLAKY + PASSING},
        [],
        {},
        True,
    ),
    "env_report": (
        {"test_a.py": FLAKY + PASSING},
        [],
        {"PYTEST_ADDOPTS": "--junit-xml report.xml"},
        True,
    ),
}

# One inner session plus its retry pass measured 1.9 s here, the xdist one 3.5 s,
# well inside the shared test deadline each subprocess gets. The harness runs every
# scenario in the first test's setup, and each is killed at that deadline, so the
# module's ceiling is twice the worst case of every scenario hitting it: a hung
# scenario fails as its own timeout message, never as this one.
pytestmark = [
    pytest.mark.timeout(2 * len(SCENARIOS) * FALLBACK_TIMEOUT_SECS),
    pytest.mark.xdist_group("ci_shard_retry_inner_sessions"),
]


def write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_scenario(
    root: Path, args: list[str], env_extra: dict[str, str], opt_in: bool
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("CI_SHARD_RETRY", "PYTEST_", "COV_CORE_", "COVERAGE_")):
            env.pop(name)
    env["COVERAGE_FILE"] = str(root / ".coverage")
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(root)
    env[RETRY.ENV_RECORD] = str(root / "record.jsonl")
    if opt_in:
        env[RETRY.ENV_ON] = "1"
        env[RETRY.ENV_EVENT] = "pull_request"
    env.update(env_extra)
    # On the command line, unless the scenario's own $PYTEST_ADDOPTS loads it.
    in_env = RETRY.names_plugin(shlex.split(env_extra.get("PYTEST_ADDOPTS", "")))
    load = [] if in_env else ["-p", RETRY.PLUGIN]
    try:
        # A timeout kills the whole tree, the plugin's retry child included.
        return stdlib_run_killing_tree_on_timeout(
            [sys.executable, "-B", "-m", "pytest", *load, "-p", "no:cacheprovider"]
            + ["-q", *args, "."],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{root.name} still running after {exc.timeout}s: {exc.stdout!r}")


Runs = dict[str, tuple[subprocess.CompletedProcess[str], list[dict[str, Any]], Path]]


def outcomes(record: list[dict[str, Any]]) -> dict[str, str]:
    """``{node id: outcome}`` of a record: every retried test, and how its retry ended."""
    assert len({row["nodeid"] for row in record}) == len(record)
    return {row["nodeid"]: row["outcome"] for row in record}


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Runs:
    """Every scenario's inner session, record and tree, run one after another."""
    results: Runs = {}
    for name, (files, args, env_extra, opt_in) in SCENARIOS.items():
        root = tmp_path_factory.mktemp(name)
        write(root, "pytest.ini", "[pytest]\n")
        for file_name, content in files.items():
            write(root, file_name, content)
        done = run_scenario(root, args, env_extra, opt_in)
        results[name] = (done, RETRY.read_record(root / "record.jsonl"), root)
    return results


def test_a_test_that_fails_twice_stays_red_and_is_not_recorded(runs: Runs) -> None:
    done, record, _ = runs["broken"]
    # Retried, failed again: in the record with that outcome, never as a flaky pass.
    assert record == [
        {
            "nodeid": "test_a.py::test_broken",
            "first_failure": "ValueError: always",
            "outcome": RETRY.FAILED_AGAIN,
            "shard": None,
            "head_sha": None,
        }
    ]
    assert "0 flaky test(s) passed on retry, 1 failed again, recorded in " in done.stdout
    assert done.returncode == 1, done.stdout
    assert "retrying 1 failed test(s) once" in done.stdout


def test_a_flaky_pass_turns_green_and_is_recorded(runs: Runs) -> None:
    done, record, _ = runs["flaky"]
    assert done.returncode == 0, done.stdout
    assert "1 flaky test(s) passed on retry, 0 failed again, recorded in " in done.stdout
    assert record == [
        {
            "nodeid": "test_a.py::test_flaky",
            "first_failure": "AssertionError: first try only",
            "outcome": RETRY.PASSED_ON_RETRY,
            "shard": "3",
            "head_sha": SHA,
        }
    ]


def test_one_flaky_and_one_broken_test_keep_the_job_red_and_record_the_flaky(
    runs: Runs,
) -> None:
    done, record, _ = runs["flaky_and_broken"]
    assert done.returncode == 1, done.stdout
    assert outcomes(record) == {
        "test_a.py::test_flaky": RETRY.PASSED_ON_RETRY,
        "test_a.py::test_broken": RETRY.FAILED_AGAIN,
    }
    assert "1 flaky test(s) passed on retry, 1 failed again, recorded in " in done.stdout


def test_more_than_five_failures_retry_nothing(runs: Runs) -> None:
    done, record, _ = runs["over_cap"]
    assert done.returncode == 1, done.stdout
    assert "7 tests failed, over the cap of 5; no retry" in done.stdout
    assert "retrying" not in done.stdout
    assert record == []


@pytest.mark.parametrize("name", ["off", "event_push", "event_empty"])
def test_no_opt_in_or_another_event_retries_nothing(runs: Runs, name: str) -> None:
    done, record, _ = runs[name]
    assert done.returncode == 1, done.stdout
    assert "shard-retry" not in done.stdout
    assert record == []


def test_an_xdist_shard_retries_a_grouped_test_by_its_plain_id(runs: Runs) -> None:
    done, record, _ = runs["xdist"]
    assert done.returncode == 0, done.stdout
    assert outcomes(record) == {"test_a.py::test_flaky": RETRY.PASSED_ON_RETRY}


def test_any_xdist_group_name_is_retried(runs: Runs) -> None:
    done, record, _ = runs["xdist_odd_groups"]
    assert done.returncode == 0, done.stdout
    assert outcomes(record) == {
        f"test_{name}.py::test_flaky": RETRY.PASSED_ON_RETRY for name in "abc"
    }


def test_a_grouped_failure_retries_itself_not_a_shorter_id(runs: Runs) -> None:
    done, record, _ = runs["xdist_prefix_ids"]
    assert done.returncode == 1, done.stdout
    assert "retrying 1 failed test(s) once" in done.stdout
    # It failed again, and is recorded under the plain id the serial pass ran.
    assert outcomes(record) == {"test_a.py::test_x[a]@b]": RETRY.FAILED_AGAIN}


@pytest.mark.parametrize("name", ["ini_positional", "env_positional"])
def test_a_selection_in_addopts_retries_only_the_failed_test(runs: Runs, name: str) -> None:
    done, record, root = runs[name]
    assert done.returncode == 0, done.stdout
    assert outcomes(record) == {"test_a.py::test_flaky": RETRY.PASSED_ON_RETRY}
    # test_counted passed in the first session: the retry pass deselected it.
    assert (root / "runs-test_b").read_text(encoding="utf-8") == "x"


@pytest.mark.parametrize("name", ["env_loads_plugin", "terminator"])
def test_a_flaky_pass_turns_green_however_the_shard_was_invoked(runs: Runs, name: str) -> None:
    # The plugin loaded by $PYTEST_ADDOPTS alone, and file arguments after `--`:
    # the retry pass starts, runs the one failed test and reports it.
    done, record, _ = runs[name]
    assert done.returncode == 0, done.stdout
    assert "shard-retry: retrying 1 failed test(s) once." in done.stdout
    assert outcomes(record) == {"test_a.py::test_flaky": RETRY.PASSED_ON_RETRY}


def test_a_setup_error_is_retried(runs: Runs) -> None:
    done, record, _ = runs["setup_error"]
    assert done.returncode == 0, done.stdout
    assert record[0]["first_failure"] == "RuntimeError: setup broke"


def test_a_session_stopped_early_retries_nothing(runs: Runs) -> None:
    # `-x` stopped after the flaky test, so the broken one never ran.
    done, record, _ = runs["fail_fast"]
    assert done.returncode == 1, done.stdout
    assert "the session stopped early; no retry" in done.stdout
    assert "retrying" not in done.stdout
    assert record == []


def test_a_retry_pass_that_exits_non_zero_keeps_the_job_red(runs: Runs) -> None:
    done, record, _ = runs["retry_exits_nonzero"]
    assert done.returncode == 1, done.stdout
    assert "the retry pass did not exit 0; job stays red" in done.stdout
    assert outcomes(record) == {"test_a.py::test_flaky": RETRY.PASSED_ON_RETRY}


def test_a_guard_that_fails_a_clean_run_still_fails_it_after_a_green_retry(runs: Runs) -> None:
    done, record, _ = runs["guard_after_retry"]
    assert done.returncode == 1, done.stdout
    assert outcomes(record) == {"test_a.py::test_flaky": RETRY.PASSED_ON_RETRY}


def test_a_failure_the_retry_pass_did_not_rerun_keeps_the_job_red(runs: Runs) -> None:
    # Two tests failed; the retry pass collected one, ran it green and exited 0.
    done, record, _ = runs["not_rerun"]
    assert "shard-retry: retrying 2 failed test(s) once." in done.stdout
    assert "1 flaky test(s) passed on retry, 1 failed again, recorded in " in done.stdout
    assert done.returncode == 1, done.stdout
    # The one the pass never ran did not pass a retry: it is recorded as failed again.
    assert outcomes(record) == {
        "test_a.py::test_flaky": RETRY.PASSED_ON_RETRY,
        "test_a.py::test_only_first": RETRY.FAILED_AGAIN,
    }


def test_a_crashed_worker_retries_nothing(runs: Runs) -> None:
    # The flaky test would pass a retry, and a green one would hide the crash.
    done, record, _ = runs["worker_crash"]
    assert done.returncode != 0, done.stdout
    assert "shard-retry: a worker crashed; no retry." in done.stdout
    assert "retrying" not in done.stdout and record == []


def test_a_test_whose_teardown_fails_on_the_retry_is_not_a_flaky_pass(runs: Runs) -> None:
    done, record, _ = runs["teardown_fails_on_retry"]
    assert "shard-retry: retrying 1 failed test(s) once." in done.stdout
    assert done.returncode == 1, done.stdout
    assert outcomes(record) == {"test_a.py::test_flaky": RETRY.FAILED_AGAIN}


def test_the_root_conftest_guard_reads_the_status_a_green_retry_left(tmp_path: Path) -> None:
    # A session the retry turned green (status 0, hook argument still 1) that left
    # an entry at the repository root is failed; so is a plain clean one; a status
    # that is not OK is left as it is.
    done = stdlib_run_killing_tree_on_timeout(
        [sys.executable, "-B", "-c", ROOT_GUARD, str(ROOT / "conftest.py")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["1", "1", "2"]


@pytest.mark.parametrize("how", ["timeout", "no results file", "results of another shape"])
def test_a_retry_pass_that_fails_to_report_passes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if how == "results of another shape":
            Path(kwargs["env"][RETRY.ENV_RESULTS]).write_text('{"passed": ["x"]}', "utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(RETRY.subprocess, "run", run)
    manager = SimpleNamespace(hasplugin=lambda name: False, get_plugin=lambda name: None)
    config = SimpleNamespace(
        invocation_params=SimpleNamespace(args=("-q",), dir=tmp_path),
        pluginmanager=manager,
        getini=lambda name: [],
    )
    retry = RETRY._Retry(config)
    assert retry._run_retry(SimpleNamespace(config=config), ["t.py::test_x"]) == ({}, {}, False)
    said = "the retry pass failed to report" in capsys.readouterr().out
    assert said == (how != "results of another shape")


def test_a_retry_pass_that_did_not_report_records_each_test_as_failed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv(RETRY.ENV_RECORD, str(record))
    monkeypatch.setenv(RETRY.ENV_SHARD, "7")
    monkeypatch.delenv(RETRY.ENV_HEAD_SHA, raising=False)
    manager = SimpleNamespace(get_plugin=lambda name: None)
    session = SimpleNamespace(
        config=SimpleNamespace(pluginmanager=manager),
        exitstatus=1,
        shouldfail=False,
        shouldstop=False,
        testsfailed=2,
    )
    retry = RETRY._Retry(session.config)
    retry.failures = {"t.py::test_a@g": "E: a", "t.py::test_b": "E: b"}
    retry.failed_reports = 2
    # The pass ran `test_a` under its plain id and reported no pass; it never ran `test_b`.
    monkeypatch.setattr(
        retry,
        "_run_retry",
        lambda session, nodeids: ({}, {"t.py::test_a@g": "t.py::test_a"}, False),
    )
    retry.pytest_sessionfinish(session, 1)
    assert session.exitstatus == 1
    assert RETRY.read_record(record) == [
        {
            "nodeid": nodeid,
            "first_failure": failure,
            "outcome": RETRY.FAILED_AGAIN,
            "shard": "7",
            "head_sha": None,
        }
        for nodeid, failure in (("t.py::test_a", "E: a"), ("t.py::test_b", "E: b"))
    ]


def test_a_failed_report_is_counted_the_way_pytest_counts_it() -> None:
    retry = RETRY._Retry(SimpleNamespace())
    plain = SimpleNamespace(failed=True, when="call", nodeid="t.py::test_a", longrepr="boom")
    retry.pytest_runtest_logreport(plain)
    assert retry.failed_reports == 1
    # pytest leaves a failed report that carries `wasxfail` out of `testsfailed`.
    marked = SimpleNamespace(**{**vars(plain), "nodeid": "t.py::test_b", "wasxfail": "known"})
    retry.pytest_runtest_logreport(marked)
    assert retry.failed_reports == 1 and list(retry.failures) == ["t.py::test_a", "t.py::test_b"]
    # A report outside the three phases is a crash, and is no test failure.
    retry.pytest_runtest_logreport(SimpleNamespace(failed=True, when="???", nodeid="t.py::c"))
    assert retry.crashed and retry.failed_reports == 1 and len(retry.failures) == 2


def test_a_failure_beside_the_tests_retries_nothing(runs: Runs) -> None:
    done, record, _ = runs["fail_beside_tests"]
    assert done.returncode == 1, done.stdout
    assert "a failure beside the tests; no retry" in done.stdout
    assert "retrying" not in done.stdout
    assert record == []


# -- the helpers, in process -------------------------------------------------


def test_retry_argv_keeps_the_sessions_arguments_and_runs_serial() -> None:
    args = ["-q", "-n", "4", "--rootdir", "test", "-m", "not slow", "test", "test/b"]
    argv = RETRY.retry_argv(args, True)
    assert argv[:3] == [sys.executable, "-m", "pytest"]
    assert argv[3:] == [*args, "-p", RETRY.PLUGIN, "-n", "0", "-o", "addopts="]
    assert "-n" not in RETRY.retry_argv(["-q"], False)


@pytest.mark.parametrize(
    "where",
    [
        (["-p", "scripts.ci_shard_retry"], []),
        (["-pscripts.ci_shard_retry"], []),
        ([], ["-p", "scripts.ci_shard_retry"]),
    ],
)
def test_retry_argv_adds_no_second_load_of_the_plugin(
    where: tuple[list[str], list[str]],
) -> None:
    args, addopts = where
    argv = RETRY.retry_argv(args, False, addopts)
    assert argv[3:] == [*args, "-o", "addopts=" + " ".join(addopts)]


def test_retry_argv_adds_its_options_before_a_terminator_and_leaves_the_rest() -> None:
    # After `--` every token is a file argument: one spelt like a report option
    # stays, and one spelt like this plugin's `-p` does not count as a load.
    tail = ["--", "test/a.py", "--junitxml=x.xml", "-p", RETRY.PLUGIN, "--", "--cov"]
    argv = RETRY.retry_argv(["-q", "--junitxml=report.xml", *tail], True)
    assert argv[3:] == ["-q", "-p", RETRY.PLUGIN, "-n", "0", "-o", "addopts=", *tail]
    assert RETRY.split_terminator(["-q", "test"]) == (["-q", "test"], [])
    assert RETRY.split_terminator(["--"]) == ([], ["--"])


def test_no_environment_text_reaches_the_retry_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every variable the retry pass inherits carries a marker; none may be an argument.
    marker = "from-the-environment"
    for name in ("PYTEST_ADDOPTS", RETRY.ENV_RECORD, RETRY.ENV_SHARD, RETRY.ENV_HEAD_SHA):
        monkeypatch.setenv(name, f"--{marker}-{name.lower()}")
    monkeypatch.setenv("PYTEST_ADDOPTS", f"-p {RETRY.PLUGIN} --{marker} --junitxml=r.xml")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        results = Path(kwargs["env"][RETRY.ENV_RESULTS])
        ids = {"t.py::test_x": "t.py::test_x"}
        results.write_text(json.dumps({"passed": ids, "ran": ids}), "utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(RETRY.subprocess, "run", run)
    manager = SimpleNamespace(hasplugin=lambda name: False, get_plugin=lambda name: None)
    config = SimpleNamespace(
        invocation_params=SimpleNamespace(args=("-q", "test"), dir=tmp_path),
        pluginmanager=manager,
        getini=lambda name: ["--strict-markers"],
    )
    retry = RETRY._Retry(config)
    passed, ran, ok = retry._run_retry(SimpleNamespace(config=config), ["t.py::test_x"])
    assert ok and passed == ran == {"t.py::test_x": "t.py::test_x"}
    (argv, kwargs), *more = calls
    assert not more
    # The command line is the session's own arguments and the ini options, nothing else.
    assert argv[3:] == ["-q", "test", "-p", RETRY.PLUGIN, "-o", "addopts=--strict-markers"]
    assert not any(marker in token for token in argv)
    # $PYTEST_ADDOPTS goes to the pass as its environment, less the report options.
    assert kwargs["env"]["PYTEST_ADDOPTS"] == f"-p {RETRY.PLUGIN} --{marker}"
    assert json.loads(kwargs["env"][RETRY.ENV_TARGETS]) == ["t.py::test_x"]


def test_match_targets_pairs_a_grouped_id_with_its_plain_id() -> None:
    collected = ["t.py::test_a", "t.py::test_b[x@y]", "t.py::test_c", "t.py::test_d"]
    targets = ["t.py::test_a@team A", "t.py::test_b[x@y]", "t.py::test_c@" + "g" * 120]
    assert RETRY.match_targets(collected, targets) == {
        "t.py::test_a": "t.py::test_a@team A",
        "t.py::test_b[x@y]": "t.py::test_b[x@y]",
        "t.py::test_c": "t.py::test_c@" + "g" * 120,
    }
    # An exact id wins over a shorter id it starts with.
    assert RETRY.match_targets(["t::f[a]", "t::f[a]@b]"], ["t::f[a]@b]"]) == {
        "t::f[a]@b]": "t::f[a]@b]"
    }
    # A grouped id picks the longest collected id it starts with.
    assert RETRY.match_targets(["t::f[a]", "t::f[a]@b]"], ["t::f[a]@b]@g"]) == {
        "t::f[a]@b]": "t::f[a]@b]@g"
    }


def test_retry_argv_drops_the_options_that_write_a_report() -> None:
    args = [
        "--junitxml=out.xml",
        "--junit-xml",
        "out2.xml",
        "--report-log",
        "log.jsonl",
        "--cov=src",
        "--cov",
        "pkg",
        "--cov-report",
        "xml",
        "--cov-append",
        "--cov",
        "--json-report",
        "--json-report-file",
        "r.json",
        "-q",
        "test/a.py",
    ]
    addopts = ["--verbose", "--junitxml", "ini.xml", "-m", "not slow"]
    argv = RETRY.retry_argv(args, False, addopts)
    assert argv[3:] == [
        "-q",
        "test/a.py",
        "-p",
        RETRY.PLUGIN,
        "-o",
        "addopts=--verbose -m 'not slow'",
    ]


def test_retry_env_drops_report_options_from_pytest_addopts() -> None:
    env = RETRY.retry_env({"PYTEST_ADDOPTS": "-q --junitxml=x.xml --cov src -m 'a b'", "K": "v"})
    assert env == {"PYTEST_ADDOPTS": "-q -m 'a b'", "K": "v"}
    assert RETRY.retry_env({"K": "v"}) == {"K": "v"}


def test_a_collection_error_retries_nothing(runs: Runs) -> None:
    done, record, _ = runs["collection_error"]
    assert done.returncode == 1, done.stdout
    assert "a collection error; no retry" in done.stdout
    assert "retrying" not in done.stdout
    assert record == []


@pytest.mark.parametrize("name", ["ini_report", "env_report"])
def test_the_retry_pass_leaves_the_first_sessions_report_whole(runs: Runs, name: str) -> None:
    done, record, root = runs[name]
    assert done.returncode == 0, done.stdout
    assert outcomes(record) == {"test_a.py::test_flaky": RETRY.PASSED_ON_RETRY}
    report = (root / "report.xml").read_text(encoding="utf-8")
    # The retry pass ran test_flaky alone: a report it rewrote would lack test_fine.
    assert 'name="test_fine"' in report and 'name="test_flaky"' in report


def test_printable_keeps_one_line_of_ascii_cut_to_the_limit() -> None:
    assert RETRY.printable("a\tb\x1b[31mc☃d") == "a b [31mc d"
    assert len(RETRY.printable("x" * 500)) == RETRY.SUMMARY_CHARS


def test_failure_summary_falls_back_to_the_report_text() -> None:
    crash = SimpleNamespace(reprcrash=SimpleNamespace(message="KeyError: 'k'\nmore"))
    assert RETRY.failure_summary(SimpleNamespace(longrepr=crash, when="call")) == "KeyError: 'k'"
    plain = SimpleNamespace(longrepr="\nboom here\n", when="teardown")
    assert RETRY.failure_summary(plain) == "boom here"
    assert RETRY.failure_summary(SimpleNamespace(longrepr=None, when="setup")) == (
        "failed in setup"
    )


def test_record_path_defaults_under_the_runner_temp_dir(tmp_path: Path) -> None:
    assert RETRY.record_path({"RUNNER_TEMP": str(tmp_path)}) == tmp_path / "ci-shard-retry.jsonl"
    named = tmp_path / "r.jsonl"
    assert RETRY.record_path({RETRY.ENV_RECORD: str(named)}) == named


def test_enabled_needs_the_opt_in_and_a_pull_request_event() -> None:
    assert RETRY.enabled({RETRY.ENV_ON: "1", RETRY.ENV_EVENT: "pull_request"})
    assert not RETRY.enabled({RETRY.ENV_ON: "1", RETRY.ENV_EVENT: "push"})
    assert not RETRY.enabled({RETRY.ENV_EVENT: "pull_request"})
    assert not RETRY.enabled({RETRY.ENV_ON: "0", RETRY.ENV_EVENT: "pull_request"})


# -- summary -----------------------------------------------------------------


def test_summary_counts_both_outcomes_and_writes_one_line_per_retried_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "record.jsonl"
    rows = [
        {
            "nodeid": "test/test_a.py::test_x@g",
            "first_failure": "AssertionError: `x`",
            "outcome": RETRY.PASSED_ON_RETRY,
        },
        {
            "nodeid": "test/test_c.py::test_z",
            "first_failure": "ValueError: always",
            "outcome": RETRY.FAILED_AGAIN,
        },
        # A row written before the record had outcomes: every row then was a flaky pass.
        {"nodeid": "test/test_b.py::test_y", "first_failure": "OSError: gone"},
        # An outcome this reader does not know is not shown as either.
        {"nodeid": "test/test_d.py::test_w", "first_failure": "x", "outcome": "skipped"},
        {"nodeid": "test/test_e.py::test_v", "first_failure": "x", "outcome": None},
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows) + "not json\n[1]\n", encoding="utf-8"
    )
    assert [row["nodeid"] for row in RETRY.read_record(path)] == [
        "test/test_a.py::test_x@g",
        "test/test_c.py::test_z",
        "test/test_b.py::test_y",
    ]
    assert RETRY.count_outcomes(RETRY.read_record(path)) == (2, 1)
    summary = tmp_path / "summary"
    env = {RETRY.ENV_RECORD: str(path), "GITHUB_STEP_SUMMARY": str(summary)}
    assert RETRY.cmd_summary(env) == 0
    expected = (
        "Shard retry: 2 test(s) passed on retry (flaky), 1 failed again.\n"
        "- Flaky, passed on retry: `test/test_a.py::test_x@g` (first failure: AssertionError: 'x')\n"
        "- Failed again on retry: `test/test_c.py::test_z` (first failure: ValueError: always)\n"
        "- Flaky, passed on retry: `test/test_b.py::test_y` (first failure: OSError: gone)\n"
    )
    assert summary.read_text(encoding="utf-8") == expected
    assert capsys.readouterr().out == expected


def test_summary_counts_a_record_of_failed_retries_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "record.jsonl"
    row = {"nodeid": "t.py::test_a", "first_failure": "E: x", "outcome": RETRY.FAILED_AGAIN}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert RETRY.cmd_summary({RETRY.ENV_RECORD: str(path)}) == 0
    assert capsys.readouterr().out == (
        "Shard retry: 0 test(s) passed on retry (flaky), 1 failed again.\n"
        "- Failed again on retry: `t.py::test_a` (first failure: E: x)\n"
    )


@pytest.mark.parametrize("content", [None, ""], ids=["missing", "empty"])
def test_summary_on_no_record_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    path = tmp_path / "record.jsonl"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    summary = tmp_path / "summary"
    env = {RETRY.ENV_RECORD: str(path), "GITHUB_STEP_SUMMARY": str(summary)}
    assert RETRY.cmd_summary(env) == 0
    assert not summary.exists()
    assert capsys.readouterr().out == "Shard retry: no test was retried.\n"
