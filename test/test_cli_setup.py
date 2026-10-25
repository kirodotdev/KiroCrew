"""The ``kirocrew setup`` wizard, ``kirocrew manifest`` and the files under ``config_dir()``.

The workspace-directory and timezone prompts (with system timezone detection), the alias lookup,
the Slack app manifest, the prerequisite check, the ``project_dir`` pointer read from, and the
Slack-token ``.env`` written to, ``config_dir()``, the listener port ``logout`` reads its secret
for, and which messaging channels ``setup`` configures.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)


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
        # Path() normalizes separators, so the POSIX literal only matches on
        # POSIX; compare against the normalized form on every platform.
        assert str(Path("/custom/workspace")) in prompt

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
        self, content="name: KiroCrew-{{ALIAS}}\ndisplay_name: KiroCrew-{{ALIAS}}\n"  # brand-ok
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
        assert "KiroCrew-alice" in out  # brand-ok
        assert "{{ALIAS}}" not in out

    def test_writes_to_output_file(self, tmp_path):
        out_file = tmp_path / "sub" / "out.yaml"
        with self._patch_template("name: KiroCrew-{{ALIAS}}\n"):  # brand-ok
            from kiro_crew.cli_setup import _manifest

            _manifest(alias="bob", output=str(out_file))
        assert out_file.exists()
        assert "KiroCrew-bob" in out_file.read_text(encoding="utf-8")  # brand-ok

    def test_creates_parent_dirs(self, tmp_path):
        out_file = tmp_path / "deep" / "nested" / "out.yaml"
        with self._patch_template("name: KiroCrew-{{ALIAS}}\n"):  # brand-ok
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
        with self._patch_template("# comment\nname: KiroCrew-{{ALIAS}}\n"):  # brand-ok
            from kiro_crew.cli_setup import _manifest

            _manifest(alias="alice", url=True)
        out = capsys.readouterr().out
        assert "https://api.slack.com/apps?new_app=1&manifest_yaml=" in out
        assert "KiroCrew-alice" in out  # alias substituted  # brand-ok
        assert "%0A" in out  # newlines are URL-encoded
        assert "\nname:" not in out  # raw YAML not printed
        assert "%23" not in out  # comments stripped from URL


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
        # "CWD has no project markers" has to be pinned, not assumed of tmp_path:
        # the walk climbs every ancestor, and a temp root under a checkout (a
        # developer's `TMPDIR=./tmp`) puts the real `skills/` + `src/kiro_crew`
        # above it. The seam the walk reads is `Path.cwd()`, so it answers a
        # fabricated, marker-free location; the path is never created because
        # `resolve()` is lexical for a missing path and nothing here opens it.
        monkeypatch.setattr(
            Path,
            "cwd",
            classmethod(lambda cls: cls("/kc-detect-project-dir-has-no-markers/cwd")),
        )

        from kiro_crew.cli import _detect_project_dir

        assert _detect_project_dir() == str(proj)

    def test_detect_project_dir_no_agents_dir(self, tmp_path, monkeypatch):
        """Detection works without a project-level agents/ dir (removed in bbbc1f6e).

        Regression guard: agent config was consolidated into src/kiro_crew/config/
        and the root agents/ dir deleted, which silently broke detection (and the
        dashboard changelog) while the marker still required agents/ + skills/.
        """
        proj = tmp_path / "KiroCrew"  # brand-ok: checkout directory name
        (proj / "skills").mkdir(parents=True)
        (proj / "src" / "kiro_crew").mkdir(parents=True)
        assert not (proj / "agents").exists()

        monkeypatch.setattr("kiro_crew.cli.config_dir", lambda: tmp_path / "cfg")
        (tmp_path / "cfg").mkdir()
        monkeypatch.chdir(proj)

        from kiro_crew.cli import _detect_project_dir

        assert _detect_project_dir() == str(proj.resolve())

    def test_logout_reads_secret_for_listener_port(self, monkeypatch, capsys):
        """_logout resolves the secret paired with the requested listener."""
        read_secret = MagicMock(return_value="test-secret")
        monkeypatch.setattr("kiro_crew.cli_server.platform_compat.IS_POSIX", True)
        monkeypatch.setattr("kiro_crew.cli_server.read_local_secret", read_secret)

        from kiro_crew.cli_server import _logout

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr(
            "kiro_crew.cli_server._owner_verified_secret_urlopen",
            lambda req, port, timeout, *, operation="": __import__(
                "kiro_crew.cli_server", fromlist=["loopback_urlopen"]
            ).loopback_urlopen(req, timeout=timeout),
        )
        with patch("kiro_crew.cli_server.loopback_urlopen", return_value=mock_resp) as opener:
            _logout(5476)
        read_secret.assert_called_once_with(5476, dial_host="127.0.0.1")
        # The secret read for that listener is the one the request carries.
        assert opener.call_args.args[0].get_header("X-local-secret") == "test-secret"
        assert "All dashboard sessions revoked" in capsys.readouterr().out

    def test_setup_slack_tokens_writes_to_config_dir(self, tmp_path, monkeypatch):
        """_setup_slack_tokens writes .env to config_dir(), not ~/.kirocrew."""
        monkeypatch.setattr("kiro_crew.cli_setup.env_path", lambda: tmp_path / ".env")

        from kiro_crew.cli_setup import _setup_slack_tokens

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
        reaches it, and that temp sits in the masked staging directory rather
        than beside .env."""
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
        # Locked exactly once, on a file in the masked staging directory, while
        # it was still empty of tokens.
        assert seen == [(tmp_path / "auth-store-staging", False)]
        assert "xoxb-test" in env_file.read_text(encoding="utf-8")
        assert not list(tmp_path.glob("*.tmp"))  # no temp residue
        assert not list((tmp_path / "auth-store-staging").iterdir())
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
