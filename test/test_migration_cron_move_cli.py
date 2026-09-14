"""The `kirocrew cron move` CLI handler.

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


def test_cron_move_plan_never_prints_a_credential_or_a_terminal_sequence(monkeypatch, capsys):
    """The CLI plan is a terminal sink, so an identity must not reach it raw.

    A cron `command` is operator-authored and echoed verbatim as a requirement
    identity. Two distinct hazards, so two assertions: a credential would persist
    in scrollback and shell history after the process is gone, and a terminal
    control sequence is not a credential at all -- redaction leaves it untouched
    -- yet printing it lets the value drive the terminal. This is the CLI twin of
    the dashboard plan route's redaction; the dashboard was covered first and
    this sink was missed.
    """
    import kiro_crew.cli_commands as cc

    job = CronJob(
        id="j1",
        name="nightly",
        message="run backup",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
        agent_id="kirocrew",
        # A credential AND an OSC title-set sequence in one command.
        command="curl -H 'Authorization: Bearer AKIAIOSFODNN7EXAMPLE' \x1b]0;pwned\x07 http://x.test",
    )
    monkeypatch.setattr(cc, "CronService", lambda *a, **k: _FakeSvc(job))
    monkeypatch.setattr(
        cc, "sel", lambda: type("S", (), {"log_api_access": staticmethod(lambda **kw: None)})()
    )

    args = argparse.Namespace(command="cron", cron_action="move", job_id="j1", to_crew="remote-ec2")
    cc._cron_dispatch(args)

    out = capsys.readouterr().out
    assert "AKIAIOSFODNN7EXAMPLE" not in out, "credential reached the terminal"
    assert "\x1b" not in out and "\x07" not in out, "terminal control sequence reached the terminal"


def test_taskrun_move_refuses_a_sensitive_runs_file(monkeypatch, capsys):
    """`--runs-file` is caller-supplied, so it must go through the path gate.

    Without the gate the file is READ before anything validates it, and a JSON
    decode error quotes the offending text -- so pointing the flag at a
    credential store puts its contents on stderr. The refusal must happen
    instead, and the same module already reads other caller-named JSON through
    `safe_read_file`, so this was an inconsistency rather than a missing helper.
    """
    import kiro_crew.cli_commands as cc

    def _refuse(path: str) -> str:
        raise PermissionError(f"Blocked: access to sensitive path: {path!r}")

    monkeypatch.setattr(cc, "safe_read_file", _refuse)
    args = argparse.Namespace(
        command="taskrun",
        taskrun_action="move",
        task_id="run-1",
        to_crew="remote-ec2",
        runs_file="/home/someone/.aws/credentials",
    )
    with pytest.raises(SystemExit) as exc:
        cc._taskrun_dispatch(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "sensitive path" in err


def test_cron_move_handler_unknown_job_exits(monkeypatch, capsys):
    import kiro_crew.cli_commands as cc

    monkeypatch.setattr(cc, "CronService", lambda *a, **k: _FakeSvc(None))
    args = argparse.Namespace(command="cron", cron_action="move", job_id="nope", to_crew="dst")
    with pytest.raises(SystemExit):
        cc._cron_dispatch(args)
    assert "job not found" in capsys.readouterr().err
