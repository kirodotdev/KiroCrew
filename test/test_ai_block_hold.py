"""The AI review hold: ci.yml's opt-in gate on a published reviewer BLOCK.

``.github/scripts/ai-block-hold.sh`` is the whole read ci.yml's ``ai-block-hold``
job makes. This file runs it end to end against a stubbed ``gh`` and pins the
three properties the workflow relies on:

* it holds only on a BLOCK it positively read: every check-run of a blocking
  lane on the head is ``completed`` / ``failure`` and carries the error
  annotation that lane's gate step prints for this exact head;
* everything else releases the heavy jobs: a verdict not published yet, a pass,
  a lane that failed without reaching a verdict, another head's BLOCK, a read
  that fails or cannot be parsed, ``shadow`` mode, an unknown mode;
* it spends at most four GETs and makes no write.

The second half reads ci.yml: the job is skipped unless the repository variable
asks for it, ``lint-gate`` keys on the ``hold`` output and never on the job's
result, a hold skips every heavy job while Coverage Gate still reports, and the
words the script matches are the words the two review workflows print.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from test_fast_gate_barrier import _LINT_GATE, _evaluate_guard, _simulate_pull_request

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "ai-block-hold.sh"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_JOB = "ai-block-hold"
_VARIABLE = "vars.CI_AI_BLOCK_HOLD"

_REPO = "kirodotdev/KiroCrew"
_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"
_OPUS = "Opus 5.5 Review"
_GPT = "GPT 6.1 Review"

needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="runs the hold script under a POSIX bash",
)

# The script pipes both API answers through `jq`. A host without one gets the
# stand-in below instead of a skip, and a host with one runs every script test
# twice, once per instrument, so the stand-in is held to the real jq's answers.
_JQ_MODES = ["stand-in"] + (["host"] if shutil.which("jq") else [])

# The two programs ai-block-hold.sh runs, told apart by the `--arg` name, and
# nothing else. Like jq it exits non-zero on input that is not JSON and on a
# value of the wrong shape, which the script reads as an unreadable answer.
_JQ_STAND_IN = r"""
import json
import sys

args = sys.argv[1:]
if len(args) != 5 or args[0] != "-r" or args[1] != "--arg" or args[2] not in ("lane", "want"):
    sys.stderr.write("jq stand-in: unsupported invocation: %r\n" % (args,))
    sys.exit(2)
name, value = args[2], args[3]
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(2)


def members(node):
    if isinstance(node, dict):
        return list(node.values())
    if isinstance(node, list):
        return node
    sys.exit(5)


def field(node, key):
    if node is None:
        return None
    if not isinstance(node, dict):
        sys.exit(5)
    return node.get(key)


if name == "lane":
    if not isinstance(data, dict):
        sys.exit(5)
    for run in members(data.get("check_runs")):
        if field(run, "name") != value or field(field(run, "app"), "slug") != "github-actions":
            continue
        status, conclusion = field(run, "status"), field(run, "conclusion")
        if status == "completed" and conclusion in ("cancelled", "skipped"):
            continue
        print("%s %s %s" % (field(run, "id"), status, conclusion or "none"))
else:
    hits = 0
    for note in members(data):
        if field(note, "annotation_level") != "failure":
            continue
        message = field(note, "message") or ""
        if not isinstance(message, str):
            sys.exit(5)
        hits += value in message
    print(hits)
"""


@pytest.fixture(params=_JQ_MODES)
def jq_instrument(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """Put the jq stand-in first on the harness PATH, or leave the host's jq."""
    if request.param == "stand-in":
        program = tmp_path / "jq_stand_in.py"
        program.write_text(_JQ_STAND_IN, encoding="utf-8")
        jq = tmp_path / "jq"
        jq.write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" -I "{program}" "$@"\n', encoding="utf-8"
        )
        jq.chmod(0o755)
    return str(request.param)


def _check_run(
    rid: int,
    name: str,
    status: str = "completed",
    conclusion: str | None = "failure",
    app: str = "github-actions",
) -> dict:
    return {
        "id": rid,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": app},
    }


def _listing(*runs: dict) -> str:
    return json.dumps({"total_count": len(runs), "check_runs": list(runs)})


def _block_note(lane: str, sha: str = _SHA, level: str = "failure") -> dict:
    model = lane.removesuffix(" Review")
    return {
        "annotation_level": level,
        "message": f"{model} review flagged a blocking issue on {sha} — see the posted review summary.",
    }


_EXIT_NOTE = {"annotation_level": "failure", "message": "Process completed with exit code 1."}
_FAIL = "__FAIL__"


class _Harness:
    """A ``gh`` stub answering the two reads the script makes, from files.

    ``listings`` maps a lane name to the check-runs page for it and
    ``annotations`` a check-run id to its annotations page; ``_FAIL`` makes that
    read exit non-zero, and an absent entry is an empty listing.
    """

    def __init__(
        self,
        tmp_path: Path,
        listings: dict[str, str] | None = None,
        annotations: dict[int, str] | None = None,
    ) -> None:
        self._dir = tmp_path
        self._calls = tmp_path / "gh-calls"
        self._calls.write_text("", encoding="utf-8")
        self.output = tmp_path / "github-output"
        self.output.write_text("", encoding="utf-8")
        self.summary = tmp_path / "step-summary"
        self.summary.write_text("", encoding="utf-8")
        for lane, key in ((_OPUS, "opus"), (_GPT, "gpt")):
            page = (listings or {}).get(lane, _listing())
            (tmp_path / f"listing-{key}").write_text(page, encoding="utf-8")
        for rid, page in (annotations or {}).items():
            (tmp_path / f"annotations-{rid}").write_text(page, encoding="utf-8")
        gh = tmp_path / "gh"
        gh.write_text(
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
                printf '%s\\n' "$*" >> {self._calls}
                answer() {{
                  if [ ! -f "$1" ] || [ "$(cat "$1")" = "{_FAIL}" ]; then
                    echo "gh: simulated API failure" >&2
                    exit 1
                  fi
                  cat "$1"
                  exit 0
                }}
                case "$*" in
                  *"/annotations"*)
                    id="$(printf '%s' "$*" | sed -n 's#.*check-runs/\\([0-9]*\\)/annotations.*#\\1#p')"
                    answer "{tmp_path}/annotations-$id"
                    ;;
                  *"check_name={_OPUS}"*) answer "{tmp_path}/listing-opus" ;;
                  *"check_name={_GPT}"*) answer "{tmp_path}/listing-gpt" ;;
                  *) echo "gh: unexpected call: $*" >&2; exit 1 ;;
                esac
                """),
            encoding="utf-8",
        )
        gh.chmod(0o755)

    def run(self, mode: str = "enforce", sha: str = _SHA) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = f"{self._dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            GH_TOKEN="x",
            REPO=_REPO,
            SHA=sha,
            MODE=mode,
            GITHUB_OUTPUT=str(self.output),
            GITHUB_STEP_SUMMARY=str(self.summary),
            TMPDIR=str(self._dir),
        )
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            env=env,
            cwd=self._dir,
            **UTF8_TEXT,
        )

    @property
    def calls(self) -> list[str]:
        return self._calls.read_text(encoding="utf-8").splitlines()

    @property
    def held(self) -> bool:
        return "hold=true" in self.output.read_text(encoding="utf-8").splitlines()


def _blocked(tmp_path: Path, lane: str = _OPUS) -> _Harness:
    return _Harness(
        tmp_path,
        {lane: _listing(_check_run(7, lane))},
        {7: json.dumps([_EXIT_NOTE, _block_note(lane)])},
    )


@needs_bash
@pytest.mark.usefixtures("jq_instrument")
class TestTheHoldFiresOnlyOnAPublishedBlock:
    @pytest.mark.parametrize("lane", [_OPUS, _GPT])
    def test_enforce_holds_on_either_lanes_block_for_this_head(
        self, tmp_path: Path, lane: str
    ) -> None:
        h = _blocked(tmp_path, lane)
        proc = h.run("enforce")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert h.held
        assert f"::error title=Heavy jobs held::{lane} published BLOCK for {_SHA}" in proc.stdout
        # The author is told what was skipped and both ways out.
        summary = h.summary.read_text(encoding="utf-8")
        assert "heavy jobs held" in summary and "no test verdict" in summary
        assert "/ai-review override" in summary and "Re-run failed jobs" in summary

    def test_enforce_names_both_lanes_when_both_blocked(self, tmp_path: Path) -> None:
        h = _Harness(
            tmp_path,
            {_OPUS: _listing(_check_run(7, _OPUS)), _GPT: _listing(_check_run(8, _GPT))},
            {7: json.dumps([_block_note(_OPUS)]), 8: json.dumps([_block_note(_GPT)])},
        )
        proc = h.run("enforce")
        assert proc.returncode == 1
        assert f"{_OPUS}, {_GPT} published BLOCK" in proc.stdout

    def test_shadow_reports_the_block_and_releases(self, tmp_path: Path) -> None:
        h = _blocked(tmp_path)
        proc = h.run("shadow")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not h.held
        assert "::notice title=AI Review Hold (shadow)::" in proc.stdout
        assert "::error" not in proc.stdout
        assert "would hold" in h.summary.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("listing", "notes", "state"),
        [
            pytest.param(_listing(), None, "absent", id="no-verdict-published-yet"),
            pytest.param(
                _listing(_check_run(7, _OPUS, "queued", None)), None, "pending", id="queued"
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS, "in_progress", None)),
                None,
                "pending",
                id="in-progress",
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS, conclusion="success")), None, "passing", id="passed"
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS, conclusion="cancelled")),
                None,
                "absent",
                id="cancelled",
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS, conclusion="neutral")),
                None,
                "passing",
                id="neutral",
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS)),
                json.dumps([_EXIT_NOTE]),
                "incomplete",
                id="failed-without-a-verdict",
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS)),
                json.dumps([_block_note(_OPUS, _OTHER_SHA)]),
                "incomplete",
                id="block-names-another-head",
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS)),
                json.dumps([_block_note(_OPUS, level="warning")]),
                "incomplete",
                id="block-words-in-a-warning",
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS, app="some-other-app")),
                json.dumps([_block_note(_OPUS)]),
                "absent",
                id="check-run-from-another-app",
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS + " (same-repo lane, not applicable to forks)")),
                json.dumps([_block_note(_OPUS)]),
                "absent",
                id="check-run-under-another-name",
            ),
            pytest.param(_FAIL, None, "unreadable", id="listing-read-fails"),
            pytest.param("not json", None, "unreadable", id="listing-is-not-json"),
            pytest.param(
                _listing(_check_run(7, _OPUS)), _FAIL, "unreadable", id="notes-read-fails"
            ),
            pytest.param(
                _listing(_check_run(7, _OPUS)),
                json.dumps({"message": "Not Found"}),
                "unreadable",
                id="notes-are-an-error-body",
            ),
        ],
    )
    def test_enforce_releases_on_anything_short_of_a_block(
        self, tmp_path: Path, listing: str, notes: str | None, state: str
    ) -> None:
        h = _Harness(tmp_path, {_OPUS: listing}, {7: notes} if notes is not None else None)
        proc = h.run("enforce")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not h.held
        assert f"{_OPUS}: {state}" in proc.stdout
        assert "::error" not in proc.stdout
        if state == "unreadable":
            assert f"::warning::AI Review Hold: could not read '{_OPUS}'" in proc.stdout

    def test_a_head_shared_with_a_passing_run_is_not_held(self, tmp_path: Path) -> None:
        # Two pull requests can carry one head commit, each with its own lane
        # run. A BLOCK on one of them is not a BLOCK on this pull request.
        h = _Harness(
            tmp_path,
            {_OPUS: _listing(_check_run(7, _OPUS), _check_run(9, _OPUS, conclusion="success"))},
            {7: json.dumps([_block_note(_OPUS)])},
        )
        proc = h.run("enforce")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not h.held

    @pytest.mark.parametrize("conclusion", ["cancelled", "skipped"])
    def test_a_run_that_judged_nothing_beside_the_block_does_not_release(
        self, tmp_path: Path, conclusion: str
    ) -> None:
        # A head often carries a cancelled run of the lane beside the run that
        # did the review; it is not a second opinion.
        h = _Harness(
            tmp_path,
            {_OPUS: _listing(_check_run(6, _OPUS, conclusion=conclusion), _check_run(7, _OPUS))},
            {7: json.dumps([_block_note(_OPUS)])},
        )
        proc = h.run("enforce")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert h.held
        assert [c for c in h.calls if "/annotations" in c] == [
            f"api -X GET repos/{_REPO}/check-runs/7/annotations -f per_page=100"
        ]

    def test_a_failed_read_on_one_lane_does_not_hide_the_other_lanes_block(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(
            tmp_path,
            {_OPUS: _FAIL, _GPT: _listing(_check_run(8, _GPT))},
            {8: json.dumps([_block_note(_GPT)])},
        )
        proc = h.run("enforce")
        assert proc.returncode == 1
        assert h.held
        assert f"{_GPT} published BLOCK" in proc.stdout

    @pytest.mark.parametrize("mode", ["", "true", "on", "ENFORCE", "off"])
    def test_any_other_mode_releases_without_reading(self, tmp_path: Path, mode: str) -> None:
        h = _blocked(tmp_path)
        proc = h.run(mode)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not h.held
        assert h.calls == []

    @pytest.mark.parametrize("sha", ["", "deadbeef", _SHA.upper(), _SHA + "0"])
    def test_a_head_that_is_not_a_full_sha_releases_without_reading(
        self, tmp_path: Path, sha: str
    ) -> None:
        h = _blocked(tmp_path)
        proc = h.run("enforce", sha=sha)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not h.held
        assert h.calls == []


@needs_bash
@pytest.mark.usefixtures("jq_instrument")
class TestTheHoldSpendsFewReadsAndNoWrite:
    def test_no_verdict_costs_one_read_per_lane(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path)
        assert h.run("enforce").returncode == 0
        assert len(h.calls) == 2
        assert not [c for c in h.calls if "/annotations" in c]

    def test_the_worst_case_is_four_reads_and_every_one_is_a_get(self, tmp_path: Path) -> None:
        h = _Harness(
            tmp_path,
            {_OPUS: _listing(_check_run(7, _OPUS)), _GPT: _listing(_check_run(8, _GPT))},
            {7: json.dumps([_block_note(_OPUS)]), 8: json.dumps([_block_note(_GPT)])},
        )
        assert h.run("enforce").returncode == 1
        assert len(h.calls) == 4
        for call in h.calls:
            assert call.startswith("api -X GET "), call
        listings = [c for c in h.calls if "/annotations" not in c]
        for call in listings:
            assert f"repos/{_REPO}/commits/{_SHA}/check-runs" in call
            assert "filter=latest" in call

    def test_the_script_has_no_loop_no_wait_and_no_write(self) -> None:
        body = "\n".join(
            line
            for line in _SCRIPT.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert not re.search(r"\b(while|until|sleep)\b", body)
        assert not re.search(r"-X\s+(POST|PUT|PATCH|DELETE)|--method", body)
        assert len(re.findall(r"\bgh api\b", body)) == 2


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load((_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


_MIXED = {"only_backend": "false", "only_frontend": "false"}
_SAME_REPO = {"github.repository": _REPO, "github.event.pull_request.head.repo.full_name": _REPO}


class TestTheWorkflowEdge:
    @staticmethod
    def _heavy(ci: dict) -> list[str]:
        def needs(job: str) -> list[str]:
            raw = ci["jobs"][job].get("needs") or []
            return [raw] if isinstance(raw, str) else list(raw)

        return [job for job in ci["jobs"] if _LINT_GATE in needs(job)]

    def test_with_the_variable_unset_the_job_is_skipped_and_the_heavy_jobs_run(
        self, ci: dict
    ) -> None:
        results = _simulate_pull_request(ci, _MIXED, context=_SAME_REPO)
        assert results[_JOB] == "skipped"
        assert results[_LINT_GATE] == "success"
        heavy = self._heavy(ci)
        assert len(heavy) >= 10
        assert results["backend-test"] == "success"
        assert results["backend-test-windows"] == "success"

    @pytest.mark.parametrize("mode", ["shadow", "enforce"])
    def test_a_release_runs_the_heavy_jobs(self, ci: dict, mode: str) -> None:
        results = _simulate_pull_request(ci, _MIXED, context={**_SAME_REPO, _VARIABLE: mode})
        assert results[_JOB] == "success"
        assert results[_LINT_GATE] == "success"
        assert results["backend-test"] == "success"

    def test_a_hold_skips_every_heavy_job_and_coverage_gate_still_reports(self, ci: dict) -> None:
        results = _simulate_pull_request(
            ci,
            _MIXED,
            forced={_JOB: "failure"},
            context={
                **_SAME_REPO,
                _VARIABLE: "enforce",
                f"needs.{_JOB}.outputs.hold": "true",
            },
        )
        assert results[_JOB] == "failure"
        assert results[_LINT_GATE] == "skipped"
        ran = [job for job in self._heavy(ci) if results[job] == "success"]
        assert not ran, f"ran behind a hold: {sorted(ran)}"
        # The held head is never green: the hold job is red, and the one job
        # that reports for the skipped shards still runs.
        assert results["coverage-gate"] == "success"

    def test_the_job_failing_for_another_reason_releases_the_heavy_jobs(self, ci: dict) -> None:
        # A runner fault or a timeout leaves the `hold` output empty.
        results = _simulate_pull_request(
            ci, _MIXED, forced={_JOB: "failure"}, context={**_SAME_REPO, _VARIABLE: "enforce"}
        )
        assert results[_JOB] == "failure"
        assert results[_LINT_GATE] == "success"
        assert results["backend-test"] == "success"

    def test_the_gate_reads_the_hold_output_and_never_the_jobs_result(self, ci: dict) -> None:
        guard = str(ci["jobs"][_LINT_GATE]["if"])
        assert f"needs.{_JOB}.outputs.hold != 'true'" in guard
        assert f"needs.{_JOB}.result" not in guard
        job = ci["jobs"][_JOB]
        assert job["outputs"] == {"hold": "${{ steps.read.outputs.hold }}"}
        assert job["steps"][-1]["id"] == "read"

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
            pytest.param({"needs.backend-lint.result": "failure"}, id="lint-red"),
            pytest.param({"needs.await-fast-gate.result": "failure"}, id="fast-gate-red"),
        ],
    )
    def test_the_job_does_not_start_outside_its_scope(
        self, ci: dict, context: dict[str, str]
    ) -> None:
        values = {
            "github.event_name": "pull_request",
            **_SAME_REPO,
            _VARIABLE: "enforce",
            "needs.changes.result": "success",
            "needs.await-fast-gate.result": "success",
            "needs.backend-lint.result": "success",
            "needs.frontend-lint.result": "skipped",
        }
        guard = str(ci["jobs"][_JOB]["if"])
        assert _evaluate_guard(guard, values)
        assert not _evaluate_guard(guard, {**values, **context})

    def test_the_job_holds_read_scopes_only_and_runs_the_script(self, ci: dict) -> None:
        job = ci["jobs"][_JOB]
        assert job["name"] == "AI Review Hold"
        assert job["permissions"] == {"checks": "read", "contents": "read"}
        checkout, read = job["steps"]
        assert checkout["with"]["persist-credentials"] is False
        assert checkout["continue-on-error"] is True
        assert read["env"] == {
            "GH_TOKEN": "${{ github.token }}",
            "REPO": "${{ github.repository }}",
            "SHA": "${{ github.event.pull_request.head.sha }}",
            "MODE": "${{ vars.CI_AI_BLOCK_HOLD }}",
        }
        assert "bash .github/scripts/ai-block-hold.sh" in read["run"]

    def test_the_coverage_gate_names_the_hold_before_it_blames_a_shard(self, ci: dict) -> None:
        gate = ci["jobs"]["coverage-gate"]
        assert _JOB in gate["needs"]
        step = gate["steps"][0]
        assert step["env"]["AI_BLOCK_HOLD"] == f"${{{{ needs.{_JOB}.outputs.hold }}}}"
        body = step["run"]
        hold_at = body.index("an AI review lane published BLOCK for this head")
        assert body.index("surface detection (changes) never reported") < hold_at
        assert hold_at < body.index("frontend-test=$FE -- failing closed")
        assert hold_at < body.index("backend-test=$BE -- failing closed")


class TestTheScriptMatchesWhatTheLanesPublish:
    """The hold keys on two strings other workflows own; a rename there must fail here."""

    @staticmethod
    def _script_value(name: str) -> str:
        text = _SCRIPT.read_text(encoding="utf-8")
        match = re.search(rf"^{name}=(.+)$", text, flags=re.MULTILINE)
        assert match, name
        return match.group(1)

    def test_the_lane_names_are_the_review_workflows_check_names(self) -> None:
        lanes = re.findall(r'"([^"]+)"', self._script_value("LANES"))
        assert lanes == [_OPUS, _GPT]
        for workflow, job, lane in (
            ("claude-review.yml", "claude-review", _OPUS),
            ("codex-review.yml", "codex-review", _GPT),
        ):
            spec = yaml.safe_load((_WORKFLOWS / workflow).read_text(encoding="utf-8"))
            assert f"&& '{lane}' ||" in spec["jobs"][job]["name"], workflow

    def test_the_block_phrase_is_what_each_gate_step_prints_for_the_head(self) -> None:
        phrase = self._script_value("BLOCK_PHRASE").strip('"')
        for workflow, lane in (("claude-review.yml", _OPUS), ("codex-review.yml", _GPT)):
            model = lane.removesuffix(" Review")
            text = (_WORKFLOWS / workflow).read_text(encoding="utf-8")
            assert f'echo "::error::{model} {phrase} $HEAD ' in text, workflow
