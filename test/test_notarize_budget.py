"""Hanging-call drills for ``packaging/signing/notarize.sh``.

The script advertises a hard wall-clock budget (``NOTARIZE_BUDGET_SECS``).
That promise has to hold not only across its own sleeps but across the
``xcrun notarytool`` calls themselves: a request that hangs instead of
returning a timeout error must be stopped when the budget runs out, or the
job runs on to the workflow-level timeout and reintroduces the
mid-publication cancellation the script exists to prevent.  These drills
stand in a fake ``xcrun`` that hangs at each call site and assert the script
still exits within the budget with the budget-exhausted verdict.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "signing" / "notarize.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="notarize.sh is a Bash script for the macOS signing runner",
)


def _fake_xcrun(tmp_path: Path, body: str) -> dict[str, str]:
    """Install a fake ``xcrun`` first on PATH; return the env for the script."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    xcrun = bin_dir / "xcrun"
    # $1 is always "notarytool"; $2 is the verb (submit / info / log).
    xcrun.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    xcrun.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        APPLE_ID="drill@example.com",
        APPLE_PW="drill",
        TEAM_ID="DRILL",
        NOTARIZE_BUDGET_SECS="2",
        NOTARIZE_POLL_SECS="1",
        NOTARIZE_RETRY_MIN_SECS="1",
    )
    return env


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], float]:
    target = tmp_path / "app.zip"
    target.write_bytes(b"not really a zip")
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        env=env,
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    return proc, time.monotonic() - started


def _record_waits(tmp_path: Path, env: dict[str, str]) -> Path:
    """Put a recording ``sleep`` first on PATH; return the file it writes.

    Every wait the script makes is a ``sleep`` (the watchdog's and the backoff's), so
    each one is recorded as ``wait <secs>``, and one longer than the budget is refused
    (``refused <secs>``) and returns at once instead of holding the run.  A fake
    ``xcrun`` hangs with ``NOTARIZE_DRILL_HANG=1 sleep``: that sleep records
    ``hang <pid>`` when it starts and ``stopped <pid>`` when a SIGTERM reaches it, which
    can be before it records ``hang`` (an early cancel).
    """
    real_sleep = shutil.which("sleep")
    assert real_sleep is not None
    record = tmp_path / "waits"
    sleep = tmp_path / "bin" / "sleep"
    sleep.write_text(
        "#!/usr/bin/env bash\n"
        f'record="{record}"\n'
        'if [ -n "${NOTARIZE_DRILL_HANG:-}" ]; then\n'
        # ``$!`` names the real sleep from the moment it is forked, so a SIGTERM at
        # any point stops it; one before the fork stops nothing and orphans nothing.
        '  trap \'echo "stopped $$" >> "$record"; [ -z "$!" ] || kill "$!" 2>/dev/null; exit 143\' TERM\n'
        '  echo "hang $$" >> "$record"\n'
        f'  "{real_sleep}" "$1" &\n'
        '  wait "$!"\n'
        "  exit 0\n"
        "fi\n"
        'if awk -v a="$1" -v b="$NOTARIZE_BUDGET_SECS" \'BEGIN { exit !(a + 0 > b + 0) }\'; then\n'
        '  echo "refused $1" >> "$record"\n'
        "  exit 0\n"
        "fi\n"
        'echo "wait $1" >> "$record"\n'
        f'exec "{real_sleep}" "$1"\n',
        encoding="utf-8",
    )
    sleep.chmod(0o755)
    return record


def _assert_waits_within_budget(record: Path) -> None:
    """No wait exceeded the 2 s budget, and every hanging call was signalled.

    A ``stopped`` with no ``hang`` is an early cancel and is valid; a ``hang`` with no
    ``stopped`` from the same shim is a hanging call the budget never stopped.
    """
    lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    assert not [
        line for line in lines if line.startswith("refused")
    ], f"waited past the budget: {lines}"
    stopped = {line.split()[1] for line in lines if line.startswith("stopped ")}
    unstopped = [
        line for line in lines if line.startswith("hang ") and line.split()[1] not in stopped
    ]
    assert not unstopped, f"a hanging call was not stopped: {lines}"


def test_hanging_submit_is_stopped_at_the_budget(tmp_path: Path) -> None:
    # The very first call hangs: no submission id ever comes back.
    env = _fake_xcrun(tmp_path, "NOTARIZE_DRILL_HANG=1 exec sleep 300\n")
    record = _record_waits(tmp_path, env)
    proc, _ = _run(tmp_path, env)
    # The watchdog waited at most the budget, then stopped the hanging call.
    _assert_waits_within_budget(record)
    assert proc.returncode == 1, proc.stderr
    assert "budget of 2s exhausted" in proc.stderr
    assert "submission: <none>" in proc.stderr
    assert "notarytool submit hung" in proc.stdout


def test_hanging_info_is_stopped_at_the_budget(tmp_path: Path) -> None:
    # Submit succeeds, then the status poll hangs.
    env = _fake_xcrun(
        tmp_path,
        'case "$2" in\n'
        '  submit) echo \'{"id":"sub-1"}\' ;;\n'
        "  *) NOTARIZE_DRILL_HANG=1 exec sleep 300 ;;\n"
        "esac\n",
    )
    record = _record_waits(tmp_path, env)
    proc, _ = _run(tmp_path, env)
    _assert_waits_within_budget(record)
    assert proc.returncode == 1, proc.stderr
    assert "budget of 2s exhausted" in proc.stderr
    assert "submission: sub-1" in proc.stderr
    assert "notarytool info hung" in proc.stdout


def test_healthy_path_still_accepts(tmp_path: Path) -> None:
    # The watchdog must not disturb a call that returns normally.
    env = _fake_xcrun(
        tmp_path,
        'case "$2" in\n'
        '  submit) echo \'{"id":"sub-1"}\' ;;\n'
        '  info) echo \'{"status":"Accepted"}\' ;;\n'
        "esac\n",
    )
    proc, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert "notarization Accepted: sub-1" in proc.stdout


def test_transient_error_before_budget_is_retried_then_bounded(tmp_path: Path) -> None:
    # A call that FAILS fast stays a retryable transient; the retry sleeps are
    # what run into the budget, so the verdict is still budget-exhausted.
    env = _fake_xcrun(tmp_path, "echo 'The request timed out.' >&2\nexit 1\n")
    record = _record_waits(tmp_path, env)
    proc, _ = _run(tmp_path, env)
    # Every backoff and watchdog wait stayed inside the budget.
    _assert_waits_within_budget(record)
    assert proc.returncode == 1, proc.stderr
    assert "treating as transient" in proc.stdout
    assert "budget of 2s exhausted" in proc.stderr


def test_a_file_notarytool_refuses_is_not_retried(tmp_path: Path) -> None:
    # Exit 64 is notarytool's usage error: it refused the file itself, before any
    # upload ("must be a zip archive"). Retrying would be refused the same way, so the
    # script exits 64 at once and leaves the next step to the caller.
    env = _fake_xcrun(
        tmp_path,
        'echo "$*" >> "$(dirname "$0")/calls"\n'
        "echo 'Error: app.zip must be a zip archive (.zip)' >&2\n"
        "exit 64\n",
    )
    proc, _ = _run(tmp_path, env)
    assert proc.returncode == 64, proc.stderr
    assert "refused" in proc.stdout
    assert "must be a zip archive" in proc.stdout
    assert "treating as transient" not in proc.stdout
    assert len((tmp_path / "bin" / "calls").read_text(encoding="utf-8").splitlines()) == 1
