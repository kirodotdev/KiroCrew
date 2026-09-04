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
                # sys.executable, not a literal like /usr/local/bin/kirocrew:
                # doctor's dead-path scan stats every absolute command for real
                # (no shutil.which stub covers it), so the fixture's "healthy"
                # spec must name a binary that actually exists on the runner.
                "command": sys.executable,
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
    default (Slack-less) so doctor runs are deterministic and isolated.

    Speech-to-Text is switched off on top of the defaults, where it ships ON. Its
    section reports the recogniser, which lives in the optional ``voice`` extra, so
    leaving it enabled makes every doctor test in this file depend on whether the
    runner installed that wheel, and on POSIX an absent one is a hard issue: the
    tests asserting ``_doctor()`` does not exit would then report the host rather
    than the behaviour they name. The STT arms pin it back on for themselves.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    def _pristine() -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.stt.enabled = False
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _pristine()))
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

    @pytest.fixture(autouse=True)
    def _hermetic_sandbox(self, monkeypatch):
        """Pin the Sandbox section host-independently: a CI runner can restrict
        user namespaces with no profile installed, which is a genuine issue on
        that HOST (doctor exits 1 for it) but not the subject of these tests."""
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc.sandbox, "detect_backend", lambda config_mode="auto": "namespace")

    def test_doctor_with_kiro(self, tmp_path):
        agent_file = tmp_path / "kirocrew.json"
        # A minimally healthy agent config so doctor walks the whole MCP
        # section cleanly and doesn't exit on "missing from mcpServers".
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch(
                "kiro_crew.cli_doctor.shutil.which",
                side_effect=lambda b, **_kw: f"/usr/local/bin/{b}",
            ),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            _doctor()

    @staticmethod
    def _stt_config(monkeypatch, **fields):
        """Re-enable STT on the pinned default config, with *fields* applied.

        The autouse ``_hermetic_config`` fixture pins STT OFF so unrelated doctor
        tests never reach this section, so any test that wants it has to ask.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        def _cfg() -> KiroCrewConfig:
            cfg = KiroCrewConfig()
            cfg.stt.enabled = True
            for name, value in fields.items():
                setattr(cfg.stt, name, value)
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _cfg()))

    def test_doctor_windows_missing_stt_engine_and_ffmpeg_is_non_fatal(self, tmp_path, monkeypatch):
        """On Windows, STT ships enabled-by-default but the recogniser extra and
        ffmpeg are not dependencies there. Reporting them as hard issues made
        `doctor` exit 1 on a healthy install and broke the guide's
        `doctor && gateway` chain, so they must be non-fatal notes: doctor exits 0."""
        import kiro_crew.cli_doctor as _doc

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        monkeypatch.setattr(_doc.platform_compat, "IS_WINDOWS", True)
        self._stt_config(monkeypatch, provider="local")
        monkeypatch.setattr(
            _doc,
            "availability_detail",
            lambda cfg=None: _doc.stt.Availability(
                False, _doc.stt.CODE_EXTRA_MISSING, "needs the voice extra"
            ),
        )
        # Left unpatched this mutates the process PATH for every later test.
        monkeypatch.setattr(_doc, "ensure_ffmpeg_in_path", lambda: None)

        def _which(binary, **_kw):
            # Everything resolves EXCEPT ffmpeg.
            if binary == "ffmpeg":
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
            # Must NOT raise SystemExit(1): both STT gaps are notes on Windows.
            _doctor()

    @pytest.mark.parametrize(
        "is_windows, expected_mark",
        [(False, "❌"), (True, "⚠️ ")],
        ids=["posix-fatal", "windows-note"],
    )
    def test_doctor_stt_marker_arms_match_platform(
        self, tmp_path, capsys, monkeypatch, is_windows, expected_mark
    ):
        """The engine/ffmpeg severity marker is derived once (stt_mark) from
        stt_fatal: a hard-issue mark on POSIX, a note mark on Windows. Pins
        both arms byte-for-byte — including the note mark's trailing pad
        space, which keeps the report columns aligned — AND that the twin
        report lines can never disagree, so a one-arm edit to either site
        fails here (issue #5096)."""
        import kiro_crew.cli_doctor as _doc

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        monkeypatch.setattr(_doc.platform_compat, "IS_WINDOWS", is_windows)
        self._stt_config(monkeypatch, provider="local")
        # Neither the recogniser nor ffmpeg is present, which is what routes both
        # report lines through the marker under test.
        monkeypatch.setattr(
            _doc,
            "availability_detail",
            lambda cfg=None: _doc.stt.Availability(
                False, _doc.stt.CODE_EXTRA_MISSING, "no recogniser here"
            ),
        )
        monkeypatch.setattr(_doc, "ensure_ffmpeg_in_path", lambda: None)

        def _which(binary, **_kw):
            if binary == "ffmpeg":
                return None
            return f"/usr/local/bin/{binary}"

        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=_which),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            # On POSIX the two gaps are hard issues and doctor exits 1; on
            # Windows they are notes. Exit semantics are pinned elsewhere —
            # here only the printed markers matter.
            try:
                _doctor()
            except SystemExit:
                pass
        out = capsys.readouterr().out
        # Exact literals from the production f-strings: any drift in glyph,
        # variation selector, or the note arm's padding space fails here.
        assert f"  engine:      {expected_mark} no recogniser here" in out
        assert f"  ffmpeg:      {expected_mark} not found" in out

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
            patch(
                "kiro_crew.cli_doctor.shutil.which",
                side_effect=lambda b, **_kw: f"/usr/local/bin/{b}",
            ),
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
            patch(
                "kiro_crew.cli_doctor.shutil.which",
                side_effect=lambda b, **_kw: f"/usr/local/bin/{b}",
            ),
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
            patch(
                "kiro_crew.cli_doctor.shutil.which",
                side_effect=lambda b, **_kw: f"/usr/local/bin/{b}",
            ),
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
            patch(
                "kiro_crew.cli_doctor.shutil.which",
                side_effect=lambda b, **_kw: f"/usr/local/bin/{b}",
            ),
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


# The _update tests simulate a source tree whose git calls are faked. The project
# root has to be a REAL directory, because update detection resolves git's answer
# to one — so each test takes it from its own tmp_path rather than a module-level
# temp dir, which would be created at collection time and outlive the run.


def _patch_path():
    """Mock Path so .install-method is absent and the .brazil dir exists."""
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


@contextlib.contextmanager
def _git_resolvable():
    """Pin git's resolution so these tests do not depend on the host's layout.

    The probe resolves git from fixed system directories rather than ``PATH``, and
    on Windows git legitimately lives under ``C:\\Program Files\\Git`` — not in
    System32 — so the real lookup declines and the probe never reaches the mocked
    ``subprocess.run`` these tests drive detection through. That is correct product
    behaviour (it degrades to the on-disk repository markers), but it makes the
    test assert the host's git layout instead of the failure handling it is about.
    """
    with patch(
        "kiro_crew.platform.update_capability.trusted_system_bin",
        return_value="/usr/bin/git",
    ):
        yield


class TestUpdateFailures:
    """Tests for _update build-step failure handling (public pip/git flow).

    The Brazil ``brazil-build`` step was removed during de-Amazoning; the
    public update flow is git fetch/reset + npm build + ``pip install -e .``.
    A non-zero return code from a critical step exits with code 1.
    """

    def test_git_fetch_failure_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if cmd and "--show-toplevel" in cmd:
                m.stdout = str(tmp_path)
            elif cmd and "rev-parse" in cmd:
                m.stdout = "beta-braveheart"
            if cmd and "fetch" in cmd:
                m.returncode = 1
                m.stderr = "network error"
            return m

        with _patch_path(), _git_resolvable(), patch("subprocess.run", side_effect=_side_effect):
            try:
                _update()
                assert False, "Expected SystemExit"
            except SystemExit as e:
                assert e.code == 1

    def test_pip_install_failure_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if cmd and "--show-toplevel" in cmd:
                m.stdout = str(tmp_path)
            elif cmd and "rev-parse" in cmd:
                m.stdout = "beta-braveheart"
            # git diff --quiet returns 1 when there ARE new commits
            if cmd and "diff" in cmd and "--quiet" in cmd:
                m.returncode = 1
            # pip install -e . fails
            if cmd and "pip" in cmd and "install" in cmd:
                m.returncode = 1
                # BYTES: the install captures without text=True.
                m.stderr = b"build failed"
                m.stdout = b""
            return m

        with (
            _patch_path(),
            _git_resolvable(),
            patch("kiro_crew.cli_server.shutil.which", return_value=None),
            patch("kiro_crew.cli_server.build_frontend_sync"),
            patch("kiro_crew.cli._ensure_node"),
            # Pin the install route to the reinstall this test is about; the real
            # probe reads the test interpreter's own Scripts dir, and the origin
            # guard would otherwise run the stubbed interpreter for its answer.
            patch("kiro_crew.dep_sync.locked_console_scripts", return_value=[]),
            patch("kiro_crew.dep_sync.venv_not_mapped_to", return_value=None),
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

    def test_cron_remove_emits_sel_audit(self):
        # Single-job delete must be SEL-audited like cron.add/cron.update:
        # after the job vanishes from crons.json the audit trail is the only
        # way to tell a deliberate delete from data loss.
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

    def test_cron_remove_not_found_audits_not_found(self):
        with (
            patch("kiro_crew.cli_commands.CronService") as mock_svc_cls,
            patch("kiro_crew.cli_commands.sel") as mock_sel,
        ):
            mock_svc = mock_svc_cls.return_value
            mock_svc.remove_job.return_value = False
            args = argparse.Namespace(cron_action="remove", job_id="ghost")
            _cron(args)
            mock_svc.remove_job.assert_called_once_with("ghost", actor="cli", source="cli")
            mock_sel.return_value.log_api_access.assert_not_called()

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
            with (
                patch.object(sys, "argv", ["kirocrew", "cron", "list"]),
                patch("kiro_crew.cli_commands._cron", lambda _ns: dispatched.append(True)),
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
        with (
            patch.object(sys, "argv", ["kirocrew", "cron", "list"]),
            patch("kiro_crew.cli_commands._cron", lambda _ns: dispatched.append(True)),
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
        # The companion tier record must be dropped with it: a stale inherited
        # level would be read as the ACTIVE tier by a descendant's passthrough
        # and corrupt its downgrade audit.
        monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "strict")
        # A trivial subcommand so main() dispatches and returns cleanly; assert
        # the marker was popped before dispatch (patch the target to observe).
        argv = ["kirocrew", "cron", "list"]
        seen = {}

        def _capture(_ns):
            seen["marker"] = os.environ.get("KIROCREW_SANDBOX_ACTIVE")
            seen["level"] = os.environ.get("KIROCREW_SANDBOX_LEVEL")

        with patch.object(sys, "argv", argv), patch("kiro_crew.cli_commands._cron", _capture):
            from kiro_crew.cli import main

            main()
        assert seen.get("marker") is None
        assert seen.get("level") is None
        assert os.environ.get("KIROCREW_SANDBOX_ACTIVE") is None
        assert os.environ.get("KIROCREW_SANDBOX_LEVEL") is None


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
        fake_tzlocal = types.SimpleNamespace(get_localzone_name=lambda: "America/Los_Angeles")
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

    def test_packaged_manifest_declares_complete_oauth_scopes(self):
        import yaml

        from kiro_crew import slack_manifest

        manifest = yaml.safe_load(slack_manifest.render("scope-test"))
        assert manifest["oauth_config"]["scopes"] == {
            "bot": [
                "app_mentions:read",
                "channels:history",
                "channels:read",
                "chat:write",
                "commands",
                "files:read",
                "files:write",
                "groups:history",
                "groups:read",
                "im:history",
                "im:read",
                "im:write",
                "reactions:write",
                "users:read",
            ],
            "user": [
                "channels:history",
                "channels:read",
                "groups:history",
                "groups:read",
                "im:history",
                "im:read",
                "mpim:history",
                "mpim:read",
                "search:read",
                "users:read",
            ],
        }
        bot_events = manifest["settings"]["event_subscriptions"]["bot_events"]
        assert "message.groups" in bot_events

    def _patch_template(
        self, content="name: KiroCrew-{{ALIAS}}\ndisplay_name: KiroCrew-{{ALIAS}}\n"
    ):
        """Patch importlib.resources.files to return a fake template.

        Patched on ``slack_manifest`` — the single module that reads the packaged
        template for the CLI, the dashboard handler, and the exfil validator.
        """
        mock_resource = MagicMock()
        mock_resource.joinpath.return_value.read_text.return_value = content
        return patch("kiro_crew.slack_manifest._pkg_files", return_value=mock_resource)

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
        with patch("kiro_crew.slack_manifest._pkg_files", return_value=mock_resource):
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
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _logout(5476)  # Should not raise

    def test_logout_gateway_not_running(self, tmp_path, monkeypatch):
        """Missing secret file means gateway not running."""
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "")

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
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")

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
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")

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
            # macOS framework build: the vendored interpreter's basename is
            # "Python" (capital P) — a case-sensitive startswith("python") missed
            # it, so stop/restart no-oped on macOS framework-build installs
            # (Toolbox being the common one).
            "/Library/Frameworks/Python.framework/Versions/3.10/Resources/"
            "Python.app/Contents/MacOS/Python -m kiro_crew gateway --no-open",
            "Python -m kiro_crew gateway",
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
    def _no_active_service(self):
        # The denied-service branch consults ``is_service_active()`` after a
        # refused ``restart_service()``. The real implementation shells out to
        # systemctl/launchctl, so an unmocked call would make these tests
        # depend on whether the BUILD HOST runs a kirocrew service. Pin it
        # False; the denied-branch tests override it per-test.
        with patch(
            "kiro_crew.cli_server.service_controller.is_service_active",
            return_value=False,
        ):
            yield

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

    def test_active_service_restart_denied_fails_loud_with_remedy(self, capsys):
        # A system-scope unit refuses an unprivileged `systemctl restart`
        # ("Interactive authentication required"), while the unit stays
        # active. Falling through to the listener path is a silent no-op on a
        # unix-socket deployment: nothing listens on TCP, so nothing is
        # stopped, and the ORIGINAL gateway keeps running while the command
        # reads like a restart. The command must instead fail loudly and name
        # the privileged command the operator has to run themselves.
        from kiro_crew import cli_server

        mock_sel = MagicMock()

        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=True,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.manual_restart_hint",
                return_value="sudo systemctl restart kirocrew",
            ),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
            patch("kiro_crew.cli_server._stop") as mock_stop,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        # The listener fallback must never be entered: it cannot see a
        # unix-socket service gateway and would spawn a doomed competitor.
        mock_ports.assert_not_called()
        mock_stop.assert_not_called()
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "NOT restarted" in out
        assert "sudo systemctl restart kirocrew" in out
        audit = mock_sel.log_api_access.call_args.kwargs
        assert audit["outcome"] == "denied"
        assert "reason=service_restart_denied" in audit["resources"]

    def test_inactive_service_still_falls_through_after_refused_restart(self):
        # ``restart_service()`` returning False because NO service is active
        # must keep taking the foreground path — the denied diagnostic is only
        # for a unit that is active right now yet refused the restart.
        from kiro_crew import cli_server

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
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
        ):
            cli_server._restart(None)
        mock_spawn.assert_called_once()

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
            # Keep the denied-service branch out of these verdict tests (and
            # keep them off the host's real systemctl/launchctl state).
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
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
        return patch("kiro_crew.cli_server.platform_compat.find_listening_pids", return_value=pids)

    def _owner(self, uid):
        return patch("kiro_crew.cli_server.platform_compat.process_owner_uid", return_value=uid)

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
        assert "http://localhost:{port}" not in src
        for fn in (cli_server._token, cli_server._logout, cli_server._print_token_url):
            body = inspect.getsource(fn)
            if "http://" in body:
                assert "_CLI_LOOPBACK" in body, fn.__name__

    def test_printed_browser_url_still_uses_the_canonical_host(self):
        """The URL handed to the browser must NOT be switched to 127.0.0.1 —
        the SPA's per-origin localStorage is keyed on `localhost`."""
        import inspect

        from kiro_crew import cli_server

        # The URL printing lives in _emit_session_urls, which _token calls. Inspect the
        # function that actually owns the invariant, and assert the call still happens,
        # so this stays a real check rather than passing on an empty search.
        body = inspect.getsource(cli_server._emit_session_urls)
        assert "resolve_dashboard_host(local_only=True)" in body
        assert 'print(f"http://{host}:{port}?token={token}")' in body
        assert "_emit_session_urls(" in inspect.getsource(cli_server._token)


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
            patch(
                "kiro_crew.cli_doctor.shutil.which",
                side_effect=lambda b, **_kw: f"/usr/local/bin/{b}",
            ),
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

        The spec gate is pinned OPEN: this guard is about what doctor does to a
        server it repairs, and a closed gate (the CI default — feature disabled)
        would skip the repair entirely and assert nothing.
        """
        from kiro_crew import agent
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_path)
        # Start from the state an upgrade leaves behind: servers registered, no
        # tool refs at all, so every ref doctor could add is attributable to it.
        data = json.loads(agent_path.read_text(encoding="utf-8"))
        data["tools"] = []
        data["allowedTools"] = []
        agent_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        gated_open = dict(agent._MANAGED_MCP_SERVERS["kirocrew-computer"])
        gated_open["spec_gate"] = lambda: True
        issues: list[str] = []
        with (
            patch.dict(agent._MANAGED_MCP_SERVERS, {"kirocrew-computer": gated_open}),
            self._mock_probe({}),
        ):
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

        Gate pinned OPEN like the mint test above: the preservation rule is
        exercised on the path where doctor walks the full entry.
        """
        from kiro_crew import agent
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_path)
        gated_open = dict(agent._MANAGED_MCP_SERVERS["kirocrew-computer"])
        gated_open["spec_gate"] = lambda: True
        issues: list[str] = []
        with (
            patch.dict(agent._MANAGED_MCP_SERVERS, {"kirocrew-computer": gated_open}),
            self._mock_probe({}),
        ):
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

    @pytest.mark.parametrize("content", ["[1, 2, 3]", "42", "null", "true", '"a string"'])
    def test_valid_json_non_object_agent_config_does_not_crash(self, tmp_path, capsys, content):
        """A spec that is valid JSON but not an object (a list, a scalar,
        null) parses fine, so the json.loads try/except never fires — but
        every .get() on the result would raise AttributeError. Doctor must
        coerce it to an empty config, say what is wrong, and report cleanly,
        same as the truncated case above — and it must never rewrite the
        user's file with the coerced empty config."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        agent_path.write_text(content)

        issues: list[str] = []
        with self._mock_probe({}) as probe_mock:
            _doctor_mcp_tools(agent_path, issues)

        out = capsys.readouterr().out
        # The defect itself is named, not just its downstream symptoms.
        assert "agent spec is not a JSON object" in out
        # Empty config → both managed servers report missing from mcpServers.
        assert "@kirocrew-core: ❌ missing from mcpServers" in out
        assert "@kirocrew-cron: ❌ missing from mcpServers" in out
        # No probe attempted since no server spec survived the coercion.
        probe_mock.assert_not_called()
        # The no-clobber contract: doctor diagnoses the broken spec, it never
        # persists the coerced empty config over the user's original file.
        assert agent_path.read_text() == content
        assert "Auto-fixed" not in out


class TestDoctorMcpSpecGate:
    """Doctor's MCP checks consult the same ``spec_gate`` emission does (#6548).

    Spec emission (``agent.build_agent_config`` / ``_refresh_dynamic_fields``)
    deliberately omits — and retracts — the ``mcpServers`` entry of a managed
    server whose ``spec_gate`` is closed (feature disabled, or no driver for
    this platform). Before this gate, doctor demanded the entry's presence
    unconditionally, so every non-macOS host reported an unfixable
    ``@kirocrew-computer: missing from mcpServers`` and `kirocrew setup` could
    never clear it: the two sides drifted because only one consulted the gate.

    Each test pins the gate through the same registry doctor resolves it from
    (``agent._MANAGED_MCP_SERVERS``), so the resolution seam is exercised too,
    and no test depends on the host's real enable-state or platform.
    """

    def _mock_probe(self, seen: list[str]):
        """Patch ``probe_server`` to record which servers doctor launches."""
        from kiro_crew.mcp_discovery import McpServerInfo

        async def fake(target: McpServerInfo) -> McpServerInfo:
            seen.append(target.name)
            target.status = "ok"
            target.tools = []
            target.error = ""
            return target

        return patch("kiro_crew.cli_doctor.probe_server", side_effect=fake)

    def _pin_gate(self, gate):
        """Return a patch pinning ``kirocrew-computer``'s spec gate to *gate*."""
        from kiro_crew import agent

        pinned = dict(agent._MANAGED_MCP_SERVERS["kirocrew-computer"])
        pinned["spec_gate"] = gate
        return patch.dict(agent._MANAGED_MCP_SERVERS, {"kirocrew-computer": pinned})

    def _config_without_computer(self, path: Path) -> None:
        """Servers as spec emission writes them on a gated-off host: every
        managed always-on server EXCEPT the gated one, refs for all of them
        (emission deliberately leaves the ``tools`` ref alone when it retracts
        the entry, so ref-present-entry-absent is the designed steady state)."""
        from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS

        present = [n for n in ALWAYS_ON_BIN_MCP_SERVERS if n != "kirocrew-computer"]
        _write_agent_config(
            path,
            tools=[f"@{n}" for n in ALWAYS_ON_BIN_MCP_SERVERS],
            allowed=[f"@{n}" for n in present],
            servers={
                n: {"command": sys.executable, "args": [f"mcp-{n.split('-', 1)[1]}"]}
                for n in present
            },
        )

    def test_gate_closed_absent_entry_is_informational_not_an_issue(self, tmp_path, capsys):
        """Gate closed + entry absent = the healthy state on this host.

        The exact #6548 report: no hard error, no ``issues`` entry (so doctor
        exits 0), no auto-mount of the ref, and the line says WHY the server is
        absent rather than looking like a silent skip.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        before = agent_path.read_text(encoding="utf-8")
        issues: list[str] = []
        probed: list[str] = []
        with self._pin_gate(lambda: False), self._mock_probe(probed):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ℹ️  gated off on this host" in out
        assert "feature disabled or no driver" in out
        assert "@kirocrew-computer: ❌" not in out
        assert issues == []
        # Nothing mounted, minted, or probed for the gated-off server — and the
        # stale ref emission leaves behind is not reported as "half a grant".
        assert "kirocrew-computer" not in probed
        assert "referenced in tools but absent" not in out
        assert agent_path.read_text(encoding="utf-8") == before

    def test_gate_open_absent_entry_keeps_the_hard_error(self, tmp_path, capsys):
        """Gate open + entry absent is still a broken install and MUST fail."""
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        issues: list[str] = []
        with self._pin_gate(lambda: True), self._mock_probe([]):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ❌ missing from mcpServers (re-run `kirocrew setup`)" in out
        assert "@kirocrew-computer config" in issues

    def test_gate_raising_is_treated_as_open(self, tmp_path, capsys):
        """A gate that raises PAST ITS OWN HANDLING must not silence the error.

        This pins the helper's contract-level fail direction — deliberately
        the opposite of emission's ``_gated_off_servers()`` (which treats a
        raising gate as closed, because its open position spawns a backend):
        here "closed" is what suppresses the error, so a gate the helper
        cannot evaluate reports the missing entry. Note the shipped
        computer-use gate catches its own internal errors and ANSWERS False,
        so this path covers registry/contract failures and any future gate
        without an internal handler; the shipped gate's swallowed-error case
        is pinned separately below.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        def explode() -> bool:
            raise RuntimeError("gate unreadable")

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        issues: list[str] = []
        with self._pin_gate(explode), self._mock_probe([]):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ❌ missing from mcpServers" in out
        assert "@kirocrew-computer config" in issues
        assert "gated off" not in out

    def test_shipped_gate_swallowing_internal_errors_reads_as_closed(self, tmp_path, capsys):
        """The shipped gate's own fail-closed answer is reported as-is.

        ``agent._computer_use_spec_gate`` catches its internal errors and
        answers False (its documented posture: an unreadable keystone must
        never hand out the desktop), so doctor cannot distinguish "unreadable
        internals" from "policy closed" through the boolean — and reporting
        closed is the emission-CONSISTENT answer: in that same state the entry
        genuinely is omitted from every emitted spec, so the ℹ️ line describes
        what the system actually does. This test pins that DELIVERED semantic
        so the helper's docstring cannot silently overclaim again; if the
        gate's exception contract ever changes, this test and the one above
        say exactly which behavior moved.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools

        agent_path = tmp_path / "kirocrew.json"
        self._config_without_computer(agent_path)
        issues: list[str] = []
        with (
            patch(
                "kiro_crew.computer_use.enable_state.is_enabled",
                side_effect=RuntimeError("keystone unreadable"),
            ),
            self._mock_probe([]),
        ):
            # No _pin_gate: the REAL registry gate runs, swallows the raise,
            # and answers False — the exact production shape.
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "@kirocrew-computer: ℹ️  gated off on this host" in out
        assert issues == []

    def test_gate_closed_stale_entry_is_not_mounted_or_probed(self, tmp_path, capsys):
        """A leftover entry from when the gate was open must not deepen the hole.

        Doctor must not mount its ref into ``tools`` (kiro-cli would spawn a
        backend emission decided against), must not probe it (nothing should
        launch), and must not count it as an issue — the next config refresh
        retracts it.
        """
        from kiro_crew.cli_doctor import _doctor_mcp_tools
        from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS

        agent_path = tmp_path / "kirocrew.json"
        present = list(ALWAYS_ON_BIN_MCP_SERVERS)
        _write_agent_config(
            agent_path,
            tools=[f"@{n}" for n in present if n != "kirocrew-computer"],
            allowed=[],
            servers={
                n: {"command": sys.executable, "args": [f"mcp-{n.split('-', 1)[1]}"]}
                for n in present
            },
        )
        issues: list[str] = []
        probed: list[str] = []
        with self._pin_gate(lambda: False), self._mock_probe(probed):
            _doctor_mcp_tools(agent_path, issues)
        out = capsys.readouterr().out
        assert "stale mcpServers entry" in out
        assert issues == []
        assert "kirocrew-computer" not in probed
        # The other always-on servers were still probed — the skip is scoped.
        assert "kirocrew-core" in probed and "kirocrew-cron" in probed
        updated = json.loads(agent_path.read_text(encoding="utf-8"))
        assert "@kirocrew-computer" not in updated["tools"]
        assert "@kirocrew-computer" not in updated["allowedTools"]

    def test_governance_denominator_skips_an_absent_gated_off_server(
        self, tmp_path, monkeypatch, capsys
    ):
        """The MCP Governance section applies the same rule to its marker count.

        On a governed non-macOS host the gated-off server has no entry to mark,
        so counting it as expected would re-create the same unfixable
        "re-run `kirocrew setup --agent-only`" loop in this section.
        """
        from kiro_crew import cli_doctor
        from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS

        monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: True)

        class _Agent:
            mcp_registry_mode = True

        class _Cfg:
            agent = _Agent()

        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
        spec_path = tmp_path / "kirocrew.json"
        spec_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        n: {
                            "command": "kirocrew",
                            "args": [f"mcp-{n.split('-', 1)[1]}"],
                            "type": "registry",
                        }
                        for n in ALWAYS_ON_BIN_MCP_SERVERS
                        if n != "kirocrew-computer"
                    }
                }
            ),
            encoding="utf-8",
        )
        issues: list[str] = []
        with self._pin_gate(lambda: False):
            cli_doctor._doctor_mcp_governance(spec_path, issues)
        out = capsys.readouterr().out
        assert issues == []
        assert "markers missing" not in out
        # The gate-open direction keeps the failure: a genuinely missing
        # always-on server still reads as unmarked.
        issues2: list[str] = []
        with self._pin_gate(lambda: True):
            cli_doctor._doctor_mcp_governance(spec_path, issues2)
        assert issues2 == ["MCP registry markers"]


class TestDoctorStt:
    """Doctor's Speech-to-Text section, driven through ``_doctor()``.

    Every arm goes through the real entry point rather than a unit call, because
    what is being pinned is both halves of the contract: the report a user reads
    and the exit status their ``kirocrew doctor && kirocrew gateway`` chain
    depends on.
    """

    @pytest.fixture(autouse=True)
    def _hermetic_doctor(self, monkeypatch):
        """Pin every section EXCEPT Speech-to-Text.

        Same reasoning as ``TestDoctor``'s fixtures: the vendored embedding runtime
        and the sandbox backend are host facts, and each is a genuine issue on a
        machine that lacks it, which would make an exit-status assertion here report
        the runner instead of the STT arm under test.
        """
        import kiro_crew.cli_doctor as _doc

        _pin_default_config(monkeypatch)
        monkeypatch.setattr(_doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(_doc, "model_file_present", lambda path=None: False)
        monkeypatch.setattr(_doc.sandbox, "detect_backend", lambda config_mode="auto": "namespace")
        # POSIX severity: a missing prerequisite is a hard issue. The Windows note
        # arm is pinned in ``TestDoctor::test_doctor_stt_marker_arms_match_platform``.
        monkeypatch.setattr(_doc.platform_compat, "IS_WINDOWS", False)
        # Left real, this prepends to the process PATH for every later test.
        monkeypatch.setattr(_doc, "ensure_ffmpeg_in_path", lambda: None)

    @staticmethod
    def _stt(monkeypatch, **fields):
        """Enable STT with *fields* applied over the shipped defaults.

        Applied last, so a test that wants the off arm asks for it explicitly
        (``enabled=False``) rather than relying on what the autouse pin left behind.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        def _cfg() -> KiroCrewConfig:
            cfg = KiroCrewConfig()
            cfg.stt.enabled = True
            for name, value in fields.items():
                setattr(cfg.stt, name, value)
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _cfg()))

    @staticmethod
    def _engine(monkeypatch, availability):
        """Answer the recogniser probe without depending on the optional wheel."""
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc, "availability_detail", lambda cfg=None: availability)

    @staticmethod
    def _model_on_disk(monkeypatch, present):
        """Say whether the resolved catalog model is downloaded.

        Patched on the ``kiro_crew.stt`` namespace the doctor actually reads. The
        real predicate compares the file's size to the catalog's, and the smallest
        entry is 77 MB, so writing one is not an option a test has.
        """
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc.stt, "is_present", lambda model: present)

    @staticmethod
    def _stt_section(out: str) -> str:
        """Just the Speech-to-Text block.

        The embeddings section prints its own ``model:`` line, so an assertion
        about the ABSENCE of one has to be scoped or it proves nothing.
        """
        return out.split("Speech-to-Text", 1)[1].split("Slack Integration", 1)[0]

    def _report(self, tmp_path, capsys, *, ffmpeg=True, modules=None):
        """Run the doctor with everything outside STT stubbed.

        Returns the captured report and the exit status, so a test can assert that
        an STT gap does or does not fail the run.
        """
        _healthy_agent_file(tmp_path / "kirocrew.json")
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")

        def _which(binary, **_kw):
            if binary == "ffmpeg" and not ffmpeg:
                return None
            return f"/usr/local/bin/{binary}"

        # `ffmpeg` has to answer BOTH routes into `transcribe._find_ffmpeg`. The
        # bundled-interpreter branch never probes PATH: it lists the imageio-ffmpeg
        # package resource, which is a really-installed dev dependency here, so a
        # bundled arm left unpinned reports the test environment's decoder instead of
        # the arrangement it asked for -- and reports it as PRESENT, the permissive
        # answer. The value is unused: the report prints availability, not a path.
        packaged_decoder = "/kirocrew-bundle/Resources/ffmpeg" if ffmpeg else None

        code = 0
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=_which),
            patch(
                "kiro_crew.transcribe._packaged_ffmpeg_resource",
                return_value=packaged_decoder,
            ),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True),
            patch.dict("sys.modules", modules or {}),
        ):
            try:
                _doctor()
            except SystemExit as exc:
                code = int(exc.code or 0)
        return capsys.readouterr().out, code

    def test_doctor_stt_local_engine_and_model_ready(self, tmp_path, capsys, monkeypatch):
        """The ready state names the resolved catalog model AND where it sits, so
        an operator can see which weights a dictation will actually use."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="local", model="small")
        self._engine(monkeypatch, _doc.stt.Availability(True))
        self._model_on_disk(monkeypatch, True)

        out, code = self._report(tmp_path, capsys)

        section = self._stt_section(out)
        assert "provider:    ✅ local" in section
        assert "engine:      ✅ local recogniser loadable (whisper.cpp, in-process)" in section
        expected = _doc.stt.models_dir() / "ggml-small.bin"
        assert f"model:       ✅ small at {expected}" in section
        assert "ffmpeg:      ✅ available" in section
        assert code == 0

    def test_doctor_stt_local_model_not_downloaded_is_a_note(self, tmp_path, capsys, monkeypatch):
        """The weights are fetched on first use, so "not downloaded" is the normal
        first-run state and never an issue. Naming the size is the useful part,
        because that transfer is what a first dictation waits on."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="local", model="base")
        self._engine(monkeypatch, _doc.stt.Availability(True))
        self._model_on_disk(monkeypatch, False)

        out, code = self._report(tmp_path, capsys)

        # Derived from the catalog entry the doctor resolved, not restated: a
        # literal here goes stale the moment the pinned artifact changes.
        megabytes = _doc.stt.resolve_model("base").size_bytes // 1_000_000
        section = self._stt_section(out)
        assert (
            f"model:       ⏹ base not downloaded yet "
            f"({megabytes} MB, fetched on first use)" in section
        )
        assert code == 0

    def test_doctor_stt_local_engine_failure_names_its_code(self, tmp_path, capsys, monkeypatch):
        """An unloadable recogniser is a hard issue on POSIX, and the summary
        carries the machine-readable code.

        The code is what distinguishes "install the extra" from "this platform has
        no wheel", so a report that only said "speech recogniser" would send the
        user to the wrong fix.
        """
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="local")
        self._engine(
            monkeypatch,
            _doc.stt.Availability(
                False,
                _doc.stt.CODE_NO_WHEEL,
                "no prebuilt speech recogniser for Intel macOS",
            ),
        )

        out, code = self._report(tmp_path, capsys)

        assert "engine:      ❌ no prebuilt speech recogniser for Intel macOS" in out
        assert "❌ Fix these issues: " in out
        assert "speech recogniser (stt_no_wheel_for_platform)" in out
        assert code == 1

    def test_doctor_stt_disabled(self, tmp_path, capsys, monkeypatch):
        """Disabled is a choice, not a gap: no engine or model line is printed,
        and ffmpeg is reported as something this install does not need."""
        self._stt(monkeypatch, enabled=False)

        out, code = self._report(tmp_path, capsys, ffmpeg=False)

        section = self._stt_section(out)
        assert "status:      ⏹ disabled" in section
        assert "ffmpeg:      ⏭  not installed (not needed)" in section
        assert "engine:" not in section
        assert "model:" not in section
        # A prerequisite nothing needs cannot fail the run.
        assert code == 0

    def test_doctor_stt_ffmpeg_is_a_prerequisite_of_every_provider(
        self, tmp_path, capsys, monkeypatch
    ):
        """Reported for a cloud provider too. A Slack voice memo arrives as
        ogg/Opus and the dashboard records webm, so the only input that reaches
        any recogniser without ffmpeg is a 16 kHz mono WAV."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="transcribe")
        monkeypatch.setattr(_doc._plat, "system", lambda: "Linux")

        out, code = self._report(
            tmp_path,
            capsys,
            ffmpeg=False,
            modules={
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": MagicMock(),
            },
        )

        assert "ffmpeg:      ❌ not found" in out
        assert "drop a static ffmpeg build into ~/.local/bin" in out
        assert "reinstall Kiro Crew" not in out
        assert "❌ Fix these issues: " in out
        assert "ffmpeg" in out.split("❌ Fix these issues: ", 1)[1]
        assert code == 1

    def test_doctor_bundled_desktop_never_requests_a_system_ffmpeg_install(
        self, tmp_path, capsys, monkeypatch
    ):
        """A corrupt desktop payload is repaired by reinstalling the app, not by
        asking its user to manage brew, winget, apt, or a loose executable."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="transcribe")
        monkeypatch.setattr(_doc.platform_compat, "is_bundled_interpreter", lambda: True)
        out, _code = self._report(
            tmp_path,
            capsys,
            ffmpeg=False,
            modules={
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": MagicMock(),
            },
        )

        section = self._stt_section(out)
        assert "reinstall Kiro Crew (the bundled audio decoder is missing)" in section
        assert "brew install ffmpeg" not in section
        assert "winget install" not in section

    def test_doctor_stt_transcribe_provider(self, tmp_path, capsys, monkeypatch):
        """The local-only lines belong to the local provider. Printing an engine or
        model row for a cloud provider would advertise a dependency the operator's
        configuration does not have."""
        self._stt(monkeypatch, provider="transcribe", transcribe_region="us-west-2")
        # boto3 and amazon-transcribe are the OPTIONAL 'voice' extra and are not
        # ambiently importable in a clean env, so the ✅ arm has to be faked or the
        # assertion below depends on what the host happens to have installed.
        out, code = self._report(
            tmp_path,
            capsys,
            modules={
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": MagicMock(),
            },
        )

        section = self._stt_section(out)
        assert "provider:    ✅ transcribe" in section
        assert "transcribe:  ✅" in section
        assert "boto3:       ✅" in section
        assert "engine:" not in section
        assert "model:" not in section
        assert code == 0

    def test_doctor_stt_transcribe_amazon_transcribe_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and amazon_transcribe is not importable,
        doctor reports it as an OPTIONAL gap (public pip extra) and does NOT
        treat it as a hard failure."""
        self._stt(monkeypatch, provider="transcribe", transcribe_region="us-west-2")
        # setitem(sys.modules, ..., None) is the documented way to make an import
        # raise for a package that is already loaded at test time.
        out, code = self._report(tmp_path, capsys, modules={"amazon_transcribe.client": None})

        assert "transcribe:  ⏹ optional cloud STT not installed" in out
        # The cloud dependencies by name: `pip install kirocrew[voice]` resolves
        # nowhere (this project is on no index), and it would also drag in the
        # local recogniser this provider does not use.
        assert "amazon-transcribe" in out
        assert "kirocrew[" not in out
        assert code == 0

    def test_doctor_stt_transcribe_boto3_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and boto3 is not importable, doctor
        reports it as an OPTIONAL gap (public pip extra), not a hard failure."""
        self._stt(monkeypatch, provider="transcribe", transcribe_region="us-west-2")
        out, code = self._report(
            tmp_path,
            capsys,
            modules={
                # amazon_transcribe importable, to isolate the boto3 gap.
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": None,
            },
        )

        assert "boto3:       ⏹ optional AWS SDK not installed" in out
        # The cloud dependencies by name: `pip install kirocrew[voice]` resolves
        # nowhere (this project is on no index), and it would also drag in the
        # local recogniser this provider does not use.
        assert "amazon-transcribe" in out
        assert "kirocrew[" not in out
        assert code == 0

    def test_doctor_stt_apple_unsupported_host_names_its_code(self, tmp_path, capsys, monkeypatch):
        """Reaching a not-ok state here means the operator selected a provider this
        machine does not support, which is a real configuration fault rather than a
        first-run state, so it carries its code into the summary."""
        import kiro_crew.cli_doctor as _doc
        from kiro_crew.transcribe import CODE_APPLE_UNSUPPORTED

        self._stt(monkeypatch, provider="apple")
        self._engine(
            monkeypatch,
            _doc.stt.Availability(
                False,
                CODE_APPLE_UNSUPPORTED,
                "Apple on-device speech is macOS only",
            ),
        )

        out, code = self._report(tmp_path, capsys)

        section = self._stt_section(out)
        assert "apple:       ❌ Apple on-device speech is macOS only" in section
        # The local provider's rows must not appear for a host capability.
        assert "engine:" not in section
        assert "apple speech (stt_apple_unsupported)" in out
        assert code == 1

    def test_doctor_stt_apple_available(self, tmp_path, capsys, monkeypatch):
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="apple")
        self._engine(monkeypatch, _doc.stt.Availability(True))

        out, code = self._report(tmp_path, capsys)

        assert "apple:       ✅ on-device SpeechAnalyzer available" in out
        assert code == 0


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

    def test_logout_reads_secret_for_listener_port(self, monkeypatch):
        """_logout resolves the secret paired with the requested listener."""
        read_secret = MagicMock(return_value="test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", read_secret)

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp):
            _logout(5476)
        read_secret.assert_called_once_with(5476)

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

    def test_setup_slack_tokens_locks_env_to_owner(self, tmp_path, monkeypatch):
        """The credential write must route through atomic_write's owner
        lockdown: a bare chmod(0o600) is a silent no-op for Windows ACLs, and
        any lockdown applied only AFTER the tokens are on disk leaves them
        readable through the directory's inherited DACL in the failure window.
        The lockdown therefore lands on the temp file, before any token byte
        reaches it."""
        import os as os_mod

        import kiro_crew.cli_setup as cs
        from kiro_crew import platform_compat as pc

        env_file = tmp_path / ".env"
        monkeypatch.setattr("kiro_crew.cli_setup.env_path", lambda: env_file)
        inputs = iter(["y", "xapp-test", "xoxb-test", "U12345"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        seen: list[tuple[Path, bool]] = []
        real = pc.restrict_to_owner

        def _spy(path):
            # Record what the lockdown saw: the path, and whether the tokens
            # were already readable there at that moment.
            p = Path(path)
            seen.append((p.parent, "xoxb-test" in p.read_text(encoding="utf-8")))
            return real(path)

        monkeypatch.setattr(pc, "restrict_to_owner", _spy)
        cs._setup_slack_tokens()
        # Locked exactly once, on a file in the credential dir, while it was
        # still empty of tokens.
        assert seen == [(tmp_path, False)]
        assert "xoxb-test" in env_file.read_text(encoding="utf-8")
        assert not list(tmp_path.glob("*.tmp"))  # no temp residue
        if os_mod.name == "posix":
            assert env_file.stat().st_mode & 0o777 == 0o600

    def test_setup_slack_tokens_survives_a_lockdown_refusal(self, tmp_path, monkeypatch):
        """A host where the owner lockdown fails (e.g. SID resolution refused)
        must not abort the wizard with a traceback after the user already
        typed their tokens: the .env doctrine is enforce-and-warn, matching
        the dashboard credential writers."""
        import kiro_crew.cli_setup as cs
        from kiro_crew import platform_compat as pc

        env_file = tmp_path / ".env"
        monkeypatch.setattr("kiro_crew.cli_setup.env_path", lambda: env_file)
        inputs = iter(["y", "xapp-test", "xoxb-test", "U12345"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        monkeypatch.setattr(
            pc, "restrict_to_owner", MagicMock(side_effect=OSError("icacls failed"))
        )

        cs._setup_slack_tokens()  # must not raise
        assert "xoxb-test" in env_file.read_text(encoding="utf-8")
        assert not list(tmp_path.glob("*.tmp"))  # failure leaves no temp residue

    def test_setup_slack_tokens_aborts_when_shared_env_lock_is_held(
        self, tmp_path, monkeypatch, capsys
    ):
        """The Slack setup writer serializes on the SAME .env.lock the importer
        and the dashboard credential writers use, so it aborts (rather than
        racing the importer's commit and clobbering it) when another writer
        holds the lock — and leaves .env untouched."""
        import os as os_mod

        import kiro_crew.cli_setup as cs
        from kiro_crew import platform_compat as pc
        from kiro_crew.secrets.migrate import _env_lock_path

        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING=1\n", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.cli_setup.env_path", lambda: env_file)
        inputs = iter(["y", "xapp-test", "xoxb-test", "U12345"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        # Simulate another writer (e.g. `kirocrew secrets import`) holding the
        # shared advisory lock.
        lock_path = _env_lock_path(env_file)
        held_fd = os_mod.open(lock_path, os_mod.O_CREAT | os_mod.O_RDWR, 0o600)
        assert pc.try_acquire_lock(held_fd, exclusive=True)
        try:
            cs._setup_slack_tokens()  # must not raise
            # .env is untouched — the aborted save did not write the tokens.
            assert env_file.read_text(encoding="utf-8") == "EXISTING=1\n"
        finally:
            pc.release_lock(held_fd)
            os_mod.close(held_fd)


class TestSetupChannelGating:
    """`kirocrew setup` runs the Slack steps only with --slack.

    Messaging channels are optional: the default wizard must configure none and
    instead print the pointer to connect channels later, while `--slack` opts
    into the guided Slack credential + slash-command steps.
    """

    def _run_setup(self, monkeypatch, tmp_path, **kwargs):
        import kiro_crew.cli_setup as cs

        calls: list[str] = []
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        # Imported inside _setup_impl — patch at their source modules.
        monkeypatch.setattr(
            "kiro_crew.agent.install_agent", lambda clean=False: tmp_path / "agent.json"
        )
        # Mirror the real signature (bin_dir, *, claim_existing): the setup path
        # passes claim_existing=True, and a stub that refused it would fail here
        # for a reason that has nothing to do with channel gating.
        monkeypatch.setattr("kiro_crew.agent.ensure_kirocrew_on_path", lambda *a, **k: None)
        monkeypatch.setattr("kiro_crew.mcp_cleanup.clean_stale_managed_mcp", lambda: [])
        # Neutralize every unrelated wizard step so only the gating is under test.
        for name in (
            "_ensure_prerequisites",
            "_setup_workspace_dir",
            "_setup_sandbox_consent",
            "_setup_timezone",
            "_maybe_setup_dashboard_url",
            "_maybe_setup_custom_domain",
            "_maybe_setup_cloud",
            "_ensure_default_agent_in_config",
        ):
            monkeypatch.setattr(cs, name, lambda *a, **k: None)
        monkeypatch.setattr(cs, "_setup_slack_tokens", lambda: calls.append("slack_tokens"))
        monkeypatch.setattr(cs, "_setup_slash_command", lambda: calls.append("slash_command"))
        monkeypatch.setattr(cs, "_setup_whatsapp", lambda: calls.append("whatsapp"))
        # Conductor-skill step catches Exception and continues.
        monkeypatch.setattr(
            cs, "KiroCrewConfig", MagicMock(load=MagicMock(side_effect=RuntimeError("no config")))
        )
        # Keep the macOS-only desktop-app input() prompt off the path.
        monkeypatch.setattr(cs.platform, "system", lambda: "Linux")

        cs._setup_impl(**kwargs)
        return calls

    def test_default_setup_skips_slack_steps(self, tmp_path, monkeypatch, capsys):
        """Default wizard: no Slack prompts, prints the channels pointer instead."""
        calls = self._run_setup(monkeypatch, tmp_path)
        assert calls == []
        out = capsys.readouterr().out
        assert "Messaging Channels" in out
        assert "setup --slack" in out

    def test_slack_flag_opts_into_slack_steps(self, tmp_path, monkeypatch):
        """--slack runs the guided Slack credential + slash-command steps in order."""
        calls = self._run_setup(monkeypatch, tmp_path, slack=True)
        assert calls == ["slack_tokens", "slash_command"]

    def test_agent_only_with_slack_warns_and_skips_slack_steps(self, tmp_path, monkeypatch, capsys):
        """--agent-only --slack: no Slack steps run, but a notice explains why."""
        calls = self._run_setup(monkeypatch, tmp_path, agent_only=True, slack=True)
        assert calls == []
        out = capsys.readouterr().out
        assert "--slack is ignored with --agent-only" in out
        assert "setup --slack" in out

    def test_agent_only_without_slack_prints_no_notice(self, tmp_path, monkeypatch, capsys):
        """--agent-only alone: the guided-setup pointer is not printed."""
        calls = self._run_setup(monkeypatch, tmp_path, agent_only=True)
        assert calls == []
        out = capsys.readouterr().out
        assert "--slack is ignored" not in out

    def test_default_setup_names_whatsapp_among_the_connectable_channels(
        self, tmp_path, monkeypatch, capsys
    ):
        """The fallback blurb is where an operator learns which channels exist.
        Omitting WhatsApp made the only channel with an install prerequisite the
        one channel the wizard never mentions."""
        self._run_setup(monkeypatch, tmp_path)
        out = capsys.readouterr().out
        assert "WhatsApp" in out
        assert "setup --whatsapp" in out

    def test_whatsapp_flag_opts_into_the_whatsapp_step_only(self, tmp_path, monkeypatch):
        """--whatsapp runs its own step and NOT the Slack ones (there is no token
        to collect and no slash command on WhatsApp)."""
        calls = self._run_setup(monkeypatch, tmp_path, whatsapp=True)
        assert calls == ["whatsapp"]

    def test_both_flags_run_both_guided_setups(self, tmp_path, monkeypatch):
        calls = self._run_setup(monkeypatch, tmp_path, slack=True, whatsapp=True)
        assert calls == ["slack_tokens", "slash_command", "whatsapp"]

    def test_the_whatsapp_flag_reaches_setup_from_the_command_line(self):
        """The wizard-level tests call ``_setup_impl`` directly, so the argparse
        flag and its plumbing need their own guard: without them
        ``kirocrew setup --whatsapp`` exits 2 instead of running anything."""
        import sys

        argv = ["kirocrew", "setup", "--whatsapp"]
        with patch.object(sys, "argv", argv), patch("kiro_crew.cli._setup") as mock_setup:
            from kiro_crew.cli import main

            main()
            assert mock_setup.call_args.kwargs["whatsapp"] is True

    def test_agent_only_with_whatsapp_warns_and_skips_the_step(self, tmp_path, monkeypatch, capsys):
        """--agent-only --whatsapp: the step is skipped, and the notice names the
        flag the caller actually passed rather than only --slack."""
        calls = self._run_setup(monkeypatch, tmp_path, agent_only=True, whatsapp=True)
        assert calls == []
        out = capsys.readouterr().out
        assert "--whatsapp is ignored with --agent-only" in out
        assert "--slack is ignored" not in out


class TestSpawnCliAuth:
    """``kirocrew spawn`` attaches X-Internal-Secret on every gateway call.

    Regression coverage for the CLI helpers in ``cli_commands.py``
    used to open ``/api/spawn`` without the per-session IPC secret, which
    caused 403 ``"gateway not running"`` errors when ``dashboard.url`` was
    set to a non-loopback host (token_auth_middleware then required either
    a session cookie or the secret header on every request).
    """

    def test_internal_secret_reads_local_secret_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port: "abc123")

        from kiro_crew.cli_commands import _internal_secret

        assert _internal_secret(5476) == "abc123"

    def test_internal_secret_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port: "")

        from kiro_crew.cli_commands import _internal_secret

        assert _internal_secret(5476) == ""

    def test_spawn_list_sends_internal_secret_header(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "kiro_crew.cli_commands.read_local_secret", lambda _port: "test-secret-xyz"
        )

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
        monkeypatch.setattr(
            "kiro_crew.cli_commands.read_local_secret", lambda _port: "run-secret-abc"
        )

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
        monkeypatch.setattr("kiro_crew.cli_commands.read_local_secret", lambda _port: "")

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

    def test_save_parses_an_explicit_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A subparser that never declared the flag exits at parse time, so this
        # pins the whole argv -> namespace -> command hop rather than the
        # forwarding alone.
        seen: dict[str, object] = {}
        from kiro_crew import cli

        monkeypatch.setattr(
            "kiro_crew.cli_commands._artifact",
            lambda args: seen.update(slug=args.slug, name=args.name),
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "kirocrew",
                "artifact",
                "save",
                "--name",
                "Run Summary",
                "--slug",
                "chosen-by-hand",
                "--content",
                "body",
            ],
        )
        cli.main()
        assert seen == {"slug": "chosen-by-hand", "name": "Run Summary"}

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
        # The verb is only registered (and dispatched) when the builtin's
        # mcp_server module resolves (#5901) — make the synthetic one resolve.
        monkeypatch.setattr(cli_mod, "_builtin_mcp_server_available", lambda _name: True)
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
        gateway_call = object()
        mock_gateway = MagicMock(return_value=gateway_call)
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli_server._gateway", mock_gateway),
            patch("kiro_crew.cli.asyncio.run") as mock_run,
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_called_once()
        mock_gateway.assert_called_once()
        mock_run.assert_called_once_with(gateway_call)

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
        gateway_call = object()
        mock_gateway = MagicMock(return_value=gateway_call)
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli_server._gateway", mock_gateway),
            patch("kiro_crew.cli.asyncio.run") as mock_run,
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_not_called()
        mock_gateway.assert_called_once()
        mock_run.assert_called_once_with(gateway_call)

    def test_seed_with_replace_flag(self, monkeypatch):
        """--seed with --seed-replace should call seed_cmd."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["kirocrew", "gateway", "--seed", "demo", "--seed-replace"],
        )
        mock_seed = MagicMock(return_value=0)
        gateway_call = object()
        mock_gateway = MagicMock(return_value=gateway_call)
        with (
            patch("kiro_crew.cli.seed_cmd", mock_seed),
            patch("kiro_crew.cli_server._gateway", mock_gateway),
            patch("kiro_crew.cli.asyncio.run") as mock_run,
        ):
            from kiro_crew.cli import main

            main()
        mock_seed.assert_called_once()
        mock_gateway.assert_called_once()
        mock_run.assert_called_once_with(gateway_call)


class TestDoctorEmbeddings:
    """Tests for the doctor Vector Memory (in-process embeddings) section."""

    @pytest.fixture(autouse=True)
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)
        # LLAMA_CPP_LIB_PATH selects between two mutually exclusive diagnoses: name the
        # missing bundled libs, or blame the operator's override directory. Which branch a
        # test exercises is therefore part of its scenario, not an ambient property of the
        # host — and the variable is easy to inherit, because the real loader sets it to its
        # own bundled libs dir the first time embeddings load. Cleared per test; the tests
        # that exercise the override branch set it themselves.
        monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)

    @staticmethod
    def _run_doctor(
        tmp_path,
        monkeypatch,
        *,
        runtime_ok: bool,
        model_present: bool,
        platform_supported: bool = True,
        missing_libs: dict | None = None,
        loader_setdefaults: str = "",
        lib_path_override: str | None = None,
    ):
        """Run _doctor with the embeddings runtime/model state stubbed.

        ``loader_setdefaults`` reproduces the real loader's side effect of
        ``setdefault``-ing LLAMA_CPP_LIB_PATH to its own bundled libs dir, which
        is what makes reading that var after the load call ambiguous.

        ``lib_path_override`` controls the LLAMA_CPP_LIB_PATH the doctor sees:
        ``None`` (default) CLEARS it — the var LEAKS between tests otherwise,
        because both the ``loader_setdefaults`` path and the real embeddings
        loader plant it via ``os.environ.setdefault`` (invisible to
        monkeypatch teardown), so whichever test ran first in the pytest
        worker poisoned override-sensitive assertions (shard-layout-dependent
        CI failures). A string sets the override deliberately, via monkeypatch
        so it is restored on teardown.
        """
        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        import kiro_crew.cli_doctor as doc

        if lib_path_override is None:
            # setenv FIRST so monkeypatch records a teardown action even when
            # the var is ABSENT: delenv(raising=False) on a missing var
            # registers nothing, so the loader_setdefaults path's direct
            # os.environ.setdefault would still leak into later tests in
            # workers where the var was never set (GPT review). The
            # setenv+delenv pair restores the original state either way.
            monkeypatch.setenv("LLAMA_CPP_LIB_PATH", "")
            monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)
        else:
            monkeypatch.setenv("LLAMA_CPP_LIB_PATH", lib_path_override)

        def _load():
            if loader_setdefaults:
                # Through monkeypatch, not a bare os.environ write: the real loader's
                # setdefault is a process-wide mutation, and reproducing it literally leaked
                # the variable into every later test in the same worker, flipping them onto
                # the override branch depending on distribution order.
                if "LLAMA_CPP_LIB_PATH" not in os.environ:
                    monkeypatch.setenv("LLAMA_CPP_LIB_PATH", loader_setdefaults)
            return object if runtime_ok else None

        monkeypatch.setattr(doc, "_load_llama_class", _load)
        monkeypatch.setattr(
            doc, "_platform_libs_dirname", lambda: "macos_arm64" if platform_supported else None
        )
        monkeypatch.setattr(doc, "verify_vendored_libs", lambda: missing_libs or {})
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
        # A COMPLETE payload that still fails to load must not be blamed on
        # packaging — that would send the user reinstalling for nothing.
        assert "incomplete" not in out

    def test_doctor_names_the_missing_native_libs(self, tmp_path, capsys, monkeypatch):
        """An incomplete shipped payload names the absent files.

        ctypes reports only "base name 'llama' not found", which reads as an
        unsupported architecture — so a bare "failed to load" sends diagnosis
        after the CPU arch instead of the packaging rule that dropped the file
        on every arch.
        """
        # This test asserts the NO-override branch, so the var must be absent.
        # It is not, reliably: the doctor branches on LLAMA_CPP_LIB_PATH and the
        # value can arrive from outside this test -- a dev shell that exports it,
        # or the sibling override test, whose helper sets it via a raw
        # os.environ.setdefault that escapes pytest teardown. Either way the
        # doctor takes the "libs load from the override dir" path and this
        # assertion can never match. Clearing it here (mirroring the sibling,
        # which explicitly setenv-s) makes the expectation independent of both
        # the ambient environment and test order -- pytest-split reshuffles
        # shards whenever the suite test count changes, so the leak surfaced on
        # an unrelated PR that merely added tests elsewhere.
        monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            missing_libs={"macos_arm64": ["libllama.dylib"]},
        )
        out = capsys.readouterr().out
        assert "Missing native libs for macos_arm64: libllama.dylib" in out
        assert "packaging" in out

    def test_doctor_blames_the_override_dir_not_the_bundled_tree(
        self, tmp_path, capsys, monkeypatch
    ):
        """Under LLAMA_CPP_LIB_PATH, point at the override — not a reinstall.

        The libs load from the operator's directory, so "reinstall Kiro Crew"
        would send them to replace a package they are deliberately not loading
        from, while saying nothing about the dir that actually failed. Mirrors
        the loader's exemption so the two diagnostics cannot disagree.
        """
        monkeypatch.setenv("LLAMA_CPP_LIB_PATH", "/opt/my-gpu-llama")
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            missing_libs={"macos_arm64": ["libllama.dylib"]},
            lib_path_override="/opt/my-gpu-llama",
        )
        out = capsys.readouterr().out
        assert "/opt/my-gpu-llama" in out
        assert "Missing native libs" not in out
        assert "reinstall Kiro Crew" not in out

    def test_doctor_does_not_mistake_the_loaders_own_setdefault_for_an_override(
        self, tmp_path, capsys, monkeypatch
    ):
        """A complete payload that fails to import is not reported as overridden.

        `_load_llama_class()` `setdefault`s LLAMA_CPP_LIB_PATH to its OWN bundled
        libs dir, so reading the var AFTER that call cannot distinguish "operator
        set it" from "the loader just set it to the bundle" — which produced the
        self-contradiction "the libs load from <bundled path>, not the bundled
        tree". Doctor must sample the environment before the load.
        """
        monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)
        # Libs ARE missing, so reading the var too late suppresses the real
        # packaging diagnosis and prints the override note in its place. With no
        # missing libs both branches stay silent and the bug is invisible.
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            missing_libs={"macos_arm64": ["libllama.dylib"]},
            loader_setdefaults="/bundled/_vendor/llama_cpp_libs/x",
        )
        out = capsys.readouterr().out

        assert "not the bundled tree" not in out
        assert "Missing native libs for macos_arm64: libllama.dylib" in out

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
            verdict = cli_server._wait_gateway_ready(self._proc([1]), 7777, None, timeout=999)

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

        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")
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

        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")
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

        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")
        monkeypatch.setattr("kiro_crew.cli_server._RESTART_READY_TIMEOUT", 0)

        _print_token_url(7777)

        out = capsys.readouterr().out
        assert "kirocrew token" in out

    def test_fallback_on_no_secret(self, tmp_path, capsys, monkeypatch):
        from kiro_crew.cli_server import _print_token_url

        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "")
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

        code = textwrap.dedent("""
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
            """).format(expected=expected_watcher)
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

        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")
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

        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: "test-secret")
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
        value = "test-secret" if secret else ""
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", lambda _port: value)
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

    class _StrictTextStream:
        """A TTY-like stream that raises on anything its codec cannot encode."""

        errors = "strict"

        def __init__(self, encoding):
            self.encoding = encoding
            self._chunks = []

        def write(self, text):
            text.encode(self.encoding, errors=self.errors)
            self._chunks.append(text)
            return len(text)

        def flush(self):
            return None

        def isatty(self):
            return True

        def getvalue(self):
            return "".join(self._chunks)

    @pytest.fixture(autouse=True)
    def _fresh_stdin_state(self, monkeypatch):
        """Poisoning is process-wide state; monkeypatch restores it per test."""
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(cli_chat, "_stdin_poisoned", False)

    @staticmethod
    def _event(
        *,
        title="Terminal",
        command=None,
        tool_name="",
        mcp_server_name="",
        tool_kind=None,
        shell_classified=True,
        raw_params=None,
    ):
        """A permission request in the shape the wire delivers.

        ``tool_input`` rather than ``raw_tool_params`` carries the command,
        because that is where a permission_request puts it -- the fallback the
        gate depends on to recover what really executes.

        ``tool_kind`` defaults to ``execute`` only when a command is supplied.
        An execute-kind request with no recoverable command is the cache-miss
        anomaly ``_unverifiable_shell`` refuses outright, so it must not be the
        default shape for tests about display, teardown or stdin -- those need a
        request that actually reaches the prompt. Pass ``tool_kind`` explicitly
        to exercise the anomaly.

        ``shell_classified`` defaults True for the same reason: the normal wire
        shape is a preceding ``tool_call`` that carried a resolvable ``kind``, so
        the shell cache was populated and ``is_shell`` is a RESOLVED answer. The
        miss -- where nothing was classified and the gate must refuse rather than
        read the payload's own kind -- is opted into explicitly.
        """
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=7,
            title=title,
            tool_kind=tool_kind if tool_kind is not None else ("execute" if command else "read"),
            is_shell=command is not None,
            shell_classified=shell_classified,
            tool_input=json.dumps({"command": command}) if command else "",
            tool_name=tool_name,
            raw_tool_params=raw_params,
            mcp_server_name=mcp_server_name,
            # Advertised by a real backend; the CLI must NOT read these -- option
            # ids are backend-specific and the ACP layer owns the mapping.
            options=[{"id": "allow", "label": "Allow Once"}],
        )

    @staticmethod
    def _direct_tool_call(client, *, path="notes.md", tool_call_id="tc-direct"):
        """Populate the real direct-client provenance caches for an edit call."""
        from kiro_crew.acp.types import JsonRpcMessage

        event = client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": tool_call_id,
                        "title": "Edit notes",
                        "kind": "edit",
                        "rawInput": {"path": path, "content": "updated"},
                        "_meta": {"kiro": {"toolName": "fs_write"}},
                    }
                },
            )
        )
        assert event is not None

    @staticmethod
    def _direct_permission_message(*, tool_call_id="tc-direct", inline_path=None):
        from kiro_crew.acp.types import JsonRpcMessage

        tool_call: dict[str, object] = {
            "toolCallId": tool_call_id,
            "title": "Edit notes",
            "kind": "edit",
        }
        if inline_path is not None:
            tool_call["input"] = {"path": inline_path, "content": "inline"}
        return JsonRpcMessage(
            id="req-direct",
            method="session/request_permission",
            params={
                "toolCall": tool_call,
                "options": [
                    {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                ],
            },
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
        # The notice's own wording stays ASCII, which is a legibility choice: a
        # redirected stream encodes with the locale codec, and an escaped
        # character reads as noise mid-sentence. This case supplies an ASCII
        # title, so the whole captured notice is ASCII.
        #
        # It is NOT a safety property, and the title is not covered by it — see
        # TestDenialNoticeEncoding, which drives a real child process to pin what
        # actually protects an arbitrary-Unicode title.
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

    @pytest.mark.asyncio
    async def test_the_teardown_audit_also_runs_off_the_event_loop(self, monkeypatch):
        """The exit path is the one where blocking the loop hurts most: the loop
        still owns the ACP reader and stderr-drain tasks, and cold ``sel()``
        initialization replays the audit log to recover the HMAC chain. Auditing
        synchronously here would relocate the freeze this path exists to end.

        Shielded, so the record still lands if another cancellation arrives
        mid-write -- ``session_aborted`` is the outcome an audit reader can least
        afford to lose, because it is what distinguishes a died-unanswered turn
        from a user who said no.
        """
        import kiro_crew.cli_chat as cli_chat

        entered = threading.Event()
        threads: list = []
        trace: list = []

        def blocking_read(prompt=""):
            entered.set()
            threading.Event().wait(timeout=self._TIMEOUT)
            return "a"

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", blocking_read)

        def _record(**kw):
            threads.append(threading.current_thread())
            trace.append(("sel", kw))

        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_record)
        )

        provider = self._GatedProvider(self._event(), trace)
        task = asyncio.create_task(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate())
        )
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=self._TIMEOUT)

        assert [s[1]["error"] for s in trace if s[0] == "sel"] == ["session_aborted"]
        assert threads[0] is not threading.main_thread(), (
            "the teardown SEL write ran on the event loop thread; a slow audit "
            "store would stall ACP draining while the turn is being torn down"
        )

    @pytest.mark.asyncio
    async def test_an_approval_that_cannot_be_audited_is_refused(self, monkeypatch):
        """AUDIT-OR-DENY on the allow path. An unwritable SEL log otherwise means
        the tool RUNS while the only record that a human authorized it is dropped
        by the background writer -- and on this surface the consent was a
        keystroke, so nothing else can reconstruct it afterwards.

        Refused rather than raised: letting the failure escape would abandon the
        request unanswered, and the backend holds the turn open until it is
        answered, which is the hang this path exists to end.
        """
        import kiro_crew.cli_chat as cli_chat

        trace: list = []

        def _explode(**kw):
            trace.append(("sel", kw))
            if kw.get("critical"):
                raise OSError("SEL log is read-only")

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": "a")
        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_explode)
        )

        provider = self._GatedProvider(self._event(), trace)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)], "the tool must not run unaudited"
        outcomes = [(s[1]["outcome"], s[1].get("error", "")) for s in trace if s[0] == "sel"]
        assert ("allowed", "") in outcomes, "the critical attempt must have been made"
        assert ("denied", "audit_unwritable") in outcomes, (
            "the downgrade must be recorded under its OWN code -- the operator said "
            "yes, so an audit reader must not be told they refused"
        )

    @pytest.mark.asyncio
    async def test_the_allow_audit_is_critical_and_the_deny_audit_is_not(self, monkeypatch):
        """The asymmetry is deliberate. Fail-closed only has meaning where the
        alternative is EXECUTION: a deny is already refusing, so a lost record
        cannot authorize anything, and making it audit-or-deny would turn a
        refusal into an error for no security gain."""
        import kiro_crew.cli_chat as cli_chat

        seen: dict = {}

        async def _drive(answer, key):
            trace: list = []
            self._patch_env(monkeypatch, trace=trace, answer=answer)
            provider = self._GatedProvider(self._event(), trace)
            await asyncio.wait_for(
                cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
                timeout=self._TIMEOUT,
            )
            seen[key] = [s[1].get("critical", False) for s in trace if s[0] == "sel"]

        await _drive("a", "allow")
        await _drive("d", "deny")

        assert seen["allow"] == [True], "the approval must be audit-or-deny"
        assert seen["deny"] == [False], "a refusal must not be gated on its own audit"

    @pytest.mark.asyncio
    async def test_the_prompt_and_the_gate_share_one_set_of_path_spellings(self, monkeypatch):
        """A ``filePath`` target used to be DISPLAYED as though it had been vetted
        while the keystone never read that key. Both sides now resolve through
        ``hooks.target_paths``, so the parity is structural rather than asserted in
        a comment -- and a sensitive value under any spelling is refused by the
        gate before a human is ever asked.
        """
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up the notes",
                tool_name="fs_write",
                tool_kind="edit",
                raw_params={"filePath": "~/.ssh/id_rsa"},
            ),
            answer="a",
        )
        assert provider.calls == [("reject", 7, False)], "the keystone must refuse it"
        assert reads["n"] == 0, "a policy denial is not the user's to override"
        assert [s["error"] for s in sels] == ["hook_deny"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer,tty,interactive,expected_error",
        [
            ("d", True, True, "user_denied"),
            ("a", False, True, "noninteractive"),
        ],
    )
    async def test_a_refusal_survives_its_own_audit_failing(
        self, monkeypatch, answer, tty, interactive, expected_error
    ):
        """The audit is bookkeeping; ``reject_tool`` is what ends the turn. If a
        raising audit skipped the rejection, the backend would hold the turn open
        on an unanswered request -- the hang this path exists to end, reached
        through the bookkeeping instead of the decision.

        Parametrised across two different refusal reasons so this pins the CLASS,
        not the single call site: every refusal path shares one helper.
        """
        import kiro_crew.cli_chat as cli_chat

        trace: list = []

        def _explode(**kw):
            trace.append(("sel", kw))
            raise ValueError("invalid SEL key")

        monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": answer)
        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_explode)
        )

        provider = self._GatedProvider(self._event(), trace)
        await asyncio.wait_for(
            cli_chat._send_and_print(
                provider, "run it", interactive=interactive, gate=self._gate()
            ),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)], (
            "the audit failure swallowed the rejection; the backend would wait "
            "forever on an unanswered permission request"
        )
        assert [s[1].get("error") for s in trace if s[0] == "sel"] == [expected_error]

    @pytest.mark.asyncio
    async def test_a_policy_denial_survives_its_own_audit_failing(self, monkeypatch):
        """The gate-deny path is the one a prompt-injected agent is most likely to
        reach, so it must not be the one where a broken audit sink turns a refusal
        into an abandoned turn."""
        import kiro_crew.cli_chat as cli_chat

        trace: list = []

        def _explode(**kw):
            trace.append(("sel", kw))
            raise ValueError("invalid SEL key")

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": "a")
        monkeypatch.setattr(
            cli_chat, "sel", lambda: types.SimpleNamespace(log_tool_invocation=_explode)
        )

        event = self._event(
            title="Tidy up",
            tool_name="fs_write",
            tool_kind="edit",
            raw_params={"path": "~/.ssh/id_rsa"},
        )
        provider = self._GatedProvider(event, trace)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)]
        assert [s[1].get("error") for s in trace if s[0] == "sel"] == ["hook_deny"]

    @pytest.mark.asyncio
    async def test_a_gate_exception_is_explicitly_rejected(self, monkeypatch, capsys):
        """A broken authorization gate cannot become an unanswered request."""
        import kiro_crew.cli_chat as cli_chat

        trace: list = []
        event = self._event(title="Check it", tool_name="fs_read")
        provider = self._GatedProvider(event, trace)
        reads = self._patch_env(monkeypatch, tty=True, trace=trace, answer="a")
        gate = self._gate()

        def broken_gate(*args, **kwargs):
            raise ValueError("malformed tool input")

        monkeypatch.setattr(gate.hooks, "on_tool_call", broken_gate)
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "run it", interactive=True, gate=gate),
            timeout=self._TIMEOUT,
        )

        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert [s[1].get("error") for s in trace if s[0] == "sel"] == ["gate_failed"]
        captured = capsys.readouterr()
        assert "done" in captured.out
        assert "security gate could not verify" in captured.err

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
        # The prompt's own terminal reset is fixed, trusted bytes rather than
        # anything a title can influence, so it is subtracted before asserting
        # that NOTHING else escaped: that keeps this a whole-output guarantee
        # instead of narrowing it to one line.
        import kiro_crew.cli_chat as cli_chat

        title = "Read file\x1b]52;c;aGVsbG8=\x07 please"
        await self._drive(monkeypatch, event=self._event(title=title))
        out = capsys.readouterr().out
        untrusted = out.replace(cli_chat._PROMPT_TERMINAL_RESET, "")
        assert "\x1b" not in untrusted and "\x07" not in untrusted
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
    @pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
    async def test_malicious_unicode_title_and_tool_input_are_answered(self, monkeypatch, encoding):
        """Strict UTF-8 rejects lone surrogates; strict cp1252 rejects wider Unicode.

        Drive the whole pending-request flow through a strict destination stream
        so either case raises before ``approve_tool`` when the render boundary is
        missing. ``tool_input`` is used because that is the permission-request
        wire shape ``shell_command`` decodes.
        """
        stream = self._StrictTextStream(encoding)
        monkeypatch.setattr(sys, "stdout", stream)
        bomb = chr(0x1F4A3)
        command = f"printf '{chr(0xDFFF)} {bomb}'"
        event = self._event(title=f"Run safely {chr(0xD800)} 刪除 {bomb}", command=command)
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")

        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1
        assert [record["outcome"] for record in sels] == ["allowed"]
        rendered = stream.getvalue()
        rendered.encode(encoding, errors="strict")
        assert "\ud800" not in rendered and "\udfff" not in rendered
        assert "Permission required:" in rendered and "Command:" in rendered
        if encoding == "cp1252":
            assert "\\u522a\\u9664" in rendered
            assert "\\U0001f4a3" in rendered
        else:
            assert "刪除" in rendered and bomb in rendered

    @pytest.mark.asyncio
    async def test_a_prompt_render_failure_is_explicitly_rejected(self, monkeypatch, capsys):
        """A render bug is not allowed to escape between request and response."""
        import kiro_crew.cli_chat as cli_chat

        async def broken_prompt(event):
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")

        monkeypatch.setattr(cli_chat, "_prompt_allows", broken_prompt)
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Render \ud800", tool_name="fs_write"),
            answer="a",
        )

        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0
        assert [record["error"] for record in sels] == ["prompt_failed"]
        captured = capsys.readouterr()
        assert "done" in captured.out
        assert "could not be rendered or read" in captured.err

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
    async def test_an_unverifiable_execute_request_is_never_put_to_the_user(
        self, monkeypatch, capsys
    ):
        """``is_shell`` comes only from the trusted preceding-tool_call cache.

        On a cache miss it stays False, and ``shell_command`` returns None
        whenever it is False -- so a real command would be gated on nothing but
        its LLM-authored title. The request is refused instead of asked about,
        even though the user would have allowed it.
        """
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="List project files", tool_kind="execute"),
            answer="a",  # the user WOULD have allowed it
        )
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0  # never prompted
        assert sels[0]["outcome"] == "denied"
        # Its own code: the gate did not reject this, we refused to ask.
        assert sels[0]["error"] == "unverified_shell"
        assert "could not be verified" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_a_cosmetic_kind_variant_still_refuses(self, monkeypatch):
        # Widening a fail-closed test is safe, so casing/padding must not be a
        # way to present an unverifiable command as an ordinary tool call.
        provider, sels, _ = await self._drive(
            monkeypatch,
            event=self._event(title="List files", tool_kind="  Execute "),
            answer="a",
        )
        assert provider.calls == [("reject", 7, False)]
        assert sels[0]["error"] == "unverified_shell"

    @pytest.mark.asyncio
    async def test_a_non_string_kind_still_gets_answered(self, monkeypatch):
        # ACP relays ``toolCall.kind`` verbatim, so a backend can send ``kind: 1``.
        # The gate reads the kind as text, so an unguarded value raises inside the
        # hook and the turn ends with the request UNANSWERED -- the hang this
        # whole path exists to end. The request must still be decided, and an
        # unusable kind must not qualify for the read-only allow-list.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="List files", tool_kind=1),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_a_builtin_call_shows_its_trusted_tool_name(self, monkeypatch, capsys):
        # A builtin has no MCP server, so the prompt otherwise carries only the
        # model-authored title and a file write reaches the human as whatever
        # prose the model chose. ``tool_name`` is the trusted ``_meta.kiro``
        # identity, so consent covers what runs rather than its description.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert "fs_write" in capsys.readouterr().out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["path", "file_path", "filePath"])
    async def test_a_file_write_discloses_its_target_path(self, monkeypatch, capsys, key):
        # The trusted identity says WHICH tool runs, not what it runs AGAINST.
        # ``fs_write`` under a benign title is consent to a verb, and the gate's
        # sensitive-path deny covers only the named-dangerous paths -- so what is
        # left undisclosed is exactly the ordinary valuable file no rule speaks
        # for. Every spelling the gate accepts must be read here too, or the
        # prompt and the gate disagree about which value is the target.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up the notes",
                tool_name="fs_write",
                raw_params={key: "/home/tester/thesis.md"},
            ),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert "/home/tester/thesis.md" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_a_multi_target_call_discloses_the_count(self, monkeypatch, capsys):
        # Now that extraction is nesting-aware, a batch call yields every target,
        # and showing only the first would have the human consent to one file
        # while approving the whole batch. The first path plus a count keeps the
        # prompt honest without turning it into a manifest.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Read the notes",
                tool_name="read",
                raw_params={
                    "operations": [
                        {"mode": "Line", "path": "/home/tester/thesis.md"},
                        {"mode": "Line", "path": "/home/tester/notes.md"},
                        {"mode": "Line", "path": "/home/tester/refs.md"},
                    ]
                },
            ),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        out = capsys.readouterr().out
        assert "/home/tester/thesis.md" in out
        assert "(+2 more)" in out

    @pytest.mark.asyncio
    async def test_a_pathless_call_is_still_asked_about(self, monkeypatch):
        # Absence of a path is NOT a refusal. Most builtin calls legitimately act
        # on no file, so denying whenever a path cannot be found would refuse
        # them all to close a gap that only exists for tools which name a file.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Remember this", tool_name="memory_write"),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1, "a call with no path must still reach the human"
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_a_long_path_cannot_push_the_question_off_screen(self, monkeypatch, capsys):
        # The path is attacker-influenceable text on an authorization prompt, so
        # it is capped for the same reason the command line is.
        import kiro_crew.cli_chat as cli_chat

        flood = "/tmp/" + "a" * (cli_chat._MAX_COMMAND_DISPLAY * 3)
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up", tool_name="fs_write", raw_params={"path": flood}),
            answer="a",
        )
        out = capsys.readouterr().out
        assert "[truncated]" in out
        assert flood not in out

    @pytest.mark.asyncio
    async def test_a_non_string_path_argument_does_not_break_the_prompt(self, monkeypatch):
        # ``raw_tool_params`` is relayed from the backend, so a value need not be
        # a string. Raising here would leave the permission request unanswered --
        # the hang this whole path exists to end.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up",
                tool_name="fs_write",
                raw_params={"path": {"nested": 1}},
            ),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_the_audit_write_never_runs_on_the_event_loop(self, monkeypatch):
        """``sel()`` opens the audit log and replays it to recover the HMAC chain,
        so the first permission of a fresh chat pays a filesystem cost inside the
        call -- unbounded on slow or corrupt storage. This coroutine shares its
        loop with the ACP reader and stderr-drain tasks, so a blocking write here
        stops draining the backend and freezes the turn the audit is about."""
        import threading

        import kiro_crew.cli_chat as cli_chat

        threads: list = []
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_chat, "_read_line_blocking", lambda prompt="": "a")
        monkeypatch.setattr(
            cli_chat,
            "sel",
            lambda: types.SimpleNamespace(
                log_tool_invocation=lambda **kw: threads.append(threading.current_thread())
            ),
        )

        class _P:
            def __init__(self):
                self.calls = []

            async def approve_tool(self, request_id, *, always: bool = False):
                self.calls.append(("approve", request_id))

            async def reject_tool(self, request_id):
                self.calls.append(("reject", request_id))

        provider = _P()
        await asyncio.wait_for(
            cli_chat._answer_permission(
                provider,
                self._event(title="Tidy up", tool_name="fs_write"),
                interactive=True,
                gate=self._gate(),
            ),
            timeout=self._TIMEOUT,
        )
        assert provider.calls == [("approve", 7)]
        assert threads, "nothing was audited"
        assert threads[0] is not threading.main_thread(), (
            "the SEL write ran on the event loop thread; a slow audit store would "
            "stall ACP draining and freeze the turn"
        )

    @pytest.mark.asyncio
    async def test_the_prompt_resets_terminal_modes_first(self, monkeypatch, capsys):
        # Streamed model output is printed raw, so a turn can leave the terminal
        # in conceal mode and the prompt would draw invisibly into it --
        # sanitising the prompt's own strings cannot undo inherited state. The
        # reset has to precede the question, or it does not protect it.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        out = capsys.readouterr().out
        assert "\x1b[0m" in out and "\x1b[?25h" in out
        assert out.index("\x1b[0m") < out.index("Permission required")

    @pytest.mark.asyncio
    async def test_the_mcp_audit_record_names_the_server(self, monkeypatch):
        # An MCP tool name is unique only within its server, so the audit record
        # must carry both halves or two servers exposing the same name produce
        # indistinguishable records.
        _, sels, _ = await self._drive(
            monkeypatch,
            event=self._event(
                title="Tidy up", tool_name="write_file", mcp_server_name="filesystem"
            ),
            answer="d",
        )
        assert sels[0]["tool_name"] == "@filesystem/write_file"

    @pytest.mark.asyncio
    async def test_the_mcp_audit_identity_cannot_collide_on_underscores(self, monkeypatch):
        # `mcp__<server>__<tool>` re-splits on the LAST `__`, so server "a__b"
        # with tool "c" and server "a" with tool "b__c" would compose to the same
        # string. The `/`-joined reference keeps them distinct, because neither an
        # MCP server nor an MCP tool name contains a slash.
        _, first, _ = await self._drive(
            monkeypatch,
            event=self._event(title="One", tool_name="c", mcp_server_name="a__b"),
            answer="d",
        )
        _, second, _ = await self._drive(
            monkeypatch,
            event=self._event(title="Two", tool_name="b__c", mcp_server_name="a"),
            answer="d",
        )
        assert first[0]["tool_name"] == "@a__b/c"
        assert second[0]["tool_name"] == "@a/b__c"
        assert first[0]["tool_name"] != second[0]["tool_name"]

    @pytest.mark.asyncio
    async def test_a_non_mcp_audit_record_keeps_the_plain_tool_name(self, monkeypatch):
        # The negative control: without an MCP server there is nothing to
        # qualify, so the record must not gain an `@` prefix.
        _, sels, _ = await self._drive(
            monkeypatch,
            event=self._event(title="Read it", tool_name="fs_read"),
            answer="d",
        )
        assert sels[0]["tool_name"] == "fs_read"

    @pytest.mark.asyncio
    async def test_the_reset_closes_an_open_string_before_resetting_modes(
        self, monkeypatch, capsys
    ):
        # An OSC/DCS sequence the model left UNTERMINATED puts the terminal in
        # string-consuming mode: everything after it is swallowed as that
        # sequence's payload. A mode reset sent into that state is itself eaten,
        # and so is the prompt. So the abort must come FIRST -- once it discards
        # the string, the rest of the reset is interpreted again.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        out = capsys.readouterr().out
        can = out.index("\x18")
        assert can < out.index("\x1b[0m"), "the abort must precede the mode reset"
        assert can < out.index("Permission required")

    @pytest.mark.asyncio
    async def test_the_reset_aborts_rather_than_terminates_a_pending_string(
        self, monkeypatch, capsys
    ):
        # A String Terminator would COMPLETE the pending string, handing an
        # unterminated OSC 52 from model output to the terminal as a finished
        # command -- which sets the user's clipboard from attacker-controlled
        # text. The reset must abort the sequence, so it must not contain ST.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        out = capsys.readouterr().out
        assert "\x18" in out
        assert "\x1b\\" not in out, "ST completes the pending string instead of discarding it"

    @pytest.mark.asyncio
    async def test_the_reset_does_not_beep(self, monkeypatch, capsys):
        # BEL also ends an OSC string, but it beeps audibly when no string is
        # open -- on every single prompt. CAN is silent and, unlike BEL, discards
        # the pending payload rather than delivering it, so the reset must not
        # fall back to BEL.
        await self._drive(
            monkeypatch,
            event=self._event(title="Tidy up the notes", tool_name="fs_write"),
            answer="a",
        )
        assert "\x07" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_the_reset_is_withheld_when_stdout_is_not_a_terminal(self, monkeypatch):
        # The negative control, exercised directly: driving a full turn with
        # tty=False never renders the prompt at all, so asserting on its output
        # would pass no matter what this branch does. Escape bytes written to a
        # pipe or a redirected file are literal noise in someone's log.
        import kiro_crew.cli_chat as cli_chat

        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        assert cli_chat._terminal_reset() == ""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert cli_chat._terminal_reset() == cli_chat._PROMPT_TERMINAL_RESET

        def boom():
            raise ValueError("detached")

        # A closed or detached stream must not let the check itself break the
        # prompt the user is waiting on.
        monkeypatch.setattr(sys.stdout, "isatty", boom, raising=False)
        assert cli_chat._terminal_reset() == ""

    @pytest.mark.asyncio
    async def test_a_verified_shell_call_is_still_asked_about(self, monkeypatch):
        # The negative control: the refusal must key on the MISSING trusted
        # signal, not on execute-kind itself, or every shell call would die
        # here and the test above would pass for the wrong reason.
        provider, sels, reads = await self._drive(
            monkeypatch,
            event=self._event(title="Run the tests", command="pytest -q"),
            answer="a",
        )
        assert provider.calls == [("approve", 7, False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_direct_client_non_shell_permission_can_be_allowed(self, monkeypatch, tmp_path):
        """The Claude/direct transport must distinguish cached False from a miss.

        This drives the production ``AcpClient`` parser before the CLI consumer;
        hand-building an event with ``shell_classified=True`` would not catch a
        transport that dropped the provenance flag while copying the cache.
        """
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        self._direct_tool_call(client)
        event = client._build_permission_event(self._direct_permission_message())

        assert event.is_shell is False
        assert event.shell_classified is True
        assert event.raw_params_trusted is True
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("approve", "req-direct", False)]
        assert reads["n"] == 1
        assert [s["outcome"] for s in sels] == ["allowed"]

    @pytest.mark.asyncio
    async def test_direct_client_repeat_keeps_cached_params_authoritative(
        self, monkeypatch, tmp_path
    ):
        """A repeated request cannot replace trusted args with inline prose."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        self._direct_tool_call(client, path="~/.ssh/id_rsa")
        first = client._build_permission_event(self._direct_permission_message())
        repeat = client._build_permission_event(
            self._direct_permission_message(inline_path="notes.md")
        )

        assert first.raw_tool_params == repeat.raw_tool_params
        assert repeat.raw_tool_params is not None
        assert repeat.raw_tool_params["path"] == "~/.ssh/id_rsa"
        assert repeat.raw_params_trusted is True
        assert repeat.shell_classified is True
        provider, _, reads = await self._drive(monkeypatch, event=repeat, answer="a")
        assert provider.calls == [("reject", "req-direct", False)]
        assert reads["n"] == 0

    @pytest.mark.asyncio
    async def test_direct_client_cache_miss_stays_fail_closed(self, monkeypatch, tmp_path):
        """Inline permission data cannot manufacture trusted provenance."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        event = client._build_permission_event(
            self._direct_permission_message(tool_call_id="uncached", inline_path="notes.md")
        )

        assert event.raw_params_trusted is False
        assert event.shell_classified is False
        provider, sels, reads = await self._drive(monkeypatch, event=event, answer="a")
        assert provider.calls == [("reject", "req-direct", False)]
        assert reads["n"] == 0
        assert sels[0]["error"] == "unverified_shell"

    @pytest.mark.asyncio
    async def test_ordinary_title_and_command_display_unchanged(self, monkeypatch, capsys):
        # The control-stripping must not disturb ordinary text: this is the
        # control for the three cases above.
        event = self._event(title="Run the test suite", command="pytest -q test/test_cli.py")
        await self._drive(monkeypatch, event=event)
        out = capsys.readouterr().out
        assert "Permission required: Run the test suite" in out
        assert "Command: pytest -q test/test_cli.py" in out
        # A shell call has no MCP identity to disclose, so the line is absent
        # rather than empty -- the negative control for the MCP cases below.
        assert "MCP tool:" not in out

    @pytest.mark.asyncio
    async def test_mcp_identity_is_disclosed_not_just_the_title(self, monkeypatch, capsys):
        # An MCP call shows no command, so before this the human saw ONLY the
        # model-authored title: a benign description could win consent for an
        # undisclosed tool. The trusted _meta identity must reach the prompt.
        event = self._event(
            title="Tidy up the workspace",
            tool_name="delete_everything",
            mcp_server_name="filesystem",
        )
        await self._drive(monkeypatch, event=event)
        out = capsys.readouterr().out
        assert "MCP tool: filesystem / delete_everything" in out

    @pytest.mark.asyncio
    async def test_mcp_identity_is_neutralised_and_survives_a_nameless_tool(
        self, monkeypatch, capsys
    ):
        # The identity is server-supplied text on the consent surface, so it
        # gets the same one-line control-stripping as the title and command. A
        # missing tool name still discloses the server rather than printing a
        # bare separator that reads as though nothing was withheld.
        event = self._event(
            title="Tidy up",
            tool_name="",
            mcp_server_name="fs\x1b[2Kspoof\nserver",
        )
        await self._drive(monkeypatch, event=event)
        line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("MCP tool:"))
        assert "\x1b" not in line
        assert line == "MCP tool: fs [2Kspoof server / unnamed tool"

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
            # One question carries every identity for the call (title, trusted
            # tool name, canonical MCP reference), so match against all of them.
            targets = [name, *k.get("extra_titles", ()), k.get("mcp_ref", "")]
            for target in targets:
                if target and target not in seen:
                    seen.append(target)
            if "@weather:srv/wipe_disk" in targets:
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

        assert "@weather:srv/wipe_disk" in seen  # forwarded, not just the title
        assert provider.calls == [("reject", 7, False)]
        assert reads["n"] == 0  # the human was never asked
        assert [s[1]["error"] for s in trace if s[0] == "sel"] == ["hook_deny"]

    @pytest.mark.asyncio
    async def test_gate_verdict_is_obtained_off_the_event_loop(self, monkeypatch):
        # The gate resolves the governance profile, which stats and reads
        # ``profiles/``. This coroutine shares its loop with the ACP reader and
        # drain tasks, so that walk must not run on the loop: on slow or network
        # storage a synchronous call stalls the whole session. Pinned by asserting
        # the call lands on a DIFFERENT thread than the loop's.
        import threading

        import kiro_crew.cli_chat as cli_chat
        import kiro_crew.hooks as hooks_mod
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        loop_thread = threading.get_ident()
        call_threads: list[int] = []
        real = hooks_mod.HookManager.on_tool_call

        def recording(self, *a, **k):
            call_threads.append(threading.get_ident())
            return real(self, *a, **k)

        monkeypatch.setattr(hooks_mod.HookManager, "on_tool_call", recording)
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=11,
            title="Read a file",
            tool_kind="read",
            options=[{"id": "allow", "label": "Allow Once"}],
        )
        trace: list = []
        provider = self._GatedProvider(event, trace)
        self._patch_env(monkeypatch, tty=True, trace=trace, answer="a")
        await asyncio.wait_for(
            cli_chat._send_and_print(provider, "go", interactive=True, gate=self._gate()),
            timeout=self._TIMEOUT,
        )

        assert call_threads, "the gate was never consulted"
        assert loop_thread not in call_threads, (
            "on_tool_call ran on the event-loop thread; its profile walk must be "
            "offloaded via asyncio.to_thread"
        )

    def test_an_unclassified_request_is_refused_whatever_kind_it_claims(self):
        # THE VECTOR. A real shell call whose preceding tool_call carried no
        # resolvable kind leaves the cache empty, so is_shell is the MISS default
        # rather than a resolved "not a shell tool". Labelling it ``read`` must not
        # buy it a trip to the human prompt, where the only thing on display would
        # be a title with no command behind it.
        import kiro_crew.cli_chat as cli_chat

        event = self._event(title="Read a file", tool_kind="read", shell_classified=False)
        assert cli_chat._unverifiable_shell(event) is True

    def test_a_resolved_non_shell_request_is_not_refused(self):
        # The negative control that keeps the deny narrow: a RESOLVED non-shell
        # classification is a real answer, and must still reach the prompt.
        import kiro_crew.cli_chat as cli_chat

        event = self._event(title="Read a file", tool_kind="read")
        assert cli_chat._unverifiable_shell(event) is False

    def test_audit_scrubs_a_credential_out_of_the_tool_identity(self, monkeypatch):
        # The audited identity comes from the backend, and SEL does not redact for
        # its callers, so a credential riding in a tool name would be persisted and
        # served over /api/sel/events.
        import types as _types

        import kiro_crew.cli_chat as cli_chat

        secret = "AKIAIOSFODNN7EXAMPLE"
        trace: list = []
        gate = self._gate()
        monkeypatch.setattr(
            cli_chat,
            "sel",
            lambda: _types.SimpleNamespace(
                log_tool_invocation=lambda **kw: trace.append(("sel", kw))
            ),
        )
        event = self._event(title="Terminal", tool_name=f"fetch_{secret}")
        cli_chat._audit(gate, event, "approved")

        logged = [s[1]["tool_name"] for s in trace if s[0] == "sel"]
        assert logged, "nothing was audited"
        assert secret not in logged[0], f"credential survived into the audit: {logged[0]!r}"


class TestDenialNoticeEncoding:
    """The automatic-denial notice interpolates a title the model or the tool
    chose, and is written to stderr precisely when stderr is REDIRECTED — where
    the stream encodes with the locale codec rather than UTF-8.

    Run in a CHILD PROCESS on purpose. The contract under test is a property of
    the real interpreter's standard streams, and in-process substitutes do not
    have it: a ``TextIOWrapper`` or ``StringIO`` built by a test carries whatever
    error handler the test chose, so a strict one would manufacture a failure
    this code path cannot actually produce, and pytest's own capture replaces
    ``sys.stderr`` outright.
    """

    #: Han the cp950 codec can encode, plus an emoji no legacy codepage can.
    _TITLE = "刪除全部 \U0001f4a3 rm -rf"

    _CHILD = """
import asyncio, json, sys
import kiro_crew.cli_chat as cli_chat
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

print(f"encoding={sys.stderr.encoding}")
print(f"errors={sys.stderr.errors}")


class P:
    async def approve_tool(self, rid):
        print("approve")

    async def reject_tool(self, rid):
        print("reject")


event = LLMEvent(
    kind=EVENT_PERMISSION_REQUEST,
    request_id=7,
    title=%(title)r,
    tool_kind="read",
    is_shell=False,
    tool_input=json.dumps({}),
    tool_name="",
    options=[{"id": "allow", "label": "Allow Once"}],
)
# interactive=False takes the automatic-denial branch, whose notice goes to
# stderr with the title interpolated into it.
asyncio.run(cli_chat._answer_permission(P(), event, interactive=False))
print("survived")
"""

    @pytest.mark.parametrize("io_encoding", ["cp950", "cp950:strict"])
    def test_denial_notice_survives_a_non_utf8_redirected_stderr(self, tmp_path, io_encoding):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = io_encoding
        # Keep the SEL audit write inside the test's own tree.
        env["KIROCREW_HOME"] = str(tmp_path / "home")
        out_path = tmp_path / "child.out"
        err_path = tmp_path / "child.err"
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            rc = subprocess.call(
                [sys.executable, "-c", self._CHILD % {"title": self._TITLE}],
                stdout=out,
                stderr=err,  # a real OS handle, so the stream is redirected
                env=env,
                # Anchor the child OUTSIDE the checkout. It imports the installed
                # package, so CWD is not on its import path, and any relative
                # artifact it or a library writes then lands in the test's own tree
                # instead of surviving in the repo.
                cwd=tmp_path,
            )
        stdout = out_path.read_text(encoding="utf-8", errors="replace")

        assert rc == 0, f"the denial notice killed the CLI:\n{stdout}"
        assert "survived" in stdout
        assert "reject" in stdout
        # The premise a reviewer keeps reaching for is that a locale codec makes
        # this strict. It does not: CPython pins stderr to backslashreplace, and
        # the ``:strict`` half of PYTHONIOENCODING is ignored for this stream.
        assert "encoding=cp950" in stdout
        assert "errors=backslashreplace" in stdout
        # And the unencodable character is escaped rather than raised.
        written = err_path.read_bytes().decode("cp950", errors="replace")
        assert "\\U0001f4a3" in written


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
