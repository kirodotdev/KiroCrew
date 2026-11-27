"""``cron list`` marks a job whose run is in flight, read from its ``cron-running`` marker."""

import argparse
import json
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from kiro_crew import cron_inflight
from kiro_crew.cron import CronService
from kiro_crew.dashboard.crash_dump_store import current_process_identity

STARTED_AT = 1_700_000_000.0


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from kiro_crew import cli_commands
    from kiro_crew.config.loader import config_dir

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    config_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_commands, "sel", MagicMock())
    return CronService(base_dir=config_dir())


def _list_lines(capsys) -> list[str]:
    from kiro_crew import cli_commands

    cli_commands._cron_dispatch(argparse.Namespace(cron_action="list"))
    return capsys.readouterr().out.splitlines()


def _line_for(lines: list[str], job_id: str) -> str:
    (line,) = [ln for ln in lines if f" {job_id} " in ln]
    return line


def test_a_job_with_a_live_run_marker_is_listed_as_running(svc, capsys):
    from kiro_crew.config.loader import config_dir

    busy = svc.add_job("busy", "slow turn", every_secs=3600).id
    idle = svc.add_job("idle", "quiet", every_secs=3600).id
    cron_inflight.write_marker(config_dir(), busy, "busy", STARTED_AT, run="r1")

    lines = _list_lines(capsys)

    assert "[running since 22:13 UTC]" in _line_for(lines, busy)
    assert "running since" not in _line_for(lines, idle)


def test_a_marker_left_by_a_dead_process_is_not_listed_as_running(svc, capsys):
    from kiro_crew.config.loader import config_dir

    job_id = svc.add_job("crashed", "was running", every_secs=3600).id
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    domain, _start = current_process_identity()
    marker = cron_inflight.running_dir(config_dir()) / f"{job_id}.r1.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {"job_id": job_id, "started_at": STARTED_AT, "pid": gone.pid, "pid_domain": domain}
    marker.write_text(json.dumps(payload))
    assert [m.job_id for m in cron_inflight.read_markers(config_dir())] == [job_id]

    assert "running since" not in _line_for(_list_lines(capsys), job_id)
