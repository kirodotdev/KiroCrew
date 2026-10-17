"""The Fast Gate barrier poll, run end to end against a stubbed ``gh`` and a fake clock.

``.github/scripts/await-fast-gate.sh`` is the whole poll ci.yml's ``await-fast-gate``
job runs. test_fast_gate_barrier.py pins the pieces of it -- the identity triple, the
jq selector, the conclusion arms -- and executes those two fragments in isolation.
This file executes the WHOLE script, driving its control flow through every
classification the barrier can reach:

* all green on the first poll                 -> exit 0, releases the matrix;
* a decided non-success conclusion            -> exit 1 at once, naming the run URL;
* a run that never appears                    -> exit 1 the instant the APPEAR budget
                                                 is crossed, not before;
* a run that appears, vanishes, then returns  -> tolerated: the vanish re-enters the
                                                 appear wait and the return concludes it;
* the API failing from the very first tick    -> exit 1 at the TOTAL budget, naming the
                                                 read error: a read that errors proves
                                                 only that the barrier could not look,
                                                 so it never spends the APPEAR budget;
* reads failing past the APPEAR window, then  -> exit 0: the failed reads were never
  a green run                                    counted as an absent run;
* a run seen but never decided                 -> exit 1 at the TOTAL budget (a pending
                                                 fork approval or a run stuck in
                                                 progress);
* a push's gate run evicted from its           -> the barrier proves it with reads only and
  concurrency group before it started             waits to be cancelled, never exiting 0;
                                                 as the probe it records the proof and
                                                 stops; a gate run that started, one with
                                                 no run entering the group at its
                                                 cancellation, a failed gate, an
                                                 unreadable proof and every pull request
                                                 still fail closed.

``.github/scripts/cancel-superseded-run.sh`` runs the barrier as that probe and holds
the workflow's one write. It is driven by the same harness: a proven supersession
makes exactly one POST, to this run's own ``/cancel``; every other outcome makes no
write and exits 0; a refused cancel exits 1; and anything but a push to main is
refused before any call.

Two seams make this run in zero wall-clock time and reproduce the production body
exactly when unset:

* ``AWAIT_FG_NOW`` -- a command printing a fake, advancing epoch, so the budgets are
  reached without waiting. Default: ``date +%s``.
* ``AWAIT_FG_SLEEP`` -- ``:`` (a no-op), so the loop spins. Default: ``sleep``.

``gh`` is a stub on PATH that replays a scripted sequence of ``/actions/workflows/
fast-gate.yml/runs?head_sha=`` responses, one per poll -- exactly how the real barrier
reads a run that is not there yet, then is. The supersession reads (the gate run's
jobs, the branch's run listing) and the cancel are answered from fixed files,
and every call is logged so a test can assert which ones were made.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "await-fast-gate.sh"

_BRANCH = "feature/mine"
_HEAD_REPO = "kirodotdev/KiroCrew"
_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_RUN_ID = "4242"

needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="runs the barrier poll under a POSIX bash with jq on PATH",
)


def _run(rid: int, status: str, conclusion: str | None, **extra: object) -> dict:
    return {
        "id": rid,
        "head_branch": _BRANCH,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/x/actions/runs/{rid}",
        "head_repository": {"full_name": _HEAD_REPO},
        **extra,
    }


def _page(*runs: dict) -> str:
    return json.dumps({"workflow_runs": list(runs)})


class _Harness:
    """A ``gh`` stub plus a fake clock, driving the script one poll at a time."""

    def __init__(
        self,
        tmp_path: Path,
        pages: list[str],
        *,
        tick: int = 30,
        jobs: str | None = None,
        group: str | None = None,
        cancel_fails: bool = False,
    ) -> None:
        self._dir = tmp_path
        # Each line of this file is one poll's `gh api ...` stdout, consumed in
        # order. The special token __FAIL__ makes the stub exit non-zero with no
        # output -- the real gh's behaviour when the API call errors, which the
        # script swallows with `2>/dev/null || true`.
        self._responses = tmp_path / "gh-responses"
        self._responses.write_text("".join(p + "\n" for p in pages), encoding="utf-8")
        self._cursor = tmp_path / "gh-cursor"
        self._cursor.write_text("0", encoding="utf-8")
        # The supersession reads answer from fixed files (absent = the call fails,
        # so an unexpected read is loud), and every call's argv is logged.
        self._calls = tmp_path / "gh-calls"
        self._calls.write_text("", encoding="utf-8")
        jobs_file = tmp_path / "gh-jobs"
        group_file = tmp_path / "gh-group"
        cancel_fail = tmp_path / "gh-cancel-fails"
        if jobs is not None:
            jobs_file.write_text(jobs, encoding="utf-8")
        if group is not None:
            group_file.write_text(group, encoding="utf-8")
        if cancel_fails:
            cancel_fail.write_text("", encoding="utf-8")

        # A `gh` that pops the next scripted response for a poll's listing. The
        # last line is reused once the list is exhausted, so a poll past the
        # script keeps the final state rather than erroring on an empty read.
        gh = tmp_path / "gh"
        gh.write_text(
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
                printf '%s\\n' "$*" >> {self._calls}
                fixed() {{
                  if [ ! -f "$1" ]; then echo "gh: unexpected call: $2" >&2; exit 1; fi
                  if [ "$(cat "$1")" = "__FAIL__" ]; then
                    echo "gh: simulated API failure" >&2
                    exit 1
                  fi
                  cat "$1"
                  exit 0
                }}
                case "$*" in
                  *"/cancel"*)
                    if [ -f {cancel_fail} ]; then
                      echo "gh: HTTP 409: Cannot cancel a workflow run" >&2
                      exit 1
                    fi
                    exit 0
                    ;;
                  *"/jobs"*) fixed {jobs_file} jobs ;;
                  *"head_sha="*) ;;
                  *"fast-gate.yml/runs"*) fixed {group_file} listing ;;
                  *) echo "gh: unexpected call" >&2; exit 1 ;;
                esac
                i="$(cat {self._cursor})"
                line="$(sed -n "$((i + 1))p" {self._responses})"
                next=$((i + 1))
                total="$(wc -l < {self._responses})"
                if [ "$next" -lt "$total" ]; then echo "$next" > {self._cursor}; fi
                if [ "$line" = "__FAIL__" ]; then
                  echo "gh: simulated API failure" >&2
                  exit 1
                fi
                printf '%s' "$line"
                """),
            encoding="utf-8",
        )
        gh.chmod(0o755)

        # A clock that advances by `tick` seconds on every call. The script reads
        # it twice per poll (loop-top `elapsed`, and `started` once at entry), and
        # a steady per-call increment is enough to walk elapsed past either budget
        # deterministically -- the test asserts on the exit, not on an exact
        # elapsed value.
        self._clock = tmp_path / "clock"
        self._clock.write_text("0", encoding="utf-8")
        now = tmp_path / "fake-now"
        now.write_text(
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
                t="$(cat {self._clock})"
                echo "$t"
                echo "$((t + {tick}))" > {self._clock}
                """),
            encoding="utf-8",
        )
        now.chmod(0o755)
        self._now = now

    def run(
        self,
        *,
        appear_budget: int = 180,
        total_budget: int = 720,
        event: str = "pull_request",
        branch: str = _BRANCH,
        script: Path = _SCRIPT,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("SUPERSEDED_FILE", None)
        env["PATH"] = f"{self._dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            GH_TOKEN="x",
            REPO=_HEAD_REPO,
            SHA=_SHA,
            EVENT=event,
            BRANCH=branch,
            HEAD_REPO=_HEAD_REPO,
            RUN_ID=_RUN_ID,
            APPEAR_BUDGET=str(appear_budget),
            TOTAL_BUDGET=str(total_budget),
            AWAIT_FG_NOW=str(self._now),
            AWAIT_FG_SLEEP=":",
            TMPDIR=str(self._dir),
        )
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            env=env,
            cwd=self._dir,
            **UTF8_TEXT,
        )

    @property
    def calls(self) -> list[str]:
        return self._calls.read_text(encoding="utf-8").splitlines()

    def cancels(self) -> list[str]:
        return [c for c in self.calls if "/cancel" in c]

    def writes(self) -> list[str]:
        return [c for c in self.calls if "--method GET" not in c]


@needs_bash
class TestTheBarrierPollClassifiesEveryOutcome:
    def test_all_green_on_the_first_poll_releases_the_matrix(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, [_page(_run(10, "completed", "success"))])
        proc = h.run()
        assert proc.returncode == 0, proc.stderr
        assert "Fast Gate passed" in proc.stdout

    @pytest.mark.parametrize(
        "conclusion",
        ["failure", "cancelled", "timed_out", "startup_failure", "neutral", "skipped"],
    )
    def test_a_decided_non_success_fails_at_once_with_the_url(
        self, tmp_path: Path, conclusion: str
    ) -> None:
        h = _Harness(tmp_path, [_page(_run(11, "completed", conclusion))])
        proc = h.run()
        assert proc.returncode == 1, proc.stdout
        assert "::error::" in proc.stdout
        assert f"concluded '{conclusion}'" in proc.stdout
        assert "https://github.com/x/actions/runs/11" in proc.stdout

    def test_a_run_that_never_appears_fails_at_the_appear_window(self, tmp_path: Path) -> None:
        # Empty page every poll. With a 30s tick and a 90s appear budget, the run
        # is declared missing on the poll whose elapsed first reaches 90 -- and
        # the TOTAL budget is never the reason, so its message must not be the one.
        h = _Harness(tmp_path, [_page()], tick=30)
        proc = h.run(appear_budget=90, total_budget=720)
        assert proc.returncode == 1, proc.stdout
        assert "No Fast Gate run found" in proc.stdout
        assert "Failing closed rather than starting the matrix unverified" not in proc.stdout

    def test_a_run_that_appears_then_vanishes_then_returns_recovers(self, tmp_path: Path) -> None:
        # seen once (running), gone (an empty page -- a listing blip), back and
        # green. The vanish must NOT fail: it re-enters the appear wait, which the
        # generous appear budget tolerates, and the return concludes the poll.
        h = _Harness(
            tmp_path,
            [
                _page(_run(12, "in_progress", None)),
                _page(),
                _page(_run(12, "completed", "success")),
            ],
            tick=10,
        )
        proc = h.run(appear_budget=180, total_budget=720)
        assert proc.returncode == 0, proc.stderr
        assert "Fast Gate passed" in proc.stdout

    def test_the_api_failing_every_tick_fails_closed_at_the_total_budget(
        self, tmp_path: Path
    ) -> None:
        # Every poll's gh call errors. A failed read proves only that the barrier
        # could not look, so it must not be reported as "No Fast Gate run found"
        # and must not spend the APPEAR budget. It still fails closed -- at the
        # TOTAL budget, naming the read error and saying the code was not judged.
        h = _Harness(tmp_path, ["__FAIL__"], tick=60)
        proc = h.run(appear_budget=120, total_budget=600)
        assert proc.returncode == 1, proc.stdout
        assert "No Fast Gate run found" not in proc.stdout
        assert "Fast Gate listing unreadable" in proc.stdout
        assert "simulated API failure" in proc.stdout

    def test_a_read_failure_past_the_appear_window_does_not_fail_a_listed_run(
        self, tmp_path: Path
    ) -> None:
        # The listing errors for longer than the APPEAR budget (a rate-limit
        # burst), then answers with a green run. Treating the failed reads as an
        # absent run would fail this commit at the APPEAR window although its
        # Fast Gate run existed the whole time.
        h = _Harness(
            tmp_path,
            ["__FAIL__"] * 6 + [_page(_run(15, "completed", "success"))],
            tick=30,
        )
        proc = h.run(appear_budget=90, total_budget=720)
        assert proc.returncode == 0, proc.stdout
        assert "Fast Gate passed" in proc.stdout
        assert "No Fast Gate run found" not in proc.stdout

    def test_a_pending_fork_approval_keeps_polling_until_the_total_budget(
        self, tmp_path: Path
    ) -> None:
        # completed/action_required is a fork run awaiting maintainer approval:
        # pending, not decided. It must neither release nor fail at once -- it
        # polls until the run is approved (here it never is, so the TOTAL budget
        # ends it, and the message names the approval it was waiting on).
        h = _Harness(
            tmp_path,
            [_page(_run(13, "completed", "action_required"))],
            tick=200,
        )
        proc = h.run(appear_budget=180, total_budget=300)
        assert proc.returncode == 1, proc.stdout
        assert "awaiting maintainer approval" in proc.stdout
        assert "concluded" not in proc.stdout

    def test_a_run_that_stays_in_progress_fails_at_the_total_budget(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, [_page(_run(14, "in_progress", None))], tick=200)
        proc = h.run(appear_budget=180, total_budget=300)
        assert proc.returncode == 1, proc.stdout
        assert "Failing closed rather than starting the matrix unverified" in proc.stdout
        assert "in_progress" in proc.stdout


# Shaped on a real eviction: the gate run for this commit waited in Fast Gate's
# group behind an older run that held the running slot, and the next push's run
# entered the group one second before this one was cancelled.
_CREATED = "2026-10-14T15:16:53Z"
_CANCELLED = "2026-10-14T15:26:18Z"
_EVICTOR_START = "2026-10-14T15:26:17Z"
_OLDER_START = "2026-10-14T15:09:49Z"
_LONG_AFTER = "2026-10-14T15:40:00Z"
_NO_JOBS = json.dumps({"total_count": 0, "jobs": []})
_RAN_JOBS = json.dumps({"total_count": 15, "jobs": [{"id": 1}]})
_MAIN = "main"
_CANCEL_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "cancel-superseded-run.sh"


def _gate(conclusion: str = "cancelled") -> dict:
    return _run(
        20,
        "completed",
        conclusion,
        event="push",
        head_branch=_MAIN,
        run_attempt=1,
        head_sha=_SHA,
        created_at=_CREATED,
        updated_at=_CANCELLED,
        run_started_at=_CREATED,
    )


def _other(rid: int, started: str, sha: str) -> dict:
    return _run(
        rid,
        "queued",
        None,
        event="push",
        head_branch=_MAIN,
        run_attempt=1,
        head_sha=sha,
        created_at=started,
        updated_at=started,
        run_started_at=started,
    )


_OLDER = _other(10, _OLDER_START, "1" * 40)
_EVICTOR = _other(30, _EVICTOR_START, "2" * 40)
_LATER = _other(31, "2026-10-14T15:26:37Z", "4" * 40)


@needs_bash
class TestASupersededPushGateIsCancelledNotFailed:
    """The barrier proves the eviction, then waits read-only to be cancelled."""

    def test_a_push_gate_evicted_before_it_started_waits_to_be_cancelled(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _OLDER, _EVICTOR, _LATER),
        )
        proc = h.run(event="push", branch=_MAIN)
        # The barrier only reads: the cancel is the cancel job's, not this one's.
        assert h.writes() == [], h.calls
        assert "evicted from its concurrency group" in proc.stdout
        assert "Cancel Superseded Run" in proc.stdout
        # The run named is the one that started closest to the cancellation, not
        # a later push that also falls inside the slack window.
        assert "https://github.com/x/actions/runs/30" in proc.stdout
        assert "https://github.com/x/actions/runs/31" not in proc.stdout
        # On a runner the cancel job's cancel ends this job mid-wait. If it never
        # lands, the barrier still fails closed: it never releases the matrix.
        assert proc.returncode == 1, proc.stdout
        assert "Fast Gate passed" not in proc.stdout
        assert "was not cancelled within 150s" in proc.stdout

    def test_the_probe_records_the_proof_and_stops_without_waiting(self, tmp_path: Path) -> None:
        proof = tmp_path / "proof"
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _OLDER, _EVICTOR, _LATER),
        )
        proc = h.run(event="push", branch=_MAIN, extra_env={"SUPERSEDED_FILE": str(proof)})
        assert proc.returncode == 1, proc.stdout
        assert h.writes() == [], h.calls
        recorded = proof.read_text(encoding="utf-8")
        assert "https://github.com/x/actions/runs/20 never started" in recorded
        assert "https://github.com/x/actions/runs/30" in recorded
        assert "was not cancelled within" not in proc.stdout
        assert "Fast Gate passed" not in proc.stdout

    @pytest.mark.parametrize("conclusion", ["success", "failure"])
    def test_the_probe_switch_never_changes_a_decided_gate(
        self, tmp_path: Path, conclusion: str
    ) -> None:
        proof = tmp_path / "proof"
        h = _Harness(tmp_path, [_page(_gate(conclusion))], jobs=_NO_JOBS)
        proc = h.run(event="push", branch=_MAIN, extra_env={"SUPERSEDED_FILE": str(proof)})
        assert proc.returncode == (0 if conclusion == "success" else 1), proc.stdout
        assert not proof.exists()

    def test_a_push_gate_that_started_and_was_cancelled_still_fails_closed(
        self, tmp_path: Path
    ) -> None:
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_RAN_JOBS,
            group=_page(_gate(), _OLDER, _EVICTOR),
        )
        proc = h.run(event="push", branch=_MAIN)
        assert proc.returncode == 1, proc.stdout
        assert "concluded 'cancelled'" in proc.stdout
        assert h.cancels() == [], h.calls

    def test_a_push_gate_cancelled_with_no_run_entering_its_group_fails_closed(
        self, tmp_path: Path
    ) -> None:
        # Never started, but no run arrived while it waited: the older run started
        # before it was created and the other one long after it was cancelled. That
        # is a cancel of this commit's gate, not an eviction.
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _OLDER, _other(40, _LONG_AFTER, "3" * 40)),
        )
        proc = h.run(event="push", branch=_MAIN)
        assert proc.returncode == 1, proc.stdout
        assert "concluded 'cancelled'" in proc.stdout
        assert h.cancels() == [], h.calls

    def test_a_failed_push_gate_fails_closed_without_reading_further(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, [_page(_gate("failure"))], jobs=_NO_JOBS)
        proc = h.run(event="push", branch=_MAIN)
        assert proc.returncode == 1, proc.stdout
        assert "concluded 'failure'" in proc.stdout
        assert len(h.calls) == 1 and "head_sha=" in h.calls[0], h.calls

    def test_a_pull_request_cancel_is_unchanged(self, tmp_path: Path) -> None:
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _OLDER, _EVICTOR),
        )
        proc = h.run(event="pull_request", branch=_MAIN)
        assert proc.returncode == 1, proc.stdout
        assert "concluded 'cancelled'" in proc.stdout
        assert len(h.calls) == 1 and "head_sha=" in h.calls[0], h.calls

    @pytest.mark.parametrize("unreadable", ["jobs", "group"])
    def test_an_unreadable_proof_fails_closed(self, tmp_path: Path, unreadable: str) -> None:
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs="__FAIL__" if unreadable == "jobs" else _NO_JOBS,
            group="__FAIL__" if unreadable == "group" else _page(_gate(), _EVICTOR),
        )
        proc = h.run(event="push", branch=_MAIN)
        assert proc.returncode == 1, proc.stdout
        assert "concluded 'cancelled'" in proc.stdout
        assert "not treated as superseded" in proc.stdout
        assert h.cancels() == [], h.calls

    def test_the_barrier_never_cancels_even_when_the_proof_holds(self, tmp_path: Path) -> None:
        # The barrier's token is read-only on every event, so the proof only ever
        # makes it wait; a refused or absent cancel cannot change its outcome.
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _EVICTOR),
            cancel_fails=True,
        )
        proc = h.run(event="push", branch=_MAIN)
        assert h.cancels() == [], h.calls
        assert proc.returncode == 1, proc.stdout


def _probe_lines_are_not_annotations(stdout: str) -> None:
    # The probe's `::error::`/`::warning::` lines belong to Await Fast Gate; in the
    # cancel job they are shown indented, so the runner does not raise them twice.
    for line in stdout.splitlines():
        if "probe|" in line:
            assert line.startswith("  probe| "), line


@needs_bash
class TestTheCancelJobCancelsOnlyAProvenSupersession:
    """cancel-superseded-run.sh: the workflow's one write, behind the barrier's proof."""

    def test_a_proven_supersession_cancels_this_run_once(self, tmp_path: Path) -> None:
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _OLDER, _EVICTOR, _LATER),
        )
        proc = h.run(event="push", branch=_MAIN, script=_CANCEL_SCRIPT)
        assert h.writes() == [
            f"api --method POST repos/{_HEAD_REPO}/actions/runs/{_RUN_ID}/cancel"
        ], h.calls
        assert "::warning::Fast Gate run https://github.com/x/actions/runs/20" in proc.stdout
        assert "https://github.com/x/actions/runs/30" in proc.stdout
        # On a runner the cancel ends this job. If it never lands, the job says so
        # and exits non-zero; it never reports success after asking.
        assert proc.returncode == 1, proc.stdout
        assert "still running after 90s" in proc.stdout
        _probe_lines_are_not_annotations(proc.stdout)
        # Its working files are gone.
        assert not list(tmp_path.glob("cancel-superseded.*")), list(tmp_path.iterdir())

    @pytest.mark.parametrize(
        "case",
        ["passed", "failed", "started-then-cancelled", "nothing-entered", "unreadable"],
    )
    def test_anything_else_cancels_nothing_and_leaves_the_verdict_to_the_barrier(
        self, tmp_path: Path, case: str
    ) -> None:
        gate = {
            "passed": _gate("success"),
            "failed": _gate("failure"),
        }.get(case, _gate())
        jobs = _RAN_JOBS if case == "started-then-cancelled" else _NO_JOBS
        group = {
            "nothing-entered": _page(_gate(), _OLDER, _other(40, _LONG_AFTER, "3" * 40)),
            "unreadable": "__FAIL__",
        }.get(case, _page(_gate(), _OLDER, _EVICTOR))
        h = _Harness(tmp_path, [_page(gate)], jobs=jobs, group=group)
        proc = h.run(event="push", branch=_MAIN, script=_CANCEL_SCRIPT)
        assert h.writes() == [], h.calls
        assert proc.returncode == 0, proc.stdout
        assert "nothing to cancel" in proc.stdout
        assert "Await Fast Gate reports the verdict" in proc.stdout
        _probe_lines_are_not_annotations(proc.stdout)
        if case == "failed":
            assert "  probe| ::error::Fast Gate concluded 'failure'" in proc.stdout

    def test_a_refused_cancel_fails_and_names_the_refusal(self, tmp_path: Path) -> None:
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _EVICTOR),
            cancel_fails=True,
        )
        proc = h.run(event="push", branch=_MAIN, script=_CANCEL_SCRIPT)
        assert len(h.cancels()) == 1, h.calls
        assert proc.returncode == 1, proc.stdout
        assert "Could not cancel this run" in proc.stdout
        assert "Cannot cancel a workflow run" in proc.stdout

    @pytest.mark.parametrize(
        ("event", "branch", "run_id"),
        [
            ("pull_request", _MAIN, _RUN_ID),
            ("merge_group", _MAIN, _RUN_ID),
            ("push", "release/1.0", _RUN_ID),
            ("push", _MAIN, ""),
        ],
    )
    def test_it_refuses_anything_but_a_push_to_main_before_any_call(
        self, tmp_path: Path, event: str, branch: str, run_id: str
    ) -> None:
        h = _Harness(
            tmp_path,
            [_page(_gate())],
            jobs=_NO_JOBS,
            group=_page(_gate(), _EVICTOR),
        )
        proc = h.run(
            event=event, branch=branch, script=_CANCEL_SCRIPT, extra_env={"RUN_ID": run_id}
        )
        assert proc.returncode == 1, proc.stdout
        assert "nothing was cancelled" in proc.stdout
        assert h.calls == [], h.calls


@needs_bash
class TestTheProductionDefaultsAreIntact:
    def test_the_budgets_default_to_the_committed_values(self) -> None:
        # The seams must not have changed the production numbers: 180 / 720 are
        # what the inline body carried, and test_fast_gate_barrier pins them in
        # the workflow's env-free call, so re-pin them at the script's source.
        text = _SCRIPT.read_text(encoding="utf-8")
        assert 'APPEAR_BUDGET="${APPEAR_BUDGET:-180}"' in text
        assert 'TOTAL_BUDGET="${TOTAL_BUDGET:-720}"' in text

    def test_the_clock_and_sleep_default_to_the_real_tools(self) -> None:
        text = _SCRIPT.read_text(encoding="utf-8")
        assert "${AWAIT_FG_NOW:-date +%s}" in text
        assert "${AWAIT_FG_SLEEP:-sleep}" in text
