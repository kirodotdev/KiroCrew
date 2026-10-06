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
                                                 progress).

Two seams make this run in zero wall-clock time and reproduce the production body
exactly when unset:

* ``AWAIT_FG_NOW`` -- a command printing a fake, advancing epoch, so the budgets are
  reached without waiting. Default: ``date +%s``.
* ``AWAIT_FG_SLEEP`` -- ``:`` (a no-op), so the loop spins. Default: ``sleep``.

``gh`` is a stub on PATH that replays a scripted sequence of ``/actions/workflows/
fast-gate.yml/runs`` responses, one per poll -- exactly how the real barrier reads a
run that is not there yet, then is.
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

needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="runs the barrier poll under a POSIX bash with jq on PATH",
)


def _run(rid: int, status: str, conclusion: str | None) -> dict:
    return {
        "id": rid,
        "head_branch": _BRANCH,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/x/actions/runs/{rid}",
        "head_repository": {"full_name": _HEAD_REPO},
    }


def _page(*runs: dict) -> str:
    return json.dumps({"workflow_runs": list(runs)})


class _Harness:
    """A ``gh`` stub plus a fake clock, driving the script one poll at a time."""

    def __init__(self, tmp_path: Path, pages: list[str], *, tick: int = 30) -> None:
        self._dir = tmp_path
        # Each line of this file is one poll's `gh api ...` stdout, consumed in
        # order. The special token __FAIL__ makes the stub exit non-zero with no
        # output -- the real gh's behaviour when the API call errors, which the
        # script swallows with `2>/dev/null || true`.
        self._responses = tmp_path / "gh-responses"
        self._responses.write_text("".join(p + "\n" for p in pages), encoding="utf-8")
        self._cursor = tmp_path / "gh-cursor"
        self._cursor.write_text("0", encoding="utf-8")

        # A `gh` that pops the next scripted response. The last line is reused
        # once the list is exhausted, so a poll past the script keeps the final
        # state rather than erroring on an empty read.
        gh = tmp_path / "gh"
        gh.write_text(
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
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
        self, *, appear_budget: int = 180, total_budget: int = 720
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = f"{self._dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            GH_TOKEN="x",
            REPO=_HEAD_REPO,
            SHA=_SHA,
            EVENT="pull_request",
            BRANCH=_BRANCH,
            HEAD_REPO=_HEAD_REPO,
            APPEAR_BUDGET=str(appear_budget),
            TOTAL_BUDGET=str(total_budget),
            AWAIT_FG_NOW=str(self._now),
            AWAIT_FG_SLEEP=":",
        )
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            env=env,
            cwd=self._dir,
            **UTF8_TEXT,
        )


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
