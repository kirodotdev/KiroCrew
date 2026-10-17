"""Drills for ``packaging/signing/cdsigner-submit.sh``.

The signing service throttles a burst of submissions with HTTP 429 and the
body ``{"message":"Too Many Requests"}`` while awscurl exits 0, and the macOS
legs of one release submit together. A nightly lost its arm64 leg that way.
These drills stand in a fake ``awscurl`` that answers from a script of
responses, and a fake ``sleep`` that records the backoff instead of waiting.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SIGNING_DIR = Path(__file__).resolve().parents[1] / "packaging" / "signing"
LIBRARY = SIGNING_DIR / "cdsigner-submit.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="cdsigner-submit.sh is a Bash library for the signing runners",
)

THROTTLED = '{"message":"Too Many Requests"}'
ACCEPTED = '{"signTaskId":"task-123"}'

# Answers call N with line N of $FAKE_DIR/responses as "<exit code> <body>".
FAKE_AWSCURL = r"""#!/usr/bin/env bash
n=$(( $(cat "$FAKE_DIR/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAKE_DIR/calls"
line=$(sed -n "${n}p" "$FAKE_DIR/responses")
[ -n "$line" ] || line=$(tail -n 1 "$FAKE_DIR/responses")
echo "${line#* }"
exit "${line%% *}"
"""

FAKE_SLEEP = r"""#!/usr/bin/env bash
echo "$1" >> "$FAKE_DIR/sleeps"
"""


def _run(
    tmp_path: Path, responses: list[tuple[int, str]], **env: str
) -> tuple[subprocess.CompletedProcess, int, list[int]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("awscurl", FAKE_AWSCURL), ("sleep", FAKE_SLEEP)):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    (tmp_path / "responses").write_text(
        "".join(f"{code} {body}\n" for code, body in responses), encoding="utf-8"
    )
    run_env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        "CDSIGNER_API_ENDPOINT": "https://signer.invalid",
        **env,
    }
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "{LIBRARY}"; submit_sign_task "$1"',
            "_",
            json.dumps({"k": "v"}),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=run_env,
        cwd=tmp_path,
        timeout=60,
    )
    calls = (
        int((tmp_path / "calls").read_text(encoding="utf-8"))
        if (tmp_path / "calls").exists()
        else 0
    )
    sleeps_file = tmp_path / "sleeps"
    sleeps = (
        [int(s) for s in sleeps_file.read_text(encoding="utf-8").split()]
        if sleeps_file.exists()
        else []
    )
    return proc, calls, sleeps


def test_a_throttled_submission_is_retried_until_accepted(tmp_path):
    proc, calls, sleeps = _run(tmp_path, [(0, THROTTLED), (0, THROTTLED), (0, ACCEPTED)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "task-123"
    assert calls == 3
    # Doubling base with up to as much again of jitter: 10..20, then 20..40.
    assert len(sleeps) == 2
    assert 10 <= sleeps[0] <= 20 and 20 <= sleeps[1] <= 40


def test_throttling_that_never_clears_fails_after_the_attempt_budget(tmp_path):
    proc, calls, sleeps = _run(tmp_path, [(0, THROTTLED)])
    assert proc.returncode == 1
    assert calls == 5 and len(sleeps) == 4
    assert sum(sleeps) <= 300
    assert "still throttled after 5 attempts" in proc.stderr
    assert THROTTLED in proc.stderr


@pytest.mark.parametrize(
    "response",
    [(0, '{"message":"User is not authorized"}'), (7, "curl: (7) Failed to connect")],
    ids=["rejected", "unreachable"],
)
def test_a_failure_that_is_not_throttling_is_reported_at_once(tmp_path, response):
    proc, calls, sleeps = _run(tmp_path, [response])
    assert proc.returncode == 1
    assert calls == 1 and sleeps == []
    assert response[1] in proc.stderr


def test_an_accepted_first_submission_does_not_wait(tmp_path):
    proc, calls, sleeps = _run(tmp_path, [(0, ACCEPTED)])
    assert proc.returncode == 0 and proc.stdout.strip() == "task-123"
    assert calls == 1 and sleeps == []


@pytest.mark.parametrize("script", ["sign.sh", "sign-dmg.sh"])
def test_both_signing_scripts_submit_through_the_helper(script):
    """A second hand-written POST would bring back the unretried submission."""
    body = (SIGNING_DIR / script).read_text(encoding="utf-8")
    assert "cdsigner-submit.sh" in body and "submit_sign_task" in body
    assert "-X POST" not in body
