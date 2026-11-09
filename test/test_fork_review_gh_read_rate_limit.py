"""The fork review lanes' resolver retry (``gh_read``) waits out a spent
installation rate limit instead of retrying inside the same window.

The primary installation limit resets on the hour, so a seconds-long backoff
lands every retry in the spent window and the lane publishes "could not run".
``gh_read`` asks ``GET /rate_limit`` when a failure names a rate limit, waits
for the reset within a cap, and fails with a message naming the reset past it.
Every other failure keeps the plain backoff.

The two functions are extracted from each workflow and run with ``gh``,
``sleep`` and the clock stubbed as shell functions, so nothing sleeps and
nothing reaches the network.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
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

LANES = ("fork-gpt-review.yml", "fork-opus-review.yml", "fork-security-scope-review.yml")
STEP = "Resolve and validate PR (authoritative from GitHub)"
FUNCTIONS = ("gh_read_reset_delay", "gh_read")

#: The fixed epoch the stubbed `date +%s` returns, so every delay is exact.
NOW = 1_800_000_000

RATE_LIMITED = "gh: API rate limit exceeded for installation ID 1. (HTTP 403)"


def _helpers(lane: str) -> str:
    script = _step_script(_workflow(lane), STEP)
    return "\n".join(_shell_function(script, name) for name in FUNCTIONS)


def _rate_limit_200(
    core: int, reset: int, graphql: int = 4000, graphql_reset: int | None = None
) -> str:
    body = json.dumps(
        {
            "resources": {
                "core": {"limit": 5000, "remaining": core, "reset": reset},
                "graphql": {
                    "limit": 5000,
                    "remaining": graphql,
                    "reset": reset if graphql_reset is None else graphql_reset,
                },
            }
        }
    )
    return f"HTTP/2.0 200 OK\nContent-Type: application/json\n\n{body}\n"


class _Run:
    def __init__(self, result: subprocess.CompletedProcess[str], tmp_path: Path) -> None:
        self.rc = result.returncode
        self.stderr = result.stderr or ""
        self.out = (tmp_path / "out").read_text(encoding="utf-8")
        sleep_log = tmp_path / "sleep.log"
        self.sleeps = (
            [int(x) for x in sleep_log.read_text(encoding="utf-8").split()]
            if sleep_log.exists()
            else []
        )
        self.gh_calls = (tmp_path / "gh.log").read_text(encoding="utf-8").splitlines()
        self.log = _proc_log(result)


def _run(
    tmp_path: Path,
    lane: str,
    replies: list[tuple[int, str, str]],
    rate_limit: str = "",
    max_wait: str = "",
) -> _Run:
    """Run `gh_read gh api repos/o/r/pulls/1` against scripted replies.

    Each reply is ``(rc, stderr, stdout)`` for one query call; the last repeats.
    ``gh api -i rate_limit`` always answers with ``rate_limit``.
    """
    bash = _bash()
    if bash is None:
        pytest.skip("gh_read is exercised where bash is available")
    for i, (rc, err, out) in enumerate(replies, start=1):
        (tmp_path / f"reply{i}.rc").write_text(str(rc), encoding="utf-8")
        (tmp_path / f"reply{i}.err").write_text(err, encoding="utf-8")
        (tmp_path / f"reply{i}.out").write_text(out, encoding="utf-8")
    (tmp_path / "rate_limit").write_text(rate_limit, encoding="utf-8")
    (tmp_path / "count").write_text("0", encoding="utf-8")
    # Shell FUNCTIONS, not PATH stubs: a function wins over any command lookup,
    # so neither a real `gh` nor a real sleep can reach the run.
    stubs = (
        "gh() {\n"
        '  printf \'%s\\n\' "$*" >> "$STUB_DIR/gh.log"\n'
        '  if [ "$*" = "api -i rate_limit" ]; then cat "$STUB_DIR/rate_limit"; return 0; fi\n'
        '  local n; n=$(( $(cat "$STUB_DIR/count") + 1 ))\n'
        '  echo "$n" > "$STUB_DIR/count"\n'
        '  if [ "$n" -gt "$REPLIES" ]; then n="$REPLIES"; fi\n'
        '  cat "$STUB_DIR/reply$n.err" >&2\n'
        '  cat "$STUB_DIR/reply$n.out"\n'
        '  return "$(cat "$STUB_DIR/reply$n.rc")"\n'
        "}\n"
        "sleep() {\n"
        '  printf \'%s\\n\' "$1" >> "$STUB_DIR/sleep.log"\n'
        "}\n"
        "date() {\n"
        '  if [ "${1:-}" = "+%s" ]; then echo "$FAKE_NOW"; else command date "$@"; fi\n'
        "}\n"
    )
    driver = (
        '\nout="$(gh_read gh api repos/o/r/pulls/1)"\n'
        "rc=$?\n"
        'printf \'%s\' "$out" > "$STUB_DIR/out"\n'
        'exit "$rc"\n'
    )
    env = {
        "PATH": _stub_path(tmp_path),
        "HOME": str(tmp_path),
        "STUB_DIR": str(tmp_path),
        "GH_TOKEN": "",
        "GH_CONFIG_DIR": str(tmp_path),
        "REPLIES": str(len(replies)),
        "FAKE_NOW": str(NOW),
    }
    if max_wait:
        env["GH_READ_MAX_WAIT"] = max_wait
    script = _script_file(
        tmp_path, "set -uo pipefail\n" + stubs + _helpers(lane) + driver, "gh_read.sh"
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


QUERY = "api repos/o/r/pulls/1"
RATE_LIMIT_READ = "api -i rate_limit"


def test_every_lane_carries_the_same_helpers() -> None:
    first, *rest = (_helpers(lane) for lane in LANES)
    assert all(other == first for other in rest)


@pytest.mark.parametrize("lane", LANES)
def test_the_wait_cap_ends_before_the_step_or_job_timeout(lane: str) -> None:
    doc = yaml.safe_load(_workflow(lane))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") != STEP:
                continue
            cap = int((step.get("env") or {}).get("GH_READ_MAX_WAIT", 600))
            limit = step.get("timeout-minutes", job["timeout-minutes"])
            # Room for the queries themselves, so gh_read's named failure wins.
            assert cap + 60 <= limit * 60, lane
            return
    raise AssertionError(f"{lane}: no {STEP!r} step")


@pytest.mark.parametrize("lane", LANES)
def test_a_spent_limit_waits_for_its_reset_then_succeeds(tmp_path: Path, lane: str) -> None:
    run = _run(
        tmp_path,
        lane,
        [(1, RATE_LIMITED, ""), (0, "", "42")],
        rate_limit=_rate_limit_200(core=0, reset=NOW + 60),
    )
    assert run.rc == 0, run.log
    assert run.out == "42"
    # reset - now + the 5 s margin, and no seconds-long backoff besides.
    assert run.sleeps == [65], run.log
    assert run.gh_calls == [QUERY, RATE_LIMIT_READ, QUERY]


def test_a_reset_header_on_a_refused_rate_limit_read_is_the_wait(tmp_path: Path) -> None:
    refused = (
        "HTTP/2.0 403 Forbidden\r\nX-Ratelimit-Remaining: 0\r\n"
        f"X-Ratelimit-Reset: {NOW + 90}\r\n\r\n{{}}\r\n"
    )
    run = _run(tmp_path, LANES[0], [(1, RATE_LIMITED, ""), (0, "", "ok")], rate_limit=refused)
    assert run.rc == 0, run.log
    assert run.sleeps == [95], run.log


def test_a_reset_past_the_cap_fails_at_once_naming_the_reset(tmp_path: Path) -> None:
    reset = NOW + 3600
    run = _run(
        tmp_path,
        LANES[0],
        [(1, RATE_LIMITED, "")],
        rate_limit=_rate_limit_200(core=0, reset=reset),
    )
    assert run.rc == 1, run.log
    assert run.sleeps == [], run.log
    assert run.gh_calls == [QUERY, RATE_LIMIT_READ]
    when = datetime.fromtimestamp(reset, tz=timezone.utc).strftime("%H:%M:%SZ")
    assert f"::error::The installation API rate limit is spent and resets at {when}" in run.stderr


def test_waits_add_up_against_the_cap(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        LANES[0],
        [(1, RATE_LIMITED, "")],
        rate_limit=_rate_limit_200(core=0, reset=NOW + 60),
        max_wait="150",
    )
    # Two 65 s waits fit in 150 s; the third would not, so it fails closed.
    assert run.rc == 1, run.log
    assert run.sleeps == [65, 65], run.log
    assert "past the 150s this query waits" in run.stderr


def test_a_failure_that_is_not_a_rate_limit_keeps_the_backoff(tmp_path: Path) -> None:
    run = _run(tmp_path, LANES[0], [(1, "gh: Bad Gateway (HTTP 502)", "")])
    assert run.rc == 1, run.log
    assert run.sleeps == [5, 10, 15, 20, 25], run.log
    assert run.gh_calls == [QUERY] * 6
    assert "rate limit is spent" not in run.stderr


def test_a_rate_limit_github_does_not_report_as_spent_keeps_the_backoff(
    tmp_path: Path,
) -> None:
    secondary = "gh: You have exceeded a secondary rate limit. (HTTP 403)"
    run = _run(
        tmp_path,
        LANES[0],
        [(1, secondary, ""), (0, "", "7")],
        rate_limit=_rate_limit_200(core=12, reset=NOW + 60),
    )
    assert run.rc == 0, run.log
    assert run.out == "7"
    assert run.sleeps == [5], run.log
    assert run.gh_calls == [QUERY, RATE_LIMIT_READ, QUERY]


def test_only_the_rest_bucket_sets_the_wait(tmp_path: Path) -> None:
    # The resolver's queries are REST: a later GraphQL reset must not stretch
    # the wait past the cap when `core` resets within it.
    run = _run(
        tmp_path,
        LANES[0],
        [(1, RATE_LIMITED, ""), (0, "", "42")],
        rate_limit=_rate_limit_200(core=0, reset=NOW + 60, graphql=0, graphql_reset=NOW + 3600),
    )
    assert run.rc == 0, run.log
    assert run.sleeps == [65], run.log


def test_a_spent_graphql_bucket_alone_keeps_the_backoff(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        LANES[0],
        [(1, RATE_LIMITED, ""), (0, "", "42")],
        rate_limit=_rate_limit_200(core=12, reset=NOW + 60, graphql=0),
    )
    assert run.rc == 0, run.log
    assert run.sleeps == [5], run.log
