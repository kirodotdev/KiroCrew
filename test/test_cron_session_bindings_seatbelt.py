"""A cron adopt or release from a real macOS Seatbelt sandbox still saves.

The binding directory is hidden from every sandbox, and on macOS Seatbelt denies it
outright. These tests run the real standard-tier crew profile through
``sandbox-exec`` so the kernel, not a monkeypatch, answers both the denial and the
confinement verdict that ``session_bindings`` relies on. A removal is not exercised
here: it takes the grant-epoch lock under the hidden ``.vault`` first, which
Seatbelt denies before any binding scan runs.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.config.paths import data_home
from kiro_crew.cron import CronService
from kiro_crew.cron_service import session_bindings
from kiro_crew.testing.wait import default_timeout

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_CONFINED_CRON_ADOPT_STEPS = """
import json, os, sys
from kiro_crew import sandbox_probe
from kiro_crew.cron import CronService
from kiro_crew.cron_service import session_bindings

bindings = session_bindings.data_home() / session_bindings.SESSION_BINDINGS_DIR_NAME
try:
    os.listdir(bindings)
    listing = "allowed"
except OSError as exc:
    listing = exc.errno
listed = session_bindings.list_session_bindings()
service = CronService()
released = service.adopt_job(sys.argv[1], "")
rebound = service.adopt_job(sys.argv[2], "dashboard:chat-9")
print(json.dumps({
    "verdict": sandbox_probe._macos_sandbox_state(),
    "listing": listing,
    "listed": listed,
    "released": released,
    "rebound": rebound,
}))
"""


def _require_unsandboxed_seatbelt() -> None:
    """Skip unless this process can apply a Seatbelt profile (it cannot from inside one)."""
    if sys.platform != "darwin" or not os.path.exists(_SANDBOX_EXEC):
        pytest.skip("needs macOS sandbox-exec")
    probe = subprocess.run(
        [_SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
        check=False,
        timeout=default_timeout(),
    )
    if probe.returncode != 0:
        pytest.skip("already inside a sandbox: sandbox_apply is refused")


@pytest.fixture
def resolved_crew_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    # Seatbelt matches resolved paths, and macOS temp dirs sit behind /var -> /private/var.
    home = data_home().resolve()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def test_a_seatbelt_confined_cron_adopt_and_release_still_save(
    resolved_crew_home: Path, tmp_path: Path
) -> None:
    _require_unsandboxed_seatbelt()
    service = CronService()
    released = service.add_job("released", "run", every_secs=3600, session_key="dashboard:chat-7")
    rebound = service.add_job("rebound", "run", every_secs=3600, session_key="dashboard:chat-8")
    released_record = session_bindings.session_binding_path(released.id)
    rebound_record = session_bindings.session_binding_path(rebound.id)
    assert released_record.is_file() and rebound_record.is_file()

    profile = sandbox._build_seatbelt_profile("standard")
    result = subprocess.run(
        [
            _SANDBOX_EXEC,
            "-p",
            profile,
            sys.executable,
            "-c",
            _CONFINED_CRON_ADOPT_STEPS,
            released.id,
            rebound.id,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=tmp_path,
        timeout=default_timeout(),
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])

    assert outcome["listing"] in (errno.EPERM, errno.EACCES), outcome
    assert outcome["verdict"] is True, outcome
    assert outcome["listed"] == [], outcome
    assert outcome["released"] is True and outcome["rebound"] is True, outcome
    reloaded = CronService()
    released_job = reloaded.get_job(released.id)
    assert released_job is not None and released_job.session_key == ""
    rebound_job = reloaded.get_job(rebound.id)
    assert rebound_job is not None and rebound_job.session_key == "dashboard:chat-9"
    # The confined shell cannot reach the gateway's records; they stay on disk unchanged.
    assert session_bindings.read_session_binding(released.id) == "dashboard:chat-7"
    assert session_bindings.read_session_binding(rebound.id) == "dashboard:chat-8"
