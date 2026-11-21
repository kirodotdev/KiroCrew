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
    "setup_error": ({"test_a.py": SETUP_ONCE}, [], {}, True),
    "fail_fast": ({"test_a.py": FLAKY + BROKEN}, ["-x"], {}, True),
    "retry_exits_nonzero": ({"test_a.py": FLAKY, "conftest.py": RETRY_EXITS_3}, [], {}, True),
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
    try:
        # A timeout kills the whole tree, the plugin's retry child included.
        return stdlib_run_killing_tree_on_timeout(
            [sys.executable, "-B", "-m", "pytest", "-p", RETRY.PLUGIN, "-p", "no:cacheprovider"]
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
    assert done.returncode == 1, done.stdout
    assert "retrying 1 failed test(s) once" in done.stdout
    assert "0 flaky test(s) passed on retry" in done.stdout
    assert record == []


def test_a_flaky_pass_turns_green_and_is_recorded(runs: Runs) -> None:
    done, record, _ = runs["flaky"]
    assert done.returncode == 0, done.stdout
    assert "1 flaky test(s) passed on retry, recorded in " in done.stdout
    assert record == [
        {
            "nodeid": "test_a.py::test_flaky",
            "first_failure": "AssertionError: first try only",
            "shard": "3",
            "head_sha": SHA,
        }
    ]


def test_one_flaky_and_one_broken_test_keep_the_job_red_and_record_the_flaky(
    runs: Runs,
) -> None:
    done, record, _ = runs["flaky_and_broken"]
    assert done.returncode == 1, done.stdout
    assert [row["nodeid"] for row in record] == ["test_a.py::test_flaky"]


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
    assert [row["nodeid"] for row in record] == ["test_a.py::test_flaky"]


def test_any_xdist_group_name_is_retried(runs: Runs) -> None:
    done, record, _ = runs["xdist_odd_groups"]
    assert done.returncode == 0, done.stdout
    assert sorted(row["nodeid"] for row in record) == [
        f"test_{name}.py::test_flaky" for name in "abc"
    ]


@pytest.mark.parametrize("name", ["ini_positional", "env_positional"])
def test_a_selection_in_addopts_retries_only_the_failed_test(runs: Runs, name: str) -> None:
    done, record, root = runs[name]
    assert done.returncode == 0, done.stdout
    assert [row["nodeid"] for row in record] == ["test_a.py::test_flaky"]
    # test_counted passed in the first session: the retry pass deselected it.
    assert (root / "runs-test_b").read_text(encoding="utf-8") == "x"


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
    assert [row["nodeid"] for row in record] == ["test_a.py::test_flaky"]


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
        (["-p", "scripts.ci_shard_retry"], [], []),
        (["-pscripts.ci_shard_retry"], [], []),
        ([], ["-p", "scripts.ci_shard_retry"], []),
        ([], [], ["-p", "scripts.ci_shard_retry"]),
    ],
)
def test_retry_argv_adds_no_second_load_of_the_plugin(
    where: tuple[list[str], list[str], list[str]],
) -> None:
    args, addopts, env_addopts = where
    argv = RETRY.retry_argv(args, False, addopts, env_addopts)
    assert argv[3:] == [*args, "-o", "addopts=" + " ".join(addopts)]


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
    assert [row["nodeid"] for row in record] == ["test_a.py::test_flaky"]
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


def test_summary_writes_one_line_per_flaky_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "record.jsonl"
    rows = [
        {"nodeid": "test/test_a.py::test_x@g", "first_failure": "AssertionError: `x`"},
        {"nodeid": "test/test_b.py::test_y", "first_failure": "OSError: gone"},
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows) + "not json\n[1]\n", encoding="utf-8"
    )
    summary = tmp_path / "summary"
    env = {RETRY.ENV_RECORD: str(path), "GITHUB_STEP_SUMMARY": str(summary)}
    assert RETRY.cmd_summary(env) == 0
    expected = (
        "- Flaky, passed on retry: `test/test_a.py::test_x@g` (first failure: AssertionError: 'x')\n"
        "- Flaky, passed on retry: `test/test_b.py::test_y` (first failure: OSError: gone)\n"
    )
    assert summary.read_text(encoding="utf-8") == expected
    assert capsys.readouterr().out == expected


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
    assert capsys.readouterr().out == "Shard retry: no flaky retries.\n"
