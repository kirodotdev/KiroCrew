"""The Windows shards hold: ci.yml's opt-in "Windows after Linux" edge.

``.github/scripts/windows_shards_hold.py`` is the whole wait ci.yml's
``windows-shards-hold`` job makes. The first half drives its ``main`` with a
scripted job listing, a recorded sleep and a clock the sleep advances, and pins
the properties the workflow relies on:

* it holds only on a red Linux shard it read, and a hold always exits 1 with
  ``hold=true`` written, so a held run has a failed job;
* everything else starts the Windows shards: all shards green, a mode that is
  not ``after-linux``, malformed inputs, a listing with no Linux shard, a
  conclusion it does not judge, reads that keep failing, a spent budget;
* a re-run is judged on its newest attempt's rows;
* it only makes GETs, and few of them.

The second half reads ci.yml: with the repository variable unset the job is
skipped and ``backend-test-windows`` starts exactly when it did before the job
existed, a hold skips the Windows shards and nothing else, and the job name and
shard count the script is given are the Linux shard job's own.
"""

from __future__ import annotations

import importlib.util
import inspect
import itertools
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from test_fast_gate_barrier import _LINT_GATE, _evaluate_guard, _simulate_pull_request

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "windows_shards_hold.py"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_JOB = "windows-shards-hold"
_WINDOWS = "backend-test-windows"
_LINUX = "backend-test"
_VARIABLE = "vars.CI_WINDOWS_SHARDS"
_MODE = "after-linux"

_REPO = "kirodotdev/KiroCrew"
_RUN_ID = "44367545719"
_LINUX_NAME = "Backend Tests"


@pytest.fixture(scope="module")
def hold() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windows_shards_hold", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(
    shard: int,
    conclusion: str | None = "success",
    status: str = "completed",
    attempt: int = 1,
    name: str | None = None,
) -> dict:
    return {
        "id": attempt * 1000 + shard,
        "run_attempt": attempt,
        "name": name or f"{_LINUX_NAME} (3.12, {shard})",
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
    }


def _shards(reds: dict[int, str] | None = None, running: tuple[int, ...] = ()) -> list[dict]:
    reds = reds or {}
    return [
        _row(n, status="in_progress") if n in running else _row(n, reds.get(n, "success"))
        for n in range(1, 9)
    ]


# Rows of the same run that are not Linux shards and must never be judged.
_NOISE = [
    _row(1, "failure", name="Backend Tests (Windows) (1)"),
    _row(2, "failure", name="Backend Tests (IPv6, ubuntu-latest)"),
    _row(3, "failure", name="Backend Tests (crew container)"),
    _row(4, "failure", name="Backend Tests"),
    _row(5, "failure", name="Frontend Tests (3)"),
    _row(6, "skipped", name="Backend Tests (Windows)"),
]


class _Harness:
    """Runs ``main`` against a scripted sequence of listings.

    Each entry of ``reads`` answers one whole listing read: a list of rows, or
    None for a read that fails. The last entry repeats. The sleep is recorded
    and moves the clock, so nothing here waits or reads the time.
    """

    def __init__(
        self,
        hold: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reads: list[list[dict] | None],
    ) -> None:
        self.hold = hold
        self.reads = reads
        self.pages: list[tuple[str, str, int]] = []
        self.sleeps: list[float] = []
        self.now = 0.0
        self.output = tmp_path / "github_output"
        self.summary = tmp_path / "step_summary"
        self.output.write_text("", encoding="utf-8")
        self.summary.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(self.output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(self.summary))
        self._listing = -1

    def _reader(self, repo: str, run_id: str, page: int) -> list[dict] | None:
        self.pages.append((repo, run_id, page))
        if page == 1:
            self._listing += 1
        rows = self.reads[min(self._listing, len(self.reads) - 1)]
        if rows is None:
            return None
        size = self.hold.PAGE_SIZE
        return rows[(page - 1) * size : page * size]

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, **env: str) -> int:
        values = {
            "MODE": _MODE,
            "REPO": _REPO,
            "RUN_ID": _RUN_ID,
            "LINUX_JOB": _LINUX_NAME,
            "LINUX_SHARDS": "8",
            **env,
        }
        return self.hold.main(
            env=values, reader=self._reader, sleep=self._sleep, clock=lambda: self.now
        )

    @property
    def listings(self) -> int:
        return sum(1 for _repo, _run, page in self.pages if page == 1)

    @property
    def held(self) -> bool:
        return "hold=true" in self.output.read_text(encoding="utf-8")

    @property
    def summary_text(self) -> str:
        return self.summary.read_text(encoding="utf-8")


@pytest.fixture
def harness(hold: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def build(*reads: list[dict] | None) -> _Harness:
        return _Harness(hold, tmp_path, monkeypatch, list(reads))

    return build


class TestItHoldsOnlyOnARedLinuxShardItRead:
    @pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
    def test_a_red_shard_holds_and_the_hold_is_a_failed_job(
        self, harness, capsys: pytest.CaptureFixture[str], conclusion: str
    ) -> None:
        run = harness(_NOISE + _shards({3: conclusion}))
        assert run.run() == 1
        assert run.held
        out = capsys.readouterr().out
        assert "::error title=Windows shards held::" in out
        assert f"{_LINUX_NAME} (3.12, 3) ({conclusion})" in out
        assert "This head has no Windows verdict" in run.summary_text
        assert "Re-run failed jobs" in run.summary_text

    def test_one_red_shard_holds_without_waiting_for_the_others(self, harness) -> None:
        run = harness(_shards({2: "failure"}, running=(1, 4, 5, 6, 7, 8)))
        assert run.run() == 1
        assert run.held
        assert run.listings == 1

    def test_every_red_shard_is_named(self, harness) -> None:
        run = harness(_shards({2: "failure", 7: "timed_out"}))
        assert run.run() == 1
        assert f"- {_LINUX_NAME} (3.12, 2) (failure)" in run.summary_text
        assert f"- {_LINUX_NAME} (3.12, 7) (timed_out)" in run.summary_text

    def test_all_shards_green_releases(self, harness) -> None:
        run = harness(_NOISE + _shards())
        assert run.run() == 0
        assert not run.held
        assert "All 8 'Backend Tests' shards passed" in run.summary_text

    def test_it_waits_while_shards_run_and_releases_when_they_pass(self, harness, hold) -> None:
        run = harness(_shards(running=(1, 2)), _shards(running=(2,)), _shards())
        assert run.run() == 0
        assert not run.held
        assert run.listings == 3
        assert run.sleeps == [hold.FIRST_WAIT, hold.POLL_INTERVAL, hold.POLL_INTERVAL]

    def test_it_waits_while_shards_run_and_holds_when_one_goes_red(self, harness) -> None:
        run = harness(_shards(running=(1, 2)), _shards({1: "failure"}, running=(2,)))
        assert run.run() == 1
        assert run.held
        assert run.listings == 2

    def test_a_shard_that_is_not_listed_yet_is_waited_for(self, harness) -> None:
        run = harness(_shards()[:5], _shards())
        assert run.run() == 0
        assert run.listings == 2

    def test_a_rerun_is_judged_on_the_newest_attempts_rows(self, harness) -> None:
        # Attempt 1 left shard 3 red; "Re-run failed jobs" added a row for it.
        first = _shards({3: "failure"})
        rerunning = first + [_row(3, status="in_progress", attempt=2)]
        passed = first + [_row(3, attempt=2)]
        run = harness(rerunning, passed)
        assert run.run() == 0
        assert not run.held
        assert run.listings == 2

    def test_a_rerun_that_fails_again_holds(self, harness) -> None:
        run = harness(_shards({3: "failure"}) + [_row(3, "failure", attempt=2)])
        assert run.run() == 1
        assert run.held

    def test_an_earlier_green_does_not_hide_a_newer_red(self, harness) -> None:
        run = harness(_shards() + [_row(5, "failure", attempt=2)])
        assert run.run() == 1
        assert run.held


class TestEverythingElseStartsTheWindowsShards:
    @pytest.mark.parametrize("mode", ["", "true", "enforce", "After-Linux", "after-linux "])
    def test_any_other_mode_releases_without_reading(self, harness, mode: str) -> None:
        run = harness(_shards({1: "failure"}))
        assert run.run(MODE=mode) == 0
        assert not run.held
        assert run.pages == []
        assert run.sleeps == []

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param({"REPO": ""}, id="no-repository"),
            pytest.param({"REPO": "not a repository"}, id="malformed-repository"),
            pytest.param({"RUN_ID": ""}, id="no-run-id"),
            pytest.param({"RUN_ID": "12/../3"}, id="malformed-run-id"),
            pytest.param({"LINUX_JOB": ""}, id="no-job-name"),
            pytest.param({"LINUX_SHARDS": "0"}, id="no-shards"),
            pytest.param({"LINUX_SHARDS": "eight"}, id="malformed-shard-count"),
        ],
    )
    def test_malformed_inputs_release_without_reading(
        self, harness, capsys: pytest.CaptureFixture[str], env: dict[str, str]
    ) -> None:
        run = harness(_shards({1: "failure"}))
        assert run.run(**env) == 0
        assert not run.held
        assert run.pages == []
        assert "::warning::Windows Shards Hold:" in capsys.readouterr().out

    def test_a_listing_with_no_linux_shard_releases(
        self, harness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = harness(_NOISE)
        assert run.run() == 0
        assert not run.held
        assert run.listings == 1
        assert "lists no 'Backend Tests' shard" in capsys.readouterr().out

    @pytest.mark.parametrize("conclusion", ["skipped", "neutral", "action_required", None])
    def test_a_conclusion_it_does_not_judge_releases(self, harness, conclusion: str | None) -> None:
        rows = _shards()
        rows[3]["conclusion"] = conclusion
        run = harness(rows)
        assert run.run() == 0
        assert not run.held

    def test_a_red_shard_outranks_a_conclusion_it_does_not_judge(self, harness) -> None:
        rows = _shards({2: "failure"})
        rows[3]["conclusion"] = "skipped"
        run = harness(rows)
        assert run.run() == 1
        assert run.held

    def test_reads_that_keep_failing_release(
        self, harness, hold, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = harness(None)
        assert run.run() == 0
        assert not run.held
        assert run.listings == hold.MAX_FAILED_READS
        assert "Could not read the jobs of run" in capsys.readouterr().out

    def test_one_failed_read_is_retried_and_does_not_count_against_later_ones(
        self, harness
    ) -> None:
        waiting = _shards(running=(1,))
        run = harness(None, None, waiting, None, None, _shards({1: "failure"}))
        assert run.run() == 1
        assert run.held
        assert run.listings == 6

    def test_a_listing_with_a_failed_page_is_a_failed_read(self, harness, hold) -> None:
        filler = [_row(n, name=f"Other ({n})") for n in range(hold.PAGE_SIZE)]
        run = harness(filler + _shards({1: "failure"}))
        # Page 1 is full, so page 2 is read; make that second page fail.
        reader = run._reader
        run._reader = lambda repo, run_id, page: None if page == 2 else reader(repo, run_id, page)
        assert run.run() == 0
        assert not run.held

    def test_a_red_shard_on_a_later_page_is_read(self, harness, hold) -> None:
        filler = [_row(n, name=f"Other ({n})") for n in range(hold.PAGE_SIZE + 5)]
        run = harness(filler + _shards({8: "failure"}))
        assert run.run() == 1
        assert [page for _repo, _run, page in run.pages] == [1, 2]

    def test_a_listing_that_never_ends_is_a_failed_read(self, harness, hold) -> None:
        endless = [_row(n, name=f"Other ({n})") for n in range(hold.PAGE_SIZE * hold.MAX_PAGES)]
        run = harness(endless)
        assert run.run() == 0
        assert not run.held
        assert len(run.pages) == hold.MAX_PAGES * hold.MAX_FAILED_READS

    def test_a_spent_budget_releases(
        self, harness, hold, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = harness(_shards(running=(1,)))
        assert run.run() == 0
        assert not run.held
        assert f"no verdict after {hold.TOTAL_BUDGET}s" in capsys.readouterr().out
        assert sum(run.sleeps) == hold.TOTAL_BUDGET
        expected = 1 + -(-(hold.TOTAL_BUDGET - hold.FIRST_WAIT) // hold.POLL_INTERVAL)
        assert run.listings == expected

    def test_a_hold_is_never_an_exit_zero(self, harness) -> None:
        # Every scripted ending: the exit code is 1 exactly when hold=true was written.
        endings = [
            [_shards()],
            [_shards({1: "failure"})],
            [_shards({1: "cancelled"})],
            [_NOISE],
            [None],
            [_shards(running=(1,))],
            [_shards(running=(1,)), _shards({4: "timed_out"})],
        ]
        for reads in endings:
            run = harness(*reads)
            run.output.write_text("", encoding="utf-8")
            assert (run.run() == 1) == run.held, reads


class TestItReadsLittleAndWritesNothing:
    def test_every_read_is_one_get_of_this_runs_jobs(self, hold: ModuleType) -> None:
        source = inspect.getsource(hold.read_jobs_page)
        assert '"--method",\n                "GET",' in source
        assert 'f"repos/{repo}/actions/runs/{run_id}/jobs"' in source
        assert "filter=all" in source
        text = _SCRIPT.read_text(encoding="utf-8")
        assert text.count("subprocess.run(") == 1
        for write in ("POST", "PUT", "PATCH", "DELETE"):
            assert f'"{write}"' not in text

    def test_the_reader_returns_none_when_gh_is_missing(
        self, hold: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        assert hold.read_jobs_page(_REPO, _RUN_ID, 1) is None

    def test_the_first_read_waits_and_the_interval_is_minutes(self, hold: ModuleType) -> None:
        # The installation token is shared: a poll in seconds would spend it.
        assert hold.FIRST_WAIT >= 300
        assert hold.POLL_INTERVAL >= 120
        assert hold.FIRST_WAIT < hold.TOTAL_BUDGET


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load((_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


_MIXED = {"only_backend": "false", "only_frontend": "false"}
_SAME_REPO = {"github.repository": _REPO, "github.event.pull_request.head.repo.full_name": _REPO}
_SURFACES = [
    _MIXED,
    {"only_backend": "true", "only_frontend": "false"},
    {"only_backend": "false", "only_frontend": "true"},
]
# `backend-test-windows`'s guard before this job existed.
_WINDOWS_GUARD_BEFORE = (
    "${{ !cancelled() && needs.lint-gate.result == 'success'"
    " && needs.changes.outputs.windows_tests != 'false' }}"
)


def _needs(ci: dict, job: str) -> list[str]:
    raw = ci["jobs"][job].get("needs") or []
    return [raw] if isinstance(raw, str) else list(raw)


class TestTheWorkflowEdge:
    @pytest.mark.parametrize("surface", _SURFACES)
    def test_with_the_variable_unset_the_job_is_skipped_and_the_shards_run(
        self, ci: dict, surface: dict[str, str]
    ) -> None:
        results = _simulate_pull_request(ci, surface, context=_SAME_REPO)
        assert results[_JOB] == "skipped"
        assert results[_WINDOWS] == "success"
        assert results[_LINUX] == "success"

    def test_with_the_job_skipped_the_windows_guard_is_the_guard_it_replaced(
        self, ci: dict
    ) -> None:
        guard = str(ci["jobs"][_WINDOWS]["if"])
        results = ["success", "failure", "cancelled", "skipped"]
        for lint_gate, windows_tests in itertools.product(results, ["", "true", "false"]):
            values = {
                f"needs.{_LINT_GATE}.result": lint_gate,
                "needs.changes.outputs.windows_tests": windows_tests,
                f"needs.{_JOB}.result": "skipped",
            }
            assert _evaluate_guard(guard, values) == _evaluate_guard(
                _WINDOWS_GUARD_BEFORE, values
            ), values

    def test_only_the_windows_shards_wait_for_the_job(self, ci: dict) -> None:
        waiting = [job for job in ci["jobs"] if _JOB in _needs(ci, job)]
        assert waiting == [_WINDOWS]
        # Its own needs are the ones the Windows shards already had, so a
        # skipped job adds no wait: they start when `lint-gate` is done.
        assert _needs(ci, _JOB) == ["changes", "await-fast-gate", _LINT_GATE]
        assert _needs(ci, _WINDOWS) == ["changes", "await-fast-gate", _LINT_GATE, _JOB]
        mentions = [
            job
            for job, spec in ci["jobs"].items()
            if job not in (_JOB, _WINDOWS) and _JOB in yaml.safe_dump(spec)
        ]
        assert not mentions, mentions

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_release_changes_no_jobs_result(self, ci: dict, surface: dict[str, str]) -> None:
        before = _simulate_pull_request(ci, surface, context=_SAME_REPO)
        after = _simulate_pull_request(ci, surface, context={**_SAME_REPO, _VARIABLE: _MODE})
        assert after[_JOB] == "success"
        assert {job: result for job, result in after.items() if job != _JOB} == {
            job: result for job, result in before.items() if job != _JOB
        }

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_hold_skips_the_windows_shards_and_nothing_else(
        self, ci: dict, surface: dict[str, str]
    ) -> None:
        before = _simulate_pull_request(ci, surface, forced={_LINUX: "failure"}, context=_SAME_REPO)
        held = _simulate_pull_request(
            ci,
            surface,
            forced={_LINUX: "failure", _JOB: "failure"},
            context={**_SAME_REPO, _VARIABLE: _MODE, f"needs.{_JOB}.outputs.hold": "true"},
        )
        assert before[_WINDOWS] == "success"
        assert held[_WINDOWS] == "skipped"
        # The held head is never green: the Linux shards and this job are red.
        assert held[_LINUX] == "failure"
        assert held[_JOB] == "failure"
        changed = {job for job in held if held[job] != before[job]}
        assert changed == {_JOB, _WINDOWS}

    def test_the_job_failing_for_another_reason_starts_the_windows_shards(self, ci: dict) -> None:
        # A runner fault or a timeout leaves the `hold` output empty.
        results = _simulate_pull_request(
            ci, _MIXED, forced={_JOB: "failure"}, context={**_SAME_REPO, _VARIABLE: _MODE}
        )
        assert results[_JOB] == "failure"
        assert results[_WINDOWS] == "success"

    def test_the_shards_read_the_hold_output_and_never_the_jobs_result(self, ci: dict) -> None:
        guard = str(ci["jobs"][_WINDOWS]["if"])
        assert f"needs.{_JOB}.outputs.hold != 'true'" in guard
        assert f"needs.{_JOB}.result" not in guard
        job = ci["jobs"][_JOB]
        assert job["outputs"] == {"hold": "${{ steps.read.outputs.hold }}"}
        assert job["steps"][-1]["id"] == "read"

    def test_a_hold_cannot_be_reported_green(self, ci: dict) -> None:
        # The script exits 1 whenever it writes hold=true (pinned above). Nothing
        # in the job may turn that exit into a passing step or a passing job.
        job = ci["jobs"][_JOB]
        assert "continue-on-error" not in job
        read = job["steps"][-1]
        assert "continue-on-error" not in read
        assert read["run"].rstrip().endswith("python3 .github/scripts/windows_shards_hold.py")
        assert "|| true" not in read["run"]

    @pytest.mark.parametrize(
        "context",
        [
            pytest.param({"github.event_name": "merge_group"}, id="merge-group"),
            pytest.param({"github.event_name": "push"}, id="push"),
            pytest.param(
                {"github.event.pull_request.head.repo.full_name": "someone/KiroCrew"}, id="fork"
            ),
            pytest.param({_VARIABLE: ""}, id="variable-unset"),
            pytest.param({_VARIABLE: "true"}, id="variable-not-a-mode"),
            pytest.param({_VARIABLE: "enforce"}, id="another-switch's-mode"),
            pytest.param({f"needs.{_LINT_GATE}.result": "skipped"}, id="lint-gate-skipped"),
            pytest.param({f"needs.{_LINT_GATE}.result": "failure"}, id="lint-gate-red"),
        ],
    )
    def test_the_job_does_not_start_outside_its_scope(
        self, ci: dict, context: dict[str, str]
    ) -> None:
        values = {
            "github.event_name": "pull_request",
            **_SAME_REPO,
            _VARIABLE: _MODE,
            f"needs.{_LINT_GATE}.result": "success",
        }
        guard = str(ci["jobs"][_JOB]["if"])
        assert _evaluate_guard(guard, values)
        assert not _evaluate_guard(guard, {**values, **context})

    def test_an_ai_review_hold_skips_this_job_and_the_shards_with_it(self, ci: dict) -> None:
        results = _simulate_pull_request(
            ci,
            _MIXED,
            forced={"ai-block-hold": "failure"},
            context={
                **_SAME_REPO,
                _VARIABLE: _MODE,
                "vars.CI_AI_BLOCK_HOLD": "enforce",
                "needs.ai-block-hold.outputs.hold": "true",
            },
        )
        assert results[_LINT_GATE] == "skipped"
        assert results[_JOB] == "skipped"
        assert results[_WINDOWS] == "skipped"
        assert results[_LINUX] == "skipped"

    def test_the_job_holds_read_scopes_only_and_runs_the_script(self, ci: dict) -> None:
        job = ci["jobs"][_JOB]
        assert job["name"] == "Windows Shards Hold"
        assert job["permissions"] == {"actions": "read", "contents": "read"}
        checkout, read = job["steps"]
        assert checkout["with"]["persist-credentials"] is False
        assert checkout["continue-on-error"] is True
        assert read["env"] == {
            "GH_TOKEN": "${{ github.token }}",
            "REPO": "${{ github.repository }}",
            "RUN_ID": "${{ github.run_id }}",
            "MODE": "${{ vars.CI_WINDOWS_SHARDS }}",
            "LINUX_JOB": ci["jobs"][_LINUX]["name"],
            "LINUX_SHARDS": str(len(ci["jobs"][_LINUX]["strategy"]["matrix"]["group"])),
        }

    def test_the_jobs_timeout_covers_the_scripts_budget(self, ci: dict, hold: ModuleType) -> None:
        assert ci["jobs"][_JOB]["timeout-minutes"] * 60 > hold.TOTAL_BUDGET


class TestTheScriptMatchesTheLinuxShardsCheckNames:
    """The hold keys on check names another job owns; a rename there must fail here."""

    def test_a_shard_check_is_named_job_name_then_the_matrix_values(
        self, ci: dict, hold: ModuleType
    ) -> None:
        linux = ci["jobs"][_LINUX]
        # No `name:` template on the matrix, so GitHub names each check
        # "<name> (<matrix values in order>)" and the shard number is last.
        assert "${{" not in linux["name"]
        assert list(linux["strategy"]["matrix"]) == ["python-version", "group"]
        rows = [
            {
                "id": group,
                "run_attempt": 1,
                "name": f"{linux['name']} ({python}, {group})",
                "status": "completed",
                "conclusion": "success",
            }
            for python in linux["strategy"]["matrix"]["python-version"]
            for group in linux["strategy"]["matrix"]["group"]
        ]
        shards = len(linux["strategy"]["matrix"]["group"])
        assert hold.judge(rows, linux["name"], shards) == ("green", [])

    def test_no_other_job_of_the_run_is_read_as_a_linux_shard(
        self, ci: dict, hold: ModuleType
    ) -> None:
        linux = ci["jobs"][_LINUX]["name"]
        others = []
        for job, spec in ci["jobs"].items():
            if job == _LINUX:
                continue
            name = str(spec.get("name", job))
            others += [name, f"{name} (1)", f"{name} (3.12)", f"{name} (ubuntu-latest, 3.12)"]
        rows = [
            {
                "id": n,
                "run_attempt": 1,
                "name": name,
                "status": "completed",
                "conclusion": "failure",
            }
            for n, name in enumerate(others)
        ]
        assert hold.newest_shard_rows(rows, linux) == {}
