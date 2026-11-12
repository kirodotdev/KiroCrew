"""``kirocrew cron`` verbs refuse a busy store with one line, not a traceback.

``cron add`` already answers ``CronStoreBusy`` with ``cron store busy, please
retry`` on stderr and exit 1. ``update``, ``remove``, ``pause`` and ``resume``
write through the same store lock, but the dispatcher translated only an
unreadable store, so a contended lock reached the person as a stack trace.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import kiro_crew.cli_commands as cc
from kiro_crew.cron import CronService, CronStoreBusy


def _busy(*_args: object, **_kwargs: object) -> object:
    raise CronStoreBusy("cron store lock busy")


@pytest.mark.parametrize(
    ("action", "method", "extra"),
    [
        ("update", "update_job", {"name": "renamed"}),
        ("remove", "remove_job", {"managed_by": None}),
        ("pause", "enable_job", {}),
        ("resume", "enable_job", {}),
    ],
)
def test_a_busy_store_is_one_line_and_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    method: str,
    extra: dict[str, object],
) -> None:
    job_id = CronService(base_dir=tmp_path).add_job(name="n", message="m", every_secs=3600).id
    monkeypatch.setattr(cc, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(CronService, method, _busy)
    args = argparse.Namespace(cron_action=action, job_id=job_id, **extra)

    raised: BaseException | None = None
    try:
        cc._cron(args)
    except BaseException as exc:  # noqa: BLE001 -- the escaping type is what is under test
        raised = exc

    assert isinstance(raised, SystemExit), f"{type(raised).__name__} escaped the CLI"
    assert raised.code == 1
    err = capsys.readouterr().err
    assert "cron store busy, please retry" in err, err
    assert "Traceback" not in err, err
