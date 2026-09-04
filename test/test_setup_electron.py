"""Tests for _setup_electron desktop app installation."""

from unittest.mock import MagicMock, patch

from kiro_crew.cli_setup import _setup, _setup_electron


class TestSetupElectronPlatformGuard:
    """_setup_electron exits early on non-macOS platforms."""

    def test_skips_on_linux(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Linux")
        _setup_electron()
        out = capsys.readouterr().out
        assert "only available on macOS" in out

    def test_skips_on_windows(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Windows")
        _setup_electron()
        out = capsys.readouterr().out
        assert "only available on macOS" in out


class TestSetupElectronNodeGuard:
    """_setup_electron exits early when Node.js is missing."""

    def test_no_node(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: None)
        _setup_electron()
        out = capsys.readouterr().out
        assert "Node.js not found" in out


class TestSetupElectronSourcesMissing:
    """_setup_electron returns gracefully (no clone) when sources are absent."""

    def test_sources_not_found_returns(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: None)
        run_called = {"n": 0}
        monkeypatch.setattr(
            "kiro_crew.cli_setup.subprocess.run",
            lambda *a, **kw: run_called.__setitem__("n", run_called["n"] + 1),
        )
        _setup_electron()
        out = capsys.readouterr().out
        assert "Desktop app sources not found" in out
        # Must NOT shell out to git/npm when sources are missing.
        assert run_called["n"] == 0


class TestSetupElectronBuild:
    """_setup_electron builds and installs the Electron app."""

    def _electron_dir(self, tmp_path, monkeypatch):
        electron_dir = tmp_path / "website" / "electron"
        electron_dir.mkdir(parents=True)
        monkeypatch.setattr("kiro_crew.cli_setup._find_electron_dir", lambda: electron_dir)
        return electron_dir

    def test_npm_install_failure_aborts(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        self._electron_dir(tmp_path, monkeypatch)

        mock_fail = MagicMock(returncode=1, stdout="", stderr="npm ERR!")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_fail)
        _setup_electron()
        out = capsys.readouterr().out
        assert "npm install failed" in out

    def test_electron_builder_failure_aborts(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        self._electron_dir(tmp_path, monkeypatch)

        call_count = {"n": 0}

        def _side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="Error: build failed")

        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", _side_effect)
        _setup_electron()
        out = capsys.readouterr().out
        assert "Electron build failed" in out

    def test_app_not_found_after_build(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "arm64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        self._electron_dir(tmp_path, monkeypatch)

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        out = capsys.readouterr().out
        assert "KiroCrew.app not found" in out

    def test_successful_install(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "arm64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        electron_dir = self._electron_dir(tmp_path, monkeypatch)
        app_dir = electron_dir / "dist" / "mac-arm64" / "KiroCrew.app" / "Contents"
        app_dir.mkdir(parents=True)
        (app_dir / "Info.plist").write_text("<plist></plist>")

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        out = capsys.readouterr().out
        assert "installed to ~/Applications" in out
        assert (tmp_path / "Applications" / "KiroCrew.app" / "Contents" / "Info.plist").exists()

    def test_replaces_existing_app(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "arm64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        electron_dir = self._electron_dir(tmp_path, monkeypatch)
        app_dir = electron_dir / "dist" / "mac-arm64" / "KiroCrew.app" / "Contents"
        app_dir.mkdir(parents=True)
        (app_dir / "Info.plist").write_text("<new>")

        old_app = tmp_path / "Applications" / "KiroCrew.app" / "Contents"
        old_app.mkdir(parents=True)
        (old_app / "Info.plist").write_text("<old>")

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        installed = tmp_path / "Applications" / "KiroCrew.app" / "Contents" / "Info.plist"
        assert installed.read_text(encoding="utf-8") == "<new>"

    def test_x86_arch_fallback(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup.Path.home", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.cli_setup.platform.system", lambda: "Darwin")
        monkeypatch.setattr("kiro_crew.cli_setup.platform.machine", lambda: "x86_64")
        monkeypatch.setattr("kiro_crew.cli_setup.shutil.which", lambda _: "/usr/local/bin/node")
        electron_dir = self._electron_dir(tmp_path, monkeypatch)
        app_dir = electron_dir / "dist" / "mac" / "KiroCrew.app" / "Contents"
        app_dir.mkdir(parents=True)
        (app_dir / "Info.plist").write_text("<x86>")

        mock_ok = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("kiro_crew.cli_setup.subprocess.run", lambda *a, **kw: mock_ok)
        _setup_electron()
        out = capsys.readouterr().out
        assert "installed to ~/Applications" in out


class TestSetupElectronOnly:
    """kirocrew setup --electron-only dispatches correctly."""

    def test_electron_only_calls_setup_electron(self, capsys, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup._setup_electron", lambda: None)
        _setup(electron_only=True)
        out = capsys.readouterr().out
        assert "Desktop App" in out

    def test_electron_only_returns_early(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.cli_setup._setup_electron", lambda: None)
        with patch("kiro_crew.agent.install_agent") as mock_agent:
            _setup(electron_only=True)
        mock_agent.assert_not_called()
