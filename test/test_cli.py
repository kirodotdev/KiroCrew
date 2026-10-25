"""Tests for CLI module.

This file keeps the doctor's full report, the update failure paths, the banner spelling and the
``config set`` value grammar. Its parts, which share ``cli_helpers.py``, are ``test_cli_cron.py``,
``test_cli_setup.py``, ``test_cli_main_entry.py``, ``test_cli_gateway_client.py``,
``test_cli_stop.py``, ``test_cli_restart.py``, ``test_cli_restart_readiness.py``,
``test_cli_token.py``, ``test_cli_mcp_doctor.py``, ``test_cli_stt_memory_doctor.py``,
``test_cli_chat_permission.py`` and ``test_cli_chat_consent_surface.py``.
"""

import contextlib
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
    _healthy_agent_file,
    _noop_probe_server,
    _pin_default_config,
)

from kiro_crew.cli_doctor import _doctor
from kiro_crew.cli_server import (
    _update,
)


class TestDoctor:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture kept in place; no test of this class calls monkeypatch.undo()
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture kept in place; no test of this class calls monkeypatch.undo()
    def _hermetic_embeddings_runtime(self, monkeypatch):
        """Pin the vendored-runtime probe host-independently: the brazil
        interpreter on a Mac can be darwin/x86_64 under Rosetta, where the
        vendored libs legitimately do not exist (designed degradation)."""
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(_doc, "model_file_present", lambda path=None: False)

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture kept in place; no test of this class calls monkeypatch.undo()
    def _hermetic_sandbox(self, monkeypatch):
        """Pin the Sandbox section host-independently: a CI runner can restrict
        user namespaces with no profile installed, which is a genuine issue on
        that HOST (doctor exits 1 for it) but not the subject of these tests."""
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc.sandbox, "detect_backend", lambda config_mode="auto": "namespace")

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture kept in place; no test of this class calls monkeypatch.undo()
    def _restore_path(self, monkeypatch):
        """Put ``PATH`` back after each doctor run.

        The doctor's media section calls ``transcribe.ensure_ffmpeg_in_path()``,
        which PREPENDS a candidate ffmpeg directory to the process ``PATH`` when
        the host has one there -- a permanent mutation of the worker's environment
        that every later test on that worker then inherits (observed as a PATH
        change leaking out of the first doctor test in a full run). Recording the
        value through monkeypatch restores it at teardown whatever the doctor did.
        """
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    def test_doctor_with_kiro(self, tmp_path, capsys, monkeypatch):
        import kiro_crew.cli_doctor as _doc

        agent_file = tmp_path / "kirocrew.json"
        # A minimally healthy agent config so doctor walks the whole MCP
        # section cleanly and doesn't exit on "missing from mcpServers".
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        # Left unpatched this mutates the process PATH for every later test.
        monkeypatch.setattr(_doc, "ensure_ffmpeg_in_path", lambda: None)
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
        out = capsys.readouterr().out
        assert "kiro-cli 1.0.0" in out
        assert out.endswith("✅ Kiro Crew is ready!\n")

    @staticmethod
    def _stt_config(monkeypatch, **fields):
        """Re-enable STT on the pinned default config, with *fields* applied.

        The autouse ``_hermetic_config`` fixture pins STT OFF so unrelated doctor
        tests never reach this section, so any test that wants it has to ask.
        """
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
        from kiro_crew.config.loader import KiroCrewConfig

        def _cfg() -> KiroCrewConfig:
            cfg = KiroCrewConfig()
            # kiro-cli, like ``_pin_default_config``: the KAS engine row is not
            # the subject here.
            cfg.agent.acp_backend = ACP_BACKEND_KIRO
            cfg.stt.enabled = True
            for name, value in fields.items():
                setattr(cfg.stt, name, value)
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _cfg()))

    def test_doctor_windows_missing_stt_engine_and_ffmpeg_is_non_fatal(
        self, tmp_path, capsys, monkeypatch
    ):
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
        out = capsys.readouterr().out
        assert "  engine:      ⚠️  needs the voice extra" in out
        assert "  ffmpeg:      ⚠️  not found" in out
        assert out.endswith("✅ Kiro Crew is ready!\n")

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
        fails here."""
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

    def test_doctor_reports_platform_boot_error_without_crashing(
        self, tmp_path, capsys, monkeypatch
    ):
        """A PlatformCompositionError from boot must be REPORTED by the doctor,
        not crash it — the doctor is the tool that diagnoses a broken setup, so
        it has to survive the very failure it explains."""
        import kiro_crew.cli_doctor as _doc
        from kiro_crew.platform import PlatformCompositionError

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        boot_err = PlatformCompositionError(
            "profile=amazon resolved no companion; set KIROCREW_PROFILE=standalone"
        )
        # Left unpatched this mutates the process PATH for every later test.
        monkeypatch.setattr(_doc, "ensure_ffmpeg_in_path", lambda: None)
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

    def test_doctor_without_kiro(self, tmp_path, monkeypatch):
        """A host with no kiro-cli: ``which`` finds nothing, and neither do the
        trusted-directory resolvers the credentials, memory-pressure and
        source-checkout sections spawn through. Left unpinned, those sections ran
        the REAL ``aws configure list-profiles``, ``systemctl is-active`` and
        ``git -C <this checkout>`` on the developer's host; ``None`` from each
        resolver is the product's own "cannot ask" arm, so no binary is reached.
        The one spawn that resolves through no seam -- the runtime section's
        ``<venv>/bin/python3 --version`` -- is mocked like every sibling here."""
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc.platform_compat, "trusted_aws_bin", lambda: None)
        monkeypatch.setattr(_doc.platform_compat, "trusted_system_bin", lambda _name: None)
        monkeypatch.setattr(_doc.platform_compat, "trusted_git_bin", lambda: None)
        mock_run = MagicMock(returncode=0, stdout="Python 3.10.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.shutil.which", return_value=None),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
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

        # cli.py, every module split out of it, and cli_chat.
        split_out = [sys.modules[name] for name in cli_mod._OWNERS]
        split_out += [
            cli_mod.cli_parser_gateway,
            cli_mod.cli_parser_agent,
            cli_mod.cli_parser_admin,
        ]
        for mod in (cli_mod, *split_out, chat_mod):
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


class TestConfigSetValueBase64Grammar:
    """``config set`` takes the value as ONE ``--value-base64=<token>`` argument.

    Windows PowerShell 5.1 drops a separate empty argument before a ``.cmd`` shim, so the
    attached ``=`` form is the only spelling that carries empty text through every shell.
    """

    @staticmethod
    def _parse(*argv: str):
        import argparse

        from kiro_crew import cli_parser_gateway

        parser = argparse.ArgumentParser(prog="kirocrew")
        cli_parser_gateway.register_config_parser(parser.add_subparsers(dest="command"))
        return parser.parse_args(["config", "set", *argv])

    def test_the_flag_defaults_to_none(self):
        args = self._parse("agent.streaming", "true")

        assert args.value_base64 is None
        assert args.value == "true"

    def test_the_equals_empty_form_parses_as_the_empty_string(self):
        args = self._parse("some.key", "--value-base64=")

        assert args.value_base64 == ""
        assert args.value is None

    def test_a_padded_token_is_kept_byte_for_byte(self):
        token = "WyJDOi9teSBza2lsbHMvYSZiJ3Mvw6kiXQ=="

        assert self._parse("skills.extra_paths", f"--value-base64={token}").value_base64 == token

    def test_the_flag_may_precede_the_key(self):
        args = self._parse("--value-base64=W10=", "skills.extra_paths")

        assert (args.key, args.value_base64) == ("skills.extra_paths", "W10=")

    def test_the_existing_positional_file_and_local_grammar_is_unchanged(self):
        assert self._parse("--local", "dashboard.url", "http://x").local is True
        assert self._parse("--file", "c.json").file == "c.json"
        positional = self._parse("agent.streaming", "false")
        assert (positional.key, positional.value, positional.local) == (
            "agent.streaming",
            "false",
            False,
        )
