"""The fork review lanes' finalize step waits out a spent installation rate
limit before it gives up on completing its check-run.

The lane opens its check-run ``in_progress`` and completes it in the last step.
A burst of fork pushes drains the hourly installation limit, and a 5 s retry
lands in the same spent window, so the row stayed ``in_progress`` until someone
re-ran the lane. ``complete()`` now asks ``GET /rate_limit`` when a PATCH names
a rate limit, waits for the reset within a cap, and retries.

The two functions are extracted from each workflow and run with ``gh``,
``sleep`` and the clock stubbed as shell functions, so nothing sleeps and
nothing reaches the network. The stub clock advances by every stubbed sleep,
and the PATCH keeps failing until that clock passes the reset.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    _bash,
    _child_env,
    _proc_log,
    _script_file,
    _shell_function,
    _step_script,
    _stub_path,
    _workflow,
)

from kiro_crew.testing.wait import default_timeout

LANES = (
    ("fork-gpt-review.yml", "Finalize check-run (fail closed)"),
    ("fork-opus-review.yml", "Finalize check-run (fail closed)"),
    ("fork-design-review.yml", "Finalize check-run"),
    ("fork-ux-review.yml", "Finalize check-run"),
    ("fork-first-principles-review.yml", "Finalize check-run"),
    ("fork-internal-content-scan.yml", "Finalize check-run (blocking)"),
)
RESOLVER = "Resolve and validate PR (authoritative from GitHub)"

#: The fixed epoch the stub clock starts at, so every delay is exact.
NOW = 1_800_000_000

RATE_LIMITED = "gh: API rate limit exceeded for installation ID 1. (HTTP 403)"
PATCH = "api --method PATCH repos/o/r/check-runs/7"
RATE_LIMIT_READ = "api -i rate_limit"


def _finalize(lane: str, step: str) -> str:
    return _step_script(_workflow(lane), step)


def _helpers(lane: str, step: str) -> str:
    script = _finalize(lane, step)
    return "\n".join(_shell_function(script, name) for name in ("gh_read_reset_delay", "complete"))


def _rate_limit_200(reset: int) -> str:
    body = json.dumps({"resources": {"core": {"limit": 5000, "remaining": 0, "reset": reset}}})
    return f"HTTP/2.0 200 OK\nContent-Type: application/json\n\n{body}\n"


class _Run:
    def __init__(self, result: subprocess.CompletedProcess[str], tmp_path: Path) -> None:
        self.rc = result.returncode
        self.output = (result.stdout or "") + (result.stderr or "")
        sleep_log = tmp_path / "sleep.log"
        self.sleeps = (
            [int(x) for x in sleep_log.read_text(encoding="utf-8").split()]
            if sleep_log.exists()
            else []
        )
        self.gh_calls = [
            " ".join(line.split()[:4])
            for line in (tmp_path / "gh.log").read_text(encoding="utf-8").splitlines()
        ]
        self.log = _proc_log(result)


def _run(
    tmp_path: Path,
    lane: str,
    step: str,
    *,
    reset: int,
    error: str = RATE_LIMITED,
    max_wait: str = "",
) -> _Run:
    """Run ``complete 7 failure title``; the PATCH fails with *error* until the
    stub clock reaches *reset*."""
    bash = _bash()
    if bash is None:
        pytest.skip("complete() is exercised where bash is available")
    (tmp_path / "rate_limit").write_text(_rate_limit_200(reset), encoding="utf-8")
    (tmp_path / "err").write_text(error, encoding="utf-8")
    (tmp_path / "slept").write_text("0", encoding="utf-8")
    # Shell FUNCTIONS, not PATH stubs: a function wins over any command lookup,
    # so neither a real `gh` nor a real sleep can reach the run.
    stubs = (
        'now() { echo $(( FAKE_NOW + $(cat "$STUB_DIR/slept") )); }\n'
        "gh() {\n"
        '  printf \'%s\\n\' "$*" >> "$STUB_DIR/gh.log"\n'
        '  if [ "$*" = "api -i rate_limit" ]; then cat "$STUB_DIR/rate_limit"; return 0; fi\n'
        '  if [ "$(now)" -lt "$RESET" ]; then cat "$STUB_DIR/err" >&2; return 1; fi\n'
        "  echo '{}'\n"
        "}\n"
        "sleep() {\n"
        '  printf \'%s\\n\' "$1" >> "$STUB_DIR/sleep.log"\n'
        '  echo $(( $(cat "$STUB_DIR/slept") + $1 )) > "$STUB_DIR/slept"\n'
        "}\n"
        "date() {\n"
        '  if [ "${1:-}" = "+%s" ]; then now; else command date "$@"; fi\n'
        "}\n"
    )
    driver = "\ncomplete 7 failure 'review incomplete'\n"
    env = {
        "PATH": _stub_path(tmp_path),
        "HOME": str(tmp_path),
        "STUB_DIR": str(tmp_path),
        "GH_TOKEN": "",
        "GH_CONFIG_DIR": str(tmp_path),
        "FAKE_NOW": str(NOW),
        "RESET": str(reset),
        "REPO": "o/r",
        "HEAD": "a" * 40,
        "summary": "s",
        "lane_text": "t",
    }
    if max_wait:
        env["FINALIZE_MAX_WAIT"] = max_wait
    script = _script_file(
        tmp_path,
        "set -uo pipefail\n" + stubs + _helpers(lane, step) + driver,
        "complete.sh",
    )
    result = subprocess.run(
        [bash, script],
        env=_child_env(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
        check=False,
    )
    return _Run(result, tmp_path)


@pytest.mark.parametrize(("lane", "step"), LANES)
def test_the_classifier_is_the_resolvers_copy(lane: str, step: str) -> None:
    resolver = _step_script(_workflow("fork-gpt-review.yml"), RESOLVER)
    assert _shell_function(_finalize(lane, step), "gh_read_reset_delay") == _shell_function(
        resolver, "gh_read_reset_delay"
    )


@pytest.mark.parametrize(("lane", "step"), LANES)
def test_the_wait_cap_ends_before_the_job_timeout(lane: str, step: str) -> None:
    doc = yaml.safe_load(_workflow(lane))
    (job,) = doc["jobs"].values()
    (finalize,) = [s for s in job["steps"] if s.get("name") == step]
    cap = int((finalize.get("env") or {}).get("FINALIZE_MAX_WAIT", 600))
    assert cap + 60 <= job["timeout-minutes"] * 60, lane


@pytest.mark.parametrize(("lane", "step"), LANES)
def test_a_spent_limit_waits_for_its_reset_then_completes(
    tmp_path: Path, lane: str, step: str
) -> None:
    run = _run(tmp_path, lane, step, reset=NOW + 300)
    assert run.rc == 0, run.log
    # reset - now + the 5 s margin, in one wait, and not a 5 s retry inside the window.
    assert run.sleeps == [305], run.log
    assert run.gh_calls == [PATCH, RATE_LIMIT_READ, PATCH], run.log


@pytest.mark.parametrize(("lane", "step"), LANES)
def test_a_reset_past_the_cap_gives_up_at_once_naming_it(
    tmp_path: Path, lane: str, step: str
) -> None:
    run = _run(tmp_path, lane, step, reset=NOW + 3000, max_wait="600")
    assert run.rc == 1, run.log
    assert run.sleeps == [], run.log
    assert "resets in 3005s, past the 600s this step waits" in run.output, run.log
    assert "could not complete check-run 7" in run.output, run.log


@pytest.mark.parametrize(("lane", "step"), LANES)
def test_a_failure_that_is_not_a_rate_limit_keeps_one_retry(
    tmp_path: Path, lane: str, step: str
) -> None:
    run = _run(tmp_path, lane, step, reset=NOW + 3000, error="gh: HTTP 502 Bad Gateway")
    assert run.rc == 1, run.log
    assert run.sleeps == [5, 5], run.log
    assert run.gh_calls == [PATCH, PATCH], run.log
