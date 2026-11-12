"""``--every 0`` and a blank ``--cron`` name no schedule.

``cron update`` warns on stderr instead of reporting a schedule change the
store never makes, and applies the other fields; with nothing else to apply it
prints the usual "Provide at least one field" line, or fails "Job not found" for
an unknown id like any other update. ``cron add`` refuses them
with their own reason instead of the "provide exactly one" line meant for a
missing schedule flag (its parser admits only one schedule flag at a time).
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cli_commands import _cron


def _update_args(**fields) -> argparse.Namespace:
    base = dict(
        cron_action="update",
        job_id="abc123",
        name=None,
        message=None,
        every_secs=None,
        cron_expr=None,
        channel=None,
        approval_mode=None,
    )
    base.update(fields)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    ("fields", "warning"),
    [
        ({"every_secs": 0}, "Warning: --every 0 leaves the schedule unchanged"),
        ({"cron_expr": ""}, "Warning: an empty --cron leaves the schedule unchanged"),
        ({"cron_expr": "   "}, "Warning: an empty --cron leaves the schedule unchanged"),
    ],
    ids=["every-zero", "cron-empty", "cron-blank"],
)
def test_update_with_only_a_blank_schedule_warns_and_updates_nothing(fields, warning, capsys):
    with patch("kiro_crew.cli_commands.CronService") as mock_svc_cls:
        mock_svc_cls.return_value.get_job.return_value = MagicMock()
        with pytest.raises(SystemExit) as exc:
            _cron(_update_args(**fields))
        mock_svc_cls.return_value.update_job.assert_not_called()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert out.err == f"{warning}\nProvide at least one field to update\n"
    assert out.out == ""


def test_a_blank_schedule_on_a_missing_job_still_fails_not_found(capsys):
    """A stale id fails with any field, so it fails with only a blank schedule too."""
    with (
        patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
        patch("kiro_crew.cli_commands.sel") as mock_sel,
    ):
        mock_svc_cls.return_value.get_job.return_value = None
        with pytest.raises(SystemExit) as exc:
            _cron(_update_args(job_id="ghost", every_secs=0))
        mock_svc_cls.return_value.update_job.assert_not_called()
        mock_sel.return_value.log_api_access.assert_called_once_with(
            caller="cli",
            operation="cron.update",
            outcome="not_found",
            source="cli",
            resources="job_id=ghost reason=not_found",
        )
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert out.err.endswith("Job not found: ghost\n")
    assert out.out == ""


def test_a_blank_schedule_over_an_unreadable_store_refuses(tmp_path, monkeypatch, capsys):
    """A corrupt store reads as empty; it must refuse, not report "Job not found"."""
    from kiro_crew.config.loader import config_dir

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (config_dir() / "crons").mkdir(parents=True)
    (config_dir() / "crons.json").write_text("{not json", encoding="utf-8")
    with patch("kiro_crew.cli_commands.sel"), pytest.raises(SystemExit) as exc:
        _cron(_update_args(every_secs=0))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith("Error: ")
    assert "Job not found" not in err


def test_update_with_a_blank_schedule_still_applies_the_other_fields(capsys):
    with (
        patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
        patch("kiro_crew.cli_commands.sel"),
    ):
        job = MagicMock()
        job.id, job.name = "abc123", "renamed"
        mock_svc_cls.return_value.update_job.return_value = job
        _cron(_update_args(name="renamed", every_secs=0))
        mock_svc_cls.return_value.update_job.assert_called_once_with("abc123", name="renamed")
    out = capsys.readouterr()
    assert "Warning: --every 0 leaves the schedule unchanged" in out.err
    assert out.out.startswith("Updated job: abc123 (renamed)")


@pytest.mark.parametrize(
    ("every", "cron_expr", "expected"),
    [
        (0, None, "Error: --every must be a positive number of seconds, got 0"),
        (None, "  ", "Error: --cron must not be empty"),
    ],
    ids=["every-zero", "cron-blank"],
)
def test_add_names_the_blank_schedule_flag(
    every, cron_expr, expected, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    args = argparse.Namespace(
        cron_action="add",
        name="n",
        message="m",
        every=every,
        cron_expr=cron_expr,
        channel=None,
        approval_mode="",
        agent="",
        silent=False,
        folder="",
    )
    with pytest.raises(SystemExit) as exit_info:
        _cron(args)
    err = capsys.readouterr().err
    assert exit_info.value.code == 1
    assert err.splitlines()[-1] == expected
