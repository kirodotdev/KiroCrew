"""Slice 2 (circle 3) — `kirocrew cron move` CLI handler (issue #7577, Task 2.6).

Exercises the argparse-level 'move' verb wiring by driving the same code path
argparse would: it builds a Namespace and confirms the handler prints a plan
carrying the handoff_id and the blocking target requirements, via the tested
plan_cron_move core. The CronService is monkeypatched to a fake so no real
crons.json / store is touched.

Side-effect discipline: fake service, captured stdout, no disk/network.
"""

from __future__ import annotations

import argparse

import pytest

from kiro_crew.cron import CronJob, CronSchedule


class _FakeSvc:
    def __init__(self, job):
        self._job = job

    def get_job(self, job_id):
        return self._job if self._job and self._job.id == job_id else None


def _job():
    return CronJob(
        id="j1",
        name="nightly",
        message="run backup",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
        agent_id="kirocrew",
        script="~/.kiro/crew/crons/x.py:go",
    )


def test_cron_move_handler_prints_plan_with_handoff_and_requirements(monkeypatch, capsys):
    import kiro_crew.cli_commands as cc

    # Point the handler's CronService at our fake, and no-op the SEL audit.
    monkeypatch.setattr(cc, "CronService", lambda *a, **k: _FakeSvc(_job()))
    monkeypatch.setattr(
        cc, "sel", lambda: type("S", (), {"log_api_access": staticmethod(lambda **kw: None)})()
    )

    args = argparse.Namespace(command="cron", cron_action="move", job_id="j1", to_crew="remote-ec2")
    cc._cron_dispatch(args)

    out = capsys.readouterr().out
    assert "Migration plan for cron job j1" in out
    assert "remote-ec2" in out
    assert "handoff_id:" in out
    # agent + script were on the job -> both surface as blocking requirements
    assert "agent: kirocrew" in out
    assert "script_path:" in out


def test_cron_move_handler_unknown_job_exits(monkeypatch, capsys):
    import kiro_crew.cli_commands as cc

    monkeypatch.setattr(cc, "CronService", lambda *a, **k: _FakeSvc(None))
    args = argparse.Namespace(command="cron", cron_action="move", job_id="nope", to_crew="dst")
    with pytest.raises(SystemExit):
        cc._cron_dispatch(args)
    assert "job not found" in capsys.readouterr().err
