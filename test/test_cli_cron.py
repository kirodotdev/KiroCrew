"""``kirocrew cron add``, ``update`` and ``remove``: the ``_cron`` handler and ``--agent`` parsing.

``TestCronCli`` pins that one locked ``add_job`` / ``update_job`` call carries every field the flags
set, that invalid input is refused, and how the ``--agent`` and ``--channel`` values are normalised;
``_add_job_kwargs`` is the full default kwarg set the ``add_job`` calls are compared with.
"""

import argparse
import sys
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)

from kiro_crew.cli_commands import _cron
from kiro_crew.cron import CronService


def _add_job_kwargs(**overrides):
    """The FULL kwarg set ``kirocrew cron add`` hands to ``CronService.add_job``.

    Every create field rides in the ONE locked ``add_job`` call -- there is no
    second unlocked ``_save()`` after it -- so a test that pins the call pins
    the whole set. Defaults here are an agent job on ``--every`` with nothing
    else given; a test overrides the fields its flags change.
    """
    kwargs = dict(
        every_secs=None,
        cron_expr=None,
        at_ts=None,
        delete_after_run=False,
        channel=None,
        approval_mode="",
        agent_id="",
        model="",
        silent=False,
        timezone="",
        hide_in_chat=False,
        folder_id="",
        command="",
        script="",
        persistent_session=True,
        minimal_context=False,
        timeout=0,
        timeout_secs=0,
        auto_pause_after_failures=None,
        alert_after_failures=None,
    )
    kwargs.update(overrides)
    return kwargs


class TestCronCli:
    @pytest.mark.parametrize("action", ["add", "update"])
    def test_auto_pause_after_help_documents_default_and_zero(self, action, monkeypatch, capsys):
        from kiro_crew.cli import main
        from kiro_crew.cron_service.model import _AUTO_PAUSE_MAX, _AUTO_PAUSE_THRESHOLD

        monkeypatch.setattr(sys, "argv", ["kirocrew", "cron", action, "--help"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        help_text = capsys.readouterr().out
        assert "--auto-pause-after" in help_text
        # The copy is derived from the model's constants, so a change to either
        # constant must show up here rather than leave the help text stale.
        # argparse wraps long help at column width; collapse whitespace so the
        # assertion does not depend on where the wrap falls.
        flat = " ".join(help_text.split())
        assert (
            f"(0..{_AUTO_PAUSE_MAX}, default {_AUTO_PAUSE_THRESHOLD}; 0 = never auto-pause. "
            f"With 0 or a limit above {_AUTO_PAUSE_THRESHOLD}, from the "
            f"{_AUTO_PAUSE_THRESHOLD}th consecutive failure each further run waits 5 "
            "minutes, doubling per failure up to one hour, until it succeeds or "
            "reaches a nonzero limit)" in flat
        )
        # 0 is agent-settable through MCP too, so the help must not claim otherwise.
        assert "through MCP" not in flat

    def test_auto_pause_after_omitted_builds_job_with_model_default(self):
        from kiro_crew.cron import build_job
        from kiro_crew.cron_service.model import _AUTO_PAUSE_THRESHOLD

        job = build_job("j", "msg", every_secs=60)
        assert job.auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD

    def test_cron_add_with_channel(self, tmp_path, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc"
            mock_job.name = "test"
            mock_job.schedule.kind = "every"
            mock_job.schedule.every_secs = 300
            mock_job.schedule.cron_expr = None
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="ops",
                message="check",
                every=300,
                cron_expr=None,
                channel="C0AP77JJSN6",
                approval_mode="",
                agent=None,
                silent=False,
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="ops",
                message="check",
                **_add_job_kwargs(every_secs=300, channel="C0AP77JJSN6"),
            )
        assert capsys.readouterr().out.startswith("Added job: abc (test) [")

    def test_cron_add_with_cron_expr_and_channel(self, tmp_path, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "def"
            mock_job.name = "daily"
            mock_job.schedule.kind = "cron"
            mock_job.schedule.every_secs = None
            mock_job.schedule.cron_expr = "0 9 * * 1-5"
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="daily",
                message="brief",
                every=None,
                cron_expr="0 9 * * 1-5",
                channel="C0APAPQ5GSY",
                approval_mode="",
                agent=None,
                silent=False,
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="daily",
                message="brief",
                **_add_job_kwargs(cron_expr="0 9 * * 1-5", channel="C0APAPQ5GSY"),
            )
        assert capsys.readouterr().out.startswith("Added job: def (daily) [")

    @pytest.mark.parametrize(
        "name, cron_expr, every, expected",
        [
            ("bad dow", "0 9 * * 8", None, "Error: Invalid cron expression: 0 9 * * 8"),
            ("n" * 501, None, 60, "Error: name exceeds max length 500"),
        ],
        ids=["cron_expr branch", "every branch"],
    )
    def test_cron_add_refuses_invalid_input(
        self, name, cron_expr, every, expected, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        args = argparse.Namespace(
            cron_action="add",
            name=name,
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
        assert "Traceback" not in err

    def test_cron_add_with_approval_mode(self, tmp_path, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "ghi"
            mock_job.name = "auto-job"
            mock_job.schedule.kind = "every"
            mock_job.schedule.every_secs = 600
            mock_job.schedule.cron_expr = None
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="auto-job",
                message="run unattended",
                every=600,
                cron_expr=None,
                channel=None,
                approval_mode="auto",
                agent=None,
                silent=False,
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="auto-job",
                message="run unattended",
                **_add_job_kwargs(every_secs=600, approval_mode="auto"),
            )
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli",
                operation="cron.add",
                outcome="allowed",
                source="cli",
                resources="job_id=ghi kind=agent approval_mode=auto agent=default silent=False",
            )
        assert capsys.readouterr().out.startswith("Added job: ghi (auto-job) [")

    def test_cron_add_with_silent(self, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "mno"
            mock_job.name = "quiet-job"
            mock_job.schedule.kind = "every"
            mock_job.schedule.every_secs = 300
            mock_job.schedule.cron_expr = None
            mock_job.schedule.at_ts = None
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="quiet-job",
                message="shh",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="",
                silent=True,
            )
            _cron(args)
            # silent rides in the ONE locked add_job call -- never a
            # post-create mutation followed by a second, unlocked _save().
            mock_svc.add_job.assert_called_once_with(
                name="quiet-job",
                message="shh",
                **_add_job_kwargs(every_secs=300, silent=True),
            )
            mock_svc._save.assert_not_called()
        assert capsys.readouterr().out.startswith("Added job: mno (quiet-job) [")

    def test_cron_update_approval_mode(self, tmp_path, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode="auto",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", approval_mode="auto")
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli",
                operation="cron.update",
                outcome="allowed",
                source="cli",
                resources="job_id=abc123 fields=approval_mode",
            )
        assert capsys.readouterr().out == "Updated job: abc123 (existing)\n"

    def test_cron_update_whitespace_channel_skipped(self, tmp_path, capsys):
        with patch("kiro_crew.cli_commands.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_svc.update_job.return_value = None
            args = argparse.Namespace(
                cron_action="update",
                job_id="job1",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel="   ",
                approval_mode=None,
            )
            _cron(args)
            out = capsys.readouterr().out
            assert "at least one field" in out

    def test_cron_update_bad_channel_exits_1_on_stderr(self, tmp_path, capsys):
        """A refused channel writes nothing and must not read as a success to a script."""
        with patch("kiro_crew.cli_commands.CronService") as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            args = argparse.Namespace(
                cron_action="update",
                job_id="job1",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel="!!",
                approval_mode=None,
            )
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            out, err = capsys.readouterr()
            assert "invalid channel ID format" in err
            assert out == ""
            mock_svc.update_job.assert_not_called()

    def test_cron_update_every_and_cron_exclusive(self, tmp_path, capsys):
        with patch("kiro_crew.cli_commands.CronService"):
            args = argparse.Namespace(
                cron_action="update",
                job_id="job1",
                name=None,
                message=None,
                every_secs=300,
                cron_expr="0 9 * * *",
                channel=None,
                approval_mode=None,
            )
            _cron(args)
            out = capsys.readouterr().out
            assert "not both" in out

    def test_cron_update_not_found(self, tmp_path, capsys):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.update_job.return_value = None
            args = argparse.Namespace(
                cron_action="update",
                job_id="nonexist",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode="auto",
            )
            # A missing job exits 1, like remove/pause/resume/trigger.
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            assert "nonexist" in capsys.readouterr().out
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli",
                operation="cron.update",
                outcome="not_found",
                source="cli",
                resources="job_id=nonexist reason=not_found",
            )

    # ── --agent flag on cron add and update ──

    def _make_add_job_mock(
        self, *, job_id: str = "ag1", every_secs: int | None = 600, cron_expr: str | None = None
    ) -> MagicMock:
        mock_job = MagicMock()
        mock_job.id = job_id
        mock_job.name = "test"
        mock_job.schedule.kind = "cron" if cron_expr else "every"
        mock_job.schedule.every_secs = every_secs
        mock_job.schedule.cron_expr = cron_expr
        mock_job.schedule.at_ts = None
        mock_job.agent_id = ""
        return mock_job

    def test_cron_add_with_agent_every(self, tmp_path, capsys):
        """--agent on `cron add` with --every sets job.agent_id, persists, and audits."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(job_id="ag1", every_secs=600)
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="c360",
                message="check pipeline",
                every=600,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="customer360-code-agent",
            )
            _cron(args)
            # agent_id rides in the ONE locked add_job call; the old
            # mutate-then-unlocked-_save() second write is gone.
            mock_svc.add_job.assert_called_once_with(
                name="c360",
                message="check pipeline",
                **_add_job_kwargs(every_secs=600, agent_id="customer360-code-agent"),
            )
            mock_svc._save.assert_not_called()
            # Audit log includes agent (permission-relevant: picks
            # which sandboxed subprocess executes the job).
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli",
                operation="cron.add",
                outcome="allowed",
                source="cli",
                resources=(
                    "job_id=ag1 kind=agent approval_mode=default "
                    "agent=customer360-code-agent silent=False"
                ),
            )
        assert capsys.readouterr().out.startswith("Added job: ag1 (test) [")

    def test_cron_add_with_agent_cron_expr(self, tmp_path, capsys):
        """--agent on `cron add` with --cron sets job.agent_id and persists."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(
                job_id="ag2", every_secs=None, cron_expr="0 9 * * 1-5"
            )
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="briefing",
                message="run briefing",
                every=None,
                cron_expr="0 9 * * 1-5",
                channel=None,
                approval_mode="",
                agent="ea-briefing",
            )
            _cron(args)
            mock_svc.add_job.assert_called_once_with(
                name="briefing",
                message="run briefing",
                **_add_job_kwargs(cron_expr="0 9 * * 1-5", agent_id="ea-briefing"),
            )
            mock_svc._save.assert_not_called()
        assert capsys.readouterr().out.startswith("Added job: ag2 (test) [")

    def test_cron_add_without_agent_does_not_save(self, tmp_path):
        """Empty/omitted --agent leaves job.agent_id untouched, no extra _save."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(job_id="ag3", every_secs=300)
            mock_svc.add_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="add",
                name="basic",
                message="hi",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="",
            )
            _cron(args)
            assert mock_job.agent_id == ""
            mock_svc._save.assert_not_called()

    def test_cron_add_agent_whitespace_stripped(self, tmp_path, monkeypatch):
        """Whitespace-only --agent is treated as omitted (no agent_id set)."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        with patch("kiro_crew.cli_commands.sel"):
            args = argparse.Namespace(
                cron_action="add",
                name="basic",
                message="hi",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="   ",
            )
            _cron(args)
        (job,) = CronService(base_dir=tmp_path).list_jobs(include_disabled=True)
        assert (job.name, job.agent_id) == ("basic", "")

    def test_cron_update_with_agent(self, tmp_path, capsys):
        """--agent on `cron update` passes agent_id kwarg to update_job."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent="oncall-agent",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", agent_id="oncall-agent")
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli",
                operation="cron.update",
                outcome="allowed",
                source="cli",
                resources="job_id=abc123 fields=agent_id agent=oncall-agent",
            )
        assert capsys.readouterr().out == "Updated job: abc123 (existing)\n"

    def test_cron_update_agent_empty_resets(self, tmp_path, capsys):
        """--agent '' on update resets agent_id to default (mirrors MCP cron_update)."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent="",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", agent_id="")
            audit = mock_sel.return_value.log_api_access.call_args.kwargs
            assert audit["resources"] == "job_id=abc123 fields=agent_id agent=default"
        assert capsys.readouterr().out == "Updated job: abc123 (existing)\n"

    def test_cron_update_agent_omitted_skipped(self, tmp_path, capsys, monkeypatch):
        """When --agent is omitted (None), the job's agent is left as it was."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        existing = CronService(base_dir=tmp_path).add_job(
            name="existing", message="hi", every_secs=300, agent_id="oncall-agent"
        )
        with patch("kiro_crew.cli_commands.sel"):
            args = argparse.Namespace(
                cron_action="update",
                job_id=existing.id,
                name="renamed",
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent=None,
            )
            _cron(args)
        (job,) = CronService(base_dir=tmp_path).list_jobs(include_disabled=True)
        assert (job.name, job.agent_id) == ("renamed", "oncall-agent")
        assert capsys.readouterr().out == f"Updated job: {existing.id} (renamed)\n"

    def test_cron_add_invalid_agent_name_rejected(self, tmp_path, capsys):
        """Bad-format --agent on add is rejected with sys.exit(1) before any add_job call."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            args = argparse.Namespace(
                cron_action="add",
                name="bad",
                message="hi",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                # Every character is an admitted one; only the trailing '-' is refused.
                agent="research-",
            )
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            mock_svc.add_job.assert_not_called()
            mock_svc._save.assert_not_called()
            err = capsys.readouterr().err
            assert "invalid agent name (expected 1-64 letters" in err
            assert "starting and ending with a letter or digit" in err

    def test_cron_update_invalid_agent_name_rejected(self, tmp_path, capsys):
        """Bad-format --agent on update is rejected with sys.exit(1) before any update_job call."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                # Every character is an admitted one; only the trailing '-' is refused.
                agent="research-",
            )
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            mock_svc.update_job.assert_not_called()
            err = capsys.readouterr().err
            assert "invalid agent name (expected 1-64 letters" in err
            assert "starting and ending with a letter or digit" in err

    def test_cron_update_agent_whitespace_stripped(self, tmp_path, capsys):
        """Whitespace around --agent on update is stripped before forwarding to update_job."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = MagicMock()
            mock_job.id = "abc123"
            mock_job.name = "existing"
            mock_svc.update_job.return_value = mock_job
            args = argparse.Namespace(
                cron_action="update",
                job_id="abc123",
                name=None,
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent="  oncall-agent  ",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", agent_id="oncall-agent")
        assert capsys.readouterr().out == "Updated job: abc123 (existing)\n"

    def test_cli_argparse_cron_add_agent_flag(self) -> None:
        """`kirocrew cron add ... --agent NAME` parses into args.agent."""
        import sys

        argv = [
            "kirocrew",
            "cron",
            "add",
            "daily-briefing",
            "Run my morning briefing",
            "--cron",
            "0 9 * * 1-5",
            "--agent",
            "ea-briefing",
        ]
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli_commands._cron") as mock_cron:
            from kiro_crew.cli import main

            main()
            mock_cron.assert_called_once()
            ns = mock_cron.call_args[0][0]
            assert ns.cron_action == "add"
            assert ns.name == "daily-briefing"
            assert ns.agent == "ea-briefing"

    def test_cli_argparse_cron_add_no_agent_default_empty(self) -> None:
        """Omitting --agent on `cron add` leaves args.agent as empty string."""
        import sys

        argv = [
            "kirocrew",
            "cron",
            "add",
            "basic",
            "hello",
            "--every",
            "300",
        ]
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli_commands._cron") as mock_cron:
            from kiro_crew.cli import main

            main()
            ns = mock_cron.call_args[0][0]
            assert ns.agent == ""

    def test_cli_argparse_cron_update_agent_flag(self) -> None:
        """`kirocrew cron update <id> --agent NAME` parses into args.agent."""
        import sys

        argv = [
            "kirocrew",
            "cron",
            "update",
            "abc123",
            "--agent",
            "oncall-agent",
        ]
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli_commands._cron") as mock_cron:
            from kiro_crew.cli import main

            main()
            ns = mock_cron.call_args[0][0]
            assert ns.cron_action == "update"
            assert ns.job_id == "abc123"
            assert ns.agent == "oncall-agent"

    def test_cli_argparse_cron_update_no_agent_default_none(self) -> None:
        """Omitting --agent on `cron update` leaves args.agent as None (skip)."""
        import sys

        argv = [
            "kirocrew",
            "cron",
            "update",
            "abc123",
            "--name",
            "renamed",
        ]
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli_commands._cron") as mock_cron:
            from kiro_crew.cli import main

            main()
            ns = mock_cron.call_args[0][0]
            assert ns.agent is None

    def test_cron_remove_emits_sel_audit(self, capsys):
        # Single-job delete must be SEL-audited like cron.add/cron.update:
        # after the job vanishes from crons.json the audit trail is the only
        # way to tell a deliberate delete from data loss. The audit is
        # remove_job's (actor/source below), so the CLI must not write a second.
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.remove_job.return_value = True
            args = argparse.Namespace(cron_action="remove", job_id="abc123")
            _cron(args)
            mock_svc.remove_job.assert_called_once_with("abc123", actor="cli", source="cli")
            mock_sel.return_value.log_api_access.assert_not_called()
        assert capsys.readouterr().out == "Removed job: abc123\n"

    def test_cron_remove_not_found_exits_1_on_stderr(self, capsys):
        # A miss must fail like pause/resume do, or `cron remove x && next`
        # runs `next` after nothing was removed.
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.remove_job.return_value = False
            args = argparse.Namespace(cron_action="remove", job_id="ghost")
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            mock_svc.remove_job.assert_called_once_with("ghost", actor="cli", source="cli")
            mock_sel.return_value.log_api_access.assert_not_called()
        assert exc.value.code == 1
        out = capsys.readouterr()
        assert out.out == ""
        assert out.err == "Job not found: ghost\n"

    def test_cron_remove_succeeds_when_audit_raises(self, capsys):
        # The first sel() of a process constructs the log and can raise; the
        # job is already removed by then, so the command must still report the
        # completed delete instead of crashing.
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.remove_job.return_value = True
            args = argparse.Namespace(cron_action="remove", job_id="abc123")
            _cron(args)
            out = capsys.readouterr()
            assert "Removed job: abc123" in out.out
            assert out.err == ""
            mock_sel.assert_not_called()
