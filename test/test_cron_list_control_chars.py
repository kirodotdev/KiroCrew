"""``kirocrew cron list`` prints one row and one detail line per job (#34218).

The list verb printed a job's ``id``, ``name`` and the first 60 characters of
its ``message`` exactly as stored, and the ownership detail line the same way.
A newline in any of them ended the row early, so the remainder printed as an
unprefixed line that reads as a second job -- and both ``name`` and ``message``
are writable by an agent through ``cron_add``. A terminal escape sequence
reached the terminal live for the same reason.

These tests pin the one-line, control-free rendering that ``agent list`` and
``workspace list`` already apply, and the escape-aware 60-character cap that
keeps a printed escape from being cut in half.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

from kiro_crew.cli_commands import _cron
from kiro_crew.cli_commands_cron import _cron_list_cell, _cron_list_clip
from kiro_crew.cron import CronJob, CronSchedule


def _job(
    *,
    job_id: str = "abc12345",
    name: str = "nightly",
    message: str = "check the build",
    **kwargs,
) -> CronJob:
    """A stored job whose text fields callers may poison with control bytes."""
    defaults = dict(
        id=job_id,
        name=name,
        message=message,
        schedule=CronSchedule(kind="every", every_secs=3600),
    )
    defaults.update(kwargs)
    return CronJob(**defaults)


def _list_output(capsys, *jobs: CronJob) -> str:
    """``out`` as ``kirocrew cron list`` prints it for *jobs*."""
    with (
        patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
        patch("kiro_crew.cli_commands.sel"),
    ):
        mock_svc_cls.return_value.list_jobs.return_value = list(jobs)
        _cron(argparse.Namespace(cron_action="list"))
    return capsys.readouterr().out


class TestCronListOneRowPerJob:
    """A stored newline cannot fabricate a second job in the listing."""

    def test_a_newline_in_the_message_stays_inside_its_own_row(self, capsys) -> None:
        # The reproduction from the issue: the tail after the newline spells a
        # complete, plausible row for a job that does not exist.
        out = _list_output(
            capsys,
            _job(message="Check the build.\n  ✅ abcd1234  other-job  (every 60s)"),
        )
        lines = out.splitlines()
        assert len(lines) == 2, out
        assert "other-job" in lines[0]
        assert lines[1].startswith("      owner:")
        # The byte that split the row is visible as text instead of doing it.
        assert "\\x0a" in lines[0]

    def test_two_jobs_render_two_rows_and_two_detail_lines(self, capsys) -> None:
        out = _list_output(
            capsys,
            _job(job_id="first111", name="one", message="a\nb"),
            _job(job_id="second22", name="two", message="c\nd"),
        )
        lines = out.splitlines()
        assert len(lines) == 4, out
        assert lines[0].startswith("  ✅ first111")
        assert lines[1].startswith("      owner:")
        assert lines[2].startswith("  ✅ second22")
        assert lines[3].startswith("      owner:")


class TestCronListControlCharacters:
    """Stored control bytes are rendered, never handed to the terminal live."""

    def test_an_escape_sequence_in_the_name_is_not_emitted(self, capsys) -> None:
        out = _list_output(capsys, _job(name="\x1b[31mnightly\x1b[0m"))
        assert "\x1b" not in out
        assert "nightly" in out

    def test_the_ownership_detail_line_is_confined_to_one_line(self, capsys) -> None:
        out = _list_output(
            capsys,
            _job(
                session_key="dashboard:chat-3\nreal",
                created_by="import:slack\x1b[2J\nthread: 1",
                managed_by="app\x07",
            ),
        )
        assert len(out.splitlines()) == 2, out
        assert "\x1b" not in out
        assert "\x07" not in out
        assert "\\x0a" in out

    def test_a_tab_cell_is_rendered_instead_of_breaking_the_columns(self, capsys) -> None:
        out = _list_output(capsys, _job(message="before\tafter"))
        assert "\t" not in out
        assert "\\x09" in out


class TestCronListMessageCap:
    """The 60-character cap counts printed characters and keeps escapes whole."""

    def test_a_cut_never_lands_inside_an_escaped_control(self) -> None:
        # 59 plain characters then a newline: the escaped newline starts at the
        # 60th printed character, so a blind cut would print a bare ``\x0``.
        cleaned = _cron_list_cell("a" * 59 + "\n" + "rest")
        assert _cron_list_clip(cleaned, 60) == "a" * 59

    def test_the_cap_bounds_the_printed_message(self) -> None:
        assert len(_cron_list_clip(_cron_list_cell("b" * 500), 60)) == 60
