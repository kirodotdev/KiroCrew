"""The gateway-restart helper script must record the restart's outcome.

The script runs as a DISOWNED process: its stdout and exit status reach no
terminal and no calling session. Before it recorded them to files, the CLI
restart verb's loud non-zero verdict (replacement never served) was thrown
away and the calling agent reported success unconditionally — on a host where
the restart is a silent no-op (root-owned system unit, unix-socket listener)
the original gateway kept running while the user was told it restarted.

These tests drive the real script with a stubbed ``kirocrew`` on PATH and
assert the contract the gateway-restart skill's "Verify the outcome" step
reads: output appended to ``logs/restart.log``, the exit status written to
``logs/restart-status``, and a stale status file removed while an attempt is
pending.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "gateway-restart" / "do-restart.sh"
PS_SCRIPT = REPO_ROOT / "skills" / "gateway-restart" / "do-restart.ps1"
SKILL_DOC = REPO_ROOT / "skills" / "gateway-restart" / "SKILL.md"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="drives the POSIX helper script with bash",
)


def _run_script(
    tmp_path: Path, exit_status: int, stub_body_extra: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run do-restart.sh against a stub ``kirocrew`` that exits *exit_status*.

    Returns the completed process and the crew home used. The stub prints a
    recognizable line so the log-append assertion is about the RESTART's
    output, not the script's own. ``cwd`` is pinned under ``tmp_path`` so a
    misbehaving script cannot litter the checkout.
    """
    crew_home = tmp_path / "crew-home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "kirocrew"
    stub.write_text(
        "#!/bin/bash\n"
        f"{stub_body_extra}"
        'echo "stub-restart-output $*"\n'
        f"exit {exit_status}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["KIROCREW_HOME"] = str(crew_home)
    # Collapse the human-scale scheduling delay; the delay is not under test.
    env["KIROCREW_RESTART_DELAY"] = "0"
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    return proc, crew_home


@needs_bash
def test_success_exit_status_is_recorded(tmp_path: Path) -> None:
    proc, crew_home = _run_script(tmp_path, exit_status=0)
    status_file = crew_home / "logs" / "restart-status"
    assert proc.returncode == 0
    assert status_file.read_text(encoding="utf-8").strip() == "0"
    log = (crew_home / "logs" / "restart.log").read_text(encoding="utf-8")
    assert "stub-restart-output restart" in log


@needs_bash
def test_failure_exit_status_is_recorded_not_discarded(tmp_path: Path) -> None:
    # The core regression: the restart verb exiting non-zero (replacement
    # never served) must land in the status file, and the script itself must
    # propagate it — a disowned run previously discarded both.
    proc, crew_home = _run_script(tmp_path, exit_status=7)
    status_file = crew_home / "logs" / "restart-status"
    assert proc.returncode == 7
    assert status_file.read_text(encoding="utf-8").strip() == "7"


@needs_bash
def test_stale_status_is_removed_before_the_restart_runs(tmp_path: Path) -> None:
    # A leftover "0" from a previous attempt must not be readable as THIS
    # attempt's verdict: the script removes it up front, so during the pending
    # window the file is absent. The stub proves the ordering by recording
    # whether the status file still existed when the restart executed.
    crew_home = tmp_path / "crew-home"
    logs = crew_home / "logs"
    logs.mkdir(parents=True)
    (logs / "restart-status").write_text("0\n", encoding="utf-8")
    marker = tmp_path / "seen"
    probe = (
        f'if [ -e "{logs / "restart-status"}" ]; then echo present > "{marker}"; '
        f'else echo absent > "{marker}"; fi\n'
    )
    proc, crew_home = _run_script(tmp_path, exit_status=3, stub_body_extra=probe)
    assert proc.returncode == 3
    assert marker.read_text(encoding="utf-8").strip() == "absent"
    assert (logs / "restart-status").read_text(encoding="utf-8").strip() == "3"


@needs_bash
def test_status_file_is_owner_only(tmp_path: Path) -> None:
    # The log can carry gateway diagnostics (paths, ports); both files live
    # under the crew home and are created owner-only via the script's umask.
    _, crew_home = _run_script(tmp_path, exit_status=0)
    for name in ("restart-status", "restart.log"):
        mode = stat.S_IMODE((crew_home / "logs" / name).stat().st_mode)
        assert mode == 0o600, f"{name} mode {oct(mode)}"


@needs_bash
def test_dashboard_token_urls_are_redacted_from_the_log(tmp_path: Path) -> None:
    # The restart verb prints a fresh dashboard token URL on success, and the
    # skill instructs the resumed agent to QUOTE this log into a conversation
    # other humans can read — the bearer must not survive into the file. The
    # restart's own exit status must also survive the redaction pipeline.
    probe = 'echo "🔑 http://localhost:5476?token=SECRET123&x=1 and http://h/?a=1&token=T2"\n'
    proc, crew_home = _run_script(tmp_path, exit_status=5, stub_body_extra=probe)
    log = (crew_home / "logs" / "restart.log").read_text(encoding="utf-8")
    assert "SECRET123" not in log
    assert "token=REDACTED" in log
    assert proc.returncode == 5
    status = (crew_home / "logs" / "restart-status").read_text(encoding="utf-8").strip()
    assert status == "5"


@needs_bash
def test_attempt_specific_status_file_override(tmp_path: Path) -> None:
    # Overlapping restart attempts must not overwrite each other's verdict:
    # when the scheduler passes KIROCREW_RESTART_STATUS_FILE, the verdict
    # lands there and the shared default is left untouched.
    crew_home = tmp_path / "crew-home"
    attempt = crew_home / "logs" / "restart-status.1234.99"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "kirocrew"
    stub.write_text("#!/bin/bash\nexit 4\n", encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["KIROCREW_HOME"] = str(crew_home)
    env["KIROCREW_RESTART_DELAY"] = "0"
    env["KIROCREW_RESTART_STATUS_FILE"] = str(attempt)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 4
    assert attempt.read_text(encoding="utf-8").strip() == "4"
    assert not (crew_home / "logs" / "restart-status").exists()
    # The diagnostic log is correlated with the attempt (status file + ".log"),
    # so a failed attempt's verifier never quotes another attempt's output out
    # of the shared restart.log.
    assert (crew_home / "logs" / "restart-status.1234.99.log").exists()
    assert not (crew_home / "logs" / "restart.log").exists()


@needs_bash
def test_nonconforming_status_paths_fall_back_to_the_shared_default(tmp_path: Path) -> None:
    # The helper runs detached from any agent sandbox, so a caller-chosen
    # status path would let it delete and overwrite arbitrary user files
    # (e.g. ~/.bashrc). Every non-conforming override — outside the logs dir,
    # nested, or traversal-carrying — must fall back to the shared default.
    crew_home = tmp_path / "crew-home"
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "kirocrew"
    stub.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    logs = crew_home / "logs"
    evil_paths = [
        str(victim),
        str(logs / "restart-status.a" / "b"),
        str(logs / "restart-status...." / ".." / ".." / "victim.txt"),
        str(logs / "restart-status...suffix"),
        str(crew_home / "config.json"),
    ]
    for evil in evil_paths:
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["KIROCREW_HOME"] = str(crew_home)
        env["KIROCREW_RESTART_DELAY"] = "0"
        env["KIROCREW_RESTART_STATUS_FILE"] = evil
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert proc.returncode == 0, evil
        # The verdict landed in the confined shared default, and the
        # caller-chosen target was never touched.
        assert (logs / "restart-status").read_text(encoding="utf-8").strip() == "0", evil
        (logs / "restart-status").unlink()
    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert not (crew_home / "config.json").exists()


@needs_bash
def test_artifact_writes_refuse_planted_symlinks(tmp_path: Path) -> None:
    # The helper's artifact writes must never follow a link planted at the
    # predictable confined path: the write is rm + O_EXCL create, so a link
    # (even re-planted) is refused and the target stays untouched.
    crew_home = tmp_path / "crew-home"
    logs = crew_home / "logs"
    logs.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    attempt = logs / "restart-status.777.1"
    # Plant links at BOTH artifact paths. rm -f removes the planted link
    # itself; the exclusive create then produces a real file — the victim the
    # links pointed at must never be written through.
    attempt.symlink_to(victim)
    (logs / "restart-status.777.1.log").symlink_to(victim)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kirocrew"
    stub.write_text("#!/bin/bash\necho probe-output\nexit 6\n", encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["KIROCREW_HOME"] = str(crew_home)
    env["KIROCREW_RESTART_DELAY"] = "0"
    env["KIROCREW_RESTART_STATUS_FILE"] = str(attempt)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 6
    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert not attempt.is_symlink() and attempt.read_text(encoding="utf-8").strip() == "6"
    log = logs / "restart-status.777.1.log"
    assert not log.is_symlink() and "probe-output" in log.read_text(encoding="utf-8")


@needs_bash
def test_artifact_opens_resolve_each_path_exactly_once() -> None:
    # The round-4 hardening created each artifact with O_EXCL and then
    # RE-OPENED it by path for the actual writes — two resolutions, and only
    # the second (link-followable) one was held. The fix opens each artifact
    # exactly once, under noclobber, directly onto the write descriptor. Pin
    # the structure: every artifact fd open must be a plain '>' redirection
    # (guarded by set -C), and no append-reopen of either artifact path may
    # exist anywhere in the script.
    text = SCRIPT.read_text(encoding="utf-8")
    assert "exec 3>>" not in text, "log artifact reopened by path after its create"
    assert '>>"$STATUS_FILE"' not in text, "status artifact reopened by path after its create"
    assert '>>"$LOG_FILE"' not in text, "log artifact reopened by path after its create"
    # The single opens exist and are noclobber-guarded (set -C precedes both).
    assert 'exec 3>"$LOG_FILE"' in text
    assert 'exec 4>"$STATUS_FILE"' in text
    assert text.count("set -C") >= 2


def test_both_platform_scripts_and_the_skill_agree_on_the_status_file() -> None:
    # The skill's "Verify the outcome" step reads the file by name; a rename
    # in one script (or the doc) would silently break verification on that
    # platform. Pin the shared contract.
    for path in (SCRIPT, PS_SCRIPT, SKILL_DOC):
        assert "restart-status" in path.read_text(encoding="utf-8"), path.name
