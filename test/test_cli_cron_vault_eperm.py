"""``kirocrew cron`` when the OS refuses this process the ``.vault`` dir.

macOS data protection can deny a terminal even a ``stat`` of
``<data home>/.vault`` while the gateway reads it fine. ``cron remove`` must
read the grant-epoch state there before it deletes a job, so it still refuses:
the revoke fence stays fail-closed. These tests pin that it refuses with one
line naming the path and the OS refusal instead of a traceback, and that the
verbs that never touch the vault keep working.

The refusal is simulated by patching ``os.stat`` / ``os.mkdir`` for the vault
path only (``Path.is_dir`` and ``Path.mkdir`` go through them), so the test
runs the same on every OS.
"""

from __future__ import annotations

import argparse
import errno
import os
from unittest.mock import patch

import pytest

from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronService


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (config_dir() / "crons").mkdir(parents=True)
    (config_dir() / ".vault").mkdir()
    with patch("kiro_crew.cli_commands.sel"):
        yield root


def _add_job() -> str:
    svc = CronService(base_dir=config_dir())
    return svc.add_job(name="t", message="hi", every_secs=3600).id


def _deny_vault(monkeypatch) -> str:
    vault = os.fspath(config_dir() / ".vault")
    real_stat, real_mkdir = os.stat, os.mkdir

    def _is_vault(p) -> bool:
        return os.fspath(p).rstrip(os.sep) == vault

    def fake_stat(p, *a, **k):
        if _is_vault(p):
            raise PermissionError(errno.EPERM, "Operation not permitted", vault)
        return real_stat(p, *a, **k)

    def fake_mkdir(p, *a, **k):
        if _is_vault(p):
            raise FileExistsError(errno.EEXIST, "File exists", vault)
        return real_mkdir(p, *a, **k)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "mkdir", fake_mkdir)
    return vault


def _job_ids() -> set[str]:
    return {j.id for j in CronService(base_dir=config_dir()).list_jobs(include_disabled=True)}


def test_remove_refuses_with_one_line_naming_the_vault(home, monkeypatch, capsys):
    job_id = _add_job()
    vault = _deny_vault(monkeypatch)

    raised: BaseException | None = None
    try:
        _cron(argparse.Namespace(cron_action="remove", job_id=job_id, managed_by=None))
    except BaseException as exc:  # an escaping OSError is the bug: assert, not error
        raised = exc

    assert isinstance(raised, SystemExit), f"traceback instead of a one-line error: {raised!r}"
    assert raised.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Error: ")
    assert err.count("\n") == 1
    assert vault in err
    assert "Operation not permitted" in err
    assert "Traceback" not in err
    # Fail-closed: the grant epoch could not be bumped, so the job stays.
    assert job_id in _job_ids()


def test_list_still_works_when_the_vault_is_denied(home, monkeypatch, capsys):
    job_id = _add_job()
    _deny_vault(monkeypatch)

    _cron(argparse.Namespace(cron_action="list"))

    assert job_id in capsys.readouterr().out


def test_a_policy_permission_error_is_not_relabelled_an_os_refusal(home, monkeypatch):
    def refuse(_args):
        raise PermissionError("Cron job ownership violation")

    monkeypatch.setattr("kiro_crew.cli_commands_cron._cron_dispatch", refuse)

    with pytest.raises(PermissionError, match="ownership violation"):
        _cron(argparse.Namespace(cron_action="list"))
