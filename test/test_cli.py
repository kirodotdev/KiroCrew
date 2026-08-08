"""Tests for CLI module."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
import types
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cli_commands import _cron
from kiro_crew.cli_doctor import _doctor
from kiro_crew.cli_server import _update


async def _noop_probe_server(server):
    """Default probe stub for tests that call ``_doctor()`` but aren't
    specifically exercising the MCP handshake. Marks the target healthy
    so doctor renders the MCP section cleanly without spawning a real
    child process.

    Tests that care about specific probe outcomes (success with tool
    count, failure with stderr, etc.) build their own probe mocks via
    ``TestDoctorMcpTools._mock_probe``.
    """
    server.status = "ok"
    server.tools = []
    return server


def _write_agent_config(path: Path, *, tools: list[str], allowed: list[str], servers: dict) -> None:
    """Write a ``kirocrew.json`` agent config with the given managed
    servers + tool references. Typed keyword arguments make it obvious
    which fields each test cares about.
    """
    path.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "tools": tools,
                "allowedTools": allowed,
                "mcpServers": servers,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _healthy_agent_file(path: Path) -> None:
    """Write a ``kirocrew.json`` whose managed MCP servers are all present
    so ``_doctor()``'s MCP section passes its static config check and only
    the (mocked) live probe remains. Used by doctor tests that exercise an
    unrelated section and must not trip the MCP exit path on an empty config.

    The server set is DERIVED from the same registry ``cli_doctor`` iterates
    (``mcp_cleanup.KIROCREW_BIN_MCP_SERVERS``) rather than hardcoded: doctor
    reports every managed server missing from ``mcpServers`` as an issue and
    exits 1, so a literal list here silently rots the moment a managed server is
    added — which is exactly how these fixtures broke when ``kirocrew-computer``
    landed.
    """
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

    refs = [f"@{name}" for name in KIROCREW_BIN_MCP_SERVERS]
    _write_agent_config(
        path,
        tools=refs,
        allowed=refs,
        servers={
            name: {
                "command": "/usr/local/bin/kirocrew",
                # The subcommand is the server name minus the "kirocrew-" prefix
                # ("kirocrew-core" -> "mcp-core"), matching the real invocation.
                "args": [f"mcp-{name.split('-', 1)[1]}"],
            }
            for name in KIROCREW_BIN_MCP_SERVERS
        },
    )


def _pin_default_config(monkeypatch) -> None:
    """Make ``_doctor()``'s config read hermetic for doctor tests.

    ``_doctor()`` calls the real ``KiroCrewConfig.load()`` / ``load_credentials()``,
    which read the shared ``~/.kirocrew`` config at runtime. ``KiroCrewConfig.save()``
    writes that same shared path non-atomically, so under ``pytest -n auto`` a
    concurrent worker's config write races these reads: a polluted/foreign config
    flips a check and ``_doctor()`` exits 1. xdist worker interleaving differs per
    interpreter, so the flake surfaced only on python3.10. Pin both to a pristine
    default (Slack-less, STT disabled) so doctor runs are deterministic and isolated.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cls()))
    monkeypatch.setattr(KiroCrewConfig, "load_credentials", lambda self: {})


class TestDoctor:
    @pytest.fixture(autouse=True)
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    @pytest.fixture(autouse=True)
    def _hermetic_embeddings_runtime(self, monkeypatch):
        """Pin the vendored-runtime probe host-independently: the brazil
        interpreter on a Mac can be darwin/x86_64 under Rosetta, where the
        vendored libs legitimately do not exist (designed degradation)."""
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(_doc, "model_file_present", lambda path=None: False)

    def test_doctor_with_kiro(self, tmp_path):
        agent_file = tmp_path / "kirocrew.json"
        # A minimally healthy agent config so doctor walks the whole MCP
        # section cleanly and doesn't exit on "missing from mcpServers".
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            _doctor()

    def test_doctor_windows_missing_whisper_ffmpeg_is_non_fatal(self, tmp_path, monkeypatch):
        """On Windows, STT ships enabled-by-default but whisper/ffmpeg are not
        dependencies there. Reporting them as hard issues made `doctor` exit 1
        on a healthy install and broke the guide's `doctor && gateway` chain, so
        they must be non-fatal notes: doctor exits 0."""
        import kiro_crew.cli_doctor as _doc
        from kiro_crew.config.loader import KiroCrewConfig

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        monkeypatch.setattr(_doc.platform_compat, "IS_WINDOWS", True)

        # STT enabled with whisper provider (the shipped default), but neither
        # binary present — the autouse fixture pins STT OFF, so re-enable here.
        def _cfg_with_stt() -> KiroCrewConfig:
            cfg = KiroCrewConfig()
            cfg.stt.enabled = True
            cfg.stt.provider = "whisper"
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _cfg_with_stt()))
        monkeypatch.setattr(_doc, "_find_whisper", lambda path=None: None)
        monkeypatch.setattr(_doc, "ensure_ffmpeg_in_path", lambda: None)

        def _which(binary, **_kw):
            # Everything resolves EXCEPT the two STT binaries.
            if binary in ("whisper", "ffmpeg"):
                return None
            return f"C:\\tools\\{binary}.exe"

        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=_which),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            # Must NOT raise SystemExit(1): the two STT gaps are notes on Windows.
            _doctor()

    def test_doctor_reports_platform_boot_error_without_crashing(self, tmp_path, capsys):
        """A PlatformCompositionError from boot must be REPORTED by the doctor,
        not crash it — the doctor is the tool that diagnoses a broken setup, so
        it has to survive the very failure it explains."""
        from kiro_crew.platform import PlatformCompositionError

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        boot_err = PlatformCompositionError(
            "profile=amazon resolved no companion; set KIROCREW_PROFILE=standalone"
        )
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            # Must not raise — and must exit 1 since a composition failure is a
            # blocking issue.
            with pytest.raises(SystemExit) as exc:
                _doctor(platform_boot_error=boot_err)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "composition failed" in out
        assert "KIROCREW_PROFILE=standalone" in out

    def test_doctor_without_kiro(self, tmp_path):
        with (
            patch("kiro_crew.cli_doctor.shutil.which", return_value=None),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
        ):
            try:
                _doctor()
            except SystemExit as e:
                assert e.code == 1

    @patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 1234 5.6.7.8 22"})
    def test_doctor_remote_shows_ssh_tunnel_hint(self, tmp_path, capsys):
        agent_file = tmp_path / "kirocrew.json"
        agent_file.write_text("{}")
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=False),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.machine_hostname", return_value="myhost"),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "ssh -NL" in out

    def test_doctor_slack_workspace_allowed_ok(self, tmp_path, capsys):
        """Slack configured + the bot token in the configured workspace
        allowlist -> doctor reports the workspace OK. validate_enterprise is
        mocked True so no live slack_sdk auth.test fires (its own logic is
        covered by test_enterprise.py); this covers the doctor-side success
        branch."""
        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        slack_creds = {
            "SLACK_APP_TOKEN": "xapp-test",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "KIROCREW_OWNER_ID": "U123",
        }
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.cli_doctor.KiroCrewConfig.load_credentials", return_value=slack_creds),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True) as mock_ve,
        ):
            _doctor()
        out = capsys.readouterr().out
        assert "✅ configured" in out
        assert "  workspace:   ✅ allowed" in out
        mock_ve.assert_called_once()

    def test_doctor_slack_workspace_not_allowed_flags_issue(self, tmp_path, capsys):
        """Slack configured but the bot token NOT in the configured workspace
        allowlist is a blocking issue: doctor prints the warning and exits 1.
        validate_enterprise is mocked False so no live auth.test fires; covers
        the doctor-side failure branch + the resulting sys.exit(1)."""
        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        slack_creds = {"SLACK_APP_TOKEN": "xapp-test", "SLACK_BOT_TOKEN": "xoxb-test"}
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.cli_doctor.KiroCrewConfig.load_credentials", return_value=slack_creds),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=False),
        ):
            with pytest.raises(SystemExit) as exc:
                _doctor()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "❌ not in configured workspace allowlist" in out


class TestSetupWorkspaceDir:
    """Tests for _setup_workspace_dir prompt default and label logic."""

    def test_uses_saved_path_as_default(self, tmp_path, monkeypatch):
        ws_file = tmp_path / "workspace_dir"
        ws_file.write_text("/custom/workspace\n")
        custom_dir = tmp_path / "custom"
        monkeypatch.setattr("kiro_crew.cli_setup._workspace_dir_file", lambda: ws_file)
        with patch("builtins.input", return_value=str(custom_dir)) as mock_input:
            from kiro_crew.cli_setup import _setup_workspace_dir

            _setup_workspace_dir()
        prompt = mock_input.call_args[0][0]
        assert "/custom/workspace" in prompt

    def test_shows_configured_label_when_saved(self, tmp_path, monkeypatch, capsys):
        ws_file = tmp_path / "workspace_dir"
        ws_file.write_text("/custom/workspace\n")
        custom_dir = tmp_path / "custom"
        monkeypatch.setattr("kiro_crew.cli_setup._workspace_dir_file", lambda: ws_file)
        with patch("builtins.input", return_value=str(custom_dir)):
            from kiro_crew.cli_setup import _setup_workspace_dir

            _setup_workspace_dir()
        output = capsys.readouterr().out
        assert "Configured:" in output

    def test_shows_default_label_when_no_saved(self, tmp_path, monkeypatch, capsys):
        ws_file = tmp_path / "no_such_file"
        custom_dir = tmp_path / "ws"
        monkeypatch.setattr("kiro_crew.cli_setup._workspace_dir_file", lambda: ws_file)
        with patch("builtins.input", return_value=str(custom_dir)):
            from kiro_crew.cli_setup import _setup_workspace_dir

            _setup_workspace_dir()
        output = capsys.readouterr().out
        assert "Default:" in output


# Common patches for _update tests — simulate a source tree with a git pull that has changes
_UPDATE_PATCHES = {
    "KIROCREW_PROJECT_DIR": "/fake/proj",
}


def _patch_path():
    """Mock Path so .git check passes, .install-method is absent, and .brazil dir exists."""
    mock_git_dir = MagicMock(
        is_dir=MagicMock(return_value=True), exists=MagicMock(return_value=True)
    )
    mock_install_method = MagicMock(is_file=MagicMock(return_value=False))
    mock_brazil_dir = MagicMock(is_dir=MagicMock(return_value=True))

    def _truediv(self, key):
        if key == ".install-method":
            return mock_install_method
        if key == ".brazil":
            return mock_brazil_dir
        return mock_git_dir

    mock_path_inst = MagicMock()
    mock_path_inst.__truediv__ = _truediv
    mock_path_inst.parent.parent = MagicMock()
    mock_path_inst.parent.parent.__truediv__ = _truediv
    mock_path_inst.parent.parent.__str__ = lambda self: "/fake/ws"
    return patch("kiro_crew.cli_server.Path", return_value=mock_path_inst)


class TestUpdateFailures:
    """Tests for _update build-step failure handling (public pip/git flow).

    The Brazil ``brazil-build`` step was removed during de-Amazoning; the
    public update flow is git fetch/reset + npm build + ``pip install -e .``.
    A non-zero return code from a critical step exits with code 1.
    """

    @patch.dict("os.environ", _UPDATE_PATCHES)
    def test_git_fetch_failure_exits(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if cmd and "rev-parse" in cmd:
                m.stdout = "beta-braveheart"
            if cmd and "fetch" in cmd:
                m.returncode = 1
                m.stderr = "network error"
            return m

        with _patch_path(), patch("subprocess.run", side_effect=_side_effect):
            try:
                _update()
                assert False, "Expected SystemExit"
            except SystemExit as e:
                assert e.code == 1

    @patch.dict("os.environ", _UPDATE_PATCHES)
    def test_pip_install_failure_exits(self):
        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if cmd and "rev-parse" in cmd:
                m.stdout = "beta-braveheart"
            # git diff --quiet returns 1 when there ARE new commits
            if cmd and "diff" in cmd and "--quiet" in cmd:
                m.returncode = 1
            # pip install -e . fails
            if cmd and "pip" in cmd and "install" in cmd:
                m.returncode = 1
                m.stderr = "build failed"
            return m

        with (
            _patch_path(),
            patch("kiro_crew.cli_server.shutil.which", return_value=None),
            patch("kiro_crew.cli_server.build_frontend_sync"),
            patch("kiro_crew.cli._ensure_node"),
            patch("subprocess.run", side_effect=_side_effect),
        ):
            try:
                _update()
                assert False, "Expected SystemExit"
            except SystemExit as e:
                assert e.code == 1


class TestCronCli:
    def test_cron_add_with_channel(self, tmp_path):
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
                every_secs=300,
                channel="C0AP77JJSN6",
                approval_mode="",
            )

    def test_cron_add_with_cron_expr_and_channel(self, tmp_path):
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
                cron_expr="0 9 * * 1-5",
                channel="C0APAPQ5GSY",
                approval_mode="",
            )

    def test_cron_add_with_approval_mode(self, tmp_path):
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
                every_secs=600,
                channel=None,
                approval_mode="auto",
            )
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli",
                operation="cron.add",
                outcome="allowed",
                source="cli",
                resources="job_id=ghi approval_mode=auto agent=default silent=False",
            )

    def test_cron_add_with_silent(self):
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
            mock_svc.add_job.assert_called_once_with(
                name="quiet-job",
                message="shh",
                every_secs=300,
                channel=None,
                approval_mode="",
            )
            # silent is set via post-create mutation, mirroring agent_id
            assert mock_job.silent is True
            mock_svc._save.assert_called_once()

    def test_cron_update_approval_mode(self, tmp_path):
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
            _cron(args)
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

    def test_cron_add_with_agent_every(self, tmp_path):
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
            mock_svc.add_job.assert_called_once_with(
                name="c360",
                message="check pipeline",
                every_secs=600,
                channel=None,
                approval_mode="",
            )
            assert mock_job.agent_id == "customer360-code-agent"
            mock_svc._save.assert_called_once()
            # Audit log includes agent (permission-relevant: picks
            # which sandboxed subprocess executes the job).
            mock_sel.return_value.log_api_access.assert_called_once_with(
                caller="cli",
                operation="cron.add",
                outcome="allowed",
                source="cli",
                resources="job_id=ag1 approval_mode=default agent=customer360-code-agent silent=False",
            )

    def test_cron_add_with_agent_cron_expr(self, tmp_path):
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
                cron_expr="0 9 * * 1-5",
                channel=None,
                approval_mode="",
            )
            assert mock_job.agent_id == "ea-briefing"
            mock_svc._save.assert_called_once()

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

    def test_cron_add_agent_whitespace_stripped(self, tmp_path):
        """Whitespace-only --agent is treated as omitted (no agent_id set)."""
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel"),
        ):
            mock_svc = mock_svc_cls.return_value
            mock_job = self._make_add_job_mock(job_id="ag4", every_secs=300)
            mock_svc.add_job.return_value = mock_job
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
            assert mock_job.agent_id == ""
            mock_svc._save.assert_not_called()

    def test_cron_update_with_agent(self, tmp_path):
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

    def test_cron_update_agent_empty_resets(self, tmp_path):
        """--agent '' on update resets agent_id to default (mirrors MCP cron_update)."""
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
                agent="",
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", agent_id="")

    def test_cron_update_agent_omitted_skipped(self, tmp_path, capsys):
        """When --agent is omitted (None), agent_id is not in update_job kwargs."""
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
                name="renamed",
                message=None,
                every_secs=None,
                cron_expr=None,
                channel=None,
                approval_mode=None,
                agent=None,
            )
            _cron(args)
            mock_svc.update_job.assert_called_once_with("abc123", name="renamed")
            assert "agent_id" not in mock_svc.update_job.call_args.kwargs

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
                agent="bad name!",
            )
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            mock_svc.add_job.assert_not_called()
            mock_svc._save.assert_not_called()
            assert "invalid agent name" in capsys.readouterr().err.lower()

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
                agent="bad name!",
            )
            with pytest.raises(SystemExit) as exc:
                _cron(args)
            assert exc.value.code == 1
            mock_svc.update_job.assert_not_called()
            assert "invalid agent name" in capsys.readouterr().err.lower()

    def test_cron_update_agent_whitespace_stripped(self, tmp_path):
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
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli._cron") as mock_cron:
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
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli._cron") as mock_cron:
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
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli._cron") as mock_cron:
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
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli._cron") as mock_cron:
            from kiro_crew.cli import main

            main()
            ns = mock_cron.call_args[0][0]
            assert ns.agent is None


class TestPortEnvValidatedAtEntry:
    """`main()` rejects an unusable KIROCREW_PORT before any subcommand runs.

    Type alone is not enough. 70000 parses as an int, so a type-only check let
    `KIROCREW_PORT=70000 kirocrew service install` bake an unbindable port into
    a service definition and report success -- leaving a gateway that dies on
    every start, with the failure surfacing far from its cause.

    Rejecting here rather than in the consumer keeps ONE policy for every entry
    point. It must reject rather than silently drop: dropping would install the
    DEFAULT port while the operator believes they set theirs.
    """

    def test_out_of_range_port_exits_before_dispatch(self, monkeypatch, capsys):
        import sys

        for bad in ("70000", "0", "-1"):
            monkeypatch.setenv("KIROCREW_PORT", bad)
            dispatched = []
            with patch.object(sys, "argv", ["kirocrew", "cron", "list"]), patch(
                "kiro_crew.cli._cron", lambda _ns: dispatched.append(True)
            ):
                from kiro_crew.cli import main

                with pytest.raises(SystemExit) as exc:
                    main()
            assert exc.value.code == 1, bad
            assert not dispatched, f"{bad} reached the subcommand"
            assert "1-65535" in capsys.readouterr().err

    def test_in_range_port_is_accepted(self, monkeypatch):
        import sys

        monkeypatch.setenv("KIROCREW_PORT", "5477")
        dispatched = []
        with patch.object(sys, "argv", ["kirocrew", "cron", "list"]), patch(
            "kiro_crew.cli._cron", lambda _ns: dispatched.append(True)
        ):
            from kiro_crew.cli import main

            main()
        assert dispatched == [True]


class TestSandboxActiveMarkerCleared:
    """cli.main() must drop an INHERITED KIROCREW_SANDBOX_ACTIVE marker.

    The marker is trusted by sandbox.wrap_argv to skip re-wrapping (nested
    passthrough); its only legitimate setter is the namespace launcher's
    in-sandbox main() (a separate process). A value present at the CLI
    entrypoint can only be forged/inherited from the gateway's environment, so
    honoring it would be a full sandbox bypass for every agent/tool spawn.
    """

    def test_main_clears_inherited_sandbox_active_marker(self, monkeypatch):
        import os
        import sys

        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        # A trivial subcommand so main() dispatches and returns cleanly; assert
        # the marker was popped before dispatch (patch the target to observe).
        argv = ["kirocrew", "cron", "list"]
        seen = {}

        def _capture(_ns):
            seen["marker"] = os.environ.get("KIROCREW_SANDBOX_ACTIVE")

        with patch.object(sys, "argv", argv), patch("kiro_crew.cli._cron", _capture):
            from kiro_crew.cli import main

            main()
        assert seen.get("marker") is None
        assert os.environ.get("KIROCREW_SANDBOX_ACTIVE") is None


class TestDirectCliOverrideAttestation:
    def test_agent_command_pins_override_before_jail_gate(self, tmp_path, monkeypatch):
        """A direct CLI agent command must pin its override before any re-exec or spawn."""
        from kiro_crew import cli, kiro_prerequisite

        executable = tmp_path / "kiro-cli"
        executable.write_bytes(b"direct CLI override")
        executable.chmod(0o700)
        data_home = tmp_path / "data"
        observed = {}

        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        monkeypatch.setenv("KIROCREW_KIRO_BIN", str(executable))
        monkeypatch.setattr(cli, "boot_platform", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "chat", "--no-jail"])
        # Exercise the POSIX-only override contract on every CI platform.
        register_attestation = kiro_prerequisite.register_process_start_override_attestation
        monkeypatch.setattr(
            kiro_prerequisite,
            "register_process_start_override_attestation",
            lambda: register_attestation(
                platform_name="linux",
                environ=cli.os.environ,
            ),
        )

        def inspect_before_provider(_command, _no_jail):
            # Read the PINNED attestation (not a fresh hash) so this asserts the
            # override was recorded before the gate, not merely hashable at it.
            observed["digest"] = kiro_prerequisite._OPERATOR_OVERRIDE_ATTESTATIONS.get(
                os.path.normcase(str(executable))
            )
            raise SystemExit(0)

        monkeypatch.setattr(cli, "_jail_reexec_gate", inspect_before_provider)

        with pytest.raises(SystemExit, match="0"):
            cli.main()

        assert observed["digest"] == hashlib.sha256(executable.read_bytes()).hexdigest()


class TestSetupTimezone:
    def test_auto_detect_from_tz_env(self, monkeypatch):
        """TZ env var is checked before /etc/localtime."""
        from kiro_crew.cli_setup import _detect_system_timezone

        monkeypatch.setenv("TZ", "Europe/London")
        assert _detect_system_timezone() == "Europe/London"

    def test_auto_detect_tz_env_with_colon(self, monkeypatch):
        """TZ env var with glibc colon prefix is handled."""
        from kiro_crew.cli_setup import _detect_system_timezone

        monkeypatch.setenv("TZ", ":America/Chicago")
        assert _detect_system_timezone() == "America/Chicago"

    def test_windows_uses_tzlocal_when_no_posix_signal(self, monkeypatch):
        """On Windows (no TZ, no /etc/localtime), the zone comes from tzlocal —
        otherwise the product silently ran in UTC and cron fired hours off."""
        import sys
        import types

        from kiro_crew import cli_setup

        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(cli_setup.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(cli_setup.Path, "is_symlink", lambda self: False)
        fake_tzlocal = types.SimpleNamespace(
            get_localzone_name=lambda: "America/Los_Angeles"
        )
        monkeypatch.setitem(sys.modules, "tzlocal", fake_tzlocal)

        assert cli_setup._detect_system_timezone() == "America/Los_Angeles"

    def test_windows_tzlocal_missing_degrades_to_empty(self, monkeypatch):
        """A source checkout without tzlocal must skip-and-ask, never crash."""
        import builtins

        from kiro_crew import cli_setup

        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(cli_setup.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(cli_setup.Path, "is_symlink", lambda self: False)
        real_import = builtins.__import__

        def _no_tzlocal(name, *args, **kwargs):
            if name == "tzlocal":
                raise ImportError("no tzlocal")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_tzlocal)
        assert cli_setup._detect_system_timezone() == ""

    def test_input_or_skip_returns_none_on_empty_and_raises_on_eof(self, monkeypatch):
        """Empty input keeps the caller's default (returns None as the "skip"
        sentinel). A closed/piped stdin raises _SetupAborted so the wizard exits
        cleanly at the top level rather than tracebacking at the NEXT bare
        input() call in a later step."""
        from kiro_crew.cli_setup import _input_or_skip, _SetupAborted

        monkeypatch.setattr("builtins.input", lambda _p: "")
        assert _input_or_skip("tz: ") is None

        def _raise_eof(_prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        with pytest.raises(_SetupAborted):
            _input_or_skip("tz: ")

    def test_timezone_retry_eof_propagates_setup_aborted(self, tmp_path, monkeypatch):
        """EOF on any prompt inside a step propagates _SetupAborted so the
        top-level catch can exit cleanly with one line, rather than leaving the
        next step to traceback."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: cfg_file)

        from kiro_crew.cli_setup import _setup_timezone, _SetupAborted

        answers = iter(["Not/AZone"])

        def _input(_prompt):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        with patch("builtins.input", _input):
            with patch("kiro_crew.cli_setup._detect_system_timezone", return_value=""):
                with pytest.raises(_SetupAborted):
                    _setup_timezone()

        # Skipped: no timezone persisted.
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert not data.get("timezone")

    def test_auto_detect_from_symlink(self, tmp_path, monkeypatch):
        """When /etc/localtime is a symlink, timezone is auto-detected."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: cfg_file)

        from kiro_crew.cli_setup import _setup_timezone

        with patch("builtins.input", return_value="") as mock_input:
            with patch(
                "kiro_crew.cli_setup._detect_system_timezone",
                return_value="America/Los_Angeles",
            ):
                _setup_timezone()

        prompt = mock_input.call_args[0][0]
        assert "America/Los_Angeles" in prompt
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["timezone"] == "America/Los_Angeles"

    def test_manual_entry(self, tmp_path, monkeypatch):
        """When no auto-detect, user types timezone manually."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: cfg_file)

        from kiro_crew.cli_setup import _setup_timezone

        with patch("builtins.input", return_value="America/New_York"):
            with patch("kiro_crew.cli_setup._detect_system_timezone", return_value=""):
                _setup_timezone()

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["timezone"] == "America/New_York"

    def test_skip_on_empty_input(self, tmp_path, monkeypatch):
        """Empty input skips timezone setup."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: cfg_file)

        from kiro_crew.cli_setup import _setup_timezone

        with patch("builtins.input", return_value=""):
            with patch("kiro_crew.cli_setup._detect_system_timezone", return_value=""):
                _setup_timezone()

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "timezone" not in data

    def test_invalid_timezone_rejected(self, tmp_path, monkeypatch, capsys):
        """Invalid timezone is rejected, not saved."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: cfg_file)

        from kiro_crew.cli_setup import _setup_timezone

        with patch("builtins.input", return_value="Invalid/Timezone"):
            with patch("kiro_crew.cli_setup._detect_system_timezone", return_value=""):
                _setup_timezone()

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "timezone" not in data
        output = capsys.readouterr().out
        assert "Unknown timezone" in output

    def test_keeps_existing_on_enter(self, tmp_path, monkeypatch):
        """Re-running setup with existing timezone keeps it on Enter."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"timezone": "America/Chicago"}))
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: cfg_file)

        from kiro_crew.cli_setup import _setup_timezone

        with patch("builtins.input", return_value=""):
            _setup_timezone()

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert data["timezone"] == "America/Chicago"

    def test_corrupted_config_not_overwritten(self, tmp_path, monkeypatch, capsys):
        """Corrupted config file is not overwritten."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json {{{")
        monkeypatch.setattr("kiro_crew.cli_setup.config_path", lambda: cfg_file)

        from kiro_crew.cli_setup import _setup_timezone

        _setup_timezone()

        # File should be unchanged
        assert cfg_file.read_text(encoding="utf-8") == "not json {{{"
        output = capsys.readouterr().out
        assert "Could not read" in output


class TestGetAlias:
    """Tests for _get_alias."""

    def test_returns_user_env(self, monkeypatch):
        monkeypatch.setenv("USER", "testuser")
        from kiro_crew.cli_setup import _get_alias

        assert _get_alias() == "testuser"

    def test_falls_back_to_getlogin(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        with patch("os.getlogin", return_value="loginuser"):
            from kiro_crew.cli_setup import _get_alias

            assert _get_alias() == "loginuser"

    def test_falls_back_to_prompt(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        with (
            patch("os.getlogin", side_effect=OSError("no tty")),
            patch("builtins.input", return_value="prompted"),
        ):
            from kiro_crew.cli_setup import _get_alias

            assert _get_alias() == "prompted"

    def test_exits_when_no_alias(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        with (
            patch("os.getlogin", side_effect=OSError("no tty")),
            patch("builtins.input", return_value=""),
        ):
            from kiro_crew.cli_setup import _get_alias

            try:
                _get_alias()
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1


class TestManifest:
    """Tests for _manifest."""

    def _patch_template(
        self, content="name: KiroCrew-{{ALIAS}}\ndisplay_name: KiroCrew-{{ALIAS}}\n"
    ):
        """Patch importlib.resources.files to return a fake template."""
        mock_resource = MagicMock()
        mock_resource.joinpath.return_value.read_text.return_value = content
        return patch("kiro_crew.cli_setup._pkg_files", return_value=mock_resource)

    def test_renders_alias_to_stdout(self, capsys):
        with self._patch_template():
            from kiro_crew.cli_setup import _manifest

            _manifest(alias="alice")
        out = capsys.readouterr().out
        assert "KiroCrew-alice" in out
        assert "{{ALIAS}}" not in out

    def test_writes_to_output_file(self, tmp_path):
        out_file = tmp_path / "sub" / "out.yaml"
        with self._patch_template("name: KiroCrew-{{ALIAS}}\n"):
            from kiro_crew.cli_setup import _manifest

            _manifest(alias="bob", output=str(out_file))
        assert out_file.exists()
        assert "KiroCrew-bob" in out_file.read_text(encoding="utf-8")

    def test_creates_parent_dirs(self, tmp_path):
        out_file = tmp_path / "deep" / "nested" / "out.yaml"
        with self._patch_template("name: KiroCrew-{{ALIAS}}\n"):
            from kiro_crew.cli_setup import _manifest

            _manifest(alias="carol", output=str(out_file))
        assert out_file.exists()

    def test_exits_when_template_missing(self):
        mock_resource = MagicMock()
        mock_resource.joinpath.return_value.read_text.side_effect = FileNotFoundError
        with patch("kiro_crew.cli_setup._pkg_files", return_value=mock_resource):
            from kiro_crew.cli_setup import _manifest

            try:
                _manifest(alias="dave")
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_rejects_invalid_alias(self):
        from kiro_crew.cli_setup import _manifest

        for bad in ["a\nb", "foo:bar", "x{{y}}", "hello world"]:
            try:
                _manifest(alias=bad)
                assert False, f"should have exited for alias={bad!r}"
            except SystemExit as e:
                assert e.code == 1

    def test_url_flag_prints_creation_link(self, capsys):
        with self._patch_template("# comment\nname: KiroCrew-{{ALIAS}}\n"):
            from kiro_crew.cli_setup import _manifest

            _manifest(alias="alice", url=True)
        out = capsys.readouterr().out
        assert "https://api.slack.com/apps?new_app=1&manifest_yaml=" in out
        assert "KiroCrew-alice" in out  # alias substituted
        assert "%0A" in out  # newlines are URL-encoded
        assert "\nname:" not in out  # raw YAML not printed
        assert "%23" not in out  # comments stripped from URL


class TestLogout:
    """Tests for _logout CLI function."""

    def test_logout_success(self, tmp_path, monkeypatch):
        """Successful logout prints success message."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _logout(5476)  # Should not raise

    def test_logout_gateway_not_running(self, tmp_path, monkeypatch):
        """Missing secret file means gateway not running."""
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)

        from kiro_crew.cli_server import _logout

        try:
            _logout(5476)
            assert False, "should have exited"
        except SystemExit as e:
            assert e.code == 1

    def test_logout_http_error(self, tmp_path, monkeypatch):
        """HTTP error from gateway is handled."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)

        from kiro_crew.cli_server import _logout

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.HTTPError(None, 403, "Forbidden", {}, None),
        ):
            try:
                _logout(5476)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_logout_connection_error(self, tmp_path, monkeypatch):
        """Connection error means gateway not running."""
        secret_file = tmp_path / ".kirocrew" / ".local_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("test-secret")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        from kiro_crew.cli_server import _logout

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            try:
                _logout(5476)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1

    def test_logout_error_response(self, tmp_path, monkeypatch):
        """Error response from gateway is handled."""
        secret_file = tmp_path / ".kirocrew" / ".local_secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("test-secret")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": false, "error": "test error"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            try:
                _logout(5476)
                assert False, "should have exited"
            except SystemExit as e:
                assert e.code == 1


class TestStatus:
    """Tests for _status() HTTP error handling."""

    def _make_args(self, port=5476):
        return argparse.Namespace(port=port)

    def test_status_auth_required(self, capsys):
        """401/403 should report gateway as running with token auth."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.HTTPError(
                "http://127.0.0.1:5476/api/status", 403, "Forbidden", {}, None
            ),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "token auth" in out

    def test_status_other_http_error(self, capsys):
        """Non-auth HTTP errors should report gateway as running with code."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.HTTPError(
                "http://127.0.0.1:5476/api/status", 500, "Internal Server Error", {}, None
            ),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "HTTP 500" in out

    def test_status_connection_refused(self, capsys):
        """Connection refused should report gateway as not running."""
        from kiro_crew.cli_server import _status

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "not running" in out

    def test_status_success(self, capsys):
        """200 OK should display stats."""
        from kiro_crew.cli_server import _status

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "uptime": "1h 0m",
                "sessions": 2,
                "messages": 10,
                "tool_calls": 5,
                "subagents": 0,
                "crons": 1,
                "lessons": 3,
            }
        ).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "1h 0m" in out
        assert "Sessions" in out or "sessions" in out.lower()

    def test_status_unexpected_exception(self, capsys):
        """Non-network exceptions should report gateway as running with unexpected response."""
        from kiro_crew.cli_server import _status

        with patch("kiro_crew.cli_server.loopback_urlopen", side_effect=RuntimeError("unexpected")):
            _status(self._make_args())
        out = capsys.readouterr().out
        assert "running" in out
        assert "unexpected response" in out


class TestIsKirocrewProcess:
    """Tests for _is_kirocrew_process helper.

    It now verifies via ``platform_compat.process_command_line`` (cross-platform:
    Linux /proc, macOS ps, Windows WMI) rather than calling ``ps`` directly, so a
    process whose image name is ``python``/``python.exe`` (the venv kirocrew.exe
    re-exec) is still classified by its command line.
    """

    def _cmdline(self, value):
        return patch(
            "kiro_crew.cli_server.platform_compat.process_command_line", return_value=value
        )

    def test_returns_true_for_kirocrew(self):
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("python3 -m kiro_crew.dashboard"):
            assert _is_kirocrew_process(1234) is True

    def test_returns_true_for_kirocrew_binary(self):
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("/usr/bin/kirocrew start"):
            assert _is_kirocrew_process(1234) is True

    def test_returns_true_for_module_gateway_form(self):
        """Regression: the real service launch form
        ``python -m kiro_crew gateway`` must be recognized. Previously the
        matcher only accepted the dotted ``kiro_crew.gateway`` form, so
        ``kirocrew stop`` no-op'd on service installs.

        Patched through the cross-platform ``process_command_line`` seam (the
        Windows port routes _is_kirocrew_process through platform_compat rather
        than calling ``subprocess.check_output`` directly)."""
        from kiro_crew.cli_server import _is_kirocrew_process

        real = (
            "/Users/x/.toolbox/tools/kirocrew/3.1.0/bin/../python3.10/bin/"
            "python3.10 -m kiro_crew gateway\n"
        )
        with self._cmdline(real):
            assert _is_kirocrew_process(54842) is True

    def test_returns_true_for_windows_python_reexec(self):
        # The venv kirocrew.exe re-execs python.exe, so the gateway cmdline reads
        # `python.exe ...\Scripts\kirocrew.exe gateway` (or `-m kiro_crew gateway`).
        # The token-parser (_args_look_like_kirocrew) recognizes the console-script
        # basename ("kirocrew"/"kirocrew.exe") and the "-m kiro_crew gateway" module
        # form; assert both Windows re-exec shapes match.
        from kiro_crew.cli_server import _is_kirocrew_process

        with patch("kiro_crew.cli_server.platform_compat.IS_WINDOWS", True):
            with self._cmdline(
                r'"C:\Program Files\Python312\python.exe" '
                r'"D:\U\.kirocrew\.venv\Scripts\kirocrew.exe" gateway --no-open'
            ):
                assert _is_kirocrew_process(1234) is True
            with self._cmdline(r"C:\Python312\python.exe -m kiro_crew gateway"):
                assert _is_kirocrew_process(1234) is True

    def test_returns_false_for_unrelated(self):
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("nginx: worker process"):
            assert _is_kirocrew_process(1234) is False

    def test_returns_false_for_broad_match(self):
        """Editing a kirocrew file should NOT match — only gateway entry points."""
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline("vim /tmp/kirocrew-notes.txt"):
            assert _is_kirocrew_process(1234) is False

    def test_returns_false_when_cmdline_unavailable(self):
        # process_command_line returns "" on any failure (dead PID, WMI/ps error);
        # _is_kirocrew_process must then fail closed (False), never raise.
        from kiro_crew.cli_server import _is_kirocrew_process

        with self._cmdline(""):
            assert _is_kirocrew_process(1234) is False


class TestArgsLookLikeKirocrew:
    """Rigorous tests for the pure command-line classifier ``_args_look_like_kirocrew``.

    These exercise the structural parser directly (no ``subprocess`` mock needed),
    covering every real launch form plus adversarial near-misses that must NOT be
    matched — a false positive would let ``kirocrew stop`` SIGTERM an unrelated
    process bound to the port.
    """

    @pytest.mark.parametrize(
        "args",
        [
            # Module-invocation form (service spawn) — the regression fix.
            "/Users/x/.toolbox/tools/kirocrew/3.1.0/bin/../python3.10/bin/python3.10 -m kiro_crew gateway",
            "python3 -m kiro_crew gateway",
            "python -m kiro_crew dashboard",
            "python3.10 -m kiro_crew start",
            # Subcommand followed by trailing flags.
            "python -m kiro_crew gateway --no-open --port 7777",
            # Legacy dotted-submodule form.
            "python3 -m kiro_crew.gateway",
            "python3 -m kiro_crew.dashboard",
            # Console-script wrapper form.
            "/usr/local/bin/kirocrew gateway",
            "/Users/x/.toolbox/bin/kirocrew start",
            "kirocrew dashboard",
        ],
    )
    def test_matches_server_launch_forms(self, args):
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew(args) is True

    @pytest.mark.parametrize(
        "args",
        [
            "",  # empty command line
            "nginx: worker process",  # unrelated daemon on the port
            "vim /tmp/kirocrew-notes.txt",  # editing a file named kirocrew*
            "cat /var/log/kiro_crew_gateway.log",  # reading a kirocrew log file
            "python -m kiro_crew",  # bare module, no server subcommand
            "python -m kiro_crew run /tmp/spec.md",  # task runner — NOT a port-bound server
            "python -m kiro_crew run gateway",  # "gateway" is a file arg to run, not the subcommand
            "python -m kiro_crew run start",  # "start" is a file arg to run, not the subcommand
            "python -m kiro_crew_other gateway",  # different package named kiro_crew_other
            "/usr/bin/kirocrew",  # wrapper with no subcommand
            "grep -m kiro_crew gateway somefile",  # "-m" is grep's flag (no python interpreter)
        ],
    )
    def test_rejects_non_server_processes(self, args):
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew(args) is False

    def test_unbalanced_quotes_do_not_raise(self):
        """A malformed args string (odd quote) must not raise: ``shlex.split``
        falls back to ``str.split`` and the command is still classified."""
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew('python -m kiro_crew gateway "') is True

    def test_subcommand_match_is_exact_case(self):
        """Subcommands are matched exactly (lower-case), mirroring how the CLI
        dispatches them; ``ps`` preserves argv case so this stays precise."""
        from kiro_crew.cli_server import _args_look_like_kirocrew

        assert _args_look_like_kirocrew("python -m kiro_crew GATEWAY") is False


class TestStop:
    """Tests for _stop CLI function."""

    def _mock_sel(self):
        mock = MagicMock()
        return patch("kiro_crew.cli_commands.sel", return_value=mock)

    @pytest.fixture(autouse=True)
    def _no_service(self):
        # ``_stop`` short-circuits via ``service_controller.stop_service()``
        # when a systemd/launchd service is active on the host. Force the
        # SIGTERM-by-port path so tests don't flake based on whether the
        # test host happens to have ``kirocrew.service`` installed.
        #
        # Also force the port-lookup tool to report AVAILABLE: ``_stop`` now
        # distinguishes "no listener" from "lsof/netstat missing" and prints a
        # different message + SEL outcome for the tool-absent case. These tests
        # exercise the genuine-no-listener path, so pin availability True instead
        # of depending on whether ``lsof`` happens to be installed on the build
        # host.
        with (
            patch("kiro_crew.cli_server.service_controller.stop_service", return_value=False),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
        ):
            yield

    def _ports(self, pids):
        return patch("kiro_crew.cli_server.platform_compat.find_listening_pids", return_value=pids)

    def _cmdline(self, value):
        # Same cmdline for any PID queried.
        return patch(
            "kiro_crew.cli_server.platform_compat.process_command_line", return_value=value
        )

    def test_no_process_on_port(self, capsys):
        # No listener on the port (lsof empty / netstat no match) → nothing to stop.
        from kiro_crew.cli_server import _stop

        with self._mock_sel(), self._ports([]):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "No Kiro Crew gateway" in capsys.readouterr().out

    def _tool_absent(self, unpinned_at):
        # The lookup tool reads as unavailable; ``unpinned_at`` is where PATH
        # finds it anyway (None when it is genuinely not installed).
        return (
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=False,
            ),
            patch("kiro_crew.cli_server.platform_compat.listening_pid_tool", return_value="lsof"),
            patch(
                "kiro_crew.cli_server.platform_compat.tool_outside_trusted_dirs",
                return_value=unpinned_at,
            ),
        )

    def test_a_tool_outside_the_pin_is_not_reported_as_missing(self, capsys):
        """A host that keeps binaries elsewhere has the tool; the pin declined it.

        NixOS and Homebrew/conda prefixes are the real population here. Telling
        that operator to install an ``lsof`` they already have sends them in
        circles, so name the path and say the pin is deliberate.
        """
        from kiro_crew.cli_server import _stop

        mock_sel = MagicMock()
        available, tool, unpinned = self._tool_absent("/run/current-system/sw/bin/lsof")
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._ports([]),
            available,
            tool,
            unpinned,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "/run/current-system/sw/bin/lsof" in out, "must name where the tool actually is"
        assert "Install lsof" not in out
        # The audit log has to separate the two causes, not just the outcome.
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=lsof_outside_trusted_dirs" in resources

    def test_a_genuinely_missing_tool_still_says_to_install_it(self, capsys):
        """Nothing on PATH means the install advice is the correct advice."""
        from kiro_crew.cli_server import _stop

        mock_sel = MagicMock()
        available, tool, unpinned = self._tool_absent(None)
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._ports([]),
            available,
            tool,
            unpinned,
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Install lsof and retry." in out
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=lsof_not_found" in resources

    def test_no_kirocrew_process(self, capsys):
        # A listener exists but its cmdline isn't a kirocrew gateway → refuse to kill.
        from kiro_crew.cli_server import _stop

        with self._mock_sel(), self._ports([1234]), self._cmdline("nginx: worker"):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "No Kiro Crew gateway" in capsys.readouterr().out

    def test_successful_stop(self, capsys):
        from kiro_crew.cli_server import _stop

        # The kill is dispatched per-platform: POSIX os.kill(SIGTERM), Windows
        # platform_compat.kill_process_tree (taskkill /T /F, so the gateway's
        # child tree is reaped too). Patch the path the running OS takes so the
        # fake PID is treated as successfully signaled + exited.
        ctx = [
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            patch("time.sleep"),
            patch("kiro_crew.cli_server.platform_compat.pid_exists", return_value=False),
        ]
        if sys.platform == "win32":
            ctx.append(
                patch("kiro_crew.cli_server.platform_compat.kill_process_tree", return_value=True)
            )
        else:
            ctx.append(patch("os.kill"))
        with contextlib.ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            _stop(5476)
        out = capsys.readouterr().out
        assert "SIGTERM" in out or "Terminated" in out

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX SIGTERM/os.kill semantics; Windows uses taskkill"
    )
    def test_permission_denied(self, capsys):
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            patch("os.kill", side_effect=PermissionError),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "No permission" in capsys.readouterr().out

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX ProcessLookupError path; Windows liveness via pid_exists",
    )
    def test_process_already_exited(self, capsys):
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            self._ports([1234]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        assert "already exited" in capsys.readouterr().out

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX SIGTERM/os.kill semantics; Windows uses taskkill"
    )
    def test_partial_permission_denied(self, capsys):
        """One PID succeeds, another is denied — reports both."""
        from kiro_crew.cli_server import _stop

        def kill_side_effect(pid, sig):
            if pid == 5678:
                raise PermissionError

        with (
            self._mock_sel(),
            self._ports([1234, 5678]),
            self._cmdline("python3 -m kiro_crew.dashboard"),
            patch("os.kill", side_effect=kill_side_effect),
            patch("time.sleep"),
        ):
            with pytest.raises(SystemExit) as exc:
                _stop(5476)
            assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "SIGTERM" in out
        assert "No permission" in out

    def test_explicit_port_bypasses_service_short_circuit(self, capsys):
        # When --port is passed explicitly (cli_port is not None), the
        # systemd/launchd service short-circuit must be bypassed so the
        # kill-by-port path can target a non-default dev gateway.
        from kiro_crew.cli_server import _stop

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.stop_service",
                return_value=True,
            ) as mock_stop_service,
            self._ports([]),
        ):
            with pytest.raises(SystemExit):
                _stop(8089)
        # Service short-circuit must NOT have been called.
        mock_stop_service.assert_not_called()
        # And we should have fallen through to the kill path
        # (which exits 1 here because no listener is found on 8089).
        assert "No Kiro Crew gateway" in capsys.readouterr().out


class TestWaitForPidsExit:
    """Tests for the bounded ``_wait_for_pids_exit`` helper."""

    def test_empty_pid_list_returns_immediately(self):
        from kiro_crew.cli_server import _wait_for_pids_exit

        assert _wait_for_pids_exit([], timeout=99) == []

    def test_zero_timeout_still_probes_once(self):
        """A zero timeout must not skip the check and report a false all-clear."""
        from kiro_crew.cli_server import _wait_for_pids_exit

        with patch("kiro_crew.cli_server._pid_exited", return_value=False) as mock_exited:
            assert _wait_for_pids_exit([7], timeout=0) == [7]
        mock_exited.assert_called_once_with(7)

    def test_returns_only_the_pids_still_alive(self):
        from kiro_crew.cli_server import _wait_for_pids_exit

        with patch("kiro_crew.cli_server._pid_exited", side_effect=lambda p: p != 9):
            assert _wait_for_pids_exit([8, 9], timeout=0) == [9]


class TestRestart:
    """Tests for the service-aware ``_restart`` CLI function.

    Mirrors :class:`TestStop` — restart re-uses the same service-detection
    plumbing, so we drive the same ``service_controller`` boundary with
    fakes and assert the two branches:

    1. service active → controller handles it, no SIGTERM/spawn
    2. no service → SIGTERM via ``_stop`` if a foreground gateway is
       listening, then detach a fresh gateway via Popen
    """

    @pytest.fixture(autouse=True)
    def _fast_restart_ready(self, monkeypatch):
        """Collapse the post-spawn readiness work so these tests don't spin.

        These tests mock the gateway lifecycle (``_spawn_detached_gateway`` /
        ``restart_service``); with no real gateway, ``_print_token_url()``'s
        readiness loop polls ``localhost`` once per second for the full
        ``_RESTART_READY_TIMEOUT`` (15s) before giving up -- ~15s x5 tests. These
        tests assert restart/spawn/stop dispatch, not token-URL readiness, so
        pin the timeout to 0 (loop is skipped, function returns immediately).
        Production default is unchanged.

        For the same reason the post-spawn readiness VERDICT is pinned to
        ``_READY_OK``: nothing here can actually become ready, so the real
        verdict would (correctly) fail every dispatch test and mask what they
        assert. The verdict itself is covered by
        :class:`TestRestartReadinessVerdict` and :class:`TestWaitGatewayReady`,
        which deliberately do NOT inherit this fixture.
        """
        from kiro_crew import cli_server

        monkeypatch.setattr("kiro_crew.cli_server._RESTART_READY_TIMEOUT", 0)
        monkeypatch.setattr(
            "kiro_crew.cli_server._wait_gateway_ready",
            lambda *a, **kw: (cli_server._READY_OK, None),
        )

    @staticmethod
    def _fake_proc(pid: int) -> MagicMock:
        """A ``Popen``-shaped stand-in: ``_restart`` reads ``.pid`` and polls it."""
        return MagicMock(pid=pid, poll=MagicMock(return_value=None))

    @pytest.fixture(autouse=True)
    def _tool_available(self):
        # ``_restart`` enters ``_stop`` when the port-lookup tool is ABSENT
        # (find_listening_pids() returns [] both for "nothing listening" and
        # "lsof missing", so a missing tool must not be mistaken for a dead
        # gateway and skipped). These tests drive the tool-present branches, so
        # pin availability True instead of depending on whether ``lsof`` is
        # installed on the build host.
        with patch(
            "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
            return_value=True,
        ):
            yield

    def _mock_sel(self):
        return patch("kiro_crew.cli_server.sel", return_value=MagicMock())

    def test_service_active_restarts_via_controller(self, capsys):
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=True,
            ) as mock_restart,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
        ):
            _restart(None)
        mock_restart.assert_called_once()
        # Service path must NOT also spawn — that would race the supervisor.
        mock_spawn.assert_not_called()
        # And must not poke at the port lookup (the supervisor owns the lifecycle).
        mock_ports.assert_not_called()
        assert "Restarted" in capsys.readouterr().out

    def test_no_service_no_running_gateway_spawns_fresh(self, capsys):
        # Restart should be tolerant of a crashed gateway: if the user runs
        # ``kirocrew restart`` after the gateway died, they should still
        # end up with a running gateway, not an error.
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(4321),
            ) as mock_spawn,
            patch("kiro_crew.cli_server._stop") as mock_stop,
        ):
            _restart(None)
        mock_stop.assert_not_called()
        mock_spawn.assert_called_once()
        out = capsys.readouterr().out
        assert "4321" in out
        assert "detached" in out.lower()

    def test_no_service_with_running_gateway_stops_then_spawns(self, capsys):
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._stop") as mock_stop,
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(5678),
            ) as mock_spawn,
        ):
            _restart(None)
        # Order matters: stop first, then spawn — otherwise the new
        # gateway would race the old one for the port and lose.
        mock_stop.assert_called_once_with(None)
        mock_spawn.assert_called_once()
        assert "5678" in capsys.readouterr().out

    def test_toctou_stop_systemexit_is_swallowed_so_spawn_proceeds(self, capsys):
        # review-bot finding on rev 1: lsof can show a listener, then the
        # gateway exits before _stop() runs. _stop() then finds nothing
        # and calls sys.exit(1). For restart, that's the wrong behavior:
        # the user asked for a restart, not a stop, and an exit here would
        # leave them with no running gateway at all. Verify we swallow
        # SystemExit and still spawn the replacement.
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._stop", side_effect=SystemExit(1)) as mock_stop,
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(9999),
            ) as mock_spawn,
        ):
            _restart(None)
        mock_stop.assert_called_once_with(None)
        mock_spawn.assert_called_once()
        assert "9999" in capsys.readouterr().out

    def test_waits_for_incumbent_to_exit_before_spawning(self):
        """The replacement must not be spawned while the old gateway is alive.

        The incumbent holds the ``KIROCREW_HOME`` flock for its whole graceful
        shutdown, and ``_stop`` waits only ~1s for exit without reporting back.
        A replacement spawned inside that window is refused by the lock and exits
        1, leaving NO gateway running. Assert the spawn happens strictly after
        the incumbent pid is observed gone.
        """
        from kiro_crew import cli_server

        # Alive for the first two probes, gone from the third.
        exited = iter([False, False, True, True, True])
        probe_log: list[bool] = []

        def fake_pid_exited(pid: int) -> bool:
            val = next(exited, True)
            probe_log.append(val)
            return val

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server._stop"),
            patch("kiro_crew.cli_server._pid_exited", side_effect=fake_pid_exited),
            patch("kiro_crew.cli_server._print_token_url"),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(5678),
            ) as mock_spawn,
        ):
            cli_server._restart(None)

        mock_spawn.assert_called_once()
        # The wait actually polled past the "still alive" answers rather than
        # spawning on the first one.
        assert probe_log[:3] == [False, False, True]

    def test_refuses_to_spawn_when_incumbent_never_exits(self, capsys):
        """A wedged incumbent must abort the restart, not produce zero gateways.

        Spawning anyway would print a success line for a child the lock kills,
        so the user ends up with nothing. Aborting leaves the (slow) gateway up
        and names the pid to force.
        """
        from kiro_crew import cli_server

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server._stop"),
            patch("kiro_crew.cli_server._pid_exited", return_value=False),
            patch("kiro_crew.cli_server._RESTART_STOP_TIMEOUT", 0),
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "1234" in out
        assert "did not exit" in out

    def test_unrelated_port_listener_is_not_waited_on(self):
        """A non-KiroCrew listener must never gate the restart.

        ``find_listening_pids`` reports whatever holds the port. Blocking on a
        foreign process would make restart hang for the full timeout and then
        refuse, so the wait set is filtered by ``_is_kirocrew_process``.
        """
        from kiro_crew import cli_server

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=False),
            patch("kiro_crew.cli_server._stop") as mock_stop,
            patch("kiro_crew.cli_server._pid_exited", return_value=False) as mock_exited,
            patch("kiro_crew.cli_server._print_token_url"),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(5678),
            ) as mock_spawn,
        ):
            cli_server._restart(None)

        # _stop still runs (it owns the "not a KiroCrew gateway" diagnostic)...
        mock_stop.assert_called_once_with(None)
        # ...but nothing is waited on, and the spawn proceeds.
        mock_exited.assert_not_called()
        mock_spawn.assert_called_once()

    def test_spawn_detached_gateway_binds_requested_port(self, tmp_path, monkeypatch):
        """The child must bind the port the parent resolved.

        The parent stops a gateway on the resolved port and then polls that same
        port for readiness, but the child re-resolves independently. With
        run-marker discovery in the chain (and the marker cleared by the stop we
        just did), an unparameterised spawn lets the replacement bind 5476 while
        the parent waits on 6776 and prints a 6776 URL.
        """
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        proc = MagicMock(pid=4321)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway(6776)
        assert mock_popen.call_args.args[0] == [
            "/usr/local/bin/kirocrew",
            "gateway",
            "--port",
            "6776",
        ]

    def test_restart_passes_resolved_port_to_spawn(self, tmp_path, monkeypatch):
        """`restart` with no --port must hand its resolved port to the child."""
        from kiro_crew import cli_server

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        with (
            patch("kiro_crew.cli_server.resolve_client_port", return_value=6776),
            patch("kiro_crew.cli_server.service_controller.restart_service", return_value=False),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids", return_value=[]),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(1234),
            ) as mock_spawn,
            patch("kiro_crew.cli_server._print_token_url"),
        ):
            cli_server._restart(None)
        assert mock_spawn.call_args.args[0] == 6776

    def test_spawn_detached_gateway_uses_kirocrew_bin(self, tmp_path, monkeypatch):
        # When ``kirocrew`` is on PATH, the detached child must invoke it
        # directly (not via ``python -m``). This exercises the production
        # path on installed hosts.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        proc = MagicMock(pid=9999)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            spawned = _spawn_detached_gateway()
        # The Popen HANDLE is returned, not a bare pid: restart must be able to
        # poll the child for early death (and its exit status) before it reports
        # success.
        assert spawned is proc
        assert spawned.pid == 9999
        argv = mock_popen.call_args.args[0]
        assert argv == ["/usr/local/bin/kirocrew", "gateway"]
        # Must detach from the controlling terminal — otherwise the detached
        # process would die when the calling shell exits. POSIX: start_new_session;
        # Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP creationflags.
        kw = mock_popen.call_args.kwargs
        if sys.platform == "win32":
            flags = kw["creationflags"]
            assert flags & subprocess.DETACHED_PROCESS
            assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
            assert "start_new_session" not in kw
        else:
            assert kw["start_new_session"] is True
        # Must not inherit stdin from the parent — otherwise reading from
        # a detached terminal would block the new gateway.
        assert kw["stdin"] == subprocess.DEVNULL

    def test_spawn_detached_gateway_prefers_own_console_script(self, tmp_path, monkeypatch):
        # Two `kirocrew` scripts can exist at once — an edition composes this
        # core behind an entry point of the same name, and a stock editable
        # install in another interpreter puts a second one on PATH. A restart
        # must respawn the one that was invoked, not whichever which() finds
        # first, or the replacement gateway composes different providers than
        # the one just stopped.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        own = tmp_path / "kirocrew"
        own.write_text("#!/bin/sh\n")
        own.chmod(0o755)
        monkeypatch.setattr(sys, "argv", [str(own), "restart"])
        proc = MagicMock(pid=7777)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        assert mock_popen.call_args.args[0] == [str(own), "gateway"]

    def test_spawn_detached_gateway_ignores_non_kirocrew_argv0(self, tmp_path, monkeypatch):
        # argv[0] is only trusted when it names the kirocrew console script.
        # Anything else (a test runner, a wrapper, `python -m kiro_crew restart`)
        # must fall through to the documented which()/module resolution.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        other = tmp_path / "pytest"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        monkeypatch.setattr(sys, "argv", [str(other), "restart"])
        proc = MagicMock(pid=6666)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        assert mock_popen.call_args.args[0] == ["/usr/local/bin/kirocrew", "gateway"]

    def test_spawn_detached_gateway_ignores_missing_argv0_path(self, tmp_path, monkeypatch):
        # Fails closed on a correctly-named but non-existent argv[0] (frozen
        # bundles and some launchers rewrite it), rather than handing Popen a
        # path that cannot be executed.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "ghost" / "kirocrew"), "restart"])
        proc = MagicMock(pid=5555)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        assert mock_popen.call_args.args[0] == ["/usr/local/bin/kirocrew", "gateway"]

    def test_spawn_detached_gateway_absolutizes_relative_argv0(self, tmp_path, monkeypatch):
        # Regression: `cd ~/checkout && .venv/bin/kirocrew restart`. shutil.which()
        # returns an argument that already has a directory component *unchanged*,
        # so it stays relative — and Popen gets cwd=$HOME, which chdirs the child
        # before exec, so a relative program path resolves under $HOME and raises
        # FileNotFoundError with no gateway running (_stop() already killed it).
        # Deliberately exercises the real which() rather than patching it.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        # Use the real per-platform venv console-script layout: `bin/kirocrew` on
        # POSIX, `Scripts\kirocrew.exe` on Windows. shutil.which() only accepts a
        # directory-qualified argument on Windows with a PATHEXT extension
        # attached, so an extensionless name there would miss and fall through to
        # the which()/module chain instead of exercising this path.
        if sys.platform == "win32":
            venv_bin = tmp_path / ".venv" / "Scripts"
            script_name = "kirocrew.exe"
        else:
            venv_bin = tmp_path / ".venv" / "bin"
            script_name = "kirocrew"
        venv_bin.mkdir(parents=True)
        own = venv_bin / script_name
        own.write_text("#!/bin/sh\n")
        own.chmod(0o755)
        monkeypatch.chdir(tmp_path)
        rel = os.path.join(".venv", venv_bin.name, script_name)
        monkeypatch.setattr(sys, "argv", [rel, "restart"])
        proc = MagicMock(pid=4444)
        with patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen:
            _spawn_detached_gateway()
        spawned = Path(mock_popen.call_args.args[0][0])
        assert spawned.is_absolute()
        assert spawned.resolve() == own.resolve()

    def test_spawn_detached_gateway_falls_back_to_python_m(self, tmp_path, monkeypatch):
        # Dev/Brazil-workspace installs may not have ``kirocrew`` on
        # PATH globally. Fall back to ``python -m kiro_crew`` so the
        # command works regardless of install layout.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        proc = MagicMock(pid=8888)
        with (
            patch("shutil.which", return_value=None),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        argv = mock_popen.call_args.args[0]
        # First arg is sys.executable (path to current Python). Just check
        # the invocation form, not the absolute path.
        assert argv[1:] == ["-m", "kiro_crew", "gateway"]

    def test_explicit_port_bypasses_service_short_circuit(self, capsys):
        # When cli_port is not None, bypass systemd: the service unit is not
        # bound to a specific port, so short-circuiting through it would
        # target the wrong gateway.
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=True,
            ) as mock_restart_service,
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(4321),
            ) as mock_spawn,
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
        ):
            _restart(8089)
        # Service short-circuit must NOT have been called.
        mock_restart_service.assert_not_called()
        # And we should have fallen through to the spawn path.
        mock_spawn.assert_called_once()
        assert "Started detached gateway" in capsys.readouterr().out


class TestRestartReadinessVerdict:
    """`restart` must report success only once the REPLACEMENT is serving.

    Deliberately a separate class from :class:`TestRestart`: that class's autouse
    ``_fast_restart_ready`` fixture pins ``_wait_gateway_ready`` to ``ready`` (and
    ``_RESTART_READY_TIMEOUT`` to 0) so its dispatch assertions don't need a live
    gateway — which is exactly the behaviour under test here, so inheriting it
    would mask every one of these tests.

    Before this verdict existed, ``restart`` printed "✅ Started detached gateway
    (pid N)" straight off the ``Popen`` pid and exited 0, so a replacement that
    the ``KIROCREW_HOME`` ownership guard refused (exit 1, milliseconds later)
    reported success with NO gateway running.
    """

    def _drive(self, *, poll, ready_status, marker_pid, timeout=None):
        """Drive ``_restart``'s fork path with a scripted replacement gateway.

        Returns ``(exit_code, mock_sel, mock_token_url)`` — ``exit_code`` is
        ``None`` when ``_restart`` returned normally — so both the happy and the
        failing path can assert the audited outcome.

        Nothing here ever sleeps for real: the timeout is collapsed, and the wait
        loop checks its deadline only AFTER probing, so every case resolves on the
        first pass.
        """
        from kiro_crew import cli_server

        mock_sel = MagicMock()
        stack = [
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
            patch("kiro_crew.cli_server.run_marker.read_pid", side_effect=marker_pid),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=True),
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=ready_status),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=MagicMock(pid=4321, poll=MagicMock(return_value=poll)),
            ),
            patch("kiro_crew.cli_server._print_token_url"),
            patch("kiro_crew.cli_server._RESTART_READY_TIMEOUT", 0 if timeout is None else timeout),
        ]
        with contextlib.ExitStack() as es:
            patched = [es.enter_context(p) for p in stack]
            code = None
            try:
                cli_server._restart(None)
            except SystemExit as exc:
                code = exc.code
        return code, mock_sel, patched[-2]

    @staticmethod
    def _outcomes(mock_sel):
        return [c.kwargs["outcome"] for c in mock_sel.log_api_access.call_args_list]

    def test_replacement_that_dies_is_reported_as_a_failure(self, capsys):
        """An immediately-exiting replacement must exit non-zero, not print ✅."""
        code, mock_sel, mock_token_url = self._drive(
            poll=1, ready_status=0, marker_pid=[None, None]
        )

        assert code == 1
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "died immediately" in out
        # The exit status is the diagnosis (1 == refused by the ownership guard),
        # and the log is where the reason is.
        assert "exit status 1" in out
        assert "4321" in out
        assert "kirocrew logs -f" in out
        # The audit must record what actually happened, not an optimistic
        # "allowed" logged before any verdict existed.
        assert self._outcomes(mock_sel) == ["denied"]
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=replacement_died exit=1" in resources
        # No point chasing a token for a gateway that is not there.
        mock_token_url.assert_not_called()

    def test_replacement_that_never_becomes_ready_is_reported_as_a_failure(self, capsys):
        """A live-but-not-serving replacement must exit non-zero with timeout wording."""
        code, mock_sel, mock_token_url = self._drive(
            poll=None, ready_status=503, marker_pid=[None, None]
        )

        assert code == 1
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "did not become ready" in out
        assert "4321" in out
        assert self._outcomes(mock_sel) == ["denied"]
        assert "reason=replacement_not_ready_within=" in (
            mock_sel.log_api_access.call_args.kwargs["resources"]
        )
        mock_token_url.assert_not_called()

    def test_ready_replacement_reports_success_and_audits_allowed(self, capsys):
        """The success line survives verbatim — it just has to wait for the verdict."""
        code, mock_sel, mock_token_url = self._drive(
            poll=None,
            ready_status=200,
            # No marker before the stop; the replacement records pid 4321.
            marker_pid=[None, 4321],
        )

        assert code is None
        assert "✅ Started detached gateway (pid 4321)" in capsys.readouterr().out
        assert self._outcomes(mock_sel) == ["allowed"]
        mock_token_url.assert_called_once()

    def test_old_gateway_answering_the_port_is_not_the_replacement(self, capsys):
        """A 200 from the OUTGOING gateway must not be read as the new one.

        The incumbent keeps serving until its socket closes, so during the
        handover the port can answer 200 while the run-marker still names the old
        pid. Accepting that would report success for the process we just asked to
        die — the failure mode the ``_gateway_start_id`` handshake in dev-fleet
        exists to avoid.
        """
        code, _mock_sel, mock_token_url = self._drive(
            poll=None,
            ready_status=200,
            # 1234 before the stop AND still 1234 while polling: the marker never
            # changed hands, so this is the old gateway.
            marker_pid=[1234, 1234, 1234],
        )

        assert code == 1
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "did not become ready" in out
        mock_token_url.assert_not_called()


class TestResolveClientPort:
    """Tests for `resolve_client_port` — the port-resolution order used by
    `kirocrew token` / `status` / `logout` / `stop` to find the gateway.

    Resolution order (see cli.resolve_client_port):
      1. explicit --port CLI arg (cli_port != None)
      2. KIROCREW_PORT env var
      3. port explicitly named in dashboard.url in config
      4. the sole live gateway run-marker (see TestResolveClientPortRunMarker)
      5. default 5476
    """

    def test_cli_flag_wins(self, monkeypatch, tmp_path):
        """An explicit --port flag must override env and config."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "9999")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:8888"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(12345) == 12345

    def test_env_var_used_when_no_cli(self, monkeypatch):
        """KIROCREW_PORT env var wins over config when no --port passed."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "6777")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:8888"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 6777

    def test_invalid_env_var_falls_through_to_config(self, monkeypatch):
        """A garbage KIROCREW_PORT must not crash; the helper falls through."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "not-a-number")
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:7778"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 7778

    def test_config_url_used_when_no_cli_no_env(self, monkeypatch):
        """The port in dashboard.url must be honoured when env is unset."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://localhost:7778"
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 7778

    def test_config_url_hostname_only_falls_through_to_default(self, monkeypatch):
        """A dashboard.url without an explicit port must fall through to 5476."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = "http://my.host.example"
        with (
            patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg),
            # No gateway advertising itself — isolate from the dev box's markers.
            patch("kiro_crew.cli_server.run_marker.marker_ports", return_value=[]),
        ):
            # A portless URL is not a port choice, so we continue past it; with no
            # live run-marker either, we land on the documented default.
            assert resolve_client_port(None) == 5476

    def test_empty_config_falls_through_to_default(self, monkeypatch):
        """No env, empty dashboard.url → 5476."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = ""
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg):
            assert resolve_client_port(None) == 5476

    def test_config_load_failure_falls_through_to_default(self, monkeypatch):
        """If config loading raises, the helper must still return a usable port."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        with patch("kiro_crew.cli_server.KiroCrewConfig.load", side_effect=RuntimeError("boom")):
            assert resolve_client_port(None) == 5476

    def test_cli_flag_zero_is_respected(self, monkeypatch):
        """Port 0 is weird but valid; it must not be coerced to None/default."""
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "9999")
        # cli_port=0 is explicit; the helper uses 'is not None' not truthiness.
        assert resolve_client_port(0) == 0


class TestResolveClientPortRunMarker:
    """The run-marker fallback in `resolve_client_port` (step 4).

    A gateway on a non-default port advertises itself by writing
    `<data-home>/run/gateway-<port>.bin`. With nothing configured, a client must
    read that marker instead of assuming 5476 — but only when a verified
    KiroCrew gateway process holds the port, and only when exactly one does.
    """

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PORT", raising=False)

    def _cfg(self, url):
        mock_cfg = MagicMock()
        mock_cfg.dashboard.url = url
        return patch("kiro_crew.cli_server.KiroCrewConfig.load", return_value=mock_cfg)

    def _markers(self, ports):
        return patch("kiro_crew.cli_server.run_marker.marker_ports", return_value=ports)

    def _owned(self, ports):
        """Pretend a verified KiroCrew gateway listens on each of *ports*."""
        return patch(
            "kiro_crew.cli_server._gateway_owns_port", side_effect=lambda p: p in set(ports)
        )

    def test_sole_owned_marker_used_when_nothing_configured(self):
        """The bug: one gateway on 6776, no config → must not return 5476."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6776

    def test_portless_config_url_falls_through_to_marker(self):
        """`dashboard.url` without a port is not a port choice — the marker wins.

        parse_dashboard_url() substitutes 5476 for a portless URL, which would
        otherwise short-circuit discovery with a value the user never wrote down.
        """
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg("http://my.host.example"), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6776

    def test_explicit_config_port_beats_marker(self):
        """An explicitly configured port is a user decision; discovery yields."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg("http://localhost:7778"), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 7778

    def test_env_var_beats_marker(self, monkeypatch):
        from kiro_crew.cli_server import resolve_client_port

        monkeypatch.setenv("KIROCREW_PORT", "6777")
        with self._cfg(""), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6777

    def test_cli_flag_beats_marker(self):
        from kiro_crew.cli_server import resolve_client_port

        with self._markers([6776]) as markers:
            assert resolve_client_port(12345) == 12345
            markers.assert_not_called()  # no lookup cost when the port is explicit

    def test_non_string_config_url_does_not_crash(self):
        """`dashboard.url: 123` must degrade, not raise.

        Core installs may lack jsonschema, so the field can hold any JSON type.
        urlparse raises TypeError (NOT ValueError) on a non-str, which would
        otherwise escape and kill every client command.
        """
        from kiro_crew.cli_server import resolve_client_port

        for bad in (123, ["http://localhost:7778"], {"url": 1}, True):
            with self._cfg(bad), self._markers([6776]), self._owned([6776]):
                assert resolve_client_port(None) == 6776
        # ...and with no marker to fall back on, still the documented default.
        with self._cfg(123), self._markers([]):
            assert resolve_client_port(None) == 5476

    def test_no_marker_falls_through_to_default(self):
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([]):
            assert resolve_client_port(None) == 5476

    def test_stale_marker_not_owned_by_gateway_is_ignored(self):
        """A crashed gateway leaves its marker behind, and an unrelated process
        may since have bound that port. Trusting it would hand the local secret
        to that process, so the port must be discarded."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([6776]), self._owned([]):
            assert resolve_client_port(None) == 5476

    def test_multiple_owned_markers_refuses_to_guess(self, capsys):
        """Two gateways up → no basis to pick; fall back to the documented
        default and tell the user how to disambiguate."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg(""), self._markers([6776, 6777]), self._owned([6776, 6777]):
            assert resolve_client_port(None) == 5476
        err = capsys.readouterr().err
        assert "6776" in err and "6777" in err
        assert "--port" in err

    def test_discovery_failure_falls_through_to_default(self):
        """A broken data home must not break client commands."""
        from kiro_crew.cli_server import resolve_client_port

        with (
            self._cfg(""),
            patch(
                "kiro_crew.cli_server.run_marker.marker_ports",
                side_effect=OSError("boom"),
            ),
        ):
            assert resolve_client_port(None) == 5476

    def test_malformed_config_url_still_reaches_marker(self):
        """A typo'd `dashboard.url` must not swallow the discovery step."""
        from kiro_crew.cli_server import resolve_client_port

        with self._cfg("http://[::1"), self._markers([6776]), self._owned([6776]):
            assert resolve_client_port(None) == 6776


class TestGatewayOwnsPort:
    """`_gateway_owns_port` — the identity gate protecting the local secret.

    Three parts, none sufficient alone: the pid recorded in the owner-only
    sidecar, that pid holding the port, and that pid being owned by the calling
    user. An argv-only check would be spoofable by launching a listener as
    `/tmp/kirocrew gateway`.
    """

    def _sidecar(self, pid):
        return patch("kiro_crew.cli_server.run_marker.read_pid", return_value=pid)

    def _listeners(self, pids):
        return patch(
            "kiro_crew.cli_server.platform_compat.find_listening_pids", return_value=pids
        )

    def _owner(self, uid):
        return patch(
            "kiro_crew.cli_server.platform_compat.process_owner_uid", return_value=uid
        )

    def _me(self):
        return os.getuid() if hasattr(os, "getuid") else 0

    def _posix(self, value=True):
        return patch("kiro_crew.cli_server.platform_compat.IS_POSIX", value)

    def test_true_when_recorded_pid_holds_the_port_and_is_ours(self):
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._posix(),
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(self._me()),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server.os.getuid", return_value=self._me(), create=True),
        ):
            assert _gateway_owns_port(6776) is True

    def test_denies_on_non_posix(self):
        """Windows cannot report a process owner, and a home writable by another
        user would let them forge both the marker and the sidecar — so the step
        is skipped rather than trusted on partial evidence. Windows keeps --port
        / KIROCREW_PORT, which is where it was before this fallback existed.
        """
        from kiro_crew.cli_server import _gateway_owns_port

        # Every OTHER precondition is satisfied, so the non-POSIX guard is the
        # only thing that can deny — otherwise this test would pass for the
        # wrong reason (e.g. an unresolvable owner for a fabricated pid).
        with (
            self._posix(False),
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(self._me()),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server.os.getuid", return_value=self._me(), create=True),
        ):
            assert _gateway_owns_port(6776) is False

    def test_false_when_listener_is_not_the_recorded_pid(self):
        """The spoofing case: an unrelated process holds the port and can name
        itself anything, but it is not the pid our own sidecar records."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([9999]),
            self._owner(self._me()),
            # Even if its argv is a perfect forgery of a gateway command line.
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
        ):
            assert _gateway_owns_port(6776) is False

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uid gate only")
    def test_false_when_pid_is_owned_by_another_user(self):
        """Pid recycling into a *foreign* user's process must not be trusted,
        even though that pid does hold the port."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(os.getuid() + 1),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
        ):
            assert _gateway_owns_port(6776) is False

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uid gate only")
    def test_false_when_owner_uid_is_unknown_on_posix(self):
        """Cannot determine ownership → deny (fail closed), do not assume ours."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(None),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
        ):
            assert _gateway_owns_port(6776) is False

    def test_false_when_no_pid_recorded(self):
        """No sidecar / unparseable pid → nothing to prove identity with."""
        from kiro_crew.cli_server import _gateway_owns_port

        with self._sidecar(None), self._listeners([4242]):
            assert _gateway_owns_port(6776) is False

    def test_false_when_pid_is_not_a_kirocrew_process(self):
        """Defense in depth kept as the last step, never as the only one."""
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            self._listeners([4242]),
            self._owner(self._me()),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=False),
        ):
            assert _gateway_owns_port(6776) is False

    def test_fails_closed_when_no_listener_or_lookup_tool(self):
        """find_listening_pids folds a missing lsof/netstat into an empty list,
        so both 'nothing there' and 'cannot tell' must deny."""
        from kiro_crew.cli_server import _gateway_owns_port

        with self._sidecar(4242), self._listeners([]):
            assert _gateway_owns_port(6776) is False

    def test_fails_closed_when_lookup_raises(self):
        from kiro_crew.cli_server import _gateway_owns_port

        with (
            self._sidecar(4242),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                side_effect=OSError("lsof exploded"),
            ),
        ):
            assert _gateway_owns_port(6776) is False


class TestCliLoopbackAddress:
    """Secret-bearing CLI requests must be pinned to the verified endpoint.

    `localhost` can resolve to `::1` first on a dual-stack host, and the listener
    verification cannot distinguish an IPv6 squatter from the real IPv4 gateway,
    so a name-based URL could deliver `X-Local-Secret` to another local user's
    socket.
    """

    def test_cli_requests_use_the_ipv4_literal(self):
        import inspect

        from kiro_crew import cli_server

        assert cli_server._CLI_LOOPBACK == "127.0.0.1"
        src = inspect.getsource(cli_server)
        # No CLI->gateway request may be built from the hostname.
        assert 'http://localhost:{port}' not in src
        for fn in (cli_server._token, cli_server._logout, cli_server._print_token_url):
            body = inspect.getsource(fn)
            if "http://" in body:
                assert "_CLI_LOOPBACK" in body, fn.__name__

    def test_printed_browser_url_still_uses_the_canonical_host(self):
        """The URL handed to the browser must NOT be switched to 127.0.0.1 —
        the SPA's per-origin localStorage is keyed on `localhost`."""
        import inspect

        from kiro_crew import cli_server

        body = inspect.getsource(cli_server._token)
        assert "resolve_dashboard_host(local_only=True)" in body
        assert 'print(f"http://{host}:{port}?token={token}")' in body


class TestEnsurePrerequisites:
    """Tests for _ensure_prerequisites return value."""

    def test_returns_true_when_all_satisfied(self):
        from kiro_crew.cli_setup import _ensure_prerequisites

        with (
            patch("kiro_crew.cli_setup.shutil.which", return_value="/usr/bin/kiro-cli"),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            assert _ensure_prerequisites() is True

    def test_returns_true_when_optional_kiro_absent(self):
        """kiro-cli's absence must not block setup.

        _ensure_prerequisites only prints guidance for missing tooling (it does
        no installs and imposes no login prerequisite) and always returns True so
        setup proceeds even when the kiro-cli backend is not yet on PATH.
        """
        from kiro_crew.cli_setup import _ensure_prerequisites

        with patch("kiro_crew.cli_setup.shutil.which", return_value=None):
            assert _ensure_prerequisites() is True


class TestDoctorStaleProjectDir:
    """Tests for doctor stale project_dir detection."""

    @pytest.fixture(autouse=True)
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    def test_doctor_detects_stale_project_dir(self, tmp_path, capsys):
        proj_file = tmp_path / "project_dir"
        proj_file.write_text("/nonexistent/deleted\n")
        agent_file = tmp_path / "kirocrew.json"
        agent_data = {
            "tools": ["@kirocrew-core", "@kirocrew-cron"],
            "allowedTools": ["@kirocrew-core", "@kirocrew-cron"],
            "mcpServers": {
                "kirocrew-core": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen"),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch.dict(
                "os.environ",
                {"KIROCREW_PROJECT_DIR": "", "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": ""},
                clear=False,
            ),
        ):
            with pytest.raises(SystemExit):
                _doctor()
        out = capsys.readouterr().out
        assert "stale" in out
        assert "project dir: ⚠️  not set" not in out  # should NOT show fallback message


class TestDoctorMcpTools:
    """Tests for the `_doctor_mcp_tools` helper — the MCP section of doctor.

    The helper live-probes only the managed servers (`kirocrew-core`,
    `kirocrew-cron`) via `probe_server`; tests monkey-patch that call so
    no child processes are spawned.
    """

    def _mock_probe(self, results: dict[str, tuple[str, list[str], str]]):
        """Return a patch target for `probe_server` that yields per-name
        results. `results[name] = (status, tools, error)`."""
        from kiro_crew.mcp_discovery import McpServerInfo

        async def fake(target: McpServerInfo) -> McpServerInfo:
            status, tools, error = results.get(target.name, ("ok", [], ""))
            target.status = status
            target.tools = list(tools)
            target.error = error
            return target

        return patch("kiro_crew.cli_doctor.probe_server", side_effect=fake)

    def test_success_shows_tool_counts(self, tmp_path, capsys):
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        # Every managed server must be present or doctor reports it as a config
        # issue, so the fixture is built from the registry doctor iterates rather
        # than a literal pair (see _healthy_agent_file).
        _healthy_agent_file(agent_path)
        issues: list[str] = []
        with self._mock_probe(
            {
                "kirocrew-core": ("ok", ["spawn_run", "learn_add", "task_run"], ""),
                "kirocrew-cron": ("ok", ["cron_add"], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-core: ✅ 3 tools" in out
        assert "@kirocrew-cron: ✅ 1 tool" in out
        assert issues == []

    def test_failure_shows_error_head_and_indented_stderr(self, tmp_path, capsys):
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core", "@kirocrew-cron"],
            allowed=["@kirocrew-core", "@kirocrew-cron"],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        fail_err = (
            "no response\n"
            "stderr: Directory isn't within a workspace: '/home/u/.kirocrew-app' "
            "(Amazon::Brazil::Cli::FindupException)"
        )
        with self._mock_probe(
            {
                "kirocrew-core": ("error", [], fail_err),
                "kirocrew-cron": ("ok", [], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        # First line of error becomes the head; subsequent lines indent.
        assert "@kirocrew-core: ❌ no response" in out
        assert "      stderr: Directory isn't within a workspace" in out
        assert "FindupException" in out
        assert "@kirocrew-cron: ✅ 0 tools" in out
        assert "@kirocrew-core probe" in issues
        # Healthy server must not pollute the issue list.
        assert "@kirocrew-cron probe" not in issues

    def test_missing_mcp_server_cannot_auto_fix(self, tmp_path, capsys):
        """A missing `mcpServers` entry is install-specific; doctor reports
        the user needs to re-run setup and does not attempt to probe."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={},
        )
        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-core: ❌ missing from mcpServers" in out
        assert "@kirocrew-cron: ❌ missing from mcpServers" in out
        assert "re-run `kirocrew setup`" in out
        assert "@kirocrew-core config" in issues
        assert "@kirocrew-cron config" in issues
        probe_mock.assert_not_called()

    def test_auto_fix_adds_missing_tools_and_allowed(self, tmp_path, capsys):
        """Missing `tools` / `allowedTools` entries are added to the agent
        config and persisted in-place."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=[],
            allowed=[],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        with self._mock_probe(
            {
                "kirocrew-core": ("ok", [], ""),
                "kirocrew-cron": ("ok", [], ""),
            }
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "Auto-fixed agent config" in out
        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert updated["tools"] == ["@kirocrew-cron", "@kirocrew-core"]
        assert updated["allowedTools"] == ["@kirocrew-cron", "@kirocrew-core"]

    def test_auto_fix_never_blanket_allows_computer_use(self, tmp_path, capsys):
        """**Doctor must never add ``@kirocrew-computer`` to ``allowedTools``.**

        ``allowedTools`` is kiro-cli's blanket auto-approve list, and an
        auto-approved MCP tool is approved LOCALLY by kiro-cli: it emits no
        permission request and therefore never reaches ``hooks.on_tool_call`` — so
        the deny floor, the governance ceiling and the approval clamp would all be
        skipped for a tool that can click and type into an already-authenticated
        application.  ``agent.py``'s managed spec omits ``autoApprove`` for exactly
        this reason; a diagnostic command silently undoing it would be a complete
        Plane-A bypass.  The ``tools`` entry is still repaired — that only makes the
        server's tools reachable, never pre-approved.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_path)
        # Start from the state an upgrade leaves behind: servers registered, no
        # tool refs at all, so every ref doctor could add is attributable to it.
        data = json.loads(agent_path.read_text(encoding="utf-8"))
        data["tools"] = []
        data["allowedTools"] = []
        agent_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        issues: list[str] = []
        with self._mock_probe({}):
            _doctor_mcp_tools(agent_path, issues)

        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@kirocrew-computer" in updated["tools"]
        assert "@kirocrew-computer" not in updated["allowedTools"]
        assert not [t for t in updated["allowedTools"] if t.startswith("@kirocrew-computer/")]
        # The other managed servers are unaffected — the carve-out is scoped.
        assert "@kirocrew-core" in updated["allowedTools"]
        assert "@kirocrew-cron" in updated["allowedTools"]

    def test_auto_fix_preserves_a_user_made_computer_use_grant(self, tmp_path, capsys):
        """Doctor never MINTS the grant, but never REMOVES a user's own either.

        A user who deliberately added the ref owns that decision; silently
        reverting their config on a diagnostic run would be its own surprise. The
        two rules are independent and both matter.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_path)
        issues: list[str] = []
        with self._mock_probe({}):
            _doctor_mcp_tools(agent_path, issues)
        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@kirocrew-computer" in updated["allowedTools"]

    def test_probe_exception_does_not_crash(self, tmp_path, capsys):
        """If `probe_server` itself raises (e.g. event-loop oddity), doctor
        prints a warning and returns cleanly instead of propagating."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core", "@kirocrew-cron"],
            allowed=["@kirocrew-core", "@kirocrew-cron"],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
            },
        )
        issues: list[str] = []
        with patch(
            "kiro_crew.cli_doctor.probe_server",
            side_effect=RuntimeError("asyncio is on fire"),
        ):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "probe failed: asyncio is on fire" in out

    def test_only_managed_servers_are_probed(self, tmp_path, capsys):
        """Third-party MCPs in the agent config must not be probed — this
        keeps doctor output focused on KiroCrew's own servers and avoids
        false negatives for optional MCPs."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _write_agent_config(
            agent_path,
            tools=["@kirocrew-core", "@kirocrew-cron", "@builder-mcp"],
            allowed=["@kirocrew-core", "@kirocrew-cron", "@builder-mcp"],
            servers={
                "kirocrew-core": {"command": "/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/bin/kirocrew", "args": ["mcp-cron"]},
                "builder-mcp": {"command": "/bin/builder-mcp"},
            },
        )
        issues: list[str] = []
        probed_names: list[str] = []

        async def recording_probe(target):
            probed_names.append(target.name)
            target.status = "ok"
            target.tools = []
            return target

        with patch("kiro_crew.cli_doctor.probe_server", side_effect=recording_probe):
            _doctor_mcp_tools(agent_path, issues)
        assert probed_names == ["kirocrew-cron", "kirocrew-core"]
        out = capsys.readouterr().out
        assert "@builder-mcp" not in out

    def test_malformed_agent_config_does_not_crash(self, tmp_path, capsys):
        """If kirocrew.json is truncated or otherwise unparseable, doctor
        must fall back to an empty config and surface missing-server
        errors cleanly rather than raising out of the MCP section."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        # Truncated mid-write, half-written JSON, totally broken content —
        # the exact failure mode the atomic_write change is meant to
        # prevent from ever landing on disk, but we still need doctor to
        # cope if it encounters one (legacy installs, disk corruption).
        agent_path.write_text('{"tools": ["@kirocrew-c')

        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)

        out = capsys.readouterr().out
        # Empty config → both managed servers report missing from mcpServers.
        assert "@kirocrew-core: ❌ missing from mcpServers" in out
        assert "@kirocrew-cron: ❌ missing from mcpServers" in out
        # No probe attempted since no server spec survived the parse failure.
        probe_mock.assert_not_called()


class TestDoctorStt:
    """Tests for doctor Speech-to-Text section."""

    def test_doctor_stt_enabled_all_found(self, tmp_path, capsys):
        from kiro_crew.config.loader import KiroCrewConfig, SttConfig

        agent_file = tmp_path / "kirocrew.json"
        agent_data = {
            "tools": ["@kirocrew-core", "@kirocrew-cron"],
            "allowedTools": ["@kirocrew-core", "@kirocrew-cron"],
            "mcpServers": {
                "kirocrew-core": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroCrewConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="whisper")
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor._find_whisper", return_value="/usr/local/bin/whisper"),
            patch("kiro_crew.cli_doctor.ensure_ffmpeg_in_path"),
            patch("kiro_crew.cli_doctor.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "Speech-to-Text" in out
        assert "provider:    ✅ whisper" in out
        assert "whisper:     ✅" in out
        assert "ffmpeg:      ✅" in out

    def test_doctor_stt_disabled(self, tmp_path, capsys):
        from kiro_crew.config.loader import KiroCrewConfig, SttConfig

        agent_file = tmp_path / "kirocrew.json"
        agent_data = {
            "tools": ["@kirocrew-core", "@kirocrew-cron"],
            "allowedTools": ["@kirocrew-core", "@kirocrew-cron"],
            "mcpServers": {
                "kirocrew-core": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroCrewConfig.load()
        cfg.stt = SttConfig(enabled=False)
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.cli_doctor._find_whisper", return_value=None),
            patch("kiro_crew.cli_doctor.ensure_ffmpeg_in_path"),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "Speech-to-Text" in out
        assert "disabled" in out
        assert "not needed" in out

    def test_doctor_stt_transcribe_provider(self, tmp_path, capsys):
        from kiro_crew.config.loader import KiroCrewConfig, SttConfig

        agent_file = tmp_path / "kirocrew.json"
        agent_data = {
            "tools": ["@kirocrew-core", "@kirocrew-cron"],
            "allowedTools": ["@kirocrew-core", "@kirocrew-cron"],
            "mcpServers": {
                "kirocrew-core": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroCrewConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="transcribe", transcribe_region="us-west-2")
        # boto3 is an OPTIONAL dep (moved to the [voice] extra; modern
        # amazon-transcribe no longer pulls the full boto3). It is not
        # ambiently importable in a clean env / CI, so fake it here to keep
        # this test hermetic — otherwise the "boto3: ✅" assertion depends on
        # the host happening to have boto3 installed.
        fake_modules = {
            "amazon_transcribe": MagicMock(),
            "amazon_transcribe.client": MagicMock(),
            "boto3": MagicMock(),
        }
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.cli_doctor._find_whisper", return_value=None),
            patch("kiro_crew.cli_doctor.ensure_ffmpeg_in_path"),
            patch.dict("sys.modules", fake_modules),
        ):
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "Speech-to-Text" in out
        assert "transcribe" in out
        assert "whisper:     ⏭" in out
        assert "ffmpeg:      ✅" in out
        # Happy-path deps should report ✅ — guards against a regression
        # where the emission is silently dropped. (Cloud STT is optional now;
        # the AWS region is no longer printed on a public install.)
        assert "transcribe:  ✅" in out
        assert "boto3:       ✅" in out

    def test_doctor_stt_transcribe_amazon_transcribe_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and amazon_transcribe is not importable,
        doctor reports it as an OPTIONAL gap (public pip extra) and does NOT
        treat it as a hard failure."""
        from kiro_crew.config.loader import KiroCrewConfig, SttConfig

        agent_file = tmp_path / "kirocrew.json"
        agent_data = {
            "tools": ["@kirocrew-core", "@kirocrew-cron"],
            "allowedTools": ["@kirocrew-core", "@kirocrew-cron"],
            "mcpServers": {
                "kirocrew-core": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroCrewConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="transcribe", transcribe_region="us-west-2")
        # Force `import amazon_transcribe.client` inside _doctor() to raise
        # ImportError even though the package is already loaded at test time.
        # setitem(sys.modules, ..., None) is the documented hook for this.
        monkeypatch.setitem(sys.modules, "amazon_transcribe.client", None)
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.cli_doctor._find_whisper", return_value=None),
            patch("kiro_crew.cli_doctor.ensure_ffmpeg_in_path"),
        ):
            # Optional cloud STT missing is NOT a hard failure — _doctor may
            # still sys.exit on unrelated env checks, so tolerate either.
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "transcribe:  ⏹ optional cloud STT not installed" in out
        assert "pip install 'kirocrew[voice]'" in out

    def test_doctor_stt_transcribe_boto3_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and boto3 is not importable, doctor
        reports it as an OPTIONAL gap (public pip extra), not a hard failure."""
        from kiro_crew.config.loader import KiroCrewConfig, SttConfig

        agent_file = tmp_path / "kirocrew.json"
        agent_data = {
            "tools": ["@kirocrew-core", "@kirocrew-cron"],
            "allowedTools": ["@kirocrew-core", "@kirocrew-cron"],
            "mcpServers": {
                "kirocrew-core": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        cfg = KiroCrewConfig.load()
        cfg.stt = SttConfig(enabled=True, provider="transcribe", transcribe_region="us-west-2")
        # amazon_transcribe importable (isolate the boto3 gap), boto3 missing.
        monkeypatch.setitem(sys.modules, "amazon_transcribe", MagicMock())
        monkeypatch.setitem(sys.modules, "amazon_transcribe.client", MagicMock())
        # Force `import boto3` inside _doctor() to raise ImportError.
        monkeypatch.setitem(sys.modules, "boto3", None)
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=lambda b, **_kw: f"/usr/local/bin/{b}"),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.cli_doctor._find_whisper", return_value=None),
            patch("kiro_crew.cli_doctor.ensure_ffmpeg_in_path"),
        ):
            # Optional AWS SDK missing is NOT a hard failure — tolerate either
            # a clean return or an unrelated env-driven sys.exit.
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        assert "boto3:       ⏹ optional AWS SDK not installed" in out
        assert "pip install 'kirocrew[voice]'" in out


class TestConfigDirOverride:
    """Tests that CLI functions respect KIROCREW_HOME env var via config_dir()."""

    def test_project_dir_file_uses_config_dir(self, tmp_path, monkeypatch):
        """_project_dir_file() returns path under config_dir(), not hardcoded home."""
        monkeypatch.setattr("kiro_crew.cli.config_dir", lambda: tmp_path)

        from kiro_crew.cli import _project_dir_file

        assert _project_dir_file() == tmp_path / "project_dir"

    def test_detect_project_dir_reads_from_config_dir(self, tmp_path, monkeypatch):
        """_detect_project_dir reads saved path from config_dir()/project_dir."""
        proj = tmp_path / "my_project"
        proj.mkdir()
        (proj / "skills").mkdir()
        (proj / "src" / "kiro_crew").mkdir(parents=True)

        config_home = tmp_path / "custom_config"
        config_home.mkdir()
        (config_home / "project_dir").write_text(str(proj) + "\n")

        monkeypatch.setattr("kiro_crew.cli.config_dir", lambda: config_home)
        monkeypatch.chdir(tmp_path)  # CWD has no project markers

        from kiro_crew.cli import _detect_project_dir

        assert _detect_project_dir() == str(proj)

    def test_detect_project_dir_no_agents_dir(self, tmp_path, monkeypatch):
        """Detection works without a project-level agents/ dir (removed in bbbc1f6e).

        Regression guard: agent config was consolidated into src/kiro_crew/config/
        and the root agents/ dir deleted, which silently broke detection (and the
        dashboard changelog) while the marker still required agents/ + skills/.
        """
        proj = tmp_path / "KiroCrew"
        (proj / "skills").mkdir(parents=True)
        (proj / "src" / "kiro_crew").mkdir(parents=True)
        assert not (proj / "agents").exists()

        monkeypatch.setattr("kiro_crew.cli.config_dir", lambda: tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        monkeypatch.chdir(proj)

        from kiro_crew.cli import _detect_project_dir

        assert _detect_project_dir() == str(proj.resolve())

    def test_logout_reads_secret_from_config_dir(self, tmp_path, monkeypatch):
        """_logout reads .local_secret from config_dir(), not ~/.kirocrew."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _logout(5476)

    def test_setup_slack_tokens_writes_to_config_dir(self, tmp_path, monkeypatch):
        """_setup_slack_tokens writes .env to config_dir(), not ~/.kirocrew."""
        monkeypatch.setattr("kiro_crew.cli_setup.env_path", lambda: tmp_path / ".env")

        from kiro_crew.cli_setup import _setup_slack_tokens

        # Simulate user providing all tokens
        inputs = iter(["y", "xapp-test", "xoxb-test", "U12345"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        _setup_slack_tokens()
        assert (tmp_path / ".env").exists()
        content = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "xapp-test" in content


class TestSpawnCliAuth:
    """``kirocrew spawn`` attaches X-Internal-Secret on every gateway call.

    Regression coverage for the CLI helpers in ``cli_commands.py``
    used to open ``/api/spawn`` without the per-session IPC secret, which
    caused 403 ``"gateway not running"`` errors when ``dashboard.url`` was
    set to a non-loopback host (token_auth_middleware then required either
    a session cookie or the secret header on every request).
    """

    def test_internal_secret_reads_local_secret_file(self, tmp_path, monkeypatch):
        (tmp_path / ".local_secret").write_text("abc123\n")
        monkeypatch.setattr("kiro_crew.cli_commands.config_dir", lambda: tmp_path)

        from kiro_crew.cli_commands import _internal_secret

        assert _internal_secret() == "abc123"

    def test_internal_secret_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_commands.config_dir", lambda: tmp_path)

        from kiro_crew.cli_commands import _internal_secret

        assert _internal_secret() == ""

    def test_spawn_list_sends_internal_secret_header(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".local_secret").write_text("test-secret-xyz")
        monkeypatch.setattr("kiro_crew.cli_commands.config_dir", lambda: tmp_path)

        captured: list[urllib.request.Request] = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"agents": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(req: urllib.request.Request, timeout: int = 0) -> MagicMock:
            captured.append(req)
            return mock_resp

        monkeypatch.setattr("kiro_crew.cli_commands.loopback_urlopen", fake_urlopen)

        from kiro_crew.cli_commands import _spawn

        args = argparse.Namespace(spawn_action="list", port=5476)
        _spawn(args)

        assert len(captured) == 1
        req = captured[0]
        assert req.full_url == "http://localhost:5476/api/spawn"
        headers_lower = {k.lower(): v for k, v in dict(req.headers).items()}
        assert headers_lower["x-internal-secret"] == "test-secret-xyz"

    def test_spawn_run_sends_internal_secret_header(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".local_secret").write_text("run-secret-abc")
        monkeypatch.setattr("kiro_crew.cli_commands.config_dir", lambda: tmp_path)

        captured: list[urllib.request.Request] = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id": "agent-1", "task": "hi"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        def fake_urlopen(req: urllib.request.Request, timeout: int = 0) -> MagicMock:
            captured.append(req)
            return mock_resp

        monkeypatch.setattr("kiro_crew.cli_commands.loopback_urlopen", fake_urlopen)

        from kiro_crew.cli_commands import _spawn_run

        args = argparse.Namespace(task="do thing", fire_and_forget=True, port=5476)
        _spawn_run(args, "http://localhost:5476")

        assert len(captured) == 1
        req = captured[0]
        assert req.full_url == "http://localhost:5476/api/spawn"
        assert req.data == b'{"task": "do thing"}'
        headers_lower = {k.lower(): v for k, v in dict(req.headers).items()}
        assert headers_lower["x-internal-secret"] == "run-secret-abc"
        assert headers_lower["content-type"] == "application/json"

    def test_spawn_list_403_prints_token_required(self, tmp_path, monkeypatch, capsys):
        """A bare 403 from the gateway is reported, not masked as 'not running'."""
        (tmp_path / ".local_secret").write_text("")
        monkeypatch.setattr("kiro_crew.cli_commands.config_dir", lambda: tmp_path)

        def fake_urlopen(*_args: object, **_kwargs: object) -> None:
            raise urllib.error.HTTPError(
                "http://localhost:5476/api/spawn",
                403,
                "Forbidden",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        monkeypatch.setattr("kiro_crew.cli_commands.loopback_urlopen", fake_urlopen)

        from kiro_crew.cli_commands import _spawn

        args = argparse.Namespace(spawn_action="list", port=5476)
        with pytest.raises(SystemExit) as excinfo:
            _spawn(args)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "Error" in out
        assert "gateway not running" not in out


class TestArtifactCli:
    """CLI-side coverage for security-critical paths in `_artifact`.

    The bulk of artifact behavior is exercised via the HTTP handler tests; this
    class focuses on the CLI's own gates (e.g. `is_sensitive_path()` refusal on
    `--content-file`).
    """

    def test_save_refuses_sensitive_content_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AUTOSDE security-controls: --content-file must be gated by
        # is_sensitive_path() before Path.read_text(encoding="utf-8") so a user (or script)
        # cannot exfiltrate ~/.aws/credentials by piping it into an artifact.
        from kiro_crew.cli_commands import _artifact

        monkeypatch.setattr("kiro_crew.cli_commands.is_sensitive_path", lambda _p: True)
        # Surface any HTTP call as a fatal so we can prove the function exited
        # at the security check, not at the network layer.
        monkeypatch.setattr(
            "kiro_crew.cli_commands.loopback_urlopen",
            lambda *_a, **_kw: pytest.fail("_artifact must refuse before opening any HTTP request"),
        )

        args = argparse.Namespace(
            artifact_action="save",
            name="x",
            kind="widget",
            content=None,
            content_file="/tmp/should-be-refused",
            description="",
            tags=None,
        )
        with pytest.raises(SystemExit) as excinfo:
            _artifact(args)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to read sensitive path" in err

    def test_update_refuses_sensitive_content_file(
        self, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.cli_commands import _artifact

        monkeypatch.setattr("kiro_crew.cli_commands.is_sensitive_path", lambda _p: True)
        monkeypatch.setattr(
            "kiro_crew.cli_commands.loopback_urlopen",
            lambda *_a, **_kw: pytest.fail("_artifact must refuse before opening any HTTP request"),
        )

        args = argparse.Namespace(
            artifact_action="update",
            slug="x",
            content=None,
            content_file="/tmp/should-be-refused",
            name=None,
            description=None,
            tags=None,
        )
        with pytest.raises(SystemExit) as excinfo:
            _artifact(args)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to read sensitive path" in err


class TestMcpBuiltinDispatch:
    """Tests for dynamic mcp-<builtin> dispatch (coverlay: cli.py L705-707)."""

    def test_mcp_builtin_dispatches_to_module(self, monkeypatch):
        """CLI 'mcp-<builtin>' dynamically imports and runs the builtin's mcp_server.

        No builtins ship publicly (BUILTIN_NAMES is empty after de-Amazoning),
        so register a synthetic builtin name to exercise the dispatch path
        (cli.py subparser registration + dynamic import).
        """
        import kiro_crew.cli as cli_mod

        builtin_name = "fakebuiltin"
        # Patch the registry the CLI reads when building subparsers and dispatching.
        monkeypatch.setattr(cli_mod, "_BUILTIN_NAMES", [builtin_name])
        mock_module = MagicMock()

        monkeypatch.setattr(sys, "argv", ["kirocrew", f"mcp-{builtin_name}"])
        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            cli_mod.main()

        mock_import.assert_called_once_with(f"kiro_crew.apps.builtins.{builtin_name}.mcp_server")
        mock_module.run_mcp_server.assert_called_once()


class TestProjectDirFile:
    """Tests for _project_dir_file helper (coverlay: cli.py L59-61)."""

    def test_returns_config_dir_path(self, monkeypatch, tmp_path):
        """_project_dir_file should return config_dir() / 'project_dir'."""
        monkeypatch.setattr("kiro_crew.cli.config_dir", lambda: tmp_path)
        from kiro_crew.cli import _project_dir_file

        assert _project_dir_file() == tmp_path / "project_dir"


class TestSeedDispatch:
    """Tests for --seed dispatch before gateway startup (coverlay: cli.py L624-627)."""

    def test_seed_calls_seed_cmd(self, monkeypatch):
        """When --seed is provided, seed_cmd should be called before gateway."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway", "--seed", "demo"])
        mock_seed = MagicMock(return_value=0)
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli._gateway"),
            patch("kiro_crew.cli.asyncio.run"),
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_called_once()

    def test_seed_nonzero_exits(self, monkeypatch):
        """When seed_cmd returns non-zero, CLI should sys.exit with that code."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway", "--seed", "bad"])
        mock_seed = MagicMock(return_value=1)
        with patch("kiro_crew.cli.seed_cmd", mock_seed):
            from kiro_crew.cli import main

            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_seed_skips_seed_cmd(self, monkeypatch):
        """When --seed is not provided, seed_cmd should not be called."""
        monkeypatch.setattr(sys, "argv", ["kirocrew", "gateway"])
        mock_seed = MagicMock()
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli._gateway"),
            patch("asyncio.run"),
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_not_called()

    def test_seed_with_replace_flag(self, monkeypatch):
        """--seed with --seed-replace should call seed_cmd."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["kirocrew", "gateway", "--seed", "demo", "--seed-replace"],
        )
        mock_seed = MagicMock(return_value=0)
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli._gateway"),
            patch("asyncio.run"),
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_called_once()


class TestDoctorEmbeddings:
    """Tests for the doctor Vector Memory (in-process embeddings) section."""

    @pytest.fixture(autouse=True)
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    @staticmethod
    def _run_doctor(tmp_path, monkeypatch, *, runtime_ok: bool, model_present: bool, platform_supported: bool = True):
        """Run _doctor with the embeddings runtime/model state stubbed."""
        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        import kiro_crew.cli_doctor as doc

        monkeypatch.setattr(doc, "_load_llama_class", lambda: object if runtime_ok else None)
        monkeypatch.setattr(
            doc, "_platform_libs_dirname", lambda: "macos_arm64" if platform_supported else None
        )
        monkeypatch.setattr(doc, "model_file_present", lambda path=None: model_present)
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=default_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            with contextlib.suppress(SystemExit):
                _doctor()

    def test_doctor_reports_runtime_and_missing_model(self, tmp_path, capsys, monkeypatch):
        """Vendored runtime loads but no model file -> runtime OK, model pending."""
        self._run_doctor(tmp_path, monkeypatch, runtime_ok=True, model_present=False)
        out = capsys.readouterr().out
        assert "runtime:     ✅ vendored llama-cpp-python importable" in out
        assert "model:       ⏹ not downloaded yet" in out
        assert "embeddings:  ✅ always-on" in out

    def test_doctor_reports_runtime_failure(self, tmp_path, capsys, monkeypatch):
        """Runtime import failing on a SUPPORTED platform is surfaced as an issue."""
        self._run_doctor(tmp_path, monkeypatch, runtime_ok=False, model_present=False)
        out = capsys.readouterr().out
        assert "runtime:     ❌ vendored runtime failed to load" in out

    def test_doctor_unsupported_platform_is_not_an_issue(self, tmp_path, capsys, monkeypatch):
        """No vendored libs for this platform = designed degradation, not a doctor failure."""
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            platform_supported=False,
        )
        out = capsys.readouterr().out
        assert "runtime:     ⏹ unsupported platform" in out
        assert "embedding runtime" not in out

    def test_doctor_reports_model_present(self, tmp_path, capsys, monkeypatch):
        """Model file present -> reported with its path."""
        self._run_doctor(tmp_path, monkeypatch, runtime_ok=True, model_present=True)
        out = capsys.readouterr().out
        assert "model:       ✅" in out

    def test_doctor_probes_model_url_when_model_absent(self, tmp_path, capsys, monkeypatch):
        """Fork-added: with the model absent, doctor probes the resolved model URL."""
        import kiro_crew.cli_doctor as doc

        probed: list[str] = []

        def _fake_urlopen(req, timeout=None, **kw):
            url = getattr(req, "full_url", str(req))
            probed.append(url)
            if "cloudfront" in url or "mirror" in url:
                resp = MagicMock(status=200)
                cm = MagicMock()
                cm.__enter__ = MagicMock(return_value=resp)
                cm.__exit__ = MagicMock(return_value=False)
                return cm
            raise urllib.error.URLError("no")

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        monkeypatch.setattr(doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(doc, "model_file_present", lambda path=None: False)
        monkeypatch.setattr(doc, "_resolve_model_url", lambda: "https://mirror.example/m.gguf")
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=default_run),
            patch("urllib.request.urlopen", side_effect=_fake_urlopen),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            with contextlib.suppress(SystemExit):
                _doctor()
        out = capsys.readouterr().out
        assert "https://mirror.example/m.gguf" in probed
        assert "model url:   ✅ reachable" in out

    def test_doctor_reports_unreachable_model_url(self, tmp_path, capsys, monkeypatch):
        """Fork-added: an unreachable model URL is flagged with a fix hint."""
        import kiro_crew.cli_doctor as doc

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        monkeypatch.setattr(doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(doc, "model_file_present", lambda path=None: False)
        monkeypatch.setattr(doc, "_resolve_model_url", lambda: "https://mirror.example/m.gguf")
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=default_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            with contextlib.suppress(SystemExit):
                _doctor()
        out = capsys.readouterr().out
        assert "model url:   ❌ unreachable" in out
        assert "Check network connectivity" in out


class TestWaitGatewayReady:
    """Unit tests for the post-spawn readiness wait (`_wait_gateway_ready`).

    The integration-level behaviour lives in
    :class:`TestRestartReadinessVerdict`; this class pins the loop's own rules:
    early-death short-circuit, the changed-marker-pid discriminator, and the
    documented degradation on hosts where `_gateway_owns_port` cannot pass.
    """

    @staticmethod
    def _proc(poll_values):
        return MagicMock(pid=4321, poll=MagicMock(side_effect=list(poll_values)))

    def test_dead_child_short_circuits_with_its_exit_status(self):
        """A refused replacement must be reported at once, not waited out."""
        from kiro_crew import cli_server

        probe = MagicMock(return_value=0)
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", probe),
            patch("kiro_crew.cli_server.time.sleep") as mock_sleep,
        ):
            verdict = cli_server._wait_gateway_ready(
                self._proc([1]), 7777, None, timeout=999
            )

        assert verdict == (cli_server._READY_DIED, 1)
        # Straight out of the loop: no probe, no sleep, no 999s stall.
        probe.assert_not_called()
        mock_sleep.assert_not_called()

    def test_zero_timeout_still_probes_once(self):
        """A collapsed timeout must report what is there, not a reflex failure."""
        from kiro_crew import cli_server

        probe = MagicMock(return_value=200)
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", probe),
            patch("kiro_crew.cli_server._replacement_is_serving", return_value=True),
        ):
            verdict = cli_server._wait_gateway_ready(self._proc([None]), 7777, None, timeout=0)

        assert verdict == (cli_server._READY_OK, None)
        probe.assert_called_once_with(7777)

    def test_not_ready_within_deadline_is_a_timeout(self):
        from kiro_crew import cli_server

        # Two polls: the loop's entry poll and the re-poll taken at the deadline
        # before the timeout verdict. Alive for both, so the verdict is TIMEOUT.
        with patch("kiro_crew.cli_server._probe_gateway_ready", return_value=503):
            verdict = cli_server._wait_gateway_ready(
                self._proc([None, None]), 7777, None, timeout=0
            )

        assert verdict == (cli_server._READY_TIMEOUT, None)

    def test_polls_until_the_replacement_answers(self):
        """A slow-booting gateway is waited for rather than failed."""
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", side_effect=[0, 503, 200]),
            patch("kiro_crew.cli_server._replacement_is_serving", return_value=True),
            patch("kiro_crew.cli_server.time.sleep") as mock_sleep,
        ):
            verdict = cli_server._wait_gateway_ready(
                self._proc([None, None, None]), 7777, None, timeout=999
            )

        assert verdict == (cli_server._READY_OK, None)
        assert mock_sleep.call_count == 2

    def test_probe_reports_zero_for_a_listener_that_does_not_speak_http(self):
        """A wedged fork holding the port must yield "not ready", not a traceback.

        ``http.client.BadStatusLine`` is neither an ``OSError`` nor a
        ``URLError``, so it escapes the connection-failure handler unless caught
        explicitly -- and a non-HTTP listener on the port is precisely the
        situation restart is being run to clear.
        """
        import http.client

        from kiro_crew import cli_server

        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            side_effect=http.client.BadStatusLine("garbage"),
        ):
            assert cli_server._probe_gateway_ready(7777) == 0

    def test_child_that_exits_during_the_last_probe_is_reported_as_died(self):
        """Exiting on the final probe must not be reported as "still running".

        Otherwise the operator is sent looking for a live process that no longer
        exists, with no exit status to explain it.
        """
        import types

        from kiro_crew import cli_server

        proc = MagicMock()
        # Alive for the loop's entry poll, exited by the deadline re-poll.
        proc.poll.side_effect = [None, 3]

        # The clock is stubbed on cli_server's OWN attribute rather than through
        # `cli_server.time.monotonic`: that path resolves to the shared `time`
        # module, so it would swap the clock for every caller in the process --
        # including background threads -- and a finite side_effect list them lets
        # steal a value and raise StopIteration here. This stub is scoped to the
        # module under test and answers any number of calls: the first reads the
        # loop's entry time, every later one is past the deadline.
        calls: list[float] = []

        def clock() -> float:
            calls.append(0.0)
            return 0.0 if len(calls) == 1 else 100.0

        fake_time = types.SimpleNamespace(monotonic=clock, sleep=lambda _seconds: None)

        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=503),
            patch.object(cli_server, "time", fake_time),
        ):
            verdict, status = cli_server._wait_gateway_ready(proc, 7777, None, 0.0)

        assert verdict == cli_server._READY_DIED
        assert status == 3

    def test_missing_marker_is_never_the_replacement(self):
        """An absent marker is the handover's own state, not proof of a new gateway.

        ``clear_marker`` runs on graceful shutdown before the outgoing gateway's
        ``_shutdown()``, so mid-restart there is a window with no marker and the
        old socket still answering. Accepting that as the replacement would
        report the outgoing gateway's 200 as the new one's — and with no listener
        lookup available, nothing downstream would catch it.
        """
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=None),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", False),
        ):
            assert cli_server._replacement_is_serving(7777, 1234) is False
            # Also unproven when there was no prior identity to exclude.
            assert cli_server._replacement_is_serving(7777, None) is False

    def test_unchanged_marker_pid_is_the_old_gateway(self):
        """The discriminator: same recorded pid as before the stop == not the new one."""
        from kiro_crew import cli_server

        with patch("kiro_crew.cli_server.run_marker.read_pid", return_value=1234):
            assert cli_server._replacement_is_serving(7777, 1234) is False

    def test_changed_marker_pid_that_owns_the_port_is_the_replacement(self):
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=4321),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", True),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=True) as mock_owns,
        ):
            assert cli_server._replacement_is_serving(7777, 1234) is True
        mock_owns.assert_called_once_with(7777)

    def test_non_posix_degrades_to_the_marker_comparison(self):
        """Windows must not fail restart: `_gateway_owns_port` denies outright there.

        Requiring it would make every Windows restart report "never became ready"
        for a perfectly healthy gateway, so the ownership proof is only applied
        where it can pass and the marker comparison stands alone elsewhere.
        """
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=4321),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", False),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=False) as mock_owns,
        ):
            assert cli_server._replacement_is_serving(7777, 1234) is True
        mock_owns.assert_not_called()

    def test_posix_without_a_listener_lookup_degrades_too(self):
        """No lsof/netstat means `_gateway_owns_port` can never pass — degrade."""
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=4321),
            patch("kiro_crew.cli_server.platform_compat.IS_POSIX", True),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=False,
            ),
            patch("kiro_crew.cli_server._gateway_owns_port", return_value=False) as mock_owns,
        ):
            assert cli_server._replacement_is_serving(7777, None) is True
        mock_owns.assert_not_called()

    def test_probe_targets_api_ready_not_api_health(self):
        """Readiness, not liveness: a bound socket is not a serving gateway."""
        from kiro_crew import cli_server

        resp = MagicMock(status=200)
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=resp) as mock_open:
            assert cli_server._probe_gateway_ready(7777) == 200
        assert mock_open.call_args.args[0] == "http://127.0.0.1:7777/api/ready"

    def test_probe_reports_zero_when_unreachable(self):
        from kiro_crew import cli_server

        with patch(
            "kiro_crew.cli_server.loopback_urlopen", side_effect=urllib.error.URLError("down")
        ):
            assert cli_server._probe_gateway_ready(7777) == 0

    def test_probe_reports_the_http_status_of_a_not_ready_gateway(self):
        from kiro_crew import cli_server

        err = urllib.error.HTTPError("u", 503, "not ready", {}, None)
        with patch("kiro_crew.cli_server.loopback_urlopen", side_effect=err):
            assert cli_server._probe_gateway_ready(7777) == 503


class TestPrintTokenUrl:
    """Tests for _print_token_url (auto-token after restart)."""

    def test_prints_token_on_success(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="")),
        )
        # Mock resolve_dashboard_host to an obviously-fake sentinel host (RFC 2606
        # .invalid, never resolvable) so the assertion proves _print_token_url builds
        # the URL from resolve_dashboard_host's output rather than hardcoding a host.
        # A sentinel distinct from the real default ("localhost") is deliberate: it
        # would catch a regression that hardcodes "localhost" back into the URL.
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"token": "abc123"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _print_token_url(7777)

        out = capsys.readouterr().out
        assert "http://canonical-host.invalid:7777?token=abc123" in out

    def test_prints_custom_origin(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="http://kirocrew.dev:7777")),
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.dashboard_origin", lambda u: "http://kirocrew.dev:7777"
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"token": "xyz789"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _print_token_url(7777)

        out = capsys.readouterr().out
        assert "http://kirocrew.dev:7777/?token=xyz789" in out

    def test_fallback_on_timeout(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_server._RESTART_READY_TIMEOUT", 0)

        _print_token_url(7777)

        out = capsys.readouterr().out
        assert "kirocrew token" in out

    def test_fallback_on_no_secret(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_server._RESTART_READY_TIMEOUT", 0)

        _print_token_url(7777)

        out = capsys.readouterr().out
        assert "kirocrew token" in out


@pytest.mark.skipif(
    not hasattr(__import__("asyncio"), "set_child_watcher"),
    reason="asserts the 3.10-3.13 child-watcher semantics; the API was removed in 3.14",
)
class TestInstallPidfdChildWatcher:
    """Verify _install_child_watcher's platform behavior."""

    @staticmethod
    def _install_then_spawn_in_child(expected_watcher: str) -> None:
        """Run "_install_child_watcher() then asyncio.run(subprocess)" in a CLEAN
        child Python process, and assert it exits 0.

        Why a subprocess instead of an in-process ``asyncio.run``: on CPython
        3.10 the child watcher is bound to the loop inside asyncio's
        ``set_event_loop()``, which only calls ``attach_loop`` when
        ``threading.current_thread() is threading.main_thread()``. pytest-xdist
        runs test bodies on a NON-main worker thread, so an in-process
        ``asyncio.run`` here skips ``attach_loop`` and the freshly-installed
        watcher reports inactive -> ``create_subprocess_exec`` raises
        "child watcher not activated" (a harness artifact, not a product bug).
        A child ``python -c`` always runs on its own MAIN thread -- exactly like
        ``kirocrew gateway`` -- so this deterministically exercises the real
        install-before-run attach path regardless of the worker thread.
        """
        import os
        import textwrap

        code = textwrap.dedent(
            """
            import asyncio
            from kiro_crew.cli import _install_child_watcher

            async def _spawn_true():
                proc = await asyncio.create_subprocess_exec(
                    "true",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return proc.returncode

            _install_child_watcher()  # mirror the gateway: install BEFORE run
            assert type(asyncio.get_child_watcher()).__name__ == {expected!r}, (
                "expected {expected} to be installed"
            )
            assert asyncio.run(_spawn_true()) == 0
            """
        ).format(expected=expected_watcher)
        # Propagate the runtime's import path so the child can import kiro_crew
        # (a bare subprocess would not inherit it without PYTHONPATH).
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, (
            f"install-before-run child failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    def test_installs_safe_watcher_on_macos(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "darwin")
        # On macOS (no pidfd) we install the SIGCHLD-based SafeChildWatcher to
        # eliminate the thread-per-child reaper storm, and must NOT touch the
        # Linux pidfd path. Spy on set_child_watcher; make the pidfd probe and
        # the PidfdChildWatcher ctor explode so reaching them fails the test.
        called = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: called.append(w))
        monkeypatch.setattr(asyncio, "SafeChildWatcher", lambda: "fake-safe-watcher")

        def _boom(*_a) -> object:
            raise AssertionError("Linux pidfd path must not be reached on macOS")

        monkeypatch.setattr("kiro_crew.cli.os.pidfd_open", _boom, raising=False)
        monkeypatch.setattr(asyncio, "PidfdChildWatcher", _boom)
        _install_child_watcher()
        assert called == ["fake-safe-watcher"], "macOS must install SafeChildWatcher"

    def test_noop_when_safe_watcher_unavailable(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "darwin")
        # Simulate a runtime where SafeChildWatcher was removed (3.14+) or never
        # existed (Windows): the installer must leave the default watcher in
        # place rather than raise.
        monkeypatch.delattr(asyncio, "SafeChildWatcher", raising=False)
        called = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: called.append(w))
        _install_child_watcher()  # must not raise
        assert called == [], "no watcher installed when SafeChildWatcher is unavailable"

    def test_sets_watcher_on_linux(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "linux")
        # Kernel supports pidfd_open -> probe succeeds -> watcher installed.
        # Fully fake the probe (sentinel fd + no-op close) so no real fd is used.
        opened = []
        closed = []
        monkeypatch.setattr(
            "kiro_crew.cli.os.pidfd_open", lambda pid: opened.append(pid) or 4242, raising=False
        )
        monkeypatch.setattr("kiro_crew.cli.os.close", lambda fd: closed.append(fd))
        called_with = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: called_with.append(w))
        monkeypatch.setattr(asyncio, "PidfdChildWatcher", lambda: "fake-pidfd-watcher")
        _install_child_watcher()
        assert opened, "pidfd_open must be probed before installing"
        assert closed == [4242], "the probe fd must be closed"
        assert called_with == ["fake-pidfd-watcher"]

    def test_falls_back_to_safe_watcher_on_old_kernel(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "linux")
        # Real 3.10 failure mode: PidfdChildWatcher.__init__ does NOT probe the
        # kernel, so the < 5.3 failure surfaces as os.pidfd_open raising OSError.
        # PidfdChildWatcher must NEVER be constructed (else the first
        # create_subprocess_exec would ENOSYS), but we must ALSO NOT leave the
        # default ThreadedChildWatcher in place -- its thread-per-child reaper
        # storm is the wedge this installer exists to prevent. So a < 5.3 kernel
        # falls back to the SIGCHLD-based SafeChildWatcher instead.

        def _no_pidfd(_pid):
            raise OSError(38, "Function not implemented")  # ENOSYS

        monkeypatch.setattr("kiro_crew.cli.os.pidfd_open", _no_pidfd, raising=False)
        installed = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: installed.append(w))
        monkeypatch.setattr(asyncio, "SafeChildWatcher", lambda: "fake-safe-watcher")

        def _ctor_must_not_run() -> object:
            raise AssertionError("PidfdChildWatcher must not be constructed when pidfd_open fails")

        monkeypatch.setattr(asyncio, "PidfdChildWatcher", _ctor_must_not_run)
        _install_child_watcher()  # must not raise
        assert installed == ["fake-safe-watcher"], (
            "a < 5.3 kernel must fall back to SafeChildWatcher, not the "
            "thread-storm ThreadedChildWatcher"
        )

    def test_falls_back_to_safe_watcher_when_pidfd_open_missing(self, monkeypatch) -> None:
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", "linux")
        # Regression for the 2026-07-10 gateway startup kill: a uv-managed /
        # Clang-built CPython 3.12 whose build omits the os.pidfd_open wrapper
        # (present on the system python, absent in the venv interpreter). The
        # probe raises AttributeError, not OSError -- the old code caught it and
        # RETURNED, leaving the thread-per-child ThreadedChildWatcher, whose
        # os.waitpid reaper-thread storm starved the loop and got the gateway
        # killed by the loop-stall watchdog. It must now fall back to
        # SafeChildWatcher instead.
        monkeypatch.delattr("kiro_crew.cli.os.pidfd_open", raising=False)
        installed = []
        monkeypatch.setattr(asyncio, "set_child_watcher", lambda w: installed.append(w))
        monkeypatch.setattr(asyncio, "SafeChildWatcher", lambda: "fake-safe-watcher")

        def _ctor_must_not_run() -> object:
            raise AssertionError(
                "PidfdChildWatcher must not be constructed when os.pidfd_open is missing"
            )

        monkeypatch.setattr(asyncio, "PidfdChildWatcher", _ctor_must_not_run)
        _install_child_watcher()  # must not raise
        assert installed == ["fake-safe-watcher"], (
            "a Python build without os.pidfd_open must fall back to "
            "SafeChildWatcher, not the thread-storm ThreadedChildWatcher"
        )

    @pytest.mark.skipif(sys.platform != "linux", reason="pidfd watcher is Linux-only")
    def test_real_subprocess_works_after_install_on_linux(self) -> None:
        """End-to-end: after installing the watcher the way the gateway does
        (before asyncio.run, on the main thread), asyncio subprocess support must
        still work. This is the property the mocked test above cannot prove — it
        guards against the watcher being installed but never attached to the loop
        (which would make every create_subprocess_exec raise RuntimeError).

        Runs in a clean child process (its own main thread) so it is immune to
        pytest-xdist executing this test body on a non-main worker thread, where
        set_event_loop's main-thread-guarded attach_loop would be skipped.

        The expected watcher is derived from the SAME probe the installer uses
        rather than hard-coded: a Python build that omits the ``os.pidfd_open``
        wrapper (observed on uv-managed CPython) correctly installs
        SafeChildWatcher instead — the documented fallback. Asserting
        PidfdChildWatcher unconditionally made this test fail on such an
        interpreter even though the product behaved exactly as designed.
        """
        try:
            fd = os.pidfd_open(os.getpid())
            os.close(fd)
        except (OSError, AttributeError):
            expected = "SafeChildWatcher"
        else:
            expected = "PidfdChildWatcher"
        self._install_then_spawn_in_child(expected_watcher=expected)

    @pytest.mark.skipif(
        sys.platform == "linux" or not hasattr(__import__("asyncio"), "SafeChildWatcher"),
        reason="exercises the real macOS SafeChildWatcher install (non-Linux Unix, 3.10-3.13)",
    )
    def test_real_subprocess_works_after_safe_watcher_install_on_macos(self) -> None:
        """End-to-end on macOS: after the REAL _install_child_watcher() installs
        SafeChildWatcher the way the gateway does (before asyncio.run, on the main
        thread), asyncio subprocess support must still work.

        This is the macOS counterpart to the Linux end-to-end test and the
        property the fully-mocked test_installs_safe_watcher_on_macos cannot
        prove: that SafeChildWatcher actually ATTACHES to the loop (its SIGCHLD
        handler) rather than being installed but inert — which would make every
        create_subprocess_exec raise RuntimeError('...not activated...').

        Runs in a clean child process (its own main thread): SafeChildWatcher's
        attach_loop installs a SIGCHLD handler via loop.add_signal_handler, which
        is itself main-thread-only, so an in-process run under a non-main
        pytest-xdist worker thread would fail spuriously.
        """
        self._install_then_spawn_in_child(expected_watcher="SafeChildWatcher")


class TestChildWatcherApiRemoved:
    """``_install_child_watcher()`` must no-op when the child-watcher API is gone.

    CPython 3.14 removed ``set_child_watcher`` / ``PidfdChildWatcher`` /
    ``SafeChildWatcher`` (the event loop reaps children directly). The Linux
    pidfd branch referenced those names unconditionally, so on 3.14 the FIRST
    thing ``kirocrew gateway`` did was raise ``AttributeError: module 'asyncio'
    has no attribute 'set_child_watcher'`` -- the gateway died before binding
    its port, while every other subcommand (``chat``, ``doctor``) kept working
    because this installer is only called on the gateway path.
    """

    @staticmethod
    def _remove_child_watcher_api(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make asyncio look like 3.14: no child-watcher API at all."""
        import asyncio

        for name in (
            "set_child_watcher",
            "get_child_watcher",
            "PidfdChildWatcher",
            "SafeChildWatcher",
            "ThreadedChildWatcher",
            "AbstractChildWatcher",
        ):
            monkeypatch.delattr(asyncio, name, raising=False)

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_noop_when_child_watcher_api_removed(self, monkeypatch, platform) -> None:
        """Simulated removal, so 3.10-3.13 CI also protects the 3.14 code path."""
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        monkeypatch.setattr("kiro_crew.cli.sys.platform", platform)
        # A pidfd-capable kernel is the WORST case: without the guard the Linux
        # branch reaches `asyncio.set_child_watcher(asyncio.PidfdChildWatcher())`
        # and raises. Keep the probe succeeding so the regression is real.
        # Hand back a REAL fd (the real os.close then reclaims it) rather than
        # stubbing os.close: `kiro_crew.cli.os` IS the shared os module, so a
        # stub there would also be seen by asyncio's own internals.
        monkeypatch.setattr(
            "kiro_crew.cli.os.pidfd_open",
            lambda pid: os.open(os.devnull, os.O_RDONLY),
            raising=False,
        )
        self._remove_child_watcher_api(monkeypatch)

        _install_child_watcher()  # must not raise

        assert not hasattr(
            asyncio, "set_child_watcher"
        ), "the guard must not resurrect the removed API"

    @pytest.mark.skipif(
        hasattr(__import__("asyncio"), "set_child_watcher"),
        reason="only meaningful on a runtime that really removed the API (3.14+)",
    )
    def test_real_subprocess_works_after_noop_install(self) -> None:
        """On a real 3.14+ runtime, the gateway's install-before-run call must be
        survivable AND leave subprocess support working.

        No monkeypatching: the API is genuinely absent here, so this asserts the
        property that actually matters (spawning still works after the no-op)
        rather than the mechanism. 3.14 reaps children in the event loop with a
        single non-thread reaper, so nothing is lost by not installing a watcher.
        """
        import asyncio

        from kiro_crew.cli import _install_child_watcher

        _install_child_watcher()  # mirror the gateway: install BEFORE run

        async def _spawn_true() -> int:
            proc = await asyncio.create_subprocess_exec(
                "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode

        assert asyncio.run(_spawn_true()) == 0


class TestTokenCommand:
    """Tests for the ``kirocrew token`` command handler (``_token``)."""

    def _mock_token_response(self, token: str) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.read.return_value = f'{{"token": "{token}"}}'.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_prints_loopback_only(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        (tmp_path / ".local_secret").write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="")),
        )
        monkeypatch.setattr("kiro_crew.cli_server.dashboard_origin", lambda u: "")
        # Sentinel canonical host (RFC 2606 .invalid) — proves the URL is built
        # from resolve_dashboard_host's output, not a hardcoded host.
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

        args = argparse.Namespace(ttl="1h", port=7777)
        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            return_value=self._mock_token_response("abc123"),
        ):
            _token(args)

        out = capsys.readouterr().out
        assert "http://canonical-host.invalid:7777?token=abc123" in out
        # No custom-domain URL, so no separating blank line is emitted. The
        # loopback URL has no '/' before '?token=...', so '/?token=...' would
        # only appear if the custom-origin URL had been printed.
        assert "/?token=abc123" not in out

    def test_separates_custom_origin_with_blank_line(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        (tmp_path / ".local_secret").write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="https://kirocrew.dev:7777")),
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.dashboard_origin", lambda u: "https://kirocrew.dev:7777"
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

        args = argparse.Namespace(ttl="1h", port=7777)
        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            return_value=self._mock_token_response("xyz789"),
        ):
            _token(args)

        out = capsys.readouterr().out
        loopback_line = "http://canonical-host.invalid:7777?token=xyz789"
        custom_line = "https://kirocrew.dev:7777/?token=xyz789"
        assert loopback_line in out
        assert custom_line in out
        # The two URLs must be separated by a blank line.
        assert f"{loopback_line}\n\n{custom_line}" in out

    # ── stdout is a machine interface: failures go to stderr ─────────────────
    #
    # `_token`'s stdout is regex-parsed by the remote-mint path
    # (kiro_crew.instances.token_mint.mint_remote_token) over SSH. Error prose on
    # stdout both breaks the Unix convention and hides the reason from a caller
    # that captures stderr — which is how a failed remote mint used to surface as
    # a bare "<no stderr>".

    def _stub_token_env(self, tmp_path, monkeypatch, *, secret: bool = True) -> None:
        if secret:
            (tmp_path / ".local_secret").write_text("test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.cli_server.KiroCrewConfig.load",
            lambda: MagicMock(dashboard=MagicMock(url="")),
        )
        monkeypatch.setattr("kiro_crew.cli_server.dashboard_origin", lambda u: "")
        monkeypatch.setattr(
            "kiro_crew.cli_server.resolve_dashboard_host",
            lambda local_only=True: "canonical-host.invalid",
        )

    def test_invalid_ttl_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as excinfo:
            _token(argparse.Namespace(ttl="banana", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Invalid TTL" in captured.err
        assert captured.out == ""

    def test_missing_secret_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch, secret=False)
        with pytest.raises(SystemExit) as excinfo:
            _token(argparse.Namespace(ttl="1h", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Gateway not running" in captured.err
        assert captured.out == ""

    def test_unreachable_gateway_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with patch(
            "kiro_crew.cli_server.loopback_urlopen", side_effect=urllib.error.URLError("refused")
        ):
            with pytest.raises(SystemExit) as excinfo:
                _token(argparse.Namespace(ttl="1h", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Could not reach gateway on port 7777" in captured.err
        assert captured.out == ""

    def test_empty_token_error_goes_to_stderr(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with patch(
            "kiro_crew.cli_server.loopback_urlopen", return_value=self._mock_token_response("")
        ):
            with pytest.raises(SystemExit) as excinfo:
                _token(argparse.Namespace(ttl="1h", port=7777))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "empty token" in captured.err
        assert captured.out == ""

    def test_success_stdout_carries_only_urls(self, tmp_path, capsys, monkeypatch):
        """Every stdout line on the success path must be a parseable URL.

        The docstring's "stdout carries only the URL(s)" is a contract the remote
        mint depends on, and prose is not enforcement: any future preflight that
        writes to stdout — a warning, or an `input()` prompt, whose prompt goes to
        stdout — would silently corrupt the stream that mint_remote_token regexes
        over SSH. This pins the contract to an assertion instead.
        """
        from kiro_crew.cli_server import _token

        self._stub_token_env(tmp_path, monkeypatch)
        with patch(
            "kiro_crew.cli_server.loopback_urlopen",
            return_value=self._mock_token_response("eyJa.b"),
        ):
            _token(argparse.Namespace(ttl="1h", port=7777))
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines, "success path printed nothing to stdout"
        for line in lines:
            assert line.lstrip().startswith("http"), f"non-URL text on stdout: {line!r}"
        assert "token=eyJa.b" in captured.out


class TestBannerBranding:
    """The ASCII banners must spell the product's real name.

    All three were figlet-`small` renderings of "KiroClaw"/"KiroClaw Cloud" — a
    pre-rename name that reached users on `kirocrew` with no args, in the chat
    REPL, and at the top of every `kirocrew cloud` run.
    """

    def _letters(self, banner: str) -> str:
        """Collapse the ASCII art to comparable letter-ish content."""
        return "".join(banner.split())

    def test_main_banner_is_kiro_crew(self):
        from kiro_crew.cli import BANNER

        # figlet 'small' renders "Crew" with the distinctive `-_)` in the 'e' row
        # and `_ _` in the 'r'/'C' row; "Claw" instead carries `/ _` + `\ V  V /`.
        assert "-_)" in BANNER, "banner does not render 'Crew'"
        assert "|__ ___" not in BANNER, "banner still renders 'Claw'"

    def test_banner_is_single_sourced(self):
        """One definition, not two pinned copies — the duplication WAS the bug.

        cli.py and cli_chat.py each held a hand-copied banner, so a rename left
        both stale. They now re-export the one in constants.py; identity (`is`)
        proves there is no second literal to drift.
        """
        from kiro_crew.cli import BANNER as MAIN
        from kiro_crew.cli_chat import BANNER as CHAT
        from kiro_crew.constants import BANNER as CANON

        assert MAIN is CANON
        assert CHAT is CANON

    def test_no_reinlined_banner_literal(self):
        """Guard the fix: neither module may re-inline the art."""
        from pathlib import Path

        import kiro_crew.cli as cli_mod
        import kiro_crew.cli_chat as chat_mod

        for mod in (cli_mod, chat_mod):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert "BANNER = r" not in src, f"{mod.__name__} re-inlined the banner literal"

    def test_cloud_banner_is_kiro_crew_cloud(self):
        from kiro_crew.cloud.ui import BANNER

        assert "-_)" in BANNER, "cloud banner does not render 'Crew'"
        assert "|__ ___" not in BANNER, "cloud banner still renders 'Claw'"
        # The 'Cloud' half must survive the edit.
        assert "\\___/\\_,_\\__,_|" in BANNER

    def test_no_kiroclaw_spelling_anywhere_in_banners(self):
        from kiro_crew.cloud.ui import BANNER as CLOUD
        from kiro_crew.constants import BANNER as MAIN

        CHAT = MAIN

        # The 'Cl' of Claw is `/ __| |` + `(__| / _`; Crew is `/ __|_ _` + `(__| '_/`.
        for name, b in (("cli", MAIN), ("cli_chat", CHAT), ("cloud", CLOUD)):
            assert "(__| / _`" not in b, f"{name} banner still spells Claw"


class TestChatPermissionRequest:
    """`kirocrew chat` must ANSWER a permission request, and answering one is an
    authorization decision.

    Every case drives the real ``_send_and_print`` against a provider whose
    stream cannot finish until the request is answered, and against a real
    ``HookManager`` -- not a stub returning the verdict the test wants.
    """

    #: Bounds a stalled turn so the suite fails instead of hanging. Never
    #: reached when the request is answered.
    _TIMEOUT = 10.0

    @pytest.fixture(autouse=True)
    def _fresh_stdin_state(self, monkeypatch):
        """Poisoning is process-wide state; monkeypatch restores it per test."""
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(cli_chat, "_stdin_poisoned", False)

    @staticmethod
    def _event(*, title="Terminal", command=None, tool_name=""):
        """A permission request in the shape the wire delivers.

        ``tool_input`` rather than ``raw_tool_params`` carries the command,
        because that is where a permission_request puts it -- the fallback the
        gate depends on to recover what really executes.
        """
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=7,
            title=title,
            tool_kind="execute",
            is_shell=command is not None,
            tool_input=json.dumps({"command": command}) if command else "",
            tool_name=tool_name,
            # Advertised by a real backend; the CLI must NOT read these -- option
            # ids are backend-specific and the ACP layer owns the mapping.
            options=[{"id": "allow", "label": "Allow Once"}],
        )

    class _GatedProvider:
        """Cannot finish its turn until the permission is answered.

        Provider calls and audit records land in ONE ``trace`` so their relative
        order is observable, not just their presence.
        """

        def __init__(self, event, trace):
            self._event, self.trace = event, trace
            self.answered = asyncio.Event()

        @property
        def calls(self):
            return [t for t in self.trace if t[0] in ("approve", "reject")]

        async def stream(self, message):
            from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="I'll check that. ")
            yield self._event
            await self.answered.wait()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        async def approve_tool(self, request_id, *, always: bool = False):
            self.trace.append(("approve", request_id, always))
            self.answered.set()

        async def reject_tool(self, request_id):
            self.trace.append(("reject", request_id, False))
            self.answered.set()

        def context_usage_pct(self):
            return 0.0

    @staticmethod
    def _gate():
        """A gate carrying the REAL HookManager: the built-in sensitive-path and
        denied-command rules are the thing under test in several cases."""
        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.hooks import HookManager, HooksConfig

        return cli_chat._ToolGate(hooks=HookManager(HooksConfig.from_dict({})), agent="cli-tester")

    @staticmethod
    def _patch_env(monkeypatch, *, tty=True, trace=None, answer="d"):
        """Wire the seams every case shares. Returns the stdin-read record.

        The blocking read is patched at ``_read_line_blocking`` -- the single
        blocking seam -- NOT at the choice helper or the gate, so the exact-match
        rule, the daemon-thread plumbing, the gate call and the audit ordering
        all run as production code.
        """
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(
            cli_chat,
            "sel",
            lambda: types.SimpleNamespace(
                log_tool_invocation=lambda **kw: (
                    trace.append(("sel", kw)) if trace is not None else None
                )
            ),
        )
        reads = {"n": 0, "prompts": []}

        def fake_read(prompt=""):
            reads["n"] += 1
            reads["prompts"].append(prompt)
            if answer is EOFError:
                raise EOFError
            return answer

        monkeypatch.setattr(cli_chat, "_read_line_blocking", fake_read)
        return reads

    async def _drive(self, monkeypatch, *, event=None, interactive=True, tty=True, answer="d"):
        """Run one gated turn. Returns (provider, sel records, stdin reads).

        ``interactive`` is the command mode the caller passes and ``tty`` is
        patched onto the real streams, so the production ``_can_prompt`` decides
        -- patching that helper would test the stub rather than the rule.
        """
        import kiro_crew.cli_chat as cli_chat

        trace: list = []
        provider = self._GatedProvider(event or self._event(), trace)
        reads = self._patch_env(monkeypatch, tty=tty, trace=trace, answer=answer)
        await asyncio.wait_for(
            cli_chat._send_and_print(
                provider, "run it", interactive=interactive, gate=self._gate()
            ),
            timeout=self._TIMEOUT,
        )
        return provider, [t[1] for t in trace if t[0] == "sel"], reads

    # ── The security gate ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_benign_title_cannot_hide_a_sensitive_command(self, monkeypatch, capsys):
        """The gate judges what executes, not what the model called it.

        ``title`` for a shell tool is an LLM-authored description, so a
        credential read labelled "List project files" is the bypass that keying
        on the title alone would let through. The user is never even asked.
        """
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="List project files", command="cat ~/.ssh/id_rsa"),
            answer="a",  # the user WOULD have allowed it
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        # A stable code, not the gate's reason: the reason names the very path
        # being protected, and an audit record must not restate it.
        assert sels[0]["error"] == "hook_deny"
        assert ".ssh" not in json.dumps(sels[0])
        # The reason still reaches the terminal, and it has to be the REAL one:
        # `is_shell` with no command also denies, via the gate's deny-by-default
        # backstop, so "it was denied" would pass just as well when the command
        # is never forwarded at all.
        err = capsys.readouterr().err
        assert "sensitive credential path" in err
        assert "could not be verified" not in err

    @pytest.mark.asyncio
    async def test_a_denied_command_is_not_the_users_to_override(self, monkeypatch):
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Tidy the workspace", command="rm -rf /"),
            answer="a",
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert [s["outcome"] for s in sels] == ["denied"]

    # ── The audit record ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_kw,answer,order,code",
        [
            (dict(title="x", command="rm -rf /"), "a", ["sel", "reject"], "hook_deny"),
            ({}, "a", ["sel", "approve"], ""),
            ({}, "d", ["sel", "reject"], "user_denied"),
        ],
    )
    async def test_the_audit_precedes_the_transport(
        self, monkeypatch, event_kw, answer, order, code
    ):
        """A transport failure must not erase the decision, and the code stays a
        stable token so the log is queryable and quotes nothing.

        Asserting the interleaved trace, rather than two separate lists, is what
        makes this an ordering test.
        """
        provider, sels, _ = await self._drive(
            monkeypatch, event=self._event(**event_kw), answer=answer
        )
        assert [t[0] for t in provider.trace] == order
        assert sels[0]["error"] == code

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_kw,expected,forbidden",
        [
            # The canonical `_meta.kiro` identity wins over LLM prose.
            (
                dict(title="do something friendly", tool_name="mcp__files__read"),
                "mcp__files__read",
                "friendly",
            ),
            # No canonical name: the title is all there is, so scrub it -- SEL
            # does not redact for its callers.
            (dict(title="deploy with AKIAIOSFODNN7EXAMPLE"), None, "AKIAIOSFODNN7EXAMPLE"),
        ],
    )
    async def test_the_audited_identity_is_not_model_authored(
        self, monkeypatch, event_kw, expected, forbidden
    ):
        _, sels, _ = await self._drive(monkeypatch, event=self._event(**event_kw), answer="a")
        if expected is not None:
            assert sels[0]["tool_name"] == expected
        assert forbidden not in sels[0]["tool_name"]

    # ── The human decision ───────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer,call,outcome",
        [("a", ("approve", 7, False), "allowed"), ("d", ("reject", 7, False), "denied")],
    )
    async def test_the_turn_completes_whatever_the_human_answers(
        self, monkeypatch, capsys, answer, call, outcome
    ):
        """Denial answers the gate; it must not abandon the turn."""
        provider, sels, reads = await self._drive(monkeypatch, answer=answer)
        assert provider.calls == [call]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == [outcome]
        assert "done" in capsys.readouterr().out  # text after the gate reached stdout

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer,allows",
        # A prefix match reads `abort` as an allow -- the opposite of intent.
        # The other rejects are what a person types when a single-key prompt did
        # not register, plus the do-nothing answers.
        [(a, True) for a in ("a", "A", " a ", "a\n")]
        + [(a, False) for a in ("abort", "allow", "always", "wait", "ad", "x", "", EOFError)],
    )
    async def test_only_the_exact_allow_token_approves(self, monkeypatch, answer, allows):
        provider, sels, _ = await self._drive(monkeypatch, answer=answer)
        assert provider.calls == [("approve", 7, False) if allows else ("reject", 7, False)]
        assert [s["outcome"] for s in sels] == ["allowed" if allows else "denied"]

    @pytest.mark.asyncio
    async def test_no_answer_grants_a_persistent_approval(self, monkeypatch):
        """There is no "always allow": a backend that records one stops sending
        permission requests for matching calls, and a request never sent is a
        call this ladder never runs and never audits."""
        for answer in ("a", "w", "always"):
            provider, _, reads = await self._drive(monkeypatch, answer=answer)
            assert all(not always for kind, _, always in provider.calls if kind == "approve")
        # The prompt must not advertise an option the responder cannot honour.
        assert "always" not in reads["prompts"][0].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "interactive,tty",
        [
            # `-m` is documented as non-interactive, so a terminal does not
            # license a prompt: a script under a pty would block on a question
            # nobody is watching for.
            (False, True),
            # And a prompt nobody can see is a hang, not consent.
            (True, False),
        ],
    )
    async def test_a_prompt_nobody_can_answer_denies_without_reading_stdin(
        self, monkeypatch, capsys, interactive, tty
    ):
        provider, sels, reads = await self._drive(
            monkeypatch, interactive=interactive, tty=tty, answer="a"
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert sels[0]["error"] == "noninteractive"
        captured = capsys.readouterr()
        assert "done" in captured.out  # the turn still finished
        assert "Denied automatically" in captured.err
        # A redirected stream encodes with the locale codec -- non-ASCII there
        # raises UnicodeEncodeError.
        captured.err.encode("ascii")

    # ── What the human is shown ──────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command,expected,absent",
        [
            ("git status --short", "Command: git status --short", None),
            # Collapsed to one line: a heredoc must not reflow the question.
            ("printf 'a\\n\\tb'\nwc -l", "Command: printf 'a\\n\\tb' wc -l", None),
            # Capped, with the cut made explicit.
            ("echo " + "x" * 400, "... [truncated]", "x" * 400),
            # Redacted on the way to the screen.
            ("deploy --key AKIAIOSFODNN7EXAMPLE", None, "AKIAIOSFODNN7EXAMPLE"),
            # Nothing to show for a non-shell call.
            (None, None, "Command:"),
        ],
    )
    async def test_a_shell_prompt_shows_what_will_actually_run(
        self, monkeypatch, capsys, command, expected, absent
    ):
        """Approving on an LLM-authored title alone is consent to a description.

        The gate already keys on ``shell_command``; the human deciding needs the
        same ground truth.
        """
        await self._drive(
            monkeypatch,
            event=self._event(title="Run a helpful script", command=command),
            answer="a",
        )
        out = capsys.readouterr().out
        # Non-vacuous control: for a shell call the line must have been printed
        # at all, or an "absent" assertion passes for a request that never
        # reached the prompt.
        assert ("Command:" in out) is (command is not None)
        if expected:
            assert expected in out
        if absent:
            assert absent not in out

    # ── stdin lifecycle ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_the_event_loop_keeps_running_while_the_prompt_waits(self, monkeypatch):
        """The turn is parked INSIDE an active stream, not at an idle REPL.

        The blocking read does not return until a loop-side ticker has advanced,
        so a synchronous implementation deadlocks and this times out rather than
        passing quietly.
        """
        import kiro_crew.cli_chat as cli_chat

        ticks, released = {"n": 0}, threading.Event()

        async def _ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks["n"] += 1
                if ticks["n"] >= 3:
                    released.set()

        def blocking_read(prompt=""):
            # Waits for the LOOP to progress: only reachable off the loop thread.
            assert released.wait(timeout=self._TIMEOUT), "event loop was blocked"
            return "a"

        self._patch_env(monkeypatch)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", blocking_read)
        provider = self._GatedProvider(self._event(), [])
        ticker = asyncio.create_task(_ticker())
        try:
            await asyncio.wait_for(
                cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
                timeout=self._TIMEOUT,
            )
        finally:
            ticker.cancel()
        assert provider.calls == [("approve", 7, False)]

    @pytest.mark.asyncio
    async def test_a_poisoned_session_never_starts_a_second_reader(self, monkeypatch):
        """No later entry point may race the abandoned reader for keystrokes --
        not a second permission prompt, and not the REPL."""
        import kiro_crew.cli_chat as cli_chat

        self._patch_env(monkeypatch)
        monkeypatch.setattr(cli_chat, "_stdin_poisoned", True)

        def never(prompt=""):
            raise AssertionError("a poisoned session read stdin")

        monkeypatch.setattr(cli_chat, "_read_line_blocking", never)
        monkeypatch.setattr("builtins.input", never)

        provider = self._GatedProvider(self._event(), [])
        with pytest.raises(cli_chat.StdinPoisonedError):
            await cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate())
        with pytest.raises(cli_chat.StdinPoisonedError):
            await cli_chat._interactive(provider, types.SimpleNamespace())

    @pytest.mark.asyncio
    async def test_teardown_never_awaits_the_backend(self, monkeypatch):
        """Cancelling frees the coroutine, not the reader thread, and a wedged
        transport must not swallow the cancellation being delivered.

        The abandoned reader stays parked and takes the next line the user types
        -- measured -- so stdin has to be marked unusable. And ``CancelledError``
        has already been raised once with nothing to re-deliver it, so awaiting a
        ``reject_tool`` that never returns would leave the Ctrl-C that asked for
        this teardown unable to land.
        """
        import kiro_crew.cli_chat as cli_chat

        entered = threading.Event()

        class _HungReject(self._GatedProvider):
            async def reject_tool(self, request_id):
                self.trace.append(("reject", request_id, False))
                await asyncio.Event().wait()  # never returns

        def blocking_read(prompt=""):
            entered.set()
            threading.Event().wait(timeout=self._TIMEOUT)
            return "a"

        trace: list = []
        self._patch_env(monkeypatch, trace=trace)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", blocking_read)
        provider = _HungReject(self._event(), trace)
        task = asyncio.create_task(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate())
        )
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=self._TIMEOUT)
        assert cli_chat._stdin_poisoned is True
        assert provider.calls == []  # nothing was awaited on the way out
        assert [s[1]["error"] for s in trace if s[0] == "sel"] == ["session_aborted"]

    # ── Unchanged behaviour ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_stream_without_a_permission_request_is_unchanged(self, capsys):
        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        class _PlainProvider(self._GatedProvider):
            async def stream(self, message):
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello ")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="world")
                yield LLMEvent(kind=EVENT_COMPLETE)

        provider = _PlainProvider(self._event(), [])
        await cli_chat._send_and_print(provider, "hi")
        assert capsys.readouterr().out == "hello world\n"
        assert provider.calls == []

    # ── Terminal controls on the consent surface ─────────────────────────

    @pytest.mark.asyncio
    async def test_osc52_title_cannot_reach_the_terminal(self, monkeypatch, capsys):
        # An OSC 52 sequence in a model-authored title writes the user's
        # clipboard if it reaches the terminal. Neither ESC nor BEL -- the
        # sequence's introducer and terminator -- may survive to the prompt.
        title = "Read file\x1b]52;c;aGVsbG8=\x07 please"
        await self._drive(monkeypatch, event=self._event(title=title))
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\x07" not in out
        assert "]52;c;aGVsbG8=" in out  # neutralised to inert text, not dropped
        assert "Read file" in out

    @pytest.mark.asyncio
    async def test_csi_sequence_cannot_reach_the_terminal(self, monkeypatch, capsys):
        # CSI can move the cursor and erase what is already drawn, so a title
        # could repaint the question the user is answering. The newline is part
        # of the same class: a multi-line title pushes the prompt off screen.
        title = "Delete\x1b[2K\x1b[1Anothing\nimportant"
        await self._drive(monkeypatch, event=self._event(title=title))
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if ln.startswith("Permission required:"))
        assert "\x1b" not in line
        assert line == "Permission required: Delete [2K [1Anothing important"

    @pytest.mark.asyncio
    async def test_control_characters_in_a_command_are_neutralised(self, monkeypatch, capsys):
        # The shell command is untrusted display text on the same surface, and
        # keeps its existing one-line collapse contract.
        event = self._event(title="Run it", command="echo \x1b]52;c;x\x07hi\nls")
        await self._drive(monkeypatch, event=event)
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if ln.startswith("Command:"))
        assert "\x1b" not in line and "\x07" not in line
        assert line == "Command: echo ]52;c;x hi ls"

    @pytest.mark.asyncio
    async def test_ordinary_title_and_command_display_unchanged(self, monkeypatch, capsys):
        # The control-stripping must not disturb ordinary text: this is the
        # control for the three cases above.
        event = self._event(title="Run the test suite", command="pytest -q test/test_cli.py")
        await self._drive(monkeypatch, event=event)
        out = capsys.readouterr().out
        assert "Permission required: Run the test suite" in out
        assert "Command: pytest -q test/test_cli.py" in out

    # ── Canonical MCP identity reaches the shared gate ───────────────────

    @pytest.mark.asyncio
    async def test_canonical_mcp_identity_is_forwarded_and_denies(self, monkeypatch):
        # The wiring guard: a permission_request carrying the trusted _meta.kiro
        # server + tool names must reach HookManager as those fields, so a
        # governance rule naming the canonical mcp__server__tool denies the call
        # -- the backend is rejected and the human is never asked.
        import kiro_crew.cli_chat as cli_chat
        import kiro_crew.hooks as hooks_mod
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        seen: list[str] = []

        def fake_gov(ctx, name, *a, **k):
            seen.append(name)
            if name == "mcp__weather:srv__wipe_disk":
                return "Blocked by governance policy: denied"
            return None

        monkeypatch.setattr(hooks_mod, "_governance_denial", fake_gov)
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=7,
            title="Check the forecast",  # benign model-authored prose
            tool_kind="other",
            mcp_server_name="weather:srv",
            tool_name="wipe_disk",
            options=[{"id": "allow", "label": "Allow Once"}],
        )
        trace: list = []
        provider = self._GatedProvider(event, trace)
        reads = self._patch_env(monkeypatch, tty=True, trace=trace, answer="a")
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert "mcp__weather:srv__wipe_disk" in seen  # forwarded, not just the title
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0  # the human was never asked
        assert [s[1]["error"] for s in trace if s[0] == "sel"] == ["hook_deny"]


class TestChatProviderShutdown:
    """``provider.start()`` hands back a live backend process, so every exit from
    the turn -- return, error, or cancellation -- has to run ``shutdown()``."""

    class _FakeProvider:
        def __init__(self):
            self.started = 0
            self.shutdowns = 0

        async def start(self):
            self.started += 1

        async def shutdown(self):
            self.shutdowns += 1

        def context_usage_pct(self):
            return 0.0

    def _patch(self, monkeypatch, provider):
        import kiro_crew.cli_chat as cli_chat
        from kiro_crew.config import KiroCrewConfig

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: KiroCrewConfig()))
        monkeypatch.setattr(
            cli_chat, "build_provider_factory", lambda cfg: (lambda *a, **k: provider)
        )
        monkeypatch.setattr(cli_chat, "_build_tool_gate", lambda agent: None)

    @pytest.mark.asyncio
    async def test_shutdown_runs_when_the_turn_is_cancelled(self, monkeypatch):
        # A permission prompt cancelled at the terminal raises through the turn
        # by design. The backend must still be torn down, and the cancellation
        # must still reach the caller.
        import kiro_crew.cli_chat as cli_chat

        provider = self._FakeProvider()
        self._patch(monkeypatch, provider)

        async def cancelled(*a, **k):
            raise asyncio.CancelledError

        monkeypatch.setattr(cli_chat, "_send_and_print", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1

    @pytest.mark.asyncio
    async def test_shutdown_runs_when_the_turn_raises(self, monkeypatch):
        import kiro_crew.cli_chat as cli_chat

        provider = self._FakeProvider()
        self._patch(monkeypatch, provider)

        async def boom(*a, **k):
            raise RuntimeError("backend died")

        monkeypatch.setattr(cli_chat, "_send_and_print", boom)
        with pytest.raises(RuntimeError, match="backend died"):
            await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1

    @pytest.mark.asyncio
    async def test_shutdown_runs_exactly_once_on_the_normal_path(self, monkeypatch):
        import kiro_crew.cli_chat as cli_chat

        provider = self._FakeProvider()
        self._patch(monkeypatch, provider)

        async def ok(*a, **k):
            return None

        monkeypatch.setattr(cli_chat, "_send_and_print", ok)
        await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1

    @pytest.mark.asyncio
    async def test_gc_runs_even_when_shutdown_raises(self, monkeypatch):
        # gc.collect() collects the subprocess transports while the loop is
        # still open; a failing shutdown must not skip it, and must not replace
        # the exception already propagating.
        import kiro_crew.cli_chat as cli_chat

        class _BadShutdown(self._FakeProvider):
            async def shutdown(self):
                self.shutdowns += 1
                raise RuntimeError("shutdown failed")

        provider = _BadShutdown()
        self._patch(monkeypatch, provider)
        collected = {"n": 0}
        monkeypatch.setattr(cli_chat.gc, "collect", lambda: collected.__setitem__("n", 1))

        async def cancelled(*a, **k):
            raise asyncio.CancelledError

        monkeypatch.setattr(cli_chat, "_send_and_print", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await cli_chat._chat("hello", None)
        assert provider.shutdowns == 1
        assert collected["n"] == 1
