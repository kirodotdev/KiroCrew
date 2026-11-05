"""The fork Opus 5.5 Review lane's bounded retry of validation after an
installation API rate limit.

A burst of fork pushes drains the shared installation token, and validation
then dies inside claude-code-action before the model runs. The lane classifies
that failure from GitHub's own rate-limit answer (``GET /rate_limit``, or the
``Retry-After`` / ``X-RateLimit-*`` headers of a refused read), waits for the
reset within a cap, and retries ONCE. Everything else still fails closed.

The classifier step is executed here with stubbed ``gh`` and ``sleep``; the
wiring around it is read from the workflow YAML.
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
    _step_script,
    _stub_path,
    _workflow,
)

LANE = "fork-opus-review.yml"
CLASSIFY = "Classify a validation failure (installation rate limit only)"
VALIDATE = "Opus 5.5 validation"
RETRY = "Opus 5.5 validation (retry after the rate-limit reset)"
FAIL_CLOSED = "Fail closed when validation did not complete"
SCRUB = "Scrub persisted credential files before the model runs"

#: The fixed epoch the stubbed `date +%s` returns, so every delay is exact.
NOW = 1_800_000_000


def _steps() -> list[dict]:
    doc = yaml.safe_load(_workflow(LANE))
    (job,) = doc["jobs"].values()
    return job["steps"]


def _step(name: str) -> dict:
    matches = [s for s in _steps() if s.get("name") == name]
    assert len(matches) == 1, f"{name!r}: found {len(matches)}"
    return matches[0]


def _rate_limit_body(core: int = 4000, graphql: int = 4000, reset: int | None = None) -> str:
    reset = reset if reset is not None else NOW + 600
    return json.dumps(
        {
            "resources": {
                "core": {"limit": 5000, "remaining": core, "reset": reset},
                "graphql": {"limit": 5000, "remaining": graphql, "reset": reset},
            }
        }
    )


def _response(status: str, headers: dict[str, str], body: str, crlf: bool = False) -> str:
    lines = [f"HTTP/2.0 {status}"] + [f"{k}: {v}" for k, v in headers.items()] + ["", body]
    return ("\r\n" if crlf else "\n").join(lines) + "\n"


class _Run:
    def __init__(self, result: subprocess.CompletedProcess[str], tmp_path: Path) -> None:
        self.result = result
        out = (tmp_path / "github_output").read_text(encoding="utf-8")
        self.outputs = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        sleep_log = tmp_path / "sleep.log"
        self.sleeps = (
            [int(x) for x in sleep_log.read_text(encoding="utf-8").split()]
            if sleep_log.exists()
            else []
        )
        gh_log = tmp_path / "gh.log"
        self.gh_calls = gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else []
        self.log = _proc_log(result)


def _classify(
    tmp_path: Path,
    response: str | None,
    gh_rc: int = 0,
    exec_file: str = "",
    max_wait: str = "1200",
) -> _Run:
    bash = _bash()
    if bash is None:
        pytest.skip("the classifier step is exercised where bash is available")
    (tmp_path / "gh_response").write_bytes((response or "").encode("utf-8"))
    (tmp_path / "github_output").write_text("", encoding="utf-8")
    # `gh`, `sleep` and the clock are shell FUNCTIONS prepended to the step, not
    # PATH stubs: a function wins over any command lookup, so neither a Git Bash
    # PATH quirk nor a real sleep can reach the run, and `date +%s` returns the
    # fixed NOW every expected delay is computed from.
    stubs = (
        "gh() {\n"
        '  printf \'%s\\n\' "$*" >> "$STUB_DIR/gh.log"\n'
        '  cat "$STUB_DIR/gh_response"\n'
        "  return $FAKE_GH_RC\n"
        "}\n"
        "sleep() {\n"
        '  printf \'%s\\n\' "$1" >> "$STUB_DIR/sleep.log"\n'
        "}\n"
        "date() {\n"
        '  if [ "${1:-}" = "+%s" ]; then echo "$FAKE_NOW"; else echo "00:00:00Z"; fi\n'
        "}\n"
    )
    env = {
        "PATH": _stub_path(tmp_path),
        "HOME": str(tmp_path),
        "STUB_DIR": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        # Starve any real gh of credentials; the function above shadows it.
        "GH_TOKEN": "",
        "GH_CONFIG_DIR": str(tmp_path),
        "FAKE_GH_RC": str(gh_rc),
        "FAKE_NOW": str(NOW),
        "EXEC_FILE": exec_file,
        "MAX_WAIT_SECONDS": max_wait,
    }
    script = _script_file(tmp_path, stubs + _step_script(_workflow(LANE), CLASSIFY), "classify.sh")
    result = subprocess.run(
        [bash, script],
        env=_child_env(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, _proc_log(result)
    return _Run(result, tmp_path)


class TestClassifierRetriesOnlyASpentQuota:
    def test_a_spent_core_bucket_waits_for_its_reset_then_retries(self, tmp_path: Path) -> None:
        reset = NOW + 60
        run = _classify(tmp_path, _response("200 OK", {}, _rate_limit_body(core=0, reset=reset)))
        assert run.outputs.get("retry") == "true", run.log
        assert len(run.sleeps) == 1, run.log
        # reset - now + the 5 s margin.
        assert run.sleeps == [65], run.log
        assert run.gh_calls == ["api -i rate_limit"]

    def test_a_spent_graphql_bucket_also_counts(self, tmp_path: Path) -> None:
        reset = NOW + 30
        run = _classify(tmp_path, _response("200 OK", {}, _rate_limit_body(graphql=0, reset=reset)))
        assert run.outputs.get("retry") == "true", run.log
        assert run.sleeps == [35], run.log

    def test_retry_after_on_a_refused_read_is_the_wait(self, tmp_path: Path) -> None:
        response = _response(
            "403 Forbidden",
            {"Retry-After": "40", "Content-Type": "application/json"},
            '{"message":"You have exceeded a secondary rate limit."}',
        )
        run = _classify(tmp_path, response, gh_rc=1)
        assert run.outputs.get("retry") == "true", run.log
        assert run.sleeps == [45]

    def test_ratelimit_reset_header_with_zero_remaining_is_the_wait(self, tmp_path: Path) -> None:
        reset = NOW + 90
        response = _response(
            "403 Forbidden",
            {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": str(reset)},
            '{"message":"API rate limit exceeded for installation ID 1."}',
        )
        run = _classify(tmp_path, response, gh_rc=1)
        assert run.outputs.get("retry") == "true", run.log
        assert run.sleeps == [95], run.log

    def test_header_case_and_crlf_line_ends_do_not_matter(self, tmp_path: Path) -> None:
        response = _response(
            "429 Too Many Requests", {"RETRY-AFTER": "12"}, '{"message":"slow down"}', crlf=True
        )
        run = _classify(tmp_path, response, gh_rc=1)
        assert run.outputs.get("retry") == "true", run.log
        assert run.sleeps == [17]

    def test_a_reset_already_past_still_waits_the_margin(self, tmp_path: Path) -> None:
        reset = NOW - 30
        run = _classify(tmp_path, _response("200 OK", {}, _rate_limit_body(core=0, reset=reset)))
        assert run.outputs.get("retry") == "true", run.log
        assert run.sleeps == [5]


class TestClassifierFailsClosedOnEverythingElse:
    def test_buckets_with_quota_left_are_not_a_rate_limit(self, tmp_path: Path) -> None:
        run = _classify(tmp_path, _response("200 OK", {}, _rate_limit_body(core=12, graphql=7)))
        assert run.outputs.get("retry") == "false", run.log
        assert run.sleeps == []
        assert "Not a rate limit" in run.result.stdout

    def test_a_403_without_rate_limit_headers_is_not_retried(self, tmp_path: Path) -> None:
        response = _response(
            "403 Forbidden",
            {"X-Ratelimit-Remaining": "4211"},
            '{"message":"Resource not accessible by integration"}',
        )
        run = _classify(tmp_path, response, gh_rc=1)
        assert run.outputs.get("retry") == "false", run.log
        assert run.sleeps == []

    def test_an_unreadable_rate_limit_is_not_retried(self, tmp_path: Path) -> None:
        run = _classify(tmp_path, None, gh_rc=1)
        assert run.outputs.get("retry") == "false", run.log
        assert run.sleeps == []

    def test_a_malformed_body_is_not_retried(self, tmp_path: Path) -> None:
        run = _classify(tmp_path, _response("200 OK", {}, "not json"))
        assert run.outputs.get("retry") == "false", run.log

    def test_a_wait_past_the_cap_fails_closed_and_names_the_rerun(self, tmp_path: Path) -> None:
        reset = NOW + 3000
        run = _classify(tmp_path, _response("200 OK", {}, _rate_limit_body(core=0, reset=reset)))
        assert run.outputs.get("retry") == "false", run.log
        assert run.sleeps == []
        assert "::error::" in run.result.stdout
        assert "needs no new push" in run.result.stdout

    def test_the_cap_comes_from_max_wait_seconds(self, tmp_path: Path) -> None:
        response = _response("429 Too Many Requests", {"Retry-After": "100"}, "{}")
        run = _classify(tmp_path, response, gh_rc=1, max_wait="60")
        assert run.outputs.get("retry") == "false", run.log
        assert run.sleeps == []

    def test_a_pass_that_left_a_transcript_is_never_retried(self, tmp_path: Path) -> None:
        transcript = tmp_path / "execution.json"
        transcript.write_text('[{"type":"result","result":"partial"}]', encoding="utf-8")
        run = _classify(
            tmp_path,
            _response("200 OK", {}, _rate_limit_body(core=0)),
            exec_file=str(transcript),
        )
        assert run.outputs.get("retry") == "false", run.log
        assert run.sleeps == []
        # Decided before any API read.
        assert run.gh_calls == []


class TestRetryWiring:
    def test_the_retry_uses_exactly_the_validation_inputs(self) -> None:
        first, retry = _step(VALIDATE), _step(RETRY)
        assert retry["uses"] == first["uses"]
        assert retry["with"] == first["with"]

    def test_only_the_first_attempt_continues_on_error(self) -> None:
        assert _step(VALIDATE).get("continue-on-error") is True
        assert "continue-on-error" not in _step(RETRY)

    def test_the_retry_runs_only_on_the_classifier_verdict(self) -> None:
        classify = _step(CLASSIFY)
        assert "steps.review.outcome == 'failure'" in classify["if"]
        retry = _step(RETRY)
        assert retry["id"] == "review_retry"
        assert "steps.rate_limit.outputs.retry == 'true'" in retry["if"]
        assert "steps.human_override.outputs.active != 'true'" in retry["if"]

    def test_the_retry_gets_fresh_credentials_then_the_scrub(self) -> None:
        steps = _steps()
        at = next(i for i, s in enumerate(steps) if s.get("name") == RETRY)
        assert steps[at - 1].get("name") == SCRUB
        assert "steps.rate_limit.outputs.retry == 'true'" in steps[at - 1]["if"]
        creds = steps[at - 2]
        assert str(creds.get("uses", "")).startswith("aws-actions/configure-aws-credentials@")
        assert "steps.rate_limit.outputs.retry == 'true'" in creds["if"]

    def test_a_failed_first_attempt_still_fails_the_job_unless_the_retry_passed(self) -> None:
        steps = _steps()
        names = [s.get("name") for s in steps]
        gate = _step(FAIL_CLOSED)
        assert names.index(FAIL_CLOSED) == names.index(RETRY) + 1
        assert "steps.review.outcome == 'failure'" in gate["if"]
        assert "steps.review_retry.outcome != 'success'" in gate["if"]
        # No status function: it runs only while the job is still green, which is
        # exactly when continue-on-error has hidden the first failure.
        assert "always()" not in gate["if"]
        assert "exit 1" in gate["run"]

    def test_downstream_readers_prefer_the_retry_transcript(self) -> None:
        both = (
            "${{ steps.review_retry.outputs.execution_file"
            " || steps.review.outputs.execution_file }}"
        )
        assert _step("Capture and redact review output")["env"]["EXEC_FILE"] == both
        assert _step("Detect turn-budget exhaustion")["env"]["REVIEW_EXEC"] == both

    def test_no_permission_was_widened(self) -> None:
        doc = yaml.safe_load(_workflow(LANE))
        assert doc["permissions"] == {
            "actions": "read",
            "checks": "write",
            "contents": "read",
            "id-token": "write",
            "pull-requests": "write",
        }
        for job in doc["jobs"].values():
            assert "permissions" not in job
        assert _step(CLASSIFY)["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
